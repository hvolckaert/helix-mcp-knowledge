from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parents[2]
    / "evaluation"
    / "integrated-read-only-preflight"
    / "integrated_preflight_probe.py"
)


def _probe_module():
    spec = importlib.util.spec_from_file_location("integrated_preflight_probe", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mapping(*, environment: str = "dev") -> dict[str, object]:
    return {
        "schema_version": 1,
        "environment": environment,
        "form": "synthetic-form",
        "fields": ["match-field", "empty-field", "present-field"],
        "qualification": "synthetic qualification",
        "limit": 2,
        "expected_result_count": 1,
        "equals": {"match-field": "synthetic-value"},
        "empty": ["empty-field"],
        "present": ["present-field"],
    }


def test_private_mapping_contract_accepts_bounded_dev(tmp_path: Path) -> None:
    module = _probe_module()
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(_mapping()), encoding="utf-8")

    loaded = module._load_private_mapping(path)

    assert loaded["environment"] == "dev"
    assert loaded["expected_result_count"] == 1
    assert loaded["limit"] == 2


def test_private_mapping_contract_rejects_non_dev(tmp_path: Path) -> None:
    module = _probe_module()
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(_mapping(environment="prod")), encoding="utf-8")

    with pytest.raises(module.ProbeStop, match="private_mapping_must_target_dev"):
        module._load_private_mapping(path)
