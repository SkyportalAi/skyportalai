"""Tests for the ported experiment scanner adapters.

Ported from skyportal-website ``tests/test_observability_scanners.py`` —
the scanners are copied near-verbatim, so these tests pin the same behavior.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skyportalai.agent.scrapers.base_scanner import DEFAULT_SEARCH_ROOTS, BaseScanner, iso_now
from skyportalai.agent.scrapers.mlflow_scanner import MlflowScanner
from skyportalai.agent.scrapers.wandb_scanner import WandbScanner, _is_within, _safe_join


class TestBaseScanner(unittest.TestCase):
    def test_iso_now_returns_iso_format(self):
        ts = iso_now()
        self.assertIn("T", ts)
        self.assertTrue(ts.endswith("+00:00"))

    def test_build_existing_runs_map(self):
        catalog = {
            "experiments": [
                {"id": "exp1", "runs": [{"run_id": "r1"}, {"run_id": "r2"}]},
                {"id": "exp2", "runs": [{"run_id": "r3"}]},
            ]
        }
        runs_map = BaseScanner.build_existing_runs_map(catalog)
        self.assertEqual(set(runs_map.keys()), {"r1", "r2", "r3"})

    def test_build_existing_runs_map_empty_catalog(self):
        runs_map = BaseScanner.build_existing_runs_map({"experiments": []})
        self.assertEqual(runs_map, {})

    def test_is_finished_true(self):
        self.assertTrue(BaseScanner.is_finished({"end_time": 1234567890.0}))

    def test_is_finished_false_none(self):
        self.assertFalse(BaseScanner.is_finished({"end_time": None}))

    def test_is_finished_false_string_none(self):
        self.assertFalse(BaseScanner.is_finished({"end_time": "None"}))


class TestWandbScanner(unittest.TestCase):
    def setUp(self):
        self.scanner = WandbScanner()

    def test_source_name(self):
        self.assertEqual(self.scanner.source_name, "wandb")

    def test_get_dependencies(self):
        deps = self.scanner.get_dependencies()
        self.assertIn("wandb", deps)
        self.assertIn("pyyaml", deps)

    def test_find_root_dirs_nonexistent(self):
        dirs = self.scanner.find_root_dirs(Path("/nonexistent/path"))
        self.assertEqual(dirs, [])

    def test_discover_runs_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runs = self.scanner.discover_runs([Path(tmpdir)], {"experiments": []})
            self.assertEqual(runs, [])

    def test_discover_runs_with_metadata_file(self):
        """Create a minimal W&B run structure and verify it's discovered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-20240101_120000-abc123"
            files_dir = run_dir / "files"
            files_dir.mkdir(parents=True)

            metadata = {
                "run_id": "abc123",
                "project": "test-project",
                "entity": "test-user",
                "display_name": "test-run",
                "start_time": 1704110400.0,
            }
            (files_dir / "wandb-metadata.json").write_text(json.dumps(metadata))

            runs = self.scanner.discover_runs([Path(tmpdir)], {"experiments": []})
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["source"], "wandb")
            self.assertEqual(runs[0]["run_id"], "abc123")
            self.assertEqual(runs[0]["project"], "test-project")
            self.assertEqual(runs[0]["entity"], "test-user")

    def test_discover_runs_skips_finished(self):
        """Runs already in the catalog with an end_time should be skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-20240101_120000-abc123"
            files_dir = run_dir / "files"
            files_dir.mkdir(parents=True)

            metadata = {"run_id": "abc123", "project": "p", "entity": "u"}
            (files_dir / "wandb-metadata.json").write_text(json.dumps(metadata))

            catalog = {
                "experiments": [
                    {
                        "id": "u/p",
                        "runs": [{"run_id": "abc123", "end_time": 1704200000.0}],
                    }
                ]
            }
            runs = self.scanner.discover_runs([Path(tmpdir)], catalog)
            self.assertEqual(len(runs), 0)

    def test_inferred_project_syncs_experiment_id(self):
        """When project is inferred from script_path, experiment_id must follow.

        The catalog keys off experiment_id, so leaving it as the stale "unknown"
        would group an otherwise-usable run under the wrong experiment.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-20240101_120000-abc123"
            files_dir = run_dir / "files"
            files_dir.mkdir(parents=True)

            # No project key -> defaults to "unknown"; program supplies the
            # script path the scanner infers the project name from.
            metadata = {"run_id": "abc123", "entity": "u", "program": "train_model.py"}
            (files_dir / "wandb-metadata.json").write_text(json.dumps(metadata))

            runs = self.scanner.discover_runs([Path(tmpdir)], {"experiments": []})
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["project"], "train_model.py")
            self.assertEqual(runs[0]["experiment_id"], "train_model.py")
            self.assertNotEqual(runs[0]["experiment_id"], "unknown")


