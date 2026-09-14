"""Anonymous access to public GitHub release resources and local attestation verification."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx

from .errors import KnowledgeError
from .github_cli import github_cli_environment
from .openclaw import _run

GITHUB_API_ROOT = "https://api.github.com"
GITHUB_RELEASE_ROOT = "https://github.com"
GITHUB_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "helix-mcp-knowledge",
}
TRUSTED_GITHUB_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
MAX_GITHUB_JSON_BYTES = 16 * 1024 * 1024
MAX_RELEASE_ASSET_BYTES = 256 * 1024 * 1024
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class ReleaseTransport(Protocol):
    """Transport for public GitHub resources that never accepts credentials."""

    def get_json(self, url: str, *, timeout: int, action: str) -> object: ...

    def download(
        self,
        url: str,
        destination: Path,
        *,
        timeout: int,
        action: str,
    ) -> None: ...


class PublicGitHubTransport:
    """Fetch public GitHub resources without reading GitHub token variables."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def get_json(self, url: str, *, timeout: int, action: str) -> object:
        try:
            with httpx.Client(
                headers=GITHUB_API_HEADERS,
                follow_redirects=False,
                timeout=timeout,
                transport=self._transport,
            ) as client:
                current_url = httpx.URL(url)
                for _ in range(10):
                    _validate_github_url(current_url, action=action)
                    response = client.get(current_url)
                    if response.is_redirect:
                        current_url = _redirect_url(response, current_url, action=action)
                        continue
                    response.raise_for_status()
                    if len(response.content) > MAX_GITHUB_JSON_BYTES:
                        raise KnowledgeError(f"{action} returned too much data")
                    return response.json()
                raise KnowledgeError(f"{action} returned too many redirects")
        except KnowledgeError:
            raise
        except (httpx.HTTPError, ValueError):
            raise KnowledgeError(f"{action} could not be completed") from None

    def download(
        self,
        url: str,
        destination: Path,
        *,
        timeout: int,
        action: str,
    ) -> None:
        try:
            with httpx.Client(
                headers=GITHUB_API_HEADERS,
                follow_redirects=False,
                timeout=timeout,
                transport=self._transport,
            ) as client:
                current_url = httpx.URL(url)
                for _ in range(10):
                    _validate_github_url(current_url, action=action)
                    with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            current_url = _redirect_url(response, current_url, action=action)
                            continue
                        response.raise_for_status()
                        content_length = response.headers.get("Content-Length")
                        if (
                            content_length is not None
                            and int(content_length) > MAX_RELEASE_ASSET_BYTES
                        ):
                            raise KnowledgeError(f"{action} returned too much data")
                        written = 0
                        with destination.open("xb") as stream:
                            for block in response.iter_bytes():
                                written += len(block)
                                if written > MAX_RELEASE_ASSET_BYTES:
                                    raise KnowledgeError(f"{action} returned too much data")
                                stream.write(block)
                        return
                raise KnowledgeError(f"{action} returned too many redirects")
        except KnowledgeError:
            destination.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, ValueError):
            destination.unlink(missing_ok=True)
            raise KnowledgeError(f"{action} could not be completed") from None


def _validate_github_url(url: httpx.URL, *, action: str) -> None:
    if url.scheme != "https" or (url.host or "").casefold() not in TRUSTED_GITHUB_HOSTS:
        raise KnowledgeError(f"{action} left trusted GitHub hosts")


def _redirect_url(response: httpx.Response, current: httpx.URL, *, action: str) -> httpx.URL:
    location = response.headers.get("Location")
    if not location:
        raise KnowledgeError(f"{action} returned an invalid redirect")
    redirected = current.join(location)
    _validate_github_url(redirected, action=action)
    return redirected


def verify_public_attestation(
    gh_command: Path,
    artifact: Path,
    *,
    repository: str,
    sha256: str,
    release_tag: str,
    signer_workflow: str,
    runner: CommandRunner,
    transport: ReleaseTransport,
    workspace: str | Path,
    source_ref: str | None = None,
) -> None:
    """Fetch public bundles anonymously and verify them locally with ``gh``."""

    payload = transport.get_json(
        f"{GITHUB_API_ROOT}/repos/{repository}/attestations/sha256:{sha256}",
        timeout=60,
        action="release provenance lookup",
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("attestations"), list):
        raise KnowledgeError("release provenance metadata is invalid")
    bundles = [
        item.get("bundle")
        for item in payload["attestations"]
        if isinstance(item, dict) and isinstance(item.get("bundle"), dict)
    ]
    if not bundles:
        raise KnowledgeError("release asset has no verifiable provenance")
    with tempfile.TemporaryDirectory(prefix=".attestation-", dir=artifact.parent) as temporary:
        bundle_path = Path(temporary) / "bundles.jsonl"
        bundle_path.write_text(
            "".join(f"{json.dumps(bundle, separators=(',', ':'))}\n" for bundle in bundles),
            encoding="utf-8",
        )
        command = [
            str(gh_command),
            "attestation",
            "verify",
            str(artifact),
            "--repo",
            repository,
            "--bundle",
            str(bundle_path),
            "--signer-workflow",
            f"{repository}/{signer_workflow}",
            "--deny-self-hosted-runners",
        ]
        if source_ref is not None:
            command.extend(["--source-ref", source_ref])
        elif release_tag:
            command.extend(["--source-ref", f"refs/tags/{release_tag}"])
        _run(
            command,
            runner=runner,
            timeout=120,
            action="release provenance verification",
            env=github_cli_environment(workspace),
        )
