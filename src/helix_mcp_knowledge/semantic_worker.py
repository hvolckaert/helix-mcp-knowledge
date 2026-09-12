"""Private loopback service owning BGE-M3 and persistent local Qdrant state."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .models.source import SourceScope
from .retrieval.embedder import SentenceTransformerEmbedder
from .storage.vectors import QdrantVectorIndex, VectorRecord

_MAX_MANAGED_INSTALLATION_BYTES = 64 * 1024
_VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


class SemanticRuntime:
    def __init__(
        self,
        *,
        model_path: Path,
        vector_path: Path,
        collection: str,
        dimension: int,
        batch_size: int,
        device: str | None,
        normalize: bool,
        max_seq_length: int = 1024,
    ) -> None:
        self.lock = threading.RLock()
        self.model_path = model_path
        self.vector_path = vector_path
        self.collection = collection
        self.dimension = dimension
        self.embedder = SentenceTransformerEmbedder(
            model_name=str(model_path),
            dimension=dimension,
            batch_size=batch_size,
            device=device,
            normalize=normalize,
            local_files_only=True,
            max_seq_length=max_seq_length,
        )
        self.vector_index = self._open_index()

    def _open_index(self) -> QdrantVectorIndex:
        self.vector_path.mkdir(parents=True, exist_ok=True)
        return QdrantVectorIndex(
            url=":local:",
            collection=self.collection,
            dimension=self.dimension,
            api_key_env="",
            path=self.vector_path,
        )

    def reset(self) -> None:
        with self.lock:
            self.vector_index.reset()


class SemanticHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        runtime: SemanticRuntime,
        token: str,
    ) -> None:
        if address[0] != "127.0.0.1":
            raise ValueError("semantic service must bind to IPv4 loopback")
        if not token:
            raise ValueError("semantic service token cannot be empty")
        super().__init__(address, SemanticRequestHandler)
        self.runtime = runtime
        self.token = token


class SemanticRequestHandler(BaseHTTPRequestHandler):
    server: SemanticHTTPServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        self._json(HTTPStatus.OK, {"status": "ready", "dimension": self.server.runtime.dimension})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        try:
            payload = self._read_json()
            runtime = self.server.runtime
            if self.path == "/encode":
                texts = payload.get("texts")
                if not isinstance(texts, list) or not all(isinstance(item, str) for item in texts):
                    raise ValueError("texts must be a list of strings")
                with runtime.lock:
                    vectors = runtime.embedder.encode(texts)
                self._json(HTTPStatus.OK, {"vectors": vectors})
                return
            if self.path == "/upsert":
                records = payload.get("records")
                if not isinstance(records, list):
                    raise ValueError("records must be a list")
                parsed = [
                    VectorRecord(
                        chunk_id=str(item["chunk_id"]),
                        vector=[float(value) for value in item["vector"]],
                        payload=dict(item["payload"]),
                    )
                    for item in records
                ]
                with runtime.lock:
                    runtime.vector_index.upsert(parsed)
                self._json(HTTPStatus.OK, {"upserted": len(parsed)})
                return
            if self.path == "/delete":
                chunk_ids = payload.get("chunk_ids")
                if not isinstance(chunk_ids, list) or not all(
                    isinstance(item, str) for item in chunk_ids
                ):
                    raise ValueError("chunk_ids must be a list of strings")
                with runtime.lock:
                    runtime.vector_index.delete(chunk_ids)
                self._json(HTTPStatus.OK, {"deleted": len(chunk_ids)})
                return
            if self.path == "/search":
                with runtime.lock:
                    candidates = runtime.vector_index.search(
                        [float(value) for value in payload["vector"]],
                        source_scope=SourceScope(payload["source_scope"]),
                        project_id=payload.get("project_id"),
                        product_id=payload.get("product_id"),
                        version=payload.get("version"),
                        document_types=payload.get("document_types"),
                        limit=int(payload["limit"]),
                    )
                self._json(
                    HTTPStatus.OK,
                    {"candidates": [item.chunk_id for item in candidates]},
                )
                return
            if self.path == "/reset":
                runtime.reset()
                self._json(HTTPStatus.OK, {"status": "reset"})
                return
            if self.path == "/chunk-ids":
                with runtime.lock:
                    chunk_ids = sorted(runtime.vector_index.chunk_ids())
                self._json(HTTPStatus.OK, {"chunk_ids": chunk_ids})
                return
            if self.path == "/shutdown":
                self._json(HTTPStatus.OK, {"status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (KeyError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)[:2000]})

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {self.server.token}":
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _read_json(self) -> dict[str, object]:
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > 64 * 1024 * 1024:
            raise ValueError("invalid request size")
        payload = json.loads(self.rfile.read(int(length)))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


def self_test(model_path: Path, dimension: int) -> None:
    with tempfile.TemporaryDirectory(prefix="helix-semantic-self-test-") as temporary:
        runtime = SemanticRuntime(
            model_path=model_path,
            vector_path=Path(temporary) / "vectors",
            collection="self_test",
            dimension=dimension,
            batch_size=2,
            device="cpu",
            normalize=True,
        )
        vectors = runtime.embedder.encode(["Helix reconciliation", "unrelated weather"])
        runtime.vector_index.upsert(
            [
                VectorRecord(
                    chunk_id="self-test",
                    vector=vectors[0],
                    payload={
                        "active": True,
                        "source_scope": "bmc_official",
                        "project_id": None,
                        "product_ids": ["cmdb"],
                        "product_versions": ["26.1"],
                        "document_type": "concept",
                    },
                )
            ]
        )
        found = runtime.vector_index.search(
            vectors[0],
            source_scope=SourceScope.BMC_OFFICIAL,
            project_id=None,
            product_id="cmdb",
            version="26.1",
            document_types=None,
            limit=1,
        )
        if not found or found[0].chunk_id != "self-test":
            raise RuntimeError("semantic self-test did not retrieve its test vector")
        runtime.vector_index.client.close()


def managed_runtime_is_active(*, metadata_path: Path, expected_version: str) -> bool:
    """Fail closed when this detached worker no longer belongs to the active release."""

    if _VERSION_PATTERN.fullmatch(expected_version) is None:
        return False
    if metadata_path.is_symlink() or (
        hasattr(os.path, "isjunction") and os.path.isjunction(metadata_path)
    ):
        return False
    try:
        details = metadata_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_MANAGED_INSTALLATION_BYTES:
            return False
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") in {1, 2}
        and payload.get("active_version") == expected_version
    )


def watch_managed_runtime(
    server: SemanticHTTPServer,
    *,
    metadata_path: Path,
    expected_version: str,
    stopped: threading.Event,
    poll_seconds: float = 1.0,
) -> None:
    """Stop the optional service promptly after an update rollback or replacement."""

    while not stopped.wait(poll_seconds):
        if not managed_runtime_is_active(
            metadata_path=metadata_path,
            expected_version=expected_version,
        ):
            server.shutdown()
            return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--dimension", required=True, type=int)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--vector-path")
    parser.add_argument("--collection", default="helix_knowledge_chunks")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--token", default=os.environ.get("HELIX_SEMANTIC_SERVICE_TOKEN"))
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-seq-length", type=int, default=1024)
    parser.add_argument("--device")
    parser.add_argument("--managed-installation-path")
    parser.add_argument("--managed-version")
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("--host must be 127.0.0.1")
    model_path = Path(args.model_path).resolve()
    if args.self_test:
        self_test(model_path, args.dimension)
        return 0
    if not args.vector_path or not args.token:
        parser.error("--vector-path and --token are required in service mode")
    if bool(args.managed_installation_path) != bool(args.managed_version):
        parser.error("managed installation path and version must be supplied together")
    managed_metadata = (
        Path(args.managed_installation_path).expanduser().absolute()
        if args.managed_installation_path
        else None
    )
    if managed_metadata is not None and not managed_runtime_is_active(
        metadata_path=managed_metadata,
        expected_version=args.managed_version,
    ):
        return 0
    runtime = SemanticRuntime(
        model_path=model_path,
        vector_path=Path(args.vector_path).resolve(),
        collection=args.collection,
        dimension=args.dimension,
        batch_size=args.batch_size,
        device=args.device,
        normalize=True,
        max_seq_length=args.max_seq_length,
    )
    if managed_metadata is not None and not managed_runtime_is_active(
        metadata_path=managed_metadata,
        expected_version=args.managed_version,
    ):
        runtime.vector_index.client.close()
        return 0
    server = SemanticHTTPServer((args.host, args.port), runtime=runtime, token=args.token)
    if managed_metadata is not None and not managed_runtime_is_active(
        metadata_path=managed_metadata,
        expected_version=args.managed_version,
    ):
        server.server_close()
        runtime.vector_index.client.close()
        return 0
    managed_watch_stop = threading.Event()
    if managed_metadata is not None:
        threading.Thread(
            target=watch_managed_runtime,
            kwargs={
                "server": server,
                "metadata_path": managed_metadata,
                "expected_version": args.managed_version,
                "stopped": managed_watch_stop,
            },
            name="helix-semantic-managed-runtime-watch",
            daemon=True,
        ).start()
    try:
        server.serve_forever()
    finally:
        managed_watch_stop.set()
        runtime.vector_index.client.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
