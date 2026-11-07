# DingTalkBridge

让大模型通过 MCP 读取 Windows 钉钉消息，支持消息搜索和会话上下文。

目前支持启动后的文本新消息，暂不支持历史消息和附件。已测试钉钉 `9.0.5.261029005`。

## 使用

需要 Windows x64、Python 3.10+ 和 Visual Studio C++ Build Tools（含 Windows SDK）。先打开并登录钉钉，再运行：

```powershell
git clone https://github.com/L1Xu4n/DingTalkBridge.git
cd DingTalkBridge
Copy-Item config.example.json config.local.json
.\scripts\Build-Native.ps1
.\scripts\Write-McpConfig.ps1
```

MCP 客户端连接时会自动启动采集，已运行的采集器会直接复用。查看状态或手动停止：

```powershell
.\scripts\Bridge.ps1 status
.\scripts\Stop-Native.ps1
```

钉钉还没打开时，采集器会等待。钉钉退出后可重连 MCP 恢复采集。更新程序前请完整退出钉钉。

消息保存在本机，默认保留 30 天，时间按采集时间记录。配置在 `config.local.json`；切换钉钉账号时，请先停止采集并换用独立的 `data_dir`。

## 接入 MCP

```powershell
.\scripts\Write-McpConfig.ps1
```

将生成的 `mcp.local.json` 中的配置加入你的 MCP 客户端即可，无需另外启动采集脚本。

提供会话列表、消息读取、关键词搜索、前后文查询和增量消息接口。

[开发说明](docs/README.md) · [AstrBot 接入](docs/ASTRBOT.md) · [MIT License](LICENSE) · [第三方许可](THIRD_PARTY_NOTICES.md)
