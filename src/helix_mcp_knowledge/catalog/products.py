"""Data-driven BMC product catalog with a legacy schema-1 fallback."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..errors import SearchValidationError
from ..models.product import BmcProduct

if TYPE_CHECKING:
    from ..sync.manifest import OfficialSourceManifest

# Kept only so workspaces created before schema 2 remain readable. New products
# belong in the revisioned manifest and do not require a runtime code change.
PRODUCTS = (
    BmcProduct(
        product_id="arsystem",
        name="BMC Helix Innovation Suite / AR System",
        aliases=["AR System", "ARS", "Remedy AR System"],
    ),
    BmcProduct(
        product_id="cmdb",
        name="BMC Helix CMDB",
        aliases=["CMDB", "BMC CMDB", "Helix CMDB"],
    ),
    BmcProduct(
        product_id="discovery",
        name="BMC Helix Discovery",
        aliases=["Discovery", "BMC Discovery", "ADDM"],
    ),
    BmcProduct(
        product_id="itsm",
        name="BMC Helix ITSM",
        aliases=["ITSM", "BMC ITSM", "Remedy ITSM"],
    ),
    BmcProduct(
        product_id="digital_workplace",
        name="BMC Helix Digital Workplace",
        aliases=["DWP", "Digital Workplace"],
    ),
    BmcProduct(
        product_id="business_workflows",
        name="BMC Helix Business Workflows",
        aliases=["BWF", "Business Workflows"],
    ),
)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


class ProductCatalog:
    def __init__(self, products: Iterable[BmcProduct] | None = None) -> None:
        self.replace(products or PRODUCTS)

    @classmethod
    def from_manifest(cls, manifest: OfficialSourceManifest) -> ProductCatalog:
        products = [
            BmcProduct.model_validate(product.model_dump(mode="json"))
            for product in manifest.products
        ]
        return cls(products or PRODUCTS)

    def replace(self, products: Iterable[BmcProduct]) -> None:
        product_map: dict[str, BmcProduct] = {}
        aliases: dict[str, str] = {}
        for raw_product in products:
            product = BmcProduct.model_validate(raw_product.model_dump(mode="json"))
            if product.product_id in product_map:
                raise ValueError(f"duplicate BMC product ID: {product.product_id}")
            product_map[product.product_id] = product
            for alias in [product.product_id, product.name, *product.aliases]:
                key = _normalize(alias)
                existing = aliases.get(key)
                if existing is not None and existing != product.product_id:
                    raise ValueError(
                        f"BMC product alias {alias!r} is shared by {existing} "
                        f"and {product.product_id}"
                    )
                aliases[key] = product.product_id
        self._products = product_map
        self._aliases = aliases

    def resolve(self, value: str) -> BmcProduct:
        product_id = self._aliases.get(_normalize(value))
        if product_id is None:
            raise SearchValidationError(f"unknown BMC product: {value}")
        return self._products[product_id]

    def get(self, product_id: str) -> BmcProduct:
        return self._products[product_id]

    def all(self) -> list[BmcProduct]:
        return list(self._products.values())
