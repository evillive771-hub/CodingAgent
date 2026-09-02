# TODO

## MVP 后续集成

- [ ] 根据企业 Teambition API 文档实现鉴权、工单查询、幂等写回和状态转测试。
- [x] 实现本地 checkout 的隔离 worktree、规范化差异哈希和审批后候选分支推送。
- [ ] 实现 GitLab/GitHub 凭据解析、安全 clone/fetch 和远端基线冻结。
- [ ] 实现 GitLab/GitHub MR/PR 创建适配器；不自动合并。
- [ ] 明确测试环境部署链路并实现 `TestEnvironmentPublisher`；当前仅提供 Fake/Local 端口。
- [ ] 为具体前端项目配置 lint、typecheck、unit、build 和 Playwright 验证命令。
- [ ] 接入 Playwright 截图、视频、控制台错误和失败请求证据。
- [ ] 实现 PostgreSQL 任务、租约、幂等预留和 Worker 恢复。
- [x] 根据 Checkpoint 从稳定阶段恢复工作流，已完成副作用使用原幂等键跳过或安全重试。
- [ ] 实现控制台与人工审批 API。
- [ ] 加入 Lighthouse、axe-core、视觉回归和浏览器兼容性质量门。

## 明确不自动化

- [ ] 生产发布只生成审批包，不允许 Agent 自动执行。
- [ ] 首版不做自动合并、通用 MCP、多 Agent 和长期语义记忆。

## 已完成的质量门槛

- [x] Git worktree 模块独立测试通过后接入 `DeliveryWorkflow`。
- [x] Git 候选分支发布模块独立测试通过后完成真实本地 bare remote 集成测试。
- [x] worktree 租约绑定任务合同哈希、基线 commit 和运行 ID。
- [x] 发布使用 pending/completed Git refs 支持幂等重试，并禁用仓库 push hook。
- [x] 结构化审批包绑定合同、Agent 结果、变更、测试部署回执和验证证据。
- [x] 持久化层拒绝不安全的运行 ID，工作流拒绝覆盖已有运行。
- [x] 持续对话会话保留并恢复历史，支持 Agent 主动请求用户澄清。
- [x] 三个零 npm 依赖的前端实例具有可重复的失败基线和独立验证合同。
- [x] 使用真实 MiMo 完成“修复并验证 → 重启会话 → 继续追问”冒烟测试。
- [x] 版本化 Plan、持久化 Steering Inbox 和运行中 `/steer /next /interrupt`。
- [x] 异步 HookBus 保持 PolicyEngine 权威，扩展点不能提升工具权限。
- [x] 大型工具结果 Evidence 归档，压缩前保存完整 transcript 并保护关键运行状态。
- [x] 后台任务仅允许精确验证命令，持久化状态并在安全边界注入完成通知。
- [x] 写工具使用进程内锁与 OS 逐文件锁，跨 Agent 冲突超时关闭并采用原子替换。
