# CodingAgent 第二轮实验方案

版本：v1.2  
日期：2026-09-16  
状态：真实服务器小型闭环已通过；benchmark runner 前置修复未完成；宿主内存门禁未通过；尚未开始正式实验

v1.2 根据 2026-09-16 的代码、工具和资源复核更新：CodingAgent 当前全量测试为 243 项通过，benchmark tools 仍为 19 项通过；D 盘可用约 62.52 GiB，C 盘可用约 26.39 GiB，Docker daemon 当前停止，宿主可用内存约 0.97 GiB。外部 runner、校准门禁与报告器仍未接入 `submitted_unverified` 和新鲜验证证据语义，正式第二轮实验未创建 root、未产生计分结果。

## 1. 实验目标

本轮验证最近完成的 Agent 改造是否解决上一轮暴露的问题，并在协议和基础设施稳定后，重新测量前端编码任务的完成能力。

实验依次回答五个问题：

1. 修改了代码、但没有执行合同规定验证的任务，是否稳定进入 `submitted_unverified`，不再误报为 `completed`。
2. `completed` 是否始终具有修改后产生的、可审计的确定性验证证据。
3. 语义目标评估器出现错误或给出不一致结论时，是否不会覆盖新鲜且成功的确定性验证结果。
4. 最后三轮预算提醒、上下文压缩和工具错误事件是否完整记录，能否解释任务为何失败。
5. 在协议有效的前提下，Agent 的 Pass@1、完成精度和连续任务抗回归能力是否优于上一轮。

本轮范围仅包括 Agent Loop、Web-Bench、ReactBench、外部验证器和离线报告。Teambition、测试环境部署和生产发布不在本轮范围内。

2026-09-16 已独立完成一次真实服务器小型闭环，覆盖 Agent 修复、确定性测试、构建、SFTP、SSH、测试环境激活、Playwright 和人工审批，运行 ID 为 `ee6723b573e74f46a112ee5411ed5831`。该结果只证明阶段一交付链路可运行，不计入本轮 Pass@1，不替代 Web-Bench/ReactBench 校准，也不改变本轮冻结协议。步骤与边界见 `docs/server-smoke-experiment-plan.md`。

## 2. 上一轮对照基线

对照报告位于 `D:\Github\benchmark-runs\scale-20260911-095313`。后续比较必须使用相同任务、相同隐藏验证器和相同计分口径。

| 指标 | 上一轮结果 |
| --- | ---: |
| 总记录数 | 70/70 |
| 可计分任务 | 63 |
| Pass@1 | 11/63，17.5% |
| Agent 返回 `completed` | 52 |
| `completed` 且验证通过 | 10 |
| 完成精度 | 10/52，19.2% |
| 错误完成率 | 42/63，66.7% |
| Web-Bench canvas | 1/20 |
| Web-Bench react | 4/20 |
| Web-Bench less | 6/20 |
| ReactBench | 0/3；另有 7 个控制组或基础设施排除 |
| 可计分任务中预算耗尽 | 8，且全部失败 |

难度字段在本轮统一为 `easy`、`moderate`、`challenging`；旧数据中的 `chanllenging` 在报表层归一化，原始记录保持不变。

## 3. 实验变量与冻结规则

正式计分前生成全新的实验根目录，并把下列内容写入 `manifest.json`：

- CodingAgent 的 Git commit、dirty diff hash 和配置指纹；
- Web-Bench、ReactBench 的 commit 和样本清单指纹；
- runner、orchestrator、report generator 的 SHA-256；
- 模型供应商、模型名、采样参数、最大轮数和工具清单；
- 每个任务的合同、公开验证命令、隐藏验证器和依赖锁文件指纹；
- 协议版本、实验 ID、开始时间和主机环境摘要。

默认沿用上一轮的 `mimo-v2.5-pro`、同一隐藏验证器和同一任务顺序。密钥和原始授权头不得进入 manifest、事件流或报告。正式任务开始后，不允许修改 prompt、合同、runner、模型参数或验证器；如果发生变化，本轮作废并使用新实验 ID 重开。

