# ADR-0004：通过兼容层接入 MiMo

- 状态：接受
- 日期：2026-09-01

## 决策

使用项目环境文件中的 MiMo OpenAI-compatible Chat Completions API，但内部模型协议保持供应商无关。

## 兼容约束

- 不依赖 `tool_choice`；
- 保留推理内容、assistant 内容和工具调用历史；
- 工具参数和最终结果全部本地校验；
- 避免复杂联合 Schema；
- 用可选的 live doctor 契约测试验证多轮工具调用。

