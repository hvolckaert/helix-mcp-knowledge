#!/usr/bin/env bash

set -euo pipefail

VERSION="1.31.7"
REPOSITORY="hvolckaert/helix-mcp-knowledge"
INSTALL_ROOT="${XDG_DATA_HOME:-${HOME:?HOME is not defined}/.local/share}/helix-mcp-knowledge"
WHEEL_PATH=""
REQUIREMENTS_PATH=""
GH_VERSION="2.100.0"
PYTHON_COMMAND="python3"
OPENCLAW_COMMAND="openclaw"
CLIENT="auto"
SERVER_NAME="helix_knowledge"
DASHBOARD_PORT=8765
PRODUCTS=()
AUTOMATIC_SYNC=1
PROBE=1
RELOAD=1
RESUME=0
OPEN_DASHBOARD=1

usage() {
  cat <<'EOF'
Install helix-mcp-knowledge from a verified GitHub release.

Usage: install-linux.sh [options]

Options:
  --version VERSION          Release version (default: 1.31.7)
  --repository OWNER/REPO   GitHub repository
  --install-root PATH       Persistent workspace and runtime root
  --wheel PATH              Use a caller-provided wheel instead of downloading
  --requirements PATH       Locked requirements used with a caller-provided wheel
  --python-command COMMAND  Python 3.12+ command (default: python3)
  --openclaw-command CMD    OpenClaw command (default: openclaw)
  --client CLIENT           MCP client integration: auto, openclaw, or none (default: auto)
  --server-name NAME        MCP server name (default: helix_knowledge)
  --dashboard-port PORT     Dashboard loopback port (default: 8765)
  --product PRODUCT=VERSION Preselect a product/version; may be repeated
  --no-automatic-sync       Disable the packaged synchronization worker
  --no-probe                Register without probing the MCP server
  --no-reload               Do not reload the OpenClaw MCP catalog
  --no-dashboard            Do not open the dashboard browser after first-run setup
  --resume                  Repair and reuse an existing versioned runtime
  -h, --help                Show this help
EOF
}

fail() {
  printf 'install-linux.sh: %s\n' "$*" >&2
  exit 1
}

need_value() {
  local option="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || fail "$option requires a value"
}

