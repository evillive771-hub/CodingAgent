param(
    [string]$Distro = "Ubuntu-24.04"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$configPath = Join-Path $repoRoot "examples\phase1\delivery.json"

if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Missing $configPath. Copy delivery.example.json to delivery.json first."
}

# A foreground WSL client keeps the lightweight VM alive on Windows 10.  The
# process is deliberately hidden; stop only this distro with `wsl -t $Distro`.
$keeper = Start-Process -FilePath "wsl.exe" -WindowStyle Hidden -PassThru -ArgumentList @(
    "-d", $Distro, "-u", "delivery", "--exec", "/usr/bin/sleep", "infinity"
)

$ip = $null
foreach ($attempt in 1..20) {
    $addresses = (& wsl.exe -d $Distro -u root -- hostname -I).Trim()
    $candidates = @($addresses -split "\s+" | Where-Object { $_ -match "^\d+\.\d+\.\d+\.\d+$" })
    $ip = $candidates | Select-Object -First 1
    if ($ip) { break }
    Start-Sleep -Milliseconds 500
}
if (-not $ip) { throw "Could not determine the IPv4 address of $Distro." }

$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$knownHostsPath = $config.deployment.storage.known_hosts -replace "/", "\"
$knownHostsDirectory = Split-Path -Parent $knownHostsPath
New-Item -ItemType Directory -Path $knownHostsDirectory -Force | Out-Null

# Pin the host key through the trusted local WSL channel instead of accepting
# an unauthenticated ssh-keyscan result.
$publicKey = (& wsl.exe -d $Distro -u root -- cat /etc/ssh/ssh_host_ed25519_key.pub).Trim() -split "\s+"
if ($publicKey.Count -lt 2 -or $publicKey[0] -ne "ssh-ed25519") {
    throw "Unexpected WSL SSH host key format."
}
$knownHost = "[$ip]:2222 $($publicKey[0]) $($publicKey[1])`n"
[IO.File]::WriteAllText($knownHostsPath, $knownHost, [Text.UTF8Encoding]::new($false))

$config.deployment.storage.host = $ip
$config.deployment.device.host = $ip
$config.deployment.public_base_url = "http://${ip}:8080/builds"
$config.deployment.public_url_template = "http://${ip}:8080/"
$config.browser.allowed_hosts = @($ip)
$json = $config | ConvertTo-Json -Depth 20
[IO.File]::WriteAllText($configPath, "$json`n", [Text.UTF8Encoding]::new($false))

$fingerprint = & ssh-keygen.exe -lf $knownHostsPath
Write-Output "WSL phase-one lab is running."
Write-Output "Distro: $Distro"
Write-Output "Keeper PID: $($keeper.Id)"
Write-Output "Address: $ip"
Write-Output "Nginx: http://${ip}:8080/"
Write-Output "SSH: delivery@${ip}:2222"
Write-Output "Host key: $fingerprint"
Write-Output "Config updated: $configPath"
Write-Output "Stop the lab with: wsl.exe -t $Distro"
