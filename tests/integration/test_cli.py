import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from helix_mcp_knowledge.cli.main import main
from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.managed_installation import (
    load_managed_installation,
    stable_launcher_path,
)
from helix_mcp_knowledge.openclaw import OpenClawInstallation


class FakeDashboardRuntimeManager:
    def __init__(self, installation, **kwargs):
        self.installation = installation

    def install_and_start(self):
        status = SimpleNamespace(
            to_dict=lambda: {
                "manager": "systemd_user",
                "installed": True,
                "enabled": True,
                "active": True,
                "port": self.installation.dashboard_port,
            }
        )
        return SimpleNamespace(status=status, process_id=8765, to_dict=status.to_dict)


def test_cli_ingests_project_document(config_path: Path, capsys) -> None:
    path = config_path.parent.parent / "data/sources/projects/example_project/docs/cli.md"
    path.parent.mkdir(parents=True)
    path.write_text("# CLI Design\n\nCliUniqueMarker configuration.", encoding="utf-8")

    exit_code = main(
        [
            "--config",
            str(config_path),
            "ingest",
            str(path),
            "--scope",
            "project",
            "--project-id",
            "example_project",
            "--document-type",
            "design",
            "--product",
            "cmdb",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output[0]["status"] == "indexed"
    assert output[0]["chunks_indexed"] == 1


def test_cli_reports_missing_evaluation_dataset_without_traceback(
    config_path: Path, capsys
) -> None:
    missing = config_path.parent.parent / "missing-evaluation.json"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--config",
                str(config_path),
                "evaluate-retrieval",
                str(missing),
            ]
        )

    assert exc.value.code == 2
    assert "could not read evaluation dataset" in capsys.readouterr().err


