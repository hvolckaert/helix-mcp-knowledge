from pathlib import Path

from helix_mcp_knowledge.catalog.products import ProductCatalog
from helix_mcp_knowledge.cli.configure import available_official_versions
from helix_mcp_knowledge.sync.manifest import OfficialSourceManifest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_packaged_catalog_exposes_all_six_selectable_products() -> None:
    manifest = OfficialSourceManifest.load(
        REPOSITORY_ROOT / "config/sources/bmc-official-26.1.yaml"
    )

    assert available_official_versions(manifest, ProductCatalog()) == {
        "arsystem": ["26.3", "26.2", "26.1"],
        "business_workflows": ["26.3", "26.2", "26.1"],
        "cmdb": ["26.3", "26.2", "26.1"],
        "digital_workplace": ["26.3", "26.2", "26.1"],
        "discovery": ["current"],
        "itsm": ["26.3", "26.2", "26.1"],
    }
