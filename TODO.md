# TODO

## Agent 侧演进（暂不实现 MCP）

- [x] 动态 Skill 目录、启动时哈希冻结和受策略约束的按需加载。
- [x] 交互式权限确认；一次/会话精确参数批准、持久化审计和无人值守失败关闭。
- [x] 统一错误分类、有界模型退避恢复和重复工具失败熔断；宿主不自动重放工具。
- [x] Agent 事件可观测性；上下文、耗时、Token、重试、工具失败和 `/metrics`。
- [x] 合同绑定的 TypeScript/ESLint 结构化 Diagnostics；不替代 Stop Gate 验证。
- [x] 持久化 Task DAG；依赖、环、负责人、attempts、阻塞、证据、Steering 和 Stop Gate。
- [x] 独立 Goal Evaluator；证据 ID 约束，仅在确定性 Stop Gate 通过后运行。
- [x] 只读 Subagent；隔离上下文、真实行片段回填、禁止写入/进程/递归派生。
- [x] 会话分叉与安全恢复；文件快照日志、冲突预检、不可逆 Effect 拒绝和 `/fork /rewind`。
- [x] 带来源、版本、TTL、敏感信息拒绝和显式用户审批的按需精选长期记忆。
- [x] 基于 Task DAG 原子认领、独立 worktree、持久 mailbox 和本地候选合成门禁的多 Agent Coordinator。

## 分层记忆与 Context Rollover

- [x] 模型窗口、输出预留、警告缓冲和压缩缓冲组成的 Token 预算管理。
- [x] 旧工具输出 MicroCompact，大型内容先保存完整 transcript 和 Evidence 哈希引用。
- [x] 独立无工具模型维护 Notes checkpoint；旧消息按不可变 Window/Item 进入 History。
- [x] Prompt Too Long 只缩小 checkpoint 输入，原始 History 保留；连续三次失败后熔断。
- [x] New Context Boundary、Notes 版本、History 引用、覆盖范围和来源哈希追加持久化。
- [x] 消息级 append-only Journal、安全中断检测和不自动重放工具的恢复。
- [x] `/new-context [focus]`、兼容 `/compact`、`/context`、`/resume [--force|--discard]`。
- [x] 自动恢复仅处理无悬空工具调用的安全状态；歧义恢复必须人工确认或放弃。
- [x] L2 Notes + History 与需审批、带 TTL 的 L3 精选项目记忆保持隔离。

## MVP 后续集成

- [ ] 根据企业 Teambition API 文档实现鉴权、工单查询、幂等写回和状态转测试。
- [x] 实现本地 checkout 的隔离 worktree、规范化差异哈希和审批后候选分支推送。
- [ ] 实现 GitLab/GitHub 凭据解析、安全 clone/fetch 和远端基线冻结。
- [ ] 实现 GitLab/GitHub MR/PR 创建适配器；不自动合并。
- [x] 实现阶段一 SFTP/NFS/固定 SSH 命令测试部署、远端哈希校验、回滚和设备隔离记录。
- [x] 支持合同配置 lint、typecheck、unit、build 和制品路径；具体项目只需填写任务合同。
- [x] 接入 Playwright 截图、Trace、视频、控制台错误和失败请求证据。
- [ ] 实现 PostgreSQL 任务、租约、幂等预留和 Worker 恢复。
- [x] 根据 Checkpoint 从稳定阶段恢复工作流，已完成副作用使用原幂等键跳过或安全重试。
- [x] 实现阶段一终端人工审批；Web 控制台与审批 API 留待后续阶段。
- [x] 回滚失败持久隔离测试设备，后续发布失败关闭；提供带人工归属和实时健康检查的恢复命令。
- [ ] 在目标企业网络的真实 NFS/SSH/Nginx 测试设备执行阶段一最终演练并归档证据（环境验收，不是代码缺口）。
- [ ] 加入 Lighthouse、axe-core、视觉回归和浏览器兼容性质量门。

## 明确不自动化

- [x] 生产发布只生成审批包，不允许 Agent 自动执行。
- [x] 不做自动合并和通用 MCP；生产发布边界不因 Agent 能力演进而变化。

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
- [x] 长会话使用 Token 感知的 Context Rollover 和可精确恢复的 History，并保留字符压缩兼容回退。
- [x] 后台任务仅允许精确验证命令，持久化状态并在安全边界注入完成通知。
- [x] 写工具使用进程内锁与 OS 逐文件锁，跨 Agent 冲突超时关闭并采用原子替换。
