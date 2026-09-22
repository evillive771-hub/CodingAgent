# ADR-0010：按需加载工作区 Skill

## 状态

Accepted

## 背景

将所有项目规范、框架经验和排障流程直接放入 System Prompt 会持续占用上下文，并使与当前任务
无关的说明干扰模型。另一方面，允许 Skill 执行任意脚本会绕过现有工具策略和任务合同。

## 决策

- `TaskContract.skill_directories` 明确 Skill 根目录，默认只检查工作区内的 `skills/`；
- 每个 Skill 使用 `<root>/<name>/SKILL.md`，frontmatter 必须包含与目录一致的受限名称和简短描述；
- Agent 启动时只发现元数据并冻结 SHA-256，Prompt 只包含名称、转义后的描述和哈希；
- 完整文档只能通过 Pydantic `load_skill` 只读工具按名称加载；工具仍受合同 allowlist、PolicyEngine、
  输出预算、Hook 和事件审计约束；
- 加载时重新解析安全路径并复核哈希。文档、符号链接或路径在运行中变化时失败关闭，要求新建 Run；
- Skill 不执行脚本，也不能修改任务合同、工具权限、验证命令或发布边界。

## 结果

相关知识可按需进入上下文，未使用的 Skill 只消耗少量目录信息。Skill 内容可按项目版本管理并由
事件中的哈希审计。代价是 Skill 更新不会影响正在执行的 Run，这是有意的确定性边界。
