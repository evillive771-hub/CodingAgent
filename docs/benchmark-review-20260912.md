# 2026-09-12 基准实验审查与改进

审查对象：`D:/Github/benchmark-runs/scale-20260911-095313`。被测项目为 `D:/Github/CodingAgent`，模型为 `mimo-v2.5-pro`。本次工作审查既有结果、修复维护代码并运行离线回归，不构成新一轮模型 Pass@1 实验。

## 结果核对

70 个正式任务均有记录，无重复或未执行任务。排除 7 项，其中基础设施问题 5 项、无效控制组 2 项；有效样本 63 项，官方通过 11 项，Pass@1 为 17.5%。

Agent 声明完成 52 项，其中通过 10 项、失败 42 项。完成声明对应的通过率为 10/52 = 19.2%，失败率为 42/52 = 80.8%。原报告 `false_positive` 的 42/63 = 66.7% 使用全部有效任务为分母，不能直接解释为“完成后的失败概率”。另有 1 项官方通过但 Agent 未正常完成。

Web-Bench 的 canvas、react、less 分别通过 1/20、4/20、6/20。ReactBench 有效样本只有 3 个 hard，全部失败；medium 无有效样本，不能据此比较难度或推断总体 React 能力。3 个有效 ReactBench 任务均为 tests=1、react_doctor=0，表明功能测试分量通过也不等于总 reward 通过。

有效任务耗时中位数为 277 秒，p90 为 543.6 秒，p95 为 629.8 秒。分位数采用排序后位置 `(n-1)×p` 线性插值，最终保留 1 位小数。

## 已定位的问题

1. **完成证据不足。** canvas task-2 的事件显示，合同未配置验证命令，停止门禁仍报告验证证据齐全；独立语义评审的唯一引用为 `outcome`，结论来自 Agent 自述。空验证集合和结构化输出不能证明运行正确。

2. **连续任务的失败会累积。** canvas task-2 将 CSS 背景改为 canvas 绘制，本任务新增的 3 项测试全部通过，但累计测试中的 task-1 背景检查失败，最终为 5 passed / 1 failed；task-3、task-4 继续继承这项失败。需要区分新增功能错误、已有行为回退及需求与验证实现的潜在冲突。不能把后续所有失败视为互相独立的能力失败，也不能为提高分数直接修改官方验证器。

3. **交互状态边界理解有误。** react task-5 第一次打开弹窗时显示 0，官方期望 1；日志为 11 passed / 1 failed。应按初始状态、首次事件、重复事件、关闭/重置逐项检查，避免仅凭“已增加计数功能”就提交。

4. **部分失败仍需诊断。** less task-4 为 14 passed / 1 failed，错误为 `Page.handleJavaScriptDialog` session closed。单次日志不足以判断是产品交互缺陷还是浏览器/驱动偶发问题，原成绩保留。AppFlowy 任务用尽 30 轮且无文件修改；可访问的事件文件仅有终态及元数据，不足以确定中间为何耗尽预算，不能断言是反复读取或某个具体工具导致。

5. **事故修复未同步到维护版。** 维护版 ReactBench 驱动丢失 exception_message、只根据 agent 组判断 infra、先标记 invalid control、路径超限中止整个阶段，以及已完成 infra 任务在 resume 时再次中止。事故副本中已有部分修复，但后续实验仍可能重新触发。

6. **代码审查发现验证状态失效问题。** 前台验证先通过、随后非零退出/超时/无法启动，旧成功标记没有清除。后台验证开始后发生代码修改，却可能在通知送达时被标记为“修改后验证”；较早后台成功还可能覆盖较新失败。此项由本次回归复现，不声称其造成了这批实验中的某个确定失败。

## 本次实现

- 完成门禁明确说明未配置验证命令时运行正确性尚未验证，兼容原先 code-only 合同的提交边界。
- 隔离评审区分 Agent 自述与主机工具证据；修改代码的完成结论必须引用主机证据，不能只引用 outcome、计划或任务总结。证据不足返回 incomplete 和可执行反馈。
- 向隔离评审提供最后修改之后的真实 read_file 片段，最多 8 条、每条正文最多 4,000 字符；写入后清除旧片段，验证证据按修改序号过滤。片段用于静态审查，不能替代浏览器或运行测试，也不保证涵盖全部文件和验收条件。
- 验证开始即清除上次成功，失败、启动异常和超时均不能沿用旧结果。后台验证按开始序号和最近尝试编号判断，迟到通知不能覆盖新失败或验证后修改。
- 系统提示增加已有行为兼容检查、交互事件序列检查、最终代码回读及未测试风险说明。提示改进的模型效果尚未经过新正式实验验证。
- 维护版驱动保留异常消息；任一控制组或 agent 组的 infra 优先处理且不计分；路径超限记录证据并允许后续任务推进；resume 不重复执行已完成尝试。保留运行摘要和 archive 证据支持。
- 维护版报告保留旧指标字段，增加 `completed_failure_rate`、`completion_precision`、p90/p95，并在 HTML/Markdown 中说明分母和插值算法。

本次涉及的 Agent 文件为 `agent/gate.py`、`agent/goal.py`、`agent/loop.py`、`tools/base.py`、`tools/builtin.py`、`tools/executor.py`、`context/builder.py`（均位于 `src/aicoding_agent`）。新增 `tests/unit/test_completion_evidence.py`，更新 `tests/unit/test_goal_evaluator.py`。工作区已有大量未提交改动，本次未将其作为本次新增成果或提交到 Git。

维护版工具为 `D:/Github/benchmark-runs/tools/scale_reactbench_runner.py` 和 `scale_report.py`，测试位于同目录 `tests/`。历史 run 的 driver、正式结果和报告保持原状。

## 验证与后续实验

执行项目本地 uv 的 `run ruff check .`、`run pytest`，并执行维护工具的离线 pytest。Ruff 全部通过，Agent 全量 236 项通过（28.48 秒），维护版驱动 10 项通过，报告生成器 9 项通过。另已通过包含工作流的针对性检查。覆盖非零退出、超时、启动失败、后台迟到、验证期间修改、自述无独立证据、遗漏 changed_files、只读任务兼容、片段上限、陈旧证据，以及 infra 分类、恢复推进、结果去重、指标零分母和小样本插值。

新统计报告位于 [独立审核目录](D:/Github/benchmark-runs/audits/scale-20260911-095313-report-metrics-20260912/report/index.html)，从历史事件和 manifest 重算展示，未重新运行模型。修复是否提高官方通过率仍需验证：新建实验目录、固定新的 SUT 指纹，先做原协议要求的校准，再按相同样本和预算对照运行。若开放公开验证命令，应单独列为协议变更的实验组，不能与原 code-only 组混算；隐藏测试继续只由驱动使用。

建议下一轮同时报告累计通过率、当前任务新增测试表现、旧测试回退数量及实际验证覆盖，区分功能实现、连续任务回退和验收证据。less 的浏览器异常另做诊断复跑并记录次数，不能覆盖原始首轮结果。

主要证据：原实验 `report/summary.json`、`manifest.json`、`notes/incident-20260911-disk-and-infra-classification.md`、`web-bench/canvas/evidence/task-2/{agent-events.jsonl,playwright.txt}`、`web-bench/react/evidence/task-5/playwright.txt`、`web-bench/less/evidence/task-4/playwright.txt`，以及 `reactbench/` 中三个有效任务的 `result.json`。
