"""Bounded HTTP client for the optional private reranker service."""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx

from .errors import RerankerBackendError
from .retrieval.reranker import RERANKER_MAX_CANDIDATES, RerankCandidate, RerankScore

RERANKER_PROTOCOL_VERSION = 1
RERANKER_COMPONENT_VERSION = 2
RERANKER_MODEL_ID = "BAAI/bge-reranker-v2-m3"
RERANKER_MODEL_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"  # pragma: allowlist secret
RERANKER_MAX_QUERY_CHARACTERS = 4_096
RERANKER_MAX_TITLE_CHARACTERS = 1_024
RERANKER_MAX_HEADING_CHARACTERS = 2_048
RERANKER_MAX_TEXT_CHARACTERS = 16_384
RERANKER_MAX_REQUEST_BYTES = 1024 * 1024
RERANKER_MAX_RESPONSE_BYTES = 256 * 1024
RERANKER_HEALTH_GATE_TIMEOUT_SECONDS = 0.25
RERANKER_HEALTH_CACHE_SECONDS = 5.0
RERANKER_CIRCUIT_BREAK_SECONDS = 2.0


class RerankerServiceClient:
    """Call a bearer-protected service that is constrained to IPv4 loopback."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("reranker service URL must be an IPv4 loopback HTTP origin")
        if not token:
            raise ValueError("reranker service token cannot be empty")
        try:
            normalized_timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("reranker timeout must be a positive finite number") from exc
        if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
            raise ValueError("reranker timeout must be a positive finite number")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = normalized_timeout
        self._gate_lock = threading.Lock()
        self._ready_until = 0.0
        self._retry_after = 0.0
        self._probe_in_flight = False

    def health(self, *, timeout: float = 1.0, require_current: bool = True) -> bool:
        try:
            with httpx.Client(trust_env=False) as client:
                response = client.get(
                    f"{self.base_url}/health",
                    headers=self._headers(),
                    timeout=timeout,
                )
            current = False
            if response.status_code == 200 and len(response.content) <= RERANKER_MAX_RESPONSE_BYTES:
                payload = response.json()
                ready = isinstance(payload, dict) and payload.get("status") == "ready"
                current = ready and (
                    not require_current
                    or (
                        payload.get("protocol_version") == RERANKER_PROTOCOL_VERSION
                        and payload.get("component_version") == RERANKER_COMPONENT_VERSION
                        and payload.get("model_revision") == RERANKER_MODEL_REVISION
                    )
                )
            if require_current:
                self._record_health(current)
            return current
        except (httpx.HTTPError, TypeError, ValueError):
            if require_current:
                self._record_health(False)
            return False

    def rerank(
        self,
        *,
        query: str,
        candidates: Sequence[RerankCandidate],
    ) -> Sequence[RerankScore]:
        bounded_query = self._bounded_required_text(
            query,
            maximum=RERANKER_MAX_QUERY_CHARACTERS,
            label="query",
        )
        submitted = list(candidates)
        if not submitted:
            return []
        if len(submitted) > RERANKER_MAX_CANDIDATES:
            raise RerankerBackendError(
                f"reranker candidate count exceeds {RERANKER_MAX_CANDIDATES}"
            )
        identifiers: set[str] = set()
        payload_candidates: list[dict[str, object]] = []
        for candidate in submitted:
            chunk_id = candidate.chunk_id.strip()
            if not chunk_id or len(chunk_id) > 256 or chunk_id in identifiers:
                raise RerankerBackendError("reranker candidates require unique bounded identifiers")
            identifiers.add(chunk_id)
            payload_candidates.append(
                {
                    "chunk_id": chunk_id,
                    "title": self._bounded_text(
                        candidate.title,
                        maximum=RERANKER_MAX_TITLE_CHARACTERS,
                    ),
                    "heading_path": [
                        self._bounded_text(item, maximum=RERANKER_MAX_HEADING_CHARACTERS)
                        for item in candidate.heading_path[:16]
                    ],
                    "text": self._bounded_text(
                        candidate.text,
                        maximum=RERANKER_MAX_TEXT_CHARACTERS,
                    ),
                }
            )
        self._require_ready()
        payload = self._post(
            "/rerank",
            {"query": bounded_query, "candidates": payload_candidates},
        )
        try:
            raw_scores = payload.get("scores")
            if not isinstance(raw_scores, list):
                raise RerankerBackendError("reranker service returned invalid scores")
            scores: list[RerankScore] = []
            returned: set[str] = set()
            for item in raw_scores:
                if not isinstance(item, dict):
                    raise RerankerBackendError("reranker service returned an invalid score item")
                chunk_id = item.get("chunk_id")
                raw_score = item.get("score")
                if not isinstance(chunk_id, str) or chunk_id not in identifiers:
                    raise RerankerBackendError("reranker service returned an unknown identifier")
                if chunk_id in returned or isinstance(raw_score, bool):
                    raise RerankerBackendError(
                        "reranker service returned duplicate or invalid scores"
                    )
                try:
                    score = float(raw_score)
                except (TypeError, ValueError) as exc:
                    raise RerankerBackendError(
                        "reranker service returned a non-numeric score"
                    ) from exc
                if not math.isfinite(score):
                    raise RerankerBackendError("reranker service returned a non-finite score")
                returned.add(chunk_id)
                scores.append(RerankScore(chunk_id=chunk_id, score=score))
            if returned != identifiers:
                raise RerankerBackendError("reranker service returned an incomplete score set")
        except RerankerBackendError:
            self._record_failure()
            raise
        return scores

    def shutdown(self) -> None:
        self._post("/shutdown", {}, timeout=5.0)

    def _post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        body = self._request_body(payload)
        try:
            with httpx.Client(trust_env=False) as client:
                response = client.post(
                    f"{self.base_url}{path}",
                    headers=self._headers(json_content=True),
                    content=body,
                    timeout=timeout or self.timeout,
                )
            response.raise_for_status()
            if len(response.content) > RERANKER_MAX_RESPONSE_BYTES:
                raise ValueError("response is too large")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("response is not an object")
            self._record_success()
            return result
        except (httpx.HTTPError, ValueError) as exc:
            self._record_failure()
            raise RerankerBackendError(
                f"reranker service request failed: {type(exc).__name__}"
            ) from exc

    def _require_ready(self) -> None:
        """Fail quickly while a cold or unhealthy worker is not ready.

        Only one concurrent caller performs the short health probe.  A successful
        probe is cached briefly; a failed request opens a short circuit so searches
        continue with the standard ranking instead of queuing on the model timeout.
        """

        now = time.monotonic()
        with self._gate_lock:
            if now < self._ready_until:
                return
            if now < self._retry_after:
                raise RerankerBackendError("reranker service is temporarily unavailable")
            if self._probe_in_flight:
                raise RerankerBackendError("reranker service readiness probe is in progress")
            self._probe_in_flight = True
        try:
            ready = self.health(timeout=RERANKER_HEALTH_GATE_TIMEOUT_SECONDS)
        finally:
            with self._gate_lock:
                self._probe_in_flight = False
        if not ready:
            raise RerankerBackendError("reranker service is not ready")

    def _record_health(self, ready: bool) -> None:
        if ready:
            self._record_success()
        else:
            self._record_failure()

    def _record_success(self) -> None:
        now = time.monotonic()
        with self._gate_lock:
            self._ready_until = now + RERANKER_HEALTH_CACHE_SECONDS
            self._retry_after = 0.0

    def _record_failure(self) -> None:
        now = time.monotonic()
        with self._gate_lock:
            self._ready_until = 0.0
            self._retry_after = now + RERANKER_CIRCUIT_BREAK_SECONDS

    def _headers(self, *, json_content: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if json_content:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers

    @staticmethod
    def _request_body(payload: dict[str, object]) -> bytes:
        """Serialize once so the client and worker enforce the same byte budget."""

        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RerankerBackendError("reranker request is not valid UTF-8 JSON") from exc
        if len(body) > RERANKER_MAX_REQUEST_BYTES:
            raise RerankerBackendError(
                f"reranker request exceeds {RERANKER_MAX_REQUEST_BYTES} bytes"
            )
        return body

    @staticmethod
    def _bounded_required_text(value: str, *, maximum: int, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise RerankerBackendError(f"reranker {label} cannot be empty")
        return RerankerServiceClient._bounded_text(value, maximum=maximum)

    @staticmethod
    def _bounded_text(value: str, *, maximum: int) -> str:
        if not isinstance(value, str):
            raise RerankerBackendError("reranker text values must be strings")
        return value[:maximum]
