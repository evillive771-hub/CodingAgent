# ADR-0022：持久 Agent Team Runtime 与人工门禁

## 状态

Accepted

## 背景

ADR-0020 的 Coordinator 能并发执行写任务，但 Worker 在任务板结束后退出，Mailbox 仅记录审计
事件，也没有团队启动确认、双向控制或计划审批。直接复制教学项目的 daemon thread、可选
worktree 和模型自行 merge 会削弱现有的异步接口、合同权限与确定性合成边界。

## 决策

- Worker 使用 `IDLE → WORK → IDLE → STOPPING → STOPPED` 生命周期。每个 Worker 仅通过
  `agent_factory` 创建一次 Agent，并把有界的任务目标与结果历史传入后续任务；
- `keep_alive=True` 时，任务板暂时清空不会结束 Worker。Worker 在安全边界优先处理 Direct 和
  Shutdown 消息，然后认领依赖已完成且与 `preferred_owner` 相容的任务；
- `TeamMessageBus` 使用类型化、追加式 JSONL 消息和序号游标。Result、Idle、Direct、Shutdown、
  Plan Approval 和 Reassignment 具有独立类型；底层通过异步锁与 OS advisory lock 串行写入；
- Lead 的 `propose_team` 工具只能记录带幂等键的提案。用户必须通过独立宿主命令批准，
  `MultiAgentCoordinator` 才能启动。批准的角色名称成为 Worker 身份，职责进入其上下文；
- `TaskGraphStore` 的读写使用 OS 文件锁。多个 Manager 发生版本竞争时重新加载最新快照后重试，
  防止不同进程重复认领同一节点；
- 运行中只允许重定向 pending 任务，或在依赖全部完成后把 blocked 任务重新入队。正在执行的任务
  不被强制抢占；
- 需要计划审批的 `TaskContract` 必须包含 `execution_plan`。批准绑定 team、worker、task、attempt
  和计划哈希；任一项变化都会使旧批准失效。Coordinator 在批准前不启动写 Agent；
- 保留 ADR-0020 的强制任务 Worktree、宿主 Merge Gate、Integration Worktree、冲突阻塞以及不
  push、不部署、不更新 Teambition 的边界。

## 对话命令

- `/team <team-id>`、`/team-approve`、`/team-cancel`：查看和处理团队启动提案；
- `/team-send`、`/team-shutdown`：向持久 Worker 发消息或请求优雅关闭；
- `/team-plan`、`/team-plan-approve`、`/team-plan-reject`：查看和处理版本绑定的计划申请；
- `/task-assign`、`/task-retry`：安全调整尚未执行或已阻塞的任务。

## 结果

当前实现兼具 s13 风格的持久身份、IDLE、自主认领和双向协议，以及原有的确定性验证与 Git 隔离。
它仍由 Python 宿主显式创建 Coordinator；对话审批不会把 Git push、部署或外部系统写入暴露为
模型工具。
