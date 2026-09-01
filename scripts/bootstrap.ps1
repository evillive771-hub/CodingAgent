$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$UvTarget = Join-Path $ProjectRoot ".tools\uv"
$UvExecutable = Join-Path $UvTarget "bin\uv.exe"
$UvCache = Join-Path $ProjectRoot ".uv-cache"

if (-not (Test-Path -LiteralPath $UvExecutable -PathType Leaf)) {
    Write-Host "Installing uv into $UvTarget"
    python -m pip install --disable-pip-version-check --target $UvTarget uv
}

$env:UV_CACHE_DIR = $UvCache
Write-Host "Using uv cache: $UvCache"
& $UvExecutable sync --extra dev
& $UvExecutable run ruff format --check .
& $UvExecutable run ruff check .
& $UvExecutable run pytest
