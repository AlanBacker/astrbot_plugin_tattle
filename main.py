from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star


DEFAULT_TEMPLATE = (
    "【LLM 告状】\n"
    "被告状对象：{accused}\n"
    "理由：{reason}\n"
    "内容：{content}\n"
    "来源：{sender_name}({sender_id})\n"
    "会话：{session_id}"
)


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class TattlePlugin(Star):
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
            content(string): Required complaint body that explains what happened and what should be reported.
            accused(string): Optional person, user ID, nickname, or short description of who is being complained about.
            reason(string): Optional reason why this complaint is being made.
        """
        content = self._clean_text(content)
        accused = self._clean_text(accused) or "未说明"
        reason = self._clean_text(reason) or "未说明"

        if not content:
            return "Complaint failed: content must not be empty."

        receivers = self._receiver_uids()
        if not receivers:
            return "Complaint failed: receiver_uids is empty. Configure receiver UIDs in WebUI first."

        platform_id = self._platform_id(event)
        if not platform_id:
            return "Complaint failed: unable to determine the current platform ID."

        message = self._render_message(event, content, accused, reason)
        sent: list[str] = []
        failed: list[str] = []

        for uid in receivers:
            ok, error = await self._send_private_message(event, platform_id, uid, message)
            if ok:
                sent.append(uid)
            else:
                failed.append(f"{uid}: {error}")

        if failed:
            logger.warning("complain_to_receivers partial failure: " + "; ".join(failed))

        summary = f"Complaint finished: {len(sent)} sent, {len(failed)} failed."
        if sent:
            summary += " Sent UIDs: " + ", ".join(sent) + "."
        if failed:
            summary += " Failed details: " + "; ".join(failed)
        return summary

    async def _send_private_message(
        self,
        event: AstrMessageEvent,
        platform_id: str,
        uid: str,
        message: str,
    ) -> tuple[bool, str]:
        session = f"{platform_id}:FriendMessage:{uid}"
        try:
            message_chain = MessageChain().message(message)
            if await self.context.send_message(session, message_chain):
                return True, ""
            context_error = "未找到匹配平台"
        except Exception as exc:
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
            if event.get_platform_name() != "aiocqhttp":
                return "当前平台不是 aiocqhttp"
        except Exception as exc:
            return f"无法读取平台名称：{exc}"

        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前事件没有 OneBot 客户端"

        try:
            await bot.api.call_action(
                "send_private_msg",
                user_id=int(uid),
                message=message,
            )
        except ValueError:
            return "UID 不是纯数字"
        except Exception as exc:
            return str(exc)
        return ""

    def _render_message(
        self,
        event: AstrMessageEvent,
        content: str,
        accused: str,
        reason: str,
    ) -> str:
        template = self.config.get("message_template", DEFAULT_TEMPLATE)
        if not isinstance(template, str) or not template.strip():
            template = DEFAULT_TEMPLATE

        values = _SafeFormatDict(
            accused=accused,
            reason=reason,
            content=content,
            sender_id=self._safe_call(event.get_sender_id),
            sender_name=self._safe_call(event.get_sender_name),
            session_id=getattr(event, "unified_msg_origin", ""),
        )
        try:
            return template.format_map(values)
        except Exception as exc:
            logger.warning(f"告状消息模板渲染失败，使用默认模板：{exc}")
            return DEFAULT_TEMPLATE.format_map(values)

    def _receiver_uids(self) -> list[str]:
        raw = self.config.get("receiver_uids", [])
        values: list[str]
        if isinstance(raw, list):
            values = [str(item) for item in raw]
        elif isinstance(raw, str):
            values = raw.replace(",", "\n").splitlines()
        else:
            values = []

        seen: set[str] = set()
        uids: list[str] = []
        for value in values:
            uid = value.strip()
            if uid and uid not in seen:
                seen.add(uid)
                uids.append(uid)
        return uids

    def _platform_id(self, event: AstrMessageEvent) -> str:
        try:
            platform_id = event.get_platform_id()
            if platform_id:
                return str(platform_id)
        except Exception:
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
    def _safe_call(func) -> str:
        try:
            return str(func())
        except Exception:
            return ""

    async def terminate(self):
        pass