为保证可比性，正式 Web-Bench 合同固定为 `max_turns=30`、`max_tool_calls=120`、`max_stop_blocks=5`、`max_elapsed_seconds=840`、`max_tool_output_chars=30000`。任何预算调整都必须使用新的协议版本和实验 ID，不能在运行中修改。

模型只看到需求、项目文件和允许公开的验证命令。隐藏测试文件、期望答案、任务评分文件和 Playwright 隐藏配置必须留在 Host 侧。逻辑失败不重试；瞬时基础设施失败最多重试一次，两次记录均保留。Pass@1 始终取第一次有效 Agent 尝试。

## 4. 正式实验前必须修复的阻断项

当前 runner 不能直接用于下一轮。以下改动全部通过测试后才能生成冻结副本：

CodingAgent 核心已经实现 `submitted_unverified`、最后一次 mutation 后的新鲜验证判定、确定性验证优先、最后三轮预算提醒，以及归档压缩与语义压缩分离计量；项目当前测试基线为 243 项通过。下面第 1、2、7 项仍要求 benchmark 工具正确消费并报告这些事件，不能因为核心代码已经实现而跳过。

1. 所有终态解析器、校准门禁和报告器识别 `agent.submitted_unverified`。代码已修改、外部验证器通过且合同没有公开验证命令时，应记录为成功提交但未由 Agent 自证，不能误判成 Agent 失败，也不能转换为 `completed`。
2. `completed` 必须关联修改后的新鲜确定性验证证据。报告新增 `submitted_unverified`、终态与验证结果矩阵、无证据完成违规数，并保留旧字段以兼容历史报告。
3. Web-Bench 分别保存当前任务验证、既有任务回归验证和累计验证结果。只保存累计结果不足以判断新任务失败还是继承回归。
4. runner 合同加入受策略约束的 `run_process` 及精确 `verification_commands`。如果某个任务没有安全的公开验证命令，则保留 code-only 合同，并预期其终态为 `submitted_unverified`。
5. orchestrator 移除事故期间设置的 2 GiB 正式磁盘门槛，改用第 5 节的资源门禁。
6. ReactBench 工作目录改为 `D:\rbj` 这类短路径。路径自检必须遍历全部 10 个任务、全部 group 和 attempt，并真实创建、读取、删除一个探针文件；最长估算路径不得超过 230 字符，绝对上限为 240。
7. 报告提取预算耗尽、最后三轮提醒、归档压缩、语义压缩、工具错误、恢复和 mutation journal 回滚事件。
8. 测试和实验子进程的 `TEMP`、`TMP` 固定到 `D:\Cache\CodingAgent\<experiment_id>`。当前 Windows 用户临时目录存在 pytest 权限冲突，同时也不符合大型临时文件优先写入 D 盘的约定。

2026-09-16 对维护版工具的逐项复核结果如下；“现有测试通过”不能替代缺失能力的实现和回归覆盖：

| 项目 | 当前实现 | 正式实验要求 |
| --- | --- | --- |
| `submitted_unverified` | runner、校准门禁和报告器未识别 | 解析、门禁、矩阵和报告全链路支持 |
| 新鲜确定性验证 | Agent 核心已实现，外部报告未核验 | `completed` 必须绑定最后一次 mutation 后的成功验证 |
| Web 三类验证 | 只运行并保存累计验证 | 分别保存当前任务、既有任务回归和累计结果 |
| Web 公开验证 | 合同仍未开放 `run_process`，没有 `verification_commands` | 有安全命令时精确开放；否则保持 code-only 并进入 `submitted_unverified` |
| 磁盘门禁 | orchestrator 仍为 2 GiB，runner 默认仍为 8 GiB | 统一采用本方案的 40/50 GiB 启动门槛和 30/25/20 GiB 运行阈值 |
| ReactBench 路径探针 | self-test 只估算最后一个任务的一条路径 | 全部 10 个任务、全部 group/attempt，真实创建、读取、删除探针文件 |
| 报告运行事件 | 仍以 `completed`/其他二分矩阵为主 | 增加预算提醒、两类压缩、工具错误、恢复和 mutation 回滚指标 |
| 正式实验 root | 不存在 `round2-*` root | 所有门禁通过后再创建，禁止提前写入正式结果 |

