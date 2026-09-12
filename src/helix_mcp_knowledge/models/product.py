"""BMC product and version contracts."""

from pydantic import BaseModel, ConfigDict, Field


class BmcProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    active: bool = True


class ProductSummary(BaseModel):
    product_id: str
    name: str
    aliases: list[str]
    configured: bool
    indexed: bool


class ProductListResponse(BaseModel):
    products: list[ProductSummary]


class VersionInfo(BaseModel):
    version: str
    indexed: bool = True


class VersionListResponse(BaseModel):
    product: str
    versions: list[VersionInfo]
