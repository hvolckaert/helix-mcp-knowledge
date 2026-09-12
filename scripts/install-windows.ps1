#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '1.31.0',

    [string]$Repository = 'hvolckaert/helix-mcp-knowledge',

    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'helix-mcp-knowledge'),

    [string]$WheelPath,

    [string]$RequirementsPath,

    [string]$PythonCommand,

    [string[]]$PythonArgument = @(),

    [string]$OpenClawCommand = 'openclaw.cmd',

    [ValidateSet('auto', 'openclaw', 'none')]
    [string]$Client = 'auto',

    [string]$ServerName = 'helix_knowledge',

    [ValidateRange(1, 65535)]
    [int]$DashboardPort = 8765,

    [string[]]$Product = @(),

    [switch]$NoAutomaticSync,

    [switch]$NoProbe,

    [switch]$NoReload,

    [switch]$NoDashboard,

    [switch]$Resume
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$gh = $null

function Resolve-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (Test-Path -LiteralPath $Command -PathType Leaf) {
        return (Resolve-Path -LiteralPath $Command).Path
    }

    $resolved = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $resolved -or -not $resolved.Source) {
        throw "$Label was not found: $Command"
    }
    return $resolved.Source
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @()
    )

    $global:LASTEXITCODE = 0
    & $FilePath @ArgumentList
    $exitCode = $global:LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $FilePath"
    }
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @()
    )

    $global:LASTEXITCODE = 0
    $output = & $FilePath @ArgumentList 2>&1
    $exitCode = $global:LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $FilePath`n$($output -join "`n")"
    }
    return ($output -join "`n")
}

if (-not $env:LOCALAPPDATA) {
    throw 'LOCALAPPDATA is not defined; run the installer from a Windows user session'
}
foreach ($selection in $Product) {
    if ($selection -notmatch '^[a-z][a-z0-9_-]*=([0-9][0-9A-Za-z._-]*|current)$') {
        throw "Invalid product selection: $selection"
    }
}

if ($PythonCommand) {
    $python = Resolve-NativeCommand -Command $PythonCommand -Label 'Python'
    $pythonPrefix = @($PythonArgument)
}
elseif (Get-Command 'py.exe' -ErrorAction SilentlyContinue) {
    $python = Resolve-NativeCommand -Command 'py.exe' -Label 'Python launcher'
    $pythonPrefix = @('-3.12')
}
else {
    $python = Resolve-NativeCommand -Command 'python.exe' -Label 'Python'
    $pythonPrefix = @()
}

if ($Client -eq 'auto') {
    $detectedOpenClaw = Get-Command $OpenClawCommand -ErrorAction SilentlyContinue
    if ($detectedOpenClaw) {
        $Client = 'openclaw'
    }
    else {
        $Client = 'none'
    }
}
if ($Client -eq 'openclaw') {
    $openclaw = Resolve-NativeCommand -Command $OpenClawCommand -Label 'OpenClaw'
}
else {
    $openclaw = $null
}
$resolvedRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$runtime = Join-Path $resolvedRoot "runtime\$Version"
$venv = Join-Path $runtime 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$cli = Join-Path $venv 'Scripts\helix-mcp-knowledge.exe'
$server = Join-Path $venv 'Scripts\helix-mcp-knowledge-server.exe'
$config = Join-Path $resolvedRoot 'config\config.yaml'

if ($WheelPath) {
    $wheel = (Resolve-Path -LiteralPath $WheelPath -ErrorAction Stop).ProviderPath
    $wheelHash = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "Using caller-provided wheel (sha256:$wheelHash): $wheel"
    if ($RequirementsPath) {
        $requirementsCandidate = $RequirementsPath
    }
    else {
        $requirementsCandidate = Join-Path (Split-Path -Parent $wheel) 'runtime-requirements.txt'
    }
    $requirements = (Resolve-Path -LiteralPath $requirementsCandidate -ErrorAction Stop).ProviderPath
    $requirementsItem = Get-Item -LiteralPath $requirements -Force
    if ($requirementsItem.PSIsContainer -or $requirementsItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Locked runtime requirements must be a regular file: $requirements"
    }
    $requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "Using caller-provided locked requirements (sha256:$requirementsHash): $requirements"
}
else {
    if ($RequirementsPath) {
        throw '-RequirementsPath can only be used together with -WheelPath'
    }
    $gh = Resolve-NativeCommand -Command 'gh.exe' -Label 'GitHub CLI'
    $download = Join-Path $resolvedRoot "downloads\$Version"
    New-Item -ItemType Directory -Force -Path $download | Out-Null
    $assetName = "helix_mcp_knowledge-$Version-py3-none-any.whl"
    $requirementsName = 'runtime-requirements.txt'
    $releaseTag = "v$Version"

    Invoke-NativeCommand -FilePath $gh -ArgumentList @(
        'release', 'download', $releaseTag,
        '--repo', $Repository,
        '--pattern', $assetName,
        '--dir', $download,
        '--clobber'
    )
    $wheel = Join-Path $download $assetName
    Invoke-NativeCommand -FilePath $gh -ArgumentList @(
        'release', 'download', $releaseTag,
        '--repo', $Repository,
        '--pattern', $requirementsName,
        '--dir', $download,
        '--clobber'
    )
    $requirements = Join-Path $download $requirementsName
    if (-not (Test-Path -LiteralPath $wheel -PathType Leaf)) {
        throw "Downloaded wheel was not found: $wheel"
    }

    $releaseJson = Invoke-NativeCapture -FilePath $gh -ArgumentList @(
        'release', 'view', $releaseTag,
        '--repo', $Repository,
        '--json', 'assets'
    )
    $release = $releaseJson | ConvertFrom-Json
    $matchingAssets = @(@($release.assets) | Where-Object { $_.name -eq $assetName })
    if ($matchingAssets.Count -ne 1 -or -not $matchingAssets[0].digest) {
        throw "Release digest was not found for $assetName"
    }
    $expectedHash = $matchingAssets[0].digest -replace '^sha256:', ''
    $wheelHash = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($wheelHash -ne $expectedHash.ToLowerInvariant()) {
        throw "Wheel digest mismatch: expected $expectedHash, found $wheelHash"
    }
    Write-Host "Verified release wheel sha256:$wheelHash"
    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        throw "Downloaded locked requirements were not found: $requirements"
    }
    $matchingRequirements = @(@($release.assets) | Where-Object { $_.name -eq $requirementsName })
    if ($matchingRequirements.Count -ne 1 -or -not $matchingRequirements[0].digest) {
        throw "Release digest was not found for $requirementsName"
    }
    $expectedRequirementsHash = $matchingRequirements[0].digest -replace '^sha256:', ''
    $requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($requirementsHash -ne $expectedRequirementsHash.ToLowerInvariant()) {
        throw "Requirements digest mismatch: expected $expectedRequirementsHash, found $requirementsHash"
    }
    Write-Host "Verified locked runtime requirements sha256:$requirementsHash"
}

