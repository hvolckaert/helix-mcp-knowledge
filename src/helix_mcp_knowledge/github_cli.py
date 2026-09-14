"""Managed GitHub CLI provisioning for release attestation verification."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol

import httpx

from .errors import KnowledgeError

GITHUB_CLI_VERSION = "2.100.0"
_GITHUB_CLI_RELEASE_ROOT = "https://github.com/cli/cli/releases/download"
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_BINARY_BYTES = 64 * 1024 * 1024
_GITHUB_HEADERS = {
    "Accept": "application/octet-stream",
    "User-Agent": "helix-mcp-knowledge",
}
_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_AUTH_ENVIRONMENT_KEYS = frozenset(
    {
        "GH_ENTERPRISE_TOKEN",
        "GH_HOST",
        "GH_REPO",
        "GH_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_TOKEN",
    }
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ArchiveKind = Literal["tar.gz", "zip"]


class GitHubCliError(KnowledgeError):
    """Managed GitHub CLI provisioning failed without exposing internals."""

    code = "GITHUB_CLI_INSTALLATION_ERROR"


@dataclass(frozen=True, slots=True)
class GitHubCliAsset:
    """Pinned GitHub CLI release asset for one supported platform."""

    platform: str
    architecture: str
    archive_name: str
    archive_kind: ArchiveKind
    archive_sha256: str
    binary_member: str
    binary_sha256: str
    executable_name: str

    @property
    def url(self) -> str:
        return f"{_GITHUB_CLI_RELEASE_ROOT}/v{GITHUB_CLI_VERSION}/{self.archive_name}"


@dataclass(frozen=True, slots=True)
class ManagedGitHubCli:
    """Safe metadata for the selected managed GitHub CLI executable."""

    version: str
    command: Path
    platform: str
    architecture: str
    archive_sha256: str
    binary_sha256: str
    source_url: str
    installed: bool
    downloaded: bool

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["command"] = str(self.command)
        return payload


class GitHubCliTransport(Protocol):
    def download(self, url: str, destination: Path, *, timeout: int) -> None: ...


class _BinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class PublicGitHubCliTransport:
    """Download official GitHub CLI assets without local credentials."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def download(self, url: str, destination: Path, *, timeout: int) -> None:
        try:
            with httpx.Client(
                headers=_GITHUB_HEADERS,
                follow_redirects=False,
                timeout=timeout,
                transport=self._transport,
            ) as client:
                current_url = httpx.URL(url)
                for _ in range(10):
                    host = (current_url.host or "").casefold()
                    if current_url.scheme != "https" or host not in _ALLOWED_DOWNLOAD_HOSTS:
                        raise GitHubCliError("GitHub CLI download left trusted GitHub hosts")
                    with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            location = response.headers.get("Location")
                            if not location:
                                raise GitHubCliError("GitHub CLI download redirect is invalid")
                            current_url = current_url.join(location)
                            continue
                        response.raise_for_status()
                        _write_download(response, destination)
                        return
                raise GitHubCliError("GitHub CLI download has too many redirects")
        except GitHubCliError:
            destination.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, ValueError):
            destination.unlink(missing_ok=True)
            raise GitHubCliError("GitHub CLI archive could not be downloaded") from None


_ASSETS = {
    ("linux", "amd64"): GitHubCliAsset(
        platform="linux",
        architecture="amd64",
        archive_name="gh_2.100.0_linux_amd64.tar.gz",
        archive_kind="tar.gz",
        archive_sha256="e4d4bb4498e8d007abe545b6568926793ace1b6447da598294a610018cb164be",
        binary_member="gh_2.100.0_linux_amd64/bin/gh",
        binary_sha256="553949e2efa12842771efe6012aa4de21f1d591530ec17fc435f610f10e017ee",
        executable_name="gh",
    ),
    ("linux", "arm64"): GitHubCliAsset(
        platform="linux",
        architecture="arm64",
        archive_name="gh_2.100.0_linux_arm64.tar.gz",
        archive_kind="tar.gz",
        archive_sha256="ea4e7a581a32ccad6cc7923cb1576ac5859ba4b9a16ab22eb8f8a96e78e2e961",
        binary_member="gh_2.100.0_linux_arm64/bin/gh",
        binary_sha256="28a037b967065aa314cb6d539943b55d27ef2f97c523ab2b6023ccf284e1828d",
        executable_name="gh",
    ),
    ("windows", "amd64"): GitHubCliAsset(
        platform="windows",
        architecture="amd64",
        archive_name="gh_2.100.0_windows_amd64.zip",
        archive_kind="zip",
        archive_sha256="227e35230b25db3fa1b997bab7cf4d67df0470a3b75b99e4ee66bce1a7cd4e72",
        binary_member="bin/gh.exe",
        binary_sha256="2ae2b350c227a618f2d8965b1900aeee13446ff42e17ef0bd5a0b6405c593cfb",
        executable_name="gh.exe",
    ),
}


