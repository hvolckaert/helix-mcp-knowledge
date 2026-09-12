import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from helix_mcp_knowledge import __version__
from helix_mcp_knowledge.semantic_client import SemanticServiceClient
from helix_mcp_knowledge.semantic_worker import (
    SemanticHTTPServer,
    managed_runtime_is_active,
    watch_managed_runtime,
)


class FakeEmbedder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]


def test_semantic_service_is_loopback_token_protected_and_encodes() -> None:
    runtime = SimpleNamespace(dimension=2, lock=threading.RLock(), embedder=FakeEmbedder())
    try:
        server = SemanticHTTPServer(("127.0.0.1", 0), runtime=runtime, token="secret")
    except PermissionError:
        pytest.skip("loopback sockets are unavailable in this sandbox")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        unauthorized = httpx.get(f"http://127.0.0.1:{port}/health")
        assert unauthorized.status_code == 401

        client = SemanticServiceClient(
            base_url=f"http://127.0.0.1:{port}",
            token="secret",
            dimension=2,
            timeout_seconds=5,
        )
        assert client.health() is True
        assert client.encode(["abc"]) == [[3.0, 1.0]]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8767",
        "http://localhost:8767",
        "http://example.com:8767",
        "http://user@127.0.0.1:8767",
        "http://127.0.0.1:8767/path",
    ],
)
def test_semantic_client_rejects_non_loopback_origins(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        SemanticServiceClient(
            base_url=url,
            token="secret",
            dimension=2,
            timeout_seconds=5,
        )


def test_semantic_client_ignores_environment_proxies(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"status": "ready"}

    class Client:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        @staticmethod
        def get(*_args, **_kwargs):
            return Response()

    monkeypatch.setattr("helix_mcp_knowledge.semantic_client.httpx.Client", Client)
    client = SemanticServiceClient(
        base_url="http://127.0.0.1:8767",
        token="secret",
        dimension=2,
        timeout_seconds=5,
    )

    assert client.health() is True
    assert calls == [{"trust_env": False}]


def test_semantic_service_refuses_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="loopback"):
        SemanticHTTPServer(
            ("0.0.0.0", 0),
            runtime=SimpleNamespace(),
            token="secret",
        )


def test_semantic_managed_runtime_gate_tracks_exact_active_version(tmp_path: Path) -> None:
    metadata = tmp_path / "installation.json"
    metadata.write_text(
        json.dumps({"schema_version": 2, "active_version": __version__}),
        encoding="utf-8",
    )

    assert managed_runtime_is_active(
        metadata_path=metadata,
        expected_version=__version__,
    )

    metadata.write_text(
        json.dumps({"schema_version": 2, "active_version": "1.24.3"}),
        encoding="utf-8",
    )
    assert not managed_runtime_is_active(
        metadata_path=metadata,
        expected_version=__version__,
    )


def test_semantic_worker_watcher_stops_after_managed_runtime_rollback(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "installation.json"
    metadata.write_text(
        json.dumps({"schema_version": 2, "active_version": __version__}),
        encoding="utf-8",
    )
    stopped = threading.Event()
    shutdown = threading.Event()
    server = SimpleNamespace(shutdown=shutdown.set)
    watcher = threading.Thread(
        target=watch_managed_runtime,
        kwargs={
            "server": server,
            "metadata_path": metadata,
            "expected_version": __version__,
            "stopped": stopped,
            "poll_seconds": 0.01,
        },
    )
    watcher.start()

    metadata.write_text(
        json.dumps({"schema_version": 2, "active_version": "1.24.3"}),
        encoding="utf-8",
    )

    assert shutdown.wait(timeout=1)
    watcher.join(timeout=1)
    assert not watcher.is_alive()
