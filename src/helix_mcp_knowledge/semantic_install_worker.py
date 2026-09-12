"""Install the semantic component and build its vector index in the background."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .config import AppConfig, load_config
from .semantic_component import SemanticComponentManager
from .storage.automation import AutomationStore
from .storage.database import Database
from .storage.vectors import VectorRecord
from .update_lock import UpdateLock, UpdateLockBusyError

SEMANTIC_INSTALL_JOB = "semantic-component-install"
SEMANTIC_PAYLOAD_VERSION = 2


@dataclass(frozen=True)
class SemanticInstallWorkerProcess:
    pid: int


@dataclass(frozen=True)
class SemanticRebuildResult:
    total: int
    completed: int
    cancelled: bool


class SemanticInstallWorkerLauncher:
    def __init__(
        self,
        *,
        config_path: Path,
        workspace: Path,
        errors_path: Path,
        python_executable: str | None = None,
    ) -> None:
        self.config_path = config_path.resolve()
        self.workspace = workspace.resolve()
        self.errors_path = errors_path.resolve()
        self.python_executable = python_executable or sys.executable
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> SemanticInstallWorkerProcess:
        if self._process is not None:
            raise RuntimeError("semantic installation worker already launched")
        self.errors_path.mkdir(parents=True, exist_ok=True)
        log_path = self.errors_path / "semantic-install-worker.log"
        kwargs: dict[str, object] = {
            "cwd": self.workspace,
            "env": {**os.environ, "PYTHONUNBUFFERED": "1"},
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":  # pragma: no cover
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        with log_path.open("ab", buffering=0) as log:
            self._process = subprocess.Popen(
                [
                    self.python_executable,
                    "-m",
                    "helix_mcp_knowledge.semantic_install_worker",
                    "--config",
                    str(self.config_path),
                ],
                stderr=log,
                **kwargs,
            )
        threading.Thread(target=self._process.wait, daemon=True).start()
        return SemanticInstallWorkerProcess(pid=self._process.pid)


def _set_enabled(config_path: Path, enabled: bool) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    payload.setdefault("retrieval", {}).setdefault("semantic", {})["enabled"] = enabled
    serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, config_path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _rows(config: AppConfig) -> tuple[int, list[object]]:
    database = Database(config.sqlite_path)
    with database.connect() as connection:
        total = connection.execute(
            """
            SELECT count(*)
            FROM chunks c JOIN documents d ON d.document_id = c.document_id
            WHERE c.active = 1 AND d.status = 'indexed'
            """
        ).fetchone()[0]
        rows = connection.execute(
            """
            SELECT c.chunk_id, c.embedding_text, c.source_scope, c.project_id,
                   c.document_type, d.document_id, d.classification, d.language,
                   COALESCE((SELECT group_concat(dp.product_id, '|')
                     FROM document_products dp WHERE dp.document_id=d.document_id), '') products,
                   COALESCE((SELECT group_concat(pv.version, '|')
                     FROM document_product_versions dpv
                     JOIN product_versions pv ON pv.product_version_id=dpv.product_version_id
                     WHERE dpv.document_id=d.document_id), '') versions,
                   COALESCE((SELECT group_concat(pv.product_id || ':' || pv.version, '|')
                     FROM document_product_versions dpv
                     JOIN product_versions pv ON pv.product_version_id=dpv.product_version_id
                     WHERE dpv.document_id=d.document_id), '') product_version_pairs
            FROM chunks c JOIN documents d ON d.document_id = c.document_id
            WHERE c.active = 1 AND d.status = 'indexed'
            ORDER BY length(c.embedding_text), c.chunk_id
            """
        ).fetchall()
    return int(total), list(rows)


def rebuild_vectors(
    config: AppConfig,
    store: AutomationStore,
    *,
    reset: bool = False,
    consistency_pass: int = 1,
) -> SemanticRebuildResult:
    client = SemanticComponentManager(config).ensure_service()
    if reset:
        client.reset()
    total, rows = _rows(config)
    current_ids = {row["chunk_id"] for row in rows}
    vector_ids = client.chunk_ids()
    stale_ids = sorted(vector_ids - current_ids)
    for offset in range(0, len(stale_ids), 512):
        client.delete(stale_ids[offset : offset + 512])
    rows = [row for row in rows if row["chunk_id"] not in vector_ids]
    completed = total - len(rows)
    batch_size = config.embeddings.batch_size
    started = time.monotonic()
    store.update_state(
        SEMANTIC_INSTALL_JOB,
        {
            **store.state(SEMANTIC_INSTALL_JOB),
            "status": "indexing",
            "indexed_chunks": completed,
            "total_chunks": total,
            "chunks_per_second": None,
            "estimated_seconds_remaining": None,
        },
    )
    for offset in range(0, len(rows), batch_size):
        if store.state(SEMANTIC_INSTALL_JOB).get("desired_enabled") is False:
            return SemanticRebuildResult(total=total, completed=completed, cancelled=True)
        batch = rows[offset : offset + batch_size]
        vectors = client.encode([row["embedding_text"] for row in batch])
        records = []
        for row, vector in zip(batch, vectors, strict=True):
            records.append(
                VectorRecord(
                    chunk_id=row["chunk_id"],
                    vector=vector,
                    payload={
                        "document_id": row["document_id"],
                        "source_scope": row["source_scope"],
                        "project_id": row["project_id"],
                        "document_type": row["document_type"],
                        "product_ids": row["products"].split("|") if row["products"] else [],
                        "product_versions": (row["versions"].split("|") if row["versions"] else []),
                        "product_version_pairs": (
                            row["product_version_pairs"].split("|")
                            if row["product_version_pairs"]
                            else []
                        ),
                        "classification": row["classification"],
                        "language": row["language"],
                        "active": True,
                    },
                )
            )
        client.upsert(records)
        completed += len(batch)
        elapsed = max(time.monotonic() - started, 0.001)
        newly_indexed = offset + len(batch)
        chunks_per_second = newly_indexed / elapsed
        current = store.state(SEMANTIC_INSTALL_JOB)
        store.update_state(
            SEMANTIC_INSTALL_JOB,
            {
                **current,
                "status": "indexing",
                "indexed_chunks": completed,
                "total_chunks": total,
                "chunks_per_second": round(chunks_per_second, 3),
                "estimated_seconds_remaining": round((total - completed) / chunks_per_second),
                "last_activity_at": datetime.now(UTC).isoformat(),
            },
        )
    final_total, final_rows = _rows(config)
    final_current_ids = {row["chunk_id"] for row in final_rows}
    final_vector_ids = client.chunk_ids()
    if final_current_ids != final_vector_ids:
        if consistency_pass >= 3:
            raise RuntimeError(
                "SQLite changed repeatedly during semantic indexing; "
                "retry when synchronization is idle"
            )
        return rebuild_vectors(
            config,
            store,
            reset=False,
            consistency_pass=consistency_pass + 1,
        )
    return SemanticRebuildResult(total=final_total, completed=final_total, cancelled=False)


def run_install(config_path: str | Path) -> bool:
    path = Path(config_path).expanduser().resolve()
    config = load_config(path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    current = store.state(SEMANTIC_INSTALL_JOB)
    store.update_state(
        SEMANTIC_INSTALL_JOB,
        {**current, "status": "installing", "started_at": datetime.now(UTC).isoformat()},
    )
    try:
        manager = SemanticComponentManager(config)
        previous_component = manager.status()
        component = manager.install()
        store.update_state(
            SEMANTIC_INSTALL_JOB,
            {
                **store.state(SEMANTIC_INSTALL_JOB),
                "status": "indexing",
                "component_version": component.component_version,
                "installed_bytes": component.installed_bytes,
                "indexed_chunks": 0,
                "payload_version": SEMANTIC_PAYLOAD_VERSION,
            },
        )
        rebuild = rebuild_vectors(
            config,
            store,
            reset=(
                not previous_component.installed
                or previous_component.component_version != component.component_version
                or current.get("payload_version") != SEMANTIC_PAYLOAD_VERSION
            ),
        )
        if rebuild.cancelled:
            _set_enabled(path, False)
            store.update_state(
                SEMANTIC_INSTALL_JOB,
                {
                    **store.state(SEMANTIC_INSTALL_JOB),
                    "status": "ready",
                    "desired_enabled": False,
                    "cancelled": True,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "indexed_chunks": rebuild.completed,
                    "total_chunks": rebuild.total,
                },
            )
            return True
        desired_enabled = store.state(SEMANTIC_INSTALL_JOB).get("desired_enabled") is not False
        _set_enabled(path, desired_enabled)
        store.update_state(
            SEMANTIC_INSTALL_JOB,
            {
                "status": "ready",
                "desired_enabled": desired_enabled,
                "finished_at": datetime.now(UTC).isoformat(),
                "component_version": component.component_version,
                "installed_bytes": component.installed_bytes,
                "indexed_chunks": rebuild.total,
                "total_chunks": rebuild.total,
                "payload_version": SEMANTIC_PAYLOAD_VERSION,
            },
        )
        return True
    except Exception as exc:
        _set_enabled(path, False)
        store.update_state(
            SEMANTIC_INSTALL_JOB,
            {
                "status": "error",
                "desired_enabled": False,
                "finished_at": datetime.now(UTC).isoformat(),
                "error": str(exc)[:2000],
            },
        )
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    try:
        with UpdateLock(
            load_config(config_path).base_dir / ".update.lock",
            timeout_seconds=30.0,
        ):
            succeeded = run_install(config_path)
    except UpdateLockBusyError as exc:
        config = load_config(config_path)
        database = Database(config.sqlite_path)
        database.initialize()
        _set_enabled(config_path, False)
        AutomationStore(database).update_state(
            SEMANTIC_INSTALL_JOB,
            {
                "status": "error",
                "desired_enabled": False,
                "finished_at": datetime.now(UTC).isoformat(),
                "error": str(exc),
            },
        )
        succeeded = False
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
