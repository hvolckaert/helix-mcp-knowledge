"""Validate the CMDB 26.1 evidence case without printing BMC source text."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters, stdio_client

from helix_mcp_knowledge.config import load_config

PRODUCT = "cmdb"
DOCUMENTATION_VERSION = "26.1"
UNSUPPORTED_VERSION = "99.9"
QUERY = "How does BMC Helix CMDB resolve conflicting duplicate CIs into a trusted production view?"
EXPECTED_EVIDENCE = {
    "Reconciliation - BMC Helix Documentation": (
        "reconcil",
        "production dataset",
        "identif",
        "merg",
        "normaliz",
    ),
    "Merging duplicate CIs by reconciling data from multiple sources - BMC Helix Documentation": (
        "reconcil",
        "identif",
        "merg",
        "normaliz",
        "duplicate",
    ),
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _structured_content(result: Any, tool_name: str) -> dict[str, Any]:
    _require(not result.is_error, f"{tool_name} returned an error")
    _require(result.structured_content is not None, f"{tool_name} returned no structured content")
    return result.structured_content


async def probe(config_path: Path) -> dict[str, object]:
    config = load_config(config_path)
    product = config.official_docs.products.get(PRODUCT)
    _require(
        product is not None and DOCUMENTATION_VERSION in product.versions,
        "CMDB 26.1 must be selected in the authorized local index",
    )
    _require(config.retrieval.lexical.enabled, "lexical retrieval must be enabled")
    _require(not config.retrieval.semantic.enabled, "semantic retrieval must remain disabled")
    _require(not config.retrieval.reranker.enabled, "reranking must remain disabled")

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "helix_mcp_knowledge.server"],
        env={"HELIX_KNOWLEDGE_CONFIG": str(config_path)},
        cwd=config_path.parent,
    )
    async with Client(stdio_client(parameters), read_timeout_seconds=60) as client:
        versions_response = _structured_content(
            await client.call_tool("list_versions", {"product": PRODUCT}),
            "list_versions",
        )
        versions = versions_response["versions"]
        _require(
            any(item["version"] == DOCUMENTATION_VERSION and item["indexed"] for item in versions),
            "CMDB 26.1 is selected but not indexed",
        )
        _require(
            all(item["version"] != UNSUPPORTED_VERSION for item in versions),
            "the insufficient-evidence version unexpectedly exists",
        )

        search_response = _structured_content(
            await client.call_tool(
                "search_docs",
                {
                    "query": QUERY,
                    "source_scope": "bmc_official",
                    "product": PRODUCT,
                    "version": DOCUMENTATION_VERSION,
                    "top_k": 5,
                },
            ),
            "search_docs",
        )
        results = search_response["results"]
        results_by_title = {item["title"]: item for item in results}
        _require(
            EXPECTED_EVIDENCE.keys() <= results_by_title.keys(),
            "the top five results do not contain both expected evidence documents",
        )

        safe_results: list[dict[str, object]] = []
        for title, required_signals in EXPECTED_EVIDENCE.items():
            item = results_by_title[title]
            _require(item["source_scope"] == "bmc_official", f"unexpected scope for {title}")
            _require(DOCUMENTATION_VERSION in item["versions"], f"unexpected version for {title}")
            _require(
                str(item["source_url"] or "").startswith("https://"),
                f"missing source URL for {title}",
            )
            match = item["match"]
            _require(match["lexical"], f"lexical match missing for {title}")
            _require(not match["semantic"], f"semantic match unexpectedly enabled for {title}")
            _require(not match["reranked"], f"reranking unexpectedly enabled for {title}")

            section = _structured_content(
                await client.call_tool(
                    "get_section",
                    {
                        "chunk_id": item["chunk_id"],
                        "context_before": 1,
                        "context_after": 1,
                    },
                ),
                "get_section",
            )
            _require(section["document_id"] == item["document_id"], f"section mismatch for {title}")
            _require(
                any(chunk["chunk_id"] == item["chunk_id"] for chunk in section["chunks"]),
                f"selected chunk missing from expanded section for {title}",
            )
            section_text = "\n".join(chunk["text"] for chunk in section["chunks"]).casefold()
            _require(
                all(signal in section_text for signal in required_signals),
                f"expanded section no longer supports the expected evidence role for {title}",
            )
            safe_results.append(
                {
                    "rank": item["rank"],
                    "title": title,
                    "version": DOCUMENTATION_VERSION,
                    "source_scope": item["source_scope"],
                    "source_url": item["source_url"],
                    "lexical": match["lexical"],
                    "semantic": match["semantic"],
                    "reranked": match["reranked"],
                    "section_expanded": True,
                    "evidence_signals_verified": True,
                }
            )

        return {
            "release": version("helix-mcp-knowledge"),
            "product": PRODUCT,
            "version": DOCUMENTATION_VERSION,
            "query": QUERY,
            "version_indexed": True,
            "retrieval_mode": "lexical_only",
            "top_k": 5,
            "evidence": sorted(safe_results, key=lambda item: int(item["rank"])),
            "insufficient_evidence": {
                "version": UNSUPPORTED_VERSION,
                "indexed": False,
                "decision": "abstain_before_search",
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve(strict=True)
    result = asyncio.run(asyncio.wait_for(probe(config_path), timeout=90))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
