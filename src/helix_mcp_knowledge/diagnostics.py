"""Read-only readiness checks for an installed knowledge server."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

import anyio

from .application import KnowledgeApplication
from .catalog.distribution import load_effective_official_catalog
from .catalog.versions import normalize_version
from .errors import ChunkNotFoundError
from .models.search import SearchRequest
from .models.source import SourceScope
from .server import create_server

EXPECTED_TOOL_NAMES = {
    "search_docs",
    "get_section",
    "list_products",
    "list_versions",
    "list_projects",
    "get_active_project",
    "set_active_project",
    "get_update_status",
    "get_sync_status",
}


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class SmokeCheck:
    name: str
    status: CheckStatus
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class SmokeTestReport:
    config: str
    checks: tuple[SmokeCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        counts = Counter(check.status.value for check in self.checks)
        return {
            "status": "pass" if self.passed else "fail",
            "config": self.config,
            "summary": {
                "passed": counts[CheckStatus.PASS.value],
                "failed": counts[CheckStatus.FAIL.value],
                "skipped": counts[CheckStatus.SKIP.value],
            },
            "checks": [check.to_dict() for check in self.checks],
        }


class SmokeTester:
    """Run non-destructive checks without starting synchronization coordinators."""

    def __init__(
        self,
        application: KnowledgeApplication,
        *,
        require_project_isolation: bool = False,
    ) -> None:
        self.application = application
        self.require_project_isolation = require_project_isolation

    def run(self) -> SmokeTestReport:
        checks = tuple(
            self._safely(name, check)
            for name, check in (
                ("database_integrity", self._database_integrity),
                ("index_readiness", self._index_readiness),
                ("official_catalog", self._official_catalog),
                ("projects", self._projects),
                ("mcp_tools", self._mcp_tools),
                ("official_search", self._official_search),
                ("project_isolation", self._project_isolation),
            )
        )
        return SmokeTestReport(
            config=str(self.application.config.config_path),
            checks=checks,
        )

    @staticmethod
    def _safely(name: str, check) -> SmokeCheck:
        try:
            return check()
        except Exception as exc:  # The report must retain every independent failure.
            return SmokeCheck(
                name=name,
                status=CheckStatus.FAIL,
                message=f"{type(exc).__name__}: {exc}",
            )

    def _database_integrity(self) -> SmokeCheck:
        with self.application.database.connect() as connection:
            quick_check = [row[0] for row in connection.execute("PRAGMA quick_check").fetchall()]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        passed = quick_check == ["ok"] and not foreign_keys
        return SmokeCheck(
            name="database_integrity",
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            message="SQLite integrity and foreign keys are valid" if passed else "SQLite failed",
            details={
                "database": str(self.application.database.path),
                "quick_check": quick_check,
                "foreign_key_violations": len(foreign_keys),
            },
        )

    def _index_readiness(self) -> SmokeCheck:
        with self.application.database.connect() as connection:
            details = {
                "documents": connection.execute(
                    "SELECT count(*) FROM documents WHERE status = 'indexed'"
                ).fetchone()[0],
                "chunks": connection.execute(
                    "SELECT count(*) FROM chunks WHERE active = 1"
                ).fetchone()[0],
                "official_documents": connection.execute(
                    """SELECT count(*) FROM documents
                       WHERE status = 'indexed' AND source_scope = 'bmc_official'"""
                ).fetchone()[0],
                "project_documents": connection.execute(
                    """SELECT count(*) FROM documents
                       WHERE status = 'indexed' AND source_scope = 'project'"""
                ).fetchone()[0],
            }
        details = {key: int(value) for key, value in details.items()}
        passed = details["documents"] > 0 and details["chunks"] > 0
        intentionally_empty = (
            details["documents"] == 0
            and details["chunks"] == 0
            and not self.application.config.official_docs.products
            and not self.application.registry.list()
        )
        if intentionally_empty:
            return SmokeCheck(
                name="index_readiness",
                status=CheckStatus.SKIP,
                message="The empty index is valid until products or projects are configured",
                details=details,
            )
        return SmokeCheck(
            name="index_readiness",
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            message="The index contains active evidence" if passed else "The index is empty",
            details=details,
        )

    def _official_catalog(self) -> SmokeCheck:
        manifest = load_effective_official_catalog(self.application.config)
        manifest_pairs = {
            (
                self.application.catalog.resolve(item.product).product_id,
                normalize_version(item.version),
            )
            for item in [*manifest.sources, *manifest.collections]
            if item.enabled
        }
        selected_pairs = {
            (product, normalize_version(version))
            for product, settings in self.application.config.official_docs.products.items()
            for version in settings.versions
        }
        missing = sorted(selected_pairs - manifest_pairs)
        if not selected_pairs:
            return SmokeCheck(
                name="official_catalog",
                status=CheckStatus.SKIP,
                message="No official products are selected",
                details={"selected": [], "missing": []},
            )
        passed = not missing
        return SmokeCheck(
            name="official_catalog",
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            message=(
                "Every selected official product/version exists in the manifest"
                if passed
                else "Official selections are empty or unavailable"
            ),
            details={
                "selected": [f"{product}={version}" for product, version in sorted(selected_pairs)],
                "missing": [f"{product}={version}" for product, version in missing],
            },
        )

    def _projects(self) -> SmokeCheck:
        projects = self.application.registry.list()
        missing_paths: list[str] = []
        summaries: list[dict[str, object]] = []
        for project in projects:
            if not project.documents_path.is_dir():
                missing_paths.append(str(project.documents_path))
            if (
                project.sources_manifest_path is not None
                and not project.sources_manifest_path.is_file()
            ):
                missing_paths.append(str(project.sources_manifest_path))
            summaries.append(
                {
                    "id": project.id,
                    "classification": project.classification.value,
                    "products": {
                        product: settings.version
                        for product, settings in project.bmc_products.items()
                    },
                }
            )
        return SmokeCheck(
            name="projects",
            status=CheckStatus.PASS if not missing_paths else CheckStatus.FAIL,
            message=(
                "Configured project paths are available"
                if not missing_paths
                else "One or more configured project paths are unavailable"
            ),
            details={"projects": summaries, "missing_paths": missing_paths},
        )

    def _mcp_tools(self) -> SmokeCheck:
        server = create_server(application=self.application)
        tools = anyio.run(server.list_tools)
        names = {tool.name for tool in tools}
        missing = sorted(EXPECTED_TOOL_NAMES - names)
        unexpected = sorted(names - EXPECTED_TOOL_NAMES)
        passed = not missing and not unexpected
        return SmokeCheck(
            name="mcp_tools",
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            message="The exact MCP tool contract is registered" if passed else "Tool mismatch",
            details={
                "tools": sorted(names),
                "missing": missing,
                "unexpected": unexpected,
            },
        )

    def _official_search(self) -> SmokeCheck:
        if not self.application.config.official_docs.products:
            return SmokeCheck(
                name="official_search",
                status=CheckStatus.SKIP,
                message="Official search is deferred until a product is selected and indexed",
            )
        probe = self._official_probe()
        if probe is None:
            return SmokeCheck(
                name="official_search",
                status=CheckStatus.FAIL,
                message="No indexed official document matches the configured selections",
            )
        response = self.application.search_engine.search(
            SearchRequest(
                query=probe["title"],
                source_scope=SourceScope.BMC_OFFICIAL,
                product=probe["product"],
                version=probe["version"],
                top_k=3,
            )
        )
        invalid = [
            result.chunk_id
            for result in response.results
            if result.source_scope is not SourceScope.BMC_OFFICIAL
            or probe["product"] not in result.product_ids
            or probe["version"] not in result.versions
            or not self._trusted_official_url(result.source_url)
        ]
        passed = bool(response.results) and not invalid
        return SmokeCheck(
            name="official_search",
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            message=(
                "Official search preserves scope, version and canonical provenance"
                if passed
                else "Official search returned no evidence or invalid provenance"
            ),
            details={
                "query": probe["title"],
                "product": probe["product"],
                "version": probe["version"],
                "results": [
                    {
                        "chunk_id": result.chunk_id,
                        "title": result.title,
                        "source_url": result.source_url,
                    }
                    for result in response.results
                ],
                "invalid_chunks": invalid,
            },
        )

    def _project_isolation(self) -> SmokeCheck:
        if self.application.config.projects.default_project is not None:
            return self._project_skip(
                "A configured default project intentionally remains effective"
            )
        probe = self._project_probe()
        if probe is None:
            return self._project_skip("No indexed active project document is available")

        original_project = self.application.get_active_project().project_id
        try:
            self.application.set_active_project(probe["project_id"])
            selected = self.application.search_engine.search(
                SearchRequest(
                    query=probe["title"],
                    source_scope=SourceScope.PROJECT,
                    product=probe["product"],
                    top_k=3,
                )
            )
            selected_chunk_id = selected.results[0].chunk_id if selected.results else None
            self.application.set_active_project(None)
            cleared = self.application.search_engine.search(
                SearchRequest(
                    query=probe["title"],
                    source_scope=SourceScope.ALL_RELEVANT,
                    product=probe["product"],
                    top_k=3,
                )
            )
            section_leaked = False
            if selected_chunk_id is not None:
                try:
                    self.application.search_engine.get_section(selected_chunk_id)
                except ChunkNotFoundError:
                    pass
                else:
                    section_leaked = True
        finally:
            self.application.set_active_project(original_project)

        selected_valid = bool(selected.results) and all(
            result.source_scope is SourceScope.PROJECT and result.project_id == probe["project_id"]
            for result in selected.results
        )
        leaked = [
            result.chunk_id
            for result in cleared.results
            if result.source_scope is SourceScope.PROJECT or result.project_id is not None
        ]
        passed = (
            selected_valid
            and cleared.context.project_id is None
            and not leaked
            and not section_leaked
        )
        return SmokeCheck(
            name="project_isolation",
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            message=(
                "Project evidence disappears after clearing the session context"
                if passed
                else "Project isolation failed"
            ),
            details={
                "project_id": probe["project_id"],
                "query": probe["title"],
                "selected_results": len(selected.results),
                "cleared_results": len(cleared.results),
                "leaked_chunks": leaked,
                "section_leaked_after_clear": section_leaked,
                "context_restored_to": original_project,
            },
        )

    def _project_skip(self, reason: str) -> SmokeCheck:
        required = self.require_project_isolation
        return SmokeCheck(
            name="project_isolation",
            status=CheckStatus.FAIL if required else CheckStatus.SKIP,
            message=reason,
            details={"required": required},
        )

    def _official_probe(self) -> dict[str, str] | None:
        selected = sorted(
            (product, version)
            for product, settings in self.application.config.official_docs.products.items()
            for version in settings.versions
        )
        with self.application.database.connect() as connection:
            for product, version in selected:
                row = connection.execute(
                    """
                    SELECT d.title
                    FROM documents d
                    JOIN document_products dp ON dp.document_id = d.document_id
                    JOIN document_product_versions dpv ON dpv.document_id = d.document_id
                    JOIN product_versions pv
                      ON pv.product_version_id = dpv.product_version_id
                    WHERE d.source_scope = 'bmc_official'
                      AND d.status = 'indexed'
                      AND dp.product_id = ?
                      AND pv.version = ?
                    ORDER BY d.document_id
                    LIMIT 1
                    """,
                    (product, version),
                ).fetchone()
                if row is not None:
                    return {"title": row["title"], "product": product, "version": version}
        return None

    def _project_probe(self) -> dict[str, str | None] | None:
        active_ids = [project.id for project in self.application.registry.list()]
        if not active_ids:
            return None
        placeholders = ", ".join("?" for _ in active_ids)
        with self.application.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT d.project_id, d.title, min(dp.product_id) AS product
                FROM documents d
                JOIN chunks c ON c.document_id = d.document_id AND c.active = 1
                LEFT JOIN document_products dp ON dp.document_id = d.document_id
                WHERE d.source_scope = 'project'
                  AND d.status = 'indexed'
                  AND d.project_id IN ({placeholders})
                GROUP BY d.document_id, d.project_id, d.title
                ORDER BY d.project_id, d.document_id
                LIMIT 1
                """,
                active_ids,
            ).fetchone()
        return dict(row) if row is not None else None

    def _trusted_official_url(self, value: str | None) -> bool:
        if value is None:
            return False
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        allowed = [
            domain.casefold() for domain in self.application.config.ingestion.http.allowed_domains
        ]
        return parsed.scheme == "https" and any(
            hostname == domain or hostname.endswith(f".{domain}") for domain in allowed
        )


def run_smoke_test(
    application: KnowledgeApplication,
    *,
    require_project_isolation: bool = False,
) -> SmokeTestReport:
    return SmokeTester(
        application,
        require_project_isolation=require_project_isolation,
    ).run()
