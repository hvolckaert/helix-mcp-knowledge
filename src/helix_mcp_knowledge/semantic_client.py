"""HTTP adapters for the private managed semantic service."""

from __future__ import annotations

from dataclasses import asdict
from urllib.parse import urlsplit

import httpx

from .errors import SemanticBackendError
from .models.source import SourceScope
from .storage.vectors import SemanticCandidate, VectorRecord


class SemanticServiceClient:
    enabled = True

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        dimension: int,
        timeout_seconds: int,
    ) -> None:
        normalized_url = base_url.rstrip("/")
        try:
            parsed = urlsplit(normalized_url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("semantic service URL is invalid") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or port is None
        ):
            raise ValueError("semantic service URL must be an IPv4 loopback HTTP origin")
        if not token:
            raise ValueError("semantic service token cannot be empty")
        self.base_url = normalized_url
        self.token = token
        self.dimension = dimension
        self.timeout = timeout_seconds

    def health(self, *, timeout: float = 2.0) -> bool:
        try:
            with httpx.Client(trust_env=False) as client:
                response = client.get(
                    f"{self.base_url}/health",
                    headers=self._headers(),
                    timeout=timeout,
                )
            return response.status_code == 200 and response.json().get("status") == "ready"
        except (httpx.HTTPError, ValueError):
            return False

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = self._post("/encode", {"texts": texts})
        vectors = payload.get("vectors")
        if not isinstance(vectors, list):
            raise SemanticBackendError("semantic service returned invalid vectors")
        return [[float(value) for value in vector] for vector in vectors]

    def upsert(self, records: list[VectorRecord]) -> None:
        if records:
            self._post("/upsert", {"records": [asdict(item) for item in records]})

    def delete(self, chunk_ids: list[str]) -> None:
        if chunk_ids:
            self._post("/delete", {"chunk_ids": chunk_ids})

    def search(
        self,
        vector: list[float],
        *,
        source_scope: SourceScope,
        project_id: str | None,
        product_id: str | None,
        version: str | None,
        document_types: list[str] | None,
        limit: int,
    ) -> list[SemanticCandidate]:
        payload = self._post(
            "/search",
            {
                "vector": vector,
                "source_scope": source_scope.value,
                "project_id": project_id,
                "product_id": product_id,
                "version": version,
                "document_types": document_types,
                "limit": limit,
            },
        )
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            raise SemanticBackendError("semantic service returned invalid candidates")
        return [
            SemanticCandidate(chunk_id=str(chunk_id), rank=rank)
            for rank, chunk_id in enumerate(candidates, start=1)
        ]

    def reset(self) -> None:
        self._post("/reset", {})

    def chunk_ids(self) -> set[str]:
        payload = self._post("/chunk-ids", {})
        chunk_ids = payload.get("chunk_ids")
        if not isinstance(chunk_ids, list):
            raise SemanticBackendError("semantic service returned invalid chunk identifiers")
        return {str(chunk_id) for chunk_id in chunk_ids}

    def shutdown(self) -> None:
        self._post("/shutdown", {}, timeout=10)

    def _post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        try:
            with httpx.Client(trust_env=False) as client:
                response = client.post(
                    f"{self.base_url}{path}",
                    headers=self._headers(),
                    json=payload,
                    timeout=timeout or self.timeout,
                )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("response is not an object")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise SemanticBackendError(f"semantic service request failed: {exc}") from exc

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}
