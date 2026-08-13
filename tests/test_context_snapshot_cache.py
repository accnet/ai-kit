"""Coverage for the bounded, incremental project-context snapshot cache."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = ROOT / ".ai" / "engine" / "ai_kit.py"
SPEC = importlib.util.spec_from_file_location("ai_kit_context_snapshot", ENGINE_PATH)
assert SPEC and SPEC.loader
ENGINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENGINE)


@unittest.skipIf(os.name == "nt", "temporary Git repositories are unreliable on the Windows runner")
class ProjectContextSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / ".ai-config").mkdir()
        (self.root / "src").mkdir()
        (self.root / ".ai-config" / "kit.yaml").write_text(
            "project:\n  stack: [node]\n  source_dirs: [src]\n",
            encoding="utf-8",
        )
        (self.root / ".ai-config" / "contexts.yaml").write_text(
            "contexts:\n  app:\n    path: src/**\n    owner: backend\n",
            encoding="utf-8",
        )
        (self.root / "package.json").write_text('{"name":"context-cache"}\n', encoding="utf-8")
        (self.root / "src" / "app.js").write_text("export const version = 1;\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "AI-Kit test")
        self._git("add", ".")
        self._git("commit", "-qm", "initial context")
        self.state = Path(self.temp.name) / "work" / "state" / "workflow.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    def _analyze(self, *, refresh: bool = False) -> dict:
        args = argparse.Namespace(state=str(self.state), refresh=refresh)
        with patch.object(ENGINE, "ROOT", self.root):
            return ENGINE.cmd_analyze(args)

    def test_cache_hit_and_tracked_source_or_config_change_refreshes_snapshot(self) -> None:
        first = self._analyze()
        self.assertEqual(first["cache"]["status"], "refreshed")
        snapshot = self.state.parent.parent / "analysis" / "project-summary.json"
        self.assertTrue(snapshot.exists())

        second = self._analyze()
        self.assertEqual(second["cache"]["status"], "hit")
        self.assertEqual(first["context_snapshot"]["fingerprint"], second["context_snapshot"]["fingerprint"])

        (self.root / "src" / "app.js").write_text("export const version = 2;\n", encoding="utf-8")
        changed_source = self._analyze()
        self.assertEqual(changed_source["cache"]["status"], "refreshed")
        self.assertNotEqual(second["context_snapshot"]["fingerprint"], changed_source["context_snapshot"]["fingerprint"])

        previous_config_hash = changed_source["context_snapshot"]["inputs"]["files"][".ai-config/kit.yaml"]
        (self.root / ".ai-config" / "kit.yaml").write_text(
            "project:\n  stack: [node, typescript]\n  source_dirs: [src]\n",
            encoding="utf-8",
        )
        changed_config = self._analyze()
        self.assertEqual(changed_config["cache"]["status"], "refreshed")
        self.assertNotEqual(previous_config_hash, changed_config["context_snapshot"]["inputs"]["files"][".ai-config/kit.yaml"])

    def test_route_adds_project_snapshot_to_minimal_context_and_reuses_it(self) -> None:
        state = Path(self.temp.name) / "route" / "state" / "workflow.json"
        workflow = ENGINE.new_state("Route context", "feature")
        workflow["tasks"].append({
            "id": "T1", "title": "Route a cached context", "owner": "backend", "phase": "build",
            "needs": [], "status": "todo", "acceptance": ["Route receives a snapshot"], "files": [], "tags": [],
            "attempts": 0, "evidence": [], "blocked_reason": None, "claimed_by": None,
            "context": None, "epic": None, "base_commit": None, "context_revision": None,
            "epic_revision": None, "upstream_context_revisions": {}, "depends_on": [], "contract_hashes": {},
            "contract_revision": None, "contract_hash": None, "superseded_by": None,
        })
        ENGINE.sync_phases(workflow)
        ENGINE.save(workflow, state)

        route_args = argparse.Namespace(state=str(state), id="T1", explain=False)
        first = ENGINE.cmd_route(route_args)
        self.assertEqual(first["project_context"]["cache_status"], "refreshed")
        self.assertEqual(first["context"][0], first["project_context"]["path"])
        self.assertTrue(Path(first["project_context"]["path"]).exists())

        second = ENGINE.cmd_route(route_args)
        self.assertEqual(second["project_context"]["cache_status"], "hit")


if __name__ == "__main__":
    unittest.main()
