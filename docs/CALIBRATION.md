# 消息区域校准（独立实验工具）

`experimental/calibration/` 是可审查的 .NET Framework WinForms 工具。默认和待审核模式只画透明绿色边框、展示说明并保存独立配置，不读 UIA Name/Value、聊天文字或像素，不截图、不启动 OCR。另有严格分开的本机一次读取入口，只有本人当前看框并点击后才可能通过活通道交给独立后端。工具不激活或点击钉钉，不改变钉钉、安全软件或系统设置。

当前没有显示过校准窗口，没有任何本人确认，没有生成可采集授权，也没有接通真实消息。

## 用户的一条命令入口

只有 root/parent 根据最终后端审核明确可以人工确认以后，用户才执行以下命令；请本人把钉钉聊天窗口放到前台，绿框区域不能被终端或其他窗口挡住。脚本不会替用户激活钉钉：

```powershell
& 'D:\Program Files\DingTalkBridge\experimental\calibration\Start-Once.ps1'
```

脚本从自身所属的当前 project 定位编译好的 `native/bin/DingTalkUia.exe`、WGC `MetadataProbe.exe`、`WgcOnce.exe` 和校准 UI，不使用固定 HWND。它刷新完整 `--mode scene`，从本轮报告解析 PID/HWND/ROI，再生成 WGC metadata-only 映射、应用文件身份、`Prepare-Descriptor -OnceBackendReview` 和 backend binding，最后检查 fresh readiness。只有技术/强输入 gate、source/backend/profile hashes 全部通过才调用 `--show-once`。脚本绝不勾选、点击确认、signal event 或发 grant。

配置优先级为显式 `-ConfigPath`、`DINGTALKBRIDGE_CONFIG`、当前 project 的 `config.local.json`；选中的显式/环境路径错误或为空会拒绝，不回落到另一个数据目录。Python 默认采用本机已有 `python.exe`，也可显式传 `-PythonPath`，不安装组件、不自动编译。编译产物、完整场景、alternative proof 或 readiness 不充分时，输出有限失败代码并退出，不显示 UI；人工点击不能解决未完成的技术 gate。

```powershell
# 刷新目标元数据并检查 readiness，始终不显示 UI、不读取像素：
& .\experimental\calibration\Start-Once.ps1 -CheckOnly

# 纯合成参数/优先级检查，不启动子进程、不查询钉钉：
& .\experimental\calibration\Start-Once.ps1 -SelfTest
& .\experimental\calibration\Test-Launcher.ps1
```

新增 launcher 的 5 个负向用例与 9 个纯合成检查已通过，证据在 `experimental/calibration/launcher-test-result.json`。这些测试没有查询实时目标、显示 UI 或发起读取；实际 fresh metadata 到 readiness 的正向链路尚未由本 agent 运行。“命令可运行”不表示真实采集已 ready，仍以 root/platform/review 最终结论为准。

运行期的 scene、WGC 元数据、descriptor、binding 和不含正文的 audit 写入本机临时目录 `DingTalkBridge-once-<随机值>`。脚本 stdout 仅输出有限状态代码，不打印本机路径、PID、聊天正文或 helper 原始异常。一次 UI 关闭后不会自动重试；重新运行会生成全新的元数据和短时描述符。

## 构建与离线检查

在项目目录执行：

```powershell
& .\experimental\calibration\Build.ps1
& .\experimental\calibration\Test.ps1
```

仅使用已安装的 `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe` 和 .NET Framework 自带程序集，不安装软件。编译明确使用 UTF-8。测试使用临时合成元数据、参数检查、取消路径和自建无界面假进程树，不显示 UI，不查询实时钉钉目标。

`Test.ps1` 的证据写入 `experimental/calibration/test-result.json`：28 个参数用例、30 个内部检查、4 个进程生命周期用例均通过，孤儿子进程为 0。生命周期覆盖关闭、异常、子进程不读 stdin 和迟到的启动返回。另有 5 个纯 Python adapter 测试，校验路径/hash 和输出字段，证明消息/图片/未知字段不会被回传到 UI。adapter 仅回传屏幕上下文计数（`ingested_screen_contexts/screen_contexts_added/captured_text_items`），不再用 `messages_added`；`verified_message_count` 只允许 0。

```powershell
python -m unittest discover -s .\experimental\calibration -p test_adapter.py -v
```

产物是 `experimental/calibration/bin/DingTalkRegionCalibration.exe`。直接运行不带参数只打印帮助并退出，不显示窗口。没有 `--confirm` 或其他命令行确认入口。

