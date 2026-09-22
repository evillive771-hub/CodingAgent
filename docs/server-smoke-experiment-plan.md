# CodingAgent 内网服务器小型验证实验

版本：v1.2  
日期：2026-09-16  
状态：无 sudo 的 WSL 用户态真实闭环已完成

## 1. 目标与范围

使用一台公司内网 Linux 测试服务器，验证 CodingAgent 能否完成下面的真实闭环：

```text
冻结 Bug 基线 → Agent 修改代码 → 本地测试 → 构建 dist
→ SFTP 上传服务器 → SSH 激活版本 → 用户态 HTTP 服务提供页面
→ Playwright 验证 → 人工审批 → 完成或回滚
```

服务器同时作为 SFTP 存储端和测试设备，两个配置端点使用同一个 IP、端口和账号。当前 WSL 用户 `lenovo` 没有 sudo 权限，因此发布目录、固定命令和 HTTP 服务全部运行在其 HOME 下。页面只监听服务器的 `127.0.0.1:8080`，开发电脑通过 SSH 隧道 `127.0.0.1:18080` 访问，无需开放新的公司内网端口。该方式足以验证阶段一主要能力，不需要第二台服务器，也不验证真实 NFS、Nginx 或系统级设备隔离。

2026-09-16 实际运行已完成，工作流 run ID 为 `ee6723b573e74f46a112ee5411ed5831`，部署 ID 为 `3bdeea0f98203cf86fd88071`。Agent 仅修改 `counter.js`，Node 回归测试 2/2 通过，构建、SFTP、SSH 激活和健康检查成功，Playwright 返回 HTTP 200 且没有控制台错误或失败请求，人工审批后终态为 `completed`，未执行 Git push 或 Teambition 写入。

## 2. 实验用例

复用已经在本机准备好的可控仓库和合同：

- 仓库：`D:\aicoding-agent-lab\counter-e2e-20260909`
- Bug 基线：Git commit `eb24290`
- 任务合同：`D:\aicoding-agent-lab\contracts\counter-phase-one.json`
- 验证命令：`node --test counter.test.js`
- 构建命令：`node build.js`
- 制品：`dist/index.html`、`dist/counter.js`

Bug 内容为：`decrement(0)` 和 `decrement(-2)` 会返回负数。正确实现应满足：

- `increment(2) === 3`；
- `decrement(2) === 1`；
- `decrement(0) === 0`；
- `decrement(-2) === 0`。

这个用例很小，结果容易人工复核，同时会经过真实模型、Git worktree、确定性验证、构建、SFTP、SSH、用户态 HTTP 服务和 Playwright。

## 3. 需要确认的服务器参数

执行前把以下非秘密参数写入本机未提交的 `examples/phase1/delivery.server.json`：

| 参数 | 示例 | 用途 |
| --- | --- | --- |
| 服务器 IP | `10.160.40.249` | SSH 和 SFTP |
| SSH 端口 | `22` | AsyncSSH 连接 |
| Linux 用户名 | `lenovo` | 上传制品和调用用户态固定脚本 |
| WSL 页面端口 | `127.0.0.1:8080` | 只允许服务器本机访问 |
| 本机隧道端口 | `127.0.0.1:18080` | Playwright 和独立复核访问 |
| 远端根目录 | `/home/lenovo/frontend-agent` | 只存放本实验 release |
| Linux 发行版 | Ubuntu 22.04.5 WSL2 | 已确认 |
| sudo 权限 | 无 | 使用本方案的用户态脚本 |

密码不得写入 JSON、任务合同、`env.txt`、命令历史或 Git。当前服务器的系统 SSH 服务没有接受实验公钥，因此配置使用 `password_env`。执行工作流前，在运行 CodingAgent 的 Windows PowerShell 中通过凭据窗口把密码放入当前进程内存：

```powershell
$Credential = Get-Credential -UserName 'lenovo' -Message 'Ubuntu 24.04 测试服务器'
$env:AICODING_TEST_SERVER_PASSWORD = $Credential.GetNetworkCredential().Password
```

工作流结束后清除当前 PowerShell 进程中的变量：

```powershell
Remove-Item Env:AICODING_TEST_SERVER_PASSWORD -ErrorAction SilentlyContinue
```

## 4. 阶段 A：只读连通性检查

此阶段不安装软件、不上传文件、不调用模型。

1. 从运行 CodingAgent 的 Windows 主机确认服务器可达：

   ```powershell
   Test-NetConnection 10.160.40.249 -Port 22
   ```

