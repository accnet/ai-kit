from __future__ import annotations

import argparse
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = REPO_ROOT / ".ai" / "engine"
sys.path.insert(0, str(ENGINE_DIR))
import ai_kit  # noqa: E402


class AdvancedArchitectureTests(unittest.TestCase):
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
        config = self.root / ".ai" / "install" / "config"
        config.mkdir(parents=True)
        (config / "contracts.json").write_text('{"schema_version":1,"contracts":{}}\n', encoding="utf-8")
        (config / "kit.yaml").write_text("kit:\n  id: fixture\nproject:\n  source_dirs: [src]\n", encoding="utf-8")
        (config / "contexts.yaml").write_text("contexts:\n", encoding="utf-8")
        (config / "delivery.json").write_text('{"schema_version":1,"integration_branch":"main","push_required":false,"pre_integration_commands":[]}\n', encoding="utf-8")
        (config / "architecture.json").write_text('{"schema_version":1,"systems":[],"external_systems":[],"containers":[],"context_mappings":{},"relationships":[]}\n', encoding="utf-8")
        (config / "architecture-fitness.json").write_text('{"schema_version":1,"rules":[],"commands":[]}\n', encoding="utf-8")

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(ai_kit, name, value)
        self.tmp.cleanup()

    def test_openapi_import_generates_dto_and_mock(self) -> None:
        source = self.root / "openapi.json"
        source.write_text(json.dumps({"openapi":"3.1.0","info":{"title":"Order API","version":"1.2.0"},"paths":{"/orders":{"post":{"operationId":"createOrder"}}},"components":{"schemas":{"Order":{"type":"object","required":["id"],"properties":{"id":{"type":"string"},"total":{"type":"number"}}}}}}), encoding="utf-8")
        result = ai_kit.cmd_contract_import(argparse.Namespace(source=str(source), format="auto", id=None, version=None, owner="backend", kind=None, represents=None, output=str(self.root / "generated"), language="typescript", no_mocks=False, force=False, actor="architect"))
        self.assertEqual(result["contract"], "order-api")
        self.assertEqual(result["version"], "1.2.0")
        self.assertIn("export interface Order", (self.root / "generated" / "contracts.ts").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "generated" / "mocks.ts").is_file())
        registry = json.loads((self.root / ".ai-config" / "contracts.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["contracts"]["order-api"]["versions"]["1.2.0"]["import"]["format"], "openapi")

    def test_asyncapi_protobuf_and_prisma_are_normalized(self) -> None:
        asyncapi = ai_kit._normalize_imported_contract("asyncapi", {"info":{"title":"Events","version":"1.0.0"},"channels":{"orders":{"publish":{"operationId":"orderPlaced"}}}}, "", self.root / "asyncapi.json")
        protobuf = ai_kit._normalize_imported_contract("protobuf", None, 'syntax = "proto3";\npackage orders;\nmessage Order {\n string id = 1;\n}\nservice Orders {\n rpc Get (Order) returns (Order);\n}\n', self.root / "orders.proto")
        prisma = ai_kit._normalize_imported_contract("prisma", None, "model Order {\n id String\n total Float?\n}\n", self.root / "schema.prisma")
        self.assertEqual(asyncapi["events"][0]["id"], "orderPlaced")
        self.assertEqual(protobuf["definitions"][0]["name"], "Order")
        self.assertEqual(prisma["models"][0]["fields"][1]["required"], False)

    def test_fitness_rule_blocks_presentation_to_database_import(self) -> None:
        presentation = self.root / "src" / "presentation"; database = self.root / "src" / "database"
        presentation.mkdir(parents=True); database.mkdir(parents=True)
        (presentation / "controller.py").write_text("from ..database.models import Model\n", encoding="utf-8")
        (database / "models.py").write_text("class Model: pass\n", encoding="utf-8")
        rules = {"schema_version":1,"rules":[{"id":"no-presentation-db","type":"forbid-dependency","from":["src/presentation/*"],"to":["src/database/*"],"message":"Use the domain boundary"}],"commands":[]}
        (self.root / ".ai" / "install" / "config" / "architecture-fitness.json").write_text(json.dumps(rules), encoding="utf-8")
        result = ai_kit._architecture_fitness(self.root)
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"][0]["violations"][0]["to"], "src/database/models.py")
        self.assertIn("_architecture_fitness(run_root)", inspect.getsource(ai_kit.cmd_verify))
        self.assertIn("_architecture_model_diagnostics()", inspect.getsource(ai_kit.cmd_verify))

    def test_c4_projection_contains_three_levels(self) -> None:
        observation = ai_kit._architecture_observation("observed", "config", ["contexts.yaml"], confidence=1.0)
        c4 = ai_kit._c4_projection([{"id":"context:orders","name":"orders","path":"src/orders/*","owner":"backend","depends_on":[],"observation":observation}])
        self.assertEqual(set(c4["levels"]), {"1", "2", "3"})
        self.assertEqual(c4["components"][0]["context_ref"], "context:orders")


class DistributedSkillRegistryTests(unittest.TestCase):
    def test_trigger_resolves_all_nested_distributed_skills(self) -> None:
        saved = ai_kit.ROOT
        ai_kit.ROOT = REPO_ROOT
        try:
            trigger = ai_kit._load_skill_triggers()["distributed-reliability"]
            resolved = [ai_kit._resolve_technology_skill(REPO_ROOT, ref) for ref in trigger["technology_skills"]]
            self.assertTrue(all(path and (path / "SKILL.md").is_file() for path in resolved))
        finally:
            ai_kit.ROOT = saved


if __name__ == "__main__":
    unittest.main()
