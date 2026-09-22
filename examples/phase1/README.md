# 阶段一测试交付配置

`task.example.json` 冻结 Agent 权限、验证命令、构建命令和制品路径；
`delivery.example.json` 冻结 SFTP/NFS/SSH 与 Playwright 配置。请复制后填写本地路径和测试环境地址，
不要把密码、私钥内容或 API key 写进 JSON。密码认证应使用 `password_env` 指向宿主环境变量。
任务中的 `repository_url` 必须与本地 checkout 的 `origin` 一致（末尾 `.git` 可不同），避免把 Bug
错误地应用到另一个仓库；`base_revision` 会解析并冻结为明确 commit。

设备上的四个命令是运维预先安装并审计的固定入口，Agent 无法修改：

- `current_release`：只输出当前版本的绝对 NFS release 路径；
- `activate`：挂载或切换候选版本并重启 Web 服务；
- `health_check`：失败时返回非零退出码；
- `rollback`：恢复传入的上一版本并重启服务。

本目录的 `device-scripts/` 提供这四个 Linux 入口的参考实现。设备需要预先把 NFS 共享挂载到
`/srv/frontend-agent`，并至少准备一个 `releases/<baseline>` 目录和指向它的 `current` 符号链接。
脚本固定只允许切换 `releases/` 的直接或间接子目录，且固定重载 Nginx，不执行 Agent 生成的命令。
`public_url_template` 表示设备实际提供页面的固定 URL；只有反向代理确实按 deployment ID 分路由时，
才应删除它并使用 `public_base_url/<deployment_id>`。

安装时由设备管理员审查脚本后复制并设置只读可执行权限（路径需与配置中的 `program` 一致）：

```bash
sudo install -o root -g root -m 0755 frontend-current-release /usr/local/bin/
sudo install -o root -g root -m 0755 frontend-activate-release /usr/local/bin/
sudo install -o root -g root -m 0755 frontend-health-check /usr/local/bin/
sudo install -o root -g root -m 0755 frontend-restore-release /usr/local/bin/
```

`activate` 和 `rollback` 以 `delivery` 身份修改由它拥有的 NFS 发布目录，只在最后通过无交互 sudo
重载 Nginx。管理员应使用 `visudo -f /etc/sudoers.d/frontend-delivery` 只授予这一条权限：

```text
delivery ALL=(root) NOPASSWD: /usr/bin/systemctl reload nginx
```

不要对两个发布脚本整体提权，也不要配置 `NOPASSWD: ALL`。这样 NFS 可以保留默认 `root_squash`；
回环导出仍应只允许 `127.0.0.1`，不得向宿主机或局域网开放 NFS 端口。

## 当前 Windows 10 + WSL2 测试环境

本机实验环境使用 `Ubuntu-24.04`、SSH 端口 `2222`、Nginx 端口 `8080`。Windows 10 上 WSL
地址会在重启后变化，而且没有前台客户端时发行版可能自动停止。每次测试前从项目根目录运行：

```powershell
& .\examples\phase1\wsl\start-lab.ps1
```

该脚本会在后台保持发行版运行，通过本地 WSL 通道固定 SSH 主机公钥，并只更新被 Git 忽略的
`delivery.json` 与项目专用 `known_hosts`。可用 `wsl.exe -t Ubuntu-24.04` 停止实验环境。

这台机器实测无法稳定完成“同一个 WSL 发行版同时充当 NFS 服务端与客户端”的回环挂载，写入会
阻塞。因此当前 WSL 实验室以 `/srv/frontend-agent` 本地目录验证 SFTP、SSH、受限激活脚本、Nginx、
Playwright 与回滚；最终 NFS 验收需要把相同配置迁移到 Linux 虚拟机/服务器，并将 NFS 服务端与
挂载客户端分开。此降级不能作为真实 NFS 验收通过的证据。

示例合同把 `dist` 声明为制品目录，制品清单和 SFTP 会保留这个相对路径，因此 Nginx 的站点根目录
固定为 `/srv/frontend-agent/current/dist`；每个 release（包括 baseline）都必须包含 `dist/index.html`。

安装交付依赖和 Chromium 后运行：

```powershell
$env:UV_CACHE_DIR = "D:\Github\CodingAgent\.uv-cache"
$env:PLAYWRIGHT_BROWSERS_PATH = "D:\Github\CodingAgent\.tools\playwright"
& ".\.tools\uv\bin\uv.exe" sync --extra dev --extra delivery
& ".\.tools\uv\bin\uv.exe" run playwright install chromium
& ".\.tools\uv\bin\uv.exe" run aicoding-agent deliver-phase-one `
  --task .\examples\phase1\task.json `
  --repository D:\path\to\frontend-repo `
  --delivery-config .\examples\phase1\delivery.json `
  --env-file .\env.txt
```

流程停在审批包并要求输入 `approve` 或 `reject`。阶段一模式不会 Git push、创建 MR/PR、修改
Teambition 或执行生产发布；拒绝、验证失败和审批后拒绝都会恢复测试设备上一版本。
运行开始时会显示 `workflow_run_id`。宿主中断后可在原命令后增加
`--run-id <该ID> --resume`，从最近的稳定 Checkpoint 继续，已完成的幂等阶段不会重复执行。

回滚失败后，设备会进入持久隔离状态，之后的发布会失败关闭：

```powershell
& ".\.tools\uv\bin\uv.exe" run aicoding-agent delivery-device-status `
  --delivery-config .\examples\phase1\delivery.json

& ".\.tools\uv\bin\uv.exe" run aicoding-agent delivery-device-recover `
  --delivery-config .\examples\phase1\delivery.json `
  --approver "frontend-owner" `
  --comment "已人工恢复上一版本并检查 NFS/Nginx"
```

恢复命令会通过 SSH 重新读取当前 release 并执行固定健康检查；检查失败时不会解除隔离。工作流只有
`completed` 返回成功退出码，验证失败、人工拒绝、已回滚或隔离均返回非零退出码。

## 无 sudo 的 WSL 服务器实验

服务器账号没有 sudo 权限时，可以使用 `user-mode-device-scripts/` 中的固定入口。它们把 release 保存在
`~/frontend-agent`，使用 `python3 -m http.server` 监听远端 `127.0.0.1:8080`，再由开发电脑建立
SSH 隧道供 Playwright 访问。完整安装、配置和复核步骤见
[`docs/server-smoke-experiment-plan.md`](../../docs/server-smoke-experiment-plan.md)。该模式用于功能实验，
不覆盖 NFS、Nginx、系统服务管理和设备级隔离验收。
