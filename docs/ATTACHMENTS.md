# 附件内容读取

通过 `list_attachments` 找到消息中的图片、文件和目录，用 `read_attachment` 读取内容。`list_group_files` 提供群钉盘根目录和子目录查询。当前版本为 0.6.3。

实机已验证普通受保护图片、PDF、PPTX、XLSX、本地 Word，以及 1793×105 的卡片内嵌原图、群根目录分页、目录中的两个 Word 文件。更新原生组件前完整退出钉钉，包括托盘进程，重新打开后重连 MCP。

## 返回给模型的内容

图片使用 MCP `image` 内容块携带实际图像。文档提供文字、表格和页面图像，并保留原始字节。`view=original` 返回带 MIME 类型的二进制资源，客户端如何将它交给模型由客户端决定。

| 格式 | 默认读取内容 | 需要视觉信息时 | 定位方式 |
| --- | --- | --- | --- |
| PNG / JPEG / WebP 等图片 | 实际图片内容块，附原始及显示尺寸 | 多帧图片可以继续翻页，原文件保留 | 帧编号 |
| PDF | 按页提取文字并返回页面图像 | 查看指定页的图表、公式、版式和扫描内容 | 页码 |
| DOCX | 按阅读顺序返回标题、段落、列表、表格与嵌入图片 | 通过本机 Word 转为 PDF 页面图像 | 内容块序号 |
| PPTX | 按幻灯片返回文字、表格、备注和可提取的图表数据 | 返回选定幻灯片的完整渲染图，保留排版和图形关系 | 幻灯片编号 |
| XLSX / XLSM | 列出工作表与范围，返回指定区域的单元格坐标、类型、值、公式和已有缓存值 | 通过本机 Excel 渲染工作表或指定区域 | 工作表名 + A1 范围 |
| XLS | 返回单元格坐标和缓存值 | 通过本机 Excel 渲染页面 | 工作表名 + A1 范围 |
| DOC / PPT | 通过本机 Office 转为 PDF 后读取 | 转换后的页面图像 | 转换后的页码 |
| CSV / TXT 等文本文件 | 按 UTF-8 或 GB18030 解码后分块返回文本 | 读取文本内容 | 文本块游标 |

扫描 PDF 可通过视觉模型读取页面图像，文档 OCR 待接入。Excel 的公式和缓存值分开返回，缺少缓存值时为空。

## AI 调用方式

先通过消息查询取得 `message_id`。

1. `list_attachments(message_id)` 列出消息关联的附件，返回稳定附件 ID、名称、格式、大小和可获取状态。
2. `read_attachment(attachment_id, view, pages, sheet, cell_range, cursor, limit)` 取得附件内容。`view` 使用 `auto`、`text`、`image` 或 `original`，默认每次读取 1 个页面、内容块、幻灯片或表格行，`limit` 可设为 1 到 20。
3. `list_group_files(conversation_id, folder_id, cursor, limit)` 列出群钉盘。省略 `folder_id` 查询根目录，进入目录时传入它的 `attachment_id`。按 `next_cursor` 继续翻页，返回的文件同样使用 `read_attachment` 读取。

目录返回 `kind=directory`、`availability=directory` 和 `list_on_read=true`。对目录调用 `read_attachment` 的默认 `auto` 视图会列出子项，再读取需要的文件。目录列表的游标保持为不透明字符串。群文件通过当前账号的钉盘服务查询，包含聊天归档没有独立附件记录的文件。

`availability=download_required` 表示尚需获取字节，`download_on_read=true` 表示调用 `read_attachment` 会由 MCP 下载。AI 直接调用读取工具，MCP 在当前账号下完成下载和解析，再返回图片内容块、文字、单元格或原文件资源。下载地址保留在 MCP 内部。下载尚在进行时按返回提示重试同一个读取请求。

默认 `view=auto` 返回图片图像、PDF 页面文字和图像、DOCX / PPTX 的结构化内容与嵌入图片，以及 Excel 工作表结构和单元格。DOC / PPT 先转为 PDF。`view=image` 可读取 Office 页面图像，需要安装对应 Microsoft Office 应用。`view=text` 只取文字，扫描页会给出视觉读取或 OCR 提示。

`pages` 使用从 1 开始的选择，例如 `1-3,5`。图片的 GIF / TIFF 等多帧内容也按此方式选择和翻页。Word 使用内容块片段编号，PPT 使用幻灯片编号。Excel 使用 `sheet` 和 `cell_range`，例如 `A1:H60`，按行翻页。默认 Excel 范围为前 30 列，更多列通过指定范围读取，结果会注明剩余列。

