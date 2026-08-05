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
ONEBOT_PLATFORM_NAME = "aiocqhttp"
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
# Admins bypass the gate by default so an auto-ban can never lock the owner
# out of their own bot. Side effect: testing with an admin account looks like
# the ban "did nothing" — hence the switch and the log line in the gate.
DEFAULT_BAN_EXEMPT_ADMINS = True
DEFAULT_BAN_MESSAGE_TEMPLATE = "您已因违反使用条约而被封禁，剩余时间{banned_time}小时！"
BAN_STATE_FILE = "ban_state.json"
# Must outrank every other handler so banned users are blocked before any of
# them (including this plugin's own commands and the LLM pipeline) can run.
BAN_GATE_PRIORITY = 10000
SECONDS_PER_HOUR = 3600.0

DEFAULT_EVIDENCE_ENABLED = True
DEFAULT_EVIDENCE_COUNT = 5
MAX_EVIDENCE_COUNT = 100
# "forward" ships the records as a QQ 聊天记录 card, "text" inlines them.
EVIDENCE_STYLE_FORWARD = "forward"
EVIDENCE_STYLE_TEXT = "text"
EVIDENCE_STYLES = (EVIDENCE_STYLE_FORWARD, EVIDENCE_STYLE_TEXT)
DEFAULT_EVIDENCE_STYLE = EVIDENCE_STYLE_FORWARD
# One pasted wall of text should not blow up the whole evidence block.
EVIDENCE_TEXT_LIMIT = 300
EVIDENCE_TIME_FORMAT = "%m-%d %H:%M:%S"
EVIDENCE_SEGMENT_LABELS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "reply": "[回复]",
    "forward": "[合并转发]",
    "node": "[合并转发]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
    "markdown": "[Markdown]",
    "poke": "[戳一戳]",
    "share": "[分享]",
    "music": "[音乐分享]",
    "location": "[位置]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}


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
        if self._ban_exempt_admins() and self._is_global_admin(event):
            logger.info(
                "tattle: %s is banned but passes as a global admin "
                "(ban_exempt_admins is on)",
                key,
            )
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

        entries = await self._collect_evidence(event)
        evidence_text = self._format_evidence(entries)
        as_card = bool(entries) and self._evidence_style() == EVIDENCE_STYLE_FORWARD
        # In card mode the records ride along as a separate forward message, so
        # the body carries no inline evidence. The plain-text OneBot fallback
        # cannot send a card, hence the second, inlined rendering.
        message = self._render_message(
            event,
            content,
            accused,
            reason,
            "" if as_card else evidence_text,
        )
        fallback_message = (
            self._render_message(event, content, accused, reason, evidence_text)
            if as_card
            else message
        )
        sent, failed = await self._broadcast_complaint(
            event,
            platform_id,
            receivers,
            message,
            fallback_message,
            entries if as_card else [],
            evidence_text,
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
        fallback_message: str = "",
        card_entries: list[dict] | None = None,
        evidence_text: str = "",
    ) -> tuple[list[str], list[str]]:
        sent: list[str] = []
        failed: list[str] = []

        results = await asyncio.gather(
            *(
                self._send_to_receiver(
                    event,
                    platform_id,
                    uid,
                    message,
                    fallback_message or message,
                    card_entries or [],
                    evidence_text,
                )
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
        fallback_message: str,
        card_entries: list[dict],
        evidence_text: str,
    ) -> tuple[str, bool, str]:
        ok, error = await self._send_private_message(
            event,
            platform_id,
            uid,
            message,
            fallback_message,
            card_entries,
            evidence_text,
        )
        return uid, ok, error

    async def _send_private_message(
        self,
        event: AstrMessageEvent,
        platform_id: str,
        uid: str,
        message: str,
        fallback_message: str = "",
        card_entries: list[dict] | None = None,
        evidence_text: str = "",
    ) -> tuple[bool, str]:
        """Send the complaint body, then the evidence card when there is one."""
        session = self._private_session_id(platform_id, uid)
        ok, context_error = await self._send_text(session, message)
        if not ok:
            # The normal path is down, so a card would fail too — squeeze the
            # records into the single plain message the OneBot fallback can send.
            fallback_error = await self._try_aiocqhttp_send_private(
                event,
                uid,
                fallback_message or message,
            )
            if fallback_error:
                return False, f"{context_error}; OneBot 兜底失败：{fallback_error}"
            return True, ""

        if not card_entries:
            return True, ""
        if await self._send_forward_card(event, uid, card_entries):
            return True, ""
        # The body already went out; deliver the records as plain text so the
        # evidence is never silently dropped.
        logger.warning("tattle: forward card failed for uid=%s, sending text", uid)
        if evidence_text:
            await self._send_text(session, evidence_text)
        return True, ""

    async def _send_text(self, session: str, text: str) -> tuple[bool, str]:
        try:
            if await self.context.send_message(session, MessageChain().message(text)):
                return True, ""
        except (
            AttributeError,
            TypeError,
            ValueError,
            RuntimeError,
            OSError,
            NotImplementedError,
        ) as exc:
            logger.exception("context.send_message failed for session=%s", session)
            return False, str(exc)
        return False, "未找到匹配平台"

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

        if platform_name != ONEBOT_PLATFORM_NAME:
            return f"当前平台不是 {ONEBOT_PLATFORM_NAME}"

        try:
            user_id = int(uid)
        except ValueError:
            return "UID 不是纯数字"

        try:
            dispatched, _ = await self._call_onebot_action(
                event,
                "send_private_msg",
                user_id=user_id,
                message=message,
            )
            if not dispatched:
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
    ) -> tuple[bool, Any]:
        """Return (dispatched, result); dispatched is False if no API exists."""
        bot = getattr(event, "bot", None)
        if bot is None:
            return False, None
        direct = getattr(bot, "call_action", None)
        if callable(direct):
            return True, await direct(action=action, **payload)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if callable(call_action):
            return True, await call_action(action, **payload)
        return False, None

    def _render_message(
        self,
        event: AstrMessageEvent,
        content: str,
        accused: str,
        reason: str,
        evidence: str = "",
    ) -> str:
        values = {
            "accused": accused,
            "reason": reason,
            "content": content,
            "sender_id": self._safe_call(event.get_sender_id),
            "sender_name": self._safe_call(event.get_sender_name),
            "session_id": str(getattr(event, "unified_msg_origin", "")),
            "evidence": evidence,
        }
        template = self._load_message_template()
        rendered = self._render_safe_template(template, values)
        # Templates written before evidence existed still get the block, appended.
        if evidence and not self._template_uses(template, "evidence"):
            rendered = f"{rendered}\n{evidence}"
        return rendered

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

    @staticmethod
    def _template_uses(template: str, key: str) -> bool:
        return any(
            match.group(1) == key
            for match in TEMPLATE_PLACEHOLDER_RE.finditer(template)
        )

    # ------------------------------------------------------------- 证据消息记录

    async def _collect_evidence(self, event: AstrMessageEvent) -> list[dict]:
        """Fetch the most recent messages of this chat as complaint evidence.

        OneBot (aiocqhttp) only — every other platform yields no entries.
        Evidence is decoration: any failure is logged and swallowed so the
        complaint itself still goes out.
        """
        count = self._evidence_count()
        if count < 1:
            return []

        platform_name = self._safe_call(event.get_platform_name)
        if platform_name != ONEBOT_PLATFORM_NAME:
            logger.debug(
                "tattle: evidence skipped, platform %r is not %s",
                platform_name,
                ONEBOT_PLATFORM_NAME,
            )
            return []

        try:
            return await self._fetch_recent_messages(event, count)
        except Exception as exc:  # noqa: BLE001 - never break the complaint
            logger.warning("tattle: failed to fetch message history: %s", exc)
            return []

    async def _fetch_recent_messages(
        self,
        event: AstrMessageEvent,
        count: int,
    ) -> list[dict]:
        group_id = self._numeric_id(self._safe_call(event.get_group_id))
        if group_id is not None:
            action = "get_group_msg_history"
            payload: dict[str, Any] = {"group_id": group_id, "count": count}
        else:
            user_id = self._numeric_id(self._safe_call(event.get_sender_id))
            if user_id is None:
                return []
            action = "get_friend_msg_history"
            payload = {"user_id": user_id, "count": count}

        dispatched, result = await self._call_onebot_action(
            event,
            action,
            **payload,
            **self._routing_params(event),
        )
        if not dispatched:
            logger.warning("tattle: OneBot client has no call_action, evidence skipped")
            return []

        messages = result.get("messages") if isinstance(result, dict) else result
        if not isinstance(messages, list):
            return []
        entries = [entry for entry in messages if isinstance(entry, dict)]
        # go-cqhttp ignores `count` and returns a fixed batch; NapCat honours it
        # but may hand back newest-first. Normalise, then keep the tail.
        if self._is_newest_first(entries):
            entries.reverse()
        return entries[-count:]

    @staticmethod
    def _is_newest_first(entries: list[dict]) -> bool:
        stamps = [
            entry["time"]
            for entry in entries
            if isinstance(entry.get("time"), (int, float))
        ]
        return len(stamps) >= 2 and stamps[0] > stamps[-1]

    async def _send_forward_card(
        self,
        event: AstrMessageEvent,
        uid: str,
        entries: list[dict],
    ) -> bool:
        """Send the records as a QQ 合并转发 card. Returns False if it could not."""
        user_id = self._numeric_id(uid)
        if user_id is None:
            return False
        routing = self._routing_params(event)
        for messages in self._forward_payloads(entries):
            try:
                dispatched, _ = await self._call_onebot_action(
                    event,
                    "send_private_forward_msg",
                    user_id=user_id,
                    messages=messages,
                    **routing,
                )
            except Exception as exc:  # noqa: BLE001 - try the next payload shape
                logger.warning("tattle: send_private_forward_msg failed: %s", exc)
                continue
            if dispatched:
                return True
            return False  # no call_action at all; retrying cannot help
        return False

    @classmethod
    def _forward_payloads(cls, entries: list[dict]) -> list[list[dict]]:
        """Candidate node payloads, most faithful first.

        A node referencing a message id is forwarded by the client itself, so
        images, @ mentions and everything else render exactly like the original
        — this is what the phone client does. Clients that refuse id nodes get
        a second attempt with the segments rebuilt by hand.
        """
        by_reference = [cls._reference_node(e) or cls._rebuilt_node(e) for e in entries]
        rebuilt = [cls._rebuilt_node(e) for e in entries]
        payloads = [by_reference]
        if by_reference != rebuilt:
            payloads.append(rebuilt)
        return payloads

    @staticmethod
    def _reference_node(entry: dict) -> dict | None:
        message_id = entry.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, (int, str)):
            return None
        text = str(message_id).strip()
        if not text or text == "0":
            return None
        return {"type": "node", "data": {"id": text}}

    @classmethod
    def _rebuilt_node(cls, entry: dict) -> dict:
        name, uid = cls._evidence_entry_sender(entry)
        content = cls._rebuild_segments(entry.get("message"))
        if not content:
            content = [{"type": "text", "data": {"text": cls._evidence_entry_text(entry)}}]
        return {
            "type": "node",
            "data": {
                "user_id": uid or "0",
                "nickname": name or uid or "未知用户",
                "content": content,
            },
        }

    @classmethod
    def _rebuild_segments(cls, message: object) -> list[dict]:
        """Re-send the original segments so images and @ survive the forward."""
        if isinstance(message, str):
            text = message.strip()
            return [{"type": "text", "data": {"text": text}}] if text else []
        if not isinstance(message, list):
            return []
        segments = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            rebuilt = cls._rebuild_segment(segment)
            if rebuilt is not None:
                segments.append(rebuilt)
        return segments

    @staticmethod
    def _rebuild_segment(segment: dict) -> dict | None:
        seg_type = str(segment.get("type") or "")
        raw_data = segment.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        if seg_type == "text":
            text = str(data.get("text") or "")
            return {"type": "text", "data": {"text": text}} if text else None
        if seg_type == "at":
            target = str(data.get("qq") or "").strip()
            return {"type": "at", "data": {"qq": target}} if target else None
        if seg_type == "face":
            face_id = str(data.get("id") or "").strip()
            return {"type": "face", "data": {"id": face_id}} if face_id else None
        if seg_type in {"image", "record", "video"}:
            # History gives both a cache name and a fetchable URL; only the URL
            # can be re-uploaded from another context.
            source = str(data.get("url") or data.get("file") or "").strip()
            return {"type": seg_type, "data": {"file": source}} if source else None
        # reply/forward point at messages outside this card, and unknown types
        # risk a rejected payload — the caller falls back to flattened text.
        return None

    @classmethod
    def _format_evidence(cls, entries: list[dict]) -> str:
        if not entries:
            return ""
        lines = [f"——最近 {len(entries)} 条消息——"]
        lines.extend(cls._format_evidence_entry(entry) for entry in entries)
        return "\n".join(lines)

    @classmethod
    def _format_evidence_entry(cls, entry: dict) -> str:
        name, uid = cls._evidence_entry_sender(entry)
        if name and uid:
            who = f"{name}({uid})"
        else:
            who = name or uid or "未知用户"

        stamp = entry.get("time")
        when = (
            time.strftime(EVIDENCE_TIME_FORMAT, time.localtime(stamp))
            if isinstance(stamp, (int, float)) and stamp > 0
            else "--"
        )
        return f"[{when}] {who}：{cls._evidence_entry_text(entry)}"

    @staticmethod
    def _evidence_entry_sender(entry: dict) -> tuple[str, str]:
        raw_sender = entry.get("sender")
        sender = raw_sender if isinstance(raw_sender, dict) else {}
        name = str(sender.get("card") or sender.get("nickname") or "").strip()
        uid = str(sender.get("user_id") or "").strip()
        return name, uid

    @classmethod
    def _evidence_entry_text(cls, entry: dict) -> str:
        text = cls._flatten_message_segments(entry.get("message"))
        if not text:
            text = str(entry.get("raw_message") or "").strip()
        if len(text) > EVIDENCE_TEXT_LIMIT:
            text = text[:EVIDENCE_TEXT_LIMIT] + "…（已截断）"
        return text or "[空消息]"

    @staticmethod
    def _flatten_message_segments(message: object) -> str:
        if isinstance(message, str):
            return message.strip()
        if not isinstance(message, list):
            return ""

        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            seg_type = str(segment.get("type") or "")
            raw_data = segment.get("data")
            data = raw_data if isinstance(raw_data, dict) else {}
            if seg_type == "text":
                parts.append(str(data.get("text") or ""))
            elif seg_type == "at":
                target = str(data.get("qq") or "").strip()
                parts.append("@全体成员" if target == "all" else f"@{target or '?'}")
            else:
                parts.append(
                    EVIDENCE_SEGMENT_LABELS.get(seg_type, f"[{seg_type or '未知'}]")
                )
        return "".join(parts).strip()

    def _evidence_count(self) -> int:
        if not self._evidence_enabled():
            return 0
        count = int(self._config_number("recent_message_count", DEFAULT_EVIDENCE_COUNT))
        if count < 1:
            return 0
        return min(count, MAX_EVIDENCE_COUNT)

    def _evidence_enabled(self) -> bool:
        return self._config_flag("forward_recent_messages", DEFAULT_EVIDENCE_ENABLED)

    def _ban_exempt_admins(self) -> bool:
        return self._config_flag("ban_exempt_admins", DEFAULT_BAN_EXEMPT_ADMINS)

    def _config_flag(self, key: str, default: bool) -> bool:
        raw = self.config.get(key, default)
        if isinstance(raw, str):
            return raw.strip().lower() not in {"", "0", "false", "no", "off"}
        return bool(raw)

    def _evidence_style(self) -> str:
        raw = self.config.get("evidence_style", DEFAULT_EVIDENCE_STYLE)
        style = str(raw).strip().lower()
        return style if style in EVIDENCE_STYLES else DEFAULT_EVIDENCE_STYLE

    @staticmethod
    def _routing_params(event: AstrMessageEvent) -> dict[str, Any]:
        # Mirrors AstrBot's own aiocqhttp calls: needed to route to the right
        # bot account when several OneBot clients share one adapter.
        self_id = getattr(getattr(event, "message_obj", None), "self_id", None)
        return {"self_id": self_id} if self_id else {}

    @staticmethod
    def _numeric_id(value: str) -> int | None:
        value = value.strip()
        return int(value) if value.isdigit() else None

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
