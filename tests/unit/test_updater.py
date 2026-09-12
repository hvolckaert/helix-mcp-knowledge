from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from helix_mcp_knowledge import updater
from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.config import AppConfig, load_config
from helix_mcp_knowledge.errors import KnowledgeError
from helix_mcp_knowledge.managed_installation import (
    activate_managed_installation,
    load_managed_installation,
    stable_dashboard_launcher_path,
    stable_launcher_path,
)
from helix_mcp_knowledge.openclaw import EXPOSED_TOOLS, openclaw_stdio_invocation
from helix_mcp_knowledge.updater import update_installation

CURRENT_VERSION = "1.0.2"
TARGET_VERSION = "1.1.0"
WHEEL_BYTES = b"verified wheel payload"
WHEEL_SHA256 = hashlib.sha256(WHEEL_BYTES).hexdigest()
REQUIREMENTS_BYTES = b"pydantic==2.12.5 --hash=sha256:" + b"0" * 64 + b"\n"
REQUIREMENTS_SHA256 = hashlib.sha256(REQUIREMENTS_BYTES).hexdigest()


def _assert_stable_stdio_definition(definition: dict[str, object], workspace: Path) -> None:
    command, arguments = openclaw_stdio_invocation(stable_launcher_path(workspace))
    assert definition["command"] == str(command)
    assert definition["args"] == list(arguments)


class FakeUpdateRunner:
    def __init__(
        self,
        *,
        workspace: Path,
        config_path: Path,
        openclaw: Path,
        previous_server: Path,
        fail_probe: bool = False,
        corrupt_download: bool = False,
        mutate_during_smoke: bool = False,
        fail_switch_after_write: bool = False,
        target_tools: tuple[str, ...] = EXPOSED_TOOLS,
    ) -> None:
        self.workspace = workspace
        self.config_path = config_path
        self.openclaw = openclaw
        self.previous_server = previous_server
        self.fail_probe = fail_probe
        self.corrupt_download = corrupt_download
        self.mutate_during_smoke = mutate_during_smoke
        self.fail_switch_after_write = fail_switch_after_write
        self.target_tools = target_tools
        self.calls: list[list[str]] = []
        self.definitions: list[dict[str, object]] = []
        self.previous_definition = {
            "command": str(previous_server),
            "cwd": str(workspace),
            "env": {"HELIX_KNOWLEDGE_CONFIG": str(config_path)},
            "toolFilter": {"include": ["search_docs", "list_versions"]},
            "connectTimeout": 30,
            "timeout": 60,
        }

    def __call__(self, command, **kwargs):
        rendered = [str(item) for item in command]
        if os.name == "nt" and "-File" in rendered:
            file_index = rendered.index("-File")
            if Path(rendered[file_index + 1]) == self.openclaw.with_suffix(".ps1"):
                rendered = [str(self.openclaw), *rendered[file_index + 2 :]]
        self.calls.append(rendered)
        stdout = ""
        returncode = 0

        if rendered[1:3] == ["release", "view"]:
            stdout = json.dumps(
                {
                    "tagName": f"v{TARGET_VERSION}",
                    "isDraft": False,
                    "isPrerelease": False,
                    "assets": [
                        {
                            "name": (f"helix_mcp_knowledge-{TARGET_VERSION}-py3-none-any.whl"),
                            "digest": f"sha256:{WHEEL_SHA256}",
                        },
                        {
                            "name": "runtime-requirements.txt",
                            "digest": f"sha256:{REQUIREMENTS_SHA256}",
                        },
                    ],
                }
            )
        elif rendered[1:3] == ["release", "download"]:
            destination = Path(rendered[rendered.index("--dir") + 1])
            asset_name = rendered[rendered.index("--pattern") + 1]
            asset = destination / asset_name
            asset.write_bytes(
                b"corrupt"
                if self.corrupt_download and asset_name.endswith(".whl")
                else REQUIREMENTS_BYTES
                if asset_name == "runtime-requirements.txt"
                else WHEEL_BYTES
            )
        elif rendered[0] == str(self.openclaw) and rendered[1:4] == [
            "mcp",
            "show",
            "helix_knowledge",
        ]:
            stdout = json.dumps(self.previous_definition)
        elif rendered[1:3] == ["-m", "venv"]:
            venv = Path(rendered[3])
            executable_dir = venv / ("Scripts" if os.name == "nt" else "bin")
            executable_dir.mkdir(parents=True)
            suffix = ".exe" if os.name == "nt" else ""
            for name in (
                f"python{suffix}",
                f"helix-mcp-knowledge{suffix}",
                f"helix-mcp-knowledge-server{suffix}",
            ):
                (executable_dir / name).write_text("executable", encoding="utf-8")
        elif len(rendered) > 1 and rendered[1] == "-c":
            stdout = (
                json.dumps(self.target_tools)
                if "EXPOSED_TOOLS" in rendered[2]
                else f"{TARGET_VERSION}\n"
            )
        elif rendered[-1] == "smoke-test":
            if self.mutate_during_smoke:
                self.config_path.write_text("mutated", encoding="utf-8")
                database = self.workspace / "data/sqlite/test.db"
                with sqlite3.connect(database) as connection:
                    connection.execute("CREATE TABLE update_mutation(value TEXT)")
                    connection.commit()
            stdout = json.dumps(
                {
                    "status": "pass",
                    "summary": {"passed": 6, "failed": 0, "skipped": 1},
                }
            )
        elif rendered[0] == str(self.openclaw) and rendered[1:3] == ["mcp", "set"]:
            definition = json.loads(rendered[4])
            self.definitions.append(definition)
            if self.fail_switch_after_write and len(self.definitions) == 1:
                returncode = 5
        elif rendered[0] == str(self.openclaw) and rendered[1:3] == ["mcp", "probe"]:
            active_definition = (
                self.definitions[-1] if self.definitions else self.previous_definition
            )
            target_server = Path(str(active_definition["command"]))
            target_arguments = [str(value) for value in active_definition.get("args") or []]
            tool_filter = active_definition.get("toolFilter")
            included = tool_filter.get("include") if isinstance(tool_filter, dict) else None
            exposed_tools = (
                [tool for tool in self.target_tools if tool in included]
                if isinstance(included, list)
                else list(self.target_tools)
            )
            stdout = json.dumps(
                {
                    "servers": {
                        "helix_knowledge": {
                            "launch": " ".join(
                                [str(target_server), *target_arguments, f"(cwd={self.workspace})"]
                            )
                        }
                    },
                    "tools": [f"helix_knowledge__{tool}" for tool in exposed_tools],
                    "diagnostics": ["probe failed"] if self.fail_probe else [],
                }
            )

        return subprocess.CompletedProcess(rendered, returncode, stdout=stdout, stderr="")


