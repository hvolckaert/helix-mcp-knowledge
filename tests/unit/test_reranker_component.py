from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import venv
from pathlib import Path

import pytest

from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.errors import ConfigurationError
from helix_mcp_knowledge.reranker_component import (
    RERANKER_COMPONENT_VERSION,
    RERANKER_MODEL_FILES,
    RERANKER_MODEL_ID,
    RERANKER_MODEL_REVISION,
    RERANKER_PACKAGES,
    RERANKER_TORCH_FIND_LINKS,
    RERANKER_TORCH_PACKAGE,
    RerankerComponentManager,
)
from helix_mcp_knowledge.reranker_worker import service_start_is_current
from helix_mcp_knowledge.update_lock import UpdateLock, UpdateLockBusyError

_TEST_RUNTIME_NAME = f"reranker-{RERANKER_COMPONENT_VERSION}-{'1' * 32}"
_TEST_MODEL_NAME = f"bge-reranker-v2-m3-{'2' * 32}"
_MISSING_RUNTIME_NAME = f"reranker-{RERANKER_COMPONENT_VERSION}-{'d' * 32}"
_MISSING_MODEL_NAME = f"bge-reranker-v2-m3-{'e' * 32}"


def _python(runtime: Path) -> Path:
    return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _write_component(config_path: Path):
    config = load_config(config_path)
    runtime = config.reranker_component_path / "runtime" / _TEST_RUNTIME_NAME
    model = config.reranker_component_path / "models" / _TEST_MODEL_NAME
    _python(runtime).parent.mkdir(parents=True)
    _python(runtime).write_text("placeholder", encoding="utf-8")
    model.mkdir(parents=True)
    (config.reranker_component_path / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component_version": RERANKER_COMPONENT_VERSION,
                "runtime": f"runtime/{_TEST_RUNTIME_NAME}",
                "model_path": f"models/{_TEST_MODEL_NAME}",
                "model_id": RERANKER_MODEL_ID,
                "model_revision": RERANKER_MODEL_REVISION,
                "inference_runtime": RERANKER_TORCH_PACKAGE,
                "packages": list(RERANKER_PACKAGES),
                "host": "127.0.0.1",
                "port": 8768,
                "token": "x" * 48,
                "installed_at": "2026-09-09T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return config


class ReadyClient:
    def health(self, **_kwargs) -> bool:
        return True

    def shutdown(self) -> None:
        return None


@pytest.mark.parametrize(("port_accepting", "expected"), [(False, True), (True, False)])
def test_service_inactive_requires_a_free_lifecycle_lock_and_closed_port(
    config_path: Path,
    monkeypatch,
    port_accepting: bool,
    expected: bool,
) -> None:
    config = _write_component(config_path)
    manager = RerankerComponentManager(config)
    monkeypatch.setattr(manager, "_port_accepting", lambda: port_accepting)

    assert manager.service_is_inactive() is expected


def test_service_inactive_does_not_collapse_an_active_lifecycle_operation(
    config_path: Path,
) -> None:
    config = _write_component(config_path)
    manager = RerankerComponentManager(config)

    with UpdateLock(manager.service_lock_path), pytest.raises(UpdateLockBusyError):
        manager.service_is_inactive()


def test_each_service_start_gets_a_new_persisted_generation(config_path: Path) -> None:
    config = _write_component(config_path)
    manager = RerankerComponentManager(config)
    first_metadata = manager._metadata()
    assert first_metadata is not None

    first_generation = manager._prepare_service_start(first_metadata)
    manager.request_service_stop()
    manager.request_service_start()
    second_metadata = manager._metadata()
    assert second_metadata is not None
    second_generation = manager._prepare_service_start(second_metadata)

    assert first_generation != second_generation
    assert manager._metadata()["service_generation"] == second_generation
    assert not service_start_is_current(
        control_path=manager.service_control_path,
        stop_path=manager.service_stop_path,
        generation=first_generation,
    )
    assert service_start_is_current(
        control_path=manager.service_control_path,
        stop_path=manager.service_stop_path,
        generation=second_generation,
    )


def test_ensure_service_reloads_metadata_after_waiting_for_lifecycle_lock(
    config_path: Path,
    monkeypatch,
) -> None:
    config = _write_component(config_path)
    manager = RerankerComponentManager(config)
    old_metadata = manager._metadata()
    assert old_metadata is not None
    replacement_runtime_name = f"reranker-{RERANKER_COMPONENT_VERSION}-{'3' * 32}"
    replacement_model_name = f"bge-reranker-v2-m3-{'4' * 32}"
    replacement_runtime = manager.root / "runtime" / replacement_runtime_name
    replacement_model = manager.root / "models" / replacement_model_name
    _python(replacement_runtime).parent.mkdir(parents=True)
    _python(replacement_runtime).write_text("replacement", encoding="utf-8")
    replacement_model.mkdir(parents=True)
    replacement_metadata = {
        **old_metadata,
        "runtime": f"runtime/{replacement_runtime_name}",
        "model_path": f"models/{replacement_model_name}",
        "token": "y" * 48,
    }

    class Client:
        def __init__(self, token: str) -> None:
            self.token = token

        def health(self, **_kwargs) -> bool:
            return False

    old_client = Client("x" * 48)
    replacement_client = Client("y" * 48)
    manager.client = lambda metadata=None: (
        replacement_client if (metadata or {}).get("token") == "y" * 48 else old_client
    )
    launched: list[tuple[dict[str, object], Client]] = []

    def start_service(metadata, client):
        launched.append((metadata, client))
        return client

    manager._start_service = start_service

    class SwitchingLock:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            manager._atomic_json_write(manager.metadata_path, replacement_metadata)
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_component.UpdateLock",
        SwitchingLock,
    )

    result = manager.ensure_service()

    assert result is replacement_client
    assert launched == [(replacement_metadata, replacement_client)]


