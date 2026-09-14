"""Tests for managed GitHub CLI provisioning."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Literal

import httpx
import pytest

import helix_mcp_knowledge.github_cli as github_cli
from helix_mcp_knowledge.github_cli import (
    GITHUB_CLI_VERSION,
    GitHubCliAsset,
    GitHubCliError,
    PublicGitHubCliTransport,
    ensure_managed_github_cli,
    github_cli_asset,
    github_cli_environment,
    managed_github_cli_plan,
)


class FakeTransport:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[str] = []

    def download(self, url: str, destination: Path, *, timeout: int) -> None:
        assert timeout == 300
        self.calls.append(url)
        destination.write_bytes(self.content)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object):
        self.calls.append((command, kwargs))
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert not github_cli._AUTH_ENVIRONMENT_KEYS.intersection(environment)
        assert environment["GH_PROMPT_DISABLED"] == "1"
        stdout = f"gh version {GITHUB_CLI_VERSION} (test)\n" if command[1:] == ["--version"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")


def _tar_archive(member: str, binary: bytes, *, symbolic: bool = False) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        metadata = tarfile.TarInfo(member)
        metadata.mode = 0o755
        if symbolic:
            metadata.type = tarfile.SYMTYPE
            metadata.linkname = "elsewhere"
        else:
            metadata.size = len(binary)
        archive.addfile(metadata, None if symbolic else io.BytesIO(binary))
    return output.getvalue()


def _zip_archive(member: str, binary: bytes, *, symbolic: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w") as archive:
        info = zipfile.ZipInfo(member)
        if symbolic:
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, binary)
    return output.getvalue()


def _asset(
    archive: bytes,
    binary: bytes,
    *,
    platform: str = "linux",
    member: str = "gh/bin/gh",
    executable: str = "gh",
    kind: Literal["tar.gz", "zip"] = "tar.gz",
) -> GitHubCliAsset:
    return GitHubCliAsset(
        platform=platform,
        architecture="amd64",
        archive_name=f"gh-test.{kind}",
        archive_kind=kind,
        archive_sha256=hashlib.sha256(archive).hexdigest(),
        binary_member=member,
        binary_sha256=hashlib.sha256(binary).hexdigest(),
        executable_name=executable,
    )


def test_platform_mapping_is_explicit() -> None:
    assert github_cli_asset(sys_platform="linux", machine="x86_64").archive_name == (
        "gh_2.100.0_linux_amd64.tar.gz"
    )
    assert github_cli_asset(sys_platform="linux", machine="aarch64").archive_name == (
        "gh_2.100.0_linux_arm64.tar.gz"
    )
    assert github_cli_asset(sys_platform="win32", machine="AMD64").archive_name == (
        "gh_2.100.0_windows_amd64.zip"
    )
    with pytest.raises(GitHubCliError, match="platform"):
        github_cli_asset(sys_platform="darwin", machine="arm64")
    with pytest.raises(GitHubCliError, match="architecture"):
        github_cli_asset(sys_platform="linux", machine="riscv64")


def test_dry_run_plan_does_not_create_the_tool_directory(tmp_path: Path) -> None:
    plan = managed_github_cli_plan(tmp_path, sys_platform="linux", machine="x86_64")

    assert plan.to_dict() == {
        "version": GITHUB_CLI_VERSION,
        "command": str(tmp_path / "tools/github-cli" / GITHUB_CLI_VERSION / "bin/gh"),
        "platform": "linux",
        "architecture": "amd64",
        "archive_sha256": github_cli_asset(sys_platform="linux", machine="x86_64").archive_sha256,
        "binary_sha256": github_cli_asset(sys_platform="linux", machine="x86_64").binary_sha256,
        "source_url": github_cli_asset(sys_platform="linux", machine="x86_64").url,
        "installed": False,
        "downloaded": False,
    }
    assert not (tmp_path / "tools").exists()


def test_managed_cli_is_installed_reused_and_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = b"official gh binary"
    member = "gh/bin/gh"
    archive = _tar_archive(member, binary)
    asset = _asset(archive, binary, member=member)
    monkeypatch.setitem(github_cli._ASSETS, ("linux", "amd64"), asset)
    transport = FakeTransport(archive)
    runner = FakeRunner()
    environment = {
        "GH_TOKEN": "secret",
        "GITHUB_TOKEN": "secret",
        "GITHUB_REPOSITORY": "private/repository",
        "PATH": "/usr/bin",
    }

    installed = ensure_managed_github_cli(
        tmp_path,
        runner=runner,
        transport=transport,
        sys_platform="linux",
        machine="x86_64",
        environ=environment,
    )
    reused = ensure_managed_github_cli(
        tmp_path,
        runner=runner,
        transport=transport,
        sys_platform="linux",
        machine="x86_64",
        environ=environment,
    )

    assert installed.downloaded is True
    assert reused.downloaded is False
    assert installed.command.read_bytes() == binary
    if os.name != "nt":
        assert stat.S_IMODE(installed.command.stat().st_mode) == 0o700
    assert len(transport.calls) == 1
    assert [call[0][1:] for call in runner.calls] == [
        ["--version"],
        ["attestation", "verify", "--help"],
        ["--version"],
        ["attestation", "verify", "--help"],
    ]
    metadata = json.loads((installed.command.parent.parent / "installation.json").read_text())
    assert metadata["version"] == GITHUB_CLI_VERSION
    assert metadata["binary_sha256"] == asset.binary_sha256


def test_windows_zip_extracts_only_the_expected_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = b"windows gh executable"
    archive = _zip_archive("bin/gh.exe", binary)
    asset = _asset(
        archive,
        binary,
        platform="windows",
        member="bin/gh.exe",
        executable="gh.exe",
        kind="zip",
    )
    monkeypatch.setitem(github_cli._ASSETS, ("windows", "amd64"), asset)

    result = ensure_managed_github_cli(
        tmp_path,
        runner=FakeRunner(),
        transport=FakeTransport(archive),
        sys_platform="win32",
        machine="AMD64",
        environ={},
    )

    assert result.command.name == "gh.exe"
    assert result.command.read_bytes() == binary


@pytest.mark.parametrize("kind", ["tar.gz", "zip"])
def test_archive_rejects_symbolic_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Literal["tar.gz", "zip"]
) -> None:
    member = "gh/bin/gh" if kind == "tar.gz" else "bin/gh.exe"
    archive = (
        _tar_archive(member, b"elsewhere", symbolic=True)
        if kind == "tar.gz"
        else _zip_archive(member, b"elsewhere", symbolic=True)
    )
    asset = _asset(
        archive,
        b"elsewhere",
        platform="linux" if kind == "tar.gz" else "windows",
        member=member,
        executable="gh" if kind == "tar.gz" else "gh.exe",
        kind=kind,
    )
    platform_key = "linux" if kind == "tar.gz" else "windows"
    sys_platform = "linux" if kind == "tar.gz" else "win32"
    monkeypatch.setitem(github_cli._ASSETS, (platform_key, "amd64"), asset)

    with pytest.raises(GitHubCliError, match="invalid executable"):
        ensure_managed_github_cli(
            tmp_path,
            runner=FakeRunner(),
            transport=FakeTransport(archive),
            sys_platform=sys_platform,
            machine="amd64",
            environ={},
        )


def test_archive_digest_mismatch_leaves_no_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = b"expected"
    archive = _tar_archive("gh/bin/gh", binary)
    asset = replace(_asset(archive, binary), archive_sha256="0" * 64)
    monkeypatch.setitem(github_cli._ASSETS, ("linux", "amd64"), asset)

    with pytest.raises(GitHubCliError, match="archive digest mismatch"):
        ensure_managed_github_cli(
            tmp_path,
            runner=FakeRunner(),
            transport=FakeTransport(archive),
            sys_platform="linux",
            machine="amd64",
            environ={},
        )

    assert not (tmp_path / "tools" / "github-cli" / GITHUB_CLI_VERSION / "bin" / "gh").exists()


def test_download_rejects_redirects_outside_github(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"Location": "https://example.test/gh.tar.gz"})
        return httpx.Response(200, content=b"untrusted")

    destination = tmp_path / "gh.tar.gz"
    transport = PublicGitHubCliTransport(transport=httpx.MockTransport(handle))

    with pytest.raises(GitHubCliError, match="trusted GitHub hosts"):
        transport.download(
            "https://github.com/cli/cli/releases/download/v1/gh.tar.gz",
            destination,
            timeout=30,
        )
    assert not destination.exists()


def test_download_requires_https_even_for_an_allowed_host(tmp_path: Path) -> None:
    requested = False

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, content=b"untrusted")

    transport = PublicGitHubCliTransport(transport=httpx.MockTransport(handle))
    with pytest.raises(GitHubCliError, match="trusted GitHub hosts"):
        transport.download(
            "http://github.com/cli/cli/releases/gh.tar.gz",
            tmp_path / "gh.tar.gz",
            timeout=30,
        )
    assert requested is False


def test_github_environment_removes_all_authentication(tmp_path: Path) -> None:
    environment = github_cli_environment(
        tmp_path,
        environ={
            "GH_ENTERPRISE_TOKEN": "secret",
            "GH_HOST": "private.example",
            "GH_REPO": "private/repository",
            "GH_TOKEN": "secret",
            "GITHUB_ENTERPRISE_TOKEN": "secret",
            "GITHUB_REPOSITORY": "private/repository",
            "GITHUB_TOKEN": "secret",
            "PATH": "/usr/bin",
        },
    )

    assert environment["PATH"] == "/usr/bin"
    assert environment["GH_PROMPT_DISABLED"] == "1"
    assert environment["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert Path(environment["GH_CONFIG_DIR"]).is_dir()
    assert not github_cli._AUTH_ENVIRONMENT_KEYS.intersection(environment)
