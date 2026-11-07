# Windows OCR 备用采集

本模块只采集当前可见消息显示区域，产生未验证的屏幕上下文。它不注入钉钉、不读取凭据、不访问客户端私有数据库，不切换会话、不滚动、不发送、不标记已读。OCR 不是完整历史 Hook。

## 文件与入口

- src/dingtalkbridge/ocr.py：run_collect(config, store, args=None)，返回不含正文的 summary。
- experimental/ocr/SafeWindowCrop.cs：单一已验证 CEF 消息 HWND 的裁剪 PrintWindow。
- experimental/ocr/Capture-Ocr.ps1：仅管道通信的 Windows PowerShell 5.1 helper，图像和 OCR 仅驻内存。
- experimental/ocr/LocalOcr.ps1：系统 Windows.Media.Ocr 的 PNG → SoftwareBitmap → RecognizeAsync。
- experimental/ocr/Verify-Runtime.ps1：只用合成图片验证运行时，不截图。
- tests/test_ocr.py：几何拒绝、编辑区排除、私密输出隔离、会话绑定、捕获前后复核和超时测试。

部署或打包必须同时包含前三个 helper 源文件（SafeWindowCrop.cs、Capture-Ocr.ps1、LocalOcr.ps1）。从源码运行时读取项目 experimental/ocr；安装包可放入 share/dingtalkbridge/ocr。

## 运行语义

mode 支持 snapshot、probe。默认 snapshot；只有 include_screen_context=True 或 store_screen_context=True 才向 Store.ingest 写入正文。probe 只读取结构几何信息，不截图。公开返回值、状态文件及异常均不含原始 OCR、截图或未筛选的 helper 输出。

调用方必须使用与 MCP 相同的 Config 和 Store；数据库路径不一致时拒绝启动。当前实际配置由 D:/Program Files/DingTalkBridge/config.local.json 指定，data_dir 为用户配置的本地独立目录；不要退回默认路径形成两份不同的库。

正文只通过子进程匿名管道进入 Python 内存，再按显式开关交给 Store.ingest。不得直接运行 Capture-Ocr.ps1 并将 stdout 打印到终端：其协议正文是 Python 的私有输入。原始截图不落盘、不上传、不写 Library；合成运行时验证也只返回统计。

## 安全几何协议

每次 snapshot 都调用现有原生 helper 的 --mode regions。来源必须是经完整映像路径核验的 D:/Program Files/DingDing/main/current_new/DingTalk.exe 进程，并满足：

1. desktop_kind=interactive_default；physical_screen_pixels / per_monitor_v2。
2. semantic Qt host 的 UIA ClassName 为 im_chat::DTIMChatComponent；原生 HWND 复核可见、同 PID。
3. message_hwnd 的原生类为 CefBrowserWindow 或 Chrome_WidgetWin_1。直接后代关系，或明确同属 DingChatWnd 祖先。实际 PrintWindow 目标永远是 message_hwnd，不是祖先/整宿主/桌面。
4. 安全消息裁剪同时包含在 Qt 宿主与消息原生窗口的物理边界内；已确认 composer 排除必须非空，与全部编辑/搜索/密码/unknown-writable/document 排除均不得相交。
5. 可写 Qt sibling 的边界只有在证明不属于被捕获 CEF HWND/子树时才能从该源的 mask 中排除；CEF 源树的未知可写区域仍需屏蔽或直接拒绝。
6. 捕获后、入库前重新调用 regions，比较 PID、HWND、原生边界、全部 mask 与来源证明 SHA256。任何变化或无法重新验证都丢弃 OCR。

原生安全图形 helper 再次独立核验路径、PID、可见性、Qt 语义类、CEF 原生类、共同祖先及即时矩形。它设置的 DPI awareness 仅作用于当前 helper 线程，不修改系统设置。

输入矩形支持原生 x/y/width/height 字典，Python 统一为 left/top/right/bottom。原生协议的 HWND 字符串按十六进制解释；不得当作十进制句柄或复用旧快照中的坐标。

## 捕获、监督与停止

PrintWindow 使用仅有安全裁剪尺寸的 bitmap，并设置 DC viewport 和 clip；不会先生成整窗口或整屏图像。没有桌面 BitBlt、宿主 HWND 回退、隐藏窗口恢复或保护绕过。目标移动、源变化、空白/黑屏或捕获失败即停止。

Python 监督 CreateProcess 启动和每次操作的期限。正常 helper 结束时自行释放位图、WinRT 流及软件位图；停止请求通过本地 stop 文件配合检查，阻塞操作超时则在有界等待后终止该 helper。不会终止钉钉进程。晚启动的 helper 也会被清理。

## 存储与可观测性

记录固定标记：

- source=windows_ocr_screen；metadata.kind=screen_context；is_message=false。
- confidence=0，sourceConfidence=0，numeric_confidence_available=false。系统 OCR 没有数字置信度，0 不表示估算正确率。
- account_identity、sender_identity、conversation_identity 均 unknown；不会推断发送者或钉钉账号。
- timestamp_kind=observation_time；时间是本次观察时间，不是发送时间。
- coverage=cropped_visible_chat_component_only；partial=true；cropped=true；history_available=false。
- evidence_region_counts 只使用 chat_component；保留 OCR 引擎、语言、裁剪、DPI、排除数量、前后复核及来源证明。
- collector_session_id 在记录和 collector-status.json 中完全一致；database_path_hash 绑定本次真实 Store 路径。