## 元数据与技术门槛

校准描述符同时绑定两个独立文件的完整 SHA-256：

- `scene`：`DingTalkUia --mode scene` 的完整顶层场景报告，包含自动分类和输入几何元数据。
- `wgc`：仅创建 capture item 的 WGC metadata 报告；`capture_started/frame_pool_created/cpu_image_created/capture_ready` 必须全部为 false。其旧 `selector_proof_stable` 不是完整 scene 证明，不能替代 scene 文件的 hash。

两份元数据必须在描述符生成前后 90/30 秒内生成，描述符 TTL 最长 5 分钟，生成脚本默认 3 分钟。坐标统一为物理屏幕像素。选中 ROI 必须完全落在当前顶层、消息 CEF 和语义聊天宿主 bounds 内，并与两份元数据选择一致。屏幕到纹理的映射也必须相同。

scene 的 `candidate_scene.calibration_gate` 必须包含：

```json
{
  "schema_version": 1,
  "technical_complete": true,
  "technical_errors": [],
  "known_strong_input_geometry_complete": true,
  "known_strong_input_rects_screen": [],
  "semantic_incomplete": true,
  "classified_provider_error_count": 0,
  "unclassified_provider_error_count": 0,
  "semantic_error_codes": ["unknown_semantic"]
}
```

`known_strong_input_rects_screen` 包含已知 Edit、输入、搜索、密码等强输入区域的有效 bbox，任何一个与候选 ROI 相交都会拒绝。技术 error、未分类 error、native-child error、失效 owner、坐标或保护状态不完整都不能用勾选豁免。自动分类不完整只有在全部技术证据完整、error 全部明确归为 `unknown_semantic` 或 `semantic_scan_incomplete` 时，才可以显示校准框。原始 strict masks 不变；未知大容器不会自动变成只读。

当前冻结的 `experimental/accessibility-probe/scene-report.json` 仍不能通过：`technical_complete=false`、`known_strong_input_geometry_complete=false`。三个 error 分别为一个无有效 bbox 的 UIA writable 元素、两个 native owner 不可见的 MSAA 元素；均为技术 error。strict scene 的 108 个 mask 覆盖整个 ROI，`capture_permitted=false`。这份报告不能被制作成“技术已完整”的校准描述符。

一次读取入口另有受控预览模式。`Prepare-Descriptor.ps1 -OnceBackendReview` 和 `--check-once-descriptor` 可以保留 primary=false 的原始报告作为待后端审查输入；它们不改原 gate、不启用 pending-only 确认、不构成授权。UI 仍强验目标、坐标、DWM/WDA、hash 和已知 strong bbox。只有固定 hash 的后端核对 `alternative_technical_evidence` 与原 3 个 error 一对一关联、owner/实际窗口/clip/padding/superset 等证据，拒绝未知/新增 error，并返回 effective technical 和 strong-input geometry 均完整及 fresh proof digest，独立一次读取按钮才可用。UI 不自行解释 alternative 或把 primary false 改 true；证据不足显示禁用原因。

## 描述符 v1

`Prepare-Descriptor.ps1` 仅在技术门槛通过后准备输入，不启动 UI：

```powershell
& .\experimental\calibration\Prepare-Descriptor.ps1 `
  -ScenePath 'ABSOLUTE\scene-report.json' `
  -WgcMetadataPath 'ABSOLUTE\metadata-result.json' `
  -OutputPath 'ABSOLUTE\region.descriptor.json'