def _managed_installation(config_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    workspace = config_path.parent.parent
    application = KnowledgeApplication.from_config(config_path)
    assert application.database.path.is_file()
    executable_dir = (
        workspace / "runtime" / CURRENT_VERSION / "venv" / ("Scripts" if os.name == "nt" else "bin")
    )
    executable_dir.mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    current_python = executable_dir / f"python{suffix}"
    current_server = executable_dir / f"helix-mcp-knowledge-server{suffix}"
    current_python.write_text("python", encoding="utf-8")
    current_server.write_text("server", encoding="utf-8")
    base_python = config_path.parent.parent / f"base-python{suffix}"
    gh = config_path.parent.parent / f"gh{'.exe' if os.name == 'nt' else ''}"
    openclaw = config_path.parent.parent / f"openclaw{'.cmd' if os.name == 'nt' else ''}"
    for command in (base_python, gh, openclaw):
        command.write_text("command", encoding="utf-8")
    if os.name == "nt":
        openclaw.with_suffix(".ps1").write_text("exit 0\n", encoding="utf-8")
    return current_python, current_server, base_python, gh, openclaw


def test_downgrade_stops_a_reranker_worker_the_target_cannot_manage(
    config_path: Path, monkeypatch
) -> None:
    stopped: list[Path] = []

    class Manager:
        def __init__(self, config: AppConfig) -> None:
            self.config = config

        def stop_service(self) -> None:
            stopped.append(self.config.reranker_component_path)

    monkeypatch.setattr("helix_mcp_knowledge.reranker_component.RerankerComponentManager", Manager)
    config = load_config(config_path)

    updater._stop_unsupported_optional_services_for_downgrade(
        config=config,
        current_version=(1, 25, 0),
        target_version=(1, 24, 3),
    )
    updater._stop_unsupported_optional_services_for_downgrade(
        config=config,
        current_version=(1, 25, 0),
        target_version=(1, 26, 0),
    )

    assert stopped == [config.reranker_component_path]


def test_failed_downgrade_restores_the_enabled_reranker_service(
    config_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setitem(globals(), "CURRENT_VERSION", "1.25.0")
    monkeypatch.setitem(globals(), "TARGET_VERSION", "1.24.3")
    configured = config_path.read_text(encoding="utf-8").replace(
        "reranker:\n    enabled: false",
        "reranker:\n    enabled: true",
    )
    config_path.write_text(configured, encoding="utf-8")
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    runner = FakeUpdateRunner(
        workspace=config_path.parent.parent,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
        fail_probe=True,
    )
    events: list[str] = []

    class Status:
        installed = True
        status = "ready"

    class Manager:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def stop_service(self) -> None:
            events.append("stop")

        def request_service_start(self) -> None:
            events.append("request_start")

        def status(self) -> Status:
            events.append("status")
            return Status()

        def ensure_service(self) -> None:
            events.append("ensure")

    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_component.RerankerComponentManager",
        Manager,
    )

    with pytest.raises(KnowledgeError, match="rollback succeeded"):
        update_installation(
            config_path=config_path,
            openclaw_command=openclaw,
            gh_command=gh,
            runner=runner,
            current_version=CURRENT_VERSION,
            current_python=current_python,
            base_python=base_python,
            allow_downgrade=True,
        )

    assert events == ["stop", "request_start", "status", "ensure"]


def test_failed_downgrade_before_service_stop_preserves_existing_stop_intent(
    config_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setitem(globals(), "CURRENT_VERSION", "1.25.0")
    monkeypatch.setitem(globals(), "TARGET_VERSION", "1.24.3")
    configured = config_path.read_text(encoding="utf-8").replace(
        "reranker:\n    enabled: false",
        "reranker:\n    enabled: true",
    )
    config_path.write_text(configured, encoding="utf-8")
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    runner = FakeUpdateRunner(
        workspace=config_path.parent.parent,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )
    events: list[str] = []

    class Manager:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def stop_service(self) -> None:
            events.append("stop")

        def request_service_start(self) -> None:
            events.append("request_start")

    def fail_smoke(*_args, **_kwargs):
        raise RuntimeError("smoke failed before service stop")

    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_component.RerankerComponentManager",
        Manager,
    )
    monkeypatch.setattr(updater, "_run_smoke_test", fail_smoke)

    with pytest.raises(KnowledgeError, match=r"smoke failed.*rollback succeeded"):
        update_installation(
            config_path=config_path,
            openclaw_command=openclaw,
            gh_command=gh,
            runner=runner,
            current_version=CURRENT_VERSION,
            current_python=current_python,
            base_python=base_python,
            allow_downgrade=True,
        )

    assert events == []


def test_update_installs_verified_runtime_and_switches_atomically(config_path: Path) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    old_runtime = workspace / "runtime/0.9.0"
    old_runtime.mkdir(parents=True)
    (old_runtime / "obsolete").write_text("runtime", encoding="utf-8")
    old_backup = workspace / "backups/update-old-success"
    old_backup.mkdir(parents=True)
    (old_backup / "update-result.json").write_text(
        json.dumps({"status": "updated"}), encoding="utf-8"
    )
    old_download = workspace / "downloads/0.9.0"
    old_download.mkdir(parents=True)
    (old_download / "obsolete.whl").write_text("wheel", encoding="utf-8")
    runner = FakeUpdateRunner(
        workspace=workspace,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )

    result = update_installation(
        config_path=config_path,
        openclaw_command=openclaw,
        gh_command=gh,
        runner=runner,
        current_version=CURRENT_VERSION,
        current_python=current_python,
        base_python=base_python,
        clock=lambda: datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
    )

    assert result.status == "updated"
    assert result.target_version == TARGET_VERSION
    assert result.sha256 == WHEEL_SHA256
    assert result.smoke_summary == {"passed": 6, "failed": 0, "skipped": 1}
    assert result.backup == (workspace / "backups/update-20260904T180000Z-1.0.2-to-1.1.0")
    assert (result.backup / "config.yaml").is_file()
    assert (result.backup / "helix_mcp_knowledge.db").is_file()
    assert (result.backup / "openclaw-server.json").is_file()
    assert json.loads((result.backup / "update-result.json").read_text())["status"] == "updated"
    assert len(runner.definitions) == 1
    activated = runner.definitions[0]
    _assert_stable_stdio_definition(activated, workspace)
    assert activated["toolFilter"] == {"include": ["search_docs", "list_versions"]}
    assert activated["env"] == {"HELIX_KNOWLEDGE_CONFIG": str(config_path)}
    assert current_server.is_file()
    assert result.storage_retention is not None
    assert result.storage_retention.status == "completed"
    assert result.storage_retention.removed_runtimes == ["0.9.0"]
    assert result.storage_retention.removed_backups == [old_backup.name]
    assert result.storage_retention.removed_downloads == ["0.9.0"]
    assert not old_runtime.exists()
    assert not old_backup.exists()
    assert not old_download.exists()


def test_update_supports_a_managed_installation_without_openclaw(config_path: Path) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    activate_managed_installation(
        workspace=workspace,
        version=CURRENT_VERSION,
        server_command=current_server,
        config_path=config_path,
        client="standalone",
    )
    runner = FakeUpdateRunner(
        workspace=workspace,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )

    result = update_installation(
        config_path=config_path,
        gh_command=gh,
        runner=runner,
        current_version=CURRENT_VERSION,
        current_python=current_python,
        base_python=base_python,
    )

    assert result.status == "updated"
    assert result.client_integration == "standalone"
    assert result.probed is False
    assert result.reloaded is False
    assert not any(call[0] == str(openclaw) for call in runner.calls)
    assert result.backup is not None
    assert not (result.backup / "openclaw-server.json").exists()
    assert (result.backup / "stable-dashboard-launcher").is_file()
    managed = load_managed_installation(workspace)
    assert managed is not None
    assert managed.active_version == TARGET_VERSION
    assert TARGET_VERSION in managed.launcher.read_text(encoding="utf-8")
    assert TARGET_VERSION in managed.dashboard_launcher.read_text(encoding="utf-8")


def test_update_aborts_if_another_process_changed_the_active_runtime(
    config_path: Path, monkeypatch
) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    activate_managed_installation(
        workspace=workspace,
        version=CURRENT_VERSION,
        server_command=current_server,
        config_path=config_path,
        client="standalone",
    )
    runner = FakeUpdateRunner(
        workspace=workspace,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )

    class RuntimeChangedLock:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self):
            activate_managed_installation(
                workspace=workspace,
                version="1.0.3",
                server_command=current_server,
                config_path=config_path,
                client="standalone",
            )
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr("helix_mcp_knowledge.updater.UpdateLock", RuntimeChangedLock)

    with pytest.raises(KnowledgeError, match="active managed runtime changed"):
        update_installation(
            config_path=config_path,
            gh_command=gh,
            runner=runner,
            current_version=CURRENT_VERSION,
            current_python=current_python,
            base_python=base_python,
        )

    assert not any(call[1:3] == ["release", "download"] for call in runner.calls)


