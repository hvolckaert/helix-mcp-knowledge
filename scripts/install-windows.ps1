#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '1.31.6',

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
$ghVersion = '2.100.0'
$trustedGitHubHosts = @(
    'api.github.com',
    'github.com',
    'objects.githubusercontent.com',
    'release-assets.githubusercontent.com'
)
$githubEnvironmentKeys = @(
    'GH_ENTERPRISE_TOKEN',
    'GH_HOST',
    'GH_REPO',
    'GH_TOKEN',
    'GITHUB_ENTERPRISE_TOKEN',
    'GITHUB_REPOSITORY',
    'GITHUB_TOKEN'
)

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

    $temporary = Join-Path ([IO.Path]::GetTempPath()) ".github-json-$([Guid]::NewGuid())"
    try {
        Save-TrustedGitHubAsset -Uri $Uri -Destination $temporary -MaxBytes 16MB `
            -Accept 'application/vnd.github+json'
        # Windows PowerShell 5.1 otherwise treats UTF-8 without a BOM as the
        # active ANSI code page.  That can corrupt signed checkpoint text in an
        # attestation bundle and make an otherwise valid signature unverifiable.
        return Get-Content -LiteralPath $temporary -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Assert-TrustedGitHubUri {
    param(
        [Parameter(Mandatory = $true)]
        [Uri]$Uri
    )

    if (-not $Uri.IsAbsoluteUri -or $Uri.Scheme -ne 'https' -or `
        $trustedGitHubHosts -notcontains $Uri.DnsSafeHost.ToLowerInvariant()) {
        throw "GitHub request left trusted GitHub hosts: $Uri"
    }
}