图像显示最长边为 2048 像素，结果标明原始尺寸及显示尺寸，原文件保存在私有缓存。单次原文件资源返回上限为 5 MiB，较大的文档使用正文分页及页面图像读取。旧 XLS 返回缓存值，XLSX 同时提供公式与已有缓存值。

每一批都保留附件 ID、原消息 ID 和页码、幻灯片号或单元格坐标。结果中的 `has_more=true` 表示还有内容，保持选择范围不变并传入 `next_cursor` 即可继续。

用户可以直接向 AI 提出读取请求。

- “打开这条消息中的图片，看一下里面的流程图。”
- “读取这个 PDF 的第 3 到 5 页，解释其中的表格。”
- “总结这个 Word 文档，并列出涉及验收的条款位置。”
- “查看 PPT 第 8 页的图表和演讲备注。”
- “读取 Excel 的预算表 A1:H60，保留公式和单元格位置。”

## 获取真实附件

原生消息中使用以下附件定位字段。

- 图片使用消息 `content` 中的 `mediaId`、`authMedia.authMediaId`、`url`、`filename` 和尺寸字段。
- 文件的 `attachments[].extension` 是 JSON 字符串，解析后包含 `f_id`、`s_id`、`f_name`、`f_size`、`f_type` 等字段。
- 富文本卡片的 `extension.payloadV2.contents[].text.items[]` 使用 `type=image`，图片定位在 `data`，尺寸在 `style`；旧版 `payload.items[]` 使用 `type=img` 和 `value`。两种表示合并去重，优先使用授权媒体 ID。
- `content_type=503` 表示目录引用，附件内部的 `type` 可能为 `0`。目录按空间与文件 ID 枚举，`f_count` 保留为客户端字段，子项数量以实际列表为准。
- 部分记录还带 `attachments[].filepath`；已确认存在可用本地文件的样本，但不能假定每个附件已下载。

读取时先检查已验证的缓存和配置下载目录中的文件，再尝试允许的媒体地址，必要时交给当前账号的客户端下载服务。链接失效或权限不足时返回下载状态。

受保护图片曾在直接请求时返回 HTTP 403，因此原生层同时接入了客户端的文件下载和媒体下载服务。下载请求来自已有消息，并绑定当前账号。客户端完成前返回可重试状态，失败时返回诊断码。

在 `config.local.json` 的 `attachment_dirs` 中填写实际下载目录，可复用其中名称、大小匹配的文件。图片还会检查尺寸。示例配置使用当前用户的 Downloads 目录。

下载结果存入当前账号使用的数据目录，缓存目录由附件身份哈希命名。缓存清单保存原文件的内容哈希，Office 渲染结果也会复用。MCP 使用归档生成的附件 ID 定位文件，下载地址和授权字段留在内部。

原生请求使用固定 LF 换行，避免 Windows 文本换行使账号标识校验失败。客户端下载结果以文件已关闭为完成条件，消息中记录的旧文件大小与客户端返回的实际大小可能不同，结果分别保留 `size` 和 `bytes`。

卡片图片按图片自身的授权媒体 ID 下载。群文件使用钉盘下载管理器及群会话上下文，支持从目录列表取得、没有单条文件消息的条目。临时地址、空间定位和授权参数保存在桥接内部。异步完成后再次检查账号，再发布可读取的文件。

目录查询单次等待 8 秒，超时保留请求，使用原参数重试。完成的目录元数据缓存 15 秒。群文件的定位清单与内容沿用附件缓存清理规则，过期后重新列出即可取得条目。
重新列出文件时，客户端返回的修改时间和文件标识参与缓存身份校验，同名、同大小的文件更新也会使旧缓存失效。

## 缓存和下载配置

在 MCP 所用的 `config.local.json` 中设置，旧配置自动使用下列默认值，修改后重连 MCP。

```json
{
  "attachment_cache_retention_days": 7,
  "attachment_download_timeout_seconds": 120
}
```

`attachment_cache_retention_days` 接受 0 到 36500 的整数天数，`0` 关闭自动清理。MCP 启动时清理一次，运行期间每小时检查。实际读取附件会刷新保留时间，附件列表查询只检查状态。旧缓存按目录及文件的最近修改时间判断。

