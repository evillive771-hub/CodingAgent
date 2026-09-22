# ADR-0021：分层记忆、Context Rollover 与精确 History 恢复

## 状态

Accepted（取代早期“增量摘要”方案）

## 背景

反复使用“旧摘要 + 新消息 → 新摘要”会形成 summary of summary，逐步丢失决策原因、精确错误、路径、
symbol 和工具证据。大型编码任务需要释放当前模型窗口，但不能把有损摘要当作旧上下文的唯一副本。

项目同时存在跨会话的精选记忆。自动产生的会话 Notes 不能进入该存储，否则会绕过用户审批、来源和
TTL 策略。

## 决策

记忆按三层管理：

1. L1 Current Context 是模型当前工作集，只包含 system、当前合同、Notes、近期完整消息及受保护的
   Plan、Task Graph、Steering 和验证状态。
2. L2 Thread Memory 由 Notes 和 History 构成。到达 Token 阈值或用户执行 `/new-context` 时，宿主先
   把换出的消息保存为不可变 `Wxxxxxx:item-xxxxxx`，再由隔离、无工具模型写 Goal、Decisions、
   Progress、Current Problem、Important History 和 Next checkpoint，最后用 `<new_context>` 创建新
   工作窗口。`/compact` 暂时作为兼容别名。
3. L3 Long-term Memory 继续使用经用户批准、带来源/版本/TTL 的精选项目记忆。自动 Notes 和原始
   History 不会自动提升为长期记忆；Agent 只能提出 pending 记忆建议。

History 保存原始 message 字典及 role、tool namespace/name、agent、源消息序号等元数据。读取接口为：

- `history_search`：大小写不敏感的 literal substring search，加精确元数据过滤；不使用 embedding；
- `history_list_items`：只列出受限数量的 item 元数据；
- `history_read_item`：按 window/item 精确读取，支持字符 offset/limit 分页。

三个接口都是 Pydantic 输入、READ 风险、合同 allowlist 和有界输出的模型工具。Notes 中的引用可直接
读取；没有引用时 Agent 可先按它记得的代码 symbol、命令或错误关键字检索。当前文件、合同、权限和
宿主证据始终高于 Notes。

`ContextBudgetManager` 继续根据模型窗口、输出预留、warning buffer 和 rollover buffer 主动触发换页。
大型工具输出在 History 中保留原始 message，同时模型 checkpoint 可以使用 Evidence Archive 的有界
表示。Prompt Too Long 只缩小 checkpoint writer 的输入，已经归档的原始 History 不受影响；连续三次
checkpoint 失败后熔断，当前窗口继续使用确定性回退。

`SessionMessageJournal` 仍负责进程中断恢复，和 Context History 是不同职责：前者保证消息协议与工具
调用状态，后者保证换页后可检索旧证据。宿主不会因为恢复或记忆内容自动重放工具、扩大权限或执行
外部变更。

## 结果

上下文管理从 repeated summarization 改为 small checkpoint + original evidence。旧窗口不再直接占用
prompt，但可按引用或关键词精确恢复；长任务的工作集保持小而可控。代价是运行目录会保存更多 History
数据，并需要按 scope 管理其生命周期。现有 `SessionMemory*`、`ConversationCompactor` 名称作为 API
兼容层保留，其运行语义已经是 rollover。
