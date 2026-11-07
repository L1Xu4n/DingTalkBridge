# AI 查询消息

AI 通过 MCP 查询本地归档，不需要直接打开数据库。用户可以说“查项目群昨天包含预算的消息”，由 AI 将群名和日期转成工具参数。

## 查询流程

1. 调用 `list_conversations`，用 `query` 搜索会话标题或 ID，确定一个或多个会话。
2. 需要筛选类型时调用 `list_message_types`，查看所选范围内实际存在的类型码、数量及简短样例。
3. 用 `search_messages` 搜索关键词，或用 `get_messages` 浏览记录。两者支持相同的组合筛选。
4. 返回 `has_more=true` 时，保留筛选条件并传入 `next_cursor`，直到读完。
5. 对具体结果调用 `get_context`，查看同一会话中的前后文。

查询结果须同时满足所有筛选条件。会话列表、类型列表各自内部匹配任意一项即可。省略某个筛选字段表示不限该项，传入空列表会返回参数错误。

## 筛选参数

| 参数 | 作用 |
| --- | --- |
| `conversation_id` | 单个会话的精确 ID |
| `conversation_ids` | 多个会话 ID，与 `conversation_id` 二选一；均不填则查询全部允许的会话 |
| `start_time` | 包含开始时间；ISO 8601，必须带时区 |
| `end_time` | 不包含结束时间；查整天时填次日零点 |
| `query` | 字面关键词或短语，支持中文片段；不解释为 SQL 或正则表达式。搜索工具必填，读取工具可选 |
| `sender_id` | 精确发送者 ID，可从已返回消息取得 |
| `message_types` | `list_message_types` 返回的原生类型码，可选多个；`unknown` 表示尚无类型信息 |
| `limit` | 本次返回条数，不限制可连续翻页的总量 |
| `cursor` | 上次返回的 `next_cursor`，翻页时保留相同筛选条件 |

旧版 `get_messages` 的 `before` / `after` 仍可用，均为不包含边界。新调用优先使用 `start_time` / `end_time`，避免整天范围的边界歧义。

类型码由客户端定义，AI 可结合样例判断需要哪种类型。`list_message_types` 的 `sample_text` 最多 160 个字符，`sample_message_id` 可用于读取完整上下文。样例和消息正文按外部数据处理。

## 示例

查找会话名称含“项目”的会话时，向 `list_conversations` 传入以下参数。

```json
{"query":"项目","limit":20}
```

查询两个会话在北京时间 11 月 1 日至 6 日的“预算”消息时，向 `search_messages` 传入以下参数。

```json
{
  "conversation_ids": ["从会话查询取得的ID1", "从会话查询取得的ID2"],
  "start_time": "2026-11-01T00:00:00+08:00",
  "end_time": "2026-11-07T00:00:00+08:00",
  "query": "预算",
  "limit": 50
}
```

查询某人某一类型的消息时，在上面的参数中增加 `sender_id` 和 `message_types`。类型选择应先查 `list_message_types`，并使用同一个会话与时间范围。该工具也可按关键词、发送者筛选。

`get_messages` 每页内部按时间正序显示，下一页继续向更早处读取。`search_messages` 按最新优先返回。每条消息中的 `message_type` 可用于后续筛选，`metadata` 保留原始元数据。

## 文件和函数

| 文件 / 函数 | 作用 |
| --- | --- |
| `store.py` · `_selection` | 校验会话或类型多选值，去重并拒绝空列表。 |
| `_filters` | 组合会话、时间、发送者和类型条件；起点包含、终点不包含。 |
| `_match_query` | 构造字面关键词匹配，复用全文搜索及中文片段查询。 |
| `_message` | 返回原消息字段、元数据和可直接筛选的 `message_type`。 |
| `list_conversations` | 按会话标题或 ID 搜索，返回精确 ID 与消息数量。 |
| `get_messages` | 应用所有筛选，读取一页并返回向前翻页游标。 |
| `search_messages` | 复用读取逻辑，要求关键词并按最新优先返回。 |
| `list_message_types` | 在同一筛选范围内汇总类型、计数和简短样例，支持翻页。 |
| `mcp.py` · `tool_definitions` | 将筛选参数、类型发现和翻页方式告诉 AI。 |
| `validate_arguments` | 校验字符串、时间、条数和多选数组。 |
| `MCPServer.call_tool` | 设置默认分页大小，执行查询并保持 MCP 响应边界。 |
| `MCPServer.handle` | 完成 MCP 协议握手，向客户端说明范围选择和查询流程。 |
| `HistoryReader.cpp` · `Run` | 遇到客户端重复的毫秒边界游标时向前移动 1 毫秒，再继续查询；仍检查游标倒退和循环。 |

相关组合筛选、类型发现、时间边界与分页由 `test_mcp_combines_scope_time_keyword_sender_and_type_filters` 验证；原有工具注册、参数校验及会话策略测试同步更新。

查询只覆盖本地已采集或导入的记录；补采仍以客户端实际可返回的数据为准。