function Save-TrustedGitHubAsset {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,

        [Parameter(Mandatory = $true)]
        [string]$Destination,

        [long]$MaxBytes = 256MB,

        [string]$Accept = 'application/octet-stream'
    )

    Add-Type -AssemblyName System.Net.Http
    $currentUri = [Uri]$Uri
    Assert-TrustedGitHubUri -Uri $currentUri
    $temporary = Join-Path (Split-Path -Parent $Destination) ".download-$([Guid]::NewGuid())"
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(300)
    try {
        for ($redirects = 0; $redirects -lt 10; $redirects++) {
            $request = [Net.Http.HttpRequestMessage]::new(
                [Net.Http.HttpMethod]::Get,
                $currentUri
            )
            $request.Headers.UserAgent.ParseAdd('helix-mcp-knowledge-installer')
            $request.Headers.Accept.ParseAdd($Accept)
            $response = $client.SendAsync(
                $request,
                [Net.Http.HttpCompletionOption]::ResponseHeadersRead
            ).GetAwaiter().GetResult()
            try {
                $statusCode = [int]$response.StatusCode
                if ($statusCode -ge 300 -and $statusCode -lt 400) {
                    $location = $response.Headers.Location
                    if (-not $location) {
                        throw 'GitHub returned an invalid redirect'
                    }
                    if (-not $location.IsAbsoluteUri) {
                        $location = [Uri]::new($currentUri, $location)
                    }
                    Assert-TrustedGitHubUri -Uri $location
                    $currentUri = $location
                    continue
                }
                # PowerShell emits every uncaptured expression from a function.
                # HttpResponseMessage.EnsureSuccessStatusCode() returns the response,
                # so discard it explicitly; otherwise callers expecting JSON receive
                # both the response object and the parsed payload on Windows PowerShell 5.1.
                [void]$response.EnsureSuccessStatusCode()
                $contentLength = $response.Content.Headers.ContentLength
                if ($null -ne $contentLength -and [long]$contentLength -gt $MaxBytes) {
                    throw "GitHub asset is too large: $Destination"
                }
                $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                $outputStream = [IO.File]::Open(
                    $temporary,
                    [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write,
                    [IO.FileShare]::None
                )
                try {
                    $buffer = New-Object byte[] (1024 * 1024)
                    $written = 0L
                    while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $written += $count
                        if ($written -gt $MaxBytes) {
                            throw "GitHub asset is too large: $Destination"
                        }
                        $outputStream.Write($buffer, 0, $count)
                    }
                }
                finally {
                    $outputStream.Dispose()
                    $inputStream.Dispose()
                }
                Move-Item -LiteralPath $temporary -Destination $Destination -Force
                return
            }
            finally {
                $response.Dispose()
                $request.Dispose()
            }
        }
        throw 'GitHub request has too many redirects'
    }
    finally {
        $client.Dispose()
        $handler.Dispose()
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-ManagedGitHubCli {
    param(
        [Parameter(Mandatory = $true)]
        [string]$GhCommand,

        [Parameter(Mandatory = $true)]
        [string]$ConfigDirectory,

        [string[]]$ArgumentList = @(),

        [switch]$Capture
    )

    $previous = @{}
    foreach ($key in $githubEnvironmentKeys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
    }
    $previous['GH_CONFIG_DIR'] = [Environment]::GetEnvironmentVariable(
        'GH_CONFIG_DIR', 'Process'
    )
    $previous['GH_NO_UPDATE_NOTIFIER'] = [Environment]::GetEnvironmentVariable(
        'GH_NO_UPDATE_NOTIFIER', 'Process'
    )
    $previous['GH_PROMPT_DISABLED'] = [Environment]::GetEnvironmentVariable(
        'GH_PROMPT_DISABLED', 'Process'
    )
    try {
        foreach ($key in $githubEnvironmentKeys) {
            [Environment]::SetEnvironmentVariable($key, $null, 'Process')
        }
        [Environment]::SetEnvironmentVariable('GH_CONFIG_DIR', $ConfigDirectory, 'Process')
        [Environment]::SetEnvironmentVariable('GH_NO_UPDATE_NOTIFIER', '1', 'Process')
        [Environment]::SetEnvironmentVariable('GH_PROMPT_DISABLED', '1', 'Process')
        if ($Capture) {
            return Invoke-NativeCapture -FilePath $GhCommand -ArgumentList $ArgumentList
        }
        # Display command output without allowing it to become function output.
        # Install-ManagedGitHubCli must return only the resolved executable path.
        Invoke-NativeCommand -FilePath $GhCommand -ArgumentList $ArgumentList | Out-Host
    }
    finally {
        foreach ($key in $previous.Keys) {
            [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
        }
    }
}

function Install-ManagedGitHubCli {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $osArchitecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    if ($osArchitecture -ne 'X64') {
        throw "Managed GitHub CLI is unavailable for Windows $osArchitecture"
    }
    $archiveName = "gh_${Version}_windows_amd64.zip"
    $archiveSha256 = '227e35230b25db3fa1b997bab7cf4d67df0470a3b75b99e4ee66bce1a7cd4e72'
    $binaryMember = 'bin/gh.exe'
    $binarySha256 = '2ae2b350c227a618f2d8965b1900aeee13446ff42e17ef0bd5a0b6405c593cfb'
    $versionRoot = Join-Path $Root "tools\github-cli\$Version"
    $binDirectory = Join-Path $versionRoot 'bin'
    $configDirectory = Join-Path $Root 'tools\github-cli\config'
    $command = Join-Path $binDirectory 'gh.exe'
    foreach ($directory in @(
        $Root,
        (Join-Path $Root 'tools'),
        (Join-Path $Root 'tools\github-cli'),
        $versionRoot,
        $binDirectory,
        $configDirectory
    )) {
        if (Test-Path -LiteralPath $directory) {
            $item = Get-Item -LiteralPath $directory -Force
            if (-not $item.PSIsContainer -or `
                $item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Managed GitHub CLI directory is not safe: $directory"
            }
        }
        else {
            New-Item -ItemType Directory -Path $directory | Out-Null
        }
    }
    if (Test-Path -LiteralPath $command) {
        $commandItem = Get-Item -LiteralPath $command -Force
        if ($commandItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Managed GitHub CLI cannot be a link'
        }
    }
    $installBinary = -not (Test-Path -LiteralPath $command -PathType Leaf)
    if (-not $installBinary) {
        $installedHash = (Get-FileHash -LiteralPath $command -Algorithm SHA256).Hash
        $installBinary = $installedHash.ToLowerInvariant() -ne $binarySha256
    }
    if ($installBinary) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = Join-Path $versionRoot ".download-$([Guid]::NewGuid()).zip"
        $temporaryBinary = Join-Path $versionRoot ".gh-$([Guid]::NewGuid()).exe"
        try {
            Save-TrustedGitHubAsset `
                -Uri "https://github.com/cli/cli/releases/download/v$Version/$archiveName" `
                -Destination $archive -MaxBytes 64MB
            $actualArchiveHash = (
                Get-FileHash -LiteralPath $archive -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            if ($actualArchiveHash -ne $archiveSha256) {
                throw 'GitHub CLI archive digest mismatch'
            }
            $bundle = [IO.Compression.ZipFile]::OpenRead($archive)
            try {
                $entries = @($bundle.Entries | Where-Object { $_.FullName -eq $binaryMember })
                if ($entries.Count -ne 1) {
                    throw 'GitHub CLI archive has an invalid executable'
                }
                $unixType = ($entries[0].ExternalAttributes -shr 16) -band 0xF000
                if ($entries[0].Length -gt 64MB -or `
                    $unixType -eq 0xA000) {
                    throw 'GitHub CLI archive has an invalid executable'
                }
                $inputStream = $entries[0].Open()
                $outputStream = [IO.File]::Open(
                    $temporaryBinary,
                    [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write,
                    [IO.FileShare]::None
                )
                try {
                    $buffer = New-Object byte[] (1024 * 1024)
                    $written = 0L
                    while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $written += $count
                        if ($written -gt 64MB) {
                            throw 'GitHub CLI executable exceeds the size limit'
                        }
                        $outputStream.Write($buffer, 0, $count)
                    }
                }
                finally {
                    $outputStream.Dispose()
                    $inputStream.Dispose()
                }
            }
            finally {
                $bundle.Dispose()
            }
            $actualBinaryHash = (
                Get-FileHash -LiteralPath $temporaryBinary -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            if ($actualBinaryHash -ne $binarySha256) {
                throw 'GitHub CLI binary digest mismatch'
            }
            $versionOutput = Invoke-ManagedGitHubCli -GhCommand $temporaryBinary `
                -ConfigDirectory $configDirectory -ArgumentList @('--version') -Capture
            if ($versionOutput -notmatch "^gh version $([Regex]::Escape($Version))") {
                throw 'Managed GitHub CLI version is invalid'
            }
            Invoke-ManagedGitHubCli -GhCommand $temporaryBinary `
                -ConfigDirectory $configDirectory `
                -ArgumentList @('attestation', 'verify', '--help')
            if (Test-Path -LiteralPath $command) {
                [IO.File]::Replace($temporaryBinary, $command, $null)
            }
            else {
                [IO.File]::Move($temporaryBinary, $command)
            }
        }
        finally {
            Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $temporaryBinary -Force -ErrorAction SilentlyContinue
        }
    }
    $versionOutput = Invoke-ManagedGitHubCli -GhCommand $command `
        -ConfigDirectory $configDirectory -ArgumentList @('--version') -Capture
    if ($versionOutput -notmatch "^gh version $([Regex]::Escape($Version))") {
        throw 'Managed GitHub CLI version is invalid'
    }
    Invoke-ManagedGitHubCli -GhCommand $command -ConfigDirectory $configDirectory `
        -ArgumentList @('attestation', 'verify', '--help')
    $metadata = @{
        schema_version = 1
        version = $Version
        command = $command
        platform = 'windows'
        architecture = 'amd64'
        source_url = "https://github.com/cli/cli/releases/download/v$Version/$archiveName"
        archive_sha256 = $archiveSha256
        binary_sha256 = $binarySha256
    } | ConvertTo-Json
    $metadataPath = Join-Path $versionRoot 'installation.json'
    $metadataTemporary = Join-Path $versionRoot ".metadata-$([Guid]::NewGuid()).json"
    try {
        [IO.File]::WriteAllText(
            $metadataTemporary,
            $metadata + "`n",
            (New-Object Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $metadataTemporary -Destination $metadataPath -Force
    }
    finally {
        Remove-Item -LiteralPath $metadataTemporary -Force -ErrorAction SilentlyContinue
    }
    return $command
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
    try {
        [IO.File]::WriteAllLines(
            $bundlePath,
            $bundles,
            (New-Object Text.UTF8Encoding($false))
        )
        Invoke-ManagedGitHubCli -GhCommand $GhCommand `
            -ConfigDirectory (Join-Path $resolvedRoot 'tools\github-cli\config') `
            -ArgumentList @(
            'attestation', 'verify', $Artifact,
            '--repo', $Repository,
            '--bundle', $bundlePath,
            '--signer-workflow', "$Repository/.github/workflows/release.yml",
            '--source-ref', "refs/tags/$ReleaseTag",
            '--deny-self-hosted-runners'
            )
    }
    finally {
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
$gh = Install-ManagedGitHubCli -Root $resolvedRoot -Version $ghVersion

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
    Save-TrustedGitHubAsset `
        -Uri "https://github.com/$Repository/releases/download/$releaseTag/$assetName" `
        -Destination (Join-Path $download $assetName)
    $wheel = Join-Path $download $assetName
    Save-TrustedGitHubAsset `
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
