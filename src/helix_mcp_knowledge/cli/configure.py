"""Persistent configuration of official BMC product and version selections."""

import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

from ..catalog.distribution import (
    load_effective_official_catalog,
    reconcile_packaged_official_catalog,
)
from ..catalog.products import ProductCatalog
from ..catalog.versions import normalize_version, version_sort_key
from ..config import AppConfig, OfficialDocsSettings, load_config
from ..errors import ConfigurationError
from ..sync.manifest import OfficialSourceManifest


def configure_github_cli(config_path: str | Path, gh_command: str | Path) -> Path:
    """Persist one verified GitHub CLI path for runtime and catalog checks."""

    path = Path(config_path).expanduser().resolve()
    candidate = str(gh_command)
    discovered = shutil.which(candidate)
    resolved = (
        Path(discovered).expanduser().resolve()
        if discovered
        else Path(candidate).expanduser().resolve()
    )
    if not resolved.is_file() or (os.name != "nt" and not os.access(resolved, os.X_OK)):
        raise ConfigurationError(f"GitHub CLI command was not found or executable: {candidate}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for section_name in ("updates", "catalog_updates"):
            section = payload.setdefault(section_name, {})
            if not isinstance(section, dict):
                raise ConfigurationError(f"configuration section {section_name} must be a mapping")
            section["gh_command"] = str(resolved)
        AppConfig.model_validate(payload)
        serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        _atomic_write(path, serialized)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"unable to update configuration {path}: {exc}") from exc
    return resolved


def configure_official_docs(
    config_path: str | Path,
    *,
    product_specs: list[str] | None,
    no_products: bool,
    automatic_sync: bool | None,
    bootstrap_on_empty: bool | None,
    interval_hours: float | None,
    retain_unselected_versions: bool | None,
) -> OfficialDocsSettings:
    path = Path(config_path).expanduser().resolve()
    config = load_config(path)
    reconcile_packaged_official_catalog(config)
    manifest = load_effective_official_catalog(config)
    catalog = ProductCatalog.from_manifest(manifest)
    available = available_official_versions(manifest, catalog)

    if no_products:
        selected: dict[str, list[str]] = {}
    else:
        specs = product_specs
        if specs is None:
            specs = _prompt_for_products(config.official_docs, available, catalog)
        selected = _parse_product_specs(specs, available, catalog)

    current = config.official_docs
    settings = OfficialDocsSettings(
        automatic_sync=(current.automatic_sync if automatic_sync is None else automatic_sync),
        bootstrap_on_empty=(
            current.bootstrap_on_empty if bootstrap_on_empty is None else bootstrap_on_empty
        ),
        interval_hours=current.interval_hours if interval_hours is None else interval_hours,
        retain_unselected_versions=(
            current.retain_unselected_versions
            if retain_unselected_versions is None
            else retain_unselected_versions
        ),
        products={product: {"versions": versions} for product, versions in selected.items()},
    )

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        payload["official_docs"] = settings.model_dump(mode="json")
        AppConfig.model_validate(payload)
        serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        _atomic_write(path, serialized)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"unable to update configuration {path}: {exc}") from exc
    return load_config(path).official_docs


def available_official_versions(
    manifest: OfficialSourceManifest, catalog: ProductCatalog
) -> dict[str, list[str]]:
    available: dict[str, set[str]] = defaultdict(set)
    for item in [*manifest.sources, *manifest.collections]:
        product_id = catalog.resolve(item.product).product_id
        available[product_id].add(normalize_version(item.version))
    return {
        product: sorted(versions, key=version_sort_key, reverse=True)
        for product, versions in sorted(available.items())
    }


def _parse_product_specs(
    specs: list[str],
    available: dict[str, list[str]],
    catalog: ProductCatalog,
) -> dict[str, list[str]]:
    selected: dict[str, set[str]] = defaultdict(set)
    for spec in specs:
        product, separator, version = spec.partition("=")
        if not separator or not product.strip() or not version.strip():
            raise ConfigurationError(
                f"invalid official product selection {spec!r}; expected PRODUCT=VERSION"
            )
        product_id = catalog.resolve(product.strip()).product_id
        normalized_version = normalize_version(version)
        if product_id not in available or normalized_version not in available[product_id]:
            choices = ", ".join(available.get(product_id, [])) or "none"
            raise ConfigurationError(
                f"official documentation {product_id}={normalized_version} is unavailable; "
                f"available versions: {choices}"
            )
        selected[product_id].add(normalized_version)
    return {
        product: sorted(versions, key=version_sort_key, reverse=True)
        for product, versions in sorted(selected.items())
    }


def _prompt_for_products(
    current: OfficialDocsSettings,
    available: dict[str, list[str]],
    catalog: ProductCatalog,
) -> list[str]:
    print("Available official BMC documentation:")
    for product, versions in available.items():
        print(f"- {product}: {catalog.get(product).name} ({', '.join(versions)})")
    defaults = [
        f"{product}={version}"
        for product, settings in current.products.items()
        for version in settings.versions
    ]
    default_text = ",".join(defaults)
    try:
        raw = input(
            f"Products and versions, comma-separated as PRODUCT=VERSION [{default_text}]: "
        ).strip()
    except EOFError as exc:
        raise ConfigurationError(
            "interactive configuration needs a terminal; use --product or --no-products"
        ) from exc
    if not raw:
        return defaults
    return [value.strip() for value in raw.split(",") if value.strip()]


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
