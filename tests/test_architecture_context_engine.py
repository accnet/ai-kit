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
        contract_path.write_text(json.dumps({
            "schema_version": 2,
            "source_format": "openapi",
            "source": "orders.yaml",
            "source_hash": "fixture-source",
            "semantic_coverage": ["auth", "errors", "operations", "request-bodies", "responses", "schemas"],
            "security_schemes": [],
            "definitions": [{
                "name": "Order",
                "fields": [
                    {"name": "id", "type": "string", "required": True},
                    {"name": "status", "type": "string", "required": True},
                ],
            }],
            "operations": [{
                "id": "createOrder", "method": "POST", "path": "/orders",
                "request": {"required": True, "content": [{"media_type": "application/json", "schema": {"ref": "#/components/schemas/Order"}}]},
                "responses": [{"status": "201", "category": "success", "content": [{"media_type": "application/json", "schema": {"ref": "#/components/schemas/Order"}}]}],
            }],
            "events": [],
            "models": [],
        }) + "\n", encoding="utf-8")
        generated_path = self.root / "src" / "generated" / "orders.ts"
        generated_path.parent.mkdir(parents=True)
        generated_path.write_text("export interface Order { id: string; status: string }\n", encoding="utf-8")
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
                            "generators": [{"name": "typescript", "outputs": ["src/generated/orders.ts"], "verify_command": None}],
                            "generated_output_hashes": {"src/generated/orders.ts": "fixture-output"},
                            "lifecycle_history": [],
                            "import": {"format": "openapi", "source": "orders.yaml", "source_hash": "fixture-source"},
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
        self.assertIn("src/generated/orders.ts", paths)
        self.assertIn(".ai/install/config/architecture-fitness.json", paths)
        self.assertGreater(package["metrics"]["estimated_tokens"], 0)
        self.assertEqual(package["schema_version"], 3)
        self.assertEqual(package["contract_impact"]["roots"], ["contract:orders@1.0.0"])
        self.assertTrue({"operation", "schema", "field", "generated-output"}.issubset(
            {item["type"] for item in package["contract_impact"]["nodes"]}
        ))
        self.assertGreater(package["metrics"]["impact_relations"], 0)
        self.assertTrue(package["symbol_context"]["source_fingerprint"])
        self.assertTrue(any(item["qualified_name"] == "create_order" for item in package["symbol_context"]["symbols"]))
        self.assertEqual(package["principle"], "minimum-sufficient-context")

    def test_free_text_contract_entity_resolution_returns_only_matching_branch(self) -> None:
        package = ai_kit.cmd_context_resolve(ns(
            query="change createOrder status response", task=None, level=2, explain=True
        ))
        impact = package["contract_impact"]
        self.assertEqual(impact["roots"], ["contract:orders@1.0.0"])
        self.assertTrue(any(item["type"] == "operation" and item["label"] == "createOrder" for item in impact["nodes"]))
        self.assertTrue(any(item["type"] == "field" and item["label"] == "status" for item in impact["nodes"]))
        self.assertTrue(any(item["source_kind"] == "generated-contract-output" for item in package["references"]))
        self.assertTrue(any(item["kind"] == "contract-impact-entity" for item in package["selection_trace"]))

    def test_contract_impact_respects_context_level_and_size_bounds(self) -> None:
        level_one = ai_kit.cmd_context_resolve(ns(
            query="change createOrder status", task=None, level=1, explain=False
        ))
        self.assertEqual(level_one["contract_impact"]["nodes"], [])
        self.assertFalse(any(item["source_kind"] == "generated-contract-output" for item in level_one["references"]))

        contract_path = self.root / "contracts" / "orders.json"
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
        payload["definitions"][0]["fields"].extend(
            {"name": f"field{index}", "type": "string", "required": False}
            for index in range(ai_kit.CONTEXT_IMPACT_MAX_NODES + 20)
        )
        contract_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        bounded = ai_kit.cmd_context_resolve(ns(
            query="change createOrder response", task=None, level=2, explain=False
        ))["contract_impact"]
        self.assertTrue(bounded["truncated"])
        self.assertLessEqual(len(bounded["nodes"]), ai_kit.CONTEXT_IMPACT_MAX_NODES)
        self.assertLessEqual(len(bounded["edges"]), ai_kit.CONTEXT_IMPACT_MAX_EDGES)
        self.assertIn("contract:orders@1.0.0", {item["id"] for item in bounded["nodes"]})

    def test_symbol_context_matches_camel_case_query_with_range_and_provenance(self) -> None:
        package = ai_kit.cmd_context_resolve(ns(query="change createOrder", task=None, level=2, explain=True))
        symbol = next(item for item in package["symbol_context"]["symbols"] if item["qualified_name"] == "create_order")
        self.assertEqual(symbol["selection"]["level"], 1)
        self.assertIn("exact normalized query token match", symbol["selection"]["reasons"])
        self.assertEqual(symbol["observation"]["classification"], "observed")
        self.assertEqual(symbol["provenance"]["content_hash"], ai_kit._sha256_file(self.root / "src" / "orders" / "service.py"))
        self.assertEqual(symbol["range"]["start_line"], 1)

    def test_symbol_context_uses_supplied_worktree_root(self) -> None:
        worktree = self.root / "task-worktree"
        (worktree / "src" / "orders").mkdir(parents=True)
        (worktree / "src" / "orders" / "service.py").write_text("def create_worktree_order(): pass\n", encoding="utf-8")
        package = ai_kit._resolve_context_package(
            "change createWorktreeOrder", task=None, state_file=ai_kit.STATE,
            analysis_root=worktree, max_level=2, explain=True,
        )
        self.assertTrue(any(item["qualified_name"] == "create_worktree_order" for item in package["symbol_context"]["symbols"]))
        self.assertFalse(any(item["qualified_name"] == "create_order" for item in package["symbol_context"]["symbols"]))

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
