"""Bounded discovery and validation of candidate BMC documentation releases."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx
import yaml
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..ingestion.chunker import Chunker
from ..ingestion.parsers.html import HtmlParser
from ..models.document import DocumentType
from ..sync.crawler import NON_CONTENT_SEGMENTS, RobotsPolicy
from ..sync.manifest import (
    OfficialCollectionDefinition,
    OfficialSourceDefinition,
    OfficialSourceManifest,
)

_RELEASE_VERSION = re.compile(r"^([0-9]{2})\.([1-4])$")
_SOFT_NOT_FOUND = ("page not found", "the requested page could not be found", "404 not found")


class _CandidateUnavailable(ValueError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogWatchTarget(_StrictModel):
    root_url_template: str = Field(min_length=1)
    local_path_prefix_template: str = Field(min_length=1)
    max_pages: int = Field(ge=1, le=5000)
    required_markers: list[str] = Field(min_length=1)
    smoke_queries: list[str] = Field(min_length=1)

    @field_validator("root_url_template")
    @classmethod
    def safe_template(cls, value: str) -> str:
        rendered = value.format(version="26.1", compact_version="261")
        parsed = urlsplit(rendered)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("catalog watch roots must have a valid HTTPS authority") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "docs.helixops.ai"
            or parsed.username
            or parsed.password
            or port not in {None, 443}
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith("/")
        ):
            raise ValueError("catalog watch roots must be credential-free BMC HTTPS URLs")
        return value


class CatalogWatchConfig(_StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    sample_pages: int = Field(default=25, ge=3, le=100)
    request_delay_seconds: float = Field(default=0.25, ge=0, le=10)
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_page_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    products: dict[str, CatalogWatchTarget]

    @classmethod
    def load(cls, path: Path) -> CatalogWatchConfig:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


@dataclass(frozen=True)
class CandidateValidation:
    product: str
    version: str
    root_url: str
    status: str
    sampled_pages: int = 0
    indexed_chunks: int = 0
    smoke_hits: dict[str, int] | None = None
    error: str | None = None


def next_release_version(versions: list[str]) -> str | None:
    """Return the next quarterly release after the newest numeric version."""

    parsed = [
        (int(match.group(1)), int(match.group(2)))
        for version in versions
        if (match := _RELEASE_VERSION.fullmatch(version))
    ]
    if not parsed:
        return None
    year, quarter = max(parsed)
    return f"{year + 1:02d}.1" if quarter == 4 else f"{year:02d}.{quarter + 1}"


def candidate_versions(manifest: OfficialSourceManifest) -> dict[str, str]:
    versions: dict[str, set[str]] = {}
    for item in manifest.collections:
        versions.setdefault(item.product, set()).add(item.version)
    return {
        product: candidate
        for product, known in sorted(versions.items())
        if (candidate := next_release_version(sorted(known))) is not None
    }


class CatalogCandidateValidator:
    def __init__(
        self,
        *,
        settings: CatalogWatchConfig,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "helix-mcp-knowledge-catalog-watch/1.0"},
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def validate(self, product: str, version: str) -> CandidateValidation:
        target = self.settings.products[product]
        compact = version.replace(".", "")
        root_url = target.root_url_template.format(version=version, compact_version=compact)
        try:
            pages = self._sample(root_url, target, version)
            chunks, hits = self._index_sample(pages, target.smoke_queries)
            if len(pages) < 3:
                raise ValueError("candidate exposes fewer than three navigable pages")
            if chunks < 3:
                raise ValueError("candidate produced fewer than three indexable chunks")
            missing_queries = [query for query, count in hits.items() if count == 0]
            if missing_queries:
                raise ValueError(
                    "temporary index returned no result for: " + ", ".join(missing_queries)
                )
            return CandidateValidation(
                product=product,
                version=version,
                root_url=root_url,
                status="validated",
                sampled_pages=len(pages),
                indexed_chunks=chunks,
                smoke_hits=hits,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403, 404}:
                return CandidateValidation(
                    product=product,
                    version=version,
                    root_url=root_url,
                    status="not_available",
                    error=f"HTTP {status}",
                )
            return CandidateValidation(
                product=product,
                version=version,
                root_url=root_url,
                status="error",
                error=f"HTTP {status}",
            )
        except _CandidateUnavailable as exc:
            return CandidateValidation(
                product=product,
                version=version,
                root_url=root_url,
                status="not_available",
                error=str(exc),
            )
        except (httpx.HTTPError, OSError, sqlite3.Error, ValueError) as exc:
            return CandidateValidation(
                product=product,
                version=version,
                root_url=root_url,
                status="error",
                error=str(exc),
            )

    def _sample(self, root_url: str, target: CatalogWatchTarget, version: str) -> dict[str, bytes]:
        root = urlsplit(root_url)
        robots = RobotsPolicy.load(self.client, root_url, "helix-mcp-knowledge-catalog-watch/1.0")
        robots.require_allowed(root_url)
        pending = deque([root_url])
        seen: set[str] = set()
        pages: dict[str, bytes] = {}
        while pending and len(pages) < self.settings.sample_pages:
            url = pending.popleft()
            if url in seen:
                continue
            seen.add(url)
            robots.require_allowed(url)
            final_url, content = self._load_page(url, root)
            text = BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
            if any(marker in text.casefold() for marker in _SOFT_NOT_FOUND):
                raise ValueError("candidate returned a soft not-found page")
            if url == root_url:
                required = [
                    marker.format(version=version).casefold() for marker in target.required_markers
                ]
                missing = [marker for marker in required if marker not in text.casefold()]
                if missing:
                    raise ValueError(
                        "candidate root is missing required markers: " + ", ".join(missing)
                    )
            pages[final_url] = content
            soup = BeautifulSoup(content, "html.parser")
            for anchor in soup.find_all("a", href=True):
                candidate = self._canonicalize(final_url, str(anchor["href"]), root)
                if candidate is not None and candidate not in seen and robots.is_allowed(candidate):
                    pending.append(candidate)
            if self.settings.request_delay_seconds:
                time.sleep(self.settings.request_delay_seconds)
        return pages

    def _load_page(self, url: str, root: SplitResult) -> tuple[str, bytes]:
        current_url = url
        for redirect_count in range(4):
            if not self._inside_root(current_url, root):
                self._raise_external_redirect(current_url)
            with self.client.stream("GET", current_url, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count == 3:
                        raise ValueError("candidate exceeded the redirect limit")
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("candidate returned a redirect without Location")
                    redirected_url = urljoin(current_url, location)
                    if not self._inside_root(redirected_url, root):
                        self._raise_external_redirect(redirected_url)
                    current_url = redirected_url
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if "html" not in content_type:
                    raise ValueError(f"candidate returned non-HTML content: {content_type}")
                declared_size = response.headers.get("content-length")
                if (
                    declared_size
                    and declared_size.isdecimal()
                    and int(declared_size) > self.settings.max_page_bytes
                ):
                    raise ValueError("candidate page exceeds the configured size limit")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > self.settings.max_page_bytes:
                        raise ValueError("candidate page exceeds the configured size limit")
                return str(response.url), bytes(content)
        raise ValueError("candidate exceeded the redirect limit")

    @staticmethod
    def _raise_external_redirect(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme == "https" and (
            parsed.path.casefold().endswith("/authorize") or "oauth" in parsed.path.casefold()
        ):
            raise _CandidateUnavailable("redirected to interactive BMC authentication")
        raise ValueError(f"candidate redirects outside its version tree: {url}")

    @staticmethod
    def _inside_root(url: str, root: SplitResult) -> bool:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname == root.hostname
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
            and parsed.path.startswith(root.path)
        )

    @staticmethod
    def _canonicalize(page_url: str, href: str, root: SplitResult) -> str | None:
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            return None
        parsed = urlsplit(urljoin(page_url, href))
        try:
            port = parsed.port
        except ValueError:
            return None
        segments = {segment.casefold() for segment in parsed.path.split("/") if segment}
        if (
            parsed.scheme != "https"
            or parsed.hostname != root.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.query
            or not parsed.path.startswith(root.path)
            or not parsed.path.endswith("/")
            or segments & NON_CONTENT_SEGMENTS
        ):
            return None
        return urlunsplit(("https", parsed.netloc.casefold(), parsed.path, "", ""))

    @staticmethod
    def _index_sample(pages: dict[str, bytes], queries: list[str]) -> tuple[int, dict[str, int]]:
        parser = HtmlParser()
        chunker = Chunker(target_tokens=600, max_tokens=900, overlap_tokens=80)
        with tempfile.TemporaryDirectory(prefix="helix-catalog-index-") as temporary:
            root = Path(temporary)
            connection = sqlite3.connect(":memory:")
            connection.execute("CREATE VIRTUAL TABLE chunks USING fts5(text)")
            chunk_count = 0
            for position, content in enumerate(pages.values()):
                path = root / f"page-{position}.html"
                path.write_bytes(content)
                for chunk in chunker.chunk(parser.parse(path)):
                    connection.execute(
                        "INSERT INTO chunks(text) VALUES (?)", (chunk.embedding_text,)
                    )
                    chunk_count += 1
            hits = {
                query: int(
                    connection.execute(
                        "SELECT count(*) FROM chunks WHERE chunks MATCH ?", (query,)
                    ).fetchone()[0]
                )
                for query in queries
            }
            connection.close()
        return chunk_count, hits


def add_validated_candidates(
    manifest: OfficialSourceManifest,
    settings: CatalogWatchConfig,
    candidates: list[CandidateValidation],
) -> OfficialSourceManifest:
    collections = list(manifest.collections)
    sources = list(manifest.sources)
    added = 0
    for candidate in candidates:
        if candidate.status != "validated":
            continue
        target = settings.products[candidate.product]
        version_slug = candidate.version.replace(".", "-")
        collection_id = f"bmc-{candidate.product.replace('_', '-')}-{version_slug}"
        source_id = f"{collection_id}-home"
        if any(item.collection_id == collection_id for item in collections):
            continue
        compact = candidate.version.replace(".", "")
        root_url = target.root_url_template.format(
            version=candidate.version, compact_version=compact
        )
        local_prefix = target.local_path_prefix_template.format(
            version=candidate.version, compact_version=compact
        )
        product = next(item for item in manifest.products if item.product_id == candidate.product)
        collections.append(
            OfficialCollectionDefinition(
                collection_id=collection_id,
                root_url=root_url,
                local_path_prefix=local_prefix,
                product=candidate.product,
                version=candidate.version,
                language="en",
                max_pages=target.max_pages,
                request_delay_seconds=settings.request_delay_seconds,
            )
        )
        sources.append(
            OfficialSourceDefinition(
                source_id=source_id,
                title=f"{product.name} {candidate.version}",
                url=root_url,
                local_path=f"{candidate.product}/{candidate.version}/home.html",
                product=candidate.product,
                version=candidate.version,
                document_type=DocumentType.CONCEPT,
                language="en",
            )
        )
        added += 1
    if not added:
        return manifest
    return OfficialSourceManifest(
        **{
            **manifest.model_dump(mode="python"),
            "catalog_revision": manifest.catalog_revision + 1,
            "collections": collections,
            "sources": sources,
        }
    )


def write_manifest(path: Path, manifest: OfficialSourceManifest) -> None:
    serialized = yaml.safe_dump(
        manifest.model_dump(mode="json", exclude_defaults=False),
        sort_keys=False,
        allow_unicode=True,
    )
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
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def markdown_report(results: list[CandidateValidation]) -> str:
    lines = [
        "## Automated BMC catalog watch",
        "",
        "The monitor only proposes catalog entries that passed bounded navigation and "
        "temporary FTS indexing checks.",
        "",
        "| Product | Candidate | Status | Pages | Chunks | Details |",
        "|---|---:|---|---:|---:|---|",
    ]
    for result in results:
        details = result.error or ", ".join(
            f"{query}: {count}" for query, count in (result.smoke_hits or {}).items()
        )
        lines.append(
            f"| `{result.product}` | `{result.version}` | {result.status} | "
            f"{result.sampled_pages} | {result.indexed_chunks} | {details or '—'} |"
        )
    lines.extend(
        [
            "",
            "Catalog publication and user selection remain separate, manual decisions.",
            "",
        ]
    )
    return "\n".join(lines)
