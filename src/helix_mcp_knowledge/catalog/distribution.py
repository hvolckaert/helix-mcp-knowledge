"""Safe adoption of official catalog additions bundled with a release."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import AppConfig
from ..errors import ConfigurationError
from ..sync.manifest import OfficialSourceManifest
from ..workspace import read_packaged_resource

PACKAGED_MANIFEST = "config/sources/bmc-official-26.1.yaml"


@dataclass(frozen=True)
class CatalogMergeResult:
    added_products: tuple[str, ...] = ()
    added_collections: tuple[str, ...] = ()
    added_sources: tuple[str, ...] = ()
    replaced_products: tuple[str, ...] = ()
    replaced_collections: tuple[str, ...] = ()
    replaced_sources: tuple[str, ...] = ()
    preserved_conflicts: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(
            self.added_products
            or self.added_collections
            or self.added_sources
            or self.replaced_products
            or self.replaced_collections
            or self.replaced_sources
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "added_products": list(self.added_products),
            "added_collections": list(self.added_collections),
            "added_sources": list(self.added_sources),
            "replaced_products": list(self.replaced_products),
            "replaced_collections": list(self.replaced_collections),
            "replaced_sources": list(self.replaced_sources),
            "preserved_conflicts": list(self.preserved_conflicts),
        }


def merge_official_catalogs(
    current: OfficialSourceManifest,
    packaged: OfficialSourceManifest,
    *,
    replace_conflicts: bool = False,
) -> tuple[OfficialSourceManifest, CatalogMergeResult]:
    """Merge catalog entries, optionally preferring a newer authoritative snapshot."""

    current_products = {item.product_id: item for item in current.products}
    current_collections = {item.collection_id: item for item in current.collections}
    current_sources = {item.source_id: item for item in current.sources}
    current_source_urls = {item.url for item in current.sources}
    current_source_paths = {item.local_path.casefold() for item in current.sources}
    new_products = [item for item in packaged.products if item.product_id not in current_products]
    new_collections = [
        item for item in packaged.collections if item.collection_id not in current_collections
    ]
    new_sources = [
        item
        for item in packaged.sources
        if item.source_id not in current_sources
        and item.url not in current_source_urls
        and item.local_path.casefold() not in current_source_paths
    ]
    added_collections = tuple(item.collection_id for item in new_collections)
    added_sources = tuple(item.source_id for item in new_sources)
    product_conflicts = [
        f"product:{item.product_id}"
        for item in packaged.products
        if item.product_id in current_products
        and current_products[item.product_id].model_dump(mode="json")
        != item.model_dump(mode="json")
    ]
    collection_conflicts = [
        f"collection:{item.collection_id}"
        for item in packaged.collections
        if item.collection_id in current_collections
        and current_collections[item.collection_id].model_dump(mode="json")
        != item.model_dump(mode="json")
    ]
    source_conflicts = [
        f"source:{item.source_id}"
        for item in packaged.sources
        if item.source_id in current_sources
        and current_sources[item.source_id].model_dump(mode="json") != item.model_dump(mode="json")
    ]
    source_collisions = [
        f"source:{item.source_id}"
        for item in packaged.sources
        if item.source_id not in current_sources
        and (item.url in current_source_urls or item.local_path.casefold() in current_source_paths)
    ]
    conflicts = [*product_conflicts, *collection_conflicts, *source_conflicts, *source_collisions]
    incoming_products = {item.product_id: item for item in packaged.products}
    incoming_collections = {item.collection_id: item for item in packaged.collections}
    incoming_sources = {item.source_id: item for item in packaged.sources}
    replaced_products = tuple(item.split(":", 1)[1] for item in product_conflicts)
    replaced_collections = tuple(item.split(":", 1)[1] for item in collection_conflicts)
    replaced_sources = tuple(item.split(":", 1)[1] for item in source_conflicts)

    products = [
        incoming_products.get(item.product_id, item) if replace_conflicts else item
        for item in current.products
    ]
    collections = [
        incoming_collections.get(item.collection_id, item) if replace_conflicts else item
        for item in current.collections
    ]
    sources = [
        incoming_sources.get(item.source_id, item) if replace_conflicts else item
        for item in current.sources
    ]
    merged = OfficialSourceManifest(
        schema_version=max(current.schema_version, packaged.schema_version),
        catalog_revision=max(current.catalog_revision, packaged.catalog_revision),
        products=[*products, *new_products],
        publisher=packaged.publisher if replace_conflicts else current.publisher,
        collections=[
            *collections,
            *new_collections,
        ],
        sources=[
            *sources,
            *new_sources,
        ],
    )
    return merged, CatalogMergeResult(
        added_products=tuple(item.product_id for item in new_products),
        added_collections=added_collections,
        added_sources=added_sources,
        replaced_products=replaced_products if replace_conflicts else (),
        replaced_collections=replaced_collections if replace_conflicts else (),
        replaced_sources=replaced_sources if replace_conflicts else (),
        preserved_conflicts=tuple(source_collisions if replace_conflicts else conflicts),
    )


def reconcile_packaged_official_catalog(config: AppConfig) -> CatalogMergeResult:
    """Merge catalog additions from this runtime into the user's manifest atomically."""

    current = OfficialSourceManifest.load(config.official_manifest_path)
    try:
        packaged_payload = yaml.safe_load(read_packaged_resource(PACKAGED_MANIFEST)) or {}
        packaged = OfficialSourceManifest.model_validate(packaged_payload)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"invalid packaged official catalog: {exc}") from exc
    merged, result = merge_official_catalogs(current, packaged)
    if result.changed:
        serialized = yaml.safe_dump(
            merged.model_dump(mode="json", exclude_defaults=False),
            sort_keys=False,
            allow_unicode=True,
        )
        _atomic_write(config.official_manifest_path, serialized)
    return result


def load_effective_official_catalog(config: AppConfig) -> OfficialSourceManifest:
    """Load the local catalog plus checksum-verified remote additions, if present."""

    local = OfficialSourceManifest.load(config.official_manifest_path)
    cached_path = config.official_catalog_cache_path
    if not cached_path.is_file() or cached_path.is_symlink():
        return local
    try:
        cached = OfficialSourceManifest.load(cached_path)
    except ConfigurationError:
        return local
    merged, _ = merge_official_catalogs(
        local,
        cached,
        replace_conflicts=(
            cached.catalog_revision > 0 and cached.catalog_revision >= local.catalog_revision
        ),
    )
    return merged


def _atomic_write(path: Path, content: str) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