OCR API 的 Text 已在本机验证可用；WinPS 5.1 的动态 WinRT OcrLine/OcrWord 集合枚举存在兼容性问题，因此统计明确使用 recognized_text_lines_and_whitespace_tokens。line_count 为识别字符串非空行数，word_count 为空白分隔 token 数，不宣称是语言学词数或引擎词框数。

状态只保存计数、错误阶段、会话、时间和安全来源标记。MCP 是否有真实正文必须通过当前 session_id 的实际 Store/协议读取核验；不能凭返回 status=available、旧数据或键盘提示文字宣称消息采集已完成。

## 验证

- 16 项合成安全测试通过。
- Verify-Runtime.ps1 已通过系统中文 OCR 的完整解码/识别链（1 行、5 个空白 token），未输出文本、未截图。
- 首次真实流程止于请求 UTF-8/BOM 解码，未产生截图；该问题已修复。
- 2026-11-06T18:59:49Z 在 Default 桌面完成一次受限真实尝试：session=ocr-26406a5a0ee44080ab156ec24d5fe822，status=blank_capture，safe_region_count=1，captured_text_items=0，ingested_screen_contexts=0，graceful_stop=true。仅调用已验证 CEF message HWND 的裁剪 PrintWindow；没有可识别像素，没有正文或截图写盘，也没有回退桌面/宿主窗口。当前不能宣称真实聊天正文已读取；受支持 WGC 单窗口路线需要独立验证。

官方依据：[OcrEngine](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine)、[OcrWord](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrword)、[PrintWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-printwindow)。


## WGC 精确子窗口能力测试

2026-11-06 在 Default 桌面，已验证同一 CEF 消息子 HWND 的完整路径、PID、物理矩形和来源证明后，只调用 IGraphicsCaptureItemInterop.CreateForWindow 创建捕获对象，没有创建 framepool 或调用 StartCapture。

结果：device_supported=true；CreateForWindow 返回 0x80070057（E_INVALIDARG），item_created=false。前后完整来源/owner/input/mask 证明一致。没有得到可核对尺寸的捕获对象，没有产生 WGC 图像、OCR 或新增入库记录，没有改隐私设置、捕获边框，也没有尝试宿主窗口或桌面回退。此结论仅针对当次精确 CEF 子 HWND，不表示整台设备缺少 WGC。

独立能力探测不接入生产采集入口，历史现场记录仅保留本地。当前 WGC 实验源码见 [WGC README](../experimental/wgc/README.md)。

接口 ABI 已对照本机 Windows SDK 头文件核验：IGraphicsCaptureItemInterop 继承 IUnknown，IID 3628E81B-3CAC-4C60-B7F4-23CE0E0C3356，CreateForWindow 为第一个新增方法。官方依据：[IGraphicsCaptureItemInterop](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nn-windows-graphics-capture-interop-igraphicscaptureiteminterop)、[CreateForWindow](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow)。

消费端另补齐三项矛盾证明拒绝：不同 PID 必须是明确 renderer=true 的精确 D3D 后代；强输入始终 mask；输入 owner_pid 必须与同一 native HWND 的已验证 owner 一致。16 项 OCR 离线测试通过。

目前真实消息读取仍未达成：PrintWindow 的精确 CEF 来源为空白，WGC 的精确子窗口建项被 API 拒绝。这里停止扩展捕获范围，不能据此把屏幕上下文通道宣称为已读到聊天正文。


## 新顶层 WGC 方案的进展

新策略 wgc_verified_toplevel_gpu_roi_masked 的 metadata-only 对象创建和物理坐标映射成功，纯 synthetic HARDWARE GPU ROI copy/opaque mask/staging Map 测试 0 mismatch。技术方案与证据在 ../experimental/wgc/README.md。

真实 capture 仍未获完整 scene 条件：108 masks 覆盖全部 1,351,350 ROI pixels，unmasked0，provider_errors3 / completefalse。严格门禁返回 no_safe_unmasked_region，未建立实际 framepool 或 StartCapture；这不是成功正文读取或已实现的生产 WGC 后端。


## Live-UI-only WGC once backend (implemented, no real capture validated)

`ocr.wgc_once_backend_info()`, `ocr.wgc_once_readiness(config, profile, backend_binding_path=...)` and `ocr.run_user_confirmed_once(config, store, args)` now provide a separate one-attempt protocol. The native FramePool/GPU masked ROI/ROI-only Map/Windows OCR path compiles; Python private-channel supervision and Store ingestion are implemented. Exact resource/build/launch instructions and verification limits are in [experimental/wgc/README.md](../experimental/wgc/README.md).

The strict all-masked result is unchanged. The manual technical gate validates the classified alternative evidence one-to-one, while an actual current UI confirmation remains mandatory for unknown semantics. Pending/old/offline grants cannot be promoted. Twenty-two offline OCR tests pass, including a real native forged request stopped before capture and a synthetic broker-to-test-Store round trip. Real WGC/OCR/MCP evidence remains unverified and real text collected remains zero.
