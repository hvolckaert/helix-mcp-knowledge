"""Run a sanitized Knowledge + Gateway preflight without creating any plan or write."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters, stdio_client

from helix_mcp_knowledge.config import load_config

OFFICIAL_QUERY = (
    "What documentation and data-integrity considerations apply when modifying "
    "a CI attribute in BMC Helix CMDB?"
)
PROJECT_QUERY = "approved procedure for the synthetic DEV assignment-marker update"
REQUIRED_MAPPING_KEYS = {
    "schema_version",
    "environment",
    "form",
    "fields",
    "qualification",
    "limit",
    "expected_result_count",
    "equals",
    "empty",
    "present",
}


class ProbeStop(RuntimeError):
    """A presentation-safe reason to stop the preflight."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ProbeStop(reason)


def _load_private_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeStop("private_mapping_unreadable") from exc
    _require(isinstance(raw, dict), "private_mapping_invalid")
    _require(set(raw) == REQUIRED_MAPPING_KEYS, "private_mapping_schema_mismatch")
    _require(raw["schema_version"] == 1, "private_mapping_version_unsupported")
    _require(raw["environment"] == "dev", "private_mapping_must_target_dev")
    _require(isinstance(raw["form"], str) and 0 < len(raw["form"]) <= 255, "form_invalid")
    _require(
        isinstance(raw["qualification"], str) and 0 < len(raw["qualification"]) <= 2_000,
        "qualification_invalid",
    )
    _require(isinstance(raw["limit"], int) and 1 <= raw["limit"] <= 20, "limit_invalid")
    _require(raw["expected_result_count"] == 1, "expected_result_count_must_be_one")

    fields = raw["fields"]
    _require(isinstance(fields, list) and 1 <= len(fields) <= 12, "fields_invalid")
    _require(
        all(isinstance(field, str) and 0 < len(field) <= 255 for field in fields),
        "fields_invalid",
    )
    _require(len(fields) == len(set(fields)), "fields_must_be_unique")
    field_set = set(fields)

    equals = raw["equals"]
    empty = raw["empty"]
    present = raw["present"]
    _require(isinstance(equals, dict) and 1 <= len(equals) <= 8, "equals_checks_invalid")
    _require(isinstance(empty, list) and len(empty) <= 8, "empty_checks_invalid")
    _require(isinstance(present, list) and len(present) <= 8, "present_checks_invalid")
    _require(all(isinstance(field, str) for field in equals), "equals_checks_invalid")
    _require(
        all(
            value is None or isinstance(value, str | int | float | bool)
            for value in equals.values()
        ),
        "equals_values_invalid",
    )
    _require(all(isinstance(field, str) for field in empty), "empty_checks_invalid")
    _require(all(isinstance(field, str) for field in present), "present_checks_invalid")
    _require(set(equals) <= field_set, "equals_fields_not_requested")
    _require(set(empty) <= field_set, "empty_fields_not_requested")
    _require(set(present) <= field_set, "present_fields_not_requested")
    _require(not (set(empty) & set(present)), "empty_and_present_checks_overlap")
    return raw


