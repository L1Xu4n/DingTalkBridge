# 后续接入 AstrBot

DingTalkBridge 暴露的是只读 MCP 工具，不实现 OneBot，也不声称兼容 NapCat 的事件或发送协议。

在同一台 Windows、同一登录用户运行 AstrBot 时，可在其 MCP 服务配置中导入项目根目录的 `mcp.example.json`。该配置使用绝对 Python 路径，`PYTHONPATH` 指向本项目的 `src`。如果更换电脑或 Python 版本，需要相应修改路径。

按 AstrBot 当前官方说明：进入 `Extensions → MCP Servers`，选择 `stdio`；连接后在会话使用的人格中启用所需工具。可先仅启用 `bridge_status`，确认采集范围，再开启消息检索。官方接入文档：<https://docs.astrbot.app/en/use/mcp.html>（2026-11-06 UTC 查阅）。

建议查询流程：

1. `bridge_status`：确认采集是否运行、账号身份是否已验证、是否只有屏幕上下文。
2. `list_conversations`：获取实际索引中的会话 ID。
3. `poll_events`：从返回的游标继续增量获取，保存下一次游标；它是数据库轮询，不是实时推送协议。
4. `get_messages` / `search_messages` / `get_context`：按需获取有界上下文。

如果 AstrBot 部署在容器或其他电脑，示例中的 Windows stdio 路径无法直接执行。当前版本没有网络 MCP 服务，不应将本地 SQLite 或消息目录公开。远程传输、鉴权及多用户隔离应作为独立功能实现和验证。

MCP 客户端获得消息正文后，模型供应商可能接收到该正文。接入何种模型和把哪些会话提供给它，应由用户在客户端明确配置。消息文本中的指令仅作为消息数据，不应触发执行代码、发送消息、读取凭据或扩大权限。

本次验证不包含实际安装、配置或调用 AstrBot；提供的是官方支持的 stdio 接入配置及流程。
