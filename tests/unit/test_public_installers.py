from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_linux_installer_uses_public_release_endpoints_and_local_verification() -> None:
    script = (REPOSITORY_ROOT / "scripts/install-linux.sh").read_text(encoding="utf-8")

    assert '"$GH" release' not in script
    assert "https://api.github.com/repos/{repository}/releases/tags/{tag}" in script
    assert "https://github.com/{repository}/releases/download/{tag}/{asset_name}" in script
    assert "https://api.github.com/repos/{repository}/attestations/sha256:{expected}" in script
    assert 'resolve_command "gh"' not in script
    assert 'GH_VERSION="2.100.0"' in script
    assert "gh_2.100.0_linux_amd64.tar.gz" not in script
    assert 'f"gh_{version}_linux_amd64.tar.gz"' in script
    assert "e4d4bb4498e8d007abe545b6568926793ace1b6447da598294a610018cb164be" in script
    assert "553949e2efa12842771efe6012aa4de21f1d591530ec17fc435f610f10e017ee" in script
    assert "ea4e7a581a32ccad6cc7923cb1576ac5859ba4b9a16ab22eb8f8a96e78e2e961" in script
    assert "28a037b967065aa314cb6d539943b55d27ef2f97c523ab2b6023ccf284e1828d" in script
    assert 'root / "tools" / "github-cli" / version' in script
    assert "TrustedRedirectHandler" in script
    assert "release-assets.githubusercontent.com" in script
    assert "max_bytes = 64 * 1024 * 1024" in script
    assert "not members[0].isfile()" in script
    assert "os.replace(binary, command)" in script
    assert '"attestation", "verify", "--help"' in script
    assert "-u GH_ENTERPRISE_TOKEN -u GH_HOST -u GH_REPO -u GH_TOKEN" in script
    assert "-u GITHUB_ENTERPRISE_TOKEN -u GITHUB_REPOSITORY -u GITHUB_TOKEN" in script
    assert "GH_NO_UPDATE_NOTIFIER=1 GH_PROMPT_DISABLED=1" in script
    assert "run_gh attestation verify" in script
    assert "INSTALL_ARGUMENTS+=(--gh-command" not in script


def test_windows_installer_uses_public_release_endpoints_and_local_verification() -> None:
    script = (REPOSITORY_ROOT / "scripts/install-windows.ps1").read_text(encoding="utf-8")

    assert "'release', 'download'" not in script
    assert "'release', 'view'" not in script
    assert "https://api.github.com/repos/$Repository/releases/tags/$releaseTag" in script
    assert "https://github.com/$Repository/releases/download/$releaseTag" in script
    assert "https://api.github.com/repos/$Repository/attestations/sha256:$Sha256" in script
    assert "Resolve-NativeCommand -Command 'gh.exe'" not in script
    assert "$ghVersion = '2.100.0'" in script
    assert "gh_${Version}_windows_amd64.zip" in script
    assert "227e35230b25db3fa1b997bab7cf4d67df0470a3b75b99e4ee66bce1a7cd4e72" in script
    assert "2ae2b350c227a618f2d8965b1900aeee13446ff42e17ef0bd5a0b6405c593cfb" in script
    assert "tools\\github-cli\\$Version" in script
    assert "$handler.AllowAutoRedirect = $false" in script
    assert ".ContentLength.Value" not in script
    assert "release-assets.githubusercontent.com" in script
    assert "-MaxBytes 64MB" in script
    assert "$unixType -eq 0xA000" in script
    assert "[IO.File]::Replace($temporaryBinary, $command, $null)" in script
    assert "@('attestation', 'verify', '--help')" in script
    assert "'GH_ENTERPRISE_TOKEN'" in script
    assert "'GITHUB_REPOSITORY'" in script
    assert "SetEnvironmentVariable('GH_PROMPT_DISABLED', '1', 'Process')" in script
    assert "'attestation', 'verify'" in script
    assert "$installArguments += @('--gh-command'" not in script
