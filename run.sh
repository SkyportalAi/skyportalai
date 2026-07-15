#!/usr/bin/env bash
# Intentionally no `set -e`: every fallible step below is checked explicitly so
# that we can fall through to the next recovery strategy instead of aborting.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SKYPORTAL_VENV:-"$ROOT_DIR/.venv"}"
PY_MINOR="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

log() {
  echo "run.sh: $*" >&2
}

# Installs one or more apt packages. Returns 1 (without aborting the script)
# if apt-get is unavailable or the install fails, so callers can try the next
# recovery strategy.
apt_install() {
  local -a packages=("$@")
  local -a apt_cmd=(apt-get)

  command -v apt-get >/dev/null 2>&1 || return 1
  command -v sudo >/dev/null 2>&1 && apt_cmd=(sudo apt-get)

  log "installing ${packages[*]} via apt (you may be prompted for your password)..."
  "${apt_cmd[@]}" update || log "apt-get update reported an error; continuing anyway."
  "${apt_cmd[@]}" install -y "${packages[@]}"
}

venv_has_pip() {
  [[ -x "$VENV_DIR/bin/python" ]] && "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1
}

# Attempts to create a fully working venv (including pip) with plain `python3 -m venv`.
create_venv_with_pip() {
  local err_log
  err_log="$(mktemp)"

  if python3 -m venv "$VENV_DIR" >"$err_log" 2>&1; then
    rm -f "$err_log"
    return 0
  fi

  if grep -qi "ensurepip" "$err_log"; then
    cat "$err_log" >&2
    rm -f "$err_log"
    log "installing python${PY_MINOR}-venv to provide ensurepip support..."
    if apt_install "python${PY_MINOR}-venv" || apt_install python3-venv; then
      rm -rf "$VENV_DIR"
      if python3 -m venv "$VENV_DIR" >"$err_log" 2>&1; then
        rm -f "$err_log"
        return 0
      fi
      cat "$err_log" >&2
    fi
  else
    cat "$err_log" >&2
  fi

  rm -f "$err_log"
  return 1
}

# Last-resort recovery that needs neither apt nor sudo: build the venv skeleton
# without pip, then bootstrap pip via the official get-pip.py installer.
bootstrap_pip_with_getpip() {
  rm -rf "$VENV_DIR"
  python3 -m venv --without-pip "$VENV_DIR" || return 1

  local get_pip
  get_pip="$(mktemp --suffix=.py)"

  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$get_pip" || { rm -f "$get_pip"; return 1; }
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$get_pip" https://bootstrap.pypa.io/get-pip.py || { rm -f "$get_pip"; return 1; }
  else
    log "neither curl nor wget is available to fetch get-pip.py."
    rm -f "$get_pip"
    return 1
  fi

  "$VENV_DIR/bin/python" "$get_pip" -q
  local status=$?
  rm -f "$get_pip"
  return $status
}

ensure_venv() {
  if venv_has_pip; then
    return 0
  fi

  log "setting up virtual environment at $VENV_DIR"

  if [[ ! -x "$VENV_DIR/bin/python" ]] && create_venv_with_pip && venv_has_pip; then
    return 0
  fi

  # venv exists but pip is missing: try ensurepip directly first.
  if [[ -x "$VENV_DIR/bin/python" ]] && "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 && venv_has_pip; then
    return 0
  fi

  # Try installing the system pip package, then rebuild the venv.
  if apt_install "python${PY_MINOR}-pip" || apt_install python3-pip; then
    rm -rf "$VENV_DIR"
    create_venv_with_pip
    venv_has_pip && return 0
  fi

  log "falling back to 'venv --without-pip' plus get-pip.py bootstrap..."
  if bootstrap_pip_with_getpip && venv_has_pip; then
    return 0
  fi

  log "failed to provision pip in $VENV_DIR."
  log "Please run: sudo apt install python${PY_MINOR}-venv python3-pip, then rerun ./run.sh"
  exit 1
}

ensure_venv

"$VENV_DIR/bin/python" -m pip install -q -e "$ROOT_DIR"
exec "$VENV_DIR/bin/skyportal" start