2. 在服务器 WSL 中读取主机公钥指纹，不需要 sudo：

   ```bash
   for key in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$key" -E sha256; done
   ```

3. 开发电脑扫描到的三个指纹应与服务器输出逐项一致：

   ```text
   ECDSA   SHA256:6rLYmwcAPWcnC3LMOIGN1OPN38a+Ero4s+dGiIfc7lU
   RSA     SHA256:Q3Yz3AkVoXRPLj1lSIchavASzO3HGVK1DsLzNkY5UmY
   ED25519 SHA256:9P+OEQcJuvwn6TQECrdusWCb5pjl1KKpHmEQl3paalk
   ```

   一致后，从开发电脑的 WSL 获取主机公钥并保存到项目外的 D 盘。Windows 自带的 `ssh-keyscan` 在当前环境中无法与服务器的 KEX 算法协商，因此这里固定使用 `Ubuntu-24.04`：

   ```powershell
   New-Item -ItemType Directory -Path D:\secure\ssh -Force | Out-Null
   wsl.exe -d Ubuntu-24.04 -- ssh-keyscan -T 10 -p 22 10.160.40.249 2>$null |
     Out-File D:\secure\ssh\codingagent-test-known_hosts -Encoding ascii
   ssh-keygen -lf D:\secure\ssh\codingagent-test-known_hosts
   ```

4. 强制使用该 `known_hosts` 验证密码登录：

   ```powershell
   ssh -p 22 -o PubkeyAuthentication=no -o PreferredAuthentications=password `
     -o StrictHostKeyChecking=yes `
     -o UserKnownHostsFile=D:\secure\ssh\codingagent-test-known_hosts `
     lenovo@10.160.40.249 "uname -a; id; df -h /; command -v python3; command -v curl; ss -lnt"
   ```

放行条件：主机公钥已经独立核对，SSH 登录成功，`python3` 和 `curl` 可用，根分区至少有 2 GiB 可用，并且 `127.0.0.1:8080` 未被占用。

## 5. 阶段 B：无 sudo 的一次性服务器配置

先在服务器 WSL 中确认依赖。Ubuntu 通常已经包含这两个命令；任一缺失都需要管理员安装，当前用户无法自行补齐：

```bash
command -v python3
command -v curl
ss -lnt | grep ':8080 ' || printf '%s\n' '8080 available'
```

在开发电脑的 `D:\Github\CodingAgent` 中执行下面的命令，将项目提供的用户态固定脚本上传到 WSL：

```powershell
$SshOptions = @(
  '-o', 'PubkeyAuthentication=no',
  '-o', 'PreferredAuthentications=password',
  '-o', 'StrictHostKeyChecking=yes',
  '-o', 'UserKnownHostsFile=D:\secure\ssh\codingagent-test-known_hosts'
)

ssh @SshOptions lenovo@10.160.40.249 "mkdir -p ~/.local/bin ~/frontend-agent/releases/baseline/dist ~/frontend-agent/run"

scp @SshOptions .\examples\phase1\user-mode-device-scripts\* `
  lenovo@10.160.40.249:/home/lenovo/.local/bin/

ssh @SshOptions lenovo@10.160.40.249 "chmod 0755 ~/.local/bin/frontend-*"
```

这些脚本不调用 `sudo` 或 `systemctl`。激活和回滚只能把 `~/frontend-agent/current` 切换到 `~/frontend-agent/releases` 下的目录；HTTP 服务只监听 WSL 回环地址。

在开发电脑构建并上传真实 Bug 基线：

```powershell
Set-Location D:\aicoding-agent-lab\counter-e2e-20260909
git rev-parse HEAD
node --test counter.test.js
node build.js

scp @SshOptions -r .\dist\* `
  lenovo@10.160.40.249:/home/lenovo/frontend-agent/releases/baseline/dist/

ssh @SshOptions lenovo@10.160.40.249 `
  "ln -sfn /home/lenovo/frontend-agent/releases/baseline /home/lenovo/frontend-agent/current; ~/.local/bin/frontend-user-http-start"
```

`git rev-parse HEAD` 必须为 `eb24290`，测试必须表现为 increment 通过、decrement clamp 失败。随后在服务器验证：

```bash
~/.local/bin/frontend-current-release
~/.local/bin/frontend-health-check smoke
curl --fail http://127.0.0.1:8080/
```

在开发电脑单独打开一个 PowerShell 或 MobaXterm 会话，建立 SSH 隧道并保持该会话运行：