清理范围为数据目录下 `attachments` 中的附件缓存子目录，覆盖原始字节、`incoming-*` 下载副本和 Office 渲染文件。配置的 `attachment_dirs` 下载源和消息归档继续保留。读取和清理使用跨进程锁协调，下载期间的新文件时间也参与过期判断。清理后，再次读取会按现有流程获取附件。

`attachment_download_timeout_seconds` 接受 1 到 3600 的整数秒数，用于单次等待钉钉客户端下载，以及直接媒体下载的网络 I/O 超时。等待超时返回可重试状态，客户端下载继续运行；后续调用同一个附件 ID 可以接着等待。未响应请求的重新提交间隔至少为两次等待时长或 120 秒。

## 实现拆分

| 模块 | 职责 |
| --- | --- |
| `attachments.py` | 从已允许访问的消息解析附件，检查本地下载目录，获取媒体或提交客户端下载请求，缓存完整原件。 |
| `attachment_cache.py` | 协调附件读写和过期清理，在 MCP 生命周期内定期回收私有缓存。 |
| `attachment_readers.py` | 按格式提取文本、表格、幻灯片、工作表及图片，提供分页和范围读取。 |
| `group_files.py` | 查询群空间、按游标列出目录，保存私有文件定位并复核账号及会话范围。 |
| `mcp.py` | 声明附件和群文件工具，返回实际 `image` / `text` / 二进制资源内容块。 |
| 原生客户端接入层 | 在现有账号绑定与生命周期内取得有效媒体 / 钉盘附件下载信息。 |

安装 `.[attachments]` 会提供 Pillow、pypdf、pypdfium2、python-docx、python-pptx、openpyxl 和 xlrd。DOC、PPT 及 Office 页面渲染另需本机 Microsoft Office。转换在隐藏子进程中运行，超时为 90 秒。对应 Office 应用已有打开文档时，转换会返回失败状态。

MCP 在工具结果、JSON-RPC 响应和 stdio 输出中使用相同的大小边界。普通消息查询为 1 MiB，包含图像或原文件资源的响应为 8 MiB。超过单批上限时会提示缩小读取范围。

## 函数说明

