# 原生采集函数说明

此文件逐项说明本轮接入的主要函数；MinHook 第三方实现保留原有许可证和注释。

## experimental/native-detour/RootDetourCore.cpp

| 函数 | 作用 |
| --- | --- |
| `LONG Read` | 原子读取共享状态，避免读到一半更新的值。 |
| `void Increment` | 原子增加诊断计数，不记录消息正文。 |
| `void Error` | 写入固定错误码和采集状态。 |
| `std::uint64_t Creation` | 获取进程创建时间，防止 PID 被复用后误认进程。 |
| `std::wstring OwnImage` | 获取当前进程的完整程序路径。 |
| `bool SamePath` | 按 Windows 路径规则比较目标路径。 |
| `bool Profile` | 核对目标进程、动态代码策略、入口指令和对象类型布局。 |
| `bool Live` | 确认采集开关和控制器仍有效。 |
| `std::string Stamp` | 生成 UTC 观察时间，不冒充消息发送时间。 |
| `void Queue` | 以容量限制和非阻塞锁保存消息副本。 |
| `void Capture` | 在同步通知生命周期内解析事件，防止重入采集。 |
| `void __fastcall Tap` | 截获目标通知并始终转发一次原始调用。 |
| `void JsonString` | 将字符串按 JSON 规则转义并限制输出大小。 |
| `std::string Frame` | 把消息副本编码成一行本地管道协议。 |
| `bool WriteFrame` | 执行带超时和取消处理的管道写入。 |
| `DWORD WINAPI Worker` | 在独立线程发送队列中的消息副本。 |
| `bool SessionId` | 检查采集会话标识只包含允许字符。 |
| `bool InitializeReadOnlyDetour` | 验证布局后安装 hook；原调用跳板保持进程期有效。 |
| `void DisableReadOnlyCapture` | 关闭消息采集，保留原调用转发和驻留代码。 |

## experimental/native-detour/bootstrap/Controller.cpp

| 函数 | 作用 |
| --- | --- |
| `BOOL WINAPI Control` | 接到终止信号后请求正常停止。 |
| `bool MatchesClient` | 核验管道连接方确实是本次选定的钉钉进程。 |
| `void Summary` | 输出不含正文的结构化运行计数。 |
| `int Fail` | 输出固定错误原因并返回失败码。 |
| `std::uint64_t Number` | 解析 PID 和窗口句柄等无符号数字参数。 |
| `int Run` | 建立用户私有 IPC，注册指定线程 hook，监督采集并正常撤销。 |
| `int wmain` | 程序入口，将未处理异常转为固定失败原因。 |

## experimental/native-detour/bootstrap/BootstrapDll.cpp

| 函数 | 作用 |
| --- | --- |
| `void Fail` | 把初始化失败写入共享状态。 |
| `bool Validate` | 在钉钉进程内独立核验请求、控制器和目标窗口身份。 |
| `DWORD WINAPI Worker` | 在加载器锁外初始化采集，监督控制器和停止请求。 |
| `extern "C" __declspec(dllexport) LRESULT CALLBACK CallWndProcHook` | 接收指定线程的启动消息，再将原窗口通知继续传递。 |
| `HANDLE ConnectVerifiedControllerPipe` | 仅连接进程身份核验通过的本地控制器管道。 |
| `extern "C" __declspec(dllexport) LONG SyntheticState` | 向自有测试宿主提供计数状态，生产构建不包含此接口。 |
| `BOOL WINAPI DllMain` | 记录自身模块地址，避免在加载器锁中执行复杂逻辑。 |

## experimental/native-detour/bootstrap/Identity.h

| 函数 | 作用 |
| --- | --- |
| `inline std::uint64_t TimeValue` | 将 FILETIME 转换为可比较的 64 位数值。 |
| `inline bool SamePath` | 比较两个 Windows 文件路径。 |
| `inline bool X64Process` | 检查进程是否为原生 x64。 |
| `inline bool ReadIdentity` | 读取 Windows 进程路径、SID、会话和完整性级别；不读取钉钉登录凭据。 |
| `inline bool MatchIdentity` | 核对进程身份是否与本次请求一致。 |
| `inline bool SameUserBoundary` | 要求双方位于相同 Windows 用户、会话及完整性级别。 |
| `inline std::wstring HexNonce` | 将随机值转换为 IPC 名称的十六进制后缀。 |
| `inline std::wstring MappingName` | 生成本次采集专属的共享内存名称。 |
| `inline std::wstring PipeName` | 生成本次采集专属的本地管道名称。 |
| `inline bool SafeSessionId` | 校验会话标识及其字符串终止符。 |
| `template<size_t N>inline bool Copy` | 把字符串安全复制到固定长度协议字段。 |
| `template<size_t N>inline bool Terminated` | 确认固定长度字符串存在终止符。 |
| `inline bool RequestStringsValid` | 逐项验证请求中的路径和会话字符串边界。 |
| `inline bool OwnSecurity` | 为 IPC 建立只属于当前 Windows 用户的访问控制。 |
| `inline bool MappingOwnedBy` | 核验共享内存对象的所有者 SID。 |
| `inline bool WindowThreadBound` | 核验顶层窗口仍属于指定进程和线程。 |
| `inline bool LockedTarget` | 限制目标为配置的钉钉程序路径。 |
| `inline bool LockedController` | 限制启动方为本构建的控制器路径。 |

