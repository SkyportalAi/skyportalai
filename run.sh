#!/usr/bin/env bash
# Bootstraps a source checkout and starts the CLI.
#
# Everything below delegates to uv, which provisions the interpreter pinned in
# .python-version, resolves dependencies and installs the project in one step.
# The previous hand-rolled ladder (venv -> ensurepip -> apt -> get-pip.py) was
# removed: a venv seeded with a pre-PEP 660 pip cannot editable-install this
# project at all, because the build backend is poetry-core rather than
# setuptools, and the ladder had no way to detect that.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
  echo "run.sh: $*" >&2
}

# Honour the historical override so existing scripts keep working.
if [[ -n "${SKYPORTAL_VENV:-}" ]]; then
  export UV_PROJECT_ENVIRONMENT="$SKYPORTAL_VENV"
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

  # The installer does not touch the PATH of the shell that invoked it.
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
exec uv run --no-dev skyportal start "$@"