```

结构如下，`hwnd` 为无 `0x` 的十六进制字符串，bounds 为 LTRB：

```json
{
  "schema": "dingtalkbridge.region-calibration-descriptor.v1",
  "intent": "pending_semantic_calibration_only",
  "capture_permitted": false,
  "coordinate_space": "physical_screen_pixels",
  "issued_utc": "UTC ISO-8601",
  "expires_utc": "UTC ISO-8601",
  "target": {
    "path": "D:\\Program Files\\DingDing\\main\\current_new\\DingTalk.exe",
    "file_version": "FILE VERSION",
    "file_sha256": "64 lowercase hex",
    "pid": 123,
    "session_id": 1,
    "process_start_utc": "UTC ISO-8601",
    "process_start_filetime": "decimal string, 100ns Windows FILETIME",
    "toplevel": {"hwnd": "101", "class_name": "NATIVE CLASS", "bounds": {"left": 1,"top": 1,"right": 2,"bottom": 2}},
    "semantic": {"hwnd": "102", "class_name": "NATIVE CLASS", "bounds": {"left": 1,"top": 1,"right": 2,"bottom": 2}},
    "message": {"hwnd": "103", "class_name": "CefBrowserWindow", "bounds": {"left": 1,"top": 1,"right": 2,"bottom": 2}}
  },
  "candidate_roi_screen": {"left": 1,"top": 1,"right": 2,"bottom": 2},
  "dwm_frame_bounds_screen": {"left": 1,"top": 1,"right": 2,"bottom": 2},
  "sources": {
    "scene": {"path": "ABSOLUTE JSON PATH", "sha256": "64 lowercase hex"},
    "wgc": {"path": "ABSOLUTE JSON PATH", "sha256": "64 lowercase hex"}
  }
}
```

此片段仅说明字段，尺寸刻意无效，不能用于显示或授权。工具读取源文件核对字段，不能靠手写描述符把错误场景变成有效场景。

```powershell
# 纯文件/参数校验，无实时目标查询、无 UI：
& .\experimental\calibration\bin\DingTalkRegionCalibration.exe --check --descriptor 'ABSOLUTE\region.descriptor.json'

# 只做 Win32 进程/窗口元数据校验，无 UI、无像素：
& .\experimental\calibration\bin\DingTalkRegionCalibration.exe --check-live --descriptor 'ABSOLUTE\region.descriptor.json'
```

实时校验固定完整可执行路径，核对 PID、进程创建时间、session、exe SHA/version、所有 HWND 的 owner/PID/native class/可见性/关系和 GWR；还核对顶层 DWM bounds、cloaking 和 display affinity。没有从 UIA Name、Value 或窗口标题取内容。

## 待审核模式

仅由 root/parent 明确安排人工核对之后，才可以使用 `--show`；本轮没有执行这个命令：

```powershell
& .\experimental\calibration\bin\DingTalkRegionCalibration.exe --show `
  --descriptor 'ABSOLUTE\region.descriptor.json' `
  --output 'ABSOLUTE\region.pending.json'
```

绿色框只覆盖元数据中选择的 ROI，内部透明，框不截屏、不接收输入、不激活钉钉。确认面板明确标为 DingTalkBridge，放在 ROI 外；鼠标操作面板不激活钉钉。确认前要求用户手动将钉钉放在前台，ROI 必须完全位于显示器内，按顶层 Z 序检查不能有其他可见窗口遮挡。自家透明边框仅在句柄、PID、bounds、layered/透明样式和 colorkey 全部匹配时豁免；自家面板不能豁免。

勾选文字是：“框内仅有要读取的聊天记录，不含输入框、搜索框、密码等区域。” 没有默认勾选，不会自动点击。按钮只保存待审核配置，任何 pending 记录都强制：

```json
{
  "schema": "dingtalkbridge.region-calibration-pending.v1",
  "authorization_granted": false,
  "capture_permitted": false,
  "usable": false,
  "applies_automatically": false,
  "ocr_started": false,
  "pixels_read": false
}
```

确认后也不能消费、应用或采集。记录包含确认时间/原文、随机 run id、完整目标绑定、ROI、DWM/source hashes 和短 TTL。它只请求后续 `user_confirmed_roi_once` 的独立审核，`maximum_capture_attempts=1` 不是授权。旧全权授权、agent 写入 true、文件存在都不能替代用户当前本人看框确认。

250ms 检查窗口几何和元数据文件身份。只监听目标 PID 的数字 WinEvent 通知，不查询属性；确认后输入、焦点、状态或结构事件会撤销。确认后的前台/遮挡变化、超时、移动/缩放、目标/源文件变化也会撤销。面板保持打开时仅每秒续写 3 秒的 `live_guard_until_utc`；关闭、取消或失效会覆写为无确认状态，进程意外终止也不能留下长期有效心跳。未来消费者仍必须重新完整扫描 scene，不能只信此监控。

纯取消无需描述符，绝不显示 UI：

```powershell
& .\experimental\calibration\bin\DingTalkRegionCalibration.exe --cancel --output 'ABSOLUTE\region.pending.json'
```

## 一次读取模式的独立边界

独立入口为 `--show-once`，不接受 pending 文件升级，没有 `--grant` 命令。面板明确说明：本机 OCR 一次、文本进入本机桥接供 MCP、原始截图不保存/上传。后端 digest 或技术 readiness 未通过时，读取按钮不可用；技术证明不足、窗口变化、代码校验不一致分别有简明中文原因。

先在最后一次构建/测试之后生成固定代码绑定（不显示 UI，不授权、不读取像素）：

```powershell
python .\experimental\calibration\Prepare-BackendBinding.py `
  --config 'ABSOLUTE\config.local.json' `
  --output 'ABSOLUTE\local.binding.json'