修复后的无模型检查包括：

```powershell
$Uv = "D:\Github\CodingAgent\.tools\uv\bin\uv.exe"
$TestTemp = "D:\Cache\CodingAgent\pytest-temp"
New-Item -ItemType Directory -Path $TestTemp -Force | Out-Null
$env:TEMP = $TestTemp
$env:TMP = $TestTemp
& $Uv --directory D:\Github\CodingAgent run ruff check .
& $Uv --directory D:\Github\CodingAgent run pytest
& $Uv --directory D:\Github\CodingAgent run pytest D:\Github\benchmark-runs\tools\tests -q
python D:\Github\benchmark-runs\tools\scale_reactbench_runner.py --self-test
```

当前基线为 CodingAgent 243 项测试通过，benchmark tools 19 项测试在 D 盘临时目录下通过，ReactBench `--self-test` 通过。现有 benchmark 测试尚未覆盖本节要求的新终态、三类 Web 验证、统一资源门禁和全量路径探针，因此新增回归测试后必须再次全部以 0 退出。随后再执行 orchestrator 初始化、两个 runner 的 dry-run、报告 schema 测试和一次断点恢复演练。

## 5. 基础设施门禁

上一轮临时资源已经清理，记录见 `D:\Github\benchmark-runs\scale-20260911-095313\notes\cleanup-20260914.md`。2026-09-16 复测时 D 盘约有 62.52 GiB 可用，C 盘约有 26.39 GiB 可用；Docker daemon 当前停止，尚未执行本轮固定镜像预拉取。磁盘满足预拉取前门禁，但宿主可用内存仅约 0.97 GiB，未达到 6 GiB 启动条件，因此当前资源门禁整体为失败。正式计分前必须释放内存、启动 Docker、预拉取固定镜像，并重新记录磁盘、宿主内存、Docker VM 内存、镜像 digest 和 `docker system df`。

| 资源 | 启动条件 | 运行中处理 |
| --- | --- | --- |
| D 盘 | Web 哨兵在依赖准备后至少 40 GiB；完整实验在固定镜像预拉取后至少 50 GiB，推荐 60 GiB | 低于 30 GiB 告警，低于 25 GiB 保存 checkpoint 并暂停，低于 20 GiB 硬停止 |
| C 盘 | 至少 10 GiB | 低于 5 GiB 硬停止 |
| 宿主内存 | Docker 启动前 Available 至少 6 GiB，推荐 8 GiB | 不满足时关闭浏览器、Steam 等非实验进程；RAGFlow、Elasticsearch、Milvus 容器不得运行 |
| Docker VM | `MemTotal` 至少 7.5 GiB，开始构建前 `MemAvailable` 至少 6 GiB | 低于 4 GiB 告警，低于 2 GiB 暂停 |
| 并发 | ReactBench 固定为 1 | 不动态提高并发 |

每个 batch 前后记录磁盘、内存、`docker system df` 和 VHDX 大小。单个任务使 D 盘减少至少 8 GiB，或 15 分钟内减少至少 5 GiB时暂停排查。

Docker 门禁必须验证 daemon、Compose 和容器探针；在容器内对实际使用的 npm、Yarn、GitHub 和 apt 源连续执行两轮 DNS 与 TLS 探针；预拉取固定基础镜像并记录 digest。由于本次清理已删除全部镜像，预拉取是强制步骤；预拉取完成后重新记录 Docker VHDX 和宿主磁盘，只有 D 盘仍不少于 50 GiB 才可正式计分。任务声明的网络策略必须在 Oracle 前检查，不支持的 `no-network` 组合直接记录为 `infra_unsupported`。

资源快照必须记录采集时间，不能沿用本文中的历史数值作为放行证据。Docker 启动前宿主内存未达到 6 GiB 时不得启动预拉取；应先结束不属于实验的浏览器、IDE、模型服务和 WSL 进程，再重新测量。Docker 启动后使用 Docker VM 总内存不少于 7.5 GiB、容器内 Available 不少于 6 GiB 作为运行门禁，同时继续记录宿主内存，但不要求两侧同时各保留 6 GiB。不得通过降低门槛绕过内存不足。

