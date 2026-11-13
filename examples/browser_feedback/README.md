# Counter 浏览器反馈示例

这是 Python AgentLoop 的公开测试反馈示例：页面初始为 0，点击减一出现负数。宿主运行固定 Playwright 用例，将失败摘要反馈给模型；模型修改 `counter.js` 并再次验证，通过后提交完成结果。这里没有服务器上传或部署。

`workspace/` 是允许模型修改的前端页面；`host/` 是宿主固定测试和配置；`task.json` 是任务合同；`suites.json` 将合同中的 `counter-public` 映射到固定执行命令。宿主测试、套件配置和运行证据必须放在模型工作区之外。示例套件使用 8087 端口，已有进程占用时会拒绝执行；换端口需要同时修改预览命令和 URL。

先准备 Node 和 Playwright Test 1.57.0。已有相同版本的测试运行时和 Chromium 可直接复用，无须再次安装。若需要新装，在 `host` 目录执行 npm 安装，设置 npm 缓存为 `D:\Cache\npm`，并设置 `PLAYWRIGHT_BROWSERS_PATH=D:\Cache\playwright` 后安装 Chromium；依赖安装在 D 盘的 `host/node_modules`，浏览器和缓存也在 D 盘。普通 npm 用户配置仍可能写入 C 盘的小型配置文件。

在 PowerShell 中创建独立源码目录后运行：

```powershell
$sample = 'D:\Github\CodingAgent\examples\browser_feedback'
$workspace = 'D:\Cache\CodingAgent\browser-feedback\workspace'
New-Item -ItemType Directory -Force -Path $workspace | Out-Null
Copy-Item "$sample\workspace\*" -Destination $workspace
$env:UV_CACHE_DIR = 'D:\Cache\uv'
$env:PLAYWRIGHT_BROWSERS_PATH = 'D:\Cache\playwright'
& 'D:\Github\CodingAgent\.tools\uv\bin\uv.exe' --directory D:\Github\CodingAgent run aicoding-agent run --task "$sample\task.json" --workspace $workspace --browser-suites "$sample\suites.json" --env-file D:\Github\CodingAgent\env.txt
```

按实际路径配置宿主 `suites.json` 的 `command` 和 `preview_command`，尤其是 Node、Python 的可执行文件位置；命令的每个参数为独立 JSON 字符串。测试文件读取 `BROWSER_TEST_BASE_URL`，JSON 报告路径由宿主提供的 `PLAYWRIGHT_JSON_OUTPUT_NAME` 指定，截图和 Trace 写到 `PLAYWRIGHT_OUTPUT_DIR`。不要把模型 API Key 写进套件配置或 Node 环境。

预期第一轮有一个断言失败，另一用例通过；修改为 `Math.max(0, value - 1)` 后两项均通过。模型必须调用 `run_playwright` 并收到成功证据后才能 `submit_result`。如验证之后又修改源码，需重新运行测试。真实报告、日志和失败 Trace 由运行时存入工作区外的 evidence 目录。

测试源码和摘要是公开反馈数据。后续实验的隐藏官方评分仍独立运行，不能把这两项公开测试通过率当作完整 benchmark Pass@1。该示例不注册 PI 工具，PI 接入需要单独的后端适配器。

统一回归模式使用 `task-regression.json` 和 `regression.json`，同时验证固定 Node 命令断言和上述 Playwright 场景。在同样的独立工作区和已配置好的浏览器运行时上执行：

```powershell
& 'D:\Github\CodingAgent\.tools\uv\bin\uv.exe' --directory D:\Github\CodingAgent run aicoding-agent run --task "$sample\task-regression.json" --workspace $workspace --browser-suites "$sample\suites.json" --regression-config "$sample\regression.json" --runtime-dir D:\Cache\CodingAgent\counter-regression --env-file D:\Github\CodingAgent\env.txt
```

宿主将在独立 worktree 中编码，运行全部测试，通过后采用修改并推进通过基线；失败保留候选和证据。连续任务保持同一运行目录，历史测试自动加入下次验收。浏览器总运行次数同时包含模型工具调用和最终宿主验收。实现与恢复规则见 [统一回归验收](../../docs/regression-gate.md)。
