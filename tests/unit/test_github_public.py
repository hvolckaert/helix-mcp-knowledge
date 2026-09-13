from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from helix_mcp_knowledge.github_public import (
    PublicGitHubTransport,
    verify_public_attestation,
)


def test_public_transport_never_forwards_github_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "authorization" not in request.headers
        if request.url.path.endswith("/latest"):
            return httpx.Response(200, json={"tag_name": "v1.2.3"})
        return httpx.Response(200, content=b"public asset")

    transport = PublicGitHubTransport(transport=httpx.MockTransport(handle))
    payload = transport.get_json(
        "https://api.github.com/repos/example/public/releases/latest",
        timeout=30,
        action="test metadata",
    )
    destination = tmp_path / "asset.whl"
    transport.download(
        "https://github.com/example/public/releases/download/v1.2.3/asset.whl",
        destination,
        timeout=30,
        action="test download",
    )

    assert payload == {"tag_name": "v1.2.3"}
    assert destination.read_bytes() == b"public asset"
    assert len(requests) == 2


def test_attestation_verification_uses_public_bundle_and_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    artifact = tmp_path / "asset.whl"
    artifact.write_bytes(b"artifact")
    urls: list[str] = []
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Transport:
        def get_json(self, url: str, *, timeout: int, action: str) -> object:
            del timeout, action
            urls.append(url)
            return {"attestations": [{"bundle": {"mediaType": "test-bundle"}}]}

        def download(self, *args, **kwargs) -> None:
            raise AssertionError("verification should not download a release asset")

    def runner(command, **kwargs):
        calls.append(([str(item) for item in command], kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    verify_public_attestation(
        tmp_path / "gh",
        artifact,
        repository="example/public",
        sha256="a" * 64,
        release_tag="v1.2.3",
        signer_workflow=".github/workflows/release.yml",
        runner=runner,
        transport=Transport(),
    )

    assert urls == [f"https://api.github.com/repos/example/public/attestations/sha256:{'a' * 64}"]
    command, options = calls[0]
    assert command[1:3] == ["attestation", "verify"]
    assert command[command.index("--source-ref") + 1] == "refs/tags/v1.2.3"
    assert command[command.index("--signer-workflow") + 1] == (
        "example/public/.github/workflows/release.yml"
    )
    assert "--bundle" in command
    assert "--deny-self-hosted-runners" in command
    environment = options["env"]
    assert isinstance(environment, dict)
    assert "GH_TOKEN" not in environment
    assert "GITHUB_TOKEN" not in environment
