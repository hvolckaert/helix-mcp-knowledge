from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_linux_installer_uses_public_release_endpoints_and_local_verification() -> None:
    script = (REPOSITORY_ROOT / "scripts/install-linux.sh").read_text(encoding="utf-8")

    assert '"$GH" release' not in script
    assert "https://api.github.com/repos/{repository}/releases/tags/{tag}" in script
    assert "https://github.com/{repository}/releases/download/{tag}/{asset_name}" in script
    assert "https://api.github.com/repos/{repository}/attestations/sha256:{expected}" in script
    assert "env -u GH_TOKEN -u GITHUB_TOKEN" in script
    assert '"$GH" attestation verify' in script


def test_windows_installer_uses_public_release_endpoints_and_local_verification() -> None:
    script = (REPOSITORY_ROOT / "scripts/install-windows.ps1").read_text(encoding="utf-8")

    assert "'release', 'download'" not in script
    assert "'release', 'view'" not in script
    assert "https://api.github.com/repos/$Repository/releases/tags/$releaseTag" in script
    assert "https://github.com/$Repository/releases/download/$releaseTag" in script
    assert "https://api.github.com/repos/$Repository/attestations/sha256:$Sha256" in script
    assert "SetEnvironmentVariable('GH_TOKEN', $null, 'Process')" in script
    assert "SetEnvironmentVariable('GITHUB_TOKEN', $null, 'Process')" in script
    assert "'attestation', 'verify'" in script
