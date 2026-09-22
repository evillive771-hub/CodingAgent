# ADR-0015：持久化依赖 Task DAG

## 状态

Accepted

## 背景

现有 Plan 适合展示单 Agent 当前执行顺序，但不能表达任务依赖、负责人、重试次数、阻塞原因、证据
和未来 worktree 归属，也不足以支撑崩溃恢复或后续原子任务认领。

## 决策

- 保留 Plan；新增会话级 append-only `TaskGraphStore` 和版本化 `TaskGraphSnapshot`；
- 节点包含稳定 ID、标题、描述、状态、`blocked_by`、阻塞原因、owner、evidence、worktree 引用和
  attempts；
- 每个快照校验唯一 ID、依赖存在、无环、依赖完成顺序、完成证据，以及同一 owner 最多一个
  `in_progress`；不同 owner 可并行，为后续 Subagent/Coordinator 留出空间；
- 活跃图更新不能删除节点、减少 attempts 或回退 completed。用户 Steering 先把旧图标记为
  superseded，随后才允许用新方向替换；
- `update_task_graph` 是 Pydantic WRITE 风险工具，只修改宿主持久化任务状态；Stop Gate 拒绝
  superseded 或仍有未完成节点的图；
- 当前实现不分配 worktree、不派生 Agent，也不执行并发写入；这些能力在后续模块独立接入。

## 结果

复杂任务具有可审计、可恢复的依赖状态，同时没有提前引入多 Agent 调度。Task DAG 是完成门禁的一
部分，但不能代替代码验证证据。
