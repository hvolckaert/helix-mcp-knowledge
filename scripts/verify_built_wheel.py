"""Cross-platform acceptance of the wheel produced by CI."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
import tempfile
import tomllib
import venv
from pathlib import Path
from zipfile import ZipFile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_RESOURCES = {
    "helix_mcp_knowledge/resources/config/config.yaml",
    "helix_mcp_knowledge/resources/config/sources/bmc-official-26.1.yaml",
    "helix_mcp_knowledge/resources/config/projects/example.yaml.example",
    "helix_mcp_knowledge/resources/config/projects/example.sources.yaml.example",
    "helix_mcp_knowledge/resources/requirements/ocr-component.txt",
    "helix_mcp_knowledge/resources/requirements/pip-bootstrap.txt",
    "helix_mcp_knowledge/resources/requirements/reranker-component.txt",
    "helix_mcp_knowledge/resources/requirements/semantic-component.txt",
}
PUBLIC_SDIST_DOCS = {
    "docs/architecture/v1-specification.md",
    "docs/catalog-maintenance.md",
    "docs/cmdb-reconciliation-demo.md",
    "docs/dashboard-ui-contract.md",
    "docs/integrated-cmdb-data-quality-case.md",
    "docs/mcp-client-integration.md",
    "docs/openclaw-windows-guide.md",
    "docs/openclaw-wsl-guide.md",
}
PUBLIC_SDIST_EVALUATION = {
    "evaluation/README.md",
    "evaluation/synthetic-baseline.json",
    "evaluation/synthetic-corpus/orion-operations.md",
    "evaluation/synthetic-corpus/orion-upgrade.md",
}
FORBIDDEN_DEVELOPMENT_MARKERS = ("example_project",)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        rendered = subprocess.list2cmdline(command)
        raise RuntimeError(
            f"command failed ({completed.returncode}): {rendered}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


def main() -> int:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        expected_version = tomllib.load(stream)["project"]["version"]
    _verify_release_metadata(expected_version)

    wheels = list((REPOSITORY_ROOT / "dist").glob(f"helix_mcp_knowledge-{expected_version}-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one wheel for version {expected_version}, found: {wheels}")
    wheel = wheels[0]

    source_distributions = list(
        (REPOSITORY_ROOT / "dist").glob(f"helix_mcp_knowledge-{expected_version}.tar.gz")
    )
    if len(source_distributions) != 1:
        raise RuntimeError(
            f"expected one source distribution for version {expected_version}, "
            f"found: {source_distributions}"
        )
    source_distribution = source_distributions[0]

    with tarfile.open(source_distribution, "r:gz") as archive:
        members = archive.getmembers()
        member_names = [member.name.casefold() for member in members]
        private_paths = [
            name
            for name in member_names
            if any(marker in name for marker in FORBIDDEN_DEVELOPMENT_MARKERS)
        ]
        if private_paths:
            raise RuntimeError(
                f"source distribution contains private project paths: {private_paths}"
            )
        archive_prefix = f"helix_mcp_knowledge-{expected_version}/"
        packaged_docs = {
            member.name.removeprefix(archive_prefix)
            for member in members
            if member.isfile() and member.name.startswith(f"{archive_prefix}docs/")
        }
        if packaged_docs != PUBLIC_SDIST_DOCS:
            raise RuntimeError(
                "source distribution documentation differs from the public allowlist: "
                f"missing={sorted(PUBLIC_SDIST_DOCS - packaged_docs)}, "
                f"unexpected={sorted(packaged_docs - PUBLIC_SDIST_DOCS)}"
            )
        packaged_evaluation = {
            member.name.removeprefix(archive_prefix)
            for member in members
            if member.isfile() and member.name.startswith(f"{archive_prefix}evaluation/")
        }
        if packaged_evaluation != PUBLIC_SDIST_EVALUATION:
            raise RuntimeError(
                "source distribution evaluation material differs from the public allowlist: "
                f"missing={sorted(PUBLIC_SDIST_EVALUATION - packaged_evaluation)}, "
                f"unexpected={sorted(packaged_evaluation - PUBLIC_SDIST_EVALUATION)}"
            )
        readme_member = next(
            (member for member in archive.getmembers() if member.name.endswith("/README.md")),
            None,
        )
        if readme_member is None:
            raise RuntimeError("source distribution does not contain README.md")
        extracted = archive.extractfile(readme_member)
        if extracted is None:
            raise RuntimeError("could not inspect source distribution README.md")
        readme_text = extracted.read().decode("utf-8").casefold()
        private_content = [
            marker for marker in FORBIDDEN_DEVELOPMENT_MARKERS if marker in readme_text
        ]
        if private_content:
            raise RuntimeError(
                f"source distribution README contains private project metadata: {private_content}"
            )
        root_config_name = f"helix_mcp_knowledge-{expected_version}/config/config.yaml"
        try:
            root_config_member = archive.getmember(root_config_name)
        except KeyError as exc:
            raise RuntimeError(
                "source distribution does not contain generic config/config.yaml"
            ) from exc
        root_config = archive.extractfile(root_config_member)
        if root_config is None:
            raise RuntimeError("could not inspect source distribution config/config.yaml")
        packaged_config = (
            REPOSITORY_ROOT / "src/helix_mcp_knowledge/resources/config/config.yaml"
        ).read_bytes()
        distribution_config = (REPOSITORY_ROOT / "config/distribution-config.yaml").read_bytes()
        if distribution_config != packaged_config:
            raise RuntimeError(
                "distribution config template differs from the generic packaged default"
            )
        if root_config.read() != packaged_config:
            raise RuntimeError(
                "source distribution root configuration differs from the generic packaged default"
            )

        license_member = next(
            (member for member in archive.getmembers() if member.name.endswith("/LICENSE")),
            None,
        )
        if license_member is None:
            raise RuntimeError("source distribution does not contain LICENSE")
        packaged_license = archive.extractfile(license_member)
        if packaged_license is None:
            raise RuntimeError("could not inspect source distribution LICENSE")
        if packaged_license.read() != (REPOSITORY_ROOT / "LICENSE").read_bytes():
            raise RuntimeError("source distribution LICENSE differs from repository LICENSE")

    with ZipFile(wheel) as archive:
        member_names = archive.namelist()
        missing = REQUIRED_RESOURCES.difference(member_names)
        license_members = [
            name for name in member_names if name.endswith(".dist-info/licenses/LICENSE")
        ]
        metadata_members = [name for name in member_names if name.endswith(".dist-info/METADATA")]
    if missing:
        raise RuntimeError(f"missing wheel resources: {sorted(missing)}")
    if len(license_members) != 1:
        raise RuntimeError(f"expected one wheel LICENSE, found: {license_members}")
    if len(metadata_members) != 1:
        raise RuntimeError(f"expected one wheel METADATA, found: {metadata_members}")
    with ZipFile(wheel) as archive:
        if archive.read(license_members[0]) != (REPOSITORY_ROOT / "LICENSE").read_bytes():
            raise RuntimeError("wheel LICENSE differs from repository LICENSE")
        metadata = archive.read(metadata_members[0]).decode("utf-8")
    if "License-Expression: MIT\n" not in metadata or "License-File: LICENSE\n" not in metadata:
        raise RuntimeError("wheel metadata does not declare the repository MIT license")

    with tempfile.TemporaryDirectory(prefix="helix-wheel-acceptance-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        cli = scripts / ("helix-mcp-knowledge.exe" if os.name == "nt" else "helix-mcp-knowledge")
        server = scripts / (
            "helix-mcp-knowledge-server.exe" if os.name == "nt" else "helix-mcp-knowledge-server"
        )

        _run([str(python), "-m", "pip", "install", str(wheel)])
        imported_version = _run(
            [
                str(python),
                "-c",
                "import helix_mcp_knowledge; print(helix_mcp_knowledge.__version__)",
            ]
        ).stdout.strip()
        if imported_version != expected_version:
            raise RuntimeError(
                f"wheel version {imported_version!r} does not match {expected_version!r}"
            )
        if not cli.is_file() or not server.is_file():
            raise RuntimeError("wheel entry points were not installed")

        workspace = root / "workspace"
        initialized = json.loads(_run([str(cli), "init", "--workspace", str(workspace)]).stdout)
        config = Path(initialized["config"])
        optional_defaults = json.loads(
            _run(
                [
                    str(python),
                    "-c",
                    (
                        "import json, sys; "
                        "from helix_mcp_knowledge.config import load_config; "
                        "from helix_mcp_knowledge.ocr_component import OcrComponentManager; "
                        "from helix_mcp_knowledge.reranker_component import "
                        "RerankerComponentManager; "
                        "from helix_mcp_knowledge.semantic_component import "
                        "SemanticComponentManager; "
                        "config = load_config(sys.argv[1]); "
                        "ocr = OcrComponentManager(config).status(); "
                        "reranker = RerankerComponentManager(config).status(); "
                        "semantic = SemanticComponentManager(config).status(); "
                        "print(json.dumps({'enabled': config.ingestion.ocr.enabled, "
                        "'installed': ocr.installed, 'status': ocr.status, "
                        "'semantic_enabled': config.retrieval.semantic.enabled, "
                        "'semantic_installed': semantic.installed, "
                        "'semantic_status': semantic.status, "
                        "'reranker_enabled': config.retrieval.reranker.enabled, "
                        "'reranker_installed': reranker.installed, "
                        "'reranker_status': reranker.status}))"
                    ),
                    str(config),
                ]
            ).stdout
        )
        if optional_defaults != {
            "enabled": False,
            "installed": False,
            "status": "not_installed",
            "semantic_enabled": False,
            "semantic_installed": False,
            "semantic_status": "not_installed",
            "reranker_enabled": False,
            "reranker_installed": False,
            "reranker_status": "not_installed",
        }:
            raise RuntimeError(
                "clean wheel unexpectedly enables or installs an optional component: "
                f"{optional_defaults}"
            )
        _run(
            [
                str(cli),
                "--config",
                str(config),
                "configure",
                "--product",
                "cmdb=26.1",
                "--no-automatic-sync",
            ]
        )
        _run([str(cli), "--config", str(config), "init-db"])
        status = json.loads(_run([str(cli), "--config", str(config), "status"]).stdout)
        if status["schema_version"] != 6:
            raise RuntimeError(f"unexpected schema version: {status['schema_version']}")
        if status["configured_projects"] != 0:
            raise RuntimeError("clean wheel unexpectedly configured a private project")
        selected = status["official_docs"]["products"]
        if selected != {"cmdb": {"versions": ["26.1"]}}:
            raise RuntimeError(f"unexpected official selection: {selected}")
        versions = json.loads(
            _run(
                [
                    str(python),
                    "-c",
                    (
                        "import sys; "
                        "from helix_mcp_knowledge.application import KnowledgeApplication; "
                        "app = KnowledgeApplication.from_config(sys.argv[1]); "
                        "print(app.search_engine.list_versions('cmdb').model_dump_json())"
                    ),
                    str(config),
                ]
            ).stdout
        )
        if versions != {
            "product": "cmdb",
            "versions": [{"version": "26.1", "indexed": False}],
        }:
            raise RuntimeError(f"configured version is not visible before sync: {versions}")
        products = json.loads(
            _run(
                [
                    str(python),
                    "-c",
                    (
                        "import sys; "
                        "from helix_mcp_knowledge.application import KnowledgeApplication; "
                        "app = KnowledgeApplication.from_config(sys.argv[1]); "
                        "print(app.search_engine.list_products().model_dump_json())"
                    ),
                    str(config),
                ]
            ).stdout
        )
        if products != {
            "products": [
                {
                    "product_id": "cmdb",
                    "name": "BMC Helix CMDB",
                    "aliases": ["CMDB", "BMC CMDB", "Helix CMDB"],
                    "configured": True,
                    "indexed": False,
                }
            ]
        }:
            raise RuntimeError(f"configured product is not visible before sync: {products}")
        catalog = json.loads(
            _run(
                [
                    str(python),
                    "-c",
                    (
                        "import json, sys; "
                        "from helix_mcp_knowledge.catalog.products import ProductCatalog; "
                        "from helix_mcp_knowledge.cli.configure import "
                        "available_official_versions; "
                        "from helix_mcp_knowledge.config import load_config; "
                        "from helix_mcp_knowledge.sync.manifest import "
                        "OfficialSourceManifest; "
                        "config = load_config(sys.argv[1]); "
                        "manifest = OfficialSourceManifest.load("
                        "config.official_manifest_path); "
                        "catalog = ProductCatalog.from_manifest(manifest); "
                        "print(json.dumps({'revision': manifest.catalog_revision, "
                        "'products': available_official_versions(manifest, catalog)}, "
                        "sort_keys=True))"
                    ),
                    str(config),
                ]
            ).stdout
        )
        expected_catalog = {
            "revision": 1,
            "products": {
                "arsystem": ["26.3", "26.2", "26.1"],
                "business_workflows": ["26.3", "26.2", "26.1"],
                "cmdb": ["26.3", "26.2", "26.1"],
                "digital_workplace": ["26.3", "26.2", "26.1"],
                "discovery": ["current"],
                "itsm": ["26.3", "26.2", "26.1"],
            },
        }
        if catalog != expected_catalog:
            raise RuntimeError(f"wheel does not expose the supported catalog: {catalog}")
        sync_status = json.loads(
            _run(
                [
                    str(python),
                    "-c",
                    (
                        "import sys; "
                        "from helix_mcp_knowledge.application import KnowledgeApplication; "
                        "app = KnowledgeApplication.from_config(sys.argv[1]); "
                        "print(app.get_sync_status().model_dump_json())"
                    ),
                    str(config),
                ]
            ).stdout
        )
        official = sync_status["official"]
        if (
            official["status"] != "not_started"
            or official["ready"] is not False
            or official["configured_products"] != {"cmdb": ["26.1"]}
            or official["indexed_documents"] != 0
            or official["indexed_chunks"] != 0
            or sync_status["project"] is not None
        ):
            raise RuntimeError(f"unexpected clean synchronization status: {sync_status}")

    print(f"verified {wheel.name} on {os.name}")
    return 0


def _verify_release_metadata(expected_version: str) -> None:
    """Keep release-facing defaults synchronized with the package version."""

    expectations = {
        "src/helix_mcp_knowledge/__init__.py": (
            rf'^__version__ = "{re.escape(expected_version)}"$'
        ),
        "scripts/install-linux.sh": rf'^VERSION="{re.escape(expected_version)}"$',
        "scripts/install-windows.ps1": (
            rf"^\s*\[string\]\$Version = '{re.escape(expected_version)}',$"
        ),
        "README.md": rf"^Version {re.escape(expected_version)} provides:$",
        "docs/openclaw-wsl-guide.md": (
            rf"^## 2\. Clean installation of v{re.escape(expected_version)}$"
        ),
        "docs/openclaw-windows-guide.md": (
            rf"^## 3\. Clean installation of v{re.escape(expected_version)}$"
        ),
    }
    for relative, pattern in expectations.items():
        content = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        if re.search(pattern, content, flags=re.MULTILINE) is None:
            raise RuntimeError(f"{relative} does not advertise package version {expected_version}")


if __name__ == "__main__":
    raise SystemExit(main())
