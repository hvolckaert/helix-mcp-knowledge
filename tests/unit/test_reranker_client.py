from __future__ import annotations

import json

import httpx
import pytest

from helix_mcp_knowledge.errors import RerankerBackendError
from helix_mcp_knowledge.reranker_client import (
    RERANKER_COMPONENT_VERSION,
    RERANKER_MAX_CANDIDATES,
    RERANKER_MAX_REQUEST_BYTES,
    RERANKER_MODEL_REVISION,
    RERANKER_PROTOCOL_VERSION,
    RerankerServiceClient,
)
from helix_mcp_knowledge.retrieval.reranker import RerankCandidate


class FakeClient:
    get_response: httpx.Response
    post_response: httpx.Response
    request_json: dict[str, object] | None = None
    get_count = 0
    post_count = 0

    def __init__(self, **kwargs) -> None:
        assert kwargs == {"trust_env": False}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, url, **kwargs):
        type(self).get_count += 1
        return self.get_response

    def post(self, url, **kwargs):
        type(self).post_count += 1
        type(self).request_json = json.loads(kwargs["content"])
        return self.post_response


def _response(status: int, payload: object) -> httpx.Response:
    request = httpx.Request("GET", "http://127.0.0.1:8768/health")
    return httpx.Response(status, content=json.dumps(payload).encode(), request=request)


def _candidate(identifier: str = "chunk-1") -> RerankCandidate:
    return RerankCandidate(
        chunk_id=identifier,
        title="Reconciliation",
        heading_path=("Data integrity",),
        text="Duplicate configuration items are merged.",
    )


def test_client_rejects_non_loopback_service() -> None:
    with pytest.raises(ValueError, match="loopback"):
        RerankerServiceClient(base_url="https://example.com", token="secret")


def test_health_requires_exact_protocol_and_model_revision(monkeypatch) -> None:
    monkeypatch.setattr("helix_mcp_knowledge.reranker_client.httpx.Client", FakeClient)
    client = RerankerServiceClient(base_url="http://127.0.0.1:8768", token="secret")
    FakeClient.get_response = _response(
        200,
        {
            "status": "ready",
            "protocol_version": RERANKER_PROTOCOL_VERSION,
            "component_version": RERANKER_COMPONENT_VERSION,
            "model_revision": RERANKER_MODEL_REVISION,
        },
    )
    assert client.health() is True

    FakeClient.get_response = _response(
        200,
        {
            "status": "ready",
            "protocol_version": RERANKER_PROTOCOL_VERSION,
            "component_version": RERANKER_COMPONENT_VERSION,
            "model_revision": "stale",
        },
    )
    assert client.health() is False
    assert client.health(require_current=False) is True


def test_rerank_uses_bounded_payload_and_validates_complete_response(monkeypatch) -> None:
    monkeypatch.setattr("helix_mcp_knowledge.reranker_client.httpx.Client", FakeClient)
    FakeClient.get_response = _response(
        200,
        {
            "status": "ready",
            "protocol_version": RERANKER_PROTOCOL_VERSION,
            "component_version": RERANKER_COMPONENT_VERSION,
            "model_revision": RERANKER_MODEL_REVISION,
        },
    )
    FakeClient.post_response = _response(
        200,
        {"scores": [{"chunk_id": "chunk-1", "score": 3.5}]},
    )
    client = RerankerServiceClient(base_url="http://127.0.0.1:8768", token="secret")

    scores = client.rerank(query="q" * 10_000, candidates=[_candidate()])

    assert [(score.chunk_id, score.score) for score in scores] == [("chunk-1", 3.5)]
    assert FakeClient.request_json is not None
    assert len(str(FakeClient.request_json["query"])) == 4_096


def test_rerank_rejects_over_limit_and_partial_results(monkeypatch) -> None:
    client = RerankerServiceClient(base_url="http://127.0.0.1:8768", token="secret")
    with pytest.raises(RerankerBackendError, match="candidate count"):
        client.rerank(
            query="query",
            candidates=[
                _candidate(f"chunk-{number}") for number in range(RERANKER_MAX_CANDIDATES + 1)
            ],
        )

    monkeypatch.setattr("helix_mcp_knowledge.reranker_client.httpx.Client", FakeClient)
    FakeClient.get_response = _response(
        200,
        {
            "status": "ready",
            "protocol_version": RERANKER_PROTOCOL_VERSION,
            "component_version": RERANKER_COMPONENT_VERSION,
            "model_revision": RERANKER_MODEL_REVISION,
        },
    )
    FakeClient.post_response = _response(200, {"scores": []})
    with pytest.raises(RerankerBackendError, match="incomplete"):
        client.rerank(query="query", candidates=[_candidate()])


def test_rerank_rejects_request_over_shared_byte_budget_before_post(monkeypatch) -> None:
    monkeypatch.setattr("helix_mcp_knowledge.reranker_client.httpx.Client", FakeClient)
    FakeClient.get_response = _response(
        200,
        {
            "status": "ready",
            "protocol_version": RERANKER_PROTOCOL_VERSION,
            "component_version": RERANKER_COMPONENT_VERSION,
            "model_revision": RERANKER_MODEL_REVISION,
        },
    )
    FakeClient.post_count = 0
    client = RerankerServiceClient(base_url="http://127.0.0.1:8768", token="secret")
    candidates = [
        RerankCandidate(
            chunk_id=f"chunk-{number}",
            title="title",
            heading_path=("h" * 2_048,) * 16,
            text="x" * 16_384,
        )
        for number in range(RERANKER_MAX_CANDIDATES)
    ]

    with pytest.raises(
        RerankerBackendError,
        match=rf"exceeds {RERANKER_MAX_REQUEST_BYTES} bytes",
    ):
        client.rerank(query="query", candidates=candidates)

    assert FakeClient.post_count == 0


def test_cold_service_opens_short_circuit_and_recovers(monkeypatch) -> None:
    monkeypatch.setattr("helix_mcp_knowledge.reranker_client.httpx.Client", FakeClient)
    clock = [100.0]
    monkeypatch.setattr("helix_mcp_knowledge.reranker_client.time.monotonic", lambda: clock[0])
    FakeClient.get_count = 0
    FakeClient.post_count = 0
    FakeClient.get_response = _response(503, {"status": "starting"})
    FakeClient.post_response = _response(
        200,
        {"scores": [{"chunk_id": "chunk-1", "score": 3.5}]},
    )
    client = RerankerServiceClient(base_url="http://127.0.0.1:8768", token="secret")

    with pytest.raises(RerankerBackendError, match="not ready"):
        client.rerank(query="query", candidates=[_candidate()])
    with pytest.raises(RerankerBackendError, match="temporarily unavailable"):
        client.rerank(query="query", candidates=[_candidate()])
    assert FakeClient.get_count == 1
    assert FakeClient.post_count == 0

    clock[0] += 2.1
    FakeClient.get_response = _response(
        200,
        {
            "status": "ready",
            "protocol_version": RERANKER_PROTOCOL_VERSION,
            "component_version": RERANKER_COMPONENT_VERSION,
            "model_revision": RERANKER_MODEL_REVISION,
        },
    )
    scores = client.rerank(query="query", candidates=[_candidate()])

    assert [(score.chunk_id, score.score) for score in scores] == [("chunk-1", 3.5)]
    assert FakeClient.get_count == 2
    assert FakeClient.post_count == 1