```

绑定 schema 为 `dingtalkbridge.roi-once-backend-binding.v1`，固定 `python_exe/python_sha256`、`adapter_script/adapter_sha256`、`ui_exe/ui_sha256`、`config_path/config_sha256` 和 `expected_backend_digest`。backend digest 由 `ocr.wgc_once_backend_info()` 提供，不包含绑定本身，避免循环。任何重编译/源码或配置修改后必须重建绑定；这里的绑定文件也不是采集授权。

以下命令仅供未来 root/parent 安排当前本人核对后使用，本轮没有运行：

```powershell
& .\experimental\calibration\bin\DingTalkRegionCalibration.exe --show-once `
  --descriptor 'ABSOLUTE\region.descriptor.json' `
  --backend-binding 'ABSOLUTE\local.binding.json' `
  --output 'ABSOLUTE\result.once-audit.json'
```

两种模式的按钮不能相互替代。一次读取的同意只存在于当前 UI 与已固定 hash 的 adapter/backend 的匿名 stdin/stdout 活通道里：

一次读取的勾选文字额外要求本人确认：“我确认只读取一次，并写入本机桥接供 MCP。” 旧的待审核勾选或 pending 文件不能替代这次动作。

1. 可见面板上的真实 Click handler 才能启动一次 attempt。没有命令行/文件确认入口。
2. UI 创建两个未 signal 的 auto-reset 匿名 event，分别供 Python/C++ 消费一次；另建 manual-reset revocation event。子进程只能继承 SYNCHRONIZE 权限，不能自行 SetEvent。
3. adapter 的 `--ui-managed` 入口在导入 bridge/backend 或创建 helper 前等待 `ui_job_ready`。UI 在 worker 上启动子进程，给它分配 non-inheritable、`KILL_ON_JOB_CLOSE` 的 Job 成功后才发送这条 preface，确保整个 helper 子树都在 Job 内。
4. backend 核验 OS 父进程 PID/创建时刻/固定路径/hash，发送 `dingtalkbridge.roi-once-challenge.v1` challenge。UI 不信任任意离线 true。
5. Click handler 再核验当前窗口、foreground、无遮挡、source/code hashes 和 challenge binding。之后隐藏自家面板/边框，重新记录前台和 `GetLastInputInfo` 数字基线；不激活钉钉。
6. UI 仅在这一路径 signal 两个 auto-reset event 各一次，发送 `dingtalkbridge.roi-once-live-grant.v1`。内容包含 challenge、32 字节 CSPRNG nonce、最长 15 秒 UTC/FILETIME TTL、profile/backend/scene hashes、issuer PID/start FILETIME、foreground HWND 和 last-input tick。`authorization_granted=true` 仅在这条当前活通道消息中，绝不写授权文件。
7. 后端必须在任何 frame pool 前消费 event 和内存 CAS，并复核当前技术证据。取消、失败、超时、失焦、输入或几何变化即撤销，不重试、不恢复、不 watch。只有后端 summary `status=completed` 才表示已成功入库。

UI 在等待结果时保持进程/stdin 存活，并持续监测；收到撤销先 signal revocation，再有界关闭自己的 Job。取消不向可能堵塞的 stdin 同步写入，管道不可写也不会卡住清理。grant 写入及运行等待有超时；迟到的 Process.Start 返回会立即接入 Job 并清理，无 signal。finally 清理不依赖 backend 自行响应，且不终止钉钉。

`result.once-audit.json` 只保存状态、目标/hash 等审计元数据，`authorization_granted=false/usable=false/audit_only_not_authorization=true`，不含聊天文字、图片或可重用 grant。UI 只接收 adapter 白名单化的状态/计数；原始异常不回显。

`BackendAdapter.py` 只做已审查函数的薄封装：readiness 不创建 Store；run-once 调用 `ocr.run_user_confirmed_once`。后端仍对事件、父链和 profile 独立校验，UI JSON 不能绕过后端 gate。当前没有显示 UI、signal 任何真实读取 event、发送/消费活 grant 或调用真实采集。窗口的实际显示、鼠标交互、前台/遮挡检查尚未在用户桌面实测，这是 root 明确要求本轮不弹窗的边界。
