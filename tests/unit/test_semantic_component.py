from __future__ import annotations

import json
import os
import subprocess
import venv
from pathlib import Path

import pytest

from helix_mcp_knowledge import __version__
from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.errors import ConfigurationError
from helix_mcp_knowledge.managed_installation import installation_metadata_path
from helix_mcp_knowledge.semantic_component import (
    SEMANTIC_COMPONENT_VERSION,
    SEMANTIC_MODEL_REVISION,
    SEMANTIC_PACKAGES,
    SEMANTIC_TORCH_FIND_LINKS,
    SEMANTIC_TORCH_PACKAGE,
    SemanticComponentManager,
)
from helix_mcp_knowledge.update_lock import UpdateLock


def _python(runtime: Path) -> Path:
    return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _write_component(config_path: Path) -> object:
    config = load_config(config_path)
    runtime = config.semantic_component_path / f"runtime/semantic-{SEMANTIC_COMPONENT_VERSION}-test"
    model = config.semantic_component_path / "models/bge-m3-test"
    _python(runtime).parent.mkdir(parents=True)
    _python(runtime).write_text("placeholder", encoding="utf-8")
    model.mkdir(parents=True)
    config.semantic_component_path.mkdir(parents=True, exist_ok=True)
    (config.semantic_component_path / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component_version": SEMANTIC_COMPONENT_VERSION,
                "runtime": f"runtime/semantic-{SEMANTIC_COMPONENT_VERSION}-test",
                "model_path": "models/bge-m3-test",
                "vector_path": "vectors",
                "model_id": config.embeddings.model,
                "model_revision": SEMANTIC_MODEL_REVISION,
                "packages": list(SEMANTIC_PACKAGES),
                "inference_runtime": SEMANTIC_TORCH_PACKAGE,
                "host": "127.0.0.1",
                "port": 8767,
                "token": "x" * 48,
                "installed_at": "2026-09-06T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return config


def _fake_install_runner(monkeypatch, events: list[str] | None = None):
    def create_environment(_builder, runtime) -> None:
        executable = _python(Path(runtime))
        executable.parent.mkdir(parents=True)
        executable.write_text("placeholder", encoding="utf-8")

    def runner(command, **_kwargs):
        rendered = [str(item) for item in command]
        if "snapshot_download" in " ".join(rendered):
            Path(rendered[-1]).mkdir(parents=True)
        if events is not None and "--self-test" in rendered:
            events.append("self-test")
        return subprocess.CompletedProcess(command, 0, stdout="ready", stderr="")

    monkeypatch.setattr(venv.EnvBuilder, "create", create_environment)
    return runner


def _write_pending_promotion(config_path: Path):
    config = _write_component(config_path)
    manager = SemanticComponentManager(config)
    previous = manager._metadata()
    assert previous is not None
    previous["component_version"] = SEMANTIC_COMPONENT_VERSION - 1
    manager._atomic_json_write(manager.metadata_path, previous)
    suffix = "a" * 32
    runtime = manager.root / f"runtime/semantic-{SEMANTIC_COMPONENT_VERSION}-{suffix}"
    model = manager.root / f"models/bge-m3-{suffix}"
    _python(runtime).parent.mkdir(parents=True)
    _python(runtime).write_text("placeholder", encoding="utf-8")
    model.mkdir(parents=True)
    candidate = {
        **previous,
        "component_version": SEMANTIC_COMPONENT_VERSION,
        "runtime": runtime.relative_to(manager.root).as_posix(),
        "model_path": model.relative_to(manager.root).as_posix(),
        "token": "c" * 48,
    }
    journal = manager._promotion_journal(
        candidate_metadata=candidate,
        previous_metadata=previous,
        previous_was_live=True,
    )
    manager._atomic_json_write(manager.pending_metadata_path, journal)
    return manager, previous, candidate, journal, runtime, model


def test_component_status_requires_managed_runtime_and_model(config_path: Path) -> None:
    config = _write_component(config_path)

    status = SemanticComponentManager(config).status()

    assert status.installed is True
    assert status.component_version == SEMANTIC_COMPONENT_VERSION
    assert status.service_ready is False


def test_component_status_reports_degraded_when_enabled_service_is_unavailable(
    config_path: Path, monkeypatch
) -> None:
    config = _write_component(config_path)
    manager = SemanticComponentManager(config)
    monkeypatch.setattr(
        manager,
        "client",
        lambda _metadata=None: type("Client", (), {"health": lambda _self: False})(),
    )

    status = manager.status(check_service=True)

    assert status.installed is True
    assert status.status == "degraded"
    assert status.service_ready is False
    assert "lexical" in (status.error or "")


def test_component_status_requires_upgrade_before_using_an_older_runtime(
    config_path: Path,
) -> None:
    config = _write_component(config_path)
    metadata_path = config.semantic_component_path / "current.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["component_version"] = SEMANTIC_COMPONENT_VERSION - 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    status = SemanticComponentManager(config).status(check_service=True)

    assert status.installed is True
    assert status.status == "update_required"
    assert status.service_ready is False
    assert "updated" in (status.error or "")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("model_revision", "stale"),
        ("inference_runtime", "torch==0"),
        ("packages", ["sentence-transformers==0"]),
    ],
)
def test_component_status_requires_the_exact_pinned_stack(
    config_path: Path,
    key: str,
    value: object,
) -> None:
    config = _write_component(config_path)
    metadata_path = config.semantic_component_path / "current.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata[key] = value
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    status = SemanticComponentManager(config).status(check_service=True)

    assert status.installed is True
    assert status.status == "update_required"
    assert status.service_ready is False