def test_standalone_update_restores_both_launchers_after_dashboard_health_failure(
    config_path: Path,
) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    managed = activate_managed_installation(
        workspace=workspace,
        version=CURRENT_VERSION,
        server_command=current_server,
        config_path=config_path,
        client="standalone",
    )
    original_launcher = managed.launcher.read_bytes()
    original_dashboard_launcher = managed.dashboard_launcher.read_bytes()
    runner = FakeUpdateRunner(
        workspace=workspace,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )

    def fail_dashboard_health(installation) -> None:
        assert installation.active_version == TARGET_VERSION
        assert TARGET_VERSION in installation.launcher.read_text(encoding="utf-8")
        assert TARGET_VERSION in installation.dashboard_launcher.read_text(encoding="utf-8")
        raise RuntimeError("dashboard health failure")

    with pytest.raises(KnowledgeError, match=r"dashboard health failure.*rollback succeeded"):
        update_installation(
            config_path=config_path,
            gh_command=gh,
            runner=runner,
            current_version=CURRENT_VERSION,
            current_python=current_python,
            base_python=base_python,
            post_activation_check=fail_dashboard_health,
        )

    assert managed.launcher.read_bytes() == original_launcher
    assert stable_dashboard_launcher_path(workspace).read_bytes() == original_dashboard_launcher
    restored = load_managed_installation(workspace)
    assert restored is not None
    assert restored.active_version == CURRENT_VERSION
    assert not any(call[0] == str(openclaw) for call in runner.calls)


