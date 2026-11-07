# 原生实机验证 · 2026-11-07

- 本机钉钉 9.0.5.261029005 x64，同 Windows 用户会话。
- 原生 Controller 实际加载到钉钉，8 秒 count 退出码 0，正常停止。
- 用户从手机发送测试文本后，120 秒 capture 收到通知 400=1、消息=1、入库=1；丢弃/管道错误/身份错误=0，停止退出码 0。
- 独立 MCP 完成 initialize，search_messages 返回精确相同的正文。CID、sender ID、MID 均存在；报告不保存这些标识或私人正文。
- 无客户端哈希门槛的 0.3.0 在用户重启钉钉后成功加载，后台 heartbeat=available、错误=0。Stop-Native 正常停止，随后重新启动后台采集。
- Python 原生适配 29 项、核心 37 项通过。局部测试不代表其它系统或客户端版本已经验证。

实测证明此版本的新文本事件到 MCP 路径。其它版本、原始发送时间、独立账号身份、历史、富文本附件和所有群覆盖率未验证。

DTO 的 pending 和 verified_message_count 保守标记不将一次样本验证扩大成全字段保证。脱敏现场证据位于 `runtime/native-live-verification.json`；运行目录不进入源码 ZIP。