## src/dingtalkbridge/native_target.py

| 函数 | 作用 |
| --- | --- |
| `discover_targets` | Enumerate only window/process metadata; never read titles or message text. |
| `select_target` | Select the largest window in one process; require a PID if several clients exist. |
| `visit` | Keep visible windows whose process image exactly matches this build target. |

## src/dingtalkbridge/native_controller.py

| 函数 | 作用 |
| --- | --- |
| `_fail` | Create a fixed diagnostic that cannot contain captured message text. |
| `_option` | Read an option from either CLI arguments or a test dictionary. |
| `_stamp` | Return the current UTC observation time. |
| `default_controller_path` | Resolve the matching production or synthetic Controller executable. |
| `_sha256` | Hash a local artifact in bounded chunks before launching it. |
| `_unique_object` | Reject duplicate JSON keys instead of silently replacing protocol fields. |
| `_reject_constant` | Reject nonstandard JSON numbers such as NaN. |
| `_json_object` | Decode one size-limited UTF-8 protocol object without exposing its contents. |
| `_validate_build` | Check build flavor, target identity and executable/DLL hashes. |
| `_validate_options` | Validate database, window, duration and session before touching the client. |
| `_status_frame` | Accept only known bounded Controller status fields. |
| `_request_stop` | Request native cooperative cleanup, then use bounded process fallbacks. |
| `_write_status` | Atomically replace the metadata-only heartbeat file. |
| `_consume_frame` | Check session and real/synthetic provenance before writing a message. |
| `_acquire_lock` | Hold a Windows byte lock so two collectors cannot own one data directory. |
| `request_stop` | Ask the current native collector to stop without terminating DingTalk. |
| `run_collect` | Run one caller-requested Controller; return metadata and counts only. |
| `__init__` | Prepare a bounded queue for private Controller stdout frames. |
| `start` | Start the single background frame reader. |
| `_put` | Enqueue a frame or signal backpressure rather than using unbounded RAM. |
| `_read` | Read complete bounded JSONL frames until shutdown or stream failure. |

0.6.4 的 Bootstrap `Worker` 保留核心初始化返回的具体错误码。`run_collect` 在原生拒绝后返回 `requires_client_restart`，由自动采集 worker 等待新的钉钉进程。

## MinHook 初始化

| 函数 | 作用 |
| --- | --- |
| `MH_EnableHook` | 在线程列表变化时最多尝试三轮冻结和核验，每次重试前确认已恢复全部暂停线程，且尚未移动指令指针或写入补丁。 |
| `VerifyThreadSet` | 核验冻结前后的线程集合，测试构建可模拟一次线程变化。 |

## src/dingtalkbridge/native_receive.py

| 函数 | 作用 |
| --- | --- |
| `_reject_constant` | Reject NaN and Infinity in incoming JSON. |
| `_unique_object` | Reject ambiguous duplicate field names. |
| `_utf8_size` | Measure actual UTF-8 bytes and reject invalid Unicode. |
| `_parse_event` | Private DTO validator; callers receive counts rather than parsed bodies. |
| `make_synthetic_event` | Create a tagged fixture; None means unknown original message time. |
| `_record` | Normalize native IDs and preserve unknown timestamps/provenance explicitly. |
| `consume_events` | Stream caller-supplied DTO frames into an already-selected local Store. |
| `__init__` | Keep only a fixed reason code, never event contents. |

## scripts/Verify-Native-Mcp.py

| 函数 | 作用 |
| --- | --- |
| `verify` | Query the real archive over MCP and return only non-content evidence. |
| `main` | Accept the user test marker and optionally save a metadata-only evidence file. |