class TestMlflowScanner(unittest.TestCase):
    def setUp(self):
        self.scanner = MlflowScanner()

    def test_source_name(self):
        self.assertEqual(self.scanner.source_name, "mlflow")

    def test_get_dependencies(self):
        deps = self.scanner.get_dependencies()
        self.assertIn("pyyaml", deps)
        # No mlflow package required
        self.assertNotIn("mlflow", deps)

    def test_find_root_dirs_nonexistent(self):
        dirs = self.scanner.find_root_dirs(Path("/nonexistent/path"))
        self.assertEqual(dirs, [])

    def test_discover_runs_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runs = self.scanner.discover_runs([Path(tmpdir)], {"experiments": []})
            self.assertEqual(runs, [])

    def test_discover_runs_with_mlflow_structure(self):
        """Create a minimal mlruns/ structure and verify it's discovered."""
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")

        with tempfile.TemporaryDirectory() as tmpdir:
            mlruns = Path(tmpdir)
            exp_dir = mlruns / "0"
            run_dir = exp_dir / "abc123def456"

            exp_dir.mkdir(parents=True)
            with (exp_dir / "meta.yaml").open("w") as f:
                yaml.dump({
                    "experiment_id": "0",
                    "name": "Default",
                    "lifecycle_stage": "active",
                }, f)

            run_dir.mkdir()
            with (run_dir / "meta.yaml").open("w") as f:
                yaml.dump({
                    "run_id": "abc123def456",
                    "experiment_id": "0",
                    "status": 3,  # FINISHED
                    "start_time": 1704110400000,
                    "end_time": 1704114000000,
                    "lifecycle_stage": "active",
                    "run_name": "test-run",
                }, f)

            params_dir = run_dir / "params"
            params_dir.mkdir()
            (params_dir / "learning_rate").write_text("0.001")
            (params_dir / "epochs").write_text("10")

            metrics_dir = run_dir / "metrics"
            metrics_dir.mkdir()
            (metrics_dir / "loss").write_text(
                "1704110400000 0.85 0\n"
                "1704110500000 0.42 1\n"
                "1704110600000 0.21 2\n"
            )
            (metrics_dir / "accuracy").write_text(
                "1704110400000 0.65 0\n"
                "1704110500000 0.82 1\n"
                "1704110600000 0.91 2\n"
            )

            tags_dir = run_dir / "tags"
            tags_dir.mkdir()
            (tags_dir / "mlflow.runName").write_text("test-run")
            (tags_dir / "mlflow.source.name").write_text("train.py")

            runs = self.scanner.discover_runs([mlruns], {"experiments": []})
            self.assertEqual(len(runs), 1)

            run = runs[0]
            self.assertEqual(run["source"], "mlflow")
            self.assertEqual(run["run_id"], "abc123def456")
            self.assertEqual(run["run_name"], "test-run")
            self.assertEqual(run["status"], "finished")
            self.assertEqual(run["config"]["learning_rate"], "0.001")
            self.assertEqual(run["config"]["epochs"], "10")
            self.assertEqual(run["summary"]["loss"], 0.21)
            self.assertEqual(run["summary"]["accuracy"], 0.91)
            self.assertEqual(len(run["history"]), 3)
            self.assertEqual(run["script_path"], "train.py")
            self.assertEqual(run["tags"]["mlflow.runName"], "test-run")

    def test_experiment_id_is_slash_free(self):
        """experiment_id is interpolated into the R2 key, so it must stay slash-free."""
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")

        with tempfile.TemporaryDirectory() as tmpdir:
            mlruns = Path(tmpdir)
            exp_dir = mlruns / "0"
            run_dir = exp_dir / "run1"

            exp_dir.mkdir(parents=True)
            with (exp_dir / "meta.yaml").open("w") as f:
                yaml.dump({"experiment_id": "0", "name": "team/project"}, f)

            run_dir.mkdir()
            with (run_dir / "meta.yaml").open("w") as f:
                yaml.dump({
                    "run_id": "run1",
                    "status": 3,
                    "lifecycle_stage": "active",
                }, f)

            runs = self.scanner.discover_runs([mlruns], {"experiments": []})
            self.assertEqual(len(runs), 1)
            self.assertNotIn("/", runs[0]["experiment_id"])
            self.assertEqual(runs[0]["project"], "team/project")

    def test_discover_runs_skips_deleted(self):
        """Runs with lifecycle_stage='deleted' should be skipped."""
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")

        with tempfile.TemporaryDirectory() as tmpdir:
            mlruns = Path(tmpdir)
            exp_dir = mlruns / "0"
            run_dir = exp_dir / "deleted_run"

            exp_dir.mkdir(parents=True)
            with (exp_dir / "meta.yaml").open("w") as f:
                yaml.dump({"experiment_id": "0", "name": "Default"}, f)

            run_dir.mkdir()
            with (run_dir / "meta.yaml").open("w") as f:
                yaml.dump({
                    "run_id": "deleted_run",
                    "status": 3,
                    "lifecycle_stage": "deleted",
                }, f)

            runs = self.scanner.discover_runs([mlruns], {"experiments": []})
            self.assertEqual(len(runs), 0)

    def test_status_mapping(self):
        """Verify MLflow integer status codes map correctly."""
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")

        status_cases = [
            (1, "running"),
            (3, "finished"),
            (4, "failed"),
            (5, "killed"),
        ]

        for status_int, expected in status_cases:
            with tempfile.TemporaryDirectory() as tmpdir:
                mlruns = Path(tmpdir)
                exp_dir = mlruns / "0"
                run_dir = exp_dir / f"run_{status_int}"

                exp_dir.mkdir(parents=True)
                with (exp_dir / "meta.yaml").open("w") as f:
                    yaml.dump({"experiment_id": "0", "name": "Default"}, f)

                run_dir.mkdir()
                with (run_dir / "meta.yaml").open("w") as f:
                    yaml.dump({
                        "run_id": f"run_{status_int}",
                        "status": status_int,
                        "lifecycle_stage": "active",
                    }, f)

                runs = self.scanner.discover_runs([mlruns], {"experiments": []})
                self.assertEqual(len(runs), 1, f"status={status_int}")
                self.assertEqual(runs[0]["status"], expected, f"status={status_int}")


