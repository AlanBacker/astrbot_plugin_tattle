"""AstrBot plugin that gives the LLM a private complaint-reporting tool."""

import re
from collections.abc import Callable
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star


PRIVATE_MESSAGE_TYPE = "FriendMessage"
TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}")
UID_SEPARATOR_RE = re.compile(r"[\s,;，；]+")
TEMPLATE_KEYS = frozenset(
    {
        "accused",
        "reason",
        "content",
        "sender_id",
        "sender_name",
        "session_id",
    }
)

DEFAULT_TEMPLATE = (
    "【LLM 告状】\n"
    "被告状对象：{accused}\n"
    "理由：{reason}\n"
    "内容：{content}\n"
    "来源：{sender_name}({sender_id})\n"
    "会话：{session_id}"
)


class TattlePlugin(Star):
    """Register an LLM-only tool that privately broadcasts complaints."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.llm_tool(name="complain_to_receivers")
    async def complain_to_receivers(
        self,
        event: AstrMessageEvent,
        content: str,
        accused: str = "",
        reason: str = "",
    ) -> str:
        """Send a private complaint message to the configured receiver UIDs.

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

        receivers = self._parse_receiver_uids(self.config.get("receiver_uids", []))
        if not receivers:
            return (
                "Complaint failed: receiver_uids is empty. "
                "Configure receiver UIDs in WebUI first."
            )

        platform_id = self._platform_id(event)
        if not platform_id:
            return "Complaint failed: unable to determine the current platform ID."

        message = self._render_message(event, content, accused, reason)
        sent, failed = await self._broadcast_complaint(
            event,
            platform_id,
            receivers,
            message,
        )

        if failed:
            logger.warning("complain_to_receivers partial failure: " + "; ".join(failed))

        summary = f"Complaint finished: {len(sent)} sent, {len(failed)} failed."
        if sent:
            summary += " Sent UIDs: " + ", ".join(sent) + "."
        if failed:
            summary += " Failed details: " + "; ".join(failed)
        return summary

    async def _broadcast_complaint(
        self,
        event: AstrMessageEvent,
        platform_id: str,
        receivers: list[str],
        message: str,
    ) -> tuple[list[str], list[str]]:
        sent: list[str] = []
        failed: list[str] = []

        for uid in receivers:
            ok, error = await self._send_private_message(event, platform_id, uid, message)
            if ok:
                sent.append(uid)
            else:
                failed.append(f"{uid}: {error}")

        return sent, failed

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
            if key in TEMPLATE_KEYS:
                return values.get(key, "")
            return match.group(0)

        return TEMPLATE_PLACEHOLDER_RE.sub(replace_placeholder, template)

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
