# Conversational demo cases

这些目录是只读的起始模板。使用 `scripts/start-chat-demo.ps1` 将所选模板复制到
`runtime/chat-demos/`，随后自动进入持续对话；Agent 只修改副本。

| Case | 类型 | 推荐首句 |
| --- | --- | --- |
| `01-counter-clamp` | 状态逻辑 Bug | `计数器点击减少后会出现负数，请定位、修复并验证。` |
| `02-search-normalization` | 前端搜索需求 | `让搜索忽略大小写和首尾空格，空搜索返回全部结果。` |
| `03-accessible-dialog` | A11y 需求 | `按照 TASK.md 完成登录弹窗的无障碍改造并验证。` |

可以在同一会话继续输入：

- `解释你刚才修改了什么。`
- `再补一个边界场景测试。`
- `有哪些剩余风险？`
- `把按钮文案改成中文，但不要破坏测试。`

每个目录的 `.aicoding-agent.json` 固定了允许工具和精确验证命令。示例不允许任意 Shell，
测试使用本机 Node.js 的 `node --test`。
