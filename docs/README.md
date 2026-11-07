# 开发说明

| 目录 | 内容 |
| --- | --- |
| `src/dingtalkbridge` | Python 存储、MCP、CLI 与采集监督 |
| `experimental/native-detour` | 当前原生采集实现，包含本地修改的 MinHook |
| `native` | 早期 UIAutomation 采集器 |
| `experimental/ocr`、`calibration`、`wgc`、`accessibility-probe` | 可见区域采集实验 |
| `scripts` | 构建、启动、停止、配置生成和打包 |
| `tests` | 本地和合成数据验证，不依赖私人聊天数据 |

修改函数时附上用途注释；完成一段开发后同步根目录 `PROGRESS.md` 和 `TODO.md`。不要提交本机配置、聊天消息、用户标识、截图、反汇编导出或编译产物。

原生路径移植涉及 `RootDetourCore.cpp` 的入口与 RTTI profile、`payload_parser.cpp` 的对象布局。保留原分发调用，禁止用“任意版本兼容”代替实测。更新驻留 DLL 前完整退出钉钉。

使用 `scripts/Test.ps1` 运行现有测试。CI 仅运行核心、原生适配和 MCP 合成验收并编译自有 fixture，不连接真实钉钉。

## 本轮新增函数

| 函数/脚本 | 作用 |
| --- | --- |
| `native_controller._manifest_target` | 检查构建目标是绝对路径下的 DingTalk.exe，不限制安装盘。 |
| `native_controller.configured_target_path` | 从本机构建清单读取目标路径，用于窗口发现；未构建时允许只做进程名称探测。 |
| `native_target.discover_targets` | 按本机构建目标筛选窗口，采集前仍由 Controller 独立核验完整身份。 |
| `scripts/Write-McpConfig.ps1` | 生成含本机 Python、源码及数据配置路径的 MCP JSON。 |
| `bootstrap/Build.ps1` | 自动找到编译器/SDK/钉钉，生成只属于本次构建的路径绑定。 |
| `test_cli_environment_config_selects_the_same_explicit_database` | 验证环境变量选择的数据库路径一致，并兼容 Windows 8.3 短路径别名。 |

其它函数见 [FUNCTIONS_NATIVE.md](FUNCTIONS_NATIVE.md)。公开验证范围见 [NATIVE_LIVE_VERIFICATION.md](NATIVE_LIVE_VERIFICATION.md)。
