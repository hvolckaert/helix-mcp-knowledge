"""Exercise a real wheel update with isolated fake GitHub and OpenClaw CLIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

from helix_mcp_knowledge import __version__
from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.cli.configure import configure_official_docs
from helix_mcp_knowledge.managed_installation import stable_launcher_path
from helix_mcp_knowledge.models.document import DocumentType
from helix_mcp_knowledge.models.ingestion import IngestRequest
from helix_mcp_knowledge.models.source import SourceScope
from helix_mcp_knowledge.openclaw import EXPOSED_TOOLS, openclaw_stdio_invocation
from helix_mcp_knowledge.updater import update_installation
from helix_mcp_knowledge.workspace import initialize_workspace


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", nargs="?", help="Built wheel; defaults to the only dist/*.whl")
    return parser.parse_args()


def _wheel_path(value: str | None) -> Path:
    if value:
        wheel = Path(value).resolve()
        if not wheel.is_file():
            raise SystemExit(f"wheel not found: {wheel}")
        return wheel
    candidates = sorted(Path("dist").glob(f"helix_mcp_knowledge-{__version__}-*.whl"))
    if len(candidates) != 1:
        raise SystemExit(f"expected exactly one dist wheel, found {len(candidates)}")
    return candidates[0].resolve()


def _version_from_wheel(wheel: Path) -> str:
    prefix = "helix_mcp_knowledge-"
    if not wheel.name.startswith(prefix):
        raise SystemExit(f"unexpected wheel name: {wheel.name}")
    return wheel.name[len(prefix) :].split("-", 1)[0]


def _write_wrapper(path: Path, script: Path) -> None:
    if os.name == "nt":
        path.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="ascii",
        )
        escaped_python = str(sys.executable).replace("'", "''")
        escaped_script = str(script).replace("'", "''")
        path.with_suffix(".ps1").write_text(
            f"& '{escaped_python}' '{escaped_script}' @args\nexit $LASTEXITCODE\n",
            encoding="utf-8",
        )
        if path.stem.casefold() == "openclaw":
            npm_entrypoint = path.parent / "node_modules/openclaw/openclaw.mjs"
            npm_entrypoint.parent.mkdir(parents=True)
            npm_entrypoint.write_text(
                "import { spawnSync } from 'node:child_process';\n"
                f"const executable = {json.dumps(str(sys.executable))};\n"
                f"const script = {json.dumps(str(script))};\n"
                "const child = spawnSync(executable, "
                "[script, ...process.argv.slice(2)], { stdio: 'inherit' });\n"
                "process.exit(child.status ?? 1);\n",
                encoding="utf-8",
            )
    else:
        path.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o700)


def _write_fake_gh(
    script: Path,
    *,
    wheel: Path,
    requirements: Path,
    version: str,
    sha256: str,
    requirements_sha256: str,
) -> None:
    script.write_text(
        f"""from __future__ import annotations
import json
import shutil
import sys
from pathlib import Path

WHEEL = Path({str(wheel)!r})
REQUIREMENTS = Path({str(requirements)!r})
VERSION = {version!r}
SHA256 = {sha256!r}
REQUIREMENTS_SHA256 = {requirements_sha256!r}
args = sys.argv[1:]
if args[:2] == ["release", "view"]:
    print(json.dumps({{
        "tagName": f"v{{VERSION}}",
        "isDraft": False,
        "isPrerelease": False,
        "assets": [
            {{"name": WHEEL.name, "digest": f"sha256:{{SHA256}}"}},
            {{
                "name": REQUIREMENTS.name,
                "digest": f"sha256:{{REQUIREMENTS_SHA256}}",
            }},
        ],
    }}))
elif args[:2] == ["release", "download"]:
    asset_name = args[args.index("--pattern") + 1]
    source = REQUIREMENTS if asset_name == REQUIREMENTS.name else WHEEL
    destination = Path(args[args.index("--dir") + 1]) / asset_name
    shutil.copy2(source, destination)
else:
    raise SystemExit(f"unexpected fake gh arguments: {{args!r}}")
""",
        encoding="utf-8",
    )


def _write_fake_openclaw(script: Path, *, state: Path) -> None:
    tools = [f"helix_knowledge__{tool}" for tool in EXPOSED_TOOLS]
    script.write_text(
        f"""from __future__ import annotations
import json
import os
import sys
from pathlib import Path

STATE = Path({str(state)!r})
TOOLS = {tools!r}
args = sys.argv[1:]
definition = json.loads(STATE.read_text(encoding="utf-8"))
if args[:3] == ["mcp", "show", "helix_knowledge"]:
    print(json.dumps(definition))
elif args[:3] == ["mcp", "set", "helix_knowledge"]:
    updated = json.loads(args[3])
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(updated), encoding="utf-8")
    os.replace(temporary, STATE)
elif args[:2] == ["mcp", "reload"]:
    pass