def github_cli_asset(
    *, sys_platform: str | None = None, machine: str | None = None
) -> GitHubCliAsset:
    """Return the pinned asset for one explicitly supported platform."""

    selected_platform = (sys_platform or sys.platform).casefold()
    if selected_platform.startswith("linux"):
        operating_system = "linux"
    elif selected_platform in {"win32", "cygwin"}:
        operating_system = "windows"
    else:
        raise GitHubCliError("managed GitHub CLI is unavailable for this platform")
    architecture = {
        "aarch64": "arm64",
        "amd64": "amd64",
        "arm64": "arm64",
        "x86_64": "amd64",
    }.get((machine or platform.machine()).casefold())
    if architecture is None:
        raise GitHubCliError("managed GitHub CLI is unavailable for this architecture")
    asset = _ASSETS.get((operating_system, architecture))
    if asset is None:
        raise GitHubCliError("managed GitHub CLI is unavailable for this platform")
    return asset


def managed_github_cli_path(workspace: str | Path, *, asset: GitHubCliAsset | None = None) -> Path:
    selected = asset or github_cli_asset()
    return (
        Path(workspace).expanduser().absolute()
        / "tools"
        / "github-cli"
        / GITHUB_CLI_VERSION
        / "bin"
        / selected.executable_name
    )


def managed_github_cli_plan(
    workspace: str | Path,
    *,
    sys_platform: str | None = None,
    machine: str | None = None,
) -> ManagedGitHubCli:
    """Describe provisioning without downloads or filesystem writes."""

    asset = github_cli_asset(sys_platform=sys_platform, machine=machine)
    command = managed_github_cli_path(workspace, asset=asset)
    return _result(
        asset,
        command,
        installed=_is_expected_binary(command, asset),
        downloaded=False,
    )