def test_service_startup_waits_for_the_process_that_owns_the_start_lock(
    config_path: Path, monkeypatch
) -> None:
    config = _write_component(config_path)
    manager = SemanticComponentManager(config)

    class Client:
        def health(self, **_kwargs) -> bool:
            return False

    client = Client()
    monkeypatch.setattr(manager, "client", lambda _metadata=None: client)
    monkeypatch.setattr(manager, "_wait_for_service_transition", lambda: client)
    monkeypatch.setattr(
        "helix_mcp_knowledge.semantic_component.SEMANTIC_SERVICE_LOCK_TIMEOUT_SECONDS",
        0.0,
    )
    with UpdateLock(manager.root / ".service.lock"):
        resolved = manager.ensure_service()

    assert resolved is client


def test_service_start_reloads_metadata_after_waiting_for_lifecycle_lock(
    config_path: Path,
    monkeypatch,
) -> None:
    config = _write_component(config_path)
    manager = SemanticComponentManager(config)
    previous = manager._metadata()
    assert previous is not None
    replacement = {**previous, "token": "n" * 48}
    starts: list[str] = []

    class Client:
        def __init__(self, token: str) -> None:
            self.token = token

        def health(self, **_kwargs) -> bool:
            return False

    manager.client = lambda metadata=None: Client(str((metadata or manager._metadata())["token"]))
    manager._start_service = lambda _metadata, client: starts.append(client.token) or client

    class SwappingLock:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            manager._atomic_json_write(manager.metadata_path, replacement)
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr("helix_mcp_knowledge.semantic_component.UpdateLock", SwappingLock)

    client = manager.ensure_service()

    assert client.token == "n" * 48
    assert starts == ["n" * 48]


def test_service_start_fences_worker_to_managed_runtime_version(
    config_path: Path,
    monkeypatch,
) -> None:
    config = _write_component(config_path)
    manager = SemanticComponentManager(config)
    metadata = manager._metadata()
    assert metadata is not None
    managed_metadata = installation_metadata_path(config.base_dir)
    managed_metadata.parent.mkdir(parents=True, exist_ok=True)
    managed_metadata.write_text("{}", encoding="utf-8")
    commands: list[list[str]] = []

    class Process:
        @staticmethod
        def wait(*_args, **_kwargs) -> int:
            return 0

    def popen(command, **_kwargs):
        commands.append(command)
        return Process()

    client = object()
    monkeypatch.setattr("helix_mcp_knowledge.semantic_component.subprocess.Popen", popen)
    monkeypatch.setattr(manager, "_wait_for_service", lambda candidate, **_kwargs: candidate)

    assert manager._start_service(metadata, client) is client

    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("--managed-installation-path") + 1] == str(managed_metadata)
    assert command[command.index("--managed-version") + 1] == __version__


def test_component_status_rejects_model_path_escape(config_path: Path) -> None:
    config = _write_component(config_path)
    metadata = json.loads(
        (config.semantic_component_path / "current.json").read_text(encoding="utf-8")
    )
    metadata["model_path"] = "../../outside"
    (config.semantic_component_path / "current.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )

    status = SemanticComponentManager(config).status()

    assert status.installed is False
    assert "escapes" in (status.error or "")


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("host", "localhost", "endpoint"),
        ("port", 9999, "endpoint"),
        ("token", "short", "token"),
    ],
)
def test_component_metadata_requires_managed_endpoint_and_token(
    config_path: Path,
    key: str,
    value: object,
    message: str,
) -> None:
    config = _write_component(config_path)
    metadata_path = config.semantic_component_path / "current.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata[key] = value
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    status = SemanticComponentManager(config).status()

    assert status.installed is False
    assert message in (status.error or "")