resolve_command() {
  local candidate="$1"
  local label="$2"
  local resolved
  if [[ "$candidate" == */* ]]; then
    [[ -f "$candidate" && -x "$candidate" ]] || fail "$label was not found: $candidate"
    readlink -f -- "$candidate"
    return
  fi
  resolved="$(command -v -- "$candidate" || true)"
  [[ -n "$resolved" ]] || fail "$label was not found in PATH: $candidate"
  readlink -f -- "$resolved"
}

while (($#)); do
  case "$1" in
    --version)
      need_value "$1" "${2:-}"
      VERSION="$2"
      shift 2
      ;;
    --repository)
      need_value "$1" "${2:-}"
      REPOSITORY="$2"
      shift 2
      ;;
    --install-root)
      need_value "$1" "${2:-}"
      INSTALL_ROOT="$2"
      shift 2
      ;;
    --wheel)
      need_value "$1" "${2:-}"
      WHEEL_PATH="$2"
      shift 2
      ;;
    --requirements)
      need_value "$1" "${2:-}"
      REQUIREMENTS_PATH="$2"
      shift 2
      ;;
    --python-command)
      need_value "$1" "${2:-}"
      PYTHON_COMMAND="$2"
      shift 2
      ;;
    --openclaw-command)
      need_value "$1" "${2:-}"
      OPENCLAW_COMMAND="$2"
      shift 2
      ;;
    --client)
      need_value "$1" "${2:-}"
      CLIENT="$2"
      shift 2
      ;;
    --server-name)
      need_value "$1" "${2:-}"
      SERVER_NAME="$2"
      shift 2
      ;;
    --dashboard-port)
      need_value "$1" "${2:-}"
      DASHBOARD_PORT="$2"
      shift 2
      ;;
    --product)
      need_value "$1" "${2:-}"
      PRODUCTS+=("$2")
      shift 2
      ;;
    --no-automatic-sync)
      AUTOMATIC_SYNC=0
      shift
      ;;
    --no-probe)
      PROBE=0
      shift
      ;;
    --no-reload)
      RELOAD=0
      shift
      ;;
    --no-dashboard)
      OPEN_DASHBOARD=0
      shift
      ;;
    --resume)
      RESUME=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid version: $VERSION"
[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "invalid repository: $REPOSITORY"
[[ "$SERVER_NAME" =~ ^[A-Za-z0-9_-]+$ ]] || fail "invalid server name: $SERVER_NAME"
[[ "$DASHBOARD_PORT" =~ ^[0-9]+$ ]] || fail "invalid dashboard port: $DASHBOARD_PORT"
((DASHBOARD_PORT >= 1 && DASHBOARD_PORT <= 65535)) || fail "invalid dashboard port: $DASHBOARD_PORT"
[[ "$CLIENT" == "auto" || "$CLIENT" == "openclaw" || "$CLIENT" == "none" ]] || \
  fail "invalid client: $CLIENT (expected auto, openclaw, or none)"
for selection in "${PRODUCTS[@]}"; do
  [[ "$selection" =~ ^[a-z][a-z0-9_-]*=([0-9][0-9A-Za-z._-]*|current)$ ]] || \
    fail "invalid product selection: $selection"
done
[[ -z "$REQUIREMENTS_PATH" || -n "$WHEEL_PATH" ]] || \
  fail "--requirements can only be used together with --wheel"

PYTHON="$(resolve_command "$PYTHON_COMMAND" "Python")"
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || \
  fail "Python 3.12 or later is required: $PYTHON"

if [[ "$CLIENT" == "auto" ]]; then
  if command -v -- "$OPENCLAW_COMMAND" >/dev/null 2>&1; then
    CLIENT="openclaw"
  else
    CLIENT="none"
  fi
fi
if [[ "$CLIENT" == "openclaw" ]]; then
  OPENCLAW="$(resolve_command "$OPENCLAW_COMMAND" "OpenClaw")"
else
  OPENCLAW=""
fi

ROOT="$(realpath -m -- "$INSTALL_ROOT")"
[[ "$ROOT" != "/" ]] || fail "the filesystem root cannot be used as the install root"
RUNTIME="$ROOT/runtime/$VERSION"
VENV="$RUNTIME/venv"
VENV_PYTHON="$VENV/bin/python"
CLI="$VENV/bin/helix-mcp-knowledge"
SERVER="$VENV/bin/helix-mcp-knowledge-server"
CONFIG="$ROOT/config/config.yaml"
GH_CONFIG_DIR="$ROOT/tools/github-cli/config"

GH="$($PYTHON - "$ROOT" "$GH_VERSION" <<'PY'
import hashlib
import json
import os
import pathlib
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

root = pathlib.Path(sys.argv[1]).absolute()
version = sys.argv[2]
architecture = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}.get(platform.machine().lower())
assets = {
    "amd64": {
        "archive": f"gh_{version}_linux_amd64.tar.gz",
        "archive_sha256": "e4d4bb4498e8d007abe545b6568926793ace1b6447da598294a610018cb164be",
        "member": f"gh_{version}_linux_amd64/bin/gh",
        "binary_sha256": "553949e2efa12842771efe6012aa4de21f1d591530ec17fc435f610f10e017ee",
    },
    "arm64": {
        "archive": f"gh_{version}_linux_arm64.tar.gz",
        "archive_sha256": "ea4e7a581a32ccad6cc7923cb1576ac5859ba4b9a16ab22eb8f8a96e78e2e961",
        "member": f"gh_{version}_linux_arm64/bin/gh",
        "binary_sha256": "28a037b967065aa314cb6d539943b55d27ef2f97c523ab2b6023ccf284e1828d",
    },
}
if architecture not in assets:
    raise SystemExit("managed GitHub CLI is unavailable for this Linux architecture")
asset = assets[architecture]
version_root = root / "tools" / "github-cli" / version
command = version_root / "bin" / "gh"
config_dir = root / "tools" / "github-cli" / "config"
trusted_hosts = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
max_bytes = 64 * 1024 * 1024


def ensure_private_directory(path: pathlib.Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise SystemExit(f"managed GitHub CLI directory is not safe: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in trusted_hosts:
        raise SystemExit("GitHub CLI download left trusted GitHub hosts")


class TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        validate_url(new_url)
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def download(url: str, destination: pathlib.Path) -> None:
    validate_url(url)
    opener = urllib.request.build_opener(TrustedRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "helix-mcp-knowledge-installer",
        },
    )
    with opener.open(request, timeout=300) as response, destination.open("xb") as output:
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > max_bytes:
            raise SystemExit("GitHub CLI archive exceeds the size limit")
        size = 0
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > max_bytes:
                raise SystemExit("GitHub CLI archive exceeds the size limit")
            output.write(block)


def validate_command(path: pathlib.Path) -> None:
    environment = os.environ.copy()
    for key in (
        "GH_ENTERPRISE_TOKEN",
        "GH_HOST",
        "GH_REPO",
        "GH_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_TOKEN",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "GH_CONFIG_DIR": str(config_dir),
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_PROMPT_DISABLED": "1",
        }
    )
    completed = subprocess.run(
        [str(path), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    if not completed.stdout.startswith(f"gh version {version}"):
        raise SystemExit("managed GitHub CLI version is invalid")
    subprocess.run(
        [str(path), "attestation", "verify", "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )


for directory in (
    root,
    root / "tools",
    root / "tools" / "github-cli",
    version_root,
    command.parent,
    config_dir,
):
    ensure_private_directory(directory)

if command.is_symlink():
    raise SystemExit("managed GitHub CLI cannot be a symbolic link")
if not command.is_file() or sha256(command) != asset["binary_sha256"]:
    archive_handle, archive_name = tempfile.mkstemp(
        prefix=".download-", suffix=".tar.gz", dir=version_root
    )
    os.close(archive_handle)
    archive = pathlib.Path(archive_name)
    archive.unlink()
    binary_handle, binary_name = tempfile.mkstemp(prefix=".gh-", dir=version_root)
    os.close(binary_handle)
    binary = pathlib.Path(binary_name)
    binary.unlink()
    try:
        url = f"https://github.com/cli/cli/releases/download/v{version}/{asset['archive']}"
        download(url, archive)
        if sha256(archive) != asset["archive_sha256"]:
            raise SystemExit("GitHub CLI archive digest mismatch")
        with tarfile.open(archive, mode="r:gz") as bundle:
            members = [item for item in bundle.getmembers() if item.name == asset["member"]]
            if (
                len(members) != 1
                or not members[0].isfile()
                or members[0].size > max_bytes
            ):
                raise SystemExit("GitHub CLI archive has an invalid executable")
            source = bundle.extractfile(members[0])
            if source is None:
                raise SystemExit("GitHub CLI archive has an invalid executable")
            with source, binary.open("xb") as output:
                size = 0
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > max_bytes:
                        raise SystemExit("GitHub CLI executable exceeds the size limit")
                    output.write(block)
        if sha256(binary) != asset["binary_sha256"]:
            raise SystemExit("GitHub CLI binary digest mismatch")
        binary.chmod(0o700)
        validate_command(binary)
        os.replace(binary, command)
    finally:
        archive.unlink(missing_ok=True)
        binary.unlink(missing_ok=True)
else:
    command.chmod(0o700)
    validate_command(command)

metadata = version_root / "installation.json"
metadata_handle, metadata_name = tempfile.mkstemp(prefix=".metadata-", dir=version_root)
with os.fdopen(metadata_handle, "w", encoding="utf-8") as output:
    json.dump(
        {
            "schema_version": 1,
            "version": version,
            "command": str(command),
            "platform": "linux",
            "architecture": architecture,
            "source_url": (
                f"https://github.com/cli/cli/releases/download/v{version}/{asset['archive']}"
            ),
            "archive_sha256": asset["archive_sha256"],
            "binary_sha256": asset["binary_sha256"],
        },
        output,
        indent=2,
        sort_keys=True,
    )
    output.write("\n")
os.chmod(metadata_name, 0o600)
os.replace(metadata_name, metadata)
print(command)
PY
)" || fail "managed GitHub CLI could not be provisioned"

run_gh() {
  env \
    -u GH_ENTERPRISE_TOKEN -u GH_HOST -u GH_REPO -u GH_TOKEN \
    -u GITHUB_ENTERPRISE_TOKEN -u GITHUB_REPOSITORY -u GITHUB_TOKEN \
    GH_CONFIG_DIR="$GH_CONFIG_DIR" GH_NO_UPDATE_NOTIFIER=1 GH_PROMPT_DISABLED=1 \
    "$GH" "$@"
}

if [[ -e "$RUNTIME" || -L "$RUNTIME" ]]; then
  [[ -d "$RUNTIME" && ! -L "$RUNTIME" ]] || fail "runtime is not a regular directory: $RUNTIME"
  ((RESUME == 1)) || fail "versioned runtime already exists: $RUNTIME (use --resume to repair it)"
  printf 'Resuming existing runtime: %s\n' "$RUNTIME"
else
  mkdir -p -- "$RUNTIME"
fi

if [[ -n "$WHEEL_PATH" ]]; then
  [[ -f "$WHEEL_PATH" && ! -L "$WHEEL_PATH" ]] || fail "wheel was not found: $WHEEL_PATH"
  WHEEL="$(readlink -f -- "$WHEEL_PATH")"
  ACTUAL_HASH="$(sha256sum -- "$WHEEL" | awk '{print $1}')"
  printf 'Using caller-provided wheel (sha256:%s): %s\n' "$ACTUAL_HASH" "$WHEEL"
  if [[ -n "$REQUIREMENTS_PATH" ]]; then
    REQUIREMENTS_CANDIDATE="$REQUIREMENTS_PATH"
  else
    REQUIREMENTS_CANDIDATE="$(dirname -- "$WHEEL")/runtime-requirements.txt"
  fi
  [[ -f "$REQUIREMENTS_CANDIDATE" && ! -L "$REQUIREMENTS_CANDIDATE" ]] || \
    fail "locked runtime requirements were not found: $REQUIREMENTS_CANDIDATE"
  REQUIREMENTS="$(readlink -f -- "$REQUIREMENTS_CANDIDATE")"
  REQUIREMENTS_HASH="$(sha256sum -- "$REQUIREMENTS" | awk '{print $1}')"
  printf 'Using caller-provided locked requirements (sha256:%s): %s\n' \
    "$REQUIREMENTS_HASH" "$REQUIREMENTS"
else
  DOWNLOAD="$ROOT/downloads/$VERSION"
  ASSET_NAME="helix_mcp_knowledge-$VERSION-py3-none-any.whl"
  RELEASE_TAG="v$VERSION"
  WHEEL="$DOWNLOAD/$ASSET_NAME"
  REQUIREMENTS_NAME="runtime-requirements.txt"
  REQUIREMENTS="$DOWNLOAD/$REQUIREMENTS_NAME"
  mkdir -p -- "$DOWNLOAD"
  env -u GH_TOKEN -u GITHUB_TOKEN "$PYTHON" - "$REPOSITORY" "$RELEASE_TAG" "$DOWNLOAD" \
    "$ASSET_NAME" "$REQUIREMENTS_NAME" <<'PY'
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import urllib.parse
import urllib.request

repository, tag, download, *asset_names = sys.argv[1:]
download_path = pathlib.Path(download)
headers = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "helix-mcp-knowledge-installer",
}
trusted_hosts = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in trusted_hosts:
        raise SystemExit("GitHub release request left trusted GitHub hosts")


class TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        validate_url(new_url)
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


opener = urllib.request.build_opener(TrustedRedirectHandler())


def request(url: str):
    validate_url(url)
    return opener.open(urllib.request.Request(url, headers=headers), timeout=300)


with request(f"https://api.github.com/repos/{repository}/releases/tags/{tag}") as response:
    release = json.load(response)
if release.get("draft") or release.get("prerelease") or release.get("tag_name") != tag:
    raise SystemExit("GitHub returned an invalid or non-stable release")

for asset_name in asset_names:
    matching = [asset for asset in release.get("assets", []) if asset.get("name") == asset_name]
    if len(matching) != 1 or not str(matching[0].get("digest", "")).startswith("sha256:"):
        raise SystemExit(f"release digest was not found for {asset_name}")
    expected = matching[0]["digest"].removeprefix("sha256:").lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise SystemExit(f"release digest was invalid for {asset_name}")
    destination = download_path / asset_name
    if destination.is_symlink():
        raise SystemExit(f"release asset cannot be a link: {destination}")
    with tempfile.NamedTemporaryFile(dir=download_path, prefix=".download-", delete=False) as output:
        temporary = pathlib.Path(output.name)
        digest = hashlib.sha256()
        size = 0
        try:
            with request(
                f"https://github.com/{repository}/releases/download/{tag}/{asset_name}"
            ) as response:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > 256 * 1024 * 1024:
                        raise SystemExit(f"release asset is too large: {asset_name}")
                    digest.update(block)
                    output.write(block)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    if digest.hexdigest() != expected:
        temporary.unlink(missing_ok=True)
        raise SystemExit(f"release digest mismatch for {asset_name}")
    os.replace(temporary, destination)
    with request(
        f"https://api.github.com/repos/{repository}/attestations/sha256:{expected}"
    ) as response:
        provenance = json.load(response)
    bundles = [
        item.get("bundle")
        for item in provenance.get("attestations", [])
        if isinstance(item, dict) and isinstance(item.get("bundle"), dict)
    ]
    if not bundles:
        raise SystemExit(f"release asset has no verifiable provenance: {asset_name}")
    bundle_path = download_path / f".{asset_name}.attestations.jsonl"
    bundle_path.write_text(
        "".join(json.dumps(bundle, separators=(",", ":")) + "\n" for bundle in bundles),
        encoding="utf-8",
    )
PY
  [[ -f "$WHEEL" && ! -L "$WHEEL" ]] || fail "downloaded wheel was not found: $WHEEL"
  ACTUAL_HASH="$(sha256sum -- "$WHEEL" | awk '{print $1}')"
  [[ -f "$REQUIREMENTS" && ! -L "$REQUIREMENTS" ]] || \
    fail "downloaded locked requirements were not found: $REQUIREMENTS"
  REQUIREMENTS_HASH="$(sha256sum -- "$REQUIREMENTS" | awk '{print $1}')"
  for asset in "$WHEEL" "$REQUIREMENTS"; do
    bundle="$DOWNLOAD/.$(basename -- "$asset").attestations.jsonl"
    run_gh attestation verify "$asset" \
      --repo "$REPOSITORY" \
      --bundle "$bundle" \
      --signer-workflow "$REPOSITORY/.github/workflows/release.yml" \
      --source-ref "refs/tags/$RELEASE_TAG" \
      --deny-self-hosted-runners
    rm -f -- "$bundle"
  done
  printf 'Verified release wheel sha256:%s\n' "$ACTUAL_HASH"
  printf 'Verified locked runtime requirements sha256:%s\n' "$REQUIREMENTS_HASH"
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV_PYTHON" -m pip install --disable-pip-version-check \
  --require-hashes --requirement "$REQUIREMENTS"
"$VENV_PYTHON" -m pip install --disable-pip-version-check --no-deps --upgrade "$WHEEL"
"$VENV_PYTHON" -m pip check

[[ -x "$CLI" ]] || fail "CLI entry point was not installed: $CLI"
[[ -x "$SERVER" ]] || fail "server entry point was not installed: $SERVER"
INSTALLED_VERSION="$("$VENV_PYTHON" -c 'import helix_mcp_knowledge; print(helix_mcp_knowledge.__version__)')"
[[ "$INSTALLED_VERSION" == "$VERSION" ]] || \
  fail "installed version $INSTALLED_VERSION does not match requested version $VERSION"

if [[ "$CLIENT" == "openclaw" ]]; then
  INSTALL_ARGUMENTS=(
    install-openclaw
    --workspace "$ROOT"
    --server-name "$SERVER_NAME"
    --openclaw-command "$OPENCLAW"
    --connect-timeout 30
    --request-timeout 60
  )
else
  INSTALL_ARGUMENTS=(install --workspace "$ROOT")
fi
INSTALL_ARGUMENTS+=(--dashboard-port "$DASHBOARD_PORT")
for selection in "${PRODUCTS[@]}"; do
  INSTALL_ARGUMENTS+=(--product "$selection")
done
if ((AUTOMATIC_SYNC == 1)); then
  INSTALL_ARGUMENTS+=(--automatic-sync)
else
  INSTALL_ARGUMENTS+=(--no-automatic-sync)
fi
if [[ "$CLIENT" == "openclaw" ]]; then
  ((PROBE == 1)) || INSTALL_ARGUMENTS+=(--no-probe)
  ((RELOAD == 1)) || INSTALL_ARGUMENTS+=(--no-reload)
fi
((OPEN_DASHBOARD == 1)) || INSTALL_ARGUMENTS+=(--no-dashboard)

"$CLI" "${INSTALL_ARGUMENTS[@]}"

if ((${#PRODUCTS[@]} == 0)); then
  if ((OPEN_DASHBOARD == 1)); then
    printf 'No products were selected. The dashboard is opening for first-run setup.\n'
  else
    printf 'No products were selected. The dashboard service is running at http://127.0.0.1:%s/.\n' "$DASHBOARD_PORT"
  fi
fi
"$CLI" --config "$CONFIG" status

if [[ "$CLIENT" == "openclaw" ]] && ((PROBE == 1)); then
  "$OPENCLAW" mcp doctor "$SERVER_NAME" --probe
  "$OPENCLAW" mcp probe "$SERVER_NAME" --json
fi

printf 'Installed helix-mcp-knowledge %s in %s\n' "$VERSION" "$RUNTIME"
printf 'Configuration: %s\n' "$CONFIG"
printf 'Stable MCP command: %s\n' "$ROOT/bin/helix-mcp-knowledge-server"
if [[ "$CLIENT" == "none" ]]; then
  printf 'No MCP client was configured. Register the stable MCP command in your client when ready.\n'
fi
printf 'Rollback runtimes, if any, were not modified.\n'
