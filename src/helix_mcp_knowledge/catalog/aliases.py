"""Alias helpers retained as a stable extension point."""

from .products import ProductCatalog


def resolve_product_alias(value: str) -> str:
    return ProductCatalog().resolve(value).product_id
