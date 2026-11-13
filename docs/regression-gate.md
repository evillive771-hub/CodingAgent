# 统一回归验收

每个实现任务需要强制先 RED 后 GREEN 时，使用 [TDD 双重验证](tdd-gate.md) 中的合同和宿主配置。

启用统一 Gate 后，宿主运行历史任务和当前任务的完整必需测试，并绑定代码、测试集合、环境与报告。前端配置默认要求命令测试和必需 Playwright 场景同时存在并通过。仅有模型提交、截图或通过率统计不能推进代码基线。

## 执行与恢复

`RegressionController` 从最近接受的实际文件快照创建独立 Git worktree。初始快照标为未验证；只有宿主 Gate 通过后，快照、历史回归集合和报告才通过原子 manifest 一起更新。新增文件也包含在快照中，源仓库的 Git HEAD 和 index 保持原样。

断言失败保留候选和报告，把失败用例及日志摘要反馈给模型，并从通过基线重新尝试一次。第二次仍失败进入 `blocked`。环境故障默认在同一候选上重跑一次，不调用模型改代码；总时间耗尽直接阻断。所有尝试、恢复和最终验收共享持久化的模型轮数、工具次数、浏览器次数和原始截止时间。每个浏览器场景的 `max_runs` 需要为模型反馈和最终宿主运行共同预留次数。

`DeliveryWorkflow` 在 `CODING → REGRESSION_VERIFYING → INSPECTING_CHANGES` 接入，检查、构建、部署之前再次检查证据。部署后的真实设备 Playwright 验证和原有审批、回滚仍继续执行。`run` 和 Chat 模式只把验收通过的修改应用回用户工作区；验收期间出现外部文件修改会阻断采用。Chat 的采用记录进入原有 MutationJournal，保留手动代码恢复能力。恢复到旧代码后，如果它与当前接受基线不一致，新任务会阻断，需要宿主明确建立并验证对应基线。

多 Agent 可通过宿主 `MultiAgentCoordinator(regression_controller=...)` 启用相同验收。协调器在 integration 锁内构建临时组合版本，执行完整回归，再检查候选和 integration 是否变化。只有组合通过才推进真实 integration 和任务完成状态。也可直接用 `controller.run(..., accept=False)` 验证独立候选，然后调用 `controller.integrate(outcome)` 验证并接受组合。

## 配置和使用

新建宿主管理的 `regression.json`，例如：

```json
{
  "command_suites": "command-suites.json",
  "infra_retries": 1,
  "require_both": true
}
```

`command_suites` 可省略，继续使用合同原有 `verification_commands`。推荐固定命令测试放在候选工作区外，由宿主读取 `command-suites.json`：

```json
{
  "suites": [
    {
      "id": "historical-api",
      "cwd": "host-tests",
      "command": ["D:/Github/CodingAgent/.venv/Scripts/python.exe", "-m", "pytest", "test_api.py", "-q"],
      "test_files": ["test_api.py"],
      "reporter": "pytest_junit",
      "expected_tests": ["test_api::test_login"],
      "timeout_seconds": 120
    }
  ]
}
```

测试 `id` 与合同 `verification_commands` 中的 ID 对应。`cwd` 相对配置目录解析，`test_files` 相对 `cwd` 解析。固定命令通过 `REGRESSION_WORKSPACE` 或 `EVAL_PROJECT_ROOT` 环境变量读取候选代码。`pytest_junit` 检查精确用例 ID，跳过、缺失、重复用例或退出码不一致都会阻断；`exit_code` 保留普通命令兼容性。

浏览器套件继续使用现有 `--browser-suites`，合同中的 `browser_verifications` 设置 `required: true`。浏览器测试文件、配置和完整用例列表由宿主固定。

```powershell
$env:UV_CACHE_DIR = 'D:\Cache\uv'
& .\.tools\uv\bin\uv.exe run aicoding-agent run --task D:\Projects\demo\task.json --workspace D:\Projects\demo --browser-suites D:\Projects\host\browser-suites.json --regression-config D:\Projects\host\regression.json --runtime-dir D:\Cache\CodingAgent\regression-demo
```

`chat` 和 `deliver-phase-one` 同样支持 `--regression-config` 和 `--browser-suites`。同一项目的连续任务应使用同一运行目录，以累积回归集。纯命令项目可显式配置 `require_both: false`。未传入 `--regression-config` 时继续原有流程，避免旧合同被强制加入浏览器要求。

## 证据与边界

运行目录下的 `regression/baseline/accepted.json` 保存基线、回归集版本、指纹和已接受报告引用；`baseline/snapshots` 保存实际文件；`baseline/candidates` 保存候选 worktree；`regression/evidence` 保存按测试 ID 记录的结果和日志；`regression/runs`、`budgets` 保存恢复状态；`events.jsonl` 保存追加式审计事件。

历史测试 ID 不能静默替换命令或降低必需性，已接受测试的指纹必须保持一致。传统工作区命令使用保守的测试文件与配置指纹：增加或调整其测试目录也可能被视为测试集合变化。需要持续扩展测试时，优先为新需求使用独立 ID 和外部固定 `CommandSuite`，避免改动历史套件。需求正式变化后的历史测试版本迁移由宿主另行处理，当前版本没有自动删除、跳过或弱化旧断言的入口。

依赖、凭据和运行产物不进入基线。`node_modules`、虚拟环境等不会自动复制到候选；测试命令需要引用既有外部运行时，或由宿主事先准备依赖。HTTP 预览可以运行在候选目录，浏览器可执行文件与 Playwright 包仍引用 D 盘现有安装。这里的代码工作区恢复与 SSH 测试设备回滚分开管理。
