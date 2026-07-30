"""AstrBot plugin that gives the LLM a private complaint-reporting tool."""

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools


PLUGIN_NAME = "astrbot_plugin_tattle"
PRIVATE_MESSAGE_TYPE = "FriendMessage"
TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}")
UID_SEPARATOR_RE = re.compile(r"[\s,;，；]+")

DEFAULT_TEMPLATE = (
    "【LLM 告状】\n"
    "被告状对象：{accused}\n"
    "理由：{reason}\n"
    "内容：{content}\n"
    "来源：{sender_name}({sender_id})\n"
    "会话：{session_id}"
)

DEFAULT_BAN_WINDOW_HOURS = 1.0
DEFAULT_BAN_MAX_VIOLATIONS = 3
DEFAULT_BAN_DURATION_HOURS = 12.0
DEFAULT_BAN_MESSAGE_TEMPLATE = "您已因违反使用条约而被封禁，剩余时间{banned_time}小时！"
BAN_STATE_FILE = "ban_state.json"
# Must outrank every other handler so banned users are blocked before any of
# them (including this plugin's own commands and the LLM pipeline) can run.
BAN_GATE_PRIORITY = 10000
SECONDS_PER_HOUR = 3600.0


def _format_hours(hours: float) -> str:
    """Format hours for user-facing text: 12.0 -> "12", 11.5 -> "11.5"."""
    rounded = round(max(hours, 0.0), 1)
    if rounded <= 0.0 < hours:
        rounded = 0.1
    text = f"{rounded:.1f}"
    return text[:-2] if text.endswith(".0") else text


