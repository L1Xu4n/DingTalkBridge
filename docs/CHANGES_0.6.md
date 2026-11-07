# 0.4.1 至 0.6.1 更新记录

2026-11-07，当前源码版本为 `0.6.1`。本次同步包含此前保留在本地的历史补采、消息查询和附件读取改动。

## 0.4.1

- 消息归档持续保留，取消按天数过期和自动删除。
- 历史补采持续读取客户端返回的会话和消息，取消会话总数及单会话消息总数限制。查询失败的会话留待后续重试。
- `get_messages` 和 `search_messages` 支持游标翻页，同一时间的消息也能连续读取。
- 旧配置中的 `retention_days`、`history_max_conversations` 和 `history_messages_per_conversation` 兼容忽略。

## 0.5.0

- 消息读取与搜索支持组合筛选会话、时间、关键词、发送者和消息类型。
- `list_conversations` 支持按标题或 ID 查找会话。`list_message_types` 返回实际类型码、数量和简短样例。
- 修复历史查询在末页毫秒游标重复时反复扫描的问题。

## 0.6.0

- 增加 `list_attachments` 和 `read_attachment`。图片返回 MCP 图像内容块，文件支持读取正文、页面或原始字节。
- 支持 PDF 页面、DOCX 内容块、PPTX 幻灯片、XLSX 单元格与公式，以及 XLS 缓存值。DOC、PPT 和 Office 页面渲染通过本机 Microsoft Office 转换。
- 附件按需从配置的下载目录、媒体地址或当前账号的客户端下载服务获取。下载结果存入私有缓存。
- 普通查询的响应上限为 1 MiB，含图像或原文件资源的响应上限为 8 MiB，长内容通过游标继续读取。

## 0.6.1 与后续配置调整

- 图片下载使用原图分支，按消息尺寸校验缓存。读取旧预览缓存时重新获取完整图片。
- 文件消息中的占位地址可回退到客户端下载。
- `attachment_cache_retention_days` 默认 7 天，设为 `0` 关闭清理。实际读取刷新保留时间，清理时跳过正在使用的缓存。
- `attachment_download_timeout_seconds` 默认 120 秒。等待超时后，可用同一个附件 ID 继续读取。

## 更新与验证

安装附件依赖后，完整退出钉钉，重新构建原生组件，再打开并登录钉钉、重连 MCP。

```powershell
python -m pip install -e ".[attachments]"
.\scripts\Build-Native.ps1
```

已有实机记录覆盖历史入库、组合查询、受保护图片获取，以及 PDF、Word、PPTX、XLSX 的内容读取。0.6.1 的原图分支已通过独立 MCP 返回 960×1280 图片。缓存清理和下载等待配置通过现有附件及配置测试核验，重连 MCP 后生效。

客户端查询仍会出现异常，后台会重试。历史覆盖范围取决于客户端实际返回的数据。扫描 PDF 可返回页面图像，文档 OCR 待接入。切换账号仍需独立 `data_dir`，其他钉钉版本也需要适配与实测。

Personal agent 已完成方案梳理，消息消费、定时触发、主动投递和回复关联仍在待办中。

[查询说明](QUERY.md) · [附件说明与函数表](ATTACHMENTS.md) · [待办](../TODO.md)