def test_service_start_rejects_metadata_replaced_before_persisting_generation(
    config_path: Path,
) -> None:
    config = _write_component(config_path)
    manager = RerankerComponentManager(config)
    stale = manager._metadata()
    assert stale is not None
    replacement = {**stale, "token": "z" * 48}
    manager._atomic_json_write(manager.metadata_path, replacement)

    with pytest.raises(ConfigurationError, match="metadata changed"):
        manager._prepare_service_start(stale)

    assert not manager.service_control_path.exists()


def test_status_accepts_only_the_pinned_component(config_path: Path) -> None:
    config = _write_component(config_path)
    manager = RerankerComponentManager(config)
    manager.client = lambda _metadata=None: ReadyClient()

    status = manager.status(check_service=True)

    assert status.installed is True
    assert status.status == "ready"
    assert status.service_ready is True
    assert status.component_version == RERANKER_COMPONENT_VERSION

    metadata_path = config.reranker_component_path / "current.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["model_revision"] = "stale"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    status = manager.status(check_service=True)
    assert status.status == "update_required"
    assert status.service_ready is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("inference_runtime", "torch==0"),
        ("packages", ["transformers==0"]),
    ],
)
def test_status_requires_the_exact_inference_stack(
    config_path: Path,
    key: str,
    value: object,
) -> None:
    config = _write_component(config_path)
    metadata_path = config.reranker_component_path / "current.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata[key] = value
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    manager = RerankerComponentManager(config)
    manager.client = lambda _metadata=None: (_ for _ in ()).throw(
        AssertionError("an outdated stack must not be contacted")
    )

    status = manager.status(check_service=True)

    assert status.status == "update_required"
    assert status.service_ready is False


def test_status_rejects_paths_outside_component_root(config_path: Path) -> None:
    config = _write_component(config_path)
    metadata_path = config.reranker_component_path / "current.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["model_path"] = "../../outside"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    status = RerankerComponentManager(config).status()

    assert status.installed is False
    assert "escapes" in (status.error or "")