def test_remove_rejects_component_outside_workspace(config_path: Path) -> None:
    config = load_config(config_path)
    config._base_dir = Path("/tmp/external-semantic-parent")

    manager = SemanticComponentManager(config)
    manager.root = Path("/tmp/external-semantic")
    manager.stop_service = lambda: None
    try:
        manager.remove()
    except Exception as exc:
        assert "outside the managed workspace" in str(exc)
    else:
        raise AssertionError("external semantic component removal was accepted")


def test_remove_deletes_only_validated_managed_component(config_path: Path) -> None:
    config = _write_component(config_path)
    marker = config.semantic_component_path / "vectors/marker.bin"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"vectors")
    manager = SemanticComponentManager(config)
    manager.stop_service = lambda: None

    reclaimed = manager.remove()

    assert reclaimed >= len(b"vectors")
    assert not config.semantic_component_path.exists()


def test_remove_accepts_standard_internal_venv_lib64_link(config_path: Path) -> None:
    config = _write_component(config_path)
    runtime = config.semantic_component_path / f"runtime/semantic-{SEMANTIC_COMPONENT_VERSION}-test"
    library = runtime / "lib"
    library.mkdir()
    link = runtime / "lib64"
    try:
        link.symlink_to("lib", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    manager = SemanticComponentManager(config)
    manager.stop_service = lambda: None

    reclaimed = manager.remove()

    assert reclaimed > 0
    assert not config.semantic_component_path.exists()


def test_remove_rejects_other_links_without_touching_target(
    config_path: Path, tmp_path: Path
) -> None:
    config = _write_component(config_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = config.semantic_component_path / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    manager = SemanticComponentManager(config)
    manager.stop_service = lambda: None

    with pytest.raises(ConfigurationError, match="containing a link"):
        manager.remove()

    assert outside.read_text(encoding="utf-8") == "keep"
    assert config.semantic_component_path.exists()


def test_install_activates_metadata_only_after_download_and_self_test(
    config_path: Path, monkeypatch
) -> None:
    config = load_config(config_path)

    def create_environment(builder, runtime):
        executable = _python(Path(runtime))
        executable.parent.mkdir(parents=True)
        executable.write_text("placeholder", encoding="utf-8")

    calls: list[list[str]] = []

    def runner(command, **kwargs):
        rendered = [str(item) for item in command]
        calls.append(rendered)
        if "snapshot_download" in " ".join(rendered):
            Path(rendered[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, stdout="ready", stderr="")

    monkeypatch.setattr(venv.EnvBuilder, "create", create_environment)
    manager = SemanticComponentManager(config, runner=runner)
    manager._start_service = lambda _metadata, client: client

    status = manager.install()

    assert status.installed is True
    assert len(calls) == 4
    assert "pip" in calls[0]
    assert "--require-hashes" in calls[0]
    assert "--find-links" not in calls[0]
    assert "pip" in calls[1]
    assert "--require-hashes" in calls[1]
    assert calls[1][calls[1].index("--find-links") + 1] == SEMANTIC_TORCH_FIND_LINKS
    assert calls[1][-1].endswith("semantic-component.txt")
    assert "snapshot_download" in " ".join(calls[2])
    assert "--self-test" in calls[3]


def test_upgrade_stops_previous_runtime_only_after_candidate_self_test(
    config_path: Path,
    monkeypatch,
) -> None:
    config = _write_component(config_path)
    previous = json.loads(
        (config.semantic_component_path / "current.json").read_text(encoding="utf-8")
    )
    previous["component_version"] = SEMANTIC_COMPONENT_VERSION - 1
    (config.semantic_component_path / "current.json").write_text(
        json.dumps(previous), encoding="utf-8"
    )
    events: list[str] = []
    manager = SemanticComponentManager(
        config,
        runner=_fake_install_runner(monkeypatch, events),
    )

    class Client:
        def __init__(self, version: int) -> None:
            self.version = version

        def health(self, **_kwargs) -> bool:
            return True

    manager.client = lambda metadata=None: Client(
        int((metadata or manager._metadata())["component_version"])
    )
    manager._stop_service_client = lambda client: events.append(f"stop:{client.version}")
    manager._start_service = lambda metadata, client: (
        events.append(f"start:{metadata['component_version']}") or client
    )

    status = manager.install()

    assert status.installed is True
    assert events == [
        "self-test",
        f"stop:{SEMANTIC_COMPONENT_VERSION - 1}",
        f"start:{SEMANTIC_COMPONENT_VERSION}",
    ]


def test_failed_upgrade_restores_metadata_and_restarts_previous_runtime(
    config_path: Path,
    monkeypatch,
) -> None:
    config = _write_component(config_path)
    metadata_path = config.semantic_component_path / "current.json"
    previous = json.loads(metadata_path.read_text(encoding="utf-8"))
    previous["component_version"] = SEMANTIC_COMPONENT_VERSION - 1
    metadata_path.write_text(json.dumps(previous), encoding="utf-8")
    events: list[str] = []
    manager = SemanticComponentManager(
        config,
        runner=_fake_install_runner(monkeypatch, events),
    )

    class Client:
        def __init__(self, version: int) -> None:
            self.version = version

        def health(self, **_kwargs) -> bool:
            return self.version != SEMANTIC_COMPONENT_VERSION

    manager.client = lambda metadata=None: Client(
        int((metadata or manager._metadata())["component_version"])
    )
    manager._stop_service_client = lambda client: events.append(f"stop:{client.version}")

    def start_service(metadata, client):
        version = int(metadata["component_version"])
        events.append(f"start:{version}")
        if version == SEMANTIC_COMPONENT_VERSION:
            raise ConfigurationError("candidate failed")
        return client

    manager._start_service = start_service

    with pytest.raises(ConfigurationError, match="candidate failed"):
        manager.install()

    assert manager._metadata() == previous
    assert events == [
        "self-test",
        f"stop:{SEMANTIC_COMPONENT_VERSION - 1}",
        f"start:{SEMANTIC_COMPONENT_VERSION}",
        f"start:{SEMANTIC_COMPONENT_VERSION - 1}",
    ]
    runtimes = list((manager.root / "runtime").iterdir())
    models = list((manager.root / "models").iterdir())
    assert [path.name for path in runtimes] == [f"semantic-{SEMANTIC_COMPONENT_VERSION}-test"]
    assert [path.name for path in models] == ["bge-m3-test"]


def test_failed_first_activation_removes_candidate_metadata(
    config_path: Path,
    monkeypatch,
) -> None:
    config = load_config(config_path)
    manager = SemanticComponentManager(
        config,
        runner=_fake_install_runner(monkeypatch),
    )
    manager._start_service = lambda _metadata, _client: (_ for _ in ()).throw(
        ConfigurationError("candidate failed")
    )

    with pytest.raises(ConfigurationError, match="candidate failed"):
        manager.install()

    assert not manager.metadata_path.exists()
    assert not list((manager.root / "runtime").iterdir())
    assert not list((manager.root / "models").iterdir())


def test_interrupted_promotion_adopts_an_authenticated_live_candidate(
    config_path: Path,
) -> None:
    manager, previous, candidate, _journal, runtime, model = _write_pending_promotion(config_path)

    class Client:
        def __init__(self, token: str) -> None:
            self.token = token

        def health(self, **_kwargs) -> bool:
            return self.token == candidate["token"]

    manager.client = lambda metadata=None: Client(str((metadata or manager._metadata())["token"]))
    manager._start_service = lambda *_args: pytest.fail("live candidate was restarted")

    assert manager.recover_interrupted_promotion() is True

    assert manager._metadata() == candidate
    assert not manager.pending_metadata_path.exists()
    assert runtime.exists()
    assert model.exists()
    assert previous != candidate


def test_interrupted_promotion_rolls_back_when_candidate_cannot_start(
    config_path: Path,
) -> None:
    manager, previous, candidate, _journal, runtime, model = _write_pending_promotion(config_path)
    starts: list[str] = []

    class Client:
        def __init__(self, token: str) -> None:
            self.token = token

        def health(self, **_kwargs) -> bool:
            return False

    manager.client = lambda metadata=None: Client(str((metadata or manager._metadata())["token"]))

    def start_service(metadata, _client):
        token = str(metadata["token"])
        starts.append(token)
        if token == candidate["token"]:
            raise ConfigurationError("candidate remains unavailable")
        return object()

    manager._start_service = start_service

    with pytest.raises(ConfigurationError, match="could not be activated"):
        manager.recover_interrupted_promotion()

    assert manager._metadata() == previous
    assert starts == [str(candidate["token"]), str(previous["token"])]
    assert not manager.pending_metadata_path.exists()
    assert not runtime.exists()
    assert not model.exists()


def test_interrupted_promotion_journal_is_cleared_after_current_was_committed(
    config_path: Path,
) -> None:
    manager, _previous, candidate, _journal, _runtime, _model = _write_pending_promotion(
        config_path
    )
    manager._atomic_json_write(manager.metadata_path, candidate)

    class Client:
        @staticmethod
        def health(**_kwargs) -> bool:
            return True

    manager.client = lambda _metadata=None: Client()

    assert manager.recover_interrupted_promotion() is True
    assert manager._metadata() == candidate
    assert not manager.pending_metadata_path.exists()