class TestSafeJoin(unittest.TestCase):
    """_safe_join must contain attacker-influenceable paths inside the base dir."""

    def test_relative_path_inside_base_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            result = _safe_join(base, "sub/file.json")
            self.assertEqual(result, (base / "sub" / "file.json").resolve())

    def test_absolute_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(_safe_join(Path(tmpdir), "/etc/passwd"))

    def test_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "run"
            base.mkdir()
            self.assertIsNone(_safe_join(base, "../../../etc/passwd"))

    def test_empty_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(_safe_join(Path(tmpdir), ""))

    def test_symlink_escaping_base_is_rejected(self):
        """A symlink inside the run dir that points outside must be rejected.

        rglob() can surface such a symlink, bypassing _safe_join, so the read
        site re-checks containment via _is_within after resolving symlinks.
        """
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            run_dir = Path(tmpdir) / "run"
            (run_dir / "files" / "code").mkdir(parents=True)
            secret = Path(outside) / "passwd"
            secret.write_text("root:x:0:0")
            link = run_dir / "files" / "code" / "leak.py"
            try:
                link.symlink_to(secret)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported on this platform")
            # The symlink resolves outside run_dir, so it must not be allowed.
            self.assertFalse(_is_within(link, run_dir))
            # A real file inside the run dir is allowed.
            inside = run_dir / "files" / "code" / "real.py"
            inside.write_text("print('hi')")
            self.assertTrue(_is_within(inside, run_dir))


