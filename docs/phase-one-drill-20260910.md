# 阶段一演练记录（2026-09-10，WSL 实验室）

本记录归档一次完整的阶段一测试交付演练：真实 MiMo 修复带 Bug 的前端 fixture，工作流完成受控构建、
SFTP 部署、Playwright 验证和人工审批，并覆盖批准与拒绝两条路径。运行环境是
[examples/phase1/README.md](../examples/phase1/README.md) 描述的 Windows 10 + WSL2 降级实验室，
**不构成真实 NFS 验收证据**。

## 环境与输入

| 项目 | 值 |
| --- | --- |
| 主机 | Windows 10 Pro 19045 + WSL2 `Ubuntu-24.04` |
| 设备地址 | `172.29.134.222`，SSH `2222`，Nginx `8080` |
| 设备固定入口 | `/usr/local/bin/frontend-{current,activate,health-check,restore}-release`，root:root 0755 |
| 运行时根目录 | `D:/aicoding-agent-runtime/wsl-phase1` |
| 模型 | 真实 MiMo（`MODEL_PROVIDER_TEXT=mimo`，`mimo-v2.5-pro`），未使用 Fake 适配器 |
| 任务合同 | `D:/aicoding-agent-lab/contracts/counter-phase-one.json`（`manual:counter-phase-one-20260909`） |
| 源码仓库 | `D:/aicoding-agent-lab/counter-e2e-20260909`，`origin` 为同目录 bare 仓库 |
| 冻结基线 | `main = eb24290`「Add intentionally buggy counter fixture」 |
| 验收命令 | `node --test counter.test.js`；构建 `node build.js`；制品目录 `dist` |

合同要求修复 `counter.js` 的 `decrement` 在 0 或负数时不得返回负数，同时保持 `increment` 不变。

## 运行结果

三次运行均由真实模型完成修改（4–5 轮、6 次工具调用），工作流阶段顺序为
`preparing_workspace → coding → inspecting_changes → building → test_deploying → test_verifying → awaiting_approval`。

| 运行 | run_id | 审批 | 结束阶段 | previous → candidate | 部署 ID |
| --- | --- | --- | --- | --- | --- |
| A（首次通过） | `69e8b6e4bde44c77a1809e005f4ccf0f` | approved / mawenbo | `completed` | `releases/5be26ff5…` → 同内容（幂等重部署） | `44a60870529b6a04576739ae` |
| B（拒绝） | `7176d3082d5f4ff2b58f24cd6d4cc1a6` | rejected / mawenbo | `rolled_back` | `releases/previous-buggy-eb24290` → `releases/5be26ff5…` | `b30969b94257bebe0cf6e8e9` |
| C（批准） | `bc808a07bab9425ab07aaac5d34aa120` | approved / mawenbo | `completed` | `releases/previous-buggy-eb24290` → `releases/5be26ff5…` | `857ced66bcaac7bb56e39b4b` |

- 运行 B 拒绝后 `rollback_status=succeeded`，回滚回执 `rollback-113f302eacdbc71e7c32a094.json`
  （`ok: true`、`isolated: false`），失败原因记录为审批人拒绝。
- 运行 A、C 通过后 `workspace_released=true`；运行 B 保留现场，租约 `cfb54571fb04621bd9190e04.json`
  仍在 `worktrees/.leases/`。
- 三次运行的制品哈希相同（`5be26ff5…`，内容寻址），说明同一产物重复部署是幂等的。
- 实体退出码符合约定：`completed` 为 0，`rolled_back` 为非 0。

## 产物与证据

