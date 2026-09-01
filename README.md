# AI Coding Agent

一个面向前端交付的受控 AI Coding Agent。它从 Teambition 工单构造可验证的任务合同，在隔离 Git 工作区中完成代码修改和验证，并由确定性交付工作流控制测试环境发布、人工审批和工单流转。

当前版本已完成 Agent Kernel，并开始交付工作流 M1：

- Python 异步 Agent Loop；
- MiMo OpenAI-compatible 模型适配器；
- Pydantic 工具 Schema、权限策略与执行预算；
- 工作区内的读取、搜索、精确文本补丁和受控进程工具；
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
- 大型工具输出 Evidence 归档、压缩前完整 transcript 快照；
- 可从稳定 Checkpoint 恢复的幂等交付工作流；
- 完成后自动释放托管 worktree，失败时默认保留现场。

真实 Teambition 写回、Git 凭据与 clone/fetch、测试设备部署、Playwright 场景和
PostgreSQL Worker 租约属于后续交付集成。生产发布永远需要独立人工审批。

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
摘要。Agent 工作时仍可继续输入：自然语言或 `/steer <要求>` 会改变当前方向，`/next <任务>`
排到当前轮之后，`/interrupt` 在下一个安全边界终止；`/plan` 查看版本化计划。`/history` 查看历史，
`/status` 查看会话和后台任务，`/exit` 退出。运行事件和对话记录保存在受保护的
`.aicoding-agent/`，不会作为候选代码发布。

后台任务不是任意 Shell：模型只能用 `start_background_process` 启动任务合同中的精确验证命令，
进程使用 `executable + argv` 异步执行。Git 推送、Teambition 写入和部署仍不属于模型工具。

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
