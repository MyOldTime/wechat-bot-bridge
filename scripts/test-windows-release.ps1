[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [string]$DistRoot
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

if (-not ('BridgeTraySmoke' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class BridgeTraySmoke {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string cls, string title);
    [DllImport("user32.dll")]
    static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr window, uint msg, IntPtr wp, IntPtr lp);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr LoadImage(IntPtr instance, string file, uint type, int width, int height, uint flags);
    [DllImport("user32.dll")]
    public static extern bool DestroyIcon(IntPtr icon);
    [StructLayout(LayoutKind.Sequential)]
    struct IconId { public uint size; public IntPtr window; public uint id; public Guid guid; }
    [StructLayout(LayoutKind.Sequential)]
    struct Rect { public int left, top, right, bottom; }
    [DllImport("shell32.dll")]
    static extern int Shell_NotifyIconGetRect(ref IconId id, out Rect rect);

    public static IntPtr Find(uint pid, string cls) {
        IntPtr window = IntPtr.Zero;
        while ((window = FindWindowEx(IntPtr.Zero, window, cls, null)) != IntPtr.Zero) {
            uint owner;
            GetWindowThreadProcessId(window, out owner);
            if (owner == pid) return window;
        }
        return IntPtr.Zero;
    }

    public static bool HasTrayIcon(IntPtr window) {
        var id = new IconId { size = (uint)Marshal.SizeOf(typeof(IconId)), window = window, id = 1 };
        Rect rect;
        return Shell_NotifyIconGetRect(ref id, out rect) >= 0 && rect.right > rect.left && rect.bottom > rect.top;
    }
}
'@
}

function Wait-TestWindow {
    param([Diagnostics.Process] $Process, [string] $WindowClass)
    # PyInstaller one-file 的托盘和对话框都属于本次启动器的子进程。
    for ($attempt = 0; $attempt -lt 100 -and -not $Process.HasExited; $attempt++) {
        foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($Process.Id)")) {
            $window = [BridgeTraySmoke]::Find($child.ProcessId, $WindowClass)
            if ($window -ne [IntPtr]::Zero) { return $window }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "没有找到烟测进程的窗口 $WindowClass。"
}

function Close-FirstLaunch {
    param([Diagnostics.Process] $Process)
    # 8000 可能正由用户的服务占用。只关闭测试子进程的托盘或错误提示，不访问该端口。
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        if ($Process.HasExited) { return }
        foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($Process.Id)")) {
            foreach ($class in @('WeChatBotBridge.Tray', '#32770')) {
                $window = [BridgeTraySmoke]::Find($child.ProcessId, $class)
                if ($window -ne [IntPtr]::Zero) {
                    $null = [BridgeTraySmoke]::PostMessage($window, 0x10, [IntPtr]::Zero, [IntPtr]::Zero)
                }
            }
        }
        Start-Sleep -Milliseconds 250
    }
    throw '首次启动的测试进程未能退出。'
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $DistRoot) { $DistRoot = Join-Path $projectRoot 'dist' }
$DistRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DistRoot)
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot 'build-windows.ps1') -DistRoot $DistRoot
}

$outputDir = Join-Path $DistRoot 'wechat-bot-bridge-windows-x64'
$executable = Join-Path $outputDir 'wechat-bot-bridge.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "未找到发布 EXE：$executable"
}

$reader = [IO.BinaryReader]::new([IO.File]::OpenRead($executable))
try {
    $reader.BaseStream.Position = 0x3c
    $peOffset = $reader.ReadInt32()
    $reader.BaseStream.Position = $peOffset + 24 + 68
    if ($reader.ReadUInt16() -ne 2) { throw 'EXE 未使用 Windows GUI 子系统。' }
} finally { $reader.Dispose() }
Add-Type -AssemblyName System.Drawing
$shellIcon = [Drawing.Icon]::ExtractAssociatedIcon($executable)
# Windows PowerShell 的 .NET Framework 无法可靠解码带 PNG 帧的 ICO，使用与托盘相同的原生加载器。
$designHandle = [BridgeTraySmoke]::LoadImage([IntPtr]::Zero, (Join-Path $projectRoot 'packaging\bridge.ico'), 1, $shellIcon.Width, $shellIcon.Height, 0x10)
if ($designHandle -eq [IntPtr]::Zero) { throw 'Windows 无法加载设计图标。' }
$designIcon = [Drawing.Icon]::FromHandle($designHandle)
$actual = $shellIcon.ToBitmap()
$expected = $designIcon.ToBitmap()
try {
    for ($x = 0; $x -lt $actual.Width; $x++) {
        for ($y = 0; $y -lt $actual.Height; $y++) {
            if ($actual.GetPixel($x, $y).ToArgb() -ne $expected.GetPixel($x, $y).ToArgb()) {
                throw 'Windows 从 EXE 提取的图标与设计文件不一致。'
            }
        }
    }
} finally {
    $actual.Dispose()
    $expected.Dispose()
    $shellIcon.Dispose()
    $designIcon.Dispose()
    $null = [BridgeTraySmoke]::DestroyIcon($designHandle)
}