实验资源使用 `experiment_id` 标签或前缀。清理只处理本实验的 stopped containers、临时 verifier 镜像、临时环境镜像、BuildKit 缓存和 job 目录，保留报告、checkpoint、runtime archive 与固定基础镜像。每个 ReactBench group 后执行限定范围清理并复测磁盘，实验 BuildKit 缓存上限为 15 GiB。VHDX 压缩只允许在实验前或实验结束后、Docker 完全停止时执行。

## 6. 分阶段执行协议

### 阶段 -1：独立交付链路验证（已完成，不计分）

2026-09-16 的服务器小型实验已经完成，审批后工作流终态为 `completed`，修复后的测试环境页面返回 HTTP 200，Node 回归测试 2/2 通过，Playwright 无控制台错误和失败请求。该阶段不使用 Web-Bench/ReactBench 样本，不属于本轮正式实验，也不能用于开放下面任何校准门禁。

### 阶段 0：终态语义资格测试

使用 fake model 和临时 Git 仓库运行 12 个确定性用例，不产生正式分数：

- 3 个“修改代码但无公开验证命令”用例，全部应为 `submitted_unverified`；
- 3 个“修改后公开验证通过”用例，全部应为 `completed`；
- 3 个“公开验证失败或超时”用例，均不得为 `completed`；
- 3 个“验证通过后再次修改”用例，旧证据失效，均不得为 `completed`。

通过条件为 12/12 符合预期、无证据完成违规为 0、每个终态只有一条最终事件、完整事件和证据可回放。任何失败都先修 Agent 或 runner，不进入后续阶段。

### 阶段 1：端到端校准

使用全新 root 和 workspace，运行 3 个 Web demo 任务及 ReactBench `hello-react`，不计入正式 Pass@1。

通过条件：

- 4/4 外部官方验证器通过，4/4 outcome 非空；
- code-only 且发生 mutation 的任务为 `submitted_unverified`；只有存在新鲜公开确定性验证证据的任务才允许 `completed`；
- 4 个任务均有唯一 run ID、完整事件、diff 和验证证据；
- `hello-react` 满足 Oracle=1、NOP=0、Agent tests=1、react doctor=1；
- submission boundary 和 SUT 指纹检查通过。

校准失败时关闭门禁。不得复用上一轮校准结果，也不得通过手工修改结果文件开放门禁。

### 阶段 2：ReactBench 控制组筛选

对固定的 10 个 ReactBench 样本先只运行 Oracle 和 NOP，不调用 Agent。任务只有在 Oracle=1 且 NOP=0 时才可计分。瞬时基础设施失败允许一次重试；确定性能力不兼容不重试。

进入正式 Agent 组需要至少 8/10 个有效控制样本，并覆盖至少 1/2 个 medium 和 7/8 个 hard。未达到时，本轮标记 `calibration_failed`，先修环境或样本适配问题。

### 阶段 3：11 个正式哨兵任务

哨兵使用正式实验 root，结果计入最终 Pass@1，后续通过 checkpoint 跳过，不得重新计分：

- Web-Bench：canvas、react、less 各前三个正式任务，共 9 个；
- ReactBench：从阶段 2 的有效样本中固定选择 1 个 medium 和 1 个 hard，共 2 个。

这一阶段只判断协议是否可靠，不能因为成绩较低而重跑并选择更好结果。放行条件为：

- 11 条唯一结果，至少 10/11 可计分；
- 所有可计分任务均有 Agent 事件、diff 和外部验证证据；
- 无 SUT 或 runner 指纹漂移；
- 无“缺少新鲜确定性证据却返回 `completed`”的记录；
- 控制组有效，无连续 3 次模型供应商错误；
- 完成一次可控中断与恢复，已完成任务不重跑，结果不重复。

如果 0/11 通过隐藏验证，记录为能力问题并暂停诊断；不得修改同一实验 root 中的正式结果。

### 阶段 4：扩展到完整 70 个任务

哨兵通过后，沿用同一 root 和 checkpoint 继续执行剩余任务，最终形成与上一轮相同的 60 个 Web-Bench 连续任务和 10 个 ReactBench 任务。Web-Bench 保持 canvas、react、less 各 20 个及原有顺序；ReactBench 并发为 1。

