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


class ContractCompatibilityTests(unittest.TestCase):
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
        shutil.copytree(REPO_ROOT / ".ai" / "install" / "config", self.root / ".ai" / "install" / "config")
        (self.root / ".ai" / "memory").mkdir(parents=True)

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(ai_kit, name, value)
        self.tmp.cleanup()

    def import_openapi(self, version: str, *, fields: dict, required: list[str], paths: dict | None = None) -> None:
        source = self.root / f"store-{version}.json"
        document = {
            "openapi": "3.1.0",
            "info": {"title": "Store API", "version": version},
            "paths": paths if paths is not None else {"/stores": {"post": {"operationId": "createStore"}}},
            "components": {"schemas": {"Store": {"type": "object", "required": required, "properties": fields}}},
        }
        source.write_text(json.dumps(document), encoding="utf-8")
        ai_kit.cmd_contract_import(ns(
            source=str(source), format="openapi", id="store-api", version=version,
            owner="backend", kind="api", represents="store", output=None,
            language="typescript", no_mocks=False, force=False, actor="architect",
        ))

    def test_diff_detects_removed_endpoint_and_required_field(self) -> None:
        self.import_openapi("1.0.0", fields={"id": {"type": "string"}, "name": {"type": "string"}}, required=["id"])
        self.import_openapi("2.0.0", fields={"id": {"type": "string"}, "name": {"type": "string"}}, required=["id", "name"], paths={})
        registry = ai_kit._load_contract_registry()
        record = registry["contracts"]["store-api"]["versions"]["2.0.0"]
        record["compatibility"] = "breaking"
        record["supersedes"] = "1.0.0"
        ai_kit._write_json_config("contracts.json", registry)

        diff = ai_kit.cmd_contract_diff(ns(id="store-api", from_version="1.0.0", to_version="2.0.0"))
        self.assertTrue(diff["applicable"])
        self.assertTrue(diff["breaking"])
        self.assertEqual({item["kind"] for item in diff["findings"]}, {"field-now-required", "operation-removed"})
        checked = ai_kit.cmd_contract_check(ns(id="store-api", from_version="1.0.0", to_version="2.0.0"))
        self.assertTrue(checked["passed"], checked)

    def test_check_rejects_breaking_change_without_major_version_or_declaration(self) -> None:
        self.import_openapi("1.0.0", fields={"id": {"type": "string"}}, required=["id"])
        self.import_openapi("1.1.0", fields={}, required=[])
        registry = ai_kit._load_contract_registry()
        registry["contracts"]["store-api"]["versions"]["1.1.0"]["supersedes"] = "1.0.0"
        ai_kit._write_json_config("contracts.json", registry)
        checked = ai_kit.cmd_contract_check(ns(id="store-api", from_version="1.0.0", to_version="1.1.0"))
        self.assertFalse(checked["passed"])
        self.assertEqual(checked["status"], "fail")
        self.assertEqual({item["name"] for item in checked["checks"] if item["status"] == "fail"}, {"compatibility-declared", "major-version"})
        ai_kit.cmd_contract_transition(ns(id="store-api", version="1.1.0", action="propose", actor="architect", evidence=None, migration=None, confirmed_by_user=False))
        with self.assertRaisesRegex(ai_kit.EngineError, "semantic contract compatibility failed"):
            ai_kit.cmd_contract_transition(ns(id="store-api", version="1.1.0", action="approve", actor="reviewer", evidence=None, migration=None, confirmed_by_user=False))

    def test_manual_contract_is_inconclusive_not_a_false_semantic_claim(self) -> None:
        path = self.root / "contracts" / "manual.json"
        path.parent.mkdir()
        path.write_text("{}\n", encoding="utf-8")
        ai_kit.cmd_contract_add(ns(id="manual", version="1.0.0", owner="backend", kind="api", represents="manual", path="contracts/manual.json", compatibility="backward-compatible", supersedes=None, actor="architect"))
        path2 = self.root / "contracts" / "manual-v2.json"
        path2.write_text("{}\n", encoding="utf-8")
        ai_kit.cmd_contract_add(ns(id="manual", version="2.0.0", owner="backend", kind="api", represents="manual", path="contracts/manual-v2.json", compatibility="breaking", supersedes="1.0.0", actor="architect"))
        result = ai_kit.cmd_contract_check(ns(id="manual", from_version="1.0.0", to_version="2.0.0"))
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "inconclusive")

    def test_parser_exposes_contract_diff_and_check(self) -> None:
        parser = ai_kit.parser()
        self.assertIs(parser.parse_args(["contract", "diff", "store-api", "1.0.0", "2.0.0"]).fn, ai_kit.cmd_contract_diff)
        self.assertIs(parser.parse_args(["contract", "check", "store-api", "1.0.0", "2.0.0"]).fn, ai_kit.cmd_contract_check)


if __name__ == "__main__":
    unittest.main()