def test_ensure_service_stops_authenticated_stale_worker_before_restart(
    config_path: Path,
) -> None:
    config = _write_component(config_path)
    events: list[str] = []

    class StaleClient:
        def health(self, *, require_current: bool = True, **_kwargs) -> bool:
            return not require_current

    client = StaleClient()
    manager = RerankerComponentManager(config)
    manager.client = lambda _metadata=None: client
    manager._stop_service_locked = lambda **_kwargs: events.append("stop")
    manager._start_service = lambda _metadata, selected: events.append("start") or selected

    assert manager.ensure_service() is client
    assert events == ["stop", "start"]


def test_install_activates_only_after_pinned_download_and_live_check(
    config_path: Path, monkeypatch
) -> None:
    config = load_config(config_path)

    def create_environment(_builder, runtime) -> None:
        executable = _python(Path(runtime))
        executable.parent.mkdir(parents=True)
        executable.write_text("placeholder", encoding="utf-8")

    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        rendered = [str(item) for item in command]
        calls.append(rendered)
        if "snapshot_download" in " ".join(rendered):
            model = Path(rendered[5])
            model.mkdir(parents=True)
            for name in RERANKER_MODEL_FILES:
                (model / name).write_text(name, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ready", stderr="")

    monkeypatch.setattr(venv.EnvBuilder, "create", create_environment)
    manager = RerankerComponentManager(config, runner=runner)
    manager.client = lambda _metadata=None: ReadyClient()

    def start_service(metadata, client):
        assert manager.pending_metadata_path.is_file()
        assert not manager.metadata_path.exists()
        if os.name != "nt":
            assert manager.pending_metadata_path.stat().st_mode & 0o777 == 0o600
        manager._prepare_service_start(metadata)
        return client

    manager._start_service = start_service

    status = manager.install()

    assert status.status == "ready"
    assert status.service_ready is True
    assert len(calls) == 4
    assert calls[0][-1].endswith("pip-bootstrap.txt")
    assert "--find-links" not in calls[0]
    assert calls[1][-1].endswith("reranker-component.txt")
    assert calls[1][calls[1].index("--find-links") + 1] == RERANKER_TORCH_FIND_LINKS
    assert RERANKER_MODEL_ID in calls[2]
    assert RERANKER_MODEL_REVISION in calls[2]
    assert "--self-test" in calls[3]
    metadata = json.loads(manager.metadata_path.read_text(encoding="utf-8"))
    assert metadata["model_revision"] == RERANKER_MODEL_REVISION
    assert not manager.pending_metadata_path.exists()


def test_failed_self_test_does_not_activate_candidate(config_path: Path, monkeypatch) -> None:
    config = load_config(config_path)

    def create_environment(_builder, runtime) -> None:
        executable = _python(Path(runtime))
        executable.parent.mkdir(parents=True)
        executable.write_text("placeholder", encoding="utf-8")

    def runner(command, **_kwargs):
        rendered = [str(item) for item in command]
        if "snapshot_download" in " ".join(rendered):
            model = Path(rendered[5])
            model.mkdir(parents=True)
            for name in RERANKER_MODEL_FILES:
                (model / name).write_text(name, encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            1 if "--self-test" in rendered else 0,
            stdout="",
            stderr="failed",
        )

    monkeypatch.setattr(venv.EnvBuilder, "create", create_environment)
    manager = RerankerComponentManager(config, runner=runner)

    with pytest.raises(ConfigurationError, match="self-test"):
        manager.install()

    assert not manager.metadata_path.exists()
    assert not list((manager.root / "runtime").iterdir())
    assert not list((manager.root / "models").iterdir())


def test_install_replaces_structurally_valid_metadata_with_missing_payloads(
    config_path: Path,
    monkeypatch,
) -> None:
    config = load_config(config_path)
    root = config.reranker_component_path
    root.mkdir(parents=True)
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component_version": RERANKER_COMPONENT_VERSION,
                "runtime": f"runtime/{_MISSING_RUNTIME_NAME}",
                "model_path": f"models/{_MISSING_MODEL_NAME}",
                "model_id": RERANKER_MODEL_ID,
                "model_revision": RERANKER_MODEL_REVISION,
                "host": "127.0.0.1",
                "port": 8768,
                "token": "x" * 48,
            }
        ),
        encoding="utf-8",
    )

    def create_environment(_builder, runtime) -> None:
        executable = _python(Path(runtime))
        executable.parent.mkdir(parents=True)
        executable.write_text("placeholder", encoding="utf-8")

    def runner(command, **_kwargs):
        rendered = [str(item) for item in command]
        if "snapshot_download" in " ".join(rendered):
            model = Path(rendered[5])
            model.mkdir(parents=True)
            for name in RERANKER_MODEL_FILES:
                (model / name).write_text(name, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ready", stderr="")

    monkeypatch.setattr(venv.EnvBuilder, "create", create_environment)
    manager = RerankerComponentManager(config, runner=runner)
    manager.client = lambda _metadata=None: ReadyClient()
    manager._start_service = lambda metadata, client: (
        manager._prepare_service_start(metadata) and client
    )

    damaged = manager.status()

    assert damaged.status == "error"
    assert damaged.installed is False
    assert damaged.removable is True
    assert (damaged.installed_bytes or 0) > 0

    status = manager.install()

    assert status.status == "ready"
    assert status.service_ready is True
    metadata = json.loads(manager.metadata_path.read_text(encoding="utf-8"))
    assert Path(str(metadata["runtime"])).name != _MISSING_RUNTIME_NAME
    assert Path(str(metadata["model_path"])).name != _MISSING_MODEL_NAME
    assert "previous_runtime" not in metadata
    assert "previous_model_path" not in metadata


def test_remove_accepts_managed_storage_with_missing_active_payloads(config_path: Path) -> None:
    config = load_config(config_path)
    root = config.reranker_component_path
    root.mkdir(parents=True)
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component_version": RERANKER_COMPONENT_VERSION,
                "runtime": f"runtime/{_MISSING_RUNTIME_NAME}",
                "model_path": f"models/{_MISSING_MODEL_NAME}",
                "model_id": RERANKER_MODEL_ID,
                "model_revision": RERANKER_MODEL_REVISION,
                "host": "127.0.0.1",
                "port": 8768,
                "token": "x" * 48,
            }
        ),
        encoding="utf-8",
    )
    manager = RerankerComponentManager(config)
    manager.stop_service = lambda: None

    reclaimed = manager.remove()

    assert reclaimed > 0
    assert not root.exists()


