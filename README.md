# AI Coding Agent

一个面向前端交付的受控 AI Coding Agent。它从 Teambition 工单构造可验证的任务合同，在隔离 Git 工作区中完成代码修改和验证，并由确定性交付工作流控制测试环境发布、人工审批和工单流转。

当前版本已完成 Agent Kernel 和阶段一测试环境交付链路：

- Python 异步 Agent Loop；
- MiMo OpenAI-compatible 模型适配器；
- Pydantic 工具 Schema、权限策略与执行预算；
- 工作区内的读取、搜索、精确文本补丁和受控进程工具；
- 工作区 Skill 目录发现和受合同约束的按需 `SKILL.md` 加载；
- 对话内 Policy `ASK` 权限确认、精确参数哈希绑定和追加式审计；
- 模型错误分类、有界退避恢复和重复工具失败熔断；
- 上下文、模型、Token、重试及工具耗时的事件指标和对话 `/metrics`；
- 通过合同 ID 运行的 TypeScript/ESLint 结构化前端 Diagnostics；
- 版本化持久 Task DAG、依赖/负责人/证据约束和 Steering 后显式重规划；
- 确定性 Stop Gate 通过后的隔离语义 Goal Evaluator；
- 隔离上下文、不可递归且证据行由宿主回填的只读 Subagent；
- 原子会话 `/fork` 与基于文件修改日志、冲突预检的保守 `/rewind`；
- 仅按需检索、带来源/版本/TTL 和用户审批的项目精选记忆；
- Task DAG 原子认领、独立 Git worktree 与本地 integration 门禁的多 Agent Coordinator；
- 基于验证证据的 Stop Gate；
- JSONL 事件日志和原子 Checkpoint；
- Teambition、Git、测试环境发布的端口及 Fake 适配器；
- 绑定完整任务合同和基线 commit 的隔离 Git worktree；
- 不受暂存状态影响的规范化变更哈希；
- 审批后幂等推送候选分支，不自动合并、不强推；
- 合同、Agent 结果、变更、测试部署和验证证据组成结构化审批包；
- Claude Code 风格的持续对话会话，可恢复历史并在需要时向用户追问；
- 持久化版本计划、运行中 `/steer`、排队 `/next` 和安全 `/interrupt`；
- 异步 Hook、受合同约束的后台验证任务和完成通知；
- 跨 Agent/跨进程的逐文件写锁、持有者诊断、审计事件和原子文件替换；
- 大型工具输出 Evidence 归档、压缩前完整 transcript 快照；
- Token 感知的 Context Rollover、Notes checkpoint、可检索 History 和消息级安全恢复；
- 可从稳定 Checkpoint 恢复的幂等交付工作流；
- 合同冻结的受控构建、逐文件 SHA-256 制品清单和构建日志脱敏；
- SFTP 上传 NFS、远端哈希复核、设备固定 SSH 命令切换、健康检查与上一版本恢复；
- Playwright 截图、Trace、视频、控制台错误和失败请求证据；
- 测试环境审批包与交互式 approve/reject，阶段一批准不执行 Git/TB 外部写入；
- 完成后自动释放托管 worktree，失败时默认保留现场。

真实 Teambition 写回、Git 凭据与 clone/fetch、MR/PR 创建和 PostgreSQL Worker 租约属于
后续交付集成。生产发布永远需要独立人工审批。

Git 发布适配器目前依赖已经配置好 `origin` 和凭据的本地 checkout，只推送
`aicoding/<task>-<id>` 候选分支。它不创建 MR/PR，也不执行 merge。

## 本地开发

```powershell
$ErrorActionPreference = "Stop"
.\scripts\bootstrap.ps1

# 后续日常命令
$env:UV_CACHE_DIR = "D:\\Github\\CodingAgent\\.uv-cache"
& ".\.tools\uv\bin\uv.exe" run pytest
& ".\.tools\uv\bin\uv.exe" run ruff format --check .
& ".\.tools\uv\bin\uv.exe" run ruff check .
```

引导脚本把 `uv`、虚拟环境和依赖缓存全部放在当前 D 盘项目目录，不向系统 Python
安装项目依赖。

模型配置从 `.env.local` 或显式的 `--env-file` 读取。不要提交真实密钥；仓库只保留 `.env.example`。

## CLI

```powershell
& ".\.tools\uv\bin\uv.exe" run aicoding-agent doctor --env-file env.txt
& ".\.tools\uv\bin\uv.exe" run aicoding-agent run `
  --task examples/task.json `
  --workspace D:\\path\\to\\repo `
  --env-file env.txt