def ensure_managed_github_cli(
    workspace: str | Path,
    *,
    runner: CommandRunner = subprocess.run,
    transport: GitHubCliTransport | None = None,
    sys_platform: str | None = None,
    machine: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ManagedGitHubCli:
    """Install and validate the pinned GitHub CLI in the managed workspace."""

    asset = github_cli_asset(sys_platform=sys_platform, machine=machine)
    resolved_workspace = Path(workspace).expanduser().absolute()
    command = managed_github_cli_path(resolved_workspace, asset=asset)
    if command.is_symlink():
        raise GitHubCliError("managed GitHub CLI cannot be a symbolic link")
    if _is_expected_binary(command, asset):
        _validate_command(
            command,
            runner=runner,
            environment=github_cli_environment(resolved_workspace, environ=environ),
        )
        return _result(asset, command, installed=True, downloaded=False)

    version_root = command.parent.parent
    for directory in (
        resolved_workspace,
        resolved_workspace / "tools",
        resolved_workspace / "tools" / "github-cli",
        version_root,
        command.parent,
    ):
        _ensure_directory(directory)
    selected_transport = transport or PublicGitHubCliTransport()
    with tempfile.TemporaryDirectory(prefix=".install-", dir=version_root) as temporary:
        temporary_root = Path(temporary)
        archive = temporary_root / asset.archive_name
        selected_transport.download(asset.url, archive, timeout=300)
        _verify_sha256(archive, asset.archive_sha256, label="archive")
        extracted = temporary_root / asset.executable_name
        _extract_binary(archive, extracted, asset)
        _verify_sha256(extracted, asset.binary_sha256, label="binary")
        if os.name != "nt":
            extracted.chmod(0o700)
        _validate_command(
            extracted,
            runner=runner,
            environment=github_cli_environment(resolved_workspace, environ=environ),
        )
        try:
            os.replace(extracted, command)
        except OSError:
            raise GitHubCliError("managed GitHub CLI could not be installed") from None
    _write_metadata(version_root / "installation.json", asset, command)
    return _result(asset, command, installed=True, downloaded=True)


def github_cli_environment(
    workspace: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an isolated non-interactive environment without GitHub auth."""

    environment = dict(os.environ if environ is None else environ)
    for key in _AUTH_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    resolved_workspace = Path(workspace).expanduser().absolute()
    config_dir = resolved_workspace / "tools" / "github-cli" / "config"
    for directory in (
        resolved_workspace,
        resolved_workspace / "tools",
        resolved_workspace / "tools" / "github-cli",
        config_dir,
    ):
        _ensure_directory(directory)
    environment.update(
        {
            "GH_CONFIG_DIR": str(config_dir),
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_PROMPT_DISABLED": "1",
        }
    )
    return environment


def _result(
    asset: GitHubCliAsset,
    command: Path,
    *,
    installed: bool,
    downloaded: bool,
) -> ManagedGitHubCli:
    return ManagedGitHubCli(
        version=GITHUB_CLI_VERSION,
        command=command,
        platform=asset.platform,
        architecture=asset.architecture,
        archive_sha256=asset.archive_sha256,
        binary_sha256=asset.binary_sha256,
        source_url=asset.url,
        installed=installed,
        downloaded=downloaded,
    )


def _ensure_directory(path: Path) -> None:
    try:
        if path.exists():
            if path.is_symlink() or not path.is_dir():
                raise GitHubCliError("managed GitHub CLI directory is not safe")
        else:
            path.mkdir(mode=0o700, parents=True)
        if os.name != "nt":
            path.chmod(0o700)
    except GitHubCliError:
        raise
    except OSError:
        raise GitHubCliError("managed GitHub CLI directory could not be prepared") from None


def _is_expected_binary(path: Path, asset: GitHubCliAsset) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        _verify_sha256(path, asset.binary_sha256, label="binary")
    except GitHubCliError:
        return False
    return True


def _verify_sha256(path: Path, expected: str, *, label: str) -> None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        raise GitHubCliError(f"GitHub CLI {label} could not be read") from None
    if digest.hexdigest() != expected:
        raise GitHubCliError(f"GitHub CLI {label} digest mismatch")


def _extract_binary(archive: Path, destination: Path, asset: GitHubCliAsset) -> None:
    try:
        if asset.archive_kind == "tar.gz":
            with tarfile.open(archive, mode="r:gz") as bundle:
                members = [item for item in bundle.getmembers() if item.name == asset.binary_member]
                if (
                    len(members) != 1
                    or not members[0].isfile()
                    or members[0].size > _MAX_BINARY_BYTES
                ):
                    raise GitHubCliError("GitHub CLI archive has an invalid executable")
                source = bundle.extractfile(members[0])
                if source is None:
                    raise GitHubCliError("GitHub CLI archive has an invalid executable")
                with source:
                    _copy_binary(source, destination)
        else:
            with zipfile.ZipFile(archive) as bundle:
                members = [
                    item for item in bundle.infolist() if item.filename == asset.binary_member
                ]
                mode = members[0].external_attr >> 16 if len(members) == 1 else 0
                if (
                    len(members) != 1
                    or members[0].is_dir()
                    or members[0].file_size > _MAX_BINARY_BYTES
                    or stat.S_IFMT(mode) == stat.S_IFLNK
                ):
                    raise GitHubCliError("GitHub CLI archive has an invalid executable")
                with bundle.open(members[0]) as source:
                    _copy_binary(source, destination)
    except GitHubCliError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        destination.unlink(missing_ok=True)
        raise GitHubCliError("GitHub CLI archive could not be extracted") from None


def _write_download(response: httpx.Response, destination: Path) -> None:
    content_length = response.headers.get("Content-Length")
    if content_length is not None and int(content_length) > _MAX_ARCHIVE_BYTES:
        raise GitHubCliError("GitHub CLI archive exceeds the size limit")
    written = 0
    with destination.open("xb") as stream:
        for block in response.iter_bytes():
            written += len(block)
            if written > _MAX_ARCHIVE_BYTES:
                raise GitHubCliError("GitHub CLI archive exceeds the size limit")
            stream.write(block)


def _copy_binary(source: _BinaryReader, destination: Path) -> None:
    try:
        with destination.open("xb") as output:
            copied = 0
            while block := source.read(1024 * 1024):
                copied += len(block)
                if copied > _MAX_BINARY_BYTES:
                    raise GitHubCliError("GitHub CLI executable exceeds the size limit")
                output.write(block)
    except GitHubCliError:
        destination.unlink(missing_ok=True)
        raise
    except OSError:
        destination.unlink(missing_ok=True)
        raise GitHubCliError("GitHub CLI executable could not be extracted") from None


def _validate_command(
    command: Path,
    *,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> None:
    version = _run([str(command), "--version"], runner=runner, environment=environment)
    first_line = version.stdout.splitlines()[0] if version.stdout else ""
    if not first_line.startswith(f"gh version {GITHUB_CLI_VERSION}"):
        raise GitHubCliError("managed GitHub CLI version is invalid")
    _run(
        [str(command), "attestation", "verify", "--help"],
        runner=runner,
        environment=environment,
    )


def _run(
    command: list[str],
    *,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=dict(environment),
        )
    except (OSError, subprocess.SubprocessError):
        raise GitHubCliError("managed GitHub CLI could not be executed") from None
    if completed.returncode != 0:
        raise GitHubCliError("managed GitHub CLI validation failed")
    return completed


def _write_metadata(path: Path, asset: GitHubCliAsset, command: Path) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "schema_version": 1,
        "version": GITHUB_CLI_VERSION,
        "command": str(command),
        "platform": asset.platform,
        "architecture": asset.architecture,
        "source_url": asset.url,
        "archive_sha256": asset.archive_sha256,
        "binary_sha256": asset.binary_sha256,
    }
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError:
        raise GitHubCliError("managed GitHub CLI metadata could not be installed") from None
    finally:
        temporary.unlink(missing_ok=True)
