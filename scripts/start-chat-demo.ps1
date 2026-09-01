param(
    [ValidateSet("01-counter-clamp", "02-search-normalization", "03-accessible-dialog")]
    [string]$Case = "01-counter-clamp",
    [string]$Session = "demo"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$source = Join-Path $projectRoot "examples\chat_cases\$Case"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$targetRoot = Join-Path $projectRoot "runtime\chat-demos"
$target = Join-Path $targetRoot "$Case-$timestamp"
$uv = Join-Path $projectRoot ".tools\uv\bin\uv.exe"
$envFile = Join-Path $projectRoot "env.txt"
$cacheDir = Join-Path $projectRoot ".uv-cache"

if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "Demo case does not exist: $source"
}
if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) {
    throw "Project-local uv is missing. Run scripts\bootstrap.ps1 first."
}

New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $target -Recurse
$env:UV_CACHE_DIR = $cacheDir

Write-Host "Demo workspace: $target" -ForegroundColor Cyan
Write-Host "Open TASK.md or start with the recommended prompt from examples\chat_cases\README.md."
& $uv run aicoding-agent chat `
    --workspace $target `
    --env-file $envFile `
    --session $Session
exit $LASTEXITCODE
