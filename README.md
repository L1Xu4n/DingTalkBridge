# DingTalkBridge

让大模型通过 MCP 读取 Windows 钉钉消息，支持消息搜索和会话上下文。

支持补采已有群聊、私聊等会话的消息，并持续同步新消息。提供图片和文档附件的按需读取接口。已测试钉钉 `9.0.5.261029005`。

## 使用

需要 Windows x64、Python 3.10+ 和 Visual Studio C++ Build Tools（含 Windows SDK）。打开并登录钉钉后，运行以下命令。

```powershell
git clone https://github.com/L1Xu4n/DingTalkBridge.git
cd DingTalkBridge
Copy-Item config.example.json config.local.json
python -m pip install -e ".[attachments]"
.\scripts\Build-Native.ps1
.\scripts\Write-McpConfig.ps1
```

MCP 客户端连接时会自动启动采集，已运行的采集器会直接复用。下面两条命令分别用于查看状态和停止采集。

```powershell
.\scripts\Bridge.ps1 status
.\scripts\Stop-Native.ps1
```

钉钉还没打开时，采集器会等待。钉钉退出后可重连 MCP 恢复采集。更新程序前请完整退出钉钉。

消息保存在本机，不按天数过期或自动清理。启动后持续分页补采客户端可返回的历史，不限制会话数和每会话消息总数。切换账号时请换用独立的 `data_dir`。

## 接入 MCP

```powershell
.\scripts\Write-McpConfig.ps1
```

将生成的 `mcp.local.json` 中的配置加入你的 MCP 客户端即可，无需另外启动采集脚本。

支持按会话、时间范围、关键词、发送者和消息类型组合查询，AI 可连续翻页直到读完，也可查看前后文。[查询方式与示例](docs/QUERY.md)

图片返回实际图像，PDF、Word、PPT、Excel 支持按页、内容块、幻灯片或单元格区域读取。`attachment_dirs` 用于指定已下载文件所在目录，Office 页面渲染需要本机安装对应 Microsoft Office 应用。[附件读取说明](docs/ATTACHMENTS.md)

服务器配置使用 `config.local.json`。`attachment_cache_retention_days` 默认 `7`，附件缓存超过最近一次读取后的指定天数会自动清理，设 `0` 关闭。`attachment_download_timeout_seconds` 默认 `120`，控制客户端下载等待和直接下载的网络超时。修改配置后重连 MCP 生效。

[更新记录](docs/CHANGES_0.6.md) · [开发说明](docs/README.md) · [AstrBot 接入](docs/ASTRBOT.md) · [MIT License](LICENSE) · [第三方许可](THIRD_PARTY_NOTICES.md)