async def _call(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    _require(not result.is_error, f"{name}_failed")
    _require(result.structured_content is not None, f"{name}_returned_no_structured_content")
    return result.structured_content


def _section_matches(result: dict[str, Any], section: dict[str, Any]) -> bool:
    return section["document_id"] == result["document_id"] and any(
        chunk["chunk_id"] == result["chunk_id"] for chunk in section["chunks"]
    )


def _lexical_provenance_matches(
    result: dict[str, Any],
    *,
    source_scope: str,
    documentation_version: str,
    project_id: str | None,
) -> bool:
    match = result["match"]
    return (
        result["source_scope"] == source_scope
        and result.get("project_id") == project_id
        and documentation_version in result["versions"]
        and match["lexical"]
        and not match["semantic"]
        and not match["reranked"]
    )


async def _probe_knowledge(
    *,
    config_path: Path,
    project_id: str,
    product: str,
    documentation_version: str,
) -> dict[str, object]:
    config = load_config(config_path)
    selected_product = config.official_docs.products.get(product)
    _require(
        selected_product is not None and documentation_version in selected_product.versions,
        "official_product_version_not_selected",
    )
    _require(config.retrieval.lexical.enabled, "lexical_retrieval_disabled")
    _require(not config.retrieval.semantic.enabled, "semantic_retrieval_must_be_disabled")
    _require(not config.retrieval.reranker.enabled, "reranker_must_be_disabled")

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "helix_mcp_knowledge.server"],
        env={"HELIX_KNOWLEDGE_CONFIG": str(config_path)},
        cwd=config_path.parent,
    )
    async with Client(stdio_client(parameters), read_timeout_seconds=60) as client:
        versions = await _call(client, "list_versions", {"product": product})
        _require(
            any(
                item["version"] == documentation_version and item["indexed"]
                for item in versions["versions"]
            ),
            "official_product_version_not_indexed",
        )

        projects = await _call(client, "list_projects", {})
        _require(
            any(
                item.get("id") == project_id or item.get("project_id") == project_id
                for item in projects["projects"]
            ),
            "validation_project_not_registered",
        )

        official = await _call(
            client,
            "search_docs",
            {
                "query": OFFICIAL_QUERY,
                "source_scope": "bmc_official",
                "product": product,
                "version": documentation_version,
                "top_k": 5,
            },
        )
        project = await _call(
            client,
            "search_docs",
            {
                "query": PROJECT_QUERY,
                "source_scope": "project",
                "project_id": project_id,
                "product": product,
                "version": documentation_version,
                "top_k": 5,
            },
        )
        _require(bool(official["results"]), "official_evidence_missing")
        _require(bool(project["results"]), "project_evidence_missing")
        official_result = official["results"][0]
        project_result = project["results"][0]
        _require(
            _lexical_provenance_matches(
                official_result,
                source_scope="bmc_official",
                documentation_version=documentation_version,
                project_id=None,
            ),
            "official_evidence_provenance_failed",
        )
        _require(bool(official_result.get("source_url")), "official_evidence_url_missing")
        _require(
            _lexical_provenance_matches(
                project_result,
                source_scope="project",
                documentation_version=documentation_version,
                project_id=project_id,
            ),
            "project_evidence_provenance_failed",
        )

        official_section = await _call(
            client,
            "get_section",
            {
                "chunk_id": official_result["chunk_id"],
                "context_before": 1,
                "context_after": 1,
            },
        )
        project_section = await _call(
            client,
            "get_section",
            {
                "chunk_id": project_result["chunk_id"],
                "project_id": project_id,
                "context_before": 1,
                "context_after": 1,
            },
        )
        _require(
            _section_matches(official_result, official_section),
            "official_section_expansion_failed",
        )
        _require(
            _section_matches(project_result, project_section),
            "project_section_expansion_failed",
        )

    return {
        "release": package_version("helix-mcp-knowledge"),
        "product_version_indexed": True,
        "project_registered": True,
        "retrieval_mode": "lexical_only",
        "official": {
            "result_count": len(official["results"]),
            "provenance_verified": True,
            "section_expanded": True,
        },
        "project": {
            "result_count": len(project["results"]),
            "provenance_verified": True,
            "section_expanded": True,
        },
    }


