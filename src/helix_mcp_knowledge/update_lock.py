"""Cross-platform advisory lock shared by all managed maintenance operations."""

from __future__ import annotations

import os
import time
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType

from .errors import KnowledgeError


class UpdateLockBusyError(KnowledgeError):
    """Another managed operation already owns the shared maintenance lock."""


class UpdateLock(AbstractContextManager["UpdateLock"]):
    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 0.0,
        poll_seconds: float = 0.25,
    ) -> None:
        self.path = path
        self.timeout_seconds = max(0.0, timeout_seconds)
        self.poll_seconds = max(0.01, poll_seconds)
        self._stream = None

    def __enter__(self) -> UpdateLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"\0")
            self._stream.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            self._stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise UpdateLockBusyError(
                        "another helix-mcp-knowledge synchronization, update, or cleanup is running"
                    ) from exc
                time.sleep(min(self.poll_seconds, max(0.0, deadline - time.monotonic())))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None
