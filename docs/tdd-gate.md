# TDD 双重验证

启用 TDD 后，宿主先在未修改的基线副本上确认指定需求断言失败，再启动编码 Agent。实现完成后，当前需求测试、历史命令回归和必需 Playwright 场景全部通过，才能接受候选和更新基线。TDD 证明、完整回归与交付审批使用相同的任务和测试身份。

## 配置已有测试

任务合同新增 `tdd`，按验证 ID 声明 RED 目标用例。命令目标使用宿主外置的 `CommandSuite`，报告类型为 `pytest_junit`；浏览器目标使用现有结构化 Playwright 套件。只有选定目标允许在 RED 中断言失败，其他当前用例和历史回归必须通过。

```json
"tdd": {
  "red_cases": {
    "counter-unit": ["test_counter_unit::test_zero_decrement"],
    "counter-e2e": ["counter cannot decrement below zero"]
  }
}
```

宿主 `regression.json` 可设置 `require_tdd: true`，强制每个任务携带 TDD 定义；`require_both: true` 保持命令与浏览器双重 GREEN。缺少 TDD 定义时阻断编码。旧配置默认 `require_tdd: false`，没有 `tdd` 的旧合同继续原有流程，旧合同的规范化指纹也不新增空 TDD 字段。

RED 的环境错误、超时、收集错误、缺失或跳过用例均不放行。目标已通过会阻断实现，并返回已有需求可能已满足的原因；不会通过破坏业务代码制造 RED。RED 的测试执行使用独立副本，前后代码指纹必须一致。

## 补充新测试

合同设置 `tdd.propose_tests: true` 后，Agent 先进入提案模式，仅开放读取、搜索和 `submit_test_proposal`，不能写业务文件或执行进程。提案包含 `files`、`red_cases` 和 `requirement_links`；需求关联使用 `acceptance_criteria` 的零起始索引。

宿主必须事先配置测试运行器、完整用例清单和新测试位置，例如当前套件的 `test_files: ["tests/test_counter.py"]`。文件可以暂不存在；模型会收到这些位置和用例清单。提案只能写到这些候选工作区外的测试位置，路径限于 `tests/` 下的测试源码，不能提交启动命令、配置文件、跳过标记或替换历史测试。已有文件只允许与已持久化提案内容完全一致的恢复操作，不能覆盖其他内容。

提案经过宿主检查和冻结后运行 RED。冻结的测试在 GREEN 中保持相同指纹；调整测试需使用明确的新任务或运行身份重新取得 RED。测试草案用于补充需求验证，任务原有固定验收和历史回归继续保留。

## 阶段与恢复

主控制器记录 `prepare → test_design → red_verify → coding → green_verify → finished`，已有需求测试时跳过 `test_design`。原有 AgentLoop CompletionGate 继续要求验证通过；RED 由宿主 TddGate 判定，测试提案通过专用提交工具结束。

证据保存于运行目录的 `regression/tdd`，独立基线副本位于 `regression/tdd-baselines`。Checkpoint 和最终结果包含完整 `tdd_evidence`，绑定合同、基线、目标用例、测试、环境、RED 报告及日志。GREEN 校验重新解析原始 JUnit 和 Playwright 报告；修改汇总 JSON 不能伪造通过或失败。审批包还包含 TDD 证明及其哈希。

RED、提案、编码、GREEN 和重试共用原有持久化预算，`phase_counts` 可查看各阶段的调用分布。恢复不会重复生成已保存的测试提案，已确认 RED 可继续编码，但必须重新校验证据。GREEN 断言失败最多从通过基线重试一次；多 Agent 的组合失败保留候选并阻断，交由任务调度器后续重新安排。浏览器额度包含 RED、模型反馈和最终 GREEN。

多 Agent 每个 Worker 在写代码前取得自己的 RED，组合阶段运行完整 GREEN。直接调用 AgentLoop 编码接口时，TDD 合同需要宿主授权；模型工具不能产生这项授权。源工作区只采用最终接受的代码，未通过任务不会构建或部署。

## Counter 示例

示例位于 `examples/tdd_counter`。源码复用 `examples/browser_feedback/workspace`，宿主测试包含两项 pytest 驱动的 Node 断言和两项真实页面测试。初始 RED 应为零值减一的命令、页面断言失败，正数操作通过；修复后四项均通过。

示例配置引用当前电脑 D 盘已有 Playwright 1.57.0，其他电脑需调整 `browser-suites.json`、JS `require` 和任务命令中的路径。8093 端口需空闲。无需上传服务器即可验证编码与双重验收。

```powershell
$sample = 'D:\Github\CodingAgent\examples\tdd_counter'
$workspace = 'D:\Cache\CodingAgent\tdd-counter\workspace'
New-Item -ItemType Directory -Force $workspace | Out-Null
Copy-Item 'D:\Github\CodingAgent\examples\browser_feedback\workspace\*' $workspace
$env:UV_CACHE_DIR = 'D:\Cache\uv'
$env:PLAYWRIGHT_BROWSERS_PATH = 'D:\Github\CodingAgent\.tools\playwright'
& 'D:\Github\CodingAgent\.tools\uv\bin\uv.exe' --directory D:\Github\CodingAgent run aicoding-agent run --task "$sample\task.json" --workspace $workspace --browser-suites "$sample\browser-suites.json" --regression-config "$sample\regression.json" --runtime-dir D:\Cache\CodingAgent\tdd-counter\runtime --env-file D:\Github\CodingAgent\env.txt
```

重跑已经完成的任务前，不要直接覆盖已接受的源码工作区。需要独立演示时选择新的工作区与运行目录，以保留原始证据和通过基线。
