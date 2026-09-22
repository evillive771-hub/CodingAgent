# ADR-0020：宿主控制的多 Agent Coordinator

## 状态

Accepted

## 背景

只读 Subagent 能并行探索，但大型实现需要多个写 Agent。让多个模型直接共享工作区、自由认领任务或
执行 Git merge 会产生覆盖、重复执行、依赖缺失和权限扩大。逐文件锁也不能解决跨文件语义冲突。

## 决策

- `TaskGraphManager.claim_next` 在宿主异步锁内原子选择依赖已完成的 pending 节点，绑定唯一 worker、
  增加 attempt 并持久化新图版本；完成、阻塞、worktree 绑定和阻塞依赖传播也由宿主方法执行；
- `MultiAgentCoordinator` 最多启动八个 worker。每个 worker 通过工厂获得隔离 Agent 实例，并为每个
  claim 从当前 integration revision 创建独立 detached Git worktree；源 checkout 必须 clean；
- Worker 结果必须为 completed 且 `changed_files` 与 Git 快照完全一致。Merge Gate 使用无 Shell、
  安全环境重新运行至少一个合同验证命令，并要求复验前后变更哈希不变；
- 通过候选以 binary patch 合成到单独托管的 integration worktree，并生成本地 detached commits。
  Git hooks 被指向工作区外空目录；源 checkout 不 reset、不 commit、不切分支；
- 并行候选修改同一路径时，只有先前该路径 revision 已是当前候选 base 的祖先才允许继续，否则保守
  阻塞。这样依赖任务可继续修改前置任务文件，而同基线并行覆盖会失败；
- Worker 事件按 owner 追加到有界持久 mailbox。失败 worktree 和 integration worktree 保留供检查，
  阻塞通过 DAG 传播；Coordinator 不 push、不创建 MR/PR、不部署、不写 Teambition、不自动合并源分支。

## 结果

写 Agent 获得真正的并发和上下文/文件系统隔离，依赖与候选合成仍由确定性宿主控制。Integration
revision 只是待审候选，不是发布或合并结果。当前入口是 Python 宿主 API；在把它接入对话命令或
DeliveryWorkflow 前，还需确定任务拆分合同、模型容量和人工启动/清理体验。
