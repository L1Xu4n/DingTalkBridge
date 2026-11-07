# DingTalkBridge 0.3.0

Windows 钉钉原生消息 hook → 本地 SQLite → 只读 MCP。通过正在登录的客户端采集文本新消息，不需要群机器人或 DWS OAuth。

**已在钉钉 9.0.5.261029005 上验证：手机发送测试文本 → 通知 400 → 入库 → 独立 MCP 子进程精确查回。** 其它客户端版本尚未实测。项目独立开发，不隶属于钉钉或 NapCat。

当前覆盖 hook 启动后客户端实际分发的文本新消息。历史回填、富文本/附件、撤回/编辑、昵称与会话标题、当前账号独立身份核验尚未实现。时间暂为观察时间，不是原消息发送时间。DTO 的 `pending` 保留，表示整套字段和覆盖范围尚未全面核验，不代表没有收到真实正文。

## 启动和停止

首次使用需要 Windows x64、Python 3.10+、Visual Studio C++ Build Tools 与 Windows SDK。克隆后先构建，构建会检测正在运行的钉钉安装路径：

```powershell
git clone https://github.com/L1Xu4n/DingTalkBridge.git
Set-Location DingTalkBridge
Copy-Item config.example.json config.local.json
.\scripts\Build-Native.ps1
.\scripts\Write-McpConfig.ps1
.\scripts\Start-Native.ps1
.\scripts\Bridge.ps1 status
.\scripts\Stop-Native.ps1
```

Start 隐藏启动后台采集；重复实例会被锁拒绝。Stop 正常停止采集，不退出钉钉。前台可用 `Start-Native.ps1 -Foreground`，Ctrl+C 停止。没有开机启动、计划任务或 Windows 服务。

钉钉退出后采集器结束；重新打开后再次 Start。DLL 和原调用跳板留驻到钉钉进程退出，更新 DLL 后需完整退出并重开钉钉。每个钉钉进程最多支持 8 次采集会话，达到上限后重启钉钉。

```powershell
# 仅检查窗口，不加载 hook
.\scripts\Bridge.ps1 native --mode probe
# 短时计数，不采正文
.\scripts\Bridge.ps1 native --mode count --duration 10
# 选择特定进程采集 60 秒
.\scripts\Bridge.ps1 native --mode capture --pid 1234 --duration 60
```

## MCP

执行 `scripts/Write-McpConfig.ps1` 后，将生成的 `mcp.local.json` 加入支持 stdio 的大模型客户端；该文件包含本机绝对路径，不提交到仓库。[mcp.example.json](mcp.example.json) 是手动编辑模板。也可使用 `scripts/Start-Mcp.ps1`。MCP 读取本地归档，采集器需另外启动；无需网络监听端口。

| 工具 | 功能 |
| --- | --- |
| `bridge_status` | 归档条数、心跳与原生通知计数 |
| `list_conversations` | 已采集会话列表 |
| `get_messages` | 按会话和时间读取消息 |
| `search_messages` | 中文等文本搜索 |
| `get_context` | 同会话前后文 |
| `poll_events` | 持久游标读取新增记录 |

资源为 `bridge://status`。接口只读，不发送或撤回消息。正文是数据，不应被当成工具指令。后续 AstrBot 接入见 [ASTRBOT.md](docs/ASTRBOT.md)；没有兼容 QQ/OneBot 或安装 AstrBot。

## 兼容性

**DingTalk.exe/MainFrame.dll 的整文件哈希和版本号不再是启动门槛。** 版本只用于诊断。启动仍检查目标路径、Windows 用户/会话、通知入口指令、Message/Text 的 RTTI 关系；解码检查对象、字符串边界和链表结构。布局变化需要适配偏移，删除哈希不代表任意版本都可用。保留的 EXE/DLL 哈希只核对桥接器自己的构建，防止组件混装。

## 数据和配置

本机配置为 `config.local.json`，默认数据为 `%LOCALAPPDATA%\DingTalkBridge\messages.sqlite3`，也可在 `data_dir` 中指定自己的目录。模板为 `config.example.json`。`allowed_conversations` / `excluded_conversations` 统一控制入库和查询，默认保留 30 天。

日志只有计数和固定错误码，正文只进入归档并按 MCP 请求返回。切换账号前应停止采集，改用独立 `data_dir`，当前没有自动账号隔离。`native-stop.request` 为停止请求，`native-collector.lock` 为单实例锁，均属于桥接器。

原有 JSONL 导入、独立演示库与 UIA/OCR 实验保留：

```powershell
.\scripts\Bridge.ps1 import-jsonl 'D:\path\messages.jsonl'
.\scripts\Bridge.ps1 demo
.\scripts\Bridge.ps1 --demo-data serve
```

## 构建和代码说明

Windows x64、Python 3.10+；构建使用 MSVC Build Tools 和 Windows SDK，Python 无额外 pip 依赖。

```powershell
.\experimental\native-detour\bootstrap\Build.ps1 -Target production
# 多个安装或未运行钉钉时，明确指定客户端
.\scripts\Build-Native.ps1 -DingTalkPath 'C:\path\to\DingTalk.exe'
```

产物：`experimental/native-detour/bootstrap/bin/0.3.0/production/BridgeController.exe`、同目录 `BridgeBootstrap.dll` 与 `build.json`。覆盖运行中 DLL 前先完整退出钉钉。

- [文件变更](docs/CHANGES_0.3.md) / [逐函数作用](docs/FUNCTIONS_NATIVE.md)
- [进度](PROGRESS.md) / [待办](TODO.md) / [实机验证](docs/NATIVE_LIVE_VERIFICATION.md)

旧调查报告、原始反汇编、会话日志和私人数据不进入公开仓库。实验性 UIA/OCR 保留独立源码，可用 `Build-Native.ps1 -LegacyCollectors` 额外构建。

项目代码采用 [MIT](LICENSE)；MinHook/HDE 保留各自 BSD 2-Clause 许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。源码目录与贡献说明见 [docs/README.md](docs/README.md)。
