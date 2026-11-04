[CmdletBinding()]
param(
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'Windows 发布烟测只能在 Windows 上运行。'
}

function Get-FreeLoopbackPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try {
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

function Stop-ProcessTree {
    param([Parameter(Mandatory)][int]$RootProcessId)

    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $RootProcessId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        Stop-ProcessTree -RootProcessId $child.ProcessId
    }
    if (Get-Process -Id $RootProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $RootProcessId -Force
    }
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot 'build-windows.ps1')
}

$outputDir = Join-Path $projectRoot 'dist\wechat-bot-bridge-windows-x64'
$executable = Join-Path $outputDir 'wechat-bot-bridge.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "未找到发布 EXE：$executable"
}

$versionOutput = (& $executable --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch '^0\.1\.0$') {
    throw "发布 EXE --version 异常：$versionOutput"
}

$testData = Join-Path ([IO.Path]::GetTempPath()) ("wechat-bot-bridge-release-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testData | Out-Null
$launchDirectory = Join-Path $testData 'release'
New-Item -ItemType Directory -Path $launchDirectory | Out-Null
$smokeExecutable = Join-Path $launchDirectory 'wechat-bot-bridge.exe'
Copy-Item -LiteralPath $executable -Destination $smokeExecutable
$stdout = Join-Path $testData 'stdout.log'
$stderr = Join-Path $testData 'stderr.log'
$databasePath = Join-Path $testData 'data\wechat-bridge.db'
$credentialsPath = Join-Path $testData 'data\wechat\credentials.json'
$port = Get-FreeLoopbackPort
$testPassword = 'release-test-password-123'
$environmentNames = @(
    'WBB_WEB_USERNAME',
    'WBB_WEB_PASSWORD',
    'WBB_HOST',
    'WBB_PORT',
    'WBB_DATA_DIR',
    'WBB_DATABASE_PATH',
    'WBB_CREDENTIALS_PATH',
    'WBB_LOG_LEVEL'
)
$previousEnvironment = @{}
$process = $null
$secondProcess = $null

try {
    foreach ($name in $environmentNames) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }

    $dataDirectoryForEnv = (Join-Path $testData 'data').Replace('\', '/')
    $databasePathForEnv = $databasePath.Replace('\', '/')
    $credentialsPathForEnv = $credentialsPath.Replace('\', '/')
    $environmentFile = @"
WBB_WEB_USERNAME=admin
WBB_WEB_PASSWORD=$testPassword
WBB_HOST=127.0.0.1
WBB_PORT=$port
WBB_DATA_DIR=$dataDirectoryForEnv
WBB_DATABASE_PATH=$databasePathForEnv
WBB_CREDENTIALS_PATH=$credentialsPathForEnv
WBB_LOG_LEVEL=warning
"@
    Set-Content -LiteralPath (Join-Path $launchDirectory '.env') -Encoding utf8 -NoNewline -Value $environmentFile

    # 无参数启动覆盖双击 EXE 的真实入口；配置只允许来自复制后 EXE 同目录的假 .env。
    $process = Start-Process -FilePath $smokeExecutable -WorkingDirectory $launchDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $baseUrl = "http://127.0.0.1:$port"
    $ready = $false
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        Start-Sleep -Milliseconds 250
        try {
            $response = Invoke-WebRequest -Uri "$baseUrl/health/live" -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            if ($process.HasExited) { break }
        }
    }
    if (-not $ready) {
        $diagnostic = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Raw) } else { '' }
        throw "发布 EXE 未通过 /health/live：$diagnostic"
    }

    $openApi = Invoke-RestMethod -Uri "$baseUrl/openapi.json" -TimeoutSec 5
    $publicPaths = @($openApi.paths.PSObject.Properties.Name)
    if ($publicPaths.Count -ne 1 -or $publicPaths[0] -ne '/api/v1/messages') {
        throw "外部 OpenAPI 路径异常：$($publicPaths -join ', ')"
    }

    $basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("admin:$testPassword"))
    $headers = @{ Authorization = "Basic $basic" }
    $admin = Invoke-WebRequest -Uri "$baseUrl/admin" -Headers $headers -UseBasicParsing -TimeoutSec 5
    if ($admin.StatusCode -ne 200 -or $admin.Content -notmatch '微信桥接') {
        throw '内嵌管理页面不可用。'
    }
    $static = Invoke-WebRequest -Uri "$baseUrl/admin/static/chat.css" -Headers $headers -UseBasicParsing -TimeoutSec 5
    if ($static.StatusCode -ne 200 -or $static.Content.Length -lt 100) {
        throw '内嵌管理页面静态资源不可用。'
    }
    if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
        throw 'SQLite 未写入烟测指定的外部数据目录。'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $testData 'data\wechat-bot-bridge.lock') -PathType Leaf)) {
        throw '单实例锁未写入烟测指定的外部数据目录。'
    }

    $secondStdout = Join-Path $testData 'second-stdout.log'
    $secondStderr = Join-Path $testData 'second-stderr.log'
    $secondProcess = Start-Process -FilePath $smokeExecutable -WorkingDirectory $launchDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput $secondStdout -RedirectStandardError $secondStderr
    if (-not $secondProcess.WaitForExit(15000)) {
        throw '第二个 EXE 未被单实例锁及时拒绝。'
    }
    if ($secondProcess.ExitCode -ne 2) {
        throw "第二个 EXE 退出码应为 2，实际为 $($secondProcess.ExitCode)。"
    }
    $stillReady = Invoke-WebRequest -Uri "$baseUrl/health/live" -UseBasicParsing -TimeoutSec 2
    if ($stillReady.StatusCode -ne 200) {
        throw '拒绝第二个实例后，首个 Bridge 不再健康。'
    }

    Write-Host 'Windows 单文件 EXE、默认 serve、单实例、外部数据目录、OpenAPI、管理页面与静态资源验证通过。'
} finally {
    if ($null -ne $secondProcess -and -not $secondProcess.HasExited) {
        Stop-ProcessTree -RootProcessId $secondProcess.Id
    }
    if ($null -ne $process) {
        Stop-ProcessTree -RootProcessId $process.Id
    }
    foreach ($name in $environmentNames) {
        if ($null -eq $previousEnvironment[$name]) {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
        }
    }
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $resolvedTestData = [IO.Path]::GetFullPath($testData)
    if ($resolvedTestData.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedTestData)) {
        Remove-Item -LiteralPath $resolvedTestData -Recurse -Force
    }
}