每个 batch 完成后生成临时报表、资源快照和完整性检查。以下情况立即停止：

- 代码、任务、runner 或验证器指纹变化；
- checkpoint 或事件流无法可靠写入；
- 正式 task ID 重复或阶段缺少记录；
- D 盘低于 20 GiB；
- Docker daemon 失联；
- 连续 3 次模型供应商错误。

单任务两次瞬时基础设施失败后标记排除并继续；连续两个任务发生基础设施失败，或累计基础设施排除超过 20%，暂停整个 batch 排查。

### 阶段 5：可选修复轮

完整 Pass@1 结果冻结后，可以另开 repair run 测量“允许读取失败摘要并再次修复”的交付成功率。该结果必须单独报告，不得覆盖或混入 Pass@1。

## 7. 指标与统计口径

每个任务保存 Agent 终态与隐藏验证结果的交叉矩阵，至少包含：

- `completed + pass`、`completed + fail`；
- `submitted_unverified + pass`、`submitted_unverified + fail`；
- `failed/budget_exhausted + pass/fail`；
- 排除原因和基础设施重试次数。

主要指标为：

- **Pass@1**：第一次有效 Agent 尝试通过隐藏验证的任务数 / 可计分任务数；
- **完成精度**：隐藏验证通过的 `completed` 数 / 全部 `completed` 数；
- **错误完成率**：隐藏验证失败的 `completed` 数 / 可计分任务数；
- **提交精度**：隐藏验证通过的 `completed` 与 `submitted_unverified` 数 / 全部有效提交数；
- **完成召回率**：隐藏验证通过且为 `completed` 的任务数 / 隐藏验证通过任务数；
- **未验证路由正确率**：缺少新鲜公开验证证据的 mutation 中，正确进入 `submitted_unverified` 的比例；
- **无证据完成违规数**：缺少新鲜确定性验证证据却进入 `completed` 的数量；
- **新鲜证据覆盖率**：`completed` 中验证发生在最后一次 mutation 之后的比例。

Web-Bench 还需分别报告当前任务通过率、既有任务无回归率、累计通过率、首次回归位置和最长绿色前缀。运行层报告预算耗尽率、最后三轮提醒覆盖率、工具错误率、归档压缩次数、语义压缩次数、恢复次数、耗时和 token 分位数。

比例同时给出分子、分母和 95% Wilson 区间。与上一轮相同任务做配对比较；任务集合与验证器完全一致时可补充 McNemar 检验。协议变更导致的终态分布只能解释为安全性变化，不能直接当作编码能力提升。

## 8. 判定标准

实验执行成功和 Agent 效果分开判定。

实验完整性硬门禁：

- 70/70 任务有唯一记录，无 duplicate 和 unexpected task；
- 所有可计分任务均有 Agent 事件、diff、外部验证结果和指纹；
- 无证据完成违规数为 0，新鲜证据覆盖率为 100%；
- ReactBench 有效控制至少 8/10；
- JSON、CSV、Markdown 与离线 HTML 重算一致；
- 离线 HTML 无外部引用，secret scan 通过；
- checkpoint 恢复不重复执行已完成任务。

Agent 改进目标：

- 完成精度至少 80%，且至少有 15 个 `completed` 样本；
- 错误完成率不高于 20%；
- Pass@1 不低于上一轮 17.5%，目标至少 25%；
- Web-Bench 既有任务无回归率至少 80%；
- 预算耗尽率不高于上一轮的 8/63；
- repair run 中需要回滚的任务，回滚成功率为 100%。

硬门禁失败表示实验协议或实现仍不可信，不能进入下一阶段。实验完整但效果目标未达标时，保留真实报告并进入下一轮 Agent 优化，不删除或重跑失败样本。

## 9. 产物目录

每轮使用 `D:\Github\benchmark-runs\round2-YYYYMMDD-HHMMSS`，不得复用旧 root。建议目录如下：

