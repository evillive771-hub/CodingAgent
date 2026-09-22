# AI Coding Agent 架构方案

## 1. 目标与边界

系统从 Teambition 获取前端 Bug 或需求，在受控 Git 工作区内完成定位、修改和验证；候选版本只允许自动发布到测试环境，生产发布始终经过独立人工审批。

本方案吸收 Claude Code 的 Agent Loop、工具注册、权限 Hook、上下文压缩和追加式会话日志等模式，但不会复制其产品表层。参考实现包括：

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
- [deep-dive-claude-code](https://github.com/sawzhang/deep-dive-claude-code)
- [Claude-Code 源码还原研究仓库](https://github.com/pengchengneo/Claude-Code)

最后一个仓库是从公开 npm 包 source map 还原的非官方研究版本，只用于理解设计，不作为依赖或权威接口。

## 2. 核心设计：双层循环

```mermaid
flowchart TD
    TB[Teambition / 人工工单] --> Contract[TaskContract]
    Contract --> Workflow[DeliveryWorkflow 确定性状态机]
    Workflow --> Workspace[准备隔离 Git 工作区]
    Workspace --> Coding[CodingSession]
    Coding --> Loop[Agent Loop]
    Loop --> Model[MiMo ModelAdapter]
    Loop --> Tools[Tool Runtime]
    Tools --> Observe[工具结果与证据]
    Observe --> Loop
    Coding --> Gate[Deterministic Stop Gate]
    Gate --> Quality[测试 / 构建质量门]
    Quality --> TestDeploy[测试环境发布]
    TestDeploy --> Browser[Playwright 验证]
    Browser --> Approval[人工审批]
    Approval --> Phase1Done[阶段一结束，不写 Git/TB]
    Approval -. 后续阶段 .-> GitPush[Git 推送]
    GitPush --> TBUpdate[Teambition 转测试]
    Workflow --> Journal[Event Log / Checkpoint / Evidence]
```

### 2.1 内层：Agent Loop

模型负责解决路径未知的问题：理解工单、搜索代码、提出修改、执行工具、观察失败并继续修复。循环结构保持简单：

1. 构造任务相关上下文；
2. 调用模型；
3. 校验并执行工具；
4. 把结构化工具结果写回消息；
5. 模型停止调用工具时进入 Stop Gate；
6. 证据不足则返回同一循环，满足合同或达到预算才退出。

### 2.2 外层：Delivery Workflow

固定业务流程不交给模型决定。状态机负责工单快照、工作区、构建、测试发布、浏览器验证、审批、推送、工单流转、失败和恢复。每次外部副作用都有幂等键、超时、事件和 Checkpoint。

### 2.3 完成定义

“模型不再调用工具”只表示它希望结束当前轮，不代表任务完成。Stop Gate 至少检查：

- 已提交结构化 `CodingOutcome`；
- 合同要求的验证命令全部退出码为 0；
- 验证发生在最后一次代码修改之后；
- 未超过工具、轮次和时间预算；
- 没有未处理的策略拒绝或外部操作失败。

模型评审可作为补充，但不能替代测试、状态断言或人工审批。

## 3. 分层与模块

```text
src/aicoding_agent/
  agent/          AgentLoop、Plan、Steering、Hooks、Background、StopGate
  tools/          Tool、Registry、Executor、Policy、Hooks
  context/        ContextPack、规则发现、确定性压缩、Evidence 归档
  workflow/       状态机、阶段、Checkpoint、恢复
  domain/         TaskContract、Budget、Evidence、Approval
  adapters/       MiMo、Teambition、Git、Process、Playwright、Publisher
  persistence/    JSONL、Checkpoint；后续 PostgreSQL
  cli.py          本地运行入口
```

依赖方向从外向内：适配器依赖领域端口，领域与 Agent Kernel 不依赖 Teambition、GitLab、GitHub 或具体设备。

## 4. 任务合同

`TaskContract` 是运行前必须冻结的输入，包含：

- 工单 ID、来源快照和目标；
- 验收标准、必需验证命令、构建命令与制品路径；
- 仓库、基线和允许路径；
- 允许工具、允许进程及预算；
- 测试环境标识和发布权限；
- 截止时间、最大修复次数与升级条件。

合同版本和哈希进入每条事件。恢复任务时继续使用原合同，不能静默采用新配置。

## 5. 工具与权限

工具由 Pydantic 输入 Schema、执行函数和静态元数据组成。元数据标识只读、写入、进程或外部副作用，以及是否允许并发。

首版模型工具：

| 工具 | 风险 | 说明 |
| --- | --- | --- |
| `read_file` | 只读 | 读取工作区内文本文件并限制大小 |
| `list_files` | 只读 | 枚举有限数量文件 |
| `search_text` | 只读 | 在工作区搜索文本 |
| `apply_text_patch` | 写入 | 只做精确旧文本替换 |
| `create_file` | 写入 | 只允许创建不存在的文件 |
| `run_process` | 进程 | executable + argv，禁止 Shell 字符串 |
| `submit_result` | 状态 | 提交结构化结果，不代表通过质量门 |

策略采用 `allow / deny / ask`。无人值守 Worker 遇到 `ask` 时失败关闭。路径必须解析到工作区内；进程必须同时出现在合同和项目配置允许列表中。Git 推送、TB 更新和发布工具不进入模型工具池。

## 6. 模型适配

当前使用 `env.txt` 中的 MiMo OpenAI-compatible API。内部消息和工具协议不绑定供应商，`MimoChatModel` 负责：

- 构造 Chat Completions 请求；
- 保留 `assistant.content`、`reasoning_content` 和 `tool_calls`；
- 解析并校验工具参数；
- 对 429 和临时服务错误做有限重试；
- 脱敏错误，不记录密钥。

不依赖 `tool_choice` 或原生 JSON Schema 输出。复杂结果通过普通工具调用提交，并在本地使用 Pydantic 校验。工具 Schema 避免不必要的 `oneOf`，并用契约测试覆盖多轮工具调用兼容性。

## 7. 状态、日志与恢复

- Session transcript 和运行事件使用只追加 JSONL，便于崩溃后诊断；
- Checkpoint 使用临时文件加原子替换；
- 事件数据递归脱敏 key、token、password、authorization 等字段；
- 大型控制台输出、截图和视频保存为独立 Evidence，只在事件中记录哈希和引用；
- 第二阶段以 PostgreSQL 作为任务、租约、幂等预留和配置版本的权威存储。

## 8. 质量门

从《前端交付质量提升方案》选择适用于具体项目的规则，而不是对每个 Bug 无条件运行全部检查：

1. Bug 复现或验收测试先失败；
2. 修改后任务专项测试通过；
3. 项目配置的 lint、typecheck、unit 和 build 通过；
4. 候选版本部署到测试环境；
5. Playwright 执行固定验收场景；
6. 按任务风险启用视觉、性能、A11y、安全和兼容性检查；
7. 生成结构化审批包，将任务合同哈希、Agent 结果、差异、测试部署回执和浏览器证据绑定到同一 SHA-256 摘要。

已有基线失败必须记录并区分，Agent 不得通过修改无关测试来掩盖失败。

## 9. 发布安全边界

- 测试环境：MVP 的唯一自动发布目标；阶段一使用 SFTP/NFS、固定 SSH 运维命令、健康检查和上一版本恢复；
- Git：通用 Git 能力兼容 GitLab 和 GitHub，专有 MR/PR API 后续按适配器接入；
- 生产环境：永不由 Coding Agent 自动发布，必须使用独立身份和人工审批流程；
- 审批绑定完整差异哈希，代码变化后旧审批自动失效。

## 10. 迭代计划

### M0：Agent Kernel

人工任务合同、本地工作区、MiMo、受控工具、Stop Gate、JSONL 和 CLI，使用 Fake Model 完成自动化测试。

### M1：单 Bug 可控闭环

隔离 worktree、受控构建与制品清单、SFTP/NFS/SSH 测试发布、Playwright 证据、审批包、失败回滚和恢复。

### M2：真实业务接入

Teambition、GitLab/GitHub 凭据与 MR/PR、PostgreSQL Worker、租约、幂等和 Web 控制台。

### M3：规模化治理

多项目调度、指标、Eval、成本预算、失败归因和权限治理。多 Agent 只有在单 Agent 闭环数据证明必要时才引入。

## 11. Teambition 接入基线

Teambition 采用开放平台 v3 API。读取项目任务使用
[`GET /api/v3/project/{projectId}/task/query`](https://open.teambition.com/docs/apis/6321c6d1912d20d3b5a49ec1)，
读取任务详情使用
[`GET /api/v3/task/query`](https://open.teambition.com/docs/apis/6321c6d2912d20d3b5a4a7b8)。
请求需要 `Authorization: Bearer ...`、`X-Tenant-Id`、`X-Tenant-Type: organization`
和操作者 ID；项目任务可通过 TQL 按执行者、状态和任务类型筛选。

当前实现提供真实只读客户端和 Fake 写入适配器。由于“转测试”在不同企业项目中可能对应不同的
`stageId`、`tfsId`、任务类型或自定义字段，在拿到目标项目的字段配置前，真实客户端会明确拒绝该
写操作。后续配置必须包含：

- 企业、项目和操作者 ID；
- 前端 Bug/需求的 TQL 过滤条件；
- Git 地址、基线、验收步骤等字段映射；
- 测试阶段/状态 ID；
- 处理结果、revision 和证据链接的写回字段；
- 幂等写回与冲突检查规则。

Agent 不直接持有 Teambition 写权限；只有审批后的 Delivery Workflow 可以调用写适配器。

## 12. 当前 Git 工作区与发布实现

M1 已实现基于现有本地 checkout 的 `LocalGitWorktreeManager`：

- worktree 根目录必须与源仓库处于不同目录树；
- 每次运行使用 detached worktree，并冻结为明确的 commit；
- 租约持久化绑定 `run_id`、任务合同哈希、源仓库和基线 commit；
- 重复准备同一租约时验证现场并幂等返回，租约不匹配时失败关闭；
- 工作流完成后自动释放托管 worktree，失败和驳回现场默认保留，终态可显式清理。

`GitChangeInspector` 的 SHA-256 摘要绑定基线 commit、排序后的变更路径、文件类型、
可执行模式和最终内容，因此不受 `git add` 前后暂存表达差异影响。当前对发生变化的 submodule
路径失败关闭，后续如需支持必须增加明确的 submodule revision 规则。

`GitBranchPublisher` 只在审批哈希复核成功后发布：

- 使用 Git plumbing 创建候选 commit，不移动 worktree 的 `HEAD`；
- 只推送固定前缀的新候选分支，不 force push、不 merge；
- 使用本地 pending/completed refs 记录幂等状态，临时推送失败可继续；
- push 时将 `core.hooksPath` 指向工作区外的空目录，禁止仓库自带 hook；
- 完整闭环已使用本地 bare remote 做集成测试。

当前适配器假定本地 checkout 已配置 remote 和凭据。GitHub/GitLab 凭据注入、clone/fetch、
远端基线冻结和 MR/PR API 仍属于后续模块。测试环境部署链路已由阶段一配置冻结，生产部署不在本项目内。

## 13. 结构化审批包

`ApprovalPackageBuilder` 在测试环境验证成功后生成版本化审批包。审批主题包含：

- 人工可读的任务目标、验收标准和冻结合同哈希；
- Agent 的结构化 `CodingOutcome`、轮次和工具调用数；
- 基线与规范化变更摘要；
- 测试环境部署回执；
- 验证结论和至少一个 Evidence 引用。

模型的自由文本收尾和审批包生成时间不进入主题摘要，避免无关文本导致摘要漂移。合同、代码、
部署回执或证据的任何变化都会改变 `subject_hash`，旧审批因而失效。审批包要求 Agent 已完成、
目标为测试环境、验证成功且证据非空；条件不满足时工作流失败关闭并执行既定回滚。

## 14. 持续对话模式

`aicoding-agent chat` 在 Agent Loop 外增加长期 `ChatSession`。外层交互遵循参考实现的稳定模式：
用户输入追加到会话，当前轮进入同一个工具循环，工具结果自动回填；模型结束本轮后控制权回到
用户，而不是退出整个进程。

本项目没有采用任意 Bash 工具。对话仍使用现有的 Pydantic 工具、路径边界、进程白名单、预算和
Stop Gate。每句用户输入派生一个新的 `TaskContract`：权限和验证命令来自工作区
`.aicoding-agent.json`，目标和验收上下文来自当前用户消息。发生写操作时必须重新运行配置的精确
验证命令；纯只读问答提交空 `changed_files` 后不要求重复测试。

会话只把近期用户消息、Agent 最终回答、Notes 和受保护的当前控制状态注入后续轮次；换出的消息保存
在按需检索的 History 中。代码和文件系统仍是事实来源。回合级 JSONL 继续兼容 `/history`、fork 和 rewind，
消息级 Journal 记录当前 Agent Loop 的模型/工具边界。近期历史仍保留 12 回合/40000 字符触发边界，
超出后把旧前缀换入不可变 Window，而不是反复总结或直接丢弃；完整 transcript、History 和 Evidence
继续保留在受保护运行目录。

`ask_user` 工具允许模型在缺少关键决策时以 `waiting_for_user` 结束当前轮。用户下一句连同前序
问题一起进入新任务合同，实现澄清后继续。该状态不是交付完成状态，不能进入发布工作流。

三个演示实例分别覆盖状态逻辑 Bug、搜索需求和无障碍需求，使用 Node.js 内置 test runner，
不安装第三方 npm 包。启动脚本先复制模板到忽略的 `runtime/chat-demos/`，避免重复演示污染基线。

## 15. 运行时控制、扩展与恢复

单 Agent Loop 增加了一个宿主控制面，但没有改变“模型提出动作、宿主校验动作”的安全边界：

- `PlanManager` 用只追加 JSONL 保存完整计划版本；每版最多一个 `in_progress`，转向时旧版先标记
  `superseded`，Stop Gate 要求模型生成新计划并完成全部项目；
- `SteeringInbox` 持久化 `steer / next / interrupt`。消息只在工具结束后或下一次模型调用前消费；
  如果消息在模型调用期间到达，尚未执行的旧方向工具调用会先得到成对的取消结果，再注入新方向；
- `AsyncHookBus` 提供 `UserPromptSubmit / BeforeModelCall / PreToolUse / PostToolUse /
  BeforeStop / AfterTurn`。`PreToolUse` 发生在宿主 Policy 允许之后，因此 Hook 只能继续收紧，不能提权；
- `ContextEvidenceArchive` 在压缩前原子保存完整 transcript；大型工具输出另存 Evidence，模型上下文
  仅保留头尾、相对路径和 SHA-256。压缩按工具调用组处理，并保护当前合同、计划、最近 Steering
  和最近验证结果；
- `BackgroundTaskManager` 只运行合同内的精确验证命令，使用 `asyncio` 子进程、有限输出和持久化
  Job。完成通知在安全边界注入 Agent Loop；宿主重启后未完成 Job 明确标记为 `interrupted`；
- `DeliveryWorkflow.resume(run_id)` 从最近 Checkpoint 继续。已保存的 Agent 结果、变更快照、部署、
  验证、审批包和 Git 发布回执不会重复计算；部署、回滚、Git 发布及 TB 写入复用稳定幂等键。

生产发布边界保持不变：恢复只能继续测试环境交付与审批后的候选分支/TB 流转，不能触发生产发布。
详细决策见 [ADR-0008](adr/0008-runtime-control-and-resume.md)。

## 16. 跨 Agent 文件锁

已提交版本原先只有 `ToolExecutor` 内的 `asyncio.Lock`，只能串行同一 Executor 实例中的写工具。
当两个 Agent 进程共享同一个工作区时，它们仍可能同时执行读-改-写并互相覆盖。当前写工具增加两层锁：

1. 进程内 `asyncio.Lock` 避免同一 LockManager 内无意义的轮询；
2. `.aicoding-agent/locks/<path-hash>.lock` 使用 Windows `msvcrt.locking` 或 POSIX `flock`
   实现跨进程互斥。

锁键来自工作区内规范化相对路径，不允许指向工作区之外。获得锁后写入临时 owner 元数据，记录
`run_id`、PID、目标路径和时间；超时错误可指出当前持有者。`ToolExecutor` 追加
`file_lock.acquired` 与 `file_lock.released` 审计事件。Agent 取消、工具失败和异常退出时，宿主在
`finally` 中释放锁；进程崩溃时 OS 会回收 advisory lock，下一位持有者覆盖可能残留的元数据。

`apply_text_patch` 在锁内重新读取原内容并校验精确匹配，然后使用同目录临时文件、`fsync` 和
`os.replace` 原子发布，同时保留原文件模式。`create_file` 使用完全写入的临时文件和原子硬链接，
并保持“已存在则拒绝”。因此两个 Agent 针对同一旧文本修改时，后执行者会看到新版本并明确失败，
而不是静默覆盖。

该锁只覆盖一次文件工具调用，不代表任务级文件所有权。不同文件仍可并发；同一文件的多个独立修改
可以顺序合并。大范围多 Agent 开发仍优先使用 task-bound Git worktree，因为文件锁不能解决跨文件
语义冲突、测试环境冲突或合并策略。此取舍参考 `learn-claude-code` s13 的“共享任务存储加锁，代码
编辑使用任务绑定 worktree”模式，而不是把整个仓库锁成单线程。详细决策见
[ADR-0009](adr/0009-cross-agent-file-locking.md)。

## 17. 动态 Skill 加载

`TaskContract.skill_directories` 声明工作区内允许发现 Skill 的目录，默认只检查 `skills/`。目录使用
`<root>/<name>/SKILL.md` 结构。Agent Run 启动时读取并验证 frontmatter，只将名称、转义后的描述和
SHA-256 目录注入上下文；完整正文由模型通过 `load_skill` 工具按需读取。

`load_skill` 是 `READ` 风险工具，具有 Pydantic 名称 Schema、合同 allowlist、PolicyEngine、Hook、
输出预算和事件审计。目录发现和文档读取使用异步接口承载阻塞文件 I/O。加载前再次执行工作区路径
解析并复核启动时哈希，因此路径逃逸、运行中替换或符号链接转向都会失败关闭。Skill 只提供说明，
不能执行其中的脚本，也不能修改合同、权限、质量门或发布边界。详细决策见
[ADR-0010](adr/0010-dynamic-skill-loading.md)。

## 18. 交互式权限确认

`ToolExecutor` 只把 PolicyEngine 的 `ASK` 判定交给可选 `PermissionBroker`。`DENY` 不进入 Broker，
因此用户批准不能扩大任务合同、路径、进程或 Hook 权限。无人值守 `run` 和交付 Worker 不配置
Broker，继续对 `ASK` 失败关闭；持续对话模式使用会话级追加式 `PermissionStore`。

请求绑定工具名、风险等级和规范化参数 SHA-256。持久化参数预览经过递归脱敏和大小限制。用户可用
`/allow <id>` 批准一次、`/allow <id> session` 复用当前会话中工具名与参数哈希完全相同的调用，或
`/deny <id>` 拒绝。超时及宿主重启遗留的未决请求自动拒绝。批准决定与审计事件写入后才执行工具。
该控制面没有新增任何外部副作用工具，发布安全边界保持不变。详细决策见
[ADR-0011](adr/0011-interactive-permission-broker.md)。

## 19. 统一错误恢复

模型适配器将最终失败分类为 transient、rate limit、protocol、authentication、request 或 unknown。
MiMo 适配器内部继续处理短暂 HTTP 重试；最终可重试错误由每个 Run 独立的 `RecoverySession` 使用
指数退避和重复失败阈值处理。限流响应的数字 `Retry-After` 在有界范围内生效。协议错误的纠正提示
只加入当前重试请求，不进入持久化 transcript。每次调度、恢复和熔断都有运行事件。

工具恢复采用不同边界：宿主从不自动重放工具。失败结果正常回填给模型，由模型决定下一步；连续
相同的工具名、规范化参数及失败输出达到阈值时打开熔断器并终止 Run。成功调用重置连续计数，用户
Steering 产生的取消结果不计入失败。这样可避免写文件和未来副作用工具被隐式重复执行。详细决策见
[ADR-0012](adr/0012-bounded-agent-recovery.md)。

## 20. Agent 可观测性

Agent Loop 在模型调用前发出 `context.prepared`，记录压缩前后字符数、估算 Token、策略、Session
Memory 版本和压缩错误；成功响应事件记录供应商 usage 和包含有界恢复过程在内的端到端耗时。工具
完成事件记录包括 Hook、锁等待和执行在内的耗时，Policy/Hook 拒绝投影为失败调用。事件不增加
Prompt 正文或完整参数副本。

`TelemetryRegistry` 从同一批 append-only 事件构建内存视图，汇总轮次、上下文、压缩、模型调用、
重试、Token、工具调用、失败、耗时和权限请求。JSONL 仍是权威记录；Registry 重启丢失不影响任务
恢复。持续对话的 `/metrics` 只呈现聚合数值。详细决策见
[ADR-0013](adr/0013-event-projected-agent-telemetry.md)。

## 21. 前端结构化 Diagnostics

`TaskContract.diagnostic_commands` 预先冻结诊断 ID、程序、参数、工作目录、解析器和超时。模型的
`get_diagnostics` Schema 只允许选择 ID 和结果上限。PolicyEngine 校验工具权限、ID、executable
allowlist 与 cwd 路径，宿主使用 `asyncio` 无 Shell 执行，因此该能力不会退化为任意命令入口。

TypeScript parser 支持常见非彩色行列格式，ESLint parser 使用 JSON；两者只返回解析到工作区内的
路径。记录数、单条消息、结构化 data、原始预览和最终 output 都有界。诊断退出非零仍可成功返回
发现的问题，但不会写入 `verification_success`，所以不能替代 Stop Gate 的精确验证命令。详细决策见
[ADR-0014](adr/0014-contract-bound-frontend-diagnostics.md)。

## 22. 持久化 Task DAG

Plan 继续表示当前执行顺序；`TaskGraphManager` 表示跨轮次依赖状态。每个节点包含稳定 ID、状态、
依赖、owner、attempts、阻塞原因、证据和可选 worktree 引用。版本快照追加到 `task-graph.jsonl`，
重启后恢复。快照校验唯一 ID、依赖存在、无环、依赖完成顺序、完成证据，并限制每个 owner 同时
只有一个进行中任务。

活跃图不能删除既有任务、减少 attempts 或回退完成状态。运行中 Steering 会同时 supersede Plan
和 Task DAG，模型必须通过 `update_task_graph` 建立新方向的活跃图。当前图会注入上下文并受到压缩
保护；Stop Gate 拒绝 superseded 或未全部完成的图。本阶段不派生 Agent 或分配 worktree，只建立
未来并发调度所需的权威任务结构。详细决策见 [ADR-0015](adr/0015-persistent-task-dag.md)。

## 23. 独立 Goal Evaluator

主 Agent 表示希望停止后，宿主仍先执行确定性 `CompletionGate`。只有 outcome、验证新鲜度、Plan 和
Task DAG 全部满足时，才构造隔离的 `GoalEvaluationInput`，其中只包含目标、验收标准、结构化结果
和有限证据目录，不包含主对话 transcript。真实 CLI 使用独立 MiMo 客户端，Evaluator 没有可执行
工具，只能按 `submit_goal_evaluation` Schema 返回结论。

`complete` 至少引用一个宿主已知证据 ID；未知引用被拒绝并有界重试。`incomplete` 的缺口重新注入
主循环，`needs_user` 进入等待，`impossible` 或评估失败关闭。评估事件包含状态、attempts、耗时与
usage，可由 `/metrics` 查看。该语义复核只能补充，不能覆盖确定性失败。详细决策见
[ADR-0016](adr/0016-isolated-semantic-goal-evaluator.md)。

## 24. 只读 Subagent

`delegate_readonly` 将有界 explore、review 或 test_analyst 任务交给 `ReadOnlySubagentManager`。子 Agent
使用独立模型客户端和全新消息列表，不继承父 transcript。其冻结合同只允许 read_file、list_files、
search_text 与 submit_subagent_report；写工具、进程、权限确认、Plan、Task DAG 和递归 delegate 都
不在 Schema 或 allowlist 中。多个只读子任务受信号量限制，模型失败使用统一恢复策略。

报告不是自由文本回传：finding 必须提供工作区路径、行号和观察，宿主重新读取文件并附加真实行片段，
拒绝不存在、越界或逃逸路径。报告按完整 finding 裁剪为有界有效 JSON；内部 transcript 和完整工具
输出不会进入父上下文。当前不分配 worktree 或并发写代码。详细决策见
[ADR-0017](adr/0017-isolated-readonly-subagents.md)。

## 25. 会话分叉与文件修改恢复

`/fork <new-session> [turn]` 在源、目标 transcript 锁内读取指定历史，为复制回合生成新的 `run_id`
并保留 `origin_run_id`，最后通过临时文件、`fsync` 和 `os.replace` 原子发布。目标 session 必须不存在，
源 transcript 不变。新 session 不继承 Plan、Task DAG、Steering、权限、后台任务或模型内部工具历史。

启用真实 CLI 时，`ToolExecutor` 使用受保护的 `MutationJournal`。文件写工具在获得既有跨进程文件锁后
先捕获 UTF-8 文本快照，工具成功修改后追加前后 SHA-256 和完整内容。若日志追加失败，宿主在释放锁前
恢复旧快照并把本次工具标记失败，避免留下无法恢复的半成功写入。日志不会作为工具结果或 Prompt
注入模型。

`/rewind <new-session> <turn>` 收集所选回合之后的 run ID，在持有 transcript 锁期间执行恢复，再写入
目标分叉。恢复先按稳定顺序获得全部文件锁，验证同一路径的跨 Run 快照链和当前内容边界，只有全量
预检通过才逆序应用。Agent 创建的文件会删除，已有文件恢复为最早快照；重复请求由恢复摘要哈希保证
幂等，重复恢复后出现的新编辑仍会被识别为冲突。

进程和外部风险工具可能产生无法由文本快照穷举的副作用，所以在启动前写入不可逆 Effect。所选回合
存在这类 Effect 时恢复失败关闭。该能力不使用 `git reset --hard`，也不恢复二进制、超限文件或外部
编辑，更不扩大 Git 推送、Teambition、部署和生产审批权限。详细决策见
[ADR-0018](adr/0018-session-fork-and-file-mutation-recovery.md)。

## 26. 精选项目记忆

项目记忆位于 workspace 对应的受保护 runtime 目录，并跨 chat session 共享。`MemoryStore` 以追加式
JSONL 保存每个记忆 ID 的状态快照；`MemoryManager` 恢复 topic、version、source、source_run_id、
pending/approved/rejected、TTL、到期时间和用户审批元数据。同 topic 允许产生新版本，检索只选择
版本最高的 approved 且未到期记录，旧版本继续保留审计。

记忆写入有两条路径：用户 `/remember <topic> <days> <text>` 直接产生 user/approved 记录；模型
`propose_memory` 只能产生 agent_proposal/pending，必须由 `/memory-approve` 或 `/memory-reject` 处理。
不存在模型可调用的 approve 工具。私钥头、常见服务 Token、Bearer 字段以及 API key/password/secret
赋值在追加前拒绝，失败内容不落盘。

`recall_memory` 是受合同 allowlist、Pydantic Schema、Policy 和工具预算约束的 READ 工具。它执行按需
文本匹配，只返回最多十条完整结构化记录，并在加入结果时保证 JSON 整体不超过合同输出预算。记忆
目录和全文不自动加入系统 Prompt，pending、rejected、expired 和旧 topic 版本不可召回。模型必须把
记忆视为辅助上下文；当前文件、合同、验证证据和权限策略优先。详细决策见
[ADR-0019](adr/0019-curated-project-memory.md)。

## 27. 多 Agent Coordinator

`MultiAgentCoordinator` 位于模型工具层之外。它接收 fresh/active Task DAG、每个节点的独立
`TaskContract` 和 Agent 工厂。`TaskGraphManager.claim_next(owner)` 在同一宿主锁内选择依赖全部完成的
pending 节点，原子写入 owner、`in_progress` 和递增 attempts；worktree 绑定、完成证据、失败阻塞和
阻塞依赖的递归传播都生成新的追加式 Task Graph 版本。

Coordinator 要求源 checkout clean，随后冻结 repository HEAD 并创建专用 integration worktree。每个 worker 认领任务时读取
当前 integration revision，将其冻结为子合同 base，再由 `LocalGitWorktreeManager` 创建独立 detached
worktree。因此平行节点共享适当基线，依赖节点能看到已经进入 integration 的前置修改。worker 工厂
负责提供隔离 Agent/模型实例；上限为八个，并发调度使用 Python `asyncio`。

候选进入 integration 前经过确定性 `CoordinatorMergeGate`：Agent 必须 completed、结构化 outcome 的
changed_files 必须等于 `GitChangeInspector` 快照、合同至少包含一个 verification command。宿主以安全
环境、`executable + argv`、无 Shell 重跑全部验证；非零、超时或复验改变候选快照均失败。验证输出只
保留有界预览和 SHA-256 证据。

`GitCandidateIntegrator` 在候选 worktree 暂存完整变化并生成 binary patch，先 `git apply --check`，
再应用到 integration index，并在禁用仓库 hook 的条件下创建本地 detached commit。每个已集成路径
记录最近 revision；另一个候选碰到该路径时，只有该 revision 是候选 base 祖先才视为依赖链内顺序
修改，否则认定为并行覆盖并阻塞。integration 操作串行，源 checkout 的 HEAD、索引和文件必须在整个
协调前后保持同一 `ChangeSnapshot`。

`CoordinatorMailboxStore` 为每个 owner 保存有界追加式 claimed、worktree、integrated/blocked 事件。
成功和失败 worker worktree 以及 integration worktree均保留供人工审查。当前 Coordinator 不作为模型
工具，也不自动接入 DeliveryWorkflow；它不执行 push、MR/PR、源分支 merge、部署、Teambition 写回或
生产发布。详细决策见 [ADR-0020](adr/0020-host-owned-multi-agent-coordinator.md)。

## 28. 分层记忆与 Token 感知 Context Rollover

原有字符压缩继续作为确定性兼容回退。真实 CLI 为主 Agent 和会话历史配置 `ContextBudgetManager`，
根据模型窗口、输出预留、warning buffer 和兼容配置中的 compact buffer 估算 Token。进入警告区间
后，先在完整 transcript 落盘的前提下清理 checkpoint 输入中的较老工具正文；达到 rollover 阈值时，
由独立、无工具的 MiMo 客户端生成 Notes。

`SessionMemoryManager` 对每个 chat session 和 Agent run 保存追加式 Notes checkpoint；换出的原始消息
由 `HistoryStore` 写成不可变 Window/Item。Notes 记录当前目标、决策、进度、问题、下一步及
`Wxxxxxx:item-xxxxxx` 引用，不再把上轮摘要当作旧历史的唯一副本。Prompt Too Long 只按完整消息组
缩小 checkpoint writer 输入，History 原始证据仍然保留；连续失败三次后熔断。Notes 落盘前脱敏，且
不会写入需要用户审批的精选项目记忆。

新上下文显式包含 `new_context` 和 `notes`，随后重新注入近期消息、合同、Plan、
Task Graph、Steering、Skill 目录/已加载 Skill 和验证状态。`SessionMessageJournal` 逐消息追加 run、
API round 和父事件链，使进程可以恢复到安全消息边界。自动恢复遇到悬空工具调用会停止；即使用户
选择 `/resume --force`，宿主也只生成状态未知的成对结果，不重放工具。详细决策见
[ADR-0021](adr/0021-session-memory-and-context-compaction.md)。

History Retrieval 使用 literal substring + metadata filtering + exact item fetch，不依赖 embedding 或
向量数据库。`history_search`、`history_list_items` 和 `history_read_item` 均为只读、Schema 校验、合同
allowlist 和输出预算约束的模型工具。整体层级是 L1 Current Context、L2 Notes + History、L3 经用户
批准且带 TTL 的 Curated Project Memory。
