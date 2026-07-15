"""
W&B (Weights & Biases) scanner adapter.

Discovers and extracts experiment data from local W&B run directories.
Handles both online and offline runs, parsing .wandb binary protobuf
files, wandb-metadata.json, wandb-summary.json, and config.yaml.

No Django imports -- this runs on remote hosts.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .base_scanner import DEFAULT_SEARCH_ROOTS, BaseScanner, Catalog, RunData, iso_now

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

try:
    import wandb  # noqa: F401
    from wandb.proto import wandb_internal_pb2
    from wandb.sdk.internal import datastore
except ImportError:
    wandb = None
    datastore = None
    wandb_internal_pb2 = None


STALE_PID_GRACE_SECONDS = 5.0


# ── Logging helper ────────────────────────────────────────────────────

def _log(msg: str, log_path: Path | None) -> None:
    line = f"[{iso_now()}] {msg}"
    print(line)
    if log_path:
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


# ── Pure‑function helpers (no state) ──────────────────────────────────

def _load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _load_yaml_file(path: Path) -> dict[str, Any] | None:
    if not path.exists() or yaml is None:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except Exception:
        return None


def _safe_join(base: Path, rel: str) -> Path | None:
    """Join *rel* onto *base*, returning ``None`` if it escapes *base*.

    Run metadata/config is attacker-influenceable, so a reference like an
    absolute path or one containing ``..`` could otherwise read arbitrary
    host files (and ship them in the payload). We reject anything that does
    not resolve to a location inside *base*.
    """
    if not rel:
        return None
    candidate = Path(rel)
    if candidate.is_absolute():
        return None
    resolved = (base / candidate).resolve()
    if _is_within(resolved, base):
        return resolved
    return None


def _is_within(path: Path, base: Path) -> bool:
    """Return *True* if *path* resolves to *base* or a location inside it.

    Resolves symlinks first, so a symlink placed inside *base* that points
    outside (e.g. ``files/code/foo.py -> /etc/passwd``) is rejected.
    """
    resolved = path.resolve()
    base_resolved = base.resolve()
    return resolved == base_resolved or base_resolved in resolved.parents


def _derive_run_id_from_dir(dirname: str) -> str:
    parts = dirname.split("-")
    return parts[-1] if parts else dirname


def _derive_start_time_from_dir(dirname: str) -> float | None:
    import datetime as dt

    try:
        for part in dirname.split("-"):
            if len(part) == 15 and "_" in part:
                try:
                    dt_obj = dt.datetime.strptime(part, "%Y%m%d_%H%M%S")
                    return dt_obj.replace(tzinfo=dt.timezone.utc).timestamp()
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
        return header.startswith(b"SQLite format 3")
    except Exception:
        return False


def _extract_from_tables(conn: sqlite3.Connection, table_candidates: list[str]) -> dict[str, Any]:
    cursor = conn.cursor()
    tables = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    data: dict[str, Any] = {}

    for table in table_candidates:
        if table not in tables:
            continue
        # SQLite does not support parameter placeholders for identifiers. Quote
        # and escape the candidate even though current callers pass constants,
        # so this helper remains safe if candidates ever become configurable.
        quoted_table = '"' + table.replace('"', '""') + '"'
        try:
            columns = [c[1] for c in cursor.execute(f"PRAGMA table_info({quoted_table})")]
            lower_cols = {c.lower() for c in columns}

            if {"key", "value_json"} <= lower_cols:
                for k, v in cursor.execute(
                    f"SELECT key, value_json FROM {quoted_table}"
                ).fetchall():
                    try:
                        data[k] = json.loads(v)
                    except Exception:
                        data[k] = v
            elif {"key", "value"} <= lower_cols:
                for k, v in cursor.execute(
                    f"SELECT key, value FROM {quoted_table}"
                ).fetchall():
                    data[k] = v
            elif {"json"} <= lower_cols:
                row = cursor.execute(f"SELECT json FROM {quoted_table} LIMIT 1").fetchone()
                if row and row[0]:
                    try:
                        data.update(json.loads(row[0]))
                    except Exception:
                        pass
            else:
                row = cursor.execute(f"SELECT * FROM {quoted_table} LIMIT 1").fetchone()
                if row and columns:
                    data.update({columns[i]: row[i] for i in range(min(len(columns), len(row)))})
        except Exception:
            continue
    return data


def _scan_wandb_file(scan_path: Path, log_path: Path | None) -> tuple[dict, dict, dict, list[dict]]:
    """Parse a .wandb binary protobuf file using the wandb datastore API."""
    meta: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    config: dict[str, Any] = {}
    history: list[dict[str, Any]] = []

    if datastore is None or wandb_internal_pb2 is None:
        return meta, summary, config, history

    try:
        ds = datastore.DataStore()
        ds.open_for_scan(str(scan_path))

        while True:
            data = ds.scan_record()
            if data is None:
                break
            if len(data) < 2:
                continue

            try:
                pb = wandb_internal_pb2.Record()
                pb.ParseFromString(data[1])
                record_type = pb.WhichOneof("record_type")

                if record_type == "run":
                    run = pb.run
                    meta.update({
                        "run_id": run.run_id,
                        "project": run.project,
                        "entity": run.entity,
                        "display_name": run.display_name,
                        "start_time": (
                            run.start_time.seconds + run.start_time.nanos / 1e9
                            if run.start_time
                            else None
                        ),
                    })
                    if run.config:
                        for update in run.config.update:
                            try:
                                config[update.key] = json.loads(update.value_json)
                            except Exception:
                                config[update.key] = update.value_json

                elif record_type == "summary":
                    for item in pb.summary.update:
                        key = "/".join(item.nested_key)
                        try:
                            summary[key] = json.loads(item.value_json)
                        except Exception:
                            summary[key] = item.value_json

                elif record_type == "history":
                    step_data: dict[str, Any] = {}
                    for item in pb.history.item:
                        key = "/".join(item.nested_key)
                        try:
                            step_data[key] = json.loads(item.value_json)
                        except Exception:
                            step_data[key] = item.value_json
                    history.append(step_data)

                elif record_type == "environment":
                    env = pb.environment
                    if env.program:
                        meta["program"] = env.program
                    if env.code_path:
                        meta["codePath"] = env.code_path
                    if env.code_path_local:
                        meta["codePathLocal"] = env.code_path_local

                elif record_type == "exit":
                    exit_data = pb.exit
                    if exit_data.runtime:
                        start = meta.get("start_time")
                        if start:
                            meta["end_time"] = start + exit_data.runtime
                        meta["runtime"] = exit_data.runtime
                    if exit_data.exit_code:
                        meta["exit_code"] = exit_data.exit_code

            except Exception as e:
                _log(f"Skipping bad record in {scan_path}: {e}", log_path)
                continue

    except Exception as e:
        _log(f"Error parsing binary wandb file {scan_path}: {e}", log_path)

    return meta, summary, config, history


def _parse_wandb_binary(db_path: Path, log_path: Path | None) -> tuple[dict, dict, dict, list[dict]]:
    """Parse a .wandb file, using a temp copy to avoid locking issues."""
    if wandb is None:
        return {}, {}, {}, []

    import shutil
    import tempfile

    # Use mkstemp so concurrent cron invocations (or multiple runs sharing a
    # common filename) cannot clobber each other or read a partial copy.
    temp_fd, temp_name = tempfile.mkstemp(prefix=f"tmp_copy_{db_path.name}_", suffix=".wandb")
    os.close(temp_fd)
    temp_db_path = Path(temp_name)

    scan_path = db_path
    copied = False
    try:
        shutil.copy2(db_path, temp_db_path)
        scan_path = temp_db_path
        copied = True
    except Exception as e:
        _log(f"Failed to copy wandb file {db_path} to temp: {e}", log_path)

    meta, summary, config, history = _scan_wandb_file(scan_path, log_path)

    if copied and not meta and not summary and not history:
        meta, summary, config, history = _scan_wandb_file(db_path, log_path)

    if temp_db_path.exists():
        try:
            temp_db_path.unlink()
        except Exception:
            pass

    return meta, summary, config, history


def _parse_wandb_db(
    db_path: Path, log_path: Path | None,
) -> tuple[dict, dict, dict, list[dict]]:
    if not db_path.exists():
        return {}, {}, {}, []
    if not _is_sqlite_file(db_path):
        return _parse_wandb_binary(db_path, log_path)
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except Exception:
        _log(f"Skipping DB parse (unable to open): {db_path.name}", log_path)
        return {}, {}, {}, []

    meta = _extract_from_tables(conn, ["meta", "run_meta", "t_meta"])
    summary = _extract_from_tables(conn, ["summary", "summary_metrics", "t_summary"])
    config = _extract_from_tables(conn, ["config", "t_config", "run_config"])
    conn.close()
    return meta, summary, config, []


def _parse_run_files(run_dir: Path) -> tuple[dict, dict, dict]:
    files_dir = run_dir / "files"
    metadata = _load_json_file(files_dir / "wandb-metadata.json") or {}
    summary = _load_json_file(files_dir / "wandb-summary.json") or {}
    config = _load_yaml_file(files_dir / "config.yaml") or _load_json_file(files_dir / "config.json") or {}
    return metadata, summary, config


def _resolve_wandb_references(data: Any, run_dir: Path) -> Any:
    if isinstance(data, dict):
        if data.get("_type") == "table-file" and "path" in data:
            for base in [run_dir, run_dir / "files"]:
                file_path = _safe_join(base, data["path"])
                if file_path is not None and file_path.exists():
                    try:
                        with file_path.open("r", encoding="utf-8") as fh:
                            return json.load(fh)
                    except Exception:
                        pass
        return {k: _resolve_wandb_references(v, run_dir) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_wandb_references(item, run_dir) for item in data]
    return data


def _sanitize_value(v: Any) -> Any:
    if v is None:
        return "None"
    if v is True:
        return "True"
    if v is False:
        return "False"
    if isinstance(v, str):
        lower = v.lower()
        if lower == "null":
            return "None"
        if lower == "false":
            return "False"
        if lower == "true":
            return "True"
    if isinstance(v, dict):
        return {k: _sanitize_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_sanitize_value(item) for item in v]
    return v


def _search_for_script(obj: Any, keys: list[str]) -> str | None:
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k]:
                return obj[k]
        for v in obj.values():
            found = _search_for_script(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _search_for_script(item, keys)
            if found:
                return found
    return None


def _extract_script_path(
    metadata: dict, config: dict, db_meta: dict, db_config: dict,
) -> str:
    keys = ["program", "codePath", "codePathLocal", "script_path"]
    for source in (metadata, config, db_meta, db_config):
        if not source:
            continue
        for k in keys:
            if isinstance(source.get(k), str) and source[k]:
                return source[k]
        wandb_obj = source.get("_wandb")
        if wandb_obj:
            found = _search_for_script(wandb_obj, keys + ["code_path"])
            if found:
                return found
        found = _search_for_script(source, keys + ["code_path"])
        if found:
            return found
    return "unknown"


def _get_pid_from_logs(run_dir: Path) -> int | None:
    log_path = run_dir / "logs" / "debug.log"
    if not log_path.exists():
        return None
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as fh:
            for _ in range(50):
                line = fh.readline()
                if not line:
                    break
                m = re.search(r"MainThread:(\d+)", line)
                if m:
                    return int(m.group(1))
                m = re.search(r"Configure stats pid to (\d+)", line)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return None


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        if os.path.exists(f"/proc/{pid}/stat"):
            with open(f"/proc/{pid}/stat", "r") as fh:
                content = fh.read()
                r_par = content.rfind(")")
                if r_par != -1:
                    state = content[r_par + 2]
                    if state == "Z":
                        return False
    except OSError:
        return False
    except Exception:
        pass
    return True


def _process_start_time(pid: int) -> float | None:
    """Return Linux process start time as epoch seconds when available."""
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return None

    try:
        boot_time = None
        with Path("/proc/stat").open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("btime "):
                    boot_time = float(line.split()[1])
                    break
        if boot_time is None:
            return None

        content = stat_path.read_text(encoding="utf-8", errors="ignore")
        r_par = content.rfind(")")
        if r_par == -1:
            return None
        parts = content[r_par + 2:].split()
        if len(parts) <= 19:
            return None

        start_ticks = int(parts[19])
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        return boot_time + (start_ticks / float(ticks_per_second))
    except Exception:
        return None


def _latest_run_activity_time(run_dir: Path) -> float | None:
    """Return the newest mtime inside a W&B run directory."""
    latest: float | None = None

    try:
        for path in run_dir.rglob("*"):
            try:
                if path.is_file():
                    mtime = path.stat().st_mtime
                    latest = mtime if latest is None else max(latest, mtime)
            except Exception:
                continue
    except Exception:
        pass

    if latest is None:
        try:
            latest = run_dir.stat().st_mtime
        except Exception:
            pass
    return latest


def _pid_started_after_run_activity(pid: int, latest_activity: float | None) -> bool:
    """Detect a reused/stale PID that cannot belong to this W&B run."""
    if latest_activity is None:
        return False
    process_start = _process_start_time(pid)
    if process_start is None:
        return False
    return process_start > latest_activity + STALE_PID_GRACE_SECONDS


def _find_wandb_runs(root: Path) -> list[Path]:
    """Find all W&B run directories under *root*."""
    if not root.exists():
        return []

    run_dirs: set[Path] = set()

    try:
        for p in root.rglob("*.wandb"):
            if p.is_file():
                run_dirs.add(p.parent)
    except Exception:
        pass

    try:
        for p in root.rglob("wandb-metadata.json"):
            if p.is_file():
                if p.parent.name == "files":
                    run_dirs.add(p.parent.parent)
                else:
                    run_dirs.add(p.parent)
    except Exception:
        pass

    return sorted(run_dirs)


def _scan_system_for_wandb_dirs(log_path: Path | None) -> list[Path]:
    """Search common filesystem locations for ``wandb/`` directories.

    Avoids a full-root ``find /`` scan (expensive on large filesystems) and
    instead searches:
    - Environment-hinted locations (``WANDB_DIR``, ``WANDB_CACHE_DIR``, etc.)
    - The current user's home directory and common project sub-directories
    - A small set of common ML / data mount points
    """
    _log("Scanning common locations for 'wandb' directories...", log_path)

    candidate_roots: set[Path] = set()

    # 1. Environment hints. Canonicalize each to an absolute path so a relative
    #    or dash-prefixed value (e.g. ``-maxdepth``) can never be interpreted as
    #    a ``find`` option or expression when passed as a path argument below.
    for var in ("WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_DATA_DIR", "WANDB_CONFIG_DIR"):
        value = os.environ.get(var)
        if not value:
            continue
        candidate_roots.add(Path(value).expanduser().resolve(strict=False))

    # 2. Current user's home directory and common project sub-directories
    try:
        home = Path.home()
        candidate_roots.update([home, home / "wandb", home / "work", home / "projects"])
    except Exception:
        pass

    # 3. Start from DEFAULT_SEARCH_ROOTS (restricted set, not '/')
    for root in DEFAULT_SEARCH_ROOTS:
        candidate_roots.add(root)

    found_dirs: set[Path] = set()
    for root in candidate_roots:
        try:
            if not root.exists():
                continue
            # If the root itself is a "wandb" directory, include it
            if root.name == "wandb" and root.is_dir():
                found_dirs.add(root)
                continue
            # Otherwise, use find restricted to this subtree. Pass an absolute,
            # canonicalized path so it cannot be parsed as a find option/expression.
            safe_root = root.resolve(strict=False)
            cmd = ["find", str(safe_root), "-maxdepth", "5", "-name", "wandb", "-type", "d"]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
            for line in result.stdout.splitlines():
                if line.strip():
                    found_dirs.add(Path(line.strip()))
        except Exception:
            pass

    paths = sorted(found_dirs)
    _log(f"Found {len(paths)} 'wandb' directories in common locations.", log_path)
    return paths


def _build_run_info(run_dir: Path, log_path: Path | None) -> RunData | None:
    """Extract structured data from a single W&B run directory."""
    mode = "offline" if run_dir.name.startswith("offline-run-") else "online"
    metadata, summary, config = _parse_run_files(run_dir)
    db_meta: dict[str, Any] = {}
    db_summary: dict[str, Any] = {}
    db_config: dict[str, Any] = {}
    db_history: list[dict[str, Any]] = []
    db_used = False

    db_files = list(run_dir.glob("*.wandb"))
    if db_files:
        db_meta, db_summary, db_config, db_history = _parse_wandb_db(db_files[0], log_path)
        if db_meta or db_summary or db_config or db_history:
            db_used = True

    metadata = {**db_meta, **metadata}
    summary = {**db_summary, **summary}
    config = {**db_config, **config}

    run_id = (
        metadata.get("run_id")
        or metadata.get("id")
        or metadata.get("runId")
        or _derive_run_id_from_dir(run_dir.name)
    )
    run_name = (
        metadata.get("display_name")
        or metadata.get("run_name")
        or metadata.get("name")
        or run_dir.name
    )
    entity = metadata.get("entity") or metadata.get("username") or "unknown"
    project = metadata.get("project") or metadata.get("project_name") or "unknown"
    start_time = (
        metadata.get("start_time")
        or metadata.get("start_time_millis")
        or metadata.get("started_at")
    )
    if not start_time:
        start_time = _derive_start_time_from_dir(run_dir.name)

    end_time = metadata.get("end_time") or metadata.get("finished_at")
    if not end_time and start_time:
        runtime = metadata.get("runtime")
        if runtime is not None:
            try:
                end_time = float(start_time) + float(runtime)
            except (ValueError, TypeError):
                pass

    latest_activity = _latest_run_activity_time(run_dir)

    pid = metadata.get("process_id") or metadata.get("pid")
    if not pid:
        pid = _get_pid_from_logs(run_dir)

    is_running = False
    if pid:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = 0
        is_running = _is_pid_running(pid_int)
        if is_running and _pid_started_after_run_activity(pid_int, latest_activity):
            _log(
                f"Ignoring stale W&B pid {pid_int} for {run_dir.name}; "
                "process started after run files stopped changing.",
                log_path,
            )
            is_running = False

    if not end_time and not is_running:
        end_time = latest_activity

    # Determine status
    if is_running:
        status = "running"
    elif end_time:
        exit_code = metadata.get("exit_code")
        status = "failed" if exit_code and exit_code != 0 else "finished"
    else:
        status = "unknown"

    summary = _resolve_wandb_references(summary, run_dir)
    config = _resolve_wandb_references(config, run_dir)

    metadata = _sanitize_value(metadata)
    summary = _sanitize_value(summary)
    config = _sanitize_value(config)
    db_history = _sanitize_value(db_history)

    script_path = _extract_script_path(metadata, config, db_meta, db_config)
    script_content = None
    if script_path and script_path != "unknown":
        # script_path comes from run metadata/config and is therefore
        # attacker-influenceable. Only resolve it relative to locations
        # *inside* the run directory; reject absolute paths and ``..``
        # escapes so we never read (and ship) arbitrary host files. The W&B
        # copy of the script, when present, lives under files/code or tmp/code.
        candidates: list[Path] = []
        for base in (run_dir, run_dir / "files" / "code"):
            joined = _safe_join(base, script_path)
            if joined is not None:
                candidates.append(joined)

        code_dir = run_dir / "files" / "code"
        if code_dir.exists():
            candidates.extend(code_dir.rglob("*.py"))

        tmp_code_dir = run_dir / "tmp" / "code"
        if tmp_code_dir.exists():
            candidates.extend(tmp_code_dir.rglob("*.py"))

        for p in candidates:
            # rglob() candidates bypass _safe_join, so a symlink inside the run
            # dir could point outside it. Re-check containment after resolving
            # symlinks before reading so we never ship arbitrary host files.
            if not _is_within(p, run_dir):
                continue
            if p.exists() and p.is_file():
                try:
                    if p.name == Path(script_path).name:
                        with p.open("r", encoding="utf-8") as fh:
                            script_content = fh.read()
                        break
                except Exception:
                    pass

    # IMPORTANT: experiment_id must NOT contain a forward slash. The server
    # interpolates it into the R2 key path
    # (`OBSERVABILITY/<user>/<server_ip>/<experiment_id>/...`), so a slash
    # creates a phantom intermediate folder ("local/<project>/...") which
    # then shows up in the UI's experiment listing as a fake "local"
    # experiment. We keep `entity` as a separate field for filtering /
    # display, and use the bare project name as the path-safe identifier.
    exp_id = project

    _log(
        f"Parsed run {run_dir.name} mode={mode} "
        f"metadata={'yes' if metadata else 'no'} summary={'yes' if summary else 'no'} "
        f"config={'yes' if config else 'no'} db_used={'yes' if db_used else 'no'}",
        log_path,
    )

    return {
        "source": "wandb",
        "run_id": run_id,
        "run_name": run_name,
        "experiment_id": exp_id,
        "entity": entity,
        "project": project,
        "status": status,
        "start_time": _sanitize_value(start_time),
        "end_time": _sanitize_value(end_time),
        "config": config,
        "summary": summary,
        "history": db_history,
        "system_metrics": [],
        "tags": [],
        "script_path": script_path,
        "script_content": _sanitize_value(script_content),
        "mode": mode,
        "path": str(run_dir.resolve()),
        "run_index": None,
    }


# ── Scanner class ─────────────────────────────────────────────────────


class WandbScanner(BaseScanner):
    """Discovers and extracts W&B experiment runs from the local filesystem."""

    @property
    def source_name(self) -> str:
        return "wandb"

    def get_dependencies(self) -> list[str]:
        return ["wandb", "pyyaml"]

    def is_available(self) -> bool:
        # Parsing .wandb binary run logs needs not just ``wandb`` but its
        # ``proto`` / ``sdk.internal.datastore`` submodules. Those can fail to
        # import even when the top-level package is present (set to None at
        # module load above). Report unavailable in that case so callers skip
        # this scanner instead of getting silently empty parses.
        return wandb is not None and datastore is not None and wandb_internal_pb2 is not None

    def find_root_dirs(self, search_root: Path | None = None) -> list[Path]:
        if search_root is not None:
            return [search_root] if search_root.exists() else []
        return _scan_system_for_wandb_dirs(log_path=None)

    def discover_runs(
        self,
        root_dirs: list[Path],
        catalog: Catalog,
        log_path: Path | None = None,
    ) -> list[RunData]:
        existing_map = self.build_existing_runs_map(catalog)

        all_run_dirs: set[Path] = set()
        for root in root_dirs:
            all_run_dirs.update(_find_wandb_runs(root))
        run_dirs = sorted(all_run_dirs)

        if not run_dirs:
            _log("No wandb runs found.", log_path)
            return []

        _log(f"Found {len(run_dirs)} W&B run directories.", log_path)

        new_runs: list[RunData] = []
        skipped = 0

        for run_dir in run_dirs:
            derived_id = _derive_run_id_from_dir(run_dir.name)
            if derived_id in existing_map and self.is_finished(existing_map[derived_id]):
                skipped += 1
                continue

            try:
                info = _build_run_info(run_dir, log_path)
                if not info:
                    continue

                if info.get("project") in ("unknown", "uncategorized"):
                    sp = info.get("script_path", "unknown")
                    if sp and sp != "unknown":
                        derived_project = Path(sp).name
                        info["project"] = derived_project
                        # experiment_id mirrors the project for wandb runs and
                        # is the catalog key; keep it in sync so the run is not
                        # grouped under the stale "unknown" experiment. It must
                        # stay slash-free (interpolated into the R2 object key).
                        info["experiment_id"] = derived_project

                if info["run_id"] in existing_map and self.is_finished(existing_map[info["run_id"]]):
                    skipped += 1
                    continue

                new_runs.append(info)
            except Exception as exc:
                _log(f"Failed to parse run {run_dir.name}: {exc}", log_path)

        _log(f"W&B scanner: {len(new_runs)} new, {skipped} skipped.", log_path)
        return new_runs
