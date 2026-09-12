#!/usr/bin/env python3
"""Detect and optionally propose the next BMC release for curated products."""

from __future__ import annotations

import argparse
from pathlib import Path

from helix_mcp_knowledge.catalog.watch import (
    CatalogCandidateValidator,
    CatalogWatchConfig,
    add_validated_candidates,
    candidate_versions,
    markdown_report,
    write_manifest,
)
from helix_mcp_knowledge.sync.manifest import OfficialSourceManifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--watch-config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    manifest = OfficialSourceManifest.load(args.catalog)
    settings = CatalogWatchConfig.load(args.watch_config)
    candidates = candidate_versions(manifest)
    validator = CatalogCandidateValidator(settings=settings)
    try:
        results = [
            validator.validate(product, version)
            for product, version in candidates.items()
            if product in settings.products
        ]
    finally:
        validator.close()
    args.report.write_text(markdown_report(results), encoding="utf-8")
    validated = [result for result in results if result.status == "validated"]
    if args.write and validated:
        write_manifest(
            args.catalog,
            add_validated_candidates(manifest, settings, validated),
        )
    print(f"validated_candidates={len(validated)}")
    return 1 if any(result.status == "error" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
