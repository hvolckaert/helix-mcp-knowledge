"""Administrative and server CLI."""

import argparse
import json
import os
import shutil
import sys
import webbrowser
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from .. import __version__
from ..application import KnowledgeApplication
from ..automation import ProjectSyncLease
from ..config import load_config
from ..dashboard import DEFAULT_DASHBOARD_PORT, run_dashboard
from ..dashboard_runtime import DashboardRuntimeManager
from ..diagnostics import run_smoke_test
from ..errors import KnowledgeError
from ..evaluation import (
    evaluate_retrieval,
    load_evaluation_dataset,
    render_evaluation_markdown,
)
from ..logging import configure_logging
from ..managed_installation import (
    ManagedInstallation,
    activate_managed_installation,
    load_managed_installation,
)
from ..models.document import DocumentType
from ..models.ingestion import IngestRequest
from ..models.project import Classification
from ..models.source import SourceScope
from ..official_automation import OfficialSyncLease
from ..openclaw import DEFAULT_SERVER_NAME, install_openclaw_server
from ..server import create_server
from ..storage.automation import AutomationStore
from ..update_lock import UpdateLock
from ..updater import DEFAULT_REPOSITORY, update_installation
from ..workspace import default_workspace_path, discover_config_path, initialize_workspace
from .configure import configure_github_cli, configure_official_docs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="helix-mcp-knowledge")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (otherwise environment, checkout, then user workspace)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init", help="Create a self-contained user workspace")
    initialize.add_argument(
        "--workspace",
        help="Workspace directory (defaults to the platform-native user data directory)",
    )
    initialize.add_argument(
        "--force",
        action="store_true",
        help="Overwrite packaged configuration files without deleting indexed data",
    )
    openclaw = subparsers.add_parser(
        "install-openclaw",
        help="Create or reuse a workspace and register this MCP server in OpenClaw",
    )
    openclaw.add_argument(
        "--workspace",
        help="Workspace directory (defaults to the platform-native user data directory)",
    )
    openclaw.add_argument(
        "--product",
        action="append",
        default=None,
        metavar="PRODUCT=VERSION",
        help="Preselect an official product/version; may be repeated",
    )
    openclaw.add_argument(
        "--automatic-sync",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable automatic official synchronization",
    )
    openclaw.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    openclaw.add_argument("--openclaw-command", default="openclaw")
    openclaw.add_argument(
        "--gh-command",
        default=None,
        help="GitHub CLI command used by background runtime and catalog checks",
    )
    openclaw.add_argument("--server-command", default=None)
    openclaw.add_argument("--dashboard-port", type=int, default=DEFAULT_DASHBOARD_PORT)
    openclaw.add_argument("--connect-timeout", type=int, default=30)
    openclaw.add_argument("--request-timeout", type=int, default=60)
    openclaw.add_argument("--no-probe", action="store_true")
    openclaw.add_argument("--no-reload", action="store_true")
    openclaw.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Do not open the dashboard browser after a new empty installation",
    )
    install = subparsers.add_parser(
        "install",
        help="Create or reuse a client-neutral managed installation",
    )
    install.add_argument(
        "--workspace",
        help="Workspace directory (defaults to the platform-native user data directory)",
    )
    install.add_argument(
        "--product",
        action="append",
        default=None,
        metavar="PRODUCT=VERSION",
        help="Preselect an official product/version; may be repeated",
    )
    install.add_argument(
        "--automatic-sync",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable automatic official synchronization",
    )
    install.add_argument("--server-command", default=None)
    install.add_argument(
        "--gh-command",
        default=None,
        help="GitHub CLI command used by background runtime and catalog checks",
    )
    install.add_argument("--dashboard-port", type=int, default=DEFAULT_DASHBOARD_PORT)
    install.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Do not open the dashboard browser after a new empty installation",
    )
    update = subparsers.add_parser(
        "update", help="Install and atomically activate a verified GitHub release"
    )
    update.add_argument("--repository", default=DEFAULT_REPOSITORY)
    update.add_argument(
        "--version",
        dest="target_version",
        help="Target stable version (defaults to the latest published release)",
    )
    update.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    update.add_argument("--openclaw-command", default="openclaw")
    update.add_argument("--gh-command", default="gh")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument(
        "--resume",
        action="store_true",
        help="Repair and reuse an existing inactive target runtime",
    )
    update.add_argument("--allow-downgrade", action="store_true")
    update.add_argument("--no-probe", action="store_true")
    update.add_argument("--no-reload", action="store_true")
    subparsers.add_parser("serve", help="Run the MCP server over stdio")
    dashboard = subparsers.add_parser(
        "dashboard", help="Run the local product and synchronization dashboard"
    )
    dashboard.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    dashboard.add_argument("--no-browser", action="store_true")
    dashboard.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    dashboard.add_argument("--openclaw-command", default="openclaw")
    subparsers.add_parser("init-db", help="Initialize and synchronize SQLite metadata")
    subparsers.add_parser("status", help="Show configuration and index counts")
    smoke_test = subparsers.add_parser(
        "smoke-test", help="Run non-destructive installation and isolation checks"
    )
    smoke_test.add_argument(
        "--require-project-isolation",
        action="store_true",
        help="Fail instead of skipping when no project evidence can be tested",
    )
    configure = subparsers.add_parser(
        "configure", help="Select official BMC products, versions and automatic refresh"
    )
    product_group = configure.add_mutually_exclusive_group()
    product_group.add_argument(
        "--product",
        action="append",
        default=None,
        metavar="PRODUCT=VERSION",
        help="Select an available official product/version; may be repeated",
    )
    product_group.add_argument(
        "--no-products",
        action="store_true",
        help="Clear every official product/version selection",
    )
    configure.add_argument(
        "--automatic-sync",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable automatic official synchronization by packaged worker",
    )
    configure.add_argument(
        "--bootstrap-on-empty",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Download selected documentation when the database is empty",
    )
    configure.add_argument(
        "--interval-hours",
        type=float,
        help="Hours between successful official documentation refreshes",
    )
    configure.add_argument(
        "--retain-unselected-versions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Keep or physically remove official versions that are no longer selected",
    )
    configure.add_argument(
        "--sync-now",
        action="store_true",
        help="Download and index the saved official selection immediately",
    )
    sync = subparsers.add_parser(
        "sync", help="Download and index curated official BMC documentation"
    )
    sync.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Synchronize only this manifest source ID; may be repeated",
    )
    sync.add_argument(
        "--collection-id",
        action="append",
        default=[],
        help="Crawl only this collection ID; may be repeated",
    )
    sync.add_argument(
        "--no-discovery",
        action="store_true",
        help="Synchronize only explicitly curated pages, without crawling collections",
    )
    sync.add_argument(
        "--verbose-results",
        action="store_true",
        help="Include every per-page result instead of only errors and summary counts",
    )
    sync_project = subparsers.add_parser(
        "sync-project", help="Synchronize local project documents from its manifest"
    )
    sync_project.add_argument("project_id", help="Configured project ID")
    sync_project.add_argument(
        "--no-prune",
        action="store_true",
        help="Keep indexed manifest documents that are no longer selected or present",
    )
    sync_project.add_argument(
        "--verbose-results",
        action="store_true",
        help="Include every per-document result instead of only errors and summary counts",
    )
    ingest = subparsers.add_parser("ingest", help="Parse and atomically index local documents")
    ingest.add_argument("path", help="File or directory inside the configured source root")
    ingest.add_argument(
        "--scope",
        choices=[SourceScope.BMC_OFFICIAL.value, SourceScope.PROJECT.value],
        required=True,
    )
    ingest.add_argument("--project-id")
    ingest.add_argument(
        "--document-type",
        choices=[item.value for item in DocumentType],
        default=DocumentType.OTHER.value,
    )
    ingest.add_argument("--language", default="en")
    ingest.add_argument("--classification", choices=[item.value for item in Classification])
    ingest.add_argument(
        "--product",
        action="append",
        default=[],
        metavar="PRODUCT[=VERSION]",
        help="Repeat for documents that apply to multiple BMC products",
    )
    ingest.add_argument("--recursive", action="store_true")
    projects = subparsers.add_parser("projects", help="List configured projects")
    projects.add_argument("--include-archived", action="store_true")
    evaluate = subparsers.add_parser(
        "evaluate-retrieval",
        help="Compare lexical, configured baseline, and optional reranked retrieval",
    )
    evaluate.add_argument("dataset", help="JSON evaluation dataset")
    evaluate.add_argument("--top-k", type=int, default=10)
    evaluate.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Report format (default: json)",
    )
    evaluate.add_argument("--output", help="Write the report to this file instead of stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_workspace(args.workspace, force=args.force)
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "install-openclaw":
            return _install_openclaw(args)
        if args.command == "install":
            return _install_standalone(args)

        config_path = discover_config_path(args.config)
        if args.command == "update":

            def verify_dashboard(installation: ManagedInstallation) -> None:
                DashboardRuntimeManager(
                    installation,
                    server_name=(installation.server_name or args.server_name),
                    openclaw_command=(installation.openclaw_command or args.openclaw_command),
                ).restart_and_verify(expected_version=installation.active_version)

            result = update_installation(
                config_path=config_path,
                repository=args.repository,
                target_version=args.target_version,
                openclaw_command=args.openclaw_command,
                gh_command=args.gh_command,
                server_name=args.server_name,
                probe=not args.no_probe,
                reload=not args.no_reload,
                dry_run=args.dry_run,
                resume=args.resume,
                allow_downgrade=args.allow_downgrade,
                post_activation_check=(None if args.dry_run else verify_dashboard),
            )
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "dashboard":
            run_dashboard(
                config_path,
                port=args.port,
                open_browser=not args.no_browser,
                server_name=args.server_name,
                openclaw_command=args.openclaw_command,
            )
            return 0
        application = KnowledgeApplication.from_config(
            config_path,
            manage_optional_services=args.command != "smoke-test",
        )
        configure_logging(application.config.logging.level)
        if args.command == "serve":
            create_server(application=application).run(transport="stdio")
        elif args.command == "init-db":
            print(json.dumps(application.database.status(), indent=2, ensure_ascii=False))
        elif args.command == "status":
            status = application.database.status()
            status["configured_projects"] = len(application.registry)
            status["lexical_enabled"] = application.config.retrieval.lexical.enabled
            status["semantic_enabled"] = application.config.retrieval.semantic.enabled
            status["embedded_sync_enabled"] = application.config.ingestion.watch.enabled
            status["automation"] = application.automation_status()
            status["official_docs"] = application.config.official_docs.model_dump(mode="json")
            status["official_sync"] = AutomationStore(application.database).state("official-docs")
            status["release_update"] = application.release_update_checker.status().model_dump(
                mode="json"
            )
            print(json.dumps(status, indent=2, ensure_ascii=False))
        elif args.command == "smoke-test":
            report = run_smoke_test(
                application,
                require_project_isolation=args.require_project_isolation,
            )
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
            return 0 if report.passed else 1
        elif args.command == "configure":
            with UpdateLock(application.config.base_dir / ".update.lock"):
                settings = configure_official_docs(
                    config_path,
                    product_specs=args.product,
                    no_products=args.no_products,
                    automatic_sync=args.automatic_sync,
                    bootstrap_on_empty=args.bootstrap_on_empty,
                    interval_hours=args.interval_hours,
                    retain_unselected_versions=args.retain_unselected_versions,
                )
            payload: dict[str, object] = {
                "config": str(config_path),
                "official_docs": settings.model_dump(mode="json"),
            }
            if args.sync_now:
                if not settings.products and settings.retain_unselected_versions:
                    raise KnowledgeError("cannot synchronize without selected official products")
                configured_application = KnowledgeApplication.from_config(config_path)
                with OfficialSyncLease(
                    AutomationStore(configured_application.database),
                    configured_application.config.ingestion.watch,
                ) as lease:
                    results = configured_application.sync_official_sources(
                        cancel_check=lease.cancel_requested
                    )
                statuses = Counter(result.status for result in results)
                payload["sync"] = {
                    "total": len(results),
                    "statuses": dict(sorted(statuses.items())),
                    "chunks_indexed": sum(result.chunks_indexed for result in results),
                    "errors": [result.to_dict() for result in results if result.status == "error"],
                }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif args.command == "sync":
            with OfficialSyncLease(
                AutomationStore(application.database),
                application.config.ingestion.watch,
            ) as lease:
                results = application.sync_official_sources(
                    set(args.source_id) if args.source_id else None,
                    collection_ids=set(args.collection_id) if args.collection_id else None,
                    discover=not args.no_discovery,
                    cancel_check=lease.cancel_requested,
                )
            statuses = Counter(result.status for result in results)
            payload: dict[str, object] = {
                "summary": {
                    "total": len(results),
                    "statuses": dict(sorted(statuses.items())),
                    "chunks_indexed": sum(result.chunks_indexed for result in results),
                },
                "errors": [result.to_dict() for result in results if result.status == "error"],
            }
            if args.verbose_results:
                payload["results"] = [result.to_dict() for result in results]
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 1 if any(result.status == "error" for result in results) else 0
        elif args.command == "sync-project":
            with ProjectSyncLease(
                AutomationStore(application.database),
                application.config.ingestion.watch,
            ):
                results = application.sync_project_sources(args.project_id, prune=not args.no_prune)
            statuses = Counter(result.status for result in results)
            payload = {
                "project_id": args.project_id,
                "summary": {
                    "total": len(results),
                    "statuses": dict(sorted(statuses.items())),
                    "chunks_indexed": sum(result.chunks_indexed for result in results),
                },
                "errors": [result.to_dict() for result in results if result.status == "error"],
            }
            if args.verbose_results:
                payload["results"] = [result.to_dict() for result in results]
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 1 if any(result.status == "error" for result in results) else 0
        elif args.command == "ingest":
            paths = _ingestion_paths(
                Path(args.path),
                recursive=args.recursive,
                allowed_extensions=application.config.ingestion.allowed_extensions,
            )
            product_versions = _parse_products(args.product)
            with UpdateLock(application.config.base_dir / ".update.lock"):
                results = [
                    application.ingestion_manager.ingest(
                        IngestRequest(
                            source_path=path,
                            source_scope=SourceScope(args.scope),
                            project_id=args.project_id,
                            document_type=DocumentType(args.document_type),
                            language=args.language,
                            classification=(
                                Classification(args.classification) if args.classification else None
                            ),
                            product_versions=product_versions,
                        )
                    ).model_dump(mode="json")
                    for path in paths
                ]
            print(json.dumps(results, indent=2, ensure_ascii=False))
        elif args.command == "projects":
            print(application.list_projects(args.include_archived).model_dump_json(indent=2))
        elif args.command == "evaluate-retrieval":
            dataset = load_evaluation_dataset(args.dataset, top_k=args.top_k)
            report = evaluate_retrieval(application.search_engine, dataset)
            database_status = application.database.status()
            report["runtime"] = {
                "helix_mcp_knowledge_version": __version__,
                "index": {
                    "schema_version": database_status["schema_version"],
                    "counts": database_status["counts"],
                    "sqlite_bytes": application.database.path.stat().st_size,
                },
                "retrieval": {
                    "lexical_enabled": application.config.retrieval.lexical.enabled,
                    "semantic_enabled": application.config.retrieval.semantic.enabled,
                    "reranker_enabled": application.config.retrieval.reranker.enabled,
                },
            }
            rendered = (
                render_evaluation_markdown(report)
                if args.format == "markdown"
                else json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
            if args.output:
                output = Path(args.output).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(rendered, encoding="utf-8")
                print(
                    json.dumps(
                        {
                            "status": "written",
                            "format": args.format,
                            "output": str(output),
                            "dataset_sha256": dataset.sha256,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(rendered, end="")
        return 0
    except (KnowledgeError, ValueError) as exc:
        parser = build_parser()
        parser.error(str(exc))
        return 2


def _install_openclaw(args: argparse.Namespace) -> int:
    workspace, config_path, initialized = _prepare_installation(args)
    server_command = _installed_server_command(args.server_command)
    previous_managed = load_managed_installation(workspace)
    standalone = activate_managed_installation(
        workspace=workspace,
        version=__version__,
        server_command=server_command,
        config_path=config_path,
        client="standalone",
        dashboard_port=args.dashboard_port,
    )
    application = KnowledgeApplication.from_config(config_path)
    try:
        installation = install_openclaw_server(
            config_path=config_path,
            workspace=workspace,
            server_name=args.server_name,
            openclaw_command=args.openclaw_command,
            server_command=standalone.launcher,
            probe=not args.no_probe,
            reload=not args.no_reload,
            connect_timeout=args.connect_timeout,
            request_timeout=args.request_timeout,
        )
    except Exception:
        if previous_managed is not None:
            activate_managed_installation(
                workspace=workspace,
                version=previous_managed.active_version,
                server_command=_versioned_server_command(
                    workspace, previous_managed.active_version
                ),
                config_path=previous_managed.config_path,
                client=previous_managed.client,
                server_name=previous_managed.server_name,
                openclaw_command=previous_managed.openclaw_command,
                dashboard_port=previous_managed.dashboard_port,
            )
        raise
    managed = activate_managed_installation(
        workspace=workspace,
        version=__version__,
        server_command=server_command,
        config_path=config_path,
        client="openclaw",
        server_name=args.server_name,
        openclaw_command=installation.openclaw_command,
        dashboard_port=args.dashboard_port,
    )
    payload = installation.to_dict()
    payload["managed_installation"] = managed.to_dict()
    payload["workspace_initialized"] = initialized
    payload["official_docs"] = application.config.official_docs.model_dump(mode="json")
    dashboard_runtime = DashboardRuntimeManager(
        managed,
        server_name=args.server_name,
        openclaw_command=args.openclaw_command,
    ).install_and_start()
    payload["dashboard_service"] = dashboard_runtime.to_dict()
    payload["dashboard"] = _dashboard_browser_result(
        managed,
        dashboard_runtime.process_id,
        open_browser=(
            initialized and not application.config.official_docs.products and not args.no_dashboard
        ),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _install_standalone(args: argparse.Namespace) -> int:
    workspace, config_path, initialized = _prepare_installation(args)
    application = KnowledgeApplication.from_config(config_path)
    managed = activate_managed_installation(
        workspace=workspace,
        version=__version__,
        server_command=_installed_server_command(args.server_command),
        config_path=config_path,
        client="standalone",
        dashboard_port=args.dashboard_port,
    )
    payload: dict[str, object] = {
        "workspace_initialized": initialized,
        "workspace": str(workspace),
        "config": str(config_path),
        "managed_installation": managed.to_dict(),
        "official_docs": application.config.official_docs.model_dump(mode="json"),
        "dashboard": None,
        "mcp_registration": {
            "command": str(managed.launcher),
            "cwd": str(workspace),
        },
    }
    dashboard_runtime = DashboardRuntimeManager(
        managed,
        server_name=DEFAULT_SERVER_NAME,
        openclaw_command="openclaw",
    ).install_and_start()
    payload["dashboard_service"] = dashboard_runtime.to_dict()
    payload["dashboard"] = _dashboard_browser_result(
        managed,
        dashboard_runtime.process_id,
        open_browser=(
            initialized and not application.config.official_docs.products and not args.no_dashboard
        ),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _dashboard_browser_result(
    managed: ManagedInstallation,
    process_id: int | None,
    *,
    open_browser: bool,
) -> dict[str, object] | None:
    if not open_browser:
        return None
    url = f"http://127.0.0.1:{managed.dashboard_port}/"
    webbrowser.open(url)
    return {"pid": process_id, "url": url}


def _prepare_installation(args: argparse.Namespace) -> tuple[Path, Path, bool]:
    if args.workspace and args.config:
        raise KnowledgeError("use either --config or --workspace for installation")
    if args.workspace:
        workspace = Path(args.workspace).expanduser().resolve()
        config_path = workspace / "config/config.yaml"
    elif args.config:
        config_path = Path(args.config).expanduser().resolve()
        workspace = config_path.parent.parent
    else:
        workspace = default_workspace_path().resolve()
        config_path = workspace / "config/config.yaml"

    initialized = False
    if not config_path.is_file():
        initialization = initialize_workspace(workspace)
        config_path = initialization.config
        initialized = True
    if args.gh_command:
        configure_github_cli(config_path, args.gh_command)
    if args.product is not None or args.automatic_sync is not None:
        product_specs = args.product
        if product_specs is None:
            current = load_config(config_path).official_docs
            product_specs = [
                f"{product}={version}"
                for product, settings in current.products.items()
                for version in settings.versions
            ]
        configure_official_docs(
            config_path,
            product_specs=product_specs,
            no_products=False,
            automatic_sync=args.automatic_sync,
            bootstrap_on_empty=None,
            interval_hours=None,
            retain_unselected_versions=None,
        )
    return workspace, config_path, initialized


def _installed_server_command(value: str | None) -> Path:
    if value:
        command = Path(value).expanduser().resolve()
    else:
        suffix = ".exe" if os.name == "nt" else ""
        adjacent = Path(sys.executable).absolute().parent / f"helix-mcp-knowledge-server{suffix}"
        discovered = shutil.which("helix-mcp-knowledge-server")
        command = adjacent if adjacent.is_file() else Path(discovered or adjacent).resolve()
    if not command.is_file():
        raise KnowledgeError(f"MCP server entry point was not found: {command}")
    return command


def _versioned_server_command(workspace: Path, version: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    executable_dir = (
        workspace / "runtime" / version / "venv" / ("Scripts" if os.name == "nt" else "bin")
    )
    command = executable_dir / f"helix-mcp-knowledge-server{suffix}"
    if not command.is_file():
        raise KnowledgeError(f"managed MCP server entry point was not found: {command}")
    return command


def _parse_products(values: list[str]) -> dict[str, str | None]:
    products: dict[str, str | None] = {}
    for value in values:
        product, separator, version = value.partition("=")
        product = product.strip()
        if not product:
            raise ValueError("--product cannot be empty")
        products[product] = version.strip() if separator and version.strip() else None
    return products


def _ingestion_paths(source: Path, *, recursive: bool, allowed_extensions: list[str]) -> list[Path]:
    source = source.expanduser().resolve()
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise ValueError(f"ingestion path does not exist: {source}")
    iterator = source.rglob("*") if recursive else source.glob("*")
    allowed = {extension.casefold() for extension in allowed_extensions}
    paths = sorted(
        path for path in iterator if path.is_file() and path.suffix.casefold() in allowed
    )
    if not paths:
        raise ValueError(f"no supported documents found under {source}")
    return paths
