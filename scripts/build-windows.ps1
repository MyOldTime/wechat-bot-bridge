[CmdletBinding()]
param([string]$DistRoot)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'Windows EXE 必须在 Windows 上构建。'
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw '未找到 uv，请先安装 uv。'
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $DistRoot) { $DistRoot = Join-Path $projectRoot 'dist' }
$distRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DistRoot)
$outputDir = Join-Path $distRoot 'wechat-bot-bridge-windows-x64'
$zipPath = Join-Path $distRoot 'wechat-bot-bridge-windows-x64.zip'
$workRoot = Join-Path $projectRoot 'build\pyinstaller'
$buildEnvironment = Join-Path $projectRoot 'build\uv-pyinstaller'
$specPath = Join-Path $projectRoot 'packaging\wechat-bot-bridge.spec'
$previousUvEnvironment = [Environment]::GetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', 'Process')

New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$allowedFiles = @(
    'wechat-bot-bridge.exe',
    'README-WINDOWS.txt',
    'LICENSE',
    'SHA256SUMS.txt'
)
# 旧版公开模板可留在原目录，ZIP 只收集本版发布文件。
$unexpectedFiles = @(Get-ChildItem -LiteralPath $outputDir -Force | Where-Object { $_.Name -notin $allowedFiles -and $_.Name -ne 'windows.env.example' })
if ($unexpectedFiles.Count -gt 0) {
    $names = ($unexpectedFiles | ForEach-Object Name) -join ', '
    throw "发布目录包含其他文件 ($names)，请使用 -DistRoot 指定单独的构建输出目录。"
}

try {
    # 使用独立构建环境，避免影响正在由项目 .venv 运行的 Bridge 服务。
    [Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', $buildEnvironment, 'Process')
    Push-Location $projectRoot
    try {
        & uv sync --frozen --all-groups
        if ($LASTEXITCODE -ne 0) { throw 'uv 构建环境同步失败。' }

        $pythonVersion = (& uv run --no-sync python -c "import platform; print(platform.python_version())").Trim()
        if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.12.13') {
            throw "打包必须精确使用 Python 3.12.13，当前为 $pythonVersion"
        }

        & uv run --no-sync ruff check .
        if ($LASTEXITCODE -ne 0) { throw 'ruff 检查失败。' }
        & uv run --no-sync ruff format --check .
        if ($LASTEXITCODE -ne 0) { throw 'ruff 格式检查失败。' }
        & uv run --no-sync mypy src
        if ($LASTEXITCODE -ne 0) { throw 'mypy 检查失败。' }
        & uv run --no-sync pytest
        if ($LASTEXITCODE -ne 0) { throw 'pytest 失败。' }

        & uv run --no-sync pyinstaller --noconfirm --clean --distpath $outputDir --workpath $workRoot $specPath
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败。' }
    } finally {
        Pop-Location
    }
} finally {
    [Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', $previousUvEnvironment, 'Process')
}

$packagedExe = Join-Path $outputDir 'wechat-bot-bridge.exe'
if (-not (Test-Path -LiteralPath $packagedExe -PathType Leaf)) {
    throw "未找到发布 EXE：$packagedExe"
}

Copy-Item -LiteralPath (Join-Path $projectRoot 'packaging\README-WINDOWS.txt') -Destination (Join-Path $outputDir 'README-WINDOWS.txt') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination (Join-Path $outputDir 'LICENSE') -Force

$hash = (Get-FileHash -LiteralPath $packagedExe -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $outputDir 'SHA256SUMS.txt') -Encoding ascii -NoNewline -Value "$hash  wechat-bot-bridge.exe`n"
$packageFiles = @($allowedFiles | ForEach-Object { Join-Path $outputDir $_ })
Compress-Archive -LiteralPath $packageFiles -DestinationPath $zipPath -CompressionLevel Optimal -Force

Write-Host "Windows 发布目录：$outputDir"
Write-Host "单文件 EXE：$packagedExe"
Write-Host "ZIP：$zipPath"
Write-Host "SHA-256：$hash"
