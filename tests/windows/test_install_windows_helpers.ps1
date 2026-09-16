#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$installerPath = Join-Path $repositoryRoot 'scripts\install-windows.ps1'
$tokens = $null
$parseErrors = $null
$installerAst = [Management.Automation.Language.Parser]::ParseFile(
    $installerPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {
    throw "Windows installer has parse errors: $($parseErrors -join '; ')"
}

function Find-InstallerFunction {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $definition = $installerAst.Find(
        {
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and `
                $node.Name -eq $Name
        },
        $true
    )
    if (-not $definition) {
        throw "Installer function was not found: $Name"
    }
    return $definition
}

# Import only the helpers under test. Dot-sourcing the installer itself would run an install.
foreach ($functionName in @(
    'Invoke-NativeCommand',
    'Invoke-ManagedGitHubCli',
    'Invoke-PublicGitHubJson'
)) {
    $definition = Find-InstallerFunction -Name $functionName
    Invoke-Expression $definition.Extent.Text
}

$ensureCall = $installerAst.Find(
    {
        param($node)
        $node -is [Management.Automation.Language.InvokeMemberExpressionAst] -and `
            $node.Member.Value -eq 'EnsureSuccessStatusCode'
    },
    $true
)
if (-not $ensureCall -or `
    $ensureCall.Parent.Extent.Text -ne '[void]$response.EnsureSuccessStatusCode()') {
    throw 'EnsureSuccessStatusCode must be explicitly discarded from function output'
}

$githubEnvironmentKeys = @(
    'GH_ENTERPRISE_TOKEN',
    'GH_HOST',
    'GH_REPO',
    'GH_TOKEN',
    'GITHUB_ENTERPRISE_TOKEN',
    'GITHUB_REPOSITORY',
    'GITHUB_TOKEN'
)
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) "helix-installer-helper-$([Guid]::NewGuid())"
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
try {
    $fakeGh = Join-Path $temporaryRoot 'gh.cmd'
    Set-Content -LiteralPath $fakeGh -Value '@echo help-output' -Encoding Ascii
    $managedOutput = @(
        Invoke-ManagedGitHubCli `
            -GhCommand $fakeGh `
            -ConfigDirectory (Join-Path $temporaryRoot 'gh-config') `
            -ArgumentList @('attestation', 'verify', '--help')
    )
    if ($managedOutput.Count -ne 0) {
        throw "Managed GitHub CLI leaked command output: $($managedOutput -join ', ')"
    }

    function Save-TrustedGitHubAsset {
        param(
            [string]$Uri,
            [string]$Destination,
            [long]$MaxBytes,
            [string]$Accept
        )

        $emDash = [char]0x2014
        $payload = '{"checkpoint":"signed ' + $emDash + ' text"}'
        [IO.File]::WriteAllText(
            $Destination,
            $payload,
            [Text.UTF8Encoding]::new($false)
        )
    }

    $parsed = Invoke-PublicGitHubJson -Uri 'https://api.github.com/test'
    $expectedCheckpoint = 'signed ' + [char]0x2014 + ' text'
    if ($parsed.checkpoint -cne $expectedCheckpoint) {
        throw "UTF-8 GitHub JSON was decoded incorrectly: $($parsed.checkpoint)"
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

Write-Host 'Windows installer helper regressions passed.'