```powershell
$SshOptions = @(
  '-o', 'PubkeyAuthentication=no',
  '-o', 'PreferredAuthentications=password',
  '-o', 'StrictHostKeyChecking=yes',
  '-o', 'UserKnownHostsFile=D:\secure\ssh\codingagent-test-known_hosts'
)
ssh @SshOptions -N -L 18080:127.0.0.1:8080 lenovo@10.160.40.249
```

另开一个 PowerShell 检查：

```powershell
Test-NetConnection 127.0.0.1 -Port 18080
Invoke-WebRequest http://127.0.0.1:18080/ -UseBasicParsing
```

这样所有页面流量都经过已经放行的 SSH 22 端口，不需要修改 Windows 或公司网络防火墙。

## 6. 阶段 C：本机配置

本机已经生成被 Git 忽略的 `examples/phase1/delivery.server.json`。当前服务器对应的关键配置如下：

```json
{
  "schema_version": "1",
  "runtime_root": "D:/aicoding-agent-runtime/server-smoke",
  "deployment": {
    "storage": {
      "host": "10.160.40.249",
      "port": 22,
      "username": "lenovo",
      "known_hosts": "D:/secure/ssh/codingagent-test-known_hosts",
      "password_env": "AICODING_TEST_SERVER_PASSWORD"
    },
    "device": {
      "host": "10.160.40.249",
      "port": 22,
      "username": "lenovo",
      "known_hosts": "D:/secure/ssh/codingagent-test-known_hosts",
      "password_env": "AICODING_TEST_SERVER_PASSWORD"
    },
    "remote_root": "/home/lenovo/frontend-agent",
    "public_base_url": "http://127.0.0.1:18080",
    "public_url_template": "http://127.0.0.1:18080/",
    "commands": {
      "current_release": {
        "program": "/home/lenovo/.local/bin/frontend-current-release"
      },
      "activate": {
        "program": "/home/lenovo/.local/bin/frontend-activate-release",
        "args": ["{candidate}", "{deployment_id}"]
      },
      "health_check": {
        "program": "/home/lenovo/.local/bin/frontend-health-check",
        "args": ["{deployment_id}"]
      },
      "rollback": {
        "program": "/home/lenovo/.local/bin/frontend-restore-release",
        "args": ["{previous}"]
      }
    }
  },
  "browser": {
    "browser": "chromium",
    "headless": true,
    "path": "",
    "ready_selector": "#value",
    "expected_text": "0",
    "timeout_seconds": 30,
    "allowed_hosts": ["127.0.0.1"],
    "record_video": true,
    "fail_on_console_error": true,
    "fail_on_request_failure": true
  }
}
```

该配置依赖阶段 B 中持续运行的 SSH 隧道。每次开始或恢复工作流前，都要确认 `127.0.0.1:18080` 可访问。

在调用模型前先执行：

```powershell
$env:UV_CACHE_DIR = "D:\Cache\uv"
$env:PLAYWRIGHT_BROWSERS_PATH = "D:\Github\CodingAgent\.tools\playwright"
$env:TEMP = "D:\Cache\CodingAgent\server-smoke"
$env:TMP = "D:\Cache\CodingAgent\server-smoke"
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null

& "D:\Github\CodingAgent\.tools\uv\bin\uv.exe" run aicoding-agent validate-task `
  --task D:\aicoding-agent-lab\contracts\counter-phase-one.json

& "D:\Github\CodingAgent\.tools\uv\bin\uv.exe" run ruff check .
& "D:\Github\CodingAgent\.tools\uv\bin\uv.exe" run pytest
```

## 7. 阶段 D：主实验

先确认 Bug 基线真实存在：

```powershell
Set-Location D:\aicoding-agent-lab\counter-e2e-20260909
node --test counter.test.js
```

预期结果为 `increment` 用例通过、`decrement clamps at zero` 用例失败。如果基线全部通过，实验输入已经污染，应先恢复到 `eb24290`，不能继续计分。

随后从 `D:\Github\CodingAgent` 执行一次真实工作流：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

& "D:\Github\CodingAgent\.tools\uv\bin\uv.exe" run aicoding-agent deliver-phase-one `
  --task D:\aicoding-agent-lab\contracts\counter-phase-one.json `
  --repository D:\aicoding-agent-lab\counter-e2e-20260909 `
  --delivery-config D:\Github\CodingAgent\examples\phase1\delivery.server.json `
  --env-file D:\Github\CodingAgent\env.txt
```

工作流到达审批环节后，检查审批包、diff、测试结果、部署 URL 和 Playwright 截图，再输入 `approve`。不要在验证完成前批准。

