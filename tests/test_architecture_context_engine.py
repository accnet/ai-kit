from __future__ import annotations

import argparse
import json
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


class ArchitectureContextEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = {
            name: getattr(ai_kit, name)
            for name in ("ROOT", "WORK", "STATE", "CURRENT", "EVENT_LOG", "VISUALIZER_DIR", "AUTO_ARTIFACT_GENERATION")
        }
        ai_kit.ROOT = self.root
        ai_kit.WORK = self.root / ".ai-work"
        ai_kit.STATE = ai_kit.WORK / "state" / "workflow.json"
        ai_kit.CURRENT = ai_kit.WORK / "state" / "current.json"
        ai_kit.EVENT_LOG = ai_kit.WORK / "logs" / "events.jsonl"
        ai_kit.VISUALIZER_DIR = self.root / ".visualizer"
        ai_kit.AUTO_ARTIFACT_GENERATION = False
        shutil.copytree(REPO_ROOT / ".ai" / "install" / "config", self.root / ".ai" / "install" / "config")
        (self.root / ".ai" / "memory").mkdir(parents=True)
        (self.root / "src" / "orders").mkdir(parents=True)
        (self.root / "src" / "shared").mkdir(parents=True)
        (self.root / "src" / "orders" / "service.py").write_text("def create_order(): pass\n", encoding="utf-8")
        (self.root / "src" / "shared" / "money.py").write_text("class Money: pass\n", encoding="utf-8")
        (self.root / ".ai" / "install" / "config" / "contexts.yaml").write_text(
            "contexts:\n"
            "  orders:\n"
            "    path: src/orders/*\n"
            "    owner: backend\n"
            "    revision: 1\n"
            "    depends_on: [\"shared\"]\n"
            "  shared:\n"
            "    path: src/shared/*\n"
            "    owner: backend\n"
            "    revision: 1\n",
            encoding="utf-8",
        )
        architecture = {
            "schema_version": 1,
            "systems": [{"id": "system:shop", "name": "Shop"}],
            "external_systems": [],
            "containers": [{"id": "container:api", "name": "API", "system_ref": "system:shop"}],
            "context_mappings": {"orders": "container:api", "shared": "container:api"},
            "relationships": [],
            "profiles": {
                "default": {"domain": "simple", "organization": "layered", "dependency": "none", "deployment": "monolith"},
                "orders": {"domain": "ddd", "organization": "vertical-slice", "dependency": "hexagonal", "deployment": "service"},
            },
        }
        (self.root / ".ai" / "install" / "config" / "architecture.json").write_text(json.dumps(architecture), encoding="utf-8")
        contract_path = self.root / "contracts" / "orders.json"
        contract_path.parent.mkdir()
        contract_path.write_text('{"type":"object"}\n', encoding="utf-8")
        registry = {
            "schema_version": 1,
            "contracts": {
                "orders": {
                    "owner": "backend",
                    "kind": "api",
                    "represents": "orders",
                    "versions": {
                        "1.0.0": {
                            "path": "contracts/orders.json",
                            "status": "approved",
                            "content_hash": "fixture",
                            "compatibility": "backward-compatible",
                            "supersedes": None,
                            "generators": [],
                            "generated_output_hashes": {},
                            "lifecycle_history": [],
                        }
                    },
                }
            },
        }
        (self.root / ".ai" / "install" / "config" / "contracts.json").write_text(json.dumps(registry), encoding="utf-8")

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(ai_kit, name, value)
        self.tmp.cleanup()

    def test_truth_registry_resolves_template_fallback_without_becoming_authority(self) -> None:
        result = ai_kit.cmd_truth_resolve(ns(topic="architecture"))
        self.assertTrue(result["canonical"])
        self.assertTrue(result["exists"])
        self.assertEqual(result["authority"], ".ai-config/architecture.json")
        self.assertEqual(result["resolved_path"], ".ai/install/config/architecture.json")

    def test_truth_registry_rejects_authority_outside_project(self) -> None:
        (self.root / ".ai" / "install" / "config" / "truth.yaml").write_text(
            "schema_version: 1\ntopics:\n  bad:\n    authority: ../secret\n    kind: source\n    required: true\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ai_kit.EngineError, "inside the project"):
            ai_kit._load_truth_registry()

    def test_architecture_validate_and_inspect_profiles(self) -> None:
        validation = ai_kit.cmd_architecture_validate(ns())
        self.assertTrue(validation["passed"], validation["checks"])
        inspected = ai_kit.cmd_architecture_inspect(ns())
        orders = next(item for item in inspected["c4"]["components"] if item["name"] == "orders")
        self.assertEqual(orders["profile"]["dependency"], "hexagonal")
        self.assertEqual(set(inspected["c4"]["levels"]), {"1", "2", "3"})

    def test_architecture_validate_rejects_unknown_profile_value(self) -> None:
        path = self.root / ".ai" / "install" / "config" / "architecture.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["profiles"]["orders"]["dependency"] = "spaghetti"
        path.write_text(json.dumps(config), encoding="utf-8")
        result = ai_kit.cmd_architecture_validate(ns())
        self.assertFalse(result["passed"])
        self.assertTrue(any(item["id"].endswith(":dependency") and not item["passed"] for item in result["checks"]))

    def test_context_package_selects_direct_dependency_contract_and_governance(self) -> None:
        task = {
            "id": "T1",
            "title": "Implement orders API contract",
            "owner": "backend",
            "task_kind": "implementation",
            "tags": ["api"],
            "context": "orders",
            "files": ["src/orders/service.py"],
            "contract_refs": [{"id": "orders", "version": "1.0.0", "relation": "implements"}],
        }
        package = ai_kit._resolve_context_package(
            task["title"], task=task, state_file=ai_kit.STATE, explain=True
        )
        paths = {item["path"] for item in package["references"]}
        self.assertEqual(package["max_level"], 3)
        self.assertEqual(package["contexts"]["direct"], ["orders"])
        self.assertEqual(package["contexts"]["dependencies"], ["shared"])
        self.assertIn("src/orders/service.py", paths)
        self.assertIn("src/shared/*", paths)
        self.assertIn("contracts/orders.json", paths)
        self.assertIn(".ai/install/config/architecture-fitness.json", paths)
        self.assertGreater(package["metrics"]["estimated_tokens"], 0)
        self.assertEqual(package["principle"], "minimum-sufficient-context")

    def test_free_text_context_resolution_works_without_workflow(self) -> None:
        result = ai_kit.cmd_context_resolve(ns(query="change orders calculation", task=None, level=2, explain=True))
        self.assertEqual(result["task"], None)
        self.assertEqual(result["contexts"]["direct"], ["orders"])
        self.assertEqual(result["max_level"], 2)
        self.assertIn("orders", result["token_matches"])

    def test_bootstrap_exception_exposes_only_source_root_when_context_registry_is_empty(self) -> None:
        (self.root / ".ai" / "install" / "config" / "contexts.yaml").write_text("contexts:\n", encoding="utf-8")
        result = ai_kit.cmd_context_resolve(ns(query="establish the first boundary", task=None, level=2, explain=True))
        self.assertTrue(result["bootstrap"]["active"])
        self.assertEqual(result["bootstrap"]["source_roots"], ["src"])
        self.assertIn("first boundaries", result["bootstrap"]["reason"])
        roots = [item for item in result["references"] if item["source_kind"] == "bootstrap-source-root"]
        self.assertEqual([item["path"] for item in roots], ["src"])
        self.assertFalse(any("service.py" in item["path"] for item in roots))

    def test_parser_exposes_truth_architecture_and_context_commands(self) -> None:
        parser = ai_kit.parser()
        self.assertIs(parser.parse_args(["truth", "resolve", "api"]).fn, ai_kit.cmd_truth_resolve)
        self.assertIs(parser.parse_args(["architecture", "validate"]).fn, ai_kit.cmd_architecture_validate)
        self.assertIs(parser.parse_args(["architecture", "inspect"]).fn, ai_kit.cmd_architecture_inspect)
        explained = parser.parse_args(["context", "explain", "orders"])
        self.assertIs(explained.fn, ai_kit.cmd_context_resolve)
        self.assertTrue(explained.explain)
        self.assertIs(parser.parse_args(["scaffold", "minimal"]).fn, ai_kit.cmd_scaffold)


if __name__ == "__main__":
    unittest.main()
