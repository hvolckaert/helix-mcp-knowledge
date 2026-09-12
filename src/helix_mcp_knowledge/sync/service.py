"""Curated official-source synchronization and ingestion orchestration."""

import logging
import os
import re
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

import httpx

from ..catalog.distribution import load_effective_official_catalog
from ..catalog.products import ProductCatalog
from ..catalog.versions import normalize_version
from ..config import AppConfig
from ..errors import IngestionError, OfficialSyncCancelled, SourceNotFoundError, SourceSyncError
from ..ingestion.manager import IngestionManager
from ..models.document import DocumentType
from ..models.ingestion import IngestRequest
from ..models.project import Classification
from ..models.source import SourceScope, SourceType
from ..official_cleanup import OfficialCorpusCleaner
from ..storage.sources import SourceStore
from .browser import OfficialBrowserDownloader
from .crawler import OfficialLinkCrawler, RobotsPolicy
from .http import DownloadResult, OfficialHttpDownloader
from .manifest import (
    OfficialCollectionDefinition,
    OfficialSourceDefinition,
    OfficialSourceManifest,
)


class SourceDownloader(Protocol):
    def fetch(
        self, url: str, destination: Path, state: dict[str, object] | None = None
    ) -> DownloadResult: ...


ProgressCallback = Callable[[dict[str, object]], None]
CancelCheck = Callable[[], bool]
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceSyncResult:
    source_id: str
    status: str
    source_url: str
    source_path: str
    document_id: str | None = None
    chunks_indexed: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class _SyncProgressReporter:
    """Build a safe, aggregate progress view for the detached worker."""

    def __init__(
        self,
        *,
        callback: ProgressCallback | None,
        catalog: ProductCatalog,
        selected: list[OfficialSourceDefinition],
        collections: list[OfficialCollectionDefinition],
        source_store: SourceStore,
    ) -> None:
        self.callback = callback
        self.catalog = catalog
        self.processed_items = 0
        self.result_counts: Counter[str] = Counter()
        self.chunks_indexed = 0
        self.phase = "preparing"
        self.current_product: str | None = None
        self.current_version: str | None = None
        self._explicit_estimates: Counter[tuple[str, str]] = Counter(
            self._pair(source.product, source.version) for source in selected
        )
        self._collection_estimates: dict[str, tuple[tuple[str, str], int]] = {}
        for collection in collections:
            known = len(source_store.collection_sources(collection.collection_id))
            previous = source_store.collection_state(collection.collection_id).get("pages_seen")
            estimate = previous if isinstance(previous, int) and previous > 0 else known
            self._collection_estimates[collection.collection_id] = (
                self._pair(collection.product, collection.version),
                max(1, estimate),
            )
        self._processed_by_pair: Counter[tuple[str, str]] = Counter()
        self.emit()

    def _pair(self, product: str, version: str) -> tuple[str, str]:
        return self.catalog.resolve(product).product_id, normalize_version(version)

    def begin(
        self,
        phase: str,
        *,
        product: str | None = None,
        version: str | None = None,
    ) -> None:
        self.phase = phase
        if product is not None and version is not None:
            self.current_product, self.current_version = self._pair(product, version)
        self.emit()

    def revise_collection_estimate(
        self, collection: OfficialCollectionDefinition, estimated_items: int
    ) -> None:
        pair = self._pair(collection.product, collection.version)
        self._collection_estimates[collection.collection_id] = (pair, max(1, estimated_items))

    def add_cleanup_estimate(self, pairs: list[tuple[str, str]]) -> None:
        self._explicit_estimates.update(pairs)
        self.emit()

    def finish(self) -> None:
        """Publish an exact terminal snapshot after all work has completed."""
        self.phase = "finishing"
        self._explicit_estimates = Counter(self._processed_by_pair)
        self._collection_estimates.clear()
        self.emit()

    def advance(
        self,
        *,
        product: str,
        version: str,
        result: SourceSyncResult | None = None,
    ) -> None:
        pair = self._pair(product, version)
        self.current_product, self.current_version = pair
        self.processed_items += 1
        self._processed_by_pair[pair] += 1
        if result is not None:
            self.result_counts[result.status] += 1
            self.chunks_indexed += result.chunks_indexed
        self.emit()

    def emit(self) -> None:
        if self.callback is None:
            return
        estimates = Counter(self._explicit_estimates)
        for pair, count in self._collection_estimates.values():
            estimates[pair] += count
        for pair, processed in self._processed_by_pair.items():
            estimates[pair] = max(estimates[pair], processed)
        estimated_total = sum(estimates.values())
        percent = (
            round(min(100.0, self.processed_items * 100 / estimated_total), 1)
            if estimated_total
            else None
        )
        pairs = sorted(set(estimates) | set(self._processed_by_pair))
        self.callback(
            {
                "phase": self.phase,
                "processed_items": self.processed_items,
                "estimated_total_items": estimated_total,
                "percent": percent,
                "current_product": self.current_product,
                "current_version": self.current_version,
                "last_activity_at": datetime.now(UTC).isoformat(),
                "result_counts": dict(sorted(self.result_counts.items())),
                "chunks_indexed": self.chunks_indexed,
                "product_versions": [
                    {
                        "product": product,
                        "version": version,
                        "processed_items": self._processed_by_pair[(product, version)],
                        "estimated_total_items": estimates[(product, version)],
                    }
                    for product, version in pairs
                ],
            }
        )


