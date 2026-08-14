from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / ".ai" / "engine"))
import ai_kit  # noqa: E402


def ns(**values):
    defaults = {"state": None}
    defaults.update(values)
    return argparse.Namespace(**defaults)


class ScaffoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = {name: getattr(ai_kit, name) for name in ("ROOT", "WORK", "STATE", "CURRENT", "EVENT_LOG", "VISUALIZER_DIR", "AUTO_ARTIFACT_GENERATION")}
        ai_kit.ROOT = self.root
        ai_kit.WORK = self.root / ".ai-work"
        ai_kit.STATE = ai_kit.WORK / "state" / "workflow.json"
        ai_kit.CURRENT = ai_kit.WORK / "state" / "current.json"
        ai_kit.EVENT_LOG = ai_kit.WORK / "logs" / "events.jsonl"
        ai_kit.VISUALIZER_DIR = self.root / ".visualizer"
        ai_kit.AUTO_ARTIFACT_GENERATION = False
        shutil.copytree(REPO_ROOT / ".ai" / "install", self.root / ".ai" / "install")
        (self.root / ".ai" / "memory").mkdir(parents=True)

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(ai_kit, name, value)
        self.tmp.cleanup()

    def test_minimal_scaffold_adds_human_companions_without_a_second_truth_registry(self) -> None:
        result = ai_kit.cmd_scaffold(ns(profile="minimal", force=False))
        self.assertTrue(result["architecture_valid"])
        self.assertTrue((self.root / "architecture" / "VERSION.yaml").is_file())
        self.assertTrue((self.root / "architecture" / "ADR" / "ADR-001-independent-boundaries.md").is_file())
        self.assertFalse((self.root / "architecture" / "truth.yaml").exists())
        self.assertEqual(result["authority"]["truth_registry"], ".ai-config/truth.yaml")

    def test_store_pilot_seeds_contracts_contexts_generated_sdk_and_executable_boundaries(self) -> None:
        result = ai_kit.cmd_scaffold(ns(profile="store-pilot", force=False))
        self.assertTrue(result["architecture_valid"])
        self.assertEqual(result["contexts"], ["backend", "frontend", "worker"])
        self.assertTrue((self.root / "contracts" / "generated" / "sdk" / "contracts.ts").is_file())
        self.assertTrue((self.root / "contracts" / "generated" / "sdk" / "mocks.ts").is_file())
        self.assertTrue((self.root / "backend" / "application" / "create_store.py").is_file())
        self.assertTrue((self.root / "worker" / "store_lifecycle.py").is_file())
        registry = ai_kit._load_contract_registry()
        self.assertEqual(set(registry["contracts"]), {"store-api", "store-lifecycle"})
        self.assertTrue(ai_kit.cmd_architecture_validate(ns())["passed"])

    def test_store_pilot_refuses_to_replace_existing_config_without_force(self) -> None:
        ai_kit.cmd_scaffold(ns(profile="store-pilot", force=False))
        with self.assertRaisesRegex(ai_kit.EngineError, "empty context"):
            ai_kit.cmd_scaffold(ns(profile="store-pilot", force=False))
        self.assertTrue(ai_kit.cmd_scaffold(ns(profile="store-pilot", force=True))["architecture_valid"])


if __name__ == "__main__":
    unittest.main()