```text
round2-YYYYMMDD-HHMMSS/
├── manifest.json
├── tasks.csv
├── events.jsonl
├── driver/
├── preflight/
├── calibration/
├── checkpoints/
├── resource-snapshots/
├── web-bench/
├── reactbench/
├── audit/
│   ├── integrity.json
│   └── secret-scan.txt
└── report/
    ├── index.html
    ├── summary.json
    ├── summary.md
    ├── task-results.csv
    ├── failures.md
    └── protocol.md
```

`events.jsonl` 只追加写入；每个 task result 使用临时文件加原子替换。报告必须能够只依赖冻结后的原始记录重新生成。

## 10. 执行命令骨架

以下命令只能在第 4 节修复完成、第 5 节资源门禁通过后执行：

```powershell
$ExperimentRoot = "D:\Github\benchmark-runs\round2-<timestamp>"
$Uv = "D:\Github\CodingAgent\.tools\uv\bin\uv.exe"
$MinFreeGiB = 50
$ExperimentTemp = "D:\Cache\CodingAgent\round2-<timestamp>"
New-Item -ItemType Directory -Path $ExperimentTemp -Force | Out-Null
$env:TEMP = $ExperimentTemp
$env:TMP = $ExperimentTemp

$Os = Get-CimInstance Win32_OperatingSystem
$AvailableMemoryGiB = ($Os.FreePhysicalMemory * 1KB) / 1GB
if ($AvailableMemoryGiB -lt 6) {
  throw "Available host memory is below 6 GiB: $([math]::Round($AvailableMemoryGiB, 2)) GiB"
}

& $Uv --directory D:\Github\CodingAgent run ruff check .
& $Uv --directory D:\Github\CodingAgent run pytest
& $Uv --directory D:\Github\CodingAgent run pytest D:\Github\benchmark-runs\tools\tests -q
python D:\Github\benchmark-runs\tools\scale_reactbench_runner.py --self-test
# 修复完成后，这里的自检还必须覆盖 12 项终态资格用例、Web 三类验证、
# 全量 ReactBench 路径读写探针、报告 schema 和断点恢复。
python D:\Github\benchmark-runs\tools\scale_orchestrator.py init --root $ExperimentRoot
# 启动 Docker，按冻结 manifest 预拉取镜像并记录 digest；完成后再次检查 D 盘不少于 $MinFreeGiB GiB。
python D:\Github\benchmark-runs\tools\scale_calibration.py --out-dir $ExperimentRoot --dry-run --min-free-gib $MinFreeGiB
python D:\Github\benchmark-runs\tools\scale_calibration.py --out-dir $ExperimentRoot --min-free-gib $MinFreeGiB
python D:\Github\benchmark-runs\tools\scale_orchestrator.py status --root $ExperimentRoot
```

哨兵任务应由修复后的冻结 driver 调用。ReactBench 的两个 task ID 在控制组筛选通过后写入 manifest；不要提前用无效控制样本占位。

哨兵门禁通过后运行：

```powershell
python D:\Github\benchmark-runs\tools\scale_orchestrator.py run --root $ExperimentRoot --from-batch A --through-batch C
python D:\Github\benchmark-runs\tools\scale_orchestrator.py status --root $ExperimentRoot
python "$ExperimentRoot\driver\scale_report.py" --root $ExperimentRoot
```

发生可恢复中断时继续使用同一个 root 和同一条 `run` 命令。不得删除 checkpoint、覆盖 result 或新建 root 来替代失败结果。

## 11. 推进顺序

严格按下面的顺序推进，前一项未通过时不得提前创建正式实验 root 或运行后续计分任务：

```text
更新 v1.2 方案（已完成）
→ 修 runner、校准门禁、报告器和资源门禁
→ 增加相应回归测试
→ 解决宿主内存不足
→ 启动 Docker 并预拉取固定镜像
→ 重新检查磁盘与 Docker 内存
→ 运行 12 项终态资格测试
→ 运行 4 项校准
→ 运行 11 项正式哨兵
→ 通过后扩展到完整 70 项
```

下一项工作是修改 benchmark runner、校准门禁、报告器和资源门禁，并为 `submitted_unverified`、新鲜验证证据、三类 Web 验证结果、统一磁盘阈值和全量路径探针增加回归测试。在这些修改及无模型检查全部通过前，Docker 保持停止，不创建 `round2-*` 正式 root。
