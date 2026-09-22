# ADR-0014：合同绑定的前端结构化 Diagnostics

## 状态

Accepted

## 背景

`run_process` 只允许运行精确验证命令，但其输出仍是原始文本。前端任务常需要从 TypeScript 或
ESLint 输出中快速提取文件、行列、级别和规则代码；同时不能为了便利重新开放任意 Shell。

## 决策

- `TaskContract.diagnostic_commands` 冻结诊断 ID、executable、argv、cwd、parser 和 timeout；
- 模型的 `get_diagnostics` 输入只有诊断 ID 和结果上限，不能提供程序、参数或 Shell 字符串；
- PolicyEngine 同时校验工具 allowlist、诊断 ID、executable allowlist、cwd 路径边界；
- 首版解析 TypeScript 文本格式和 ESLint JSON，过滤工作区外文件，限制记录数、单条消息、原始预览、
  结构化 data 和模型可见 output；未知格式保留有界原始预览；
- 诊断命令退出非零表示发现问题，不等于工具执行失败；返回值明确包含 exit code 和错误/警告数量；
- Diagnostics 不产生 `verification_id`，不能满足 Stop Gate。最终验证仍必须运行合同中的精确
  `verification_commands`。

## 结果

Agent 能以稳定结构定位常见前端编译和 lint 问题，同时保留已有进程权限边界。项目需要在合同中
显式配置适合自身脚本和非彩色/机器可读输出的命令。