def _capture_find_calls(module, env: dict[str, str]) -> list[list[str]]:
    """Run a scanner's system-scan with mocked env/fs and capture find argv."""
    seen: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        seen.append(cmd)
        return SimpleNamespace(stdout="")

    scan = getattr(module, "_scan_system_for_mlruns_dirs", None) or module._scan_system_for_wandb_dirs
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(module.Path, "home", return_value=Path("/home/none")),
        patch.object(module.Path, "exists", autospec=True, return_value=True),
        patch.object(module.Path, "is_dir", autospec=True, return_value=False),
        patch.object(module.subprocess, "run", side_effect=fake_run),
    ):
        scan(log_path=None)
    return seen


class TestMlflowEnvHints(unittest.TestCase):
    """File-URI MLflow env hints must be recognised as local roots."""

    def test_file_uri_tracking_uri_becomes_find_root(self):
        # file:///data/mlruns must be scanned as the local path /data/mlruns.
        from skyportalai.agent.scrapers import mlflow_scanner as m

        roots = {cmd[1] for cmd in _capture_find_calls(m, {"MLFLOW_TRACKING_URI": "file:///data/mlruns"})}
        self.assertIn("/data/mlruns", roots)

    def test_non_local_scheme_is_not_a_find_root(self):
        # http/databricks/s3 tracking URIs have no local subtree to scan.
        from skyportalai.agent.scrapers import mlflow_scanner as m

        roots = {cmd[1] for cmd in _capture_find_calls(m, {"MLFLOW_TRACKING_URI": "http://mlflow.example/"})}
        self.assertNotIn("http://mlflow.example/", roots)
        self.assertNotIn("mlflow.example", roots)


class TestWandbFindHardening(unittest.TestCase):
    """find paths derived from WANDB_* env vars must be absolute, not options."""

    def test_dash_prefixed_env_root_is_canonicalized(self):
        # A value like "-maxdepth" must resolve to an absolute path so it can
        # never be parsed by find as an option/expression.
        resolved = Path("-maxdepth").expanduser().resolve(strict=False)
        self.assertTrue(str(resolved).startswith("/"))

    def test_find_invocation_always_uses_absolute_path(self):
        from skyportalai.agent.scrapers import wandb_scanner as w

        seen = _capture_find_calls(w, {"WANDB_DIR": "-maxdepth"})
        self.assertTrue(seen, "find was never invoked")
        for cmd in seen:
            # cmd == ["find", <path>, "-maxdepth", ...]; the path arg is index 1.
            self.assertTrue(cmd[1].startswith("/"), cmd)


class TestDefaultSearchRoots(unittest.TestCase):
    def test_default_search_roots_does_not_include_root(self):
        """DEFAULT_SEARCH_ROOTS must NOT include '/' to prevent full-filesystem scans."""
        self.assertNotIn(Path("/"), DEFAULT_SEARCH_ROOTS)

    def test_default_search_roots_includes_home(self):
        home = Path.home()
        self.assertIn(home, DEFAULT_SEARCH_ROOTS)

    def test_default_search_roots_without_home(self):
        """DEFAULT_SEARCH_ROOTS builder should not crash when HOME is unset."""
        from skyportalai.agent.scrapers.base_scanner import _default_search_roots

        with patch.object(Path, "home", side_effect=RuntimeError("no HOME")):
            roots = _default_search_roots()
        self.assertNotIn(Path("/"), roots)
        self.assertIn(Path("/home"), roots)
        self.assertIn(Path("/data"), roots)


if __name__ == "__main__":
    unittest.main()