def test_cli_writes_retrieval_evaluation_markdown(config_path: Path, capsys) -> None:
    dataset = config_path.parent.parent / "synthetic-evaluation.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "Synthetic CLI baseline",
                "description": "Safe generated evidence only.",
                "cases": [
                    {
                        "case_id": "missing-001",
                        "category": "concept",
                        "difficulty": "easy",
                        "language": "en",
                        "query": "synthetic evidence marker",
                        "expected_terms": ["synthetic marker"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = config_path.parent.parent / "reports/evaluation.md"

    exit_code = main(
        [
            "--config",
            str(config_path),
            "evaluate-retrieval",
            str(dataset),
            "--top-k",
            "5",
            "--format",
            "markdown",
            "--output",
            str(output),
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["status"] == "written"
    assert result["format"] == "markdown"
    assert len(result["dataset_sha256"]) == 64
    markdown = output.read_text(encoding="utf-8")
    assert "# Retrieval evaluation: Synthetic CLI baseline" in markdown
    assert "| missing-001 | concept | easy | en |" in markdown
    assert "## Runtime" in markdown
    assert "SQLite bytes:" in markdown
    assert str(config_path.parent.parent) not in markdown


def test_cli_syncs_project_manifest(config_path: Path, capsys) -> None:
    project_path = config_path.parent / "projects/example_project.yaml"
    project_path.write_text(
        project_path.read_text(encoding="utf-8").replace(
            "  path: data/sources/projects/example_project/docs",
            "  path: data/sources/projects/example_project/docs\n"
            "  sources_manifest: config/projects/example_project.sources.yaml",
        ),
        encoding="utf-8",
    )
    (config_path.parent / "projects/example_project.sources.yaml").write_text(
        """schema_version: 1
project_id: example_project
sources:
  - path: cli-sync.md
    document_type: design
    products: {cmdb: null}
""",
        encoding="utf-8",
    )
    path = config_path.parent.parent / "data/sources/projects/example_project/docs/cli-sync.md"
    path.parent.mkdir(parents=True)
    path.write_text("# CLI Sync\n\nCliSyncMarker.", encoding="utf-8")

    exit_code = main(
        ["--config", str(config_path), "sync-project", "example_project", "--verbose-results"]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["summary"]["statuses"] == {"indexed": 1}
    assert output["results"][0]["source_path"] == str(path)


def test_cli_configures_official_products_and_schedule(config_path: Path, capsys) -> None:
    manifest = config_path.parent / "sources/bmc-official-26.1.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        """schema_version: 1
collections:
  - collection_id: bmc-cmdb-26-1-test
    root_url: https://docs.bmc.com/cmdb/26.1/
    local_path_prefix: cmdb/26.1/discovered
    product: cmdb
    version: "26.1"
""",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "configure",
            "--product",
            "cmdb=26.1",
            "--automatic-sync",
            "--interval-hours",
            "12",
            "--no-retain-unselected-versions",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    configured = load_config(config_path).official_docs

    assert exit_code == 0
    assert output["official_docs"]["products"] == {"cmdb": {"versions": ["26.1"]}}
    assert configured.automatic_sync is True
    assert configured.interval_hours == 12
    assert configured.retain_unselected_versions is False


def test_cli_initializes_standalone_workspace(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "standalone"

    exit_code = main(["init", "--workspace", str(workspace)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["workspace"] == str(workspace)
    assert output["config"] == str(workspace / "config/config.yaml")
    assert len(output["created"]) == 4
    assert (workspace / "data/sources/bmc/official").is_dir()


def test_cli_creates_client_neutral_managed_installation(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = tmp_path / "managed-standalone"
    server = tmp_path / "helix-mcp-knowledge-server"
    server.write_text("server", encoding="utf-8")
    (tmp_path / ("python.exe" if os.name == "nt" else "python")).write_text(
        "python", encoding="utf-8"
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.cli.main.DashboardRuntimeManager",
        FakeDashboardRuntimeManager,
    )

    exit_code = main(
        [
            "install",
            "--workspace",
            str(workspace),
            "--server-command",
            str(server),
            "--no-dashboard",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["workspace_initialized"] is True
    assert output["official_docs"]["products"] == {}
    assert output["mcp_registration"]["command"] == str(stable_launcher_path(workspace))
    managed = load_managed_installation(workspace)
    assert managed is not None
    assert managed.client == "standalone"


def test_cli_persists_github_command_for_managed_background_checks(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = tmp_path / "managed-github"
    server = tmp_path / "helix-mcp-knowledge-server"
    server.write_text("server", encoding="utf-8")
    python = tmp_path / ("python.exe" if os.name == "nt" else "python")
    python.write_text("python", encoding="utf-8")
    gh = tmp_path / ("gh.exe" if os.name == "nt" else "gh")
    gh.write_text("github", encoding="utf-8")
    if os.name != "nt":
        gh.chmod(0o700)
    monkeypatch.setattr(
        "helix_mcp_knowledge.cli.main.DashboardRuntimeManager",
        FakeDashboardRuntimeManager,
    )

    exit_code = main(
        [
            "install",
            "--workspace",
            str(workspace),
            "--server-command",
            str(server),
            "--gh-command",
            str(gh),
            "--no-dashboard",
        ]
    )
    capsys.readouterr()
    config = load_config(workspace / "config/config.yaml")

    assert exit_code == 0
    assert config.updates.gh_command == str(gh.resolve())
    assert config.catalog_updates.gh_command == str(gh.resolve())


def test_cli_initializes_and_registers_openclaw_workspace(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = tmp_path / "openclaw-workspace"
    captured: dict[str, object] = {}

    def fake_install(**kwargs):
        captured.update(kwargs)
        return OpenClawInstallation(
            server_name=kwargs["server_name"],
            config=kwargs["config_path"],
            workspace=kwargs["workspace"],
            server_command=Path("/runtime/helix-mcp-knowledge-server"),
            openclaw_command=Path("/usr/bin/openclaw"),
            probed=kwargs["probe"],
            reloaded=kwargs["reload"],
            registration_output="saved",
            reload_output="reloaded",
        )

    monkeypatch.setattr("helix_mcp_knowledge.cli.main.install_openclaw_server", fake_install)
    monkeypatch.setattr(
        "helix_mcp_knowledge.cli.main.DashboardRuntimeManager",
        FakeDashboardRuntimeManager,
    )

    exit_code = main(
        [
            "install-openclaw",
            "--workspace",
            str(workspace),
            "--product",
            "arsystem=26.1",
            "--product",
            "cmdb=26.1",
            "--product",
            "itsm=26.1",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["workspace_initialized"] is True
    assert output["official_docs"]["automatic_sync"] is True
    assert set(output["official_docs"]["products"]) == {"arsystem", "cmdb", "itsm"}
    assert captured["config_path"] == workspace / "config/config.yaml"
    assert captured["workspace"] == workspace
    assert captured["probe"] is True
    assert captured["reload"] is True
    assert (workspace / "data/sqlite/helix_mcp_knowledge.db").is_file()


def test_cli_registers_empty_workspace_without_product_selection(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = tmp_path / "empty-openclaw-workspace"
    captured: dict[str, object] = {}

    def fake_install(**kwargs):
        captured.update(kwargs)
        return OpenClawInstallation(
            server_name=kwargs["server_name"],
            config=kwargs["config_path"],
            workspace=kwargs["workspace"],
            server_command=Path("/runtime/helix-mcp-knowledge-server"),
            openclaw_command=Path("/usr/bin/openclaw"),
            probed=kwargs["probe"],
            reloaded=kwargs["reload"],
            registration_output="saved",
            reload_output="reloaded",
        )

    monkeypatch.setattr("helix_mcp_knowledge.cli.main.install_openclaw_server", fake_install)
    monkeypatch.setattr(
        "helix_mcp_knowledge.cli.main.DashboardRuntimeManager",
        FakeDashboardRuntimeManager,
    )
    monkeypatch.setattr("helix_mcp_knowledge.cli.main.webbrowser.open", lambda *_: True)

    exit_code = main(
        [
            "install-openclaw",
            "--workspace",
            str(workspace),
            "--automatic-sync",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["workspace_initialized"] is True
    assert output["official_docs"]["automatic_sync"] is True
    assert output["official_docs"]["products"] == {}
    assert output["dashboard"] == {
        "pid": 8765,
        "url": "http://127.0.0.1:8765/",
    }
    assert captured["config_path"] == workspace / "config/config.yaml"
    assert captured["probe"] is True
    assert captured["reload"] is True


def test_cli_can_install_empty_workspace_without_opening_dashboard(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = tmp_path / "headless-openclaw-workspace"

    def fake_install(**kwargs):
        return OpenClawInstallation(
            server_name=kwargs["server_name"],
            config=kwargs["config_path"],
            workspace=kwargs["workspace"],
            server_command=Path("/runtime/helix-mcp-knowledge-server"),
            openclaw_command=Path("/usr/bin/openclaw"),
            probed=False,
            reloaded=False,
            registration_output="saved",
            reload_output=None,
        )

    monkeypatch.setattr("helix_mcp_knowledge.cli.main.install_openclaw_server", fake_install)
    monkeypatch.setattr(
        "helix_mcp_knowledge.cli.main.DashboardRuntimeManager",
        FakeDashboardRuntimeManager,
    )

    exit_code = main(
        [
            "install-openclaw",
            "--workspace",
            str(workspace),
            "--no-probe",
            "--no-reload",
            "--no-dashboard",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["workspace_initialized"] is True
    assert output["official_docs"]["products"] == {}
    assert output["dashboard"] is None


def test_cli_routes_transactional_update_without_opening_application(
    config_path: Path, capsys, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def fake_update(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {
                "status": "planned",
                "current_version": "1.0.2",
                "target_version": "1.1.0",
            }
        )

    monkeypatch.setattr("helix_mcp_knowledge.cli.main.update_installation", fake_update)
    monkeypatch.setattr(
        "helix_mcp_knowledge.cli.main.KnowledgeApplication.from_config",
        lambda *_: (_ for _ in ()).throw(AssertionError("application should remain closed")),
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "update",
            "--version",
            "1.1.0",
            "--repository",
            "example/private",
            "--openclaw-command",
            "/opt/openclaw",
            "--gh-command",
            "/opt/gh",
            "--dry-run",
            "--no-probe",
            "--no-reload",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert captured == {
        "config_path": config_path,
        "repository": "example/private",
        "target_version": "1.1.0",
        "openclaw_command": "/opt/openclaw",
        "gh_command": "/opt/gh",
        "server_name": "helix_knowledge",
        "probe": False,
        "reload": False,
        "dry_run": True,
        "resume": False,
        "allow_downgrade": False,
        "post_activation_check": None,
    }
