# 已有会话消息

0.4.0 通过客户端已有的会话与消息服务查询记录，和实时通知共用本地归档。没有按群聊筛选，私聊及其他会话也使用同一条查询路径。

启动后列出会话，每页查询 100 条消息；完成首轮后按 `lastMid` 检查变化。记录包含会话标题、消息 ID、发送者 ID、原始时间、类型与内容。非文本内容保留客户端的文本或序列化结果，不下载附件文件。

查询范围受客户端实际返回的数据、保留期和以下上限影响，不表示服务端完整历史都已下载：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `retention_days` | 30 | 最早保留到多少天前，最大 36500 |
| `history_max_conversations` | 500 | 每轮最多列出多少个会话，最大 5000 |
| `history_messages_per_conversation` | 1000 | 每个会话一次补采上限，最大 10000 |
| `history_interval_seconds` | 30 | 检查会话变化的间隔，最小 5 秒 |

`bridge_status` 的 `history_state` 显示正在列会话、查询消息或等待变化；`history_errors` 和 `history_truncated` 显示失败及触及上限的次数。已有消息按同一个 CID/MID 去重，历史结果会补齐实时记录的标题、时间和类型。

更改补采配置后，先执行 `Stop-Native.ps1`，再重连 MCP，使后台采集器加载新配置。

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
| `Milliseconds` / `Now` | 将服务返回时间与归档保留期比较。 |
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

原生查询使用静态确认的 MSVC x64 容器及回调 ABI；入口或布局不兼容时返回明确状态。原始反汇编及现场消息不进入源码仓库。
