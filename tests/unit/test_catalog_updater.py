from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import yaml

from helix_mcp_knowledge.catalog.updater import CatalogUpdateChecker
from helix_mcp_knowledge.config import CatalogUpdateSettings
from helix_mcp_knowledge.storage.automation import AutomationStore
from helix_mcp_knowledge.sync.manifest import OfficialSourceManifest


def _catalog(revision: int) -> OfficialSourceManifest:
    return OfficialSourceManifest.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": revision,
            "products": [{"product_id": "cmdb", "name": "BMC Helix CMDB", "aliases": ["CMDB"]}],
            "collections": [
                {
                    "collection_id": f"bmc-cmdb-26-{revision}",
                    "root_url": f"https://docs.bmc.com/cmdb/26.{revision}/",
                    "local_path_prefix": f"cmdb/26.{revision}/discovered",
                    "product": "cmdb",
                    "version": f"26.{revision}",
                }
            ],
        }
    )


class CatalogRunner:
    def __init__(self, manifest: OfficialSourceManifest, *, corrupt_checksum: bool = False):
        self.manifest = manifest
        self.corrupt_checksum = corrupt_checksum
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        rendered = [str(item) for item in command]
        self.calls.append(rendered)
        if rendered[1:3] == ["release", "list"]:
            return subprocess.CompletedProcess(
                rendered,
                0,
                stdout=json.dumps(
                    [
                        {
                            "tagName": f"catalog-v{self.manifest.catalog_revision}",
                            "isDraft": False,
                        }
                    ]
                ),
                stderr="",
            )
        destination = Path(rendered[rendered.index("--dir") + 1])
        manifest_name = "bmc-official-catalog.yaml"
        content = yaml.safe_dump(
            self.manifest.model_dump(mode="json", exclude_defaults=False), sort_keys=False
        ).encode()
        (destination / manifest_name).write_bytes(content)
        digest = "0" * 64 if self.corrupt_checksum else hashlib.sha256(content).hexdigest()
        (destination / "bmc-official-catalog.sha256").write_text(
            f"{digest}  {manifest_name}\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(rendered, 0, stdout="", stderr="")


def _checker(app, tmp_path: Path, runner: CatalogRunner, callback=lambda: None):
    manifest_path = app.config.official_manifest_path
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        yaml.safe_dump(_catalog(1).model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    gh = tmp_path / "gh"
    gh.write_text("command", encoding="utf-8")
    app.config.catalog_updates = CatalogUpdateSettings(
        enabled=True,
        repository="example/private",
        gh_command=str(gh),
        cache_path=Path("data/cache/catalog.yaml"),
    )
    return CatalogUpdateChecker(
        config=app.config,
        store=AutomationStore(app.database),
        runner=runner,
        clock=lambda: 1_800_000_000.0,
        owner_id="catalog-checker",
        on_updated=callback,
    )


def test_catalog_checker_downloads_verifies_and_activates_new_revision(app, tmp_path: Path) -> None:
    activated: list[bool] = []
    runner = CatalogRunner(_catalog(2))
    checker = _checker(app, tmp_path, runner, lambda: activated.append(True))

    result = checker.check(force=True)

    assert result.status == "updated"
    assert result.current_revision == 2
    assert result.latest_revision == 2
    assert activated == [True]
    assert OfficialSourceManifest.load(app.config.official_catalog_cache_path).catalog_revision == 2
    assert len(runner.calls) == 2


def test_catalog_checker_uses_runtime_update_command_for_legacy_configuration(
    app, tmp_path: Path
) -> None:
    runner = CatalogRunner(_catalog(1))
    checker = _checker(app, tmp_path, runner)
    gh = Path(app.config.catalog_updates.gh_command)
    app.config.catalog_updates.gh_command = "gh"
    app.config.updates.gh_command = str(gh)

    result = checker.check(force=True)

    assert result.status == "current"
    assert runner.calls[0][0] == str(gh)


def test_catalog_checker_rejects_a_checksum_mismatch(app, tmp_path: Path) -> None:
    runner = CatalogRunner(_catalog(2), corrupt_checksum=True)
    checker = _checker(app, tmp_path, runner)

    result = checker.check(force=True)

    assert result.status == "error"
    assert result.current_revision == 1
    assert result.error is not None and "SHA-256" in result.error
    assert not app.config.official_catalog_cache_path.exists()


def test_catalog_checker_does_not_download_an_existing_revision(app, tmp_path: Path) -> None:
    runner = CatalogRunner(_catalog(1))
    checker = _checker(app, tmp_path, runner)

    result = checker.check(force=True)

    assert result.status == "current"
    assert result.current_revision == 1
    assert len(runner.calls) == 1
