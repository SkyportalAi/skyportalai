"""Tests for the ported catalog persistence + run-diffing helpers.

Ported from skyportal-website ``tests/test_observability_scanners.py``
(``TestMainEntryPoint`` catalog cases). The run loop / CLI that also lived
in ``main.py`` is deferred to P1 (Reliability).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from skyportalai.agent.catalog import (
    assign_run_indices,
    load_catalog,
    save_catalog,
    upsert_runs,
)


class TestCatalog(unittest.TestCase):
    def test_load_catalog_missing_file(self):
        catalog = load_catalog(Path("/nonexistent/catalog.json"))
        self.assertIsNone(catalog.get("last_updated"))
        self.assertEqual(catalog["experiments"], [])

    def test_save_and_load_catalog(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "catalog.json"
            catalog = {"last_updated": "2024-01-01", "experiments": [{"id": "e1", "runs": []}]}
            save_catalog(path, catalog)

            loaded = load_catalog(path)
            self.assertEqual(loaded["last_updated"], "2024-01-01")
            self.assertEqual(len(loaded["experiments"]), 1)

    def test_save_catalog_leaves_no_temp_file(self):
        # Atomic write goes through a sibling ``.tmp`` file that must be renamed
        # away on success, never left behind.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "catalog.json"
            save_catalog(path, {"last_updated": None, "experiments": []})
            self.assertTrue(path.exists())
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_load_catalog_malformed_json_raises(self):
        # A corrupt catalog must surface loudly rather than silently resetting
        # state (which would re-ship every run on the next pass).
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "catalog.json"
            path.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(path)

    def test_upsert_runs_new_experiment(self):
        catalog = {"experiments": []}
        # experiment_id must be slash-free (it is interpolated into the R2 object
        # key) -- use a slash-free id in the fixture to keep the invariant.
        runs = [{"run_id": "r1", "experiment_id": "exp_proj", "entity": "u", "project": "p", "source": "wandb"}]
        result = upsert_runs(catalog, runs)
        self.assertEqual(len(result["experiments"]), 1)
        self.assertEqual(result["experiments"][0]["id"], "exp_proj")
        self.assertEqual(len(result["experiments"][0]["runs"]), 1)

    def test_upsert_runs_updates_existing(self):
        catalog = {
            "experiments": [
                {"id": "exp_proj", "entity": "u", "project": "p", "runs": [{"run_id": "r1", "status": "running"}]}
            ]
        }
        runs = [{"run_id": "r1", "experiment_id": "exp_proj", "status": "finished", "source": "wandb"}]
        result = upsert_runs(catalog, runs)
        self.assertEqual(len(result["experiments"]), 1)
        self.assertEqual(len(result["experiments"][0]["runs"]), 1)
        self.assertEqual(result["experiments"][0]["runs"][0]["status"], "finished")

    def test_assign_run_indices(self):
        catalog = {
            "experiments": [
                {"id": "exp1", "runs": [{"run_id": "existing1"}, {"run_id": "existing2"}]}
            ]
        }
        new_runs = [
            {"run_id": "new1", "experiment_id": "exp1"},
            {"run_id": "new2", "experiment_id": "exp1"},
            {"run_id": "new3", "experiment_id": "exp2"},
        ]
        assign_run_indices(catalog, new_runs)
        self.assertEqual(new_runs[0]["run_index"], 2)  # after 2 existing
        self.assertEqual(new_runs[1]["run_index"], 3)
        self.assertEqual(new_runs[2]["run_index"], 0)  # new experiment


if __name__ == "__main__":
    unittest.main()