async def _probe_gateway(
    *,
    command: Path,
    cwd: Path,
    mapping: dict[str, Any],
) -> dict[str, object]:
    environment = mapping["environment"]
    parameters = StdioServerParameters(command=str(command), args=[], cwd=cwd)
    async with Client(stdio_client(parameters), read_timeout_seconds=60) as client:
        targets = await _call(client, "list_targets", {"include_disabled": False})
        target = next(
            (item for item in targets["targets"] if item["environment"] == environment),
            None,
        )
        _require(target is not None, "dev_target_missing")
        _require(target["enabled"], "dev_target_disabled")
        _require(not target["production"], "dev_target_marked_as_production")
        _require(target["capabilities"]["form_read"], "dev_form_read_not_available")

        health = await _call(
            client,
            "health_check",
            {"environment": environment, "force_refresh": False},
        )
        _require(health["status"] == "healthy", "dev_target_unhealthy")

        query = await _call(
            client,
            "query_form",
            {
                "environment": environment,
                "form": mapping["form"],
                "fields": mapping["fields"],
                "qualification": mapping["qualification"],
                "offset": 0,
                "limit": mapping["limit"],
                "include_total": True,
            },
        )
        entries = query["entries"]
        _require(len(entries) == mapping["expected_result_count"], "unexpected_result_count")
        _require(query.get("total") == mapping["expected_result_count"], "unexpected_total_count")
        values = entries[0]["values"]
        equals_passed = sum(
            values.get(field) == expected for field, expected in mapping["equals"].items()
        )
        empty_passed = sum(values.get(field) in (None, "") for field in mapping["empty"])
        present_passed = sum(values.get(field) not in (None, "") for field in mapping["present"])
        _require(equals_passed == len(mapping["equals"]), "private_equality_check_failed")
        _require(empty_passed == len(mapping["empty"]), "private_empty_check_failed")
        _require(present_passed == len(mapping["present"]), "private_presence_check_failed")

    return {
        "environment": "dev",
        "target_enabled": True,
        "target_production": False,
        "health": "healthy",
        "operation": "bounded_form_read",
        "result_count": len(entries),
        "private_checks": {
            "equality": {"configured": len(mapping["equals"]), "passed": equals_passed},
            "empty": {"configured": len(mapping["empty"]), "passed": empty_passed},
            "present": {"configured": len(mapping["present"]), "passed": present_passed},
        },
    }


async def probe(args: argparse.Namespace) -> dict[str, object]:
    mapping = _load_private_mapping(args.private_mapping)
    knowledge = await _probe_knowledge(
        config_path=args.knowledge_config,
        project_id=args.project_id,
        product=args.product,
        documentation_version=args.documentation_version,
    )
    gateway = await _probe_gateway(
        command=args.gateway_command,
        cwd=args.gateway_cwd,
        mapping=mapping,
    )
    return {
        "schema_version": 1,
        "case": "integrated_read_only_preflight",
        "decision": "ready_for_human_review",
        "knowledge": knowledge,
        "gateway": gateway,
        "actions_not_taken": [
            "no_plan_created",
            "no_write_executed",
            "no_qa_or_prod_call",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-config", type=Path, required=True)
    parser.add_argument("--gateway-command", type=Path, required=True)
    parser.add_argument("--gateway-cwd", type=Path, required=True)
    parser.add_argument("--private-mapping", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--product", default="cmdb")
    parser.add_argument("--documentation-version", default="26.1")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        args.knowledge_config = args.knowledge_config.expanduser().resolve(strict=True)
        args.gateway_command = args.gateway_command.expanduser().resolve(strict=True)
        args.gateway_cwd = args.gateway_cwd.expanduser().resolve(strict=True)
        args.private_mapping = args.private_mapping.expanduser().resolve(strict=True)
        result = asyncio.run(asyncio.wait_for(probe(args), timeout=120))
    except ProbeStop as exc:
        blocker = str(exc)
    except TimeoutError:
        blocker = "preflight_timeout"
    except Exception:  # Keep private paths and tool payloads out of presentation output.
        blocker = "unexpected_preflight_failure"
    else:
        print(json.dumps(result, indent=2))
        return
    print(
        json.dumps(
            {
                "schema_version": 1,
                "case": "integrated_read_only_preflight",
                "decision": "stop",
                "blocker": blocker,
                "actions_not_taken": [
                    "no_plan_created",
                    "no_write_executed",
                    "no_qa_or_prod_call",
                ],
            },
            indent=2,
        )
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
