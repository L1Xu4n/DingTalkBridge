# 已有会话消息

通过客户端已有的会话与消息服务查询记录，和实时通知共用本地归档。没有按群聊筛选，私聊及其他会话也使用同一条查询路径。

启动后列出会话，每页查询 100 条消息。完成首轮后按 `lastMid` 检查变化。记录包含会话标题、消息 ID、发送者 ID、原始时间、类型与内容。非文本消息保留客户端的文本或序列化结果，附件在调用 `read_attachment` 时按需获取。

0.4.1 起取消保留期、会话总数和每会话消息总数限制。会话列表逐步扩大查询范围，消息按客户端游标持续翻页，直到返回结束；失败的会话会在下一轮重试。客户端未返回的记录仍无法由桥接器补出，不表示服务端完整历史都已下载。

旧配置中的 `retention_days`、`history_max_conversations`、`history_messages_per_conversation` 会被忽略。已经被旧版清理的消息，需要客户端再次返回才能重新入库。

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `history_interval_seconds` | 30 | 检查会话变化的间隔，最小 5 秒 |
| `max_tool_results` | 示例配置 100 | MCP 单页最多返回多少条，最大 200；不限制可翻页的总量 |

`bridge_status` 的 `retention_days=null` 表示不过期；`history_state` 显示正在列会话、查询消息或等待变化，`history_errors` 显示查询失败次数。`history_truncated` 是保留的旧状态字段，新版不再按总数截断。已有消息按同一个 CID/MID 去重，历史结果会补齐实时记录的标题、时间和类型。

从旧版更新时，完整退出并重新打开钉钉，再重连 MCP，以加载新版原生 DLL 和查询接口。更改刷新间隔后，先执行 `Stop-Native.ps1`，再重连 MCP。

`get_messages` 和 `search_messages` 均支持 `cursor`。当 `has_more=true` 时，保留查询条件并将返回的 `next_cursor` 传入下一次调用；到 `has_more=false` 为止。游标同时使用消息时间和唯一序号，时间相同的消息也可完整翻页。单页条数、帧大小和队列容量仍用于控制单次内存占用。

归档绑定当前客户端 UID，切换账号后停止向旧归档写入。新账号需要独立 `data_dir`。UID 只从已初始化的当前用户对象读取，不读取登录凭据。

## 文件与函数

| 文件 / 函数 | 作用 |
| --- | --- |
| `history/HistoryReader.cpp` · `Matches` | 校验查询函数入口，不要求客户端整文件哈希相同。 |
| `Field` / `Integer` | 读取原生字段，按整数解析 ID、游标和时间。 |
| `CopyFields` / `CopyRows` | 在异步回调有效期内复制有界数据。 |
| `Pending::Complete` / `Pending::Fail` | 接收查询结果或失败，唤醒历史采集线程。 |
| `Conversations` | 读取会话列表，并通过客户端自己的析构函数释放返回容器。 |
| `Page` | 查询一页消息，保存异步回调生命周期并设置超时。 |
| `String` / `Frame` | 转义并编码私有 JSONL 消息帧。 |
| `Delay` | 等待下一轮查询，同时响应停止请求。 |
| `ReadAccountUid` | 读取初始化标记和当前 UID，核验已知对象布局。 |
| `Run` | 遍历会话、分页补采、检查变化，记录计数与错误。 |
| `RootDetourCore.cpp` · `AccountValid` | 绑定采集会话的账号，拒绝账号切换后的数据。 |
| `HistoryActive` / `HistoryWorker` | 将历史线程接入现有停止和进程生命周期。 |
| `QueueHistory` | 历史线程等待输出队列容量，不阻塞客户端通知线程。 |
| `Frame` / `Capture` / `Worker` | 复用现有管道，附加账号标识并传输历史帧。 |
| `bootstrap/Controller.cpp` · `Run` / `Summary` | 传入分页设置，输出不含正文的计数并等待历史线程停止。 |
| `native_history.normalize_history` | 校验返回字段，保留原类型、时间和非文本内容。 |
| `native_history.consume_history` | 应用账号与归档策略，只返回处理计数。 |
| `native_receive.native_record_id` | 延续原 ID 规则，让历史和实时记录合并。 |
| `native_receive._parse_event` / `_record` / `consume_events` | 接受可选账号标识，在实时记录入库前核对归档绑定。 |
| `Store.native_account_id` / `bind_native_account` | 读取并维护归档账号，拒绝跨账号写入。 |
| `Store.ingest_native_history` | 插入历史消息，或补齐同一条实时消息。 |
| `Store.status` / `_collector_status` | 展示历史消息数量、账号绑定和补采状态。 |
| `Config.__post_init__` / `load_config` | 校验新增的分页和刷新配置。 |
| `autostart._settings` / `native_controller.run_collect` | 将同一份有效配置传给后台进程和原生控制器。 |
| `native_controller._consume_frame` | 将历史帧交给独立解析器，保留原实时帧入口。 |
| `Verify-Native-Mcp.verify` | 经真实 MCP 检查样本及会话数量，不输出私人正文。 |

## 0.4.1 修改的函数

| 文件 / 函数 | 作用 |
| --- | --- |
| `config.Config.__post_init__` | 只校验仍有效的路径、过滤、单页大小和刷新间隔。 |
| `config.load_config` | 读取配置，并兼容忽略已取消的三项限制。 |
| `Store.__init__` | 打开消息库和索引，不再设置定期清理状态。 |
| `Store.ingest` / `ingest_native_history` | 接收任意年代的消息，保留去重及历史字段补齐。 |
| `Store._where` | 生成会话过滤条件，不再附加日期截止点。 |
| `Store.status` | 报告所有已入库记录，并用空保留期表示不过期。 |
| `Store._page_before` | 按上一页末条消息的时间与序号定位更早记录。 |
| `Store.get_messages` / `search_messages` | 返回当前页及下一页游标，支持连续读完和搜完。 |
| `mcp.tool_definitions` | 向 AI 声明游标参数和翻页方式。 |
| `native_receive.consume_events` | 实时接收不再按原时间或观察时间丢弃旧记录。 |
| `autostart._settings` | 将现行配置传给后台，不再传递已取消限制。 |
| `native_controller.default_controller_path` / `run_collect` | 选择 0.4.1 构建，启动时只传入历史刷新间隔。 |
| `Controller.cpp` · `Run` | 解析控制器参数，不再接受历史日期和累计数量限制。 |
| `RootDetourCore.cpp` · `InitializeReadOnlyDetour` | 验证新版启动协议及刷新间隔，再初始化采集。 |
| `HistoryReader.cpp` · `Conversations` / `Run` | 扩大会话查询至结束，逐页补采全部可返回消息；失败不标记为已完成。 |

删除了 `Store._cutoff`、`Store.prune` 和原生 `Milliseconds`、`Now`，不再执行按年龄清理或过滤。版本号、构建目录和打包名称统一为 0.4.1。测试调整为验证旧消息保留、旧配置兼容及同时间消息分页。

原生查询使用静态确认的 MSVC x64 容器及回调 ABI；入口或布局不兼容时返回明确状态。原始反汇编及现场消息不进入源码仓库。
