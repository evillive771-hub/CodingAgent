# ADR-0017：隔离且不可递归的只读 Subagent

## 状态

Accepted

## 背景

大型仓库探索、独立复核和测试失败分析会占用主 Agent 上下文。直接让多个 Agent 共享写工作区会引入
语义冲突，而把完整主 transcript 交给子 Agent 又失去上下文隔离价值。

## 决策

- `delegate_readonly` 只支持 explore、review、test_analyst 三类有界任务；父 Agent 传入任务、建议
  路径和轮次预算，不传入完整 transcript；
- 子 Agent 使用独立模型客户端和全新消息列表，只暴露 read_file、list_files、search_text 和
  submit_subagent_report；合同排除写工具、进程、用户提问、Plan、Task DAG 和 delegate_readonly；
- 最多三个只读子任务并行，每个子 Agent 有独立轮次、工具、时间和输出预算，并复用统一模型恢复；
- 子报告必须结构化提交。每个 finding 的路径和行号由宿主重新读取，实际行片段由宿主附加；不存在、
  越界或工作区外证据被拒绝；
- 返回主 Agent 的报告按完整 finding 裁剪，保持有效 JSON。子 Agent 只返回最终报告，不回传内部
  transcript 或全部工具输出；
- 本模块不允许子 Agent 修改代码，也不实现任务认领、worktree 分配或合并。

## 结果

主上下文可以获得独立、具名且有真实文件行支撑的探索结论，同时写权限仍集中在主 Agent。Task DAG
已为未来 owner/任务认领保留结构，但多 Agent 写入仍属于后续独立模块。