如终端中断，使用输出的 `workflow_run_id` 恢复：

```powershell
& "D:\Github\CodingAgent\.tools\uv\bin\uv.exe" run aicoding-agent deliver-phase-one `
  --task D:\aicoding-agent-lab\contracts\counter-phase-one.json `
  --repository D:\aicoding-agent-lab\counter-e2e-20260909 `
  --delivery-config D:\Github\CodingAgent\examples\phase1\delivery.server.json `
  --env-file D:\Github\CodingAgent\env.txt `
  --run-id <WORKFLOW_RUN_ID> --resume
```

## 8. 独立复核与通过标准

工作流使用隔离 worktree，不会把候选修改写回源 checkout。因此工作流结束后不应使用原仓库文件冒充候选验证；应从测试页面下载已部署脚本并独立执行断言：

```powershell
$Page = Invoke-WebRequest http://127.0.0.1:18080/ -UseBasicParsing
$Script = Invoke-WebRequest http://127.0.0.1:18080/counter.js -UseBasicParsing
$DownloadedCounter = "D:\Cache\CodingAgent\server-smoke\deployed-counter.js"
[IO.File]::WriteAllText($DownloadedCounter, $Script.Content, [Text.UTF8Encoding]::new($false))
node -e "const m=require(process.argv[1]); if(m.increment(2)!==3 || m.decrement(2)!==1 || m.decrement(0)!==0 || m.decrement(-2)!==0) process.exit(1)" $DownloadedCounter
$Page.StatusCode
```

再通过 SSH 检查：

```bash
~/.local/bin/frontend-current-release
~/.local/bin/frontend-health-check verify
sha256sum ~/frontend-agent/current/dist/index.html
sha256sum ~/frontend-agent/current/dist/counter.js
```

一次主实验只有同时满足以下条件才算通过：

- Agent 终态为 `completed`，最后一次代码修改后存在成功的确定性测试证据；
- `node --test counter.test.js` 全部通过；
- Agent 只修改修复 Bug 所需文件，没有修改测试和隐藏验证；
- 构建成功，制品清单和哈希存在；
- SFTP 上传后的远端哈希与本地制品一致；
- SSH 激活和健康检查成功；
- 页面返回 HTTP 200，`#value` 存在，包含 `Counter phase-one demo`；
- Playwright 没有控制台错误和失败请求，并生成截图、Trace 和视频；
- 审批包绑定合同、diff、制品、部署和浏览器证据哈希；
- 最终工作流阶段为 `completed`，进程退出码为 0。

需要记录但不作为单次成败门槛的效果指标：模型轮数、工具调用数、输入/输出 token、Agent 耗时、全流程耗时、修改文件数、diff 行数和人工介入次数。

## 9. 可选的回滚实验

主实验通过后可以把服务器重新指向 Bug 基线，再运行一次相同任务，并在审批环节输入 `reject`。预期结果为：

- 候选版本在拒绝前已经通过测试、部署和浏览器验证；
- 工作流自动恢复 `previous` release；
- `rollback_status=succeeded`；
- 当前 release、远端文件哈希和页面内容与实验前基线一致；
- 工作流终态为 `rolled_back`，进程使用非零退出码。

这个非零退出码表示候选版本没有被批准，不表示回滚测试失败。若回滚失败，设备必须进入隔离状态，禁止继续部署。

## 10. 实验产物

至少保留：

- 工作流 `run_id` 和完整命令参数，但不含密码；
- Agent 事件、工具结果和代码 diff；
- 构建日志与 artifact manifest；
- SFTP 上传回执、部署状态和远端哈希；
- Playwright verification report、截图、Trace 和视频；
- 审批记录和最终 checkpoint；
- 独立执行的 HTTP、SSH 和文件哈希复核结果；
- 若执行可选实验，保存回滚回执和回滚后的独立复核。

实验报告按“编码正确性、确定性验证、真实部署、浏览器验证、安全回滚”五部分给出结论，不能只依据 Agent 自己提交的 summary 判定成功。

## 11. 无 sudo 方案的边界

该方案验证真实的 SFTP 上传、SSH 激活、版本切换、用户态 HTTP 服务、Playwright 和回滚。它不构成 NFS、Nginx、系统服务重启或设备级隔离的验收证据。`~/.local/bin` 中的脚本由 `lenovo` 用户所有，安全强度低于管理员安装的只读固定入口，因此只用于这次受控测试；正式共享测试设备仍应由管理员安装并保护部署脚本。