def test_update_adds_current_public_tool_to_a_complete_legacy_filter(config_path: Path) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    runner = FakeUpdateRunner(
        workspace=config_path.parent.parent,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )
    runner.previous_definition["toolFilter"] = {"include": list(EXPOSED_TOOLS[:-1])}

    result = update_installation(
        config_path=config_path,
        openclaw_command=openclaw,
        gh_command=gh,
        runner=runner,
        current_version=CURRENT_VERSION,
        current_python=current_python,
        base_python=base_python,
    )

    assert result.status == "updated"
    assert runner.definitions[0]["toolFilter"] == {"include": list(EXPOSED_TOOLS)}


def test_update_reads_and_activates_the_target_runtime_tool_contract(config_path: Path) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    target_tools = (*EXPOSED_TOOLS, "future_read_only_tool")
    runner = FakeUpdateRunner(
        workspace=config_path.parent.parent,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
        target_tools=target_tools,
    )
    runner.previous_definition["toolFilter"] = {"include": list(EXPOSED_TOOLS)}

    result = update_installation(
        config_path=config_path,
        openclaw_command=openclaw,
        gh_command=gh,
        runner=runner,
        current_version=CURRENT_VERSION,
        current_python=current_python,
        base_python=base_python,
    )

    assert result.status == "updated"
    assert runner.definitions[0]["toolFilter"] == {"include": list(target_tools)}