if (Test-Path -LiteralPath $runtime) {
    if (-not $Resume) {
        throw "Versioned runtime already exists: $runtime (use -Resume to repair it)"
    }
    Write-Host "Resuming existing runtime: $runtime"
}
else {
    New-Item -ItemType Directory -Path $runtime | Out-Null
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Invoke-NativeCommand -FilePath $python -ArgumentList ($pythonPrefix + @('-m', 'venv', $venv))
}
Invoke-NativeCommand -FilePath $venvPython -ArgumentList @(
    '-m', 'pip', 'install', '--disable-pip-version-check',
    '--require-hashes', '--requirement', $requirements
)
Invoke-NativeCommand -FilePath $venvPython -ArgumentList @(
    '-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps', $wheel
)
Invoke-NativeCommand -FilePath $venvPython -ArgumentList @('-m', 'pip', 'check')
$installedVersion = Invoke-NativeCapture -FilePath $venvPython -ArgumentList @(
    '-c', 'import helix_mcp_knowledge; print(helix_mcp_knowledge.__version__)'
)
if ($installedVersion.Trim() -ne $Version) {
    throw "Installed version $($installedVersion.Trim()) does not match requested version $Version"
}

if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) {
    throw "CLI entry point was not installed: $cli"
}
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
    throw "Server entry point was not installed: $server"
}

if ($Client -eq 'openclaw') {
    $installArguments = @(
        'install-openclaw',
        '--workspace', $resolvedRoot,
        '--server-name', $ServerName,
        '--openclaw-command', $openclaw,
        '--connect-timeout', '30',
        '--request-timeout', '60'
    )
}
else {
    $installArguments = @('install', '--workspace', $resolvedRoot)
}
$installArguments += @('--dashboard-port', $DashboardPort.ToString())
if ($gh) {
    $installArguments += @('--gh-command', $gh)
}
foreach ($selection in $Product) {
    $installArguments += @('--product', $selection)
}
if ($NoAutomaticSync) {
    $installArguments += '--no-automatic-sync'
}
else {
    $installArguments += '--automatic-sync'
}
if ($Client -eq 'openclaw') {
    if ($NoProbe) {
        $installArguments += '--no-probe'
    }
    if ($NoReload) {
        $installArguments += '--no-reload'
    }
}
if ($NoDashboard) {
    $installArguments += '--no-dashboard'
}

Invoke-NativeCommand -FilePath $cli -ArgumentList $installArguments
Invoke-NativeCommand -FilePath $cli -ArgumentList @('--config', $config, 'status')

if (-not $Product -or $Product.Count -eq 0) {
    if ($NoDashboard) {
        Write-Host "No products were selected. The dashboard supervisor is running at http://127.0.0.1:$DashboardPort/."
    }
    else {
        Write-Host 'No products were selected. The dashboard is opening for first-run setup.'
    }
}

if ($Client -eq 'openclaw' -and -not $NoProbe) {
    Invoke-NativeCommand -FilePath $openclaw -ArgumentList @(
        'mcp', 'doctor', $ServerName, '--probe'
    )
    Invoke-NativeCommand -FilePath $openclaw -ArgumentList @(
        'mcp', 'probe', $ServerName, '--json'
    )
}

Write-Host "Installed helix-mcp-knowledge $Version in $runtime"
Write-Host "Configuration: $config"
Write-Host "Stable MCP command: $(Join-Path $resolvedRoot 'bin\helix-mcp-knowledge-server.cmd')"
if ($Client -eq 'none') {
    Write-Host 'No MCP client was configured. Register the stable MCP command in your client when ready.'
}
Write-Host "Rollback runtimes, if any, were not modified."
