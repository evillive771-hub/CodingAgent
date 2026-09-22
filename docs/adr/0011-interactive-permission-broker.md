# ADR-0011：交互式 ASK 权限确认

## 状态

Accepted

## 背景

`PolicyEngine` 已支持 `allow / deny / ask`，但过去 `ToolExecutor` 将 `ask` 一律按拒绝处理。这适合
无人值守 Worker，却无法让对话用户确认一个合同已允许、但仍需逐次审批的调用。

## 决策

- `deny` 永远终止调用；Permission Broker 只接收 PolicyEngine 已返回 `ask` 的请求，不能覆盖合同
  allowlist、路径边界、进程白名单或 Hook 拒绝；
- 无 Permission Broker 的运行继续失败关闭；普通 `run` 和交付 Worker 默认不创建 Broker；
- 对话模式使用追加式 `PermissionStore`，保存请求、参数 SHA-256 和决定；参数预览递归脱敏并限制
  深度、字段数、列表长度和字符串长度；
- `/allow <id>` 只批准一次，`/allow <id> session` 只复用相同会话中工具名和完整参数哈希都一致的
  请求，`/deny <id>` 拒绝；
- 未决请求在超时或宿主重启后自动拒绝。`permission.resolved` 审计事件写入后才解除工具等待；
- 本模块不新增外部工具。Git 推送、Teambition 写入、部署和生产发布仍不进入模型工具池。

## 结果

交互式会话可以在 Agent Loop 运行期间安全处理 `ask`，同时保持无人值守和确定性交付流程的原有
边界。会话级批准故意绑定精确参数，而不是授予整个工具的宽泛权限。
