import json
import os
from pathlib import Path

import yaml

from helix_mcp_knowledge.cli.main import main
from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.diagnostics import CheckStatus, run_smoke_test
from helix_mcp_knowledge.reranker_client import (
    RERANKER_COMPONENT_VERSION,
    RERANKER_MODEL_ID,
    RERANKER_MODEL_REVISION,
)
from helix_mcp_knowledge.reranker_component import (
    RERANKER_PACKAGES,
    RERANKER_TORCH_PACKAGE,
)
from helix_mcp_knowledge.workspace import initialize_workspace


def _configure_stopped_reranker(config_path: Path) -> tuple[Path, Path]:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    root = config.reranker_component_path
    runtime_name = f"reranker-{RERANKER_COMPONENT_VERSION}-{'1' * 32}"
    model_name = f"bge-reranker-v2-m3-{'2' * 32}"
    runtime = root / "runtime" / runtime_name
    model = root / "models" / model_name
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")
    model.mkdir(parents=True)
    root.joinpath("current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component_version": RERANKER_COMPONENT_VERSION,
                "runtime": f"runtime/{runtime_name}",
                "model_path": f"models/{model_name}",
                "model_id": RERANKER_MODEL_ID,
                "model_revision": RERANKER_MODEL_REVISION,
                "inference_runtime": RERANKER_TORCH_PACKAGE,
                "packages": list(RERANKER_PACKAGES),
                "host": "127.0.0.1",
                "port": 8768,
                "token": "x" * 48,
            }
        ),
        encoding="utf-8",
    )
    stop = root / "service.stop"
    stop.write_text("preserve update rollback fence", encoding="utf-8")
    return stop, root / "service-control.json"


def _prepare_smoke_fixture(app, config_path: Path) -> None:
    manifest = config_path.parent / "sources/bmc-official-26.1.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        """schema_version: 1
sources:
  - source_id: bmc-cmdb-26-1-test
    title: BMC CMDB 26.1 test
    url: https://docs.bmc.com/cmdb/26.1/
    local_path: cmdb/26.1/test.html
    product: cmdb
    version: "26.1"
""",
        encoding="utf-8",
    )
    for project in app.registry.list():
        project.documents_path.mkdir(parents=True)

    with app.database.connect() as connection:
        connection.execute(
            "UPDATE documents SET source_url = ? WHERE document_id = 'doc_official'",
            ("https://docs.bmc.com/cmdb/26.1/reconciliation/",),
        )
        connection.execute(
            "DELETE FROM document_product_versions WHERE document_id = 'doc_official'"
        )
        connection.execute(
            """INSERT INTO document_product_versions(document_id, product_version_id)
               VALUES ('doc_official', 'cmdb:26.1')"""
        )
        connection.commit()


def _check(report, name: str):
    return next(check for check in report.checks if check.name == name)


def test_smoke_test_validates_search_and_restores_context(app, config_path: Path) -> None:
    _prepare_smoke_fixture(app, config_path)
    app.set_active_project("example_project")
    before = app.database.status()["counts"]

    report = run_smoke_test(app, require_project_isolation=True)

    assert report.passed is True
    assert all(check.status is CheckStatus.PASS for check in report.checks)
    assert _check(report, "mcp_tools").details["tools"] == [
        "get_active_project",
        "get_section",
        "get_sync_status",
        "get_update_status",
        "list_products",
        "list_projects",
        "list_versions",
        "search_docs",
        "set_active_project",
    ]
    assert _check(report, "official_search").details["invalid_chunks"] == []
    assert _check(report, "project_isolation").details["leaked_chunks"] == []
    assert _check(report, "project_isolation").details["section_leaked_after_clear"] is False
    assert app.get_active_project().project_id == "example_project"
    assert app.database.status()["counts"] == before


def test_smoke_test_reports_empty_installation(config_path: Path, capsys) -> None:
    stop, control = _configure_stopped_reranker(config_path)

    exit_code = main(
        [
            "--config",
            str(config_path),
            "smoke-test",
            "--require-project-isolation",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["status"] == "fail"
    assert output["summary"]["failed"] >= 2
    checks = {check["name"]: check for check in output["checks"]}
    assert checks["database_integrity"]["status"] == "pass"
    assert checks["index_readiness"]["status"] == "fail"
    assert checks["mcp_tools"]["status"] == "pass"
    assert checks["project_isolation"]["status"] == "fail"
    assert stop.read_text(encoding="utf-8") == "preserve update rollback fence"
    assert not control.exists()


def test_smoke_test_accepts_product_free_empty_installation(tmp_path: Path, capsys) -> None:
    initialized = initialize_workspace(tmp_path / "workspace")
    stop, control = _configure_stopped_reranker(initialized.config)

    exit_code = main(["--config", str(initialized.config), "smoke-test"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "pass"
    assert output["summary"] == {"passed": 3, "failed": 0, "skipped": 4}
    checks = {check["name"]: check for check in output["checks"]}
    assert checks["database_integrity"]["status"] == "pass"
    assert checks["index_readiness"]["status"] == "skip"
    assert checks["official_catalog"]["status"] == "skip"
    assert checks["mcp_tools"]["status"] == "pass"
    assert checks["official_search"]["status"] == "skip"
    assert checks["project_isolation"]["status"] == "skip"
    assert stop.read_text(encoding="utf-8") == "preserve update rollback fence"
    assert not control.exists()
