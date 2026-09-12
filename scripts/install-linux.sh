#!/usr/bin/env bash

set -euo pipefail

VERSION="1.29.0"
REPOSITORY="hvolckaert/helix-mcp-knowledge"
INSTALL_ROOT="${XDG_DATA_HOME:-${HOME:?HOME is not defined}/.local/share}/helix-mcp-knowledge"
WHEEL_PATH=""
REQUIREMENTS_PATH=""
GH=""
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
  --version VERSION          Release version (default: 1.29.0)
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
  GH="$(resolve_command "gh" "GitHub CLI")"
  DOWNLOAD="$ROOT/downloads/$VERSION"
  ASSET_NAME="helix_mcp_knowledge-$VERSION-py3-none-any.whl"
  RELEASE_TAG="v$VERSION"
  WHEEL="$DOWNLOAD/$ASSET_NAME"
  REQUIREMENTS_NAME="runtime-requirements.txt"
  REQUIREMENTS="$DOWNLOAD/$REQUIREMENTS_NAME"
  mkdir -p -- "$DOWNLOAD"
  "$GH" release download "$RELEASE_TAG" \
    --repo "$REPOSITORY" \
    --pattern "$ASSET_NAME" \
    --output "$WHEEL" \
    --clobber
  "$GH" release download "$RELEASE_TAG" \
    --repo "$REPOSITORY" \
    --pattern "$REQUIREMENTS_NAME" \
    --output "$REQUIREMENTS" \
    --clobber
  [[ -f "$WHEEL" && ! -L "$WHEEL" ]] || fail "downloaded wheel was not found: $WHEEL"
  EXPECTED_DIGEST="$(
    "$GH" release view "$RELEASE_TAG" \
      --repo "$REPOSITORY" \
      --json assets \
      --jq ".assets[] | select(.name == \"$ASSET_NAME\") | .digest"
  )"
  [[ "$EXPECTED_DIGEST" =~ ^sha256:[0-9A-Fa-f]{64}$ ]] || \
    fail "release digest was not found for $ASSET_NAME"
  EXPECTED_HASH="${EXPECTED_DIGEST#sha256:}"
  EXPECTED_HASH="${EXPECTED_HASH,,}"
  ACTUAL_HASH="$(sha256sum -- "$WHEEL" | awk '{print $1}')"
  [[ "$ACTUAL_HASH" == "$EXPECTED_HASH" ]] || \
    fail "wheel digest mismatch: expected $EXPECTED_HASH, found $ACTUAL_HASH"
  printf 'Verified release wheel sha256:%s\n' "$ACTUAL_HASH"
  [[ -f "$REQUIREMENTS" && ! -L "$REQUIREMENTS" ]] || \
    fail "downloaded locked requirements were not found: $REQUIREMENTS"
  EXPECTED_REQUIREMENTS_DIGEST="$(
    "$GH" release view "$RELEASE_TAG" \
      --repo "$REPOSITORY" \
      --json assets \
      --jq ".assets[] | select(.name == \"$REQUIREMENTS_NAME\") | .digest"
  )"
  [[ "$EXPECTED_REQUIREMENTS_DIGEST" =~ ^sha256:[0-9A-Fa-f]{64}$ ]] || \
    fail "release digest was not found for $REQUIREMENTS_NAME"
  EXPECTED_REQUIREMENTS_HASH="${EXPECTED_REQUIREMENTS_DIGEST#sha256:}"
  EXPECTED_REQUIREMENTS_HASH="${EXPECTED_REQUIREMENTS_HASH,,}"
  REQUIREMENTS_HASH="$(sha256sum -- "$REQUIREMENTS" | awk '{print $1}')"
  [[ "$REQUIREMENTS_HASH" == "$EXPECTED_REQUIREMENTS_HASH" ]] || \
    fail "requirements digest mismatch: expected $EXPECTED_REQUIREMENTS_HASH, found $REQUIREMENTS_HASH"
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
if [[ -n "$GH" ]]; then
  INSTALL_ARGUMENTS+=(--gh-command "$GH")
fi
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
