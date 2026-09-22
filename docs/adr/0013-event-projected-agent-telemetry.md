# ADR-0013：从运行事件投影 Agent 可观测指标

## 状态

Accepted

## 背景

现有 JSONL 已保存轮次、模型响应和工具结果，但缺少统一的上下文占用、耗时、Token、重试和失败
计数；交互用户也无法在 Agent 运行时快速查看这些信息。

## 决策

- Agent Loop 在每次模型调用前记录 `context.prepared`，只包含原始/实际字符数、消息数和是否压缩，
  不记录 Prompt 正文；
- `model.response` 增加端到端毫秒耗时并沿用供应商数值 usage；恢复模块继续记录重试；
- `tool.finished` 增加包含 Hook 和文件锁等待在内的调用耗时；Policy/Hook 拒绝计为失败调用；
- `TelemetryRegistry` 仅作为 append-only 事件的内存投影，JSONL 事件仍是审计和恢复的权威来源；
- 对话 `/metrics` 显示当前或最近 Run 的聚合值，不显示提示词、工具参数或工具输出。

## 结果

开发和运行时可快速识别上下文膨胀、慢模型、重试和工具失败，不引入第二套持久化事实源，也不会
把敏感正文复制到指标界面。
