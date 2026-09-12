from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import httpx
import pytest

from helix_mcp_knowledge import __version__
from helix_mcp_knowledge.reranker_client import (
    RERANKER_MAX_REQUEST_BYTES,
    RerankerServiceClient,
)
from helix_mcp_knowledge.reranker_worker import (
    RERANKER_MAX_REQUEST_BYTES as WORKER_MAX_REQUEST_BYTES,
)
from helix_mcp_knowledge.reranker_worker import (
    RerankerHTTPServer,
    main,
    managed_runtime_is_active,
    service_start_is_current,
)
from helix_mcp_knowledge.retrieval.reranker import RerankCandidate, RerankScore


class RecordingRuntime:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def rerank(self, *, query, candidates):
        self.queries.append(query)
        return [
            RerankScore(chunk_id=candidate.chunk_id, score=float(len(candidates) - index))
            for index, candidate in enumerate(candidates)
        ]


def _candidate(identifier: str) -> RerankCandidate:
    return RerankCandidate(
        chunk_id=identifier,
        title="CMDB",
        heading_path=("Normalization",),
        text="Official documentation.",
    )


def _server(runtime: RecordingRuntime) -> RerankerHTTPServer:
    try:
        return RerankerHTTPServer(("127.0.0.1", 0), runtime=runtime, token="secret")
    except PermissionError:
        pytest.skip("loopback sockets are unavailable in this sandbox")


def test_loopback_service_requires_bearer_token_and_reranks() -> None:
    runtime = RecordingRuntime()
    server = _server(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(trust_env=False) as raw_client:
            assert raw_client.get(f"{base_url}/health", timeout=2).status_code == 401
        client = RerankerServiceClient(base_url=base_url, token="secret", timeout_seconds=2)
        assert client.health() is True

        scores = client.rerank(
            query="normalization reconciliation",
            candidates=[_candidate("one"), _candidate("two")],
        )

        assert [(item.chunk_id, item.score) for item in scores] == [("one", 2.0), ("two", 1.0)]
        assert runtime.queries == ["normalization reconciliation"]
        client.shutdown()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_service_refuses_non_loopback_bind_before_opening_socket() -> None:
    with pytest.raises(ValueError, match="loopback"):
        RerankerHTTPServer(("0.0.0.0", 0), runtime=RecordingRuntime(), token="secret")


def test_client_and_worker_share_the_same_request_byte_budget() -> None:
    assert WORKER_MAX_REQUEST_BYTES == RERANKER_MAX_REQUEST_BYTES


def test_service_rejects_unbounded_candidate_text() -> None:
    server = _server(RecordingRuntime())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                f"http://127.0.0.1:{port}/rerank",
                headers={"Authorization": "Bearer secret"},
                json={
                    "query": "query",
                    "candidates": [
                        {
                            "chunk_id": "one",
                            "title": "title",
                            "heading_path": [],
                            "text": "x" * 20_000,
                        }
                    ],
                },
                timeout=2,
            )
        assert response.status_code == 400
        assert "at most" in response.json()["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_service_generation_gate_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "service-control.json"
    stop = tmp_path / "service.stop"
    generation = "a" * 32
    control.write_text(
        json.dumps({"schema_version": 1, "generation": generation, "desired": True}),
        encoding="utf-8",
    )

    assert service_start_is_current(
        control_path=control,
        stop_path=stop,
        generation=generation,
    )

    for payload in (
        "not-json",
        json.dumps({"schema_version": 1, "generation": "b" * 32, "desired": True}),
        json.dumps({"schema_version": 1, "generation": generation, "desired": False}),
    ):
        control.write_text(payload, encoding="utf-8")
        assert not service_start_is_current(
            control_path=control,
            stop_path=stop,
            generation=generation,
        )

    control.write_text(
        json.dumps({"schema_version": 1, "generation": generation, "desired": True}),
        encoding="utf-8",
    )
    stop.write_text("stop", encoding="utf-8")
    assert not service_start_is_current(
        control_path=control,
        stop_path=stop,
        generation=generation,
    )


def test_worker_cancelled_during_model_load_never_binds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from helix_mcp_knowledge import reranker_worker

    model = tmp_path / "model"
    model.mkdir()
    control = tmp_path / "service-control.json"
    stop = tmp_path / "service.stop"
    generation = "c" * 32
    control.write_text(
        json.dumps({"schema_version": 1, "generation": generation, "desired": True}),
        encoding="utf-8",
    )

    class CancellingRuntime:
        def __init__(self, **_kwargs) -> None:
            stop.write_text("stop", encoding="utf-8")

    def unexpected_server(*_args, **_kwargs):
        raise AssertionError("a cancelled worker must not bind")

    monkeypatch.setattr(reranker_worker, "RerankerRuntime", CancellingRuntime)
    monkeypatch.setattr(reranker_worker, "RerankerHTTPServer", unexpected_server)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reranker-worker",
            "--model-path",
            str(model),
            "--token",
            "secret",
            "--control-path",
            str(control),
            "--stop-path",
            str(stop),
            "--generation",
            generation,
        ],
    )

    assert main() == 0


def test_managed_runtime_gate_tracks_exact_active_version(tmp_path: Path) -> None:
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


def test_managed_worker_exits_when_candidate_activation_is_rolled_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from helix_mcp_knowledge import reranker_worker

    model = tmp_path / "model"
    model.mkdir()
    control = tmp_path / "service-control.json"
    stop = tmp_path / "service.stop"
    installation = tmp_path / "installation.json"
    generation = "d" * 32
    control.write_text(
        json.dumps({"schema_version": 1, "generation": generation, "desired": True}),
        encoding="utf-8",
    )
    installation.write_text(
        json.dumps({"schema_version": 2, "active_version": __version__}),
        encoding="utf-8",
    )
    server_created = threading.Event()
    serve_started = threading.Event()
    server_stopped = threading.Event()

    class Runtime:
        def __init__(self, **_kwargs) -> None:
            pass

    class Server:
        def __init__(self, *_args, **_kwargs) -> None:
            server_created.set()

        def serve_forever(self) -> None:
            serve_started.set()
            assert server_stopped.wait(timeout=3)

        def shutdown(self) -> None:
            server_stopped.set()

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(reranker_worker, "RerankerRuntime", Runtime)
    monkeypatch.setattr(reranker_worker, "RerankerHTTPServer", Server)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reranker-worker",
            "--model-path",
            str(model),
            "--token",
            "secret",
            "--control-path",
            str(control),
            "--stop-path",
            str(stop),
            "--generation",
            generation,
            "--managed-installation-path",
            str(installation),
            "--managed-version",
            __version__,
        ],
    )
    worker = threading.Thread(target=main)
    worker.start()
    assert server_created.wait(timeout=1)
    assert serve_started.wait(timeout=1)

    installation.write_text(
        json.dumps({"schema_version": 2, "active_version": "1.24.3"}),
        encoding="utf-8",
    )

    worker.join(timeout=3)
    assert not worker.is_alive()
    assert server_stopped.is_set()