def test_update_dry_run_does_not_download_or_mutate(config_path: Path) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    runner = FakeUpdateRunner(
        workspace=config_path.parent.parent,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )

    result = update_installation(
        config_path=config_path,
        openclaw_command=openclaw,
        gh_command=gh,
        runner=runner,
        current_version=CURRENT_VERSION,
        current_python=current_python,
        base_python=base_python,
        dry_run=True,
    )

    assert result.status == "planned"
    assert len(runner.calls) == 1
    assert runner.calls[0][1:3] == ["release", "view"]
    assert not (config_path.parent.parent / "runtime" / TARGET_VERSION).exists()


def test_update_rejects_a_corrupt_release_before_creating_runtime(config_path: Path) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    runner = FakeUpdateRunner(
        workspace=workspace,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
        corrupt_download=True,
    )

    with pytest.raises(KnowledgeError, match="release asset digest mismatch"):
        update_installation(
            config_path=config_path,
            openclaw_command=openclaw,
            gh_command=gh,
            runner=runner,
            current_version=CURRENT_VERSION,
            current_python=current_python,
            base_python=base_python,
        )

    assert not (workspace / "runtime" / TARGET_VERSION).exists()
    assert runner.definitions == []


def test_update_rejects_an_active_documentation_sync(config_path: Path) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    with sqlite3.connect(workspace / "data/sqlite/test.db") as connection:
        connection.execute(
            """
            INSERT INTO automation_leases(lease_name, owner_id, expires_at, heartbeat_at)
            VALUES (?, ?, ?, ?)
            """,
            ("official-sync", "test-worker", time.time() + 120, "2026-09-04T18:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO automation_state(job_id, state_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                "official-docs",
                json.dumps({"status": "running", "owner_id": "test-worker"}),
                "2026-09-04T18:00:00Z",
            ),
        )
        connection.commit()
    runner = FakeUpdateRunner(
        workspace=workspace,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )

    with pytest.raises(KnowledgeError, match="documentation synchronization is active"):
        update_installation(
            config_path=config_path,
            openclaw_command=openclaw,
            gh_command=gh,
            runner=runner,
            current_version=CURRENT_VERSION,
            current_python=current_python,
            base_python=base_python,
        )

    assert all(call[1:3] != ["mcp", "show"] for call in runner.calls)
    assert not (workspace / "runtime" / TARGET_VERSION).exists()


def test_update_allows_an_idle_leader_lease(config_path: Path) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    with sqlite3.connect(workspace / "data/sqlite/test.db") as connection:
        connection.execute(
            """
            INSERT INTO automation_leases(lease_name, owner_id, expires_at, heartbeat_at)
            VALUES (?, ?, ?, ?)
            """,
            ("embedded-project-sync", "idle-worker", time.time() + 120, "2026-09-04T18:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO automation_state(job_id, state_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                "project:example_project",
                json.dumps({"status": "ok", "owner_id": "idle-worker"}),
                "2026-09-04T18:00:00Z",
            ),
        )
        connection.commit()
    runner = FakeUpdateRunner(
        workspace=workspace,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )

    result = update_installation(
        config_path=config_path,
        openclaw_command=openclaw,
        gh_command=gh,
        runner=runner,
        current_version=CURRENT_VERSION,
        current_python=current_python,
        base_python=base_python,
    )

    assert result.status == "updated"


