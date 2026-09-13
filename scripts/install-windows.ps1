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

function Invoke-PublicGitHubJson {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri
    )

    $headers = @{
        Accept = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
        'User-Agent' = 'helix-mcp-knowledge-installer'
    }
    return Invoke-RestMethod -Uri $Uri -Headers $headers -Method Get -TimeoutSec 300
}

function Save-PublicGitHubAsset {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,

        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    $headers = @{'User-Agent' = 'helix-mcp-knowledge-installer'}
    $temporary = Join-Path (Split-Path -Parent $Destination) ".download-$([Guid]::NewGuid())"
    try {
        Invoke-WebRequest -Uri $Uri -Headers $headers -Method Get -TimeoutSec 300 `
            -OutFile $temporary -UseBasicParsing
        $item = Get-Item -LiteralPath $temporary -Force
        if ($item.Length -gt 256MB) {
            throw "Release asset is too large: $Destination"
        }
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-PublicAttestationVerification {
    param(
        [Parameter(Mandatory = $true)]
        [string]$GhCommand,

        [Parameter(Mandatory = $true)]
        [string]$Artifact,

        [Parameter(Mandatory = $true)]
        [string]$Repository,

        [Parameter(Mandatory = $true)]
        [string]$ReleaseTag,

        [Parameter(Mandatory = $true)]
        [string]$Sha256
    )

    $provenance = Invoke-PublicGitHubJson -Uri (
        "https://api.github.com/repos/$Repository/attestations/sha256:$Sha256"
    )
    $bundles = @($provenance.attestations | Where-Object { $_.bundle } | ForEach-Object {
        $_.bundle | ConvertTo-Json -Depth 100 -Compress
    })
    if ($bundles.Count -eq 0) {
        throw "Release asset has no verifiable provenance: $Artifact"
    }
    $bundlePath = Join-Path (Split-Path -Parent $Artifact) ".attestations-$([Guid]::NewGuid()).jsonl"
    $previousGhToken = [Environment]::GetEnvironmentVariable('GH_TOKEN', 'Process')
    $previousGithubToken = [Environment]::GetEnvironmentVariable('GITHUB_TOKEN', 'Process')
    try {
        [IO.File]::WriteAllLines(
            $bundlePath,
            $bundles,
            (New-Object Text.UTF8Encoding($false))
        )
        [Environment]::SetEnvironmentVariable('GH_TOKEN', $null, 'Process')
        [Environment]::SetEnvironmentVariable('GITHUB_TOKEN', $null, 'Process')
        Invoke-NativeCommand -FilePath $GhCommand -ArgumentList @(
            'attestation', 'verify', $Artifact,
            '--repo', $Repository,
            '--bundle', $bundlePath,
            '--signer-workflow', "$Repository/.github/workflows/release.yml",
            '--source-ref', "refs/tags/$ReleaseTag",
            '--deny-self-hosted-runners'
        )
    }
    finally {
        [Environment]::SetEnvironmentVariable('GH_TOKEN', $previousGhToken, 'Process')
        [Environment]::SetEnvironmentVariable('GITHUB_TOKEN', $previousGithubToken, 'Process')
        Remove-Item -LiteralPath $bundlePath -Force -ErrorAction SilentlyContinue
    }
}

if (-not $env:LOCALAPPDATA) {
    throw 'LOCALAPPDATA is not defined; run the installer from a Windows user session'
}
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw "Invalid GitHub repository: $Repository"
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

    $release = Invoke-PublicGitHubJson -Uri (
        "https://api.github.com/repos/$Repository/releases/tags/$releaseTag"
    )
    if ($release.draft -or $release.prerelease -or $release.tag_name -ne $releaseTag) {
        throw 'GitHub returned an invalid or non-stable release'
    }
    Save-PublicGitHubAsset `
        -Uri "https://github.com/$Repository/releases/download/$releaseTag/$assetName" `
        -Destination (Join-Path $download $assetName)
    $wheel = Join-Path $download $assetName
    Save-PublicGitHubAsset `
        -Uri "https://github.com/$Repository/releases/download/$releaseTag/$requirementsName" `
        -Destination (Join-Path $download $requirementsName)
    $requirements = Join-Path $download $requirementsName
    if (-not (Test-Path -LiteralPath $wheel -PathType Leaf)) {
        throw "Downloaded wheel was not found: $wheel"
    }

    $matchingAssets = @(@($release.assets) | Where-Object { $_.name -eq $assetName })
    if ($matchingAssets.Count -ne 1 -or -not $matchingAssets[0].digest) {
        throw "Release digest was not found for $assetName"
    }
    $expectedHash = $matchingAssets[0].digest -replace '^sha256:', ''
    $wheelHash = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($wheelHash -ne $expectedHash.ToLowerInvariant()) {
        throw "Wheel digest mismatch: expected $expectedHash, found $wheelHash"
    }
    Invoke-PublicAttestationVerification -GhCommand $gh -Artifact $wheel `
        -Repository $Repository -ReleaseTag $releaseTag -Sha256 $wheelHash
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
    Invoke-PublicAttestationVerification -GhCommand $gh -Artifact $requirements `
        -Repository $Repository -ReleaseTag $releaseTag -Sha256 $requirementsHash
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
