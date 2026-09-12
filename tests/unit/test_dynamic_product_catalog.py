import pytest

from helix_mcp_knowledge.catalog.products import ProductCatalog
from helix_mcp_knowledge.sync.manifest import OfficialSourceManifest


def test_product_catalog_loads_a_product_declared_only_in_the_manifest() -> None:
    manifest = OfficialSourceManifest.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": 7,
            "products": [
                {
                    "product_id": "operations_management",
                    "name": "BMC Helix Operations Management",
                    "aliases": ["BHOM", "Operations Management"],
                }
            ],
        }
    )

    catalog = ProductCatalog.from_manifest(manifest)

    assert [product.product_id for product in catalog.all()] == ["operations_management"]
    assert catalog.resolve("BHOM").product_id == "operations_management"


def test_product_catalog_rejects_aliases_shared_by_two_products() -> None:
    manifest = OfficialSourceManifest.model_validate(
        {
            "schema_version": 2,
            "products": [
                {"product_id": "first", "name": "First", "aliases": ["shared"]},
                {"product_id": "second", "name": "Second", "aliases": ["Shared"]},
            ],
        }
    )

    try:
        ProductCatalog.from_manifest(manifest)
    except ValueError as exc:
        assert "shared" in str(exc)
    else:
        raise AssertionError("shared aliases must be rejected")


def test_schema_two_rejects_entries_for_an_undeclared_product() -> None:
    with pytest.raises(ValueError, match="undeclared products: itsm"):
        OfficialSourceManifest.model_validate(
            {
                "schema_version": 2,
                "products": [{"product_id": "cmdb", "name": "BMC Helix CMDB"}],
                "collections": [
                    {
                        "collection_id": "bmc-itsm-26-1",
                        "root_url": "https://docs.helixops.ai/itsm/261/",
                        "local_path_prefix": "itsm/26.1/discovered",
                        "product": "itsm",
                        "version": "26.1",
                    }
                ],
            }
        )
