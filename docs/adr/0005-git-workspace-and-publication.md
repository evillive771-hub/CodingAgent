# ADR 0005：隔离 Git 工作区与候选分支发布

## 状态

已接受。

## 背景

Coding Agent 不能直接在开发者 checkout 中修改代码，也不能在审批后自动合并或覆盖远端分支。
工作区准备、审批摘要和发布操作还必须在进程重启或临时推送失败时保持可诊断和可重试。

## 决策

1. 从已有本地 checkout 的明确 commit 创建 detached worktree；worktree 根目录与源仓库分离。
2. worktree 租约绑定运行 ID、完整任务合同哈希、源仓库路径和冻结基线。
3. 变更摘要规范化为基线、路径、文件类型/模式和最终内容，不依赖暂存区状态。
4. 审批后使用 `git commit-tree` 创建候选 commit，不移动 worktree `HEAD`。
5. 只推送确定命名的新候选分支，禁止 force push 和自动 merge。
6. 使用 pending/completed Git refs 记录发布状态；push hook 指向工作区外的空目录。
7. 成功完成的托管 worktree 自动释放；失败现场默认保留，以便诊断或显式清理。

## 结果

Agent 修改与开发者工作区隔离，审批同时绑定基线和最终内容，候选分支推送可以安全重试。
代价是本地仓库会增加内部发布 refs，失败 worktree 需要后续清理策略；远端 clone/fetch、凭据、
MR/PR 创建和 submodule 变化尚需独立适配器。