def test_update_ignores_a_concurrent_release_check(config_path: Path) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    with sqlite3.connect(workspace / "data/sqlite/test.db") as connection:
        connection.execute(
            """
            INSERT INTO automation_leases(lease_name, owner_id, expires_at, heartbeat_at)
            VALUES (?, ?, ?, ?)
            """,
            ("release-update-check", "release-worker", time.time() + 120, "2026-09-04T18:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO automation_state(job_id, state_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                "release-update",
                json.dumps({"status": "checking", "owner_id": "release-worker"}),
                "2026-09-04T18:00:00Z",
            ),
        )
        connection.commit()
    runner = FakeUpdateRunner(
        workspace=workspace,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
    )

    result = update_installation(
        config_path=config_path,
        openclaw_command=openclaw,
        gh_command=gh,
        runner=runner,
        current_version=CURRENT_VERSION,
        current_python=current_python,
        base_python=base_python,
    )

    assert result.status == "updated"


def test_update_restores_data_and_openclaw_definition_after_probe_failure(
    config_path: Path,
) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    original_config = config_path.read_bytes()
    runner = FakeUpdateRunner(
        workspace=workspace,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
        fail_probe=True,
        mutate_during_smoke=True,
    )

    with pytest.raises(KnowledgeError, match="rollback succeeded"):
        update_installation(
            config_path=config_path,
            openclaw_command=openclaw,
            gh_command=gh,
            runner=runner,
            current_version=CURRENT_VERSION,
            current_python=current_python,
            base_python=base_python,
        )

    assert config_path.read_bytes() == original_config
    with sqlite3.connect(workspace / "data/sqlite/test.db") as connection:
        mutation = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='update_mutation'"
        ).fetchone()
    assert mutation is None
    assert len(runner.definitions) == 2
    _assert_stable_stdio_definition(runner.definitions[0], workspace)
    assert runner.definitions[1] == runner.previous_definition
    failure_backups = list((workspace / "backups").glob("*/update-result.json"))
    assert len(failure_backups) == 1
    failure = json.loads(failure_backups[0].read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["rollback"] == "succeeded"
    assert failure["error"]


def test_update_restores_definition_when_atomic_switch_reports_failure(
    config_path: Path,
) -> None:
    current_python, current_server, base_python, gh, openclaw = _managed_installation(config_path)
    runner = FakeUpdateRunner(
        workspace=config_path.parent.parent,
        config_path=config_path,
        openclaw=openclaw,
        previous_server=current_server,
        fail_switch_after_write=True,
    )

    with pytest.raises(KnowledgeError, match="rollback succeeded"):
        update_installation(
            config_path=config_path,
            openclaw_command=openclaw,
            gh_command=gh,
            runner=runner,
            current_version=CURRENT_VERSION,
            current_python=current_python,
            base_python=base_python,
        )

    assert len(runner.definitions) == 2
    _assert_stable_stdio_definition(runner.definitions[0], config_path.parent.parent)
    assert runner.definitions[1] == runner.previous_definition


def test_update_rejects_a_linked_runtime_root(config_path: Path, tmp_path: Path) -> None:
    current_python, _, base_python, gh, openclaw = _managed_installation(config_path)
    workspace = config_path.parent.parent
    runtime_root = workspace / "runtime"
    moved_runtime = tmp_path / "external-runtime"
    runtime_root.rename(moved_runtime)
    try:
        runtime_root.symlink_to(moved_runtime, target_is_directory=True)
    except OSError:
        pytest.skip("directory links are not available for this test user")

    with pytest.raises(KnowledgeError, match="runtime root cannot be a link"):
        update_installation(
            config_path=config_path,
            openclaw_command=openclaw,
            gh_command=gh,
            runner=lambda *_args, **_kwargs: pytest.fail("runner should not be called"),
            current_version=CURRENT_VERSION,
            current_python=current_python,
            base_python=base_python,
        )
