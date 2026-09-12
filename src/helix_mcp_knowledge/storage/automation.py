"""Cross-process automation leadership and operational state in SQLite."""

import json
import time
from datetime import UTC, datetime

from .database import Database


class AutomationStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def acquire_or_renew(self, lease_name: str, owner_id: str, lease_seconds: int) -> bool:
        now = time.time()
        expires_at = now + lease_seconds
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT owner_id, expires_at FROM automation_leases WHERE lease_name = ?",
                    (lease_name,),
                ).fetchone()
                if row is not None and row["owner_id"] != owner_id and row["expires_at"] > now:
                    connection.rollback()
                    return False
                connection.execute(
                    """
                    INSERT INTO automation_leases(
                        lease_name, owner_id, expires_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(lease_name) DO UPDATE SET
                        owner_id=excluded.owner_id,
                        expires_at=excluded.expires_at,
                        heartbeat_at=excluded.heartbeat_at
                    """,
                    (lease_name, owner_id, expires_at, datetime.now(UTC).isoformat()),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def renew(self, lease_name: str, owner_id: str, lease_seconds: int) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE automation_leases
                SET expires_at = ?, heartbeat_at = ?
                WHERE lease_name = ? AND owner_id = ?
                """,
                (
                    time.time() + lease_seconds,
                    datetime.now(UTC).isoformat(),
                    lease_name,
                    owner_id,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def release(self, lease_name: str, owner_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM automation_leases WHERE lease_name = ? AND owner_id = ?",
                (lease_name, owner_id),
            )
            connection.commit()

    def active_owner_ids(self) -> set[str]:
        """Return owners whose cross-process lease has not expired."""

        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT owner_id FROM automation_leases WHERE expires_at > ?",
                (time.time(),),
            ).fetchall()
        return {str(row["owner_id"]) for row in rows}

    def running_state_is_active(self, state: dict[str, object]) -> bool:
        owner_id = state.get("owner_id")
        return (
            state.get("status") == "running"
            and isinstance(owner_id, str)
            and owner_id in self.active_owner_ids()
        )

    def update_state(self, job_id: str, state: dict[str, object]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_state(job_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (
                    job_id,
                    json.dumps(state, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()

    def patch_state(
        self,
        job_id: str,
        values: dict[str, object],
        *,
        defaults: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Atomically merge fields without losing concurrent control values.

        ``defaults`` are applied only when a field is absent, which is useful for
        initializing desired state without overwriting a cancellation written by
        another process or dashboard request.
        """

        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state_json FROM automation_state WHERE job_id = ?", (job_id,)
                ).fetchone()
                current = json.loads(row["state_json"]) if row is not None else {}
                if not isinstance(current, dict):
                    current = {}
                updated = {**(defaults or {}), **current, **values}
                connection.execute(
                    """
                    INSERT INTO automation_state(job_id, state_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        state_json=excluded.state_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        job_id,
                        json.dumps(updated, ensure_ascii=False),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                connection.commit()
                return updated
            except Exception:
                connection.rollback()
                raise

    def update_state_if_current(
        self,
        job_id: str,
        *,
        owner_id: str,
        expected_status: str,
        state: dict[str, object],
    ) -> bool:
        """Replace a state only while the expected owner still controls its run."""

        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state_json FROM automation_state WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
                current = json.loads(row["state_json"])
                if (
                    not isinstance(current, dict)
                    or current.get("owner_id") != owner_id
                    or current.get("status") != expected_status
                ):
                    connection.rollback()
                    return False
                connection.execute(
                    """
                    UPDATE automation_state
                    SET state_json = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        json.dumps(state, ensure_ascii=False),
                        datetime.now(UTC).isoformat(),
                        job_id,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def patch_state_if_current(
        self,
        job_id: str,
        *,
        expected_status: str,
        values: dict[str, object],
        owner_id: str | None = None,
    ) -> bool:
        """Atomically merge fields into a state while its run remains current."""

        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state_json FROM automation_state WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
                current = json.loads(row["state_json"])
                if (
                    not isinstance(current, dict)
                    or current.get("status") != expected_status
                    or (owner_id is not None and current.get("owner_id") != owner_id)
                ):
                    connection.rollback()
                    return False
                updated = {**current, **values}
                connection.execute(
                    """
                    UPDATE automation_state
                    SET state_json = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        json.dumps(updated, ensure_ascii=False),
                        datetime.now(UTC).isoformat(),
                        job_id,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def state(self, job_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM automation_state WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return {}
        value = json.loads(row["state_json"])
        return value if isinstance(value, dict) else {}
