from pathlib import Path

import pytest
import yaml

from helix_mcp_knowledge.catalog.distribution import (
    load_effective_official_catalog,
    merge_official_catalogs,
    reconcile_packaged_official_catalog,
)
from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.sync.manifest import OfficialSourceManifest


def _manifest(*, root: str, identifier: str = "bmc-cmdb-26-1") -> OfficialSourceManifest:
    return OfficialSourceManifest.model_validate(
        {
            "schema_version": 1,
            "collections": [
                {
                    "collection_id": identifier,
                    "root_url": root,
                    "local_path_prefix": identifier,
                    "product": "cmdb",
                    "version": "26.1" if identifier.endswith("1") else "26.2",
                }
            ],
        }
    )


def test_catalog_merge_adds_new_release_entries() -> None:
    current = _manifest(root="https://example.test/cmdb/261/")
    packaged = OfficialSourceManifest.model_validate(
        {
            "schema_version": 1,
            "collections": [
                current.collections[0].model_dump(mode="json"),
                _manifest(root="https://example.test/cmdb/262/", identifier="bmc-cmdb-26-2")
                .collections[0]
                .model_dump(mode="json"),
            ],
        }
    )

    merged, result = merge_official_catalogs(current, packaged)

    assert result.changed is True
    assert result.added_collections == ("bmc-cmdb-26-2",)
    assert [item.collection_id for item in merged.collections] == [
        "bmc-cmdb-26-1",
        "bmc-cmdb-26-2",
    ]


def test_catalog_merge_preserves_local_conflict() -> None:
    current = _manifest(root="https://local.example.test/cmdb/261/").model_copy(
        update={"catalog_revision": 1}
    )
    packaged = _manifest(root="https://release.example.test/cmdb/261/").model_copy(
        update={"catalog_revision": 2}
    )

    merged, result = merge_official_catalogs(current, packaged)

    assert result.changed is False
    assert result.preserved_conflicts == ("collection:bmc-cmdb-26-1",)
    assert merged.collections[0].root_url == "https://local.example.test/cmdb/261/"
    assert merged.catalog_revision == 2


def test_catalog_reconciliation_is_persistent_and_idempotent(
    config_path: Path, monkeypatch
) -> None:
    manifest_path = config_path.parent / "sources/bmc-official-26.1.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    current = _manifest(root="https://example.test/cmdb/261/")
    manifest_path.write_text(
        yaml.safe_dump(current.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    packaged = OfficialSourceManifest.model_validate(
        {
            "schema_version": 1,
            "collections": [
                current.collections[0].model_dump(mode="json"),
                _manifest(root="https://example.test/cmdb/262/", identifier="bmc-cmdb-26-2")
                .collections[0]
                .model_dump(mode="json"),
            ],
        }
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.catalog.distribution.read_packaged_resource",
        lambda relative: yaml.safe_dump(packaged.model_dump(mode="json")).encode(),
    )

    first = reconcile_packaged_official_catalog(load_config(config_path))
    second = reconcile_packaged_official_catalog(load_config(config_path))

    assert first.added_collections == ("bmc-cmdb-26-2",)
    assert second.changed is False
    saved = OfficialSourceManifest.load(manifest_path)
    assert saved.catalog_revision == current.catalog_revision
    assert [item.version for item in saved.collections] == ["26.1", "26.2"]


@pytest.mark.parametrize("remote_revision", [1, 2])
def test_effective_catalog_uses_current_or_newer_cached_corrections_and_additions(
    config_path: Path, remote_revision: int
) -> None:
    config = load_config(config_path)
    local = OfficialSourceManifest.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": 1,
            "products": [{"product_id": "cmdb", "name": "Local CMDB"}],
            "collections": [
                {
                    "collection_id": "bmc-cmdb-26-1",
                    "root_url": "https://example.test/cmdb/261/",
                    "local_path_prefix": "cmdb/26.1/discovered",
                    "product": "cmdb",
                    "version": "26.1",
                }
            ],
        }
    )
    remote = OfficialSourceManifest.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": remote_revision,
            "products": [
                {"product_id": "cmdb", "name": "Remote CMDB"},
                {"product_id": "new_product", "name": "New BMC Product"},
            ],
            "collections": [
                local.collections[0].model_dump(mode="json"),
                {
                    "collection_id": "bmc-new-product-26-1",
                    "root_url": "https://example.test/new/261/",
                    "local_path_prefix": "new_product/26.1/discovered",
                    "product": "new_product",
                    "version": "26.1",
                },
            ],
        }
    )
    config.official_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    config.official_manifest_path.write_text(
        yaml.safe_dump(local.model_dump(mode="json")), encoding="utf-8"
    )
    config.official_catalog_cache_path.parent.mkdir(parents=True, exist_ok=True)
    config.official_catalog_cache_path.write_text(
        yaml.safe_dump(remote.model_dump(mode="json")), encoding="utf-8"
    )

    effective = load_effective_official_catalog(config)

    assert effective.catalog_revision == remote_revision
    assert [product.product_id for product in effective.products] == ["cmdb", "new_product"]
    assert effective.products[0].name == "Remote CMDB"
    assert [collection.version for collection in effective.collections] == ["26.1", "26.1"]


def test_effective_catalog_ignores_a_corrupted_cache(config_path: Path) -> None:
    config = load_config(config_path)
    local = _manifest(root="https://example.test/cmdb/261/")
    config.official_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    config.official_manifest_path.write_text(
        yaml.safe_dump(local.model_dump(mode="json")), encoding="utf-8"
    )
    config.official_catalog_cache_path.parent.mkdir(parents=True, exist_ok=True)
    config.official_catalog_cache_path.write_text("not: [valid", encoding="utf-8")

    effective = load_effective_official_catalog(config)

    assert effective.collections[0].root_url == "https://example.test/cmdb/261/"


def test_effective_catalog_keeps_a_newer_local_revision(config_path: Path) -> None:
    config = load_config(config_path)
    local = OfficialSourceManifest.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": 3,
            "products": [{"product_id": "cmdb", "name": "Newer local CMDB"}],
        }
    )
    cached = OfficialSourceManifest.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": 2,
            "products": [{"product_id": "cmdb", "name": "Older cached CMDB"}],
        }
    )
    config.official_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    config.official_manifest_path.write_text(
        yaml.safe_dump(local.model_dump(mode="json")), encoding="utf-8"
    )
    config.official_catalog_cache_path.parent.mkdir(parents=True, exist_ok=True)
    config.official_catalog_cache_path.write_text(
        yaml.safe_dump(cached.model_dump(mode="json")), encoding="utf-8"
    )

    effective = load_effective_official_catalog(config)

    assert effective.catalog_revision == 3
    assert effective.products[0].name == "Newer local CMDB"
