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

    def import_document(self, contract_id: str, version: str, fmt: str, document: dict) -> None:
        source = self.root / f"{contract_id}-{version}.json"
        source.write_text(json.dumps(document), encoding="utf-8")
        ai_kit.cmd_contract_import(ns(
            source=str(source), format=fmt, id=contract_id, version=version,
            owner="backend", kind="event" if fmt == "asyncapi" else "api",
            represents=contract_id, output=None, language="typescript",
            no_mocks=False, force=False, actor="architect",
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

    def test_openapi_operation_normalizes_body_responses_status_auth_and_errors(self) -> None:
        document = {
            "openapi": "3.1.0",
            "info": {"title": "Store API", "version": "1.0.0"},
            "security": [{"bearerAuth": ["store:write"]}],
            "paths": {"/stores": {"post": {
                "operationId": "createStore",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateStore"}}}},
                "responses": {
                    "201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Store"}}}},
                    "400": {"$ref": "#/components/responses/BadRequest"},
                },
            }}},
            "components": {
                "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}},
                "responses": {"BadRequest": {"content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Error"}}}}},
                "schemas": {
                    "CreateStore": {"type": "object", "properties": {"name": {"type": "string"}}},
                    "Store": {"type": "object", "properties": {"id": {"type": "string"}}},
                    "Error": {"type": "object", "properties": {"code": {"type": "string"}}},
                },
            },
        }
        normalized = ai_kit._normalize_imported_contract("openapi", document, json.dumps(document), self.root / "openapi.json")
        self.assertEqual(normalized["schema_version"], 2)
        operation = normalized["operations"][0]
        self.assertTrue(operation["request"]["required"])
        self.assertEqual(operation["request"]["content"][0]["schema"]["ref"], "#/components/schemas/CreateStore")
        self.assertEqual([(item["status"], item["category"]) for item in operation["responses"]], [("201", "success"), ("400", "client-error")])
        self.assertEqual([item["status"] for item in operation["errors"]], ["400"])
        self.assertEqual(operation["auth"][0]["schemes"][0], {"name": "bearerAuth", "scopes": ["store:write"]})
        self.assertEqual(normalized["security_schemes"][0]["scheme"], "bearer")

        self.import_document("store-api", "1.0.0", "openapi", document)
        projected = ai_kit._contract_artifact(ai_kit.STATE, None)["items"][0]["semantic"]
        self.assertTrue(projected["complete"])
        self.assertEqual(projected["operations"][0]["errors"][0]["status"], "400")
        self.assertNotIn("source", projected)

        registry = ai_kit._load_contract_registry()
        version = registry["contracts"]["store-api"]["versions"]["1.0.0"]
        version["generated_output_hashes"] = {"generated/store-sdk.ts": "abc123"}
        version["generators"] = [{"name": "typescript-sdk", "outputs": ["generated/store-sdk.ts", "generated/store-mocks.ts"]}]
        ai_kit._write_json_config("contracts.json", registry)
        impact = ai_kit._contract_impact_payload("store-api", "1.0.0")
        self.assertEqual(impact["schema_version"], 2)
        self.assertTrue({"operation", "schema", "field", "generated-output"}.issubset(impact["entity_refs"]))
        relations = {edge["relation"] for edge in impact["graph"]["edges"]}
        self.assertTrue({"contains", "request-body", "response", "error-response", "generates"}.issubset(relations))
        generated = next(node for node in impact["graph"]["nodes"] if node.get("path") == "generated/store-sdk.ts")
        self.assertEqual(generated["content_hash"], "abc123")
        self.assertTrue(generated["materialized"])
        self.assertEqual(generated["generators"], ["typescript-sdk"])
        configured = next(node for node in impact["graph"]["nodes"] if node.get("path") == "generated/store-mocks.ts")
        self.assertFalse(configured["materialized"])
        self.assertEqual(impact["generated_outputs"], ["generated/store-mocks.ts", "generated/store-sdk.ts"])
        self.assertEqual(impact["graph"], ai_kit._contract_impact_payload("store-api", "1.0.0")["graph"])
        artifact_graph = ai_kit._contract_artifact(ai_kit.STATE, None)["impact_graph"]
        self.assertEqual(
            {key: impact["graph"][key] for key in ("nodes", "edges", "summary")},
            artifact_graph,
        )
        contract, version_record = ai_kit._contract_version(ai_kit._load_contract_registry(), "store-api", "1.0.0")
        task_graph = ai_kit._contract_impact_graph("store-api", "1.0.0", contract, version_record, [{
            "workflow_id": "wf-test", "task": "T1", "status": "todo", "context": "store", "relations": ["consumes"],
        }])
        self.assertIn("task", {node["type"] for node in task_graph["nodes"]})
        self.assertIn("consumes", {edge["relation"] for edge in task_graph["edges"]})

    def test_openapi_diff_detects_body_response_status_auth_and_error_changes(self) -> None:
        def document(version: str, changed: bool) -> dict:
            request_schema = {"type": "object", "properties": {"name": {"type": "string"}}}
            if changed:
                request_schema["required"] = ["name"]
            responses = {
                "201": {"content": {"application/json": {"schema": {"type": "string" if not changed else "integer"}}}},
                ("401" if changed else "400"): {"content": {"application/problem+json": {"schema": {"type": "object"}}}},
            }
            return {
                "openapi": "3.1.0", "info": {"title": "Store API", "version": version},
                "security": [{"bearerAuth": []}] if changed else [],
                "paths": {"/stores": {"post": {
                    "operationId": "createStore",
                    "requestBody": {"required": changed, "content": {"application/json": {"schema": request_schema}}},
                    "responses": responses,
                }}},
                "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}, "schemas": {}},
            }

        self.import_document("store-api", "1.0.0", "openapi", document("1.0.0", False))
        self.import_document("store-api", "2.0.0", "openapi", document("2.0.0", True))
        diff = ai_kit.cmd_contract_diff(ns(id="store-api", from_version="1.0.0", to_version="2.0.0"))
        kinds = {item["kind"] for item in diff["findings"]}
        self.assertTrue(diff["complete"])
        self.assertTrue({
            "request-body-now-required", "request-schema-changed", "response-schema-changed",
            "response-status-removed", "error-status-added", "auth-requirement-tightened",
        }.issubset(kinds), diff)

    def test_optional_auth_alternative_and_oauth_scope_addition_are_non_breaking(self) -> None:
        self.assertFalse(ai_kit._auth_requirement_tightened([], [{"schemes": []}]))
        old = {"type": "oauth2", "flows": {"clientCredentials": {"token_url": "/token", "authorization_url": None, "refresh_url": None, "scopes": ["store:read"]}}}
        new = {"type": "oauth2", "flows": {"clientCredentials": {"token_url": "/token", "authorization_url": None, "refresh_url": None, "scopes": ["store:read", "store:write"]}}}
        self.assertFalse(ai_kit._security_scheme_breaks(old, new))
        self.assertTrue(ai_kit._security_scheme_breaks(new, old))

    def test_asyncapi_diff_detects_event_payload_and_message_variants(self) -> None:
        def document(version: str, changed: bool) -> dict:
            payload = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}, "name": {"type": "string"}}}
            if changed:
                payload["required"].append("name")
            messages = [{"name": "StoreChanged", "contentType": "application/json", "payload": payload}]
            if changed:
                messages.append({"name": "StoreDeleted", "contentType": "application/json", "payload": {"type": "object"}})
            message = messages[0] if len(messages) == 1 else {"oneOf": messages}
            return {
                "asyncapi": "2.6.0", "info": {"title": "Store Events", "version": version},
                "channels": {"store.lifecycle": {"publish": {"operationId": "storeLifecycle", "message": message}}},
                "components": {"schemas": {}},
            }

        self.import_document("store-events", "1.0.0", "asyncapi", document("1.0.0", False))
        self.import_document("store-events", "2.0.0", "asyncapi", document("2.0.0", True))
        diff = ai_kit.cmd_contract_diff(ns(id="store-events", from_version="1.0.0", to_version="2.0.0"))
        self.assertTrue(diff["complete"])
        self.assertTrue({"event-payload-changed", "event-message-added"}.issubset({item["kind"] for item in diff["findings"]}), diff)
        impact = ai_kit._contract_impact_payload("store-events", "2.0.0")
        self.assertTrue({"event", "message", "schema", "field"}.issubset(impact["entity_refs"]))
        self.assertIn("event-payload", {edge["relation"] for edge in impact["graph"]["edges"]})

    def test_asyncapi_v3_operation_resolves_channel_message_payload(self) -> None:
        document = {
            "asyncapi": "3.0.0", "info": {"title": "Store Events", "version": "1.0.0"},
            "channels": {"storeLifecycle": {"address": "store.lifecycle", "messages": {"changed": {"$ref": "#/components/messages/StoreChanged"}}}},
            "operations": {"publishStoreChanged": {
                "action": "send", "channel": {"$ref": "#/channels/storeLifecycle"},
                "messages": [{"$ref": "#/channels/storeLifecycle/messages/changed"}],
            }},
            "components": {"schemas": {}, "messages": {"StoreChanged": {
                "name": "StoreChanged", "contentType": "application/json",
                "payload": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
            }}},
        }
        normalized = ai_kit._normalize_imported_contract("asyncapi", document, json.dumps(document), self.root / "events.json")
        event = normalized["events"][0]
        self.assertEqual((event["channel"], event["direction"]), ("store.lifecycle", "publish"))
        self.assertEqual(event["messages"][0]["name"], "StoreChanged")
        self.assertEqual(event["messages"][0]["payload"]["required"], ["id"])

    def test_normalized_v1_contract_remains_readable_but_inconclusive(self) -> None:
        self.import_openapi("1.0.0", fields={"id": {"type": "string"}}, required=["id"])
        self.import_openapi("2.0.0", fields={"id": {"type": "string"}}, required=["id"])
        registry = ai_kit._load_contract_registry()
        first = ai_kit._registry_contract_path(registry["contracts"]["store-api"]["versions"]["1.0.0"])
        payload = json.loads(first.read_text(encoding="utf-8"))
        payload["schema_version"] = 1
        payload.pop("semantic_coverage", None)
        first.write_text(json.dumps(payload), encoding="utf-8")
        diff = ai_kit.cmd_contract_diff(ns(id="store-api", from_version="1.0.0", to_version="2.0.0"))
        self.assertTrue(diff["applicable"])
        self.assertFalse(diff["complete"])
        checked = ai_kit.cmd_contract_check(ns(id="store-api", from_version="1.0.0", to_version="2.0.0"))
        self.assertEqual(checked["status"], "inconclusive")
        self.assertFalse(checked["passed"])

    def test_explicit_structured_import_cannot_claim_wrong_format_coverage(self) -> None:
        source = self.root / "not-openapi.json"
        source.write_text(json.dumps({"info": {"title": "Not OpenAPI"}, "paths": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ai_kit.EngineError, "version marker"):
            ai_kit.cmd_contract_import(ns(
                source=str(source), format="openapi", id="invalid", version="1.0.0",
                owner="backend", kind="api", represents="invalid", output=None,
                language="typescript", no_mocks=False, force=False, actor="architect",
            ))

    def test_parser_exposes_contract_diff_and_check(self) -> None:
        parser = ai_kit.parser()
        self.assertIs(parser.parse_args(["contract", "diff", "store-api", "1.0.0", "2.0.0"]).fn, ai_kit.cmd_contract_diff)
        self.assertIs(parser.parse_args(["contract", "check", "store-api", "1.0.0", "2.0.0"]).fn, ai_kit.cmd_contract_check)


if __name__ == "__main__":
    unittest.main()