$testData = Join-Path ([IO.Path]::GetTempPath()) ("wechat-bot-bridge-release-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testData | Out-Null
$launchDirectory = Join-Path $testData '桥接 release'
New-Item -ItemType Directory -Path $launchDirectory | Out-Null
$smokeExecutable = Join-Path $launchDirectory 'wechat-bot-bridge.exe'
Copy-Item -LiteralPath $executable -Destination $smokeExecutable
$testProfile = Join-Path $testData 'local-app-data'
$dataDirectory = Join-Path $testProfile 'wechat-bot-bridge'
$configFile = Join-Path $dataDirectory 'config\settings.json'
$databasePath = Join-Path $dataDirectory 'wechat-bridge.db'
$port = Get-FreeLoopbackPort
$environmentNames = @('LOCALAPPDATA', 'WBB_PORT', 'WBB_WEB_PASSWORD')
$previousEnvironment = @{}
$process = $null
$secondProcess = $null

try {
    foreach ($name in $environmentNames) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    $env:LOCALAPPDATA = $testProfile
    $env:WBB_PORT = 'invalid-inherited-port'
    $env:WBB_WEB_PASSWORD = 'ignored-environment-example'
    foreach ($directory in @($testData, $launchDirectory)) {
        [IO.File]::WriteAllBytes((Join-Path $directory '.env'), [byte[]]@(0xff))
    }
    $process = Start-Process -FilePath $smokeExecutable -WorkingDirectory $testData -WindowStyle Hidden -PassThru
    for ($attempt = 0; $attempt -lt 80 -and -not (Test-Path -LiteralPath $configFile); $attempt++) {
        if ($process.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) { throw '首次启动没有生成桌面配置。' }
    $config = [IO.File]::ReadAllText($configFile) | ConvertFrom-Json
    $testPassword = $config.WBB_WEB_PASSWORD
    if ($config.WBB_WEB_USERNAME -ne 'admin' -or $config.WBB_PORT -ne 8000 -or
        $testPassword -notmatch '^[A-Za-z0-9_-]{32,}$') { throw '首次生成的登录配置无效。' }
    Close-FirstLaunch $process

    # 复用首次启动生成的密码，在随机端口验证正常登录和自定义配置。
    $config.WBB_PORT = $port
    $config | Add-Member -MemberType NoteProperty -Name WBB_LOG_LEVEL -Value debug
    [IO.File]::WriteAllText($configFile, ($config | ConvertTo-Json))
    $process = Start-Process -FilePath $smokeExecutable -WorkingDirectory $testData -WindowStyle Hidden -PassThru
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
        throw '发布 EXE 未通过 /health/live。'
    }

    $openApi = Invoke-RestMethod -Uri "$baseUrl/openapi.json" -TimeoutSec 5
    if (@($openApi.paths.PSObject.Properties | ForEach-Object Name).Count -ne 0) {
        throw '外部 OpenAPI 中仍有 REST 业务路径。'
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
    $trayWindow = Wait-TestWindow $process 'WeChatBotBridge.Tray'
    if (-not [BridgeTraySmoke]::HasTrayIcon($trayWindow)) {
        throw 'Windows 通知区没有注册本次 EXE 的图标。'
    }
    $mcpToken = (Invoke-RestMethod -Uri "$baseUrl/admin/api/public-api-token" -Headers $headers -TimeoutSec 5).token
    $mcpHeaders = @{ Authorization = "Bearer $mcpToken"; Accept = 'application/json, text/event-stream' }
    $rpc = @{ jsonrpc = '2.0'; id = 1; method = 'initialize'; params = @{
        protocolVersion = '2025-11-25'; capabilities = @{}; clientInfo = @{ name = 'release-smoke'; version = '1' }
    }}
    $init = Invoke-RestMethod -Method Post -Uri "$baseUrl/mcp" -Headers $mcpHeaders -ContentType 'application/json' -Body ($rpc | ConvertTo-Json -Depth 5) -TimeoutSec 5
    if ($init.result.serverInfo.name -ne 'wechat-bot-bridge') { throw 'MCP 握手失败。' }
    $mcpHeaders['MCP-Protocol-Version'] = $init.result.protocolVersion
    $null = Invoke-RestMethod -Method Post -Uri "$baseUrl/mcp" -Headers $mcpHeaders -ContentType 'application/json' -Body '{"jsonrpc":"2.0","method":"notifications/initialized"}' -TimeoutSec 5
    $discovery = Invoke-RestMethod -Method Post -Uri "$baseUrl/mcp" -Headers $mcpHeaders -ContentType 'application/json' -Body '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' -TimeoutSec 5
    if ($discovery.result.tools.Count -ne 1 -or $discovery.result.tools[0].name -ne 'send_wechat_message') { throw 'MCP 工具发现失败。' }
    $sendTool = $discovery.result.tools[0]
    if (-not $sendTool.inputSchema.properties.message.description -or
        $sendTool.inputSchema.'$defs'.TextMessageRequest.properties.text.description -notmatch 'Markdown' -or
        -not $sendTool.outputSchema.properties.sent_at.description) {
        throw '发布版的 MCP 字段说明或 Markdown 正文指引缺失。'
    }
    $call = Invoke-RestMethod -Method Post -Uri "$baseUrl/mcp" -Headers $mcpHeaders -ContentType 'application/json' -Body '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"send_wechat_message","arguments":{"message":{"type":"text","text":"smoke"}}}}' -TimeoutSec 5
    if (-not $call.result.isError -or ($call.result.content[0].text | ConvertFrom-Json).code -ne 'wechat_not_ready') {
        throw '无微信授权时 MCP 工具未返回预期错误。'
    }
    $invalid = Invoke-RestMethod -Method Post -Uri "$baseUrl/mcp" -Headers $mcpHeaders -ContentType 'application/json' -Body '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"send_wechat_message","arguments":"release-private-example"}}' -TimeoutSec 5
    if ($invalid.error.code -ne -32602) { throw 'MCP 无效参数未被拒绝。' }
    if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
        throw 'SQLite 未写入烟测指定的外部数据目录。'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $dataDirectory 'wechat-bot-bridge.lock') -PathType Leaf)) {
        throw '单实例锁未写入烟测指定的外部数据目录。'
    }

    $secondProcess = Start-Process -FilePath $smokeExecutable -WorkingDirectory $testData -WindowStyle Hidden -PassThru
    $dialog = Wait-TestWindow $secondProcess '#32770'
    $null = [BridgeTraySmoke]::PostMessage($dialog, 0x10, [IntPtr]::Zero, [IntPtr]::Zero)
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

    # WM_COMMAND 的 ID 2 与用户选择托盘菜单的“退出”走同一个处理函数。
    if (-not [BridgeTraySmoke]::PostMessage($trayWindow, 0x111, [IntPtr]2, [IntPtr]::Zero) -or
        -not $process.WaitForExit(30000) -or $process.ExitCode -ne 0) {
        throw '托盘退出没有正常结束服务。'
    }
    $log = [IO.File]::ReadAllText((Join-Path $dataDirectory 'wechat-bot-bridge.log'))
    if (-not $log.Contains('微信桥接服务已安全停止') -or $log.Contains($mcpToken) -or
        $log.Contains($testPassword) -or $log.Contains('release-private-example')) {
        throw '日志未记录安全退出，或意外包含测试令牌或 MCP 请求正文。'
    }
    Write-Host 'EXE 首次生成配置、随机密码复用、忽略 .env、托盘启停、单实例、MCP 和管理页面验证通过。'
} finally {
    if ($null -ne $secondProcess -and -not $secondProcess.HasExited) {
        Stop-ProcessTree -RootProcessId $secondProcess.Id
    }
    if ($null -ne $process -and -not $process.HasExited) {
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