class OfficialSourceSynchronizer:
    def __init__(
        self,
        *,
        config: AppConfig,
        ingestion_manager: IngestionManager,
        source_store: SourceStore,
        catalog: ProductCatalog,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.ingestion_manager = ingestion_manager
        self.source_store = source_store
        self.catalog = catalog
        self.client = client

    def sync(
        self,
        source_ids: set[str] | None = None,
        *,
        collection_ids: set[str] | None = None,
        discover: bool = True,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> list[SourceSyncResult]:
        manifest = load_effective_official_catalog(self.config)
        selected_pairs = self._selected_product_versions(manifest)
        selected = [
            source
            for source in manifest.sources
            if source.enabled
            and self._definition_pair(source.product, source.version) in selected_pairs
            and (source_ids is None or source.source_id in source_ids)
        ]
        if source_ids is not None:
            missing = source_ids - {source.source_id for source in manifest.sources}
            if missing:
                raise SourceSyncError("unknown official source IDs: " + ", ".join(sorted(missing)))
            disabled = source_ids - {source.source_id for source in selected}
            if disabled:
                raise SourceSyncError(
                    "official source IDs are not selected in official_docs: "
                    + ", ".join(sorted(disabled))
                )
        if collection_ids is not None:
            missing_collections = collection_ids - {
                collection.collection_id for collection in manifest.collections
            }
            if missing_collections:
                raise SourceSyncError(
                    "unknown official collection IDs: " + ", ".join(sorted(missing_collections))
                )
        enabled_collection_ids = {
            collection.collection_id
            for collection in manifest.collections
            if collection.enabled
            and self._definition_pair(collection.product, collection.version) in selected_pairs
        }
        if collection_ids is not None:
            disabled_collections = collection_ids - enabled_collection_ids
            if disabled_collections:
                raise SourceSyncError(
                    "official collection IDs are not selected in official_docs: "
                    + ", ".join(sorted(disabled_collections))
                )
            enabled_collection_ids &= collection_ids
        active_collections = [
            collection
            for collection in manifest.collections
            if collection.enabled and collection.collection_id in enabled_collection_ids
        ]
        reporter = _SyncProgressReporter(
            callback=progress_callback,
            catalog=self.catalog,
            selected=selected,
            collections=active_collections if discover and not source_ids else [],
            source_store=self.source_store,
        )
        self._require_not_cancelled(cancel_check)
        if self.client is not None:
            downloader = OfficialHttpDownloader(
                client=self.client,
                allowed_domains=self.config.ingestion.http.allowed_domains,
                max_bytes=self.config.ingestion.max_file_size_mb * 1024 * 1024,
            )
            results = self._run(
                manifest,
                selected,
                downloader,
                self.client,
                discover and not source_ids,
                enabled_collection_ids,
                reporter,
                cancel_check,
            )
            return self._finalize(
                results,
                manifest,
                selected_pairs,
                source_ids,
                collection_ids,
                reporter,
                cancel_check,
            )
        settings = self.config.ingestion.http
        if settings.authentication.mode == "browser":
            with (
                OfficialBrowserDownloader(self.config) as downloader,
                self._http_client(None) as robots_client,
            ):
                results = self._run(
                    manifest,
                    selected,
                    downloader,
                    robots_client,
                    discover and not source_ids,
                    enabled_collection_ids,
                    reporter,
                    cancel_check,
                )
                return self._finalize(
                    results,
                    manifest,
                    selected_pairs,
                    source_ids,
                    collection_ids,
                    reporter,
                    cancel_check,
                )
        authentication = self._authentication()
        with self._http_client(authentication) as client:
            downloader = OfficialHttpDownloader(
                client=client,
                allowed_domains=self.config.ingestion.http.allowed_domains,
                max_bytes=self.config.ingestion.max_file_size_mb * 1024 * 1024,
            )
            results = self._run(
                manifest,
                selected,
                downloader,
                client,
                discover and not source_ids,
                enabled_collection_ids,
                reporter,
                cancel_check,
            )
            return self._finalize(
                results,
                manifest,
                selected_pairs,
                source_ids,
                collection_ids,
                reporter,
                cancel_check,
            )

    def _selected_product_versions(self, manifest: OfficialSourceManifest) -> set[tuple[str, str]]:
        if not self.config.official_docs.products:
            if self.config.official_docs.retain_unselected_versions:
                raise SourceSyncError(
                    "no official product/version is selected; run helix-mcp-knowledge configure"
                )
            return set()
        selected = {
            (self.catalog.resolve(product).product_id, normalize_version(version))
            for product, settings in self.config.official_docs.products.items()
            for version in settings.versions
        }
        available = {
            self._definition_pair(item.product, item.version)
            for item in [*manifest.sources, *manifest.collections]
        }
        unavailable = selected - available
        if unavailable:
            values = ", ".join(f"{product}={version}" for product, version in sorted(unavailable))
            raise SourceSyncError(
                "official product/version selections are not available in the manifest: " + values
            )
        return selected

    def _definition_pair(self, product: str, version: str) -> tuple[str, str]:
        return self.catalog.resolve(product).product_id, normalize_version(version)

    def _finalize(
        self,
        results: list[SourceSyncResult],
        manifest: OfficialSourceManifest,
        selected_pairs: set[tuple[str, str]],
        source_ids: set[str] | None,
        collection_ids: set[str] | None,
        reporter: _SyncProgressReporter,
        cancel_check: CancelCheck | None,
    ) -> list[SourceSyncResult]:
        self._require_not_cancelled(cancel_check)
        if (
            self.config.official_docs.retain_unselected_versions
            or source_ids is not None
            or collection_ids is not None
            or any(result.status == "error" for result in results)
        ):
            reporter.finish()
            return results
        unavailable_pairs = self._unindexed_pairs(selected_pairs)
        if unavailable_pairs:
            for product, version in sorted(unavailable_pairs):
                result = SourceSyncResult(
                    source_id=f"cleanup-deferred-{product}-{version}",
                    status="error",
                    source_url="",
                    source_path="",
                    error=(
                        "storage cleanup was deferred because the selected official "
                        f"documentation {product}={version} has no indexed evidence"
                    ),
                )
                results.append(result)
                reporter.advance(product=product, version=version, result=result)
            reporter.finish()
            return results
        cleaner = OfficialCorpusCleaner(
            database=self.ingestion_manager.store.database,
            vector_index=self.ingestion_manager.vector_index,
            official_sources_root=self.ingestion_manager.official_sources_root,
            catalog=self.catalog,
            manifest=manifest,
        )
        cleanup_preview = cleaner.preview(selected_pairs)
        incomplete_collections = self._incomplete_selected_collections(manifest, selected_pairs)
        if cleanup_preview.required and incomplete_collections:
            for collection_id, product, version, status in incomplete_collections:
                result = SourceSyncResult(
                    source_id=f"cleanup-deferred-{collection_id}",
                    status="cleanup_deferred",
                    source_url="",
                    source_path="",
                    error=(
                        "storage cleanup was deferred because selected official collection "
                        f"{collection_id} ({product}={version}) is {status}"
                    ),
                )
                results.append(result)
                reporter.advance(product=product, version=version, result=result)
            reporter.finish()
            return results
        reporter.begin("cleaning_up")
        purged = self._purge_unselected(
            manifest,
            selected_pairs,
            reporter,
            cancel_check,
            cleaner=cleaner,
        )
        reporter.finish()
        return [*results, *purged]

    def _purge_unselected(
        self,
        manifest: OfficialSourceManifest,
        selected_pairs: set[tuple[str, str]],
        reporter: _SyncProgressReporter,
        cancel_check: CancelCheck | None,
        *,
        cleaner: OfficialCorpusCleaner | None = None,
    ) -> list[SourceSyncResult]:
        cleaner = cleaner or OfficialCorpusCleaner(
            database=self.ingestion_manager.store.database,
            vector_index=self.ingestion_manager.vector_index,
            official_sources_root=self.ingestion_manager.official_sources_root,
            catalog=self.catalog,
            manifest=manifest,
        )
        preview = cleaner.preview(selected_pairs)
        cleanup_pairs = [
            (item.product, item.version)
            for item in preview.product_versions
            for _ in range(max(1, item.documents))
        ]
        reporter.add_cleanup_estimate(cleanup_pairs)
        self._require_not_cancelled(cancel_check)
        report = cleaner.purge(selected_pairs)
        for warning in report.warnings:
            LOGGER.warning("official corpus cleanup warning: %s", warning)
        purged: list[SourceSyncResult] = []
        for document in report.documents:
            result = SourceSyncResult(
                source_id=f"purged-{document.document_id}",
                status="purged",
                source_url=document.source_url,
                source_path=document.source_path,
                document_id=document.document_id,
            )
            purged.append(result)
            reporter.advance(
                product=document.product,
                version=document.version,
                result=result,
            )
        if report.warnings:
            purged.append(
                SourceSyncResult(
                    source_id="cleanup-retry-pending",
                    status="error",
                    source_url="",
                    source_path="",
                    error="; ".join(report.warnings),
                )
            )
        return purged

    def _unindexed_pairs(self, selected_pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
        if not selected_pairs:
            return set()
        with self.ingestion_manager.store.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT pv.product_id, pv.version
                FROM documents d
                JOIN document_product_versions dpv
                  ON dpv.document_id = d.document_id
                JOIN product_versions pv
                  ON pv.product_version_id = dpv.product_version_id
                WHERE d.source_scope = 'bmc_official' AND d.status = 'indexed'
                """
            ).fetchall()
        indexed = {(str(row["product_id"]), normalize_version(str(row["version"]))) for row in rows}
        return selected_pairs - indexed

    def _incomplete_selected_collections(
        self,
        manifest: OfficialSourceManifest,
        selected_pairs: set[tuple[str, str]],
    ) -> list[tuple[str, str, str, str]]:
        incomplete: list[tuple[str, str, str, str]] = []
        for collection in manifest.collections:
            pair = self._definition_pair(collection.product, collection.version)
            if not collection.enabled or pair not in selected_pairs:
                continue
            state = self.source_store.collection_state(collection.collection_id)
            status = str(state.get("status") or "not_started")
            if status != "complete":
                incomplete.append((collection.collection_id, pair[0], pair[1], status))
        return incomplete

    def _run(
        self,
        manifest: OfficialSourceManifest,
        selected: list[OfficialSourceDefinition],
        downloader: SourceDownloader,
        robots_client: httpx.Client,
        discover: bool,
        collection_ids: set[str] | None,
        reporter: _SyncProgressReporter,
        cancel_check: CancelCheck | None,
    ) -> list[SourceSyncResult]:
        results = self._sync_selected(selected, downloader, reporter, cancel_check)
        if not discover:
            return results
        synced_paths = {
            result.source_url: Path(result.source_path)
            for result in results
            if result.status != "error"
        }
        explicit_by_url = {source.url: source for source in selected}
        for collection in manifest.collections:
            if collection.enabled and (
                collection_ids is None or collection.collection_id in collection_ids
            ):
                results.extend(
                    self._crawl_collection(
                        collection,
                        downloader,
                        robots_client,
                        explicit_by_url,
                        synced_paths,
                        reporter,
                        cancel_check,
                    )
                )
        return results

    def _sync_selected(
        self,
        selected: list[OfficialSourceDefinition],
        downloader: SourceDownloader,
        reporter: _SyncProgressReporter,
        cancel_check: CancelCheck | None,
    ) -> list[SourceSyncResult]:
        results: list[SourceSyncResult] = []
        for source in selected:
            self._require_not_cancelled(cancel_check)
            reporter.begin("downloading", product=source.product, version=source.version)
            result = self._sync_one(source, downloader)
            results.append(result)
            reporter.advance(product=source.product, version=source.version, result=result)
        return results

    def _sync_one(
        self, source: OfficialSourceDefinition, downloader: SourceDownloader
    ) -> SourceSyncResult:
        destination = self._destination(source)
        self.source_store.upsert(source, str(destination))
        previous_state = self.source_store.state(source.source_id)
        try:
            product_id = self.catalog.resolve(source.product).product_id
            version = normalize_version(source.version)
            download = downloader.fetch(source.url, destination, previous_state)
            ingested = self.ingestion_manager.ingest(
                IngestRequest(
                    source_path=destination,
                    source_scope=SourceScope.BMC_OFFICIAL,
                    document_type=source.document_type,
                    language=source.language,
                    classification=Classification.PUBLIC,
                    product_versions={product_id: version},
                    title=source.title,
                    source_url=source.url,
                    source_type=SourceType.BMC_PUBLIC_URL,
                    etag=download.etag,
                    last_modified=download.last_modified,
                    metadata={
                        "publisher": "BMC Software",
                        "official_source_id": source.source_id,
                        "canonical_url": source.url,
                        **source.metadata,
                    },
                )
            )
            self.source_store.update_state(
                source.source_id,
                {
                    "etag": download.etag,
                    "last_modified": download.last_modified,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "last_status": ingested.status,
                    "document_id": ingested.document_id,
                    "content_hash": ingested.content_hash,
                },
            )
            return SourceSyncResult(
                source_id=source.source_id,
                status=ingested.status,
                source_url=source.url,
                source_path=str(destination),
                document_id=ingested.document_id,
                chunks_indexed=ingested.chunks_indexed,
            )
        except SourceNotFoundError as exc:
            self.source_store.update_state(
                source.source_id,
                {
                    **previous_state,
                    "last_attempt_at": datetime.now(UTC).isoformat(),
                    "last_status": "missing",
                    "last_error": str(exc),
                },
            )
            return SourceSyncResult(
                source_id=source.source_id,
                status="missing",
                source_url=source.url,
                source_path=str(destination),
                error=str(exc),
            )
        except IngestionError as exc:
            if "document produced no indexable chunks" not in str(exc):
                return self._error_result(source, destination, previous_state, exc)
            self.source_store.update_state(
                source.source_id,
                {
                    **previous_state,
                    "last_attempt_at": datetime.now(UTC).isoformat(),
                    "last_status": "empty",
                    "last_error": str(exc),
                },
            )
            return SourceSyncResult(
                source_id=source.source_id,
                status="empty",
                source_url=source.url,
                source_path=str(destination),
                error=str(exc),
            )
        except Exception as exc:
            return self._error_result(source, destination, previous_state, exc)

    def _error_result(
        self,
        source: OfficialSourceDefinition,
        destination: Path,
        previous_state: dict[str, object],
        exc: Exception,
    ) -> SourceSyncResult:
        self.source_store.update_state(
            source.source_id,
            {
                **previous_state,
                "last_attempt_at": datetime.now(UTC).isoformat(),
                "last_status": "error",
                "last_error": str(exc),
            },
        )
        return SourceSyncResult(
            source_id=source.source_id,
            status="error",
            source_url=source.url,
            source_path=str(destination),
            error=str(exc),
        )

    def _crawl_collection(
        self,
        collection: OfficialCollectionDefinition,
        downloader: SourceDownloader,
        robots_client: httpx.Client,
        explicit_by_url: dict[str, OfficialSourceDefinition],
        synced_paths: dict[str, Path],
        reporter: _SyncProgressReporter,
        cancel_check: CancelCheck | None,
    ) -> list[SourceSyncResult]:
        reporter.begin("discovering", product=collection.product, version=collection.version)
        self._require_not_cancelled(cancel_check)
        robots = RobotsPolicy.load(
            robots_client, collection.root_url, self.config.ingestion.http.user_agent
        )
        crawler = OfficialLinkCrawler(
            collection,
            allowed_domains=self.config.ingestion.http.allowed_domains,
            robots=robots,
        )
        queue = deque([collection.root_url])
        seen_urls: set[str] = set()
        seen_source_ids: set[str] = set()
        results: list[SourceSyncResult] = []
        known_paths = self.source_store.collection_sources(collection.collection_id)
        previous_collection_state = self.source_store.collection_state(collection.collection_id)
        # Cached pages are reused only while continuing a bounded crawl. Once a
        # collection is complete, a later scheduled run must revalidate every
        # discovered page so periodic synchronization can detect changes.
        resume = previous_collection_state.get("status") == "truncated"
        new_pages = 0
        while queue and new_pages < collection.max_pages:
            self._require_not_cancelled(cancel_check)
            url = queue.popleft()
            if url in seen_urls:
                continue
            robots.require_allowed(url)
            source = explicit_by_url.get(url) or self._discovered_source(collection, url)
            seen_urls.add(url)
            seen_source_ids.add(source.source_id)
            source_path = synced_paths.get(url)
            if source_path is None and resume and url in known_paths:
                cached_path = Path(known_paths[url])
                previous_source_state = self.source_store.state(source.source_id)
                if cached_path.is_file() and previous_source_state.get("last_status") != "error":
                    source_path = cached_path
            if source_path is None:
                new_pages += 1
                result = self._sync_one(source, downloader)
                results.append(result)
                reporter.advance(
                    product=collection.product,
                    version=collection.version,
                    result=result,
                )
                if result.status in {"error", "missing"}:
                    continue
                source_path = Path(result.source_path)
                synced_paths[url] = source_path
                if collection.request_delay_seconds:
                    self._cooperative_delay(collection.request_delay_seconds, cancel_check)
            else:
                reporter.advance(product=collection.product, version=collection.version)
            for link in crawler.links(url, source_path):
                if link not in seen_urls:
                    queue.append(link)
            reporter.revise_collection_estimate(
                collection, max(len(seen_urls) + len(queue), new_pages)
            )
        complete = not queue
        pending_vectors = self._string_values(
            previous_collection_state.get("vector_cleanup_pending")
        )
        pending_files = self._string_values(previous_collection_state.get("file_cleanup_pending"))
        if complete:
            cleanup = self.source_store.mark_collection_missing(
                collection.collection_id, seen_source_ids
            )
            pending_vectors.update(cleanup.chunk_ids)
            pending_files.update(cleanup.source_paths)
        if pending_vectors and self.ingestion_manager.vector_index.enabled:
            try:
                self.ingestion_manager.vector_index.delete(sorted(pending_vectors))
            except Exception as exc:
                LOGGER.warning(
                    "vector cleanup remains pending for collection %s: %s",
                    collection.collection_id,
                    exc,
                )
            else:
                pending_vectors.clear()
        pending_files = self._remove_missing_source_files(
            pending_files,
            collection_id=collection.collection_id,
        )
        self.source_store.update_collection_state(
            collection.collection_id,
            {
                "status": "complete" if complete else "truncated",
                "pages_seen": len(seen_urls),
                "new_pages_processed": new_pages,
                "pending_urls": len(queue),
                "completed_at": datetime.now(UTC).isoformat() if complete else None,
                "vector_cleanup_pending": sorted(pending_vectors),
                "file_cleanup_pending": sorted(pending_files),
            },
        )
        return results

    @staticmethod
    def _string_values(value: object) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {item for item in value if isinstance(item, str) and item}

    def _remove_missing_source_files(
        self,
        paths: set[str],
        *,
        collection_id: str,
    ) -> set[str]:
        root = self.ingestion_manager.official_sources_root.resolve()
        pending: set[str] = set()
        for value in paths:
            path = Path(value)
            try:
                resolved = path.resolve(strict=False)
                resolved.relative_to(root)
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise OSError("cached source is not a regular file")
                path.unlink(missing_ok=True)
            except (OSError, ValueError) as exc:
                pending.add(value)
                LOGGER.warning(
                    "file cleanup remains pending for collection %s (%s): %s",
                    collection_id,
                    value,
                    exc,
                )
        return pending

    @staticmethod
    def _require_not_cancelled(cancel_check: CancelCheck | None) -> None:
        if cancel_check is not None and cancel_check():
            raise OfficialSyncCancelled("official documentation synchronization was cancelled")

    @classmethod
    def _cooperative_delay(cls, seconds: float, cancel_check: CancelCheck | None) -> None:
        deadline = time.monotonic() + seconds
        while True:
            cls._require_not_cancelled(cancel_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    @staticmethod
    def _discovered_source(
        collection: OfficialCollectionDefinition, url: str
    ) -> OfficialSourceDefinition:
        digest = sha256(url.encode("utf-8")).hexdigest()[:20]
        slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "home")
        slug = re.sub(r"[^a-z0-9]+", "-", slug.casefold()).strip("-")[:60] or "page"
        version_slug = collection.version.replace(".", "-")
        return OfficialSourceDefinition(
            source_id=f"bmc-crawl-{collection.product}-{version_slug}-{digest}",
            url=url,
            local_path=f"{collection.local_path_prefix}/{slug}-{digest}.html",
            product=collection.product,
            version=collection.version,
            document_type=OfficialSourceSynchronizer._infer_document_type(url),
            language=collection.language,
            metadata={
                "collection_id": collection.collection_id,
                "discovered": True,
            },
        )

    @staticmethod
    def _infer_document_type(url: str) -> DocumentType:
        path = urlsplit(url).path.casefold()
        rules = (
            (("release-notes", "known-and-corrected"), DocumentType.RELEASE_NOTES),
            (("troubleshoot", "support-information"), DocumentType.TROUBLESHOOTING),
            (("common-data-model",), DocumentType.CDM),
            (("rest-api", "web-services-api", "/api/"), DocumentType.API),
            (("architecture",), DocumentType.ARCHITECTURE),
            (("administer",), DocumentType.ADMINISTRATION),
            (("integrat",), DocumentType.INTEGRATION),
            (("develop",), DocumentType.DEVELOPMENT),
            (("setting-up", "getting-started", "using/"), DocumentType.HOWTO),
        )
        for markers, document_type in rules:
            if any(marker in path for marker in markers):
                return document_type
        return DocumentType.REFERENCE

    def _http_client(self, authentication: httpx.Auth | None) -> httpx.Client:
        settings = self.config.ingestion.http
        return httpx.Client(
            timeout=settings.timeout_seconds,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml",
            },
            auth=authentication,
            follow_redirects=False,
        )

    def _destination(self, source: OfficialSourceDefinition) -> Path:
        root = (self.config.sources_path / "bmc" / "official").resolve()
        destination = (root / source.local_path).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise SourceSyncError(
                f"official source path escapes configured root: {source.local_path}"
            ) from exc
        return destination

    def _authentication(self) -> httpx.BasicAuth | None:
        settings = self.config.ingestion.http.authentication
        if settings.mode in {"none", "browser"}:
            return None
        username = os.environ.get(settings.username_env)
        password = os.environ.get(settings.password_env)
        missing = [
            name
            for name, value in (
                (settings.username_env, username),
                (settings.password_env, password),
            )
            if not value
        ]
        if missing:
            raise SourceSyncError(
                "missing BMC credential environment variables: " + ", ".join(missing)
            )
        return httpx.BasicAuth(username, password)
