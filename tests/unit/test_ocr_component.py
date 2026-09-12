from __future__ import annotations

import json
import os
import subprocess
import venv
from pathlib import Path

import pytest

from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.errors import IngestionError
from helix_mcp_knowledge.ocr_component import (
    OCR_COMPONENT_VERSION,
    OcrClient,
    OcrComponentManager,
)


def _component_python(runtime: Path) -> Path:
    return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _write_component(config_path: Path) -> tuple[object, Path]:
    config = load_config(config_path)
    runtime = config.ocr_component_path / "runtime/ocr-1-test"
    python = _component_python(runtime)
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")
    config.ocr_component_path.mkdir(parents=True, exist_ok=True)
    (config.ocr_component_path / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component_version": 1,
                "runtime": "runtime/ocr-1-test",
                "installed_bytes": 1234,
                "installed_at": "2026-09-06T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return config, python


def test_component_status_reads_only_a_valid_managed_runtime(config_path: Path) -> None:
    config, python = _write_component(config_path)

    status = OcrComponentManager(config).status()

    assert status.installed is True
    assert status.component_version == 1
    assert status.installed_bytes == 1234
    assert OcrComponentManager(config).command()[0] == str(python)


def test_component_status_rejects_a_runtime_outside_its_root(config_path: Path) -> None:
    config = load_config(config_path)
    config.ocr_component_path.mkdir(parents=True, exist_ok=True)
    (config.ocr_component_path / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component_version": 1,
                "runtime": "../../outside",
            }
        ),
        encoding="utf-8",
    )

    status = OcrComponentManager(config).status()

    assert status.installed is False
    assert status.status == "error"
    assert "escapes" in (status.error or "")


def test_ocr_client_validates_and_maps_worker_output(config_path: Path) -> None:
    config, _ = _write_component(config_path)
    config.ingestion.ocr.enabled = True

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"pages": {"2": {"text": "Recognized text", "confidence": 0.91}}}),
            stderr="",
        )

    pages = OcrClient(config, runner=runner).extract(Path("document.pdf"), [2])

    assert pages[2].text == "Recognized text"
    assert pages[2].confidence == 0.91


def test_ocr_client_enforces_the_page_safety_limit(config_path: Path) -> None:
    config, _ = _write_component(config_path)
    config.ingestion.ocr.enabled = True
    config.ingestion.ocr.max_pages_per_document = 1

    with pytest.raises(IngestionError, match="safety limit"):
        OcrClient(config).extract(Path("document.pdf"), [1, 2])


def test_component_install_activates_only_after_smoke_test(config_path: Path, monkeypatch) -> None:
    config = load_config(config_path)

    def create_environment(builder, runtime):
        python = _component_python(Path(runtime))
        python.parent.mkdir(parents=True)
        python.write_text("placeholder", encoding="utf-8")

    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append([str(item) for item in command])
        return subprocess.CompletedProcess(command, 0, stdout="ready", stderr="")

    monkeypatch.setattr(venv.EnvBuilder, "create", create_environment)

    status = OcrComponentManager(config, runner=runner).install()

    assert status.installed is True
    assert status.component_version == OCR_COMPONENT_VERSION
    assert len(calls) == 3
    assert "pip" in calls[0]
    assert "--require-hashes" in calls[0]
    assert "pip" in calls[1]
    assert "--require-hashes" in calls[1]
    assert "--self-test" in calls[2]