elif args[:3] == ["mcp", "probe", "helix_knowledge"]:
    launch = " ".join([definition["command"], *definition.get("args", [])])
    print(json.dumps({{
        "servers": {{
            "helix_knowledge": {{
                "launch": f"{{launch}} (cwd={{definition['cwd']}})"
            }}
        }},
        "tools": TOOLS,
        "diagnostics": [],
    }}))
else:
    raise SystemExit(f"unexpected fake OpenClaw arguments: {{args!r}}")
""",
        encoding="utf-8",
    )


def _prepare_workspace(workspace: Path) -> Path:
    initialized = initialize_workspace(workspace)
    config = initialized.config
    configure_official_docs(
        config,
        product_specs=["cmdb=26.1"],
        no_products=False,
        automatic_sync=False,
        bootstrap_on_empty=None,
        interval_hours=None,
        retain_unselected_versions=None,
    )
    source = workspace / "data/sources/bmc/official/update-acceptance.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "# CMDB reconciliation\n\nNormalization and reconciliation preserve CMDB integrity.\n",
        encoding="utf-8",
    )
    application = KnowledgeApplication.from_config(config)
    result = application.ingestion_manager.ingest(
        IngestRequest(
            source_path=source,
            source_scope=SourceScope.BMC_OFFICIAL,
            document_type=DocumentType.ADMINISTRATION,
            language="en",
            product_versions={"cmdb": "26.1"},
            source_url="https://docs.helixops.ai/update-acceptance/",
        )
    )
    if result.status != "indexed":
        raise SystemExit(f"could not prepare acceptance document: {result.status}")
    return config


def main() -> int:
    wheel = _wheel_path(_arguments().wheel)
    version = _version_from_wheel(wheel)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    requirements = Path("runtime-requirements.txt").resolve()
    if not requirements.is_file():
        raise SystemExit(f"locked runtime requirements not found: {requirements}")
    requirements_digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="helix-update-acceptance-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace with spaces"
        config = _prepare_workspace(workspace)
        reranker_marker = workspace / "components/reranker/preserve.marker"
        reranker_marker.parent.mkdir(parents=True, exist_ok=True)
        reranker_marker.write_text("optional component data", encoding="utf-8")
        executable_dir = (
            workspace / "runtime/0.0.1/venv" / ("Scripts" if os.name == "nt" else "bin")
        )
        executable_dir.mkdir(parents=True)
        suffix = ".exe" if os.name == "nt" else ""
        current_python = executable_dir / f"python{suffix}"
        current_server = executable_dir / f"helix-mcp-knowledge-server{suffix}"
        current_python.write_text("placeholder", encoding="utf-8")
        current_server.write_text("placeholder", encoding="utf-8")

        command_dir = root / "commands with spaces"
        command_dir.mkdir()
        gh_script = command_dir / "fake_gh.py"
        openclaw_script = command_dir / "fake_openclaw.py"
        gh = command_dir / ("gh.cmd" if os.name == "nt" else "gh")
        openclaw = command_dir / ("openclaw.cmd" if os.name == "nt" else "openclaw")
        state = root / "openclaw-server.json"
        state.write_text(
            json.dumps(
                {
                    "command": str(current_server),
                    "cwd": str(workspace),
                    "env": {"HELIX_KNOWLEDGE_CONFIG": str(config)},
                    "toolFilter": {"include": list(EXPOSED_TOOLS)},
                    "connectTimeout": 30,
                    "timeout": 60,
                }
            ),
            encoding="utf-8",
        )
        _write_fake_gh(
            gh_script,
            wheel=wheel,
            requirements=requirements,
            version=version,
            sha256=digest,
            requirements_sha256=requirements_digest,
        )
        _write_fake_openclaw(openclaw_script, state=state)
        _write_wrapper(gh, gh_script)
        _write_wrapper(openclaw, openclaw_script)

        result = update_installation(
            config_path=config,
            target_version=version,
            openclaw_command=openclaw,
            gh_command=gh,
            current_version="0.0.1",
            current_python=current_python,
            base_python=Path(getattr(sys, "_base_executable", sys.executable)),
        )
        active = json.loads(state.read_text(encoding="utf-8"))
        launcher = stable_launcher_path(workspace)
        expected_command, expected_arguments = openclaw_stdio_invocation(launcher)
        if (
            result.status != "updated"
            or Path(active["command"]).resolve() != expected_command.resolve()
            or active.get("args", []) != list(expected_arguments)
            or version not in launcher.read_text(encoding="utf-8")
        ):
            raise SystemExit("the built wheel was not activated by the update acceptance flow")
        if not result.backup or not (result.backup / "helix_mcp_knowledge.db").is_file():
            raise SystemExit("the update acceptance flow did not retain a database backup")
        if reranker_marker.read_text(encoding="utf-8") != "optional component data":
            raise SystemExit("the update acceptance flow did not preserve optional components")
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