| 证据 | 路径（相对 `D:/aicoding-agent-runtime/wsl-phase1/`） |
| --- | --- |
| 制品清单（C） | `evidence/build/b8514756e3e5e0a9ce771392/artifact-manifest.json` |
| 构建命令日志（C） | `evidence/build/b8514756e3e5e0a9ce771392/command-01-counter-build.log` |
| 部署回执与远端激活（C） | `evidence/deployment/857ced66bcaac7bb56e39b4b/{deployment.json,deployment-receipt.json,deployment-state.json}` |
| 回滚回执（B） | `evidence/deployment/b30969b94257bebe0cf6e8e9/rollback-113f302eacdbc71e7c32a094.json` |
| Playwright 报告（C） | `evidence/browser/857ced66bcaac7bb56e39b4b/verification-report.json`（`ok: true`，HTTP 200，无控制台错误、无失败请求） |
| 截图 / Trace / 视频（C） | `evidence/browser/857ced66bcaac7bb56e39b4b/{page.png,trace.zip,video/page@22c9d625156582fe39c6f35c7cdd8f62.webm}` |
| 审批包与 Checkpoint | `checkpoints/<run_id>/checkpoint.json` |
| 追加式事件（含 `stage_changed`、`approval_package_built`） | `workflow-events/<run_id>/events.jsonl` |

哈希：

- `dist/counter.js` = `4e6477cd2a7af8ab35179acf3f7fe6fea9e7767a307741d01a11e315bf683b9d`（570 B）
- `dist/index.html` = `636cf2b06dc19c9a43b6c5cb0e896770dc3191c9a8f601c63613de0db5708919`（380 B）
- 制品集合哈希 `artifact_hash` = `5be26ff5e6dde47d80b4261ddf70ed65849915254a618986e9dd98cc659168d4`
- 变更哈希 `change_hash` = `ff0b3417481b47f3d6ef1b815e27b863e38beaee2d013c18af0ef4c74d1bc8b1`
- 回滚前的上一版本 `dist/counter.js` = `299daddcca2a114ffb542a35d65cd470a6739afa632ef59689243e0f6e3de564`

## 宿主侧独立复核

工作流自身的回执之外，演练后在宿主机直接查设备与线上服务：

1. 运行 B 之后：`frontend-current-release` 输出 `releases/previous-buggy-eb24290`，
   `current/dist/counter.js` 哈希为 `299daddc…`（无 clamp），`frontend-health-check` 输出 `healthy`。
2. 运行 C 之后：`frontend-current-release` 输出 `releases/5be26ff5…`，
   `current/dist/counter.js` 哈希为 `4e6477cd…`，`frontend-health-check` 输出 `healthy`。
3. 直接从 Nginx 取线上脚本复核：`http://172.29.134.222:8080/counter.js` 的 SHA-256 等于
   `4e6477cd…`；把该脚本在 Node 中执行合同断言，得到
   `decrement(2)=1`、`decrement(0)=0`、`decrement(-2)=0`、`increment(2)=3`，四条验收标准全部满足。

## 本次演练暴露的问题

1. **Windows 非 UTF-8 输出会让审批环节崩溃。** 输出被重定向到文件时，`typer.echo` 打印审批包遇到
   模型 summary 里的 `✓` 抛 `UnicodeEncodeError: 'gbk' codec can't encode character`。进程以非零码
   退出，Checkpoint 完好，需要 `--resume` 继续。演练脚本通过 `PYTHONUTF8=1` /
   `PYTHONIOENCODING=utf-8` 规避。
2. **stdin 的中文审批意见按 cp936 解码。** 审批 comment 传入 UTF-8 中文时产生代理字符，
   `ApprovalRecord` 校验失败（`unable to parse raw data as a unicode string`），该次决策没有记录。
   改用 ASCII 意见后正常。
3. **`contract_hash` 每次运行都不同。** 冻结契约包含载入时刻的 `created_at`，因此同一份合同文件在
   不同 run 中哈希不同；租约和审批包绑定的是当次快照，同一 run 内一致。

## 与真实验收的差距

本实验室用 `/srv/frontend-agent` 本地目录代替 NFS 挂载，NFS 服务端与客户端未分离，因此
`docs/phase-one-acceptance.md` 中要求的真实 NFS/SSH/Nginx 最终演练仍未完成；设备隔离与人工恢复
路径本次也未触发（回滚成功，未进入隔离）。本次也手工在设备上放置了 `previous-buggy-eb24290`
（用 `eb24290` 的 buggy 构建）作为"上一版本"，否则回滚目标只有占位用的 `baseline` 目录。
