# AstrBot LLM 告状机

这个插件会向 AstrBot 注册一个 LLM 工具 `complain_to_receivers`。当 LLM 判断“我要告状”时，可以调用该工具，把告状内容私发给 WebUI 配置里的 UID 列表。

从 v1.1.0 起，插件还内置了自动封禁：同一用户在 x 小时内触发告状达到 y 次后，会被禁止与 Bot 交互 z 小时。

## 安装

把 `astrbot_plugin_tattle` 目录放到 AstrBot 的 `data/plugins/` 下，然后在 WebUI 重载或启用插件。

## 配置

在插件配置里填写：

- `receiver_uids`: 告状接收人的 UID 列表。
- `message_template`: 告状消息模板，可用变量包括 `{accused}`、`{reason}`、`{content}`、`{sender_id}`、`{sender_name}`、`{session_id}`。
- `ban_window_hours`: 违规统计时间窗口 x（小时，支持小数），默认 `1`。
- `ban_max_violations`: 窗口内违规次数上限 y，默认 `3`。设为 `0` 可关闭自动封禁。
- `ban_duration_hours`: 封禁时长 z（小时，支持小数），默认 `12`。
- `ban_message_template`: 封禁提示文案，默认 `您已因违反使用条约而被封禁，剩余时间{banned_time}小时！`，可用变量 `{banned_time}`（剩余封禁小时数，触发封禁那一刻等于 z）。

配置会在工具调用时读取，WebUI 修改后无需重启 Bot。模板只会替换白名单占位符，不支持属性访问或表达式解析；占位符内允许空格，例如 `{ accused }`。

## LLM 工具

工具名：`complain_to_receivers`

参数：

- `content`: 告状正文。
- `accused`: 被告状对象，可留空。
- `reason`: 告状理由，可留空。

当前实现使用触发工具的同一平台 ID，并按 `平台ID:FriendMessage:UID` 构造私聊会话发送消息。QQ/OneBot 私聊场景下，AstrBot 会话通常形如 `default:FriendMessage:123456`。

工具执行结果只返回给 LLM，不会在当前聊天会话额外发送“告状完成”提示；但若本次告状触发了自动封禁，会在当前会话向该用户发送封禁提示。

## 自动封禁

- 每次 LLM 成功调用告状工具，都会给“触发本次告状的消息发送者”记一次违规（按 `平台ID:发送者ID` 区分用户）。
- 同一用户在 `ban_window_hours` 小时的滑动窗口内累计违规达到 `ban_max_violations` 次时，立即封禁 `ban_duration_hours` 小时，同时清空其违规记录，并在当前会话发送封禁提示。
- 封禁期间该用户的所有消息都会被插件以最高优先级拦截（`event.stop_event()`），不会进入 LLM 或其他插件处理：
  - 私聊消息、群聊中 @ Bot 或命中唤醒前缀的消息，会收到 `ban_message_template` 渲染出的提示；
  - 群聊中未唤醒 Bot 的消息则被静默拦截，避免刷屏。
- 封禁到期后自动解除。封禁与违规记录保存在 `data/plugin_data/astrbot_plugin_tattle/ban_state.json`，重启 Bot 不会丢失。
- AstrBot 全局管理员不受封禁拦截，可正常使用指令（避免管理员被误封后失去控制）。
- 修改 z 只影响之后新触发的封禁，不改变已生效封禁的到期时间。

### 管理指令（仅全局管理员）

- `/tattle_bans`: 查看当前被封禁的用户及剩余时长。
- `/tattle_unban <UID>`: 解除指定用户的封禁并清空其违规记录，支持传裸 UID 或 `平台ID:UID`。
