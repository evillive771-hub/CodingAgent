# ADR-0016：确定性门禁后的隔离 Goal Evaluator

## 状态

Accepted

## 背景

测试退出码和新鲜度可以确定性判断，但“交互是否符合需求”“解释是否覆盖全部验收语义”等条件无法
完全由命令结果表达。让主 Agent 自己宣布完成会形成自我评审，而让模型结论覆盖失败测试又不安全。

## 决策

- `CompletionGate` 始终先执行；只有结构化 outcome、Plan/Task DAG 和验证证据全部通过后才调用
  Goal Evaluator；
- CLI 为 evaluator 创建独立模型客户端和独立上下文，不传入主 transcript，也不向其提供可执行工具；
- evaluator 只能调用一个 Schema 工具 `submit_goal_evaluation`，返回 complete、incomplete、
  needs_user 或 impossible；该工具只是结构化输出协议，不由 ToolExecutor 执行；
- 宿主提供有限证据目录。complete 必须至少引用一个已知证据 ID，引用未知证据会触发有界协议重试；
- incomplete 将缺口注入主 Agent Loop；needs_user 暂停对话；impossible 和 evaluator 协议/模型耗尽
  失败关闭；
- evaluator 的状态、耗时、attempts 和 Token usage 写入运行事件并投影到 `/metrics`。

## 结果

自然语言验收获得独立复核，同时模型评审不能绕过失败测试、陈旧验证或发布审批。代价是每次候选
完成增加一次隔离模型调用。
