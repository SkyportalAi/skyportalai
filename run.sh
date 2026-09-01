#!/usr/bin/env bash
# Bootstraps a source checkout and starts the CLI.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SKYPORTALAI_VENV:-"${SKYPORTAL_VENV:-}"}"

log() {
  echo "run.sh: $*" >&2
}

if [[ -n "$VENV_DIR" ]]; then
  export UV_PROJECT_ENVIRONMENT="$VENV_DIR"
fi

install_uv() {
  log "uv not found; installing it to ~/.local/bin ..."

  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    log "neither curl nor wget is available to download uv."
    log "Install it manually (https://docs.astral.sh/uv/getting-started/installation/) and rerun ./run.sh"
    exit 1
  fi

  export PATH="${XDG_BIN_HOME:-${XDG_DATA_HOME:-$HOME/.local}/bin}:$HOME/.local/bin:$PATH"
}

if ! command -v uv >/dev/null 2>&1; then
  install_uv
fi

if ! command -v uv >/dev/null 2>&1; then
  log "uv is installed but not on PATH. Open a new shell, or add ~/.local/bin to PATH, then rerun ./run.sh"
  exit 1
fi

cd "$ROOT_DIR"
exec uv run --no-dev skyportalai start "$@"