| 函数 | 作用 |
| --- | --- |
| `attachments._object` / `_text` | 解析附件内部 JSON，并只保留符合预期的字符串字段。 |
| `_positive_int` | 规范化附件大小及图片尺寸，兼容缺失或格式错误的客户端字段。 |
| `_digest` | 分块计算文件哈希，兼容 Python 3.10。 |
| `Attachment.public` / `fingerprint` | 分别生成给 AI 的附件元数据和预期图片尺寸，以及用于缓存更新校验的附件身份哈希。 |
| `_message` / `_catalog` / `resolve` | 重新检查会话访问范围，解析图片尺寸及文件定位字段，通过归档生成的 ID 定位附件。 |
| `_embedded_images` | 遍历卡片中的两版富文本图片节点，统一媒体表示并保留尺寸。 |
| `_directory` / `_local_file` | 定位账号私有缓存，并在配置的下载目录内按名称、大小及图片尺寸找到可用文件。 |
| `_image_complete` | 将实际图片尺寸与消息中的尺寸比较，识别模糊预览图并兼容旋转图片。 |
| `_safe_url` / `_Redirect.redirect_request` | 校验媒体下载地址与重定向目标。 |
| `_download` | 按配置的网络 I/O 超时将媒体流式写入临时文件，返回可读的失败原因。 |
| `_download_complete` | 检查客户端是否已完成并关闭目标文件。 |
| `_client_download` | 提交当前账号的媒体或文件下载请求，按配置等待目标文件，拒绝预览图并支持后续重试。 |
| `cached_file` / `fetch` | 校验缓存哈希及图片尺寸，自动下载完整字节后原子更新内容文件与清单；占位地址回退到客户端下载。 |
| `list_attachments` / `read_attachment` | 提供工具入口，返回附件状态或实际内容；读取时锁定缓存并刷新保留时间。 |
| `group_files._conversation` | 每次操作检查归档允许的会话和已绑定账号。 |
| `_write_json` / `_rpc` | 原子保存私有定位，提交有界的原生查询，恢复未完成请求并短时缓存元数据。 |
| `resolve_entry` / `list_group_files` | 解析已列出的群文件 ID，查询群根目录或子目录，返回完整分页信息。 |
| `attachment_cache._try_lock` | 获取非阻塞系统锁，区分锁争用与实际文件错误。 |
| `cache_lock` | 在整个附件下载和读取期间持有跨进程锁，清理时跳过正在使用的缓存。 |
| `_last_used` | 检查现有缓存目录和文件的最新时间，遇到符号链接或 Windows 目录联接时跳过。 |
| `cleanup_cache` | 在已核对的私有缓存范围内清理过期目录，兼容旧缓存并跳过忙碌目录。 |
| `maintain_cache` / 内部 `maintain` | 启动时执行清理，通过后台线程每小时再检查，并在 MCP 退出时停止等待。 |
| `attachment_readers._image` | 处理方向与透明背景，生成实际图像内容块及尺寸信息。 |
| `_pages` / `_batch` / `_chunks` | 校验范围并分页，长文本继续分块读取。 |
| `_pdf` | 提取指定页文字，渲染扫描页及普通 PDF 页面图像。 |
| `_table` / `_docx` | 保留 Word 段落和表格次序，返回所选内容及内嵌图片。 |
| `_shapes` / `_pptx` | 读取分组形状、幻灯片文字、表格、图表数据、图片和备注。 |
| `_value` / `_xlsx` / `_xls` | 返回带单元格坐标的工作表数据，并区分公式和缓存值。 |
| `_convert` / `convert_office.ps1` | 在隐藏的转换过程中将 Office 文件导出为 PDF，按原文件哈希缓存页面；客户端已有打开文档时返回转换失败，避免干扰当前工作。 |
| `attachment_readers.read` | 按格式和视图选择读取器，并支持原文件资源返回。 |
| `mcp._response_limit` / `MCPServer.call_tool` / `serve` | 执行工具，统一媒体和文本的传输上限；服务器运行期间管理缓存清理线程。 |
| `mcp.tool_definitions` / `validate_arguments` / `MCPServer.handle` | 声明参数、校验视图与范围、完成协议握手与调用分发。 |
| `HistoryReader.DownloadMedia` / `DownloadRequests` / `Run` | 使用当前账号的消息和媒体服务下载附件，在持续采集中处理私有下载请求。 |
| `NativeFiles.Export` / `CheckEntry` | 解析客户端 SDK 符号并检查依赖的入口布局。 |
| `Fail` / `RpcSuccess` / `RpcFailure` | 写入固定失败状态，或在账号复核后原子保存有大小上限的查询结果。 |
| `DestroyContext` / `Own` | 清理异步回调状态，使用 SDK 的析构路径释放原生对象。 |
| `Unhex` / `UrlDecode` | 解码私有查询载荷及 SDK 生成的媒体参数。 |
| `QueryFiles` | 通过当前账号的 SDK 查询群空间或目录，只接受这两个固定的读取操作。 |
| `DownloadInlineImage` | 使用卡片图片的授权标识取得原图，并在完成后发布缓存文件。 |
| `DownloadCloudFile` | 使用钉盘下载管理器取得群文件，兼容 SDK 已有的下载缓存。 |
| `native_controller.run_collect` / `Controller.Run` / `RequestStringsValid` | 将私有请求目录传给原生层，并校验协议字符串边界。 |
| `config.Config.__post_init__` / `load_config` | 加载并校验下载目录、缓存天数及下载超时，将环境变量和用户目录转换为实际路径。 |
| `native_controller.default_controller_path` | 选择包含卡片及群文件修复的 0.6.3 原生组件。 |

## 验证范围

已有实机核验覆盖客户端下载的图片、PDF、PPTX、XLSX，以及本地 DOCX 的全文和页面图像。0.6.1 原图样本返回 960×1280 图像。缓存清理、下载等待和旧缓存兼容由现有附件测试覆盖，私人样本和运行报告保存在本地。

0.6.3 在钉钉完整重启、Controller 与驻留 DLL 的加载路径核验后，使用标准 MCP stdio 子进程通过正式常驻采集链验证卡片原图、群根目录四页、目录内两个 Word 的下载与正文，以及另一个目录的四个子项。采集器继续正常运行，详细记录见 `PROGRESS.md`。

旧 Office 格式提供了读取路径，实机覆盖仍需补充。无权限、加密 PDF、过期附件或转换器缺失会返回对应状态。Office 转换关闭文档宏执行。

工具结果与资源格式参见 [MCP 工具结果](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)和 [MCP 资源](https://modelcontextprotocol.io/specification/2025-11-25/server/resources)。