class TattlePlugin(Star):
    """Register an LLM-only tool that privately broadcasts complaints."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._violations: dict[str, list[float]] = {}
        self._bans: dict[str, float] = {}
        self._state_lock = asyncio.Lock()
        self._state_path = self._resolve_state_path()
        self._load_state()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=BAN_GATE_PRIORITY)
    async def enforce_interaction_ban(self, event: AstrMessageEvent):
        """拦截被封禁用户的消息，并在其尝试与 Bot 交互时发送封禁提示。"""
        key = self._sender_key(event)
        if not key:
            return
        remaining = await self._get_ban_remaining_hours(key)
        if remaining is None:
            return
        if self._is_global_admin(event):
            return
        if self._should_notify(event):
            yield event.plain_result(self._render_ban_message(remaining))
        event.stop_event()

    @filter.llm_tool(name="complain_to_receivers")
    async def complain_to_receivers(
        self,
        event: AstrMessageEvent,
        content: str,
        accused: str = "",
        reason: str = "",
    ) -> str:
        """Send a private complaint message to the configured receiver UIDs.

        Repeated complaints triggered by the same sender within the configured
        time window automatically ban that sender from interacting with the bot.

        Args:
            content(string): Required complaint body that explains what
                happened and what should be reported.
            accused(string): Optional person, user ID, nickname, or short
                description of who is being complained about.
            reason(string): Optional reason why this complaint is being made.
        """
        content = self._clean_text(content)
        accused = self._clean_text(accused) or "未说明"
        reason = self._clean_text(reason) or "未说明"

        if not content:
            return "Complaint failed: content must not be empty."

        ban_note = await self._apply_violation(event)

        receivers = self._parse_receiver_uids(self.config.get("receiver_uids", []))
        if not receivers:
            return self._append_note(
                "Complaint failed: receiver_uids is empty. "
                "Configure receiver UIDs in WebUI first.",
                ban_note,
            )

        platform_id = self._platform_id(event)
        if not platform_id:
            return self._append_note(
                "Complaint failed: unable to determine the current platform ID.",
                ban_note,
            )

        message = self._render_message(event, content, accused, reason)
        sent, failed = await self._broadcast_complaint(
            event,
            platform_id,
            receivers,
            message,
        )

        if failed:
            logger.warning(
                "complain_to_receivers partial failure: %s",
                "; ".join(failed),
            )

        summary = f"Complaint finished: {len(sent)} sent, {len(failed)} failed."
        if sent:
            summary += " Sent UIDs: " + ", ".join(sent) + "."
        if failed:
            summary += " Failed details: " + "; ".join(failed)
        return self._append_note(summary, ban_note)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tattle_bans")
    async def list_bans(self, event: AstrMessageEvent):
        """查看当前被自动封禁的用户及剩余时长。"""
        now = time.time()
        lines = [
            f"{key}：剩余 {_format_hours((until - now) / SECONDS_PER_HOUR)} 小时"
            for key, until in sorted(self._bans.items())
            if until > now
        ]
        if not lines:
            yield event.plain_result("当前没有被封禁的用户。")
            return
        yield event.plain_result("当前封禁列表：\n" + "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tattle_unban")
    async def unban_user(self, event: AstrMessageEvent, uid: str = ""):
        """解除指定 UID 的封禁并清空其违规记录。"""
        uid = self._clean_text(uid)
        if not uid:
            yield event.plain_result("用法：/tattle_unban <UID 或 平台ID:UID>")
            return
        removed = await self._lift_ban(uid)
        if removed:
            yield event.plain_result("已解除封禁并清空违规记录：" + "、".join(removed))
        else:
            yield event.plain_result(f"未找到与 {uid} 匹配的封禁或违规记录。")

    async def _broadcast_complaint(
        self,
        event: AstrMessageEvent,
        platform_id: str,
        receivers: list[str],
        message: str,
    ) -> tuple[list[str], list[str]]:
        sent: list[str] = []
        failed: list[str] = []

        results = await asyncio.gather(
            *(
                self._send_to_receiver(event, platform_id, uid, message)
                for uid in receivers
            )
        )
        for uid, ok, error in results:
            if ok:
                sent.append(uid)
            else:
                failed.append(f"{uid}: {error}")

        return sent, failed

    async def _send_to_receiver(
        self,
        event: AstrMessageEvent,
        platform_id: str,
        uid: str,
        message: str,
    ) -> tuple[str, bool, str]:
        ok, error = await self._send_private_message(event, platform_id, uid, message)
        return uid, ok, error

    async def _send_private_message(
        self,
        event: AstrMessageEvent,
        platform_id: str,
        uid: str,
        message: str,
    ) -> tuple[bool, str]:
        session = self._private_session_id(platform_id, uid)
        try:
            message_chain = MessageChain().message(message)
            if await self.context.send_message(session, message_chain):
                return True, ""
            context_error = "未找到匹配平台"
        except (
            AttributeError,
            TypeError,
            ValueError,
            RuntimeError,
            OSError,
            NotImplementedError,
        ) as exc:
            logger.exception("context.send_message failed for session=%s", session)
            context_error = str(exc)

        fallback_error = await self._try_aiocqhttp_send_private(event, uid, message)
        if not fallback_error:
            return True, ""
        return False, f"{context_error}; OneBot 兜底失败：{fallback_error}"

    async def _try_aiocqhttp_send_private(
        self,
        event: AstrMessageEvent,
        uid: str,
        message: str,
    ) -> str:
        try:
            platform_name = event.get_platform_name()
        except (AttributeError, TypeError, ValueError, NotImplementedError) as exc:
            logger.exception("Failed to read platform name for OneBot fallback")
            return f"无法读取平台名称：{exc}"

        if platform_name != "aiocqhttp":
            return "当前平台不是 aiocqhttp"

        try:
            user_id = int(uid)
        except ValueError:
            return "UID 不是纯数字"

        try:
            if not await self._call_onebot_action(
                event,
                "send_private_msg",
                user_id=user_id,
                message=message,
            ):
                return "当前 OneBot 客户端不支持 call_action"
        except (
            AttributeError,
            TypeError,
            RuntimeError,
            OSError,
            NotImplementedError,
        ) as exc:
            logger.exception("OneBot send_private_msg failed for uid=%s", uid)
            return str(exc)
        return ""

    @staticmethod
    async def _call_onebot_action(
        event: AstrMessageEvent,
        action: str,
        **payload: Any,
    ) -> bool:
        bot = getattr(event, "bot", None)
        if bot is None:
            return False
        direct = getattr(bot, "call_action", None)
        if callable(direct):
            await direct(action=action, **payload)
            return True
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if callable(call_action):
            await call_action(action, **payload)
            return True
        return False

    def _render_message(
        self,
        event: AstrMessageEvent,
        content: str,
        accused: str,
        reason: str,
    ) -> str:
        values = {
            "accused": accused,
            "reason": reason,
            "content": content,
            "sender_id": self._safe_call(event.get_sender_id),
            "sender_name": self._safe_call(event.get_sender_name),
            "session_id": str(getattr(event, "unified_msg_origin", "")),
        }
        return self._render_safe_template(self._load_message_template(), values)

    def _load_message_template(self) -> str:
        raw = self.config.get("message_template", DEFAULT_TEMPLATE)
        if isinstance(raw, str) and raw.strip():
            return raw
        return DEFAULT_TEMPLATE

    @staticmethod
    def _render_safe_template(template: str, values: dict[str, str]) -> str:
        def replace_placeholder(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in values:
                return values.get(key, "")
            return match.group(0)

        return TEMPLATE_PLACEHOLDER_RE.sub(replace_placeholder, template)

    # ---------------------------------------------------------------- ban 逻辑

    async def _apply_violation(self, event: AstrMessageEvent) -> str:
        """Record one violation for the sender; ban and notify when over limit."""
        key = self._sender_key(event)
        if not key:
            return ""
        duration_hours = await self._record_violation(key)
        if duration_hours is None:
            return ""
        logger.info(
            "tattle: %s banned for %s hours after repeated violations",
            key,
            _format_hours(duration_hours),
        )
        await self._notify_ban(event, duration_hours)
        event.stop_event()
        return (
            "Additionally, the sender has now been banned from interacting "
            f"with the bot for {_format_hours(duration_hours)} hours due to "
            "repeated violations."
        )

    async def _record_violation(self, key: str) -> float | None:
        """Append one violation; return the ban duration if a ban was applied."""
        window_hours, max_violations, duration_hours = self._ban_settings()
        if window_hours <= 0 or max_violations < 1 or duration_hours <= 0:
            return None
        now = time.time()
        async with self._state_lock:
            if self._bans.get(key, 0.0) > now:
                return None
            stamps = [
                stamp
                for stamp in self._violations.get(key, [])
                if now - stamp < window_hours * SECONDS_PER_HOUR
            ]
            stamps.append(now)
            if len(stamps) >= max_violations:
                self._violations.pop(key, None)
                self._bans[key] = now + duration_hours * SECONDS_PER_HOUR
                self._save_state()
                return duration_hours
            self._violations[key] = stamps
            self._save_state()
            return None

    async def _get_ban_remaining_hours(self, key: str) -> float | None:
        until = self._bans.get(key)
        if until is None:
            return None
        now = time.time()
        if until > now:
            return (until - now) / SECONDS_PER_HOUR
        async with self._state_lock:
            stored = self._bans.get(key)
            if stored is not None and stored <= time.time():
                del self._bans[key]
                self._save_state()
        return None

    async def _lift_ban(self, uid: str) -> list[str]:
        def matches(key: str) -> bool:
            return key == uid or key.endswith(f":{uid}")

        async with self._state_lock:
            keys = {key for key in self._bans if matches(key)} | {
                key for key in self._violations if matches(key)
            }
            if not keys:
                return []
            for key in keys:
                self._bans.pop(key, None)
                self._violations.pop(key, None)
            self._save_state()
            return sorted(keys)

    async def _notify_ban(self, event: AstrMessageEvent, remaining_hours: float) -> None:
        message = self._render_ban_message(remaining_hours)
        try:
            await event.send(MessageChain().message(message))
        except (
            AttributeError,
            TypeError,
            ValueError,
            RuntimeError,
            OSError,
            NotImplementedError,
        ) as exc:
            logger.warning("tattle: failed to deliver ban notice: %s", exc)

    def _render_ban_message(self, remaining_hours: float) -> str:
        return self._render_safe_template(
            self._load_ban_template(),
            {"banned_time": _format_hours(remaining_hours)},
        )

    def _load_ban_template(self) -> str:
        raw = self.config.get("ban_message_template", DEFAULT_BAN_MESSAGE_TEMPLATE)
        if isinstance(raw, str) and raw.strip():
            return raw
        return DEFAULT_BAN_MESSAGE_TEMPLATE

    def _ban_settings(self) -> tuple[float, int, float]:
        window = self._config_number("ban_window_hours", DEFAULT_BAN_WINDOW_HOURS)
        max_violations = int(
            self._config_number("ban_max_violations", DEFAULT_BAN_MAX_VIOLATIONS)
        )
        duration = self._config_number("ban_duration_hours", DEFAULT_BAN_DURATION_HOURS)
        return window, max_violations, duration

    def _config_number(self, key: str, default: float) -> float:
        raw = self.config.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(value):
            return default
        return value

    def _sender_key(self, event: AstrMessageEvent) -> str:
        sender_id = self._safe_call(event.get_sender_id).strip()
        if not sender_id:
            return ""
        return f"{self._platform_id(event) or 'unknown'}:{sender_id}"

    @staticmethod
    def _is_global_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except (AttributeError, TypeError, ValueError, NotImplementedError):
            return False

    @staticmethod
    def _should_notify(event: AstrMessageEvent) -> bool:
        try:
            if event.is_private_chat():
                return True
        except (AttributeError, TypeError, ValueError, NotImplementedError):
            pass
        return bool(
            getattr(event, "is_at_or_wake_command", False)
            or getattr(event, "is_wake", False)
        )

    @staticmethod
    def _append_note(text: str, note: str) -> str:
        return f"{text} {note}" if note else text

    # ------------------------------------------------------------- 状态持久化

    def _resolve_state_path(self) -> Path | None:
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except (RuntimeError, ValueError, OSError) as exc:
            logger.warning(
                "tattle: unable to resolve plugin data dir, "
                "ban state will not persist: %s",
                exc,
            )
            return None
        return Path(data_dir) / BAN_STATE_FILE

    def _load_state(self) -> None:
        path = self._state_path
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("tattle: failed to load ban state: %s", exc)
            return
        if not isinstance(raw, dict):
            return

        now = time.time()
        window_hours = self._ban_settings()[0]
        violations = raw.get("violations", {})
        if isinstance(violations, dict):
            for key, stamps in violations.items():
                if not isinstance(stamps, list):
                    continue
                cleaned = [
                    float(stamp)
                    for stamp in stamps
                    if isinstance(stamp, (int, float))
                    and (window_hours <= 0 or now - stamp < window_hours * SECONDS_PER_HOUR)
                ]
                if cleaned:
                    self._violations[str(key)] = cleaned

        bans = raw.get("bans", {})
        if isinstance(bans, dict):
            for key, until in bans.items():
                if isinstance(until, (int, float)) and float(until) > now:
                    self._bans[str(key)] = float(until)

    def _save_state(self) -> None:
        path = self._state_path
        if path is None:
            return
        payload = {"violations": self._violations, "bans": self._bans}
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except OSError as exc:
            logger.warning("tattle: failed to persist ban state: %s", exc)

    # ----------------------------------------------------------------- 工具函数

    @staticmethod
    def _parse_receiver_uids(raw: object) -> list[str]:
        if isinstance(raw, list):
            text = "\n".join(str(item) for item in raw if item is not None)
        elif isinstance(raw, str):
            text = raw
        else:
            return []

        seen: set[str] = set()
        uids: list[str] = []
        for uid in UID_SEPARATOR_RE.split(text):
            uid = uid.strip()
            if uid and uid not in seen:
                seen.add(uid)
                uids.append(uid)
        return uids

    @staticmethod
    def _private_session_id(platform_id: str, uid: str) -> str:
        # AstrBot's public active-send API currently accepts a unified session ID.
        return f"{platform_id}:{PRIVATE_MESSAGE_TYPE}:{uid}"

    def _platform_id(self, event: AstrMessageEvent) -> str:
        try:
            platform_id = event.get_platform_id()
            if platform_id:
                return str(platform_id)
        except (AttributeError, TypeError, ValueError, NotImplementedError):
            pass

        umo = getattr(event, "unified_msg_origin", "")
        if isinstance(umo, str) and ":" in umo:
            return umo.split(":", 1)[0]
        return ""

    @staticmethod
    def _clean_text(value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _safe_call(func: Callable[[], object]) -> str:
        try:
            return str(func())
        except (AttributeError, TypeError, ValueError, NotImplementedError):
            return ""

    async def terminate(self):
        pass