```

`run` 会真实修改指定工作区。任务合同中的 `allowed_tools`、`allowed_commands` 和 `verification_commands` 决定 Agent 能做什么以及什么算完成。

### 阶段一测试环境交付

阶段一入口按顺序执行隔离 worktree、Coding Agent、变更快照、受控 build、SFTP/NFS/SSH
测试部署、Playwright 验证和人工审批：

```powershell
$env:UV_CACHE_DIR = "D:\Github\CodingAgent\.uv-cache"
$env:PLAYWRIGHT_BROWSERS_PATH = "D:\Github\CodingAgent\.tools\playwright"
& ".\.tools\uv\bin\uv.exe" sync --extra dev --extra delivery
& ".\.tools\uv\bin\uv.exe" run playwright install chromium
& ".\.tools\uv\bin\uv.exe" run aicoding-agent deliver-phase-one `
  --task .\examples\phase1\task.json `
  --repository D:\path\to\frontend-repo `
  --delivery-config .\examples\phase1\delivery.json `
  --env-file .\env.txt
```

可复制 [阶段一配置模板](examples/phase1/README.md) 后填写测试环境。密码只通过配置中的
`password_env` 从宿主环境变量读取；也可使用私钥文件。SSH 强制使用 `known_hosts`，不会接受关闭
主机密钥校验。审批包绑定任务合同、变更哈希、制品清单、部署回执和浏览器证据。阶段一即使批准也
不会 Git push 或写回 Teambition；这些仍属于后续阶段。

设备回滚失败会进入持久隔离并阻断后续发布，可通过 `delivery-device-status` 查看。人工修复后只能用
`delivery-device-recover` 携带审批人和说明，并在实时健康检查通过后解除隔离。完整的软件范围、自动化
证据和仍需真实测试设备执行的最终演练见 [阶段一实现与验收](docs/phase-one-acceptance.md)。

### 持续对话模式

项目根目录已经提供 `.aicoding-agent.json`，因此可以直接启动：

```powershell
& ".\.tools\uv\bin\uv.exe" run aicoding-agent chat --env-file env.txt
```

启动后直接输入自然语言，不需要为每个任务重新组织命令：

```text
You > 帮我定位这个测试为什么失败并修复
You > 解释刚才的修改
You > 再补一个边界测试
```

对话使用同一工作区并按 session 持久化。重新使用 `--session default` 会恢复之前的用户/Agent
回合、Notes 与 History。Agent 工作时仍可继续输入：自然语言或 `/steer <要求>` 会改变当前方向，
排到当前轮之后，`/interrupt` 在下一个安全边界终止；`/plan` 查看版本化计划。`/history` 查看历史，
`/status` 查看会话和后台任务，`/exit` 退出。`/fork <新会话> [回合]` 只复制历史并保留源会话；
`/rewind <新会话> <回合>` 还会撤销该回合之后由 Agent 文件工具记录的修改。运行事件和对话记录保存在受保护的
`.aicoding-agent/`，不会作为候选代码发布。

`/rewind` 不是 `git reset`：它在逐文件锁内按 Mutation Journal 逆序恢复，并先检查所有文件，避免
部分回滚。文件存在未记录的外部编辑、日志链不连续，或后续回合启动过进程/外部风险工具时会拒绝；
源会话始终保留。分叉不复制 Plan、Task DAG、Steering、权限和后台任务，新会话从干净控制状态开始。

长会话接近模型 Token 阈值时，系统把旧消息换出当前窗口，原样保存为可寻址的
`Wxxxxxx:item-xxxxxx` History，再用无工具调用写入只含 Goal、Decisions、Progress、当前问题、关键
引用和 Next 的 Notes checkpoint。新窗口只加载 Notes 和近期工作集；模型可用 `history_search` 做字面
子串与元数据检索，用 `history_read_item` 精确分页读取原始证据。使用 `/context` 查看预算、Notes 版本
和最近 History Window，`/new-context [重点]` 主动换页；`/compact` 是兼容别名。启动时默认
只自动恢复没有悬空工具调用的中断；`/resume --force` 只在人工检查后继续且不会重放工具，
`/resume --discard` 放弃该中断回合。可通过 `AUTO_COMPACT` 和 `AUTO_RESUME=safe|ask|disabled` 配置。

长期记忆不会自动从 transcript 提炼或注入每轮 Prompt。模型可用 `propose_memory` 生成 pending 建议，
用户通过 `/memory` 查看，使用 `/memory-approve <id>` 或 `/memory-reject <id>` 决策；
`/remember <topic> <ttl-days> <content>` 直接添加用户明确批准的记忆。Agent 只有调用
`recall_memory` 时才会看到已批准、未过期且同 topic 版本最高的内容。疑似 API key、Token、密码或
私钥的内容在持久化前拒绝，记忆也不能覆盖任务合同与权限策略。
Notes + History 是当前会话的 L2 Thread Memory，不会自动进入上述精选 L3 长期记忆审批库。

PolicyEngine 返回 `ASK` 时，对话会显示短请求 ID。使用 `/allow <id>` 仅批准本次，使用
`/allow <id> session` 可在当前会话复用工具名和完整参数都相同的批准，使用 `/deny <id>` 拒绝；
`/permissions` 查看待确认请求。无人值守 `run` 不创建 Permission Broker，因此 `ASK` 继续失败
关闭。权限确认不能覆盖合同或 Policy 的 `DENY`，也不会让 Git 推送、工单写回或部署成为模型工具。

模型适配器把临时服务错误、限流、协议错误、认证错误和请求错误分类后交给每次 Run 独立的恢复
会话。可重试模型调用采用有界指数退避；协议错误会获得一次不写入长期 transcript 的格式纠正提示。
宿主不会自动重放任何工具调用，连续相同的工具失败达到阈值后会熔断并保留明确原因。

对话中使用 `/metrics` 可查看当前或最近 Run 的上下文字符数、压缩次数、模型耗时、Token、重试、
工具耗时及失败数。指标由追加式运行事件投影，不显示 Prompt 正文、工具参数或工具输出。

前端项目可在任务合同中配置 `diagnostic_commands`，例如固定的 TypeScript `--pretty false` 或
ESLint JSON 命令。模型只能调用 `get_diagnostics({diagnostic_id: ...})`，不能修改 executable 和
argv。结果包含工作区相对文件、行列、级别、代码和消息，并受到合同输出预算限制。Diagnostics
用于定位问题，不会生成 `verification_id`；代码修改后仍须运行全部 `verification_commands`。

复杂任务可通过 `update_task_graph` 维护依赖图。节点记录稳定 ID、状态、`blocked_by`、owner、
attempts、阻塞原因、证据和可选 worktree 引用；宿主拒绝依赖环、未完成依赖的执行、同一 owner 的
多个进行中任务、无证据完成及活跃图中隐藏旧任务。对话使用 `/tasks` 查看当前图。Steering 会先
supersede 旧图，再要求模型按新方向建图；未完成图会阻止 Stop Gate 放行。

候选完成先经过原有的结构化 outcome、验证新鲜度、Plan 和 Task DAG 门禁；全部通过后，独立 Goal
Evaluator 才会依据目标、验收标准和宿主证据目录复核语义完成度。Evaluator 使用独立上下文且没有
可执行工具，`complete` 必须引用已知证据 ID。`incomplete` 会把缺口返回主 Agent，`needs_user`
暂停，`impossible` 或协议耗尽失败关闭。它不能覆盖失败测试或生产审批边界。

主 Agent 可用 `delegate_readonly` 派发 explore、review 或 test_analyst 子任务。子 Agent 使用全新
上下文，只能读取、枚举、搜索并提交报告，不允许写文件、运行进程、询问用户或再次委派。每条 finding
的路径和行号都会由宿主重新读取并附上实际代码片段；超大报告按完整 finding 裁剪，保持有效 JSON。
子 transcript 不返回主 Agent。当前能力只做只读分析，不进行并发代码修改或合并。

写任务并发由宿主侧 `MultiAgentCoordinator` 提供，而不是模型工具。Worker 原子认领依赖已完成的
Task DAG 节点，每项使用独立 detached worktree；候选必须再次通过合同验证，且 Agent 声明的
`changed_files` 必须与 Git 快照完全一致。安全候选只合成到托管的本地 integration worktree。同基线
并行修改同一路径会阻塞，依赖任务基线已经包含前序 revision 时才允许继续修改该路径。Coordinator
不会改变源 checkout，也不会 push、创建 MR/PR、部署或写回 Teambition。当前通过 Python 宿主 API
调用，尚未暴露为模型工具或默认对话自动动作。

后台任务不是任意 Shell：模型只能用 `start_background_process` 启动任务合同中的精确验证命令，
进程使用 `executable + argv` 异步执行。Git 推送、Teambition 写入和部署仍不属于模型工具。

### 动态 Skills

任务合同可通过 `skill_directories` 配置工作区 Skill 根目录，默认值为 `skills`。每个 Skill 使用
`skills/<name>/SKILL.md`，文件开头需要包含：

```markdown
---
name: frontend-debugging
description: Diagnose and verify browser-facing bugs with reproducible evidence
---
```

Agent 启动时只把名称、描述和 SHA-256 放入上下文；完整内容仅在模型调用 `load_skill` 后进入当前
工具循环。Skill 为只读说明，不能执行脚本或提升权限。文件在 Run 中途变化时加载失败，需要开始
新的 Run。仓库提供的 `skills/frontend-debugging/SKILL.md` 可作为格式示例。

### 前端对话实例

无需安装 npm 包即可启动计数器 Bug 实例：

```powershell
.\scripts\start-chat-demo.ps1 -Case 01-counter-clamp
```

其他实例：

- `02-search-normalization`：搜索大小写、空格和空查询需求；
- `03-accessible-dialog`：登录弹窗无障碍属性需求。

启动脚本会将模板复制到 `runtime/chat-demos/`，Agent 只修改副本。实例说明和推荐追问见
[examples/chat_cases/README.md](examples/chat_cases/README.md)。

详细设计见 [docs/architecture.md](docs/architecture.md)。

## 开发与合并门槛

每个适配器或领域模块先运行自己的单元/集成测试；模块通过后才接入
`DeliveryWorkflow`，随后运行整体集成测试和全量 `pytest + ruff`。未通过模块测试的
实现不得进入交付主流程。
