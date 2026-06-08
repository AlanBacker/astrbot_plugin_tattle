# AstrBot LLM 告状机

这个插件会向 AstrBot 注册一个 LLM 工具 `complain_to_receivers`。当 LLM 判断“我要告状”时，可以调用该工具，把告状内容私发给 WebUI 配置里的 UID 列表。

## 安装

把 `astrbot_plugin_tattle` 目录放到 AstrBot 的 `data/plugins/` 下，然后在 WebUI 重载或启用插件。

## 配置

在插件配置里填写：

- `receiver_uids`: 告状接收人的 UID 列表。
- `message_template`: 告状消息模板，可用变量包括 `{accused}`、`{reason}`、`{content}`、`{sender_id}`、`{sender_name}`、`{session_id}`。

配置会在工具调用时读取，WebUI 修改后无需重启 Bot。模板只会替换这些简单白名单占位符，不支持属性访问或表达式解析；占位符内允许空格，例如 `{ accused }`。

## LLM 工具

工具名：`complain_to_receivers`

参数：

- `content`: 告状正文。
- `accused`: 被告状对象，可留空。
- `reason`: 告状理由，可留空。

当前实现使用触发工具的同一平台 ID，并按 `平台ID:FriendMessage:UID` 构造私聊会话发送消息。QQ/OneBot 私聊场景下，AstrBot 会话通常形如 `default:FriendMessage:123456`。

工具执行结果只返回给 LLM，不会在当前聊天会话额外发送“告状完成”提示。
