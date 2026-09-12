"""Private loopback worker for the optional BGE reranker component."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import threading
from collections.abc import Sequence
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .reranker_client import (
    RERANKER_COMPONENT_VERSION,
    RERANKER_MAX_HEADING_CHARACTERS,
    RERANKER_MAX_QUERY_CHARACTERS,
    RERANKER_MAX_REQUEST_BYTES,
    RERANKER_MAX_TEXT_CHARACTERS,
    RERANKER_MAX_TITLE_CHARACTERS,
    RERANKER_MODEL_REVISION,
    RERANKER_PROTOCOL_VERSION,
)
from .retrieval.reranker import RERANKER_MAX_CANDIDATES, RerankCandidate, RerankScore

RERANKER_DEFAULT_MAX_SEQUENCE_LENGTH = 256
RERANKER_DEFAULT_BATCH_SIZE = 4
RERANKER_DEFAULT_CPU_THREADS = max(1, min(8, os.cpu_count() or 1))
_MAX_SERVICE_CONTROL_BYTES = 4096
_MAX_MANAGED_INSTALLATION_BYTES = 64 * 1024
_SERVICE_GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")
_VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


class RerankerRuntime:
    """Load the pinned local model without executing repository-provided code."""

    def __init__(
        self,
        *,
        model_path: Path,
        max_sequence_length: int = RERANKER_DEFAULT_MAX_SEQUENCE_LENGTH,
        batch_size: int = RERANKER_DEFAULT_BATCH_SIZE,
        cpu_threads: int = RERANKER_DEFAULT_CPU_THREADS,
    ) -> None:
        if not model_path.is_dir():
            raise ValueError("reranker model path does not exist")
        if not 64 <= max_sequence_length <= 1024:
            raise ValueError("reranker sequence length must be between 64 and 1024")
        if not 1 <= batch_size <= 16:
            raise ValueError("reranker batch size must be between 1 and 16")
        if not 1 <= cpu_threads <= 16:
            raise ValueError("reranker CPU thread count must be between 1 and 16")
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        torch.set_num_threads(cpu_threads)
        self.torch = torch
        self.batch_size = batch_size
        self.max_sequence_length = max_sequence_length
        self.tokenizer = AutoTokenizer.from_pretrained(
            str(model_path),
            local_files_only=True,
            trust_remote_code=False,
            use_fast=True,
        )
        self.model = AutoModelForSequenceClassification.from_pretrained(
            str(model_path),
            local_files_only=True,
            trust_remote_code=False,
        )
        self.model.to("cpu")
        self.model.eval()
        self.lock = threading.Lock()

    def rerank(
        self,
        *,
        query: str,
        candidates: Sequence[RerankCandidate],
    ) -> list[RerankScore]:
        scores: list[RerankScore] = []
        with self.lock, self.torch.inference_mode():
            for offset in range(0, len(candidates), self.batch_size):
                batch = candidates[offset : offset + self.batch_size]
                documents = [self._document_text(candidate) for candidate in batch]
                encoded = self.tokenizer(
                    [query] * len(batch),
                    documents,
                    padding=True,
                    truncation=True,
                    max_length=self.max_sequence_length,
                    return_tensors="pt",
                )
                output = self.model(**encoded, return_dict=True)
                raw_scores = output.logits.reshape(-1).float().cpu().tolist()
                if len(raw_scores) != len(batch):
                    raise RuntimeError("reranker model returned an unexpected score count")
                for candidate, raw_score in zip(batch, raw_scores, strict=True):
                    score = float(raw_score)
                    if not math.isfinite(score):
                        raise RuntimeError("reranker model returned a non-finite score")
                    scores.append(RerankScore(chunk_id=candidate.chunk_id, score=score))
        return scores

    @staticmethod
    def _document_text(candidate: RerankCandidate) -> str:
        heading = " > ".join(candidate.heading_path)
        return "\n".join(part for part in (candidate.title, heading, candidate.text) if part)


class RerankerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        runtime: RerankerRuntime,
        token: str,
    ) -> None:
        if address[0] != "127.0.0.1":
            raise ValueError("reranker service must bind to IPv4 loopback")
        if not token:
            raise ValueError("reranker service token cannot be empty")
        super().__init__(address, RerankerRequestHandler)
        self.runtime = runtime
        self.token = token


class RerankerRequestHandler(BaseHTTPRequestHandler):
    server: RerankerHTTPServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        self._json(
            HTTPStatus.OK,
            {
                "status": "ready",
                "protocol_version": RERANKER_PROTOCOL_VERSION,
                "component_version": RERANKER_COMPONENT_VERSION,
                "model_revision": RERANKER_MODEL_REVISION,
            },
        )

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path == "/shutdown":
            self._json(HTTPStatus.OK, {"status": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path != "/rerank":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = self._read_json()
            query = self._required_text(
                payload.get("query"),
                label="query",
                maximum=RERANKER_MAX_QUERY_CHARACTERS,
            )
            candidates = self._candidates(payload.get("candidates"))
            scores = self.server.runtime.rerank(query=query, candidates=candidates)
            self._json(
                HTTPStatus.OK,
                {"scores": [asdict(item) for item in scores]},
            )
        except (TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            # Do not include inference exceptions: they can contain submitted text.
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "reranking failed"})

    def _candidates(self, value: object) -> list[RerankCandidate]:
        if not isinstance(value, list) or not value:
            raise ValueError("candidates must be a non-empty list")
        if len(value) > RERANKER_MAX_CANDIDATES:
            raise ValueError(f"candidate count cannot exceed {RERANKER_MAX_CANDIDATES}")
        candidates: list[RerankCandidate] = []
        identifiers: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("each candidate must be an object")
            chunk_id = self._required_text(item.get("chunk_id"), label="chunk_id", maximum=256)
            if chunk_id in identifiers:
                raise ValueError("candidate identifiers must be unique")
            identifiers.add(chunk_id)
            title = self._text(
                item.get("title"),
                label="title",
                maximum=RERANKER_MAX_TITLE_CHARACTERS,
            )
            raw_headings = item.get("heading_path")
            if not isinstance(raw_headings, list) or len(raw_headings) > 16:
                raise ValueError("heading_path must be a list of at most 16 strings")
            heading_path = tuple(
                self._text(
                    heading,
                    label="heading_path item",
                    maximum=RERANKER_MAX_HEADING_CHARACTERS,
                )
                for heading in raw_headings
            )
            text = self._text(
                item.get("text"),
                label="text",
                maximum=RERANKER_MAX_TEXT_CHARACTERS,
            )
            candidates.append(
                RerankCandidate(
                    chunk_id=chunk_id,
                    title=title,
                    heading_path=heading_path,
                    text=text,
                )
            )
        return candidates

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {self.server.token}":
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _read_json(self) -> dict[str, object]:
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or not 0 < int(length) <= RERANKER_MAX_REQUEST_BYTES:
            raise ValueError("invalid request size")
        payload = json.loads(self.rfile.read(int(length)))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    @staticmethod
    def _required_text(value: object, *, label: str, maximum: int) -> str:
        text = RerankerRequestHandler._text(value, label=label, maximum=maximum)
        if not text.strip():
            raise ValueError(f"{label} cannot be empty")
        return text

    @staticmethod
    def _text(value: object, *, label: str, maximum: int) -> str:
        if not isinstance(value, str) or len(value) > maximum:
            raise ValueError(f"{label} must be a string of at most {maximum} characters")
        return value

    def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Queries and candidate content must never reach HTTP access logs.
        return None


def self_test(model_path: Path) -> None:
    runtime = RerankerRuntime(
        model_path=model_path,
        max_sequence_length=256,
        batch_size=2,
        cpu_threads=2,
    )
    scores = runtime.rerank(
        query="How does CMDB reconciliation handle duplicate configuration items?",
        candidates=[
            RerankCandidate(
                chunk_id="relevant",
                title="Reconciliation",
                heading_path=("Data integrity",),
                text="Reconciliation identifies and merges duplicate CIs between datasets.",
            ),
            RerankCandidate(
                chunk_id="unrelated",
                title="Weather",
                heading_path=(),
                text="Tomorrow will be sunny.",
            ),
        ],
    )
    by_id = {item.chunk_id: item.score for item in scores}
    if set(by_id) != {"relevant", "unrelated"} or by_id["relevant"] <= by_id["unrelated"]:
        raise RuntimeError("reranker self-test did not prefer the relevant document")


def service_start_is_current(
    *,
    control_path: Path,
    stop_path: Path,
    generation: str,
) -> bool:
    """Fail closed unless this worker is still the uniquely requested generation."""

    if _SERVICE_GENERATION_PATTERN.fullmatch(generation) is None:
        return False
    if stop_path.is_symlink() or stop_path.exists():
        return False
    if control_path.is_symlink() or not control_path.is_file():
        return False
    try:
        details = control_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_SERVICE_CONTROL_BYTES:
            return False
        payload = json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == 1
        and payload.get("generation") == generation
        and payload.get("desired") is True
    )


def managed_runtime_is_active(*, metadata_path: Path, expected_version: str) -> bool:
    """Fail closed when a managed worker no longer belongs to the active runtime."""

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
    server: RerankerHTTPServer,
    *,
    metadata_path: Path,
    expected_version: str,
    stopped: threading.Event,
    poll_seconds: float = 1.0,
) -> None:
    """Stop a detached worker promptly when a managed activation is rolled back."""

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
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--token", default=os.environ.get("HELIX_RERANKER_SERVICE_TOKEN"))
    parser.add_argument("--batch-size", type=int, default=RERANKER_DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--max-seq-length",
        type=int,
        default=RERANKER_DEFAULT_MAX_SEQUENCE_LENGTH,
    )
    parser.add_argument("--cpu-threads", type=int, default=RERANKER_DEFAULT_CPU_THREADS)
    parser.add_argument("--control-path")
    parser.add_argument("--stop-path")
    parser.add_argument("--generation")
    parser.add_argument("--managed-installation-path")
    parser.add_argument("--managed-version")
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("--host must be 127.0.0.1")
    model_path = Path(args.model_path).expanduser().resolve()
    if args.self_test:
        self_test(model_path)
        return 0
    if not args.token:
        parser.error("--token or HELIX_RERANKER_SERVICE_TOKEN is required")
    if not args.control_path or not args.stop_path or not args.generation:
        parser.error("--control-path, --stop-path, and --generation are required")
    if bool(args.managed_installation_path) != bool(args.managed_version):
        parser.error("managed installation path and version must be supplied together")
    control_path = Path(args.control_path).expanduser().absolute()
    stop_path = Path(args.stop_path).expanduser().absolute()
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
    runtime = RerankerRuntime(
        model_path=model_path,
        max_sequence_length=args.max_seq_length,
        batch_size=args.batch_size,
        cpu_threads=args.cpu_threads,
    )
    if not service_start_is_current(
        control_path=control_path,
        stop_path=stop_path,
        generation=args.generation,
    ):
        return 0
    if managed_metadata is not None and not managed_runtime_is_active(
        metadata_path=managed_metadata,
        expected_version=args.managed_version,
    ):
        return 0
    server = RerankerHTTPServer(
        (args.host, args.port),
        runtime=runtime,
        token=args.token,
    )
    if not service_start_is_current(
        control_path=control_path,
        stop_path=stop_path,
        generation=args.generation,
    ):
        server.server_close()
        return 0
    if managed_metadata is not None and not managed_runtime_is_active(
        metadata_path=managed_metadata,
        expected_version=args.managed_version,
    ):
        server.server_close()
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
            name="helix-reranker-managed-runtime-watch",
            daemon=True,
        ).start()
    try:
        server.serve_forever()
    finally:
        managed_watch_stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
