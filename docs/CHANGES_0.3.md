# 0.3.0 文件变更

| 文件或目录 | 改动与作用 |
| --- | --- |
| `experimental/native-detour/` | 恢复原生 hook、Windows bootstrap、私有管道、payload parser 与 MinHook 源码和许可证。 |
| `RootDetourCore.cpp` | 移除客户端文件哈希 gate；校验入口与 RTTI；截获通知 400、复制文本、保留原分发调用。 |
| `bootstrap/Controller.cpp` / `BootstrapDll.cpp` / `Identity.h` | 同用户、同会话、确定进程的加载与 IPC；移除客户端哈希校验。 |
| `bootstrap/Build.ps1` / `BridgeProtocol.h` | 生成 0.3.0 构建，旧哈希字段改为保留空间；仅校验桥接器自身构建完整性。 |
| `src/dingtalkbridge/native_target.py` | 枚举 Default 桌面窗口并选择钉钉主进程，不读取窗口标题和正文。 |
| `native_controller.py` | 构建检查、私有输出监督、消息入库、心跳、停止请求、单实例锁。 |
| `native_receive.py` | DTO 校验、消息规范化、去重和配置过滤；版本/哈希仅作为元数据。 |
| `cli.py` | 新增 native probe/count/capture/stop，补充注释。 |
| `store.py` | 原生计数纳入现有 MCP 状态输出。 |
| `scripts/Start-Native.ps1` / `Stop-Native.ps1` | 前后台启动及正常停止采集，不终止钉钉。 |
| `scripts/Verify-Native-Mcp.py` | 真实 stdio MCP 核对用户测试文本，仅输出验证布尔值和计数。 |
| `tests/test_native_controller.py` / `test_native_receive.py` | 恢复已有测试，适配新 CLI、停止行为与无客户端哈希门槛。 |
| `pyproject.toml` / `__init__.py` / `scripts/Package.ps1` | 版本更新 0.3.0，源码包纳入原生实现并排除私人数据。 |
| `README.md` / `PROGRESS.md` / `TODO.md` / 新文档 | 更新实际状态、使用方法与覆盖边界。 |

各函数见 [FUNCTIONS_NATIVE.md](FUNCTIONS_NATIVE.md)。MinHook 保留许可证及上游信息；没有修改系统防护或钉钉安装文件。