def test_remove_deletes_only_validated_managed_component(config_path: Path) -> None:
    config = _write_component(config_path)
    marker = config.reranker_component_path / "model.bin"
    marker.write_bytes(b"model")
    manager = RerankerComponentManager(config)
    manager.stop_service = lambda: None

    reclaimed = manager.remove()

    assert reclaimed >= len(b"model")
    assert not config.reranker_component_path.exists()


def test_remove_rejects_external_link_without_touching_target(
    config_path: Path, tmp_path: Path
) -> None:
    config = _write_component(config_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = config.reranker_component_path / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    manager = RerankerComponentManager(config)
    manager.stop_service = lambda: None

    with pytest.raises(ConfigurationError, match="containing a link"):
        manager.remove()

    assert outside.read_text(encoding="utf-8") == "keep"
    assert config.reranker_component_path.exists()


def test_cleanup_removes_only_unreferenced_candidates(config_path: Path) -> None:
    config = _write_component(config_path)
    root = config.reranker_component_path
    previous_runtime_name = f"reranker-1-{'3' * 32}"
    previous_model_name = f"bge-reranker-v2-m3-{'4' * 32}"
    previous_runtime = root / "runtime" / previous_runtime_name
    previous_model = root / "models" / previous_model_name
    previous_runtime.mkdir(parents=True)
    previous_model.mkdir(parents=True)
    metadata = json.loads((root / "current.json").read_text(encoding="utf-8"))
    metadata["previous_runtime"] = f"runtime/{previous_runtime_name}"
    metadata["previous_model_path"] = f"models/{previous_model_name}"
    (root / "current.json").write_text(json.dumps(metadata), encoding="utf-8")
    orphan_runtime = root / f"runtime/reranker-1-{'a' * 32}"
    orphan_model = root / f"models/bge-reranker-v2-m3-{'b' * 32}"
    orphan_runtime.mkdir()
    orphan_model.mkdir()
    (orphan_runtime / "payload").write_bytes(b"runtime")
    (orphan_model / "payload").write_bytes(b"model")
    manager = RerankerComponentManager(config)
    manager._port_accepting = lambda: False

    result = manager.cleanup_incomplete_candidates()

    assert result.reclaimed_bytes == len(b"runtime") + len(b"model")
    assert result.deferred_candidates == 0
    assert not orphan_runtime.exists()
    assert not orphan_model.exists()
    assert (root / "runtime" / _TEST_RUNTIME_NAME).exists()
    assert (root / "models" / _TEST_MODEL_NAME).exists()
    assert previous_runtime.exists()
    assert previous_model.exists()


def test_cleanup_preserves_unrecognized_prefixed_directories(config_path: Path) -> None:
    config = _write_component(config_path)
    root = config.reranker_component_path
    unknown = root / "runtime/reranker-user-files"
    unknown.mkdir(parents=True)
    (unknown / "keep").write_text("user-owned", encoding="utf-8")
    manager = RerankerComponentManager(config)
    manager._port_accepting = lambda: False

    result = manager.cleanup_incomplete_candidates()

    assert result.reclaimed_bytes == 0
    assert (unknown / "keep").read_text(encoding="utf-8") == "user-owned"


def test_cleanup_defers_recent_candidates(config_path: Path) -> None:
    config = load_config(config_path)
    candidate = config.reranker_component_path / f"runtime/reranker-1-{'c' * 32}"
    candidate.mkdir(parents=True)
    (candidate / "payload").write_bytes(b"partial")
    manager = RerankerComponentManager(config)
    manager._port_accepting = lambda: False

    result = manager.cleanup_incomplete_candidates(minimum_age_seconds=3_600)

    assert result.reclaimed_bytes == 0
    assert result.deferred_candidates == 1
    assert candidate.exists()


def test_cleanup_rejects_linked_candidate_without_touching_target(
    config_path: Path,
    tmp_path: Path,
) -> None:
    config = load_config(config_path)
    runtime_parent = config.reranker_component_path / "runtime"
    runtime_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep", encoding="utf-8")
    candidate = runtime_parent / f"reranker-1-{'d' * 32}"
    try:
        candidate.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    manager = RerankerComponentManager(config)
    manager._port_accepting = lambda: False

    with pytest.raises(ConfigurationError, match="unexpected reranker component path"):
        manager.cleanup_incomplete_candidates()

    assert (outside / "keep").read_text(encoding="utf-8") == "keep"


def test_stop_publishes_cancellation_before_waiting_for_service_lock(
    config_path: Path,
) -> None:
    config = _write_component(config_path)
    manager = RerankerComponentManager(config)
    failures: list[BaseException] = []

    def stop() -> None:
        try:
            manager.stop_service()
        except BaseException as exc:  # pragma: no cover - assertion reports thread failure
            failures.append(exc)

    with UpdateLock(manager.service_lock_path):
        thread = threading.Thread(target=stop)
        thread.start()
        deadline = time.monotonic() + 2
        while not manager.service_stop_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager.service_stop_path.is_file()
        # A newer enable supersedes the queued stop before it acquires the lock.
        manager.request_service_start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures == []
    assert not manager.service_stop_path.exists()


def test_interrupted_activation_keeps_recoverable_pending_metadata(
    config_path: Path,
    monkeypatch,
) -> None:
    config = load_config(config_path)

    def create_environment(_builder, runtime) -> None:
        executable = _python(Path(runtime))
        executable.parent.mkdir(parents=True)
        executable.write_text("placeholder", encoding="utf-8")

    def runner(command, **_kwargs):
        rendered = [str(item) for item in command]
        if "snapshot_download" in " ".join(rendered):
            model = Path(rendered[5])
            model.mkdir(parents=True)
            for name in RERANKER_MODEL_FILES:
                (model / name).write_text(name, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ready", stderr="")

    monkeypatch.setattr(venv.EnvBuilder, "create", create_environment)
    manager = RerankerComponentManager(config, runner=runner)
    manager.client = lambda _metadata=None: ReadyClient()

    def interrupted_start(metadata, _client):
        manager._prepare_service_start(metadata)
        raise KeyboardInterrupt

    manager._start_service = interrupted_start

    with pytest.raises(KeyboardInterrupt):
        manager.install()

    pending = manager._pending_metadata()
    assert pending is not None
    assert len(str(pending["token"])) >= 32
    assert manager.pending_metadata_path.is_file()
    assert not manager.metadata_path.exists()

    class OrphanClient:
        running = True

        def health(self, **_kwargs) -> bool:
            return self.running

        def shutdown(self) -> None:
            self.running = False

    orphan = OrphanClient()
    manager.client = lambda _metadata=None: orphan
    manager._port_accepting = lambda: orphan.running
    manager.stop_service()
    result = manager.cleanup_incomplete_candidates()

    assert orphan.running is False
    assert result.deferred_candidates == 0
    assert not manager.pending_metadata_path.exists()
    assert not list((manager.root / "runtime").iterdir())
    assert not list((manager.root / "models").iterdir())


def test_cancellation_before_candidate_promotion_never_activates(
    config_path: Path,
    monkeypatch,
) -> None:
    config = load_config(config_path)

    def create_environment(_builder, runtime) -> None:
        executable = _python(Path(runtime))
        executable.parent.mkdir(parents=True)
        executable.write_text("placeholder", encoding="utf-8")

    def runner(command, **_kwargs):
        rendered = [str(item) for item in command]
        if "snapshot_download" in " ".join(rendered):
            model = Path(rendered[5])
            model.mkdir(parents=True)
            for name in RERANKER_MODEL_FILES:
                (model / name).write_text(name, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ready", stderr="")

    monkeypatch.setattr(venv.EnvBuilder, "create", create_environment)
    manager = RerankerComponentManager(config, runner=runner)
    manager.client = lambda _metadata=None: ReadyClient()

    def cancel_after_start(metadata, client):
        manager._prepare_service_start(metadata)
        manager.request_service_stop()
        return client

    manager._start_service = cancel_after_start

    with pytest.raises(ConfigurationError, match="activation was cancelled"):
        manager.install()

    assert not manager.metadata_path.exists()
    assert not manager.pending_metadata_path.exists()
    assert not list((manager.root / "runtime").iterdir())
    assert not list((manager.root / "models").iterdir())


def test_service_log_uses_configured_retained_errors_directory(
    config_path: Path,
    monkeypatch,
) -> None:
    config = _write_component(config_path)
    manager = RerankerComponentManager(config)
    metadata = manager._metadata()
    assert metadata is not None
    managed_metadata = config.base_dir / "runtime/installation.json"
    managed_metadata.parent.mkdir(parents=True, exist_ok=True)
    managed_metadata.write_text(
        json.dumps({"schema_version": 2, "active_version": "1.25.0"}),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class Process:
        pid = 123

        @staticmethod
        def wait() -> int:
            return 0

        @staticmethod
        def poll() -> None:
            return None

    def popen(command, *, stderr, **_kwargs):
        captured["command"] = command
        captured["stderr"] = Path(stderr.name)
        return Process()

    def wait_for_service(client, **kwargs):
        captured["log_path"] = kwargs["log_path"]
        return client

    monkeypatch.setattr(subprocess, "Popen", popen)
    manager._wait_for_service = wait_for_service

    manager._start_service(metadata, ReadyClient())

    expected = config.resolve_path(config.paths.errors) / "reranker-service.log"
    assert captured["stderr"] == expected
    assert captured["log_path"] == expected
    assert not (manager.root / "reranker-service.log").exists()
    assert "--control-path" in captured["command"]
    assert "--generation" in captured["command"]
    assert "--managed-installation-path" in captured["command"]
    assert str(managed_metadata) in captured["command"]
    assert "--managed-version" in captured["command"]
