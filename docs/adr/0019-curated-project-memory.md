# ADR-0019：带审批与有效期的精选项目记忆

## 状态

Accepted

## 背景

长期对话需要保留用户偏好和稳定项目约定，但自动把历史摘要永久写入 Prompt 会积累错误、泄漏敏感
信息并让过期约定继续影响执行。原始 transcript 也不等于经过用户认可的长期知识。

## 决策

- `MemoryManager` 使用追加式 JSONL 保存 topic、version、source、source_run_id、status、TTL、创建、
  到期和用户审批时间；同 topic 只检索最高的已批准且未过期版本；
- 模型只能调用 `propose_memory` 创建 pending 建议，不能批准或拒绝。用户使用
  `/memory-approve` 或 `/memory-reject` 决策，也可用 `/remember` 直接写入明确批准的用户记忆；
- `recall_memory` 只按需搜索，不在每轮自动注入记忆目录或全文。返回条数、单条内容和整体 JSON 受
  任务合同输出预算限制；
- 私钥标记、常见 Token 前缀、Bearer 凭据及 key/password/secret 赋值在持久化前拒绝。记忆内容不
  能扩大合同、Policy、验证、Git、Teambition、部署或生产审批权限；
- 分叉会话共享同一项目记忆，但不复制独立副本。过期记录和拒绝记录保留审计，不参与 recall。

## 结果

Agent 可以在需要时复用经过用户确认的稳定约定，同时来源、版本和时效均可见。记忆是辅助上下文，
代码、任务合同和宿主证据仍是事实来源。当前敏感信息检测是保守规则集，不替代运行目录权限和通用
秘密扫描，因此命中内容直接拒绝而不尝试自动脱敏保存。
