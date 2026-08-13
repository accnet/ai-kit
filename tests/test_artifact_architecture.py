"""Artifact-first Architecture Machine contract tests."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from tests.test_ai_kit import EngineTestCase, ai_kit, ns


class ArtifactArchitectureTests(EngineTestCase):
    def artifact_root(self) -> Path:
        return ai_kit.workspace(self.state_file) / "artifacts" / "project"

    def generate(self, refresh: bool = False) -> dict:
        return ai_kit._generate_project_artifacts(str(self.state_file), refresh=refresh)

    def test_exact_thirteen_file_contract_without_workflow(self) -> None:
        result = self.generate(refresh=True)
        expected = {"manifest.json", *ai_kit.ARTIFACT_PAYLOAD_FILES}
        self.assertEqual({path.name for path in self.artifact_root().iterdir() if path.is_file()}, expected)
        self.assertEqual(len(expected), 13)
        self.assertIsNone(result["manifest"]["workflow_id"])
        self.assertEqual(set(result["manifest"]["artifacts"]), set(ai_kit.ARTIFACT_PAYLOAD_FILES))
        self.assertEqual(ai_kit.cmd_artifact_validate(ns(state=str(self.state_file)))["artifacts"], 12)

    def test_custom_state_owns_its_artifact_workspace(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        result = self.generate(refresh=True)
        self.assertEqual(Path(result["root"]), Path("work/artifacts/project"))
        self.assertEqual(result["manifest"]["workflow_id"], json.loads(self.state_file.read_text())["workflow_id"])
        tasks = json.loads((self.artifact_root() / "tasks.json").read_text())
        self.assertEqual(tasks["data"]["items"][0]["id"].split(":")[-1], "T1")
        self.assertFalse((ai_kit.WORK / "artifacts" / "project" / "manifest.json").exists())

    def test_cache_hit_and_refresh_generation(self) -> None:
        first = self.generate(refresh=True)
        second = self.generate()
        third = self.generate(refresh=True)
        self.assertEqual(second["status"], "hit")
        self.assertEqual(first["manifest"]["generation_id"], second["manifest"]["generation_id"])
        self.assertNotEqual(second["manifest"]["generation_id"], third["manifest"]["generation_id"])

    def test_tracked_same_size_content_change_invalidates_cache(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        source = self.root / "src" / "service.py"
        source.parent.mkdir(parents=True)
        source.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "src/service.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)

        first = self.generate(refresh=True)
        source.write_text("value = 2\n", encoding="utf-8")
        second = self.generate()
        source.write_text("value = 3\n", encoding="utf-8")
        third = self.generate()

        self.assertEqual(second["status"], "refreshed")
        self.assertEqual(third["status"], "refreshed")
        self.assertNotEqual(first["manifest"]["source_fingerprint"], second["manifest"]["source_fingerprint"])
        self.assertNotEqual(second["manifest"]["source_fingerprint"], third["manifest"]["source_fingerprint"])

    def test_manifest_hash_detects_tampered_payload(self) -> None:
        self.generate(refresh=True)
        modules_path = self.artifact_root() / "modules.json"
        payload = json.loads(modules_path.read_text())
        payload["data"]["items"].append({"id": "module:tampered"})
        modules_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ai_kit.EngineError, "hash mismatch"):
            ai_kit.cmd_artifact_validate(ns(state=str(self.state_file)))

    def test_manifest_is_unchanged_when_commit_marker_replace_fails(self) -> None:
        first = self.generate(refresh=True)
        old_generation = first["manifest"]["generation_id"]
        original_replace = os.replace

        def fail_manifest(source, destination):
            if Path(source).name == "manifest.json" and Path(destination).name == "manifest.json":
                raise OSError("simulated manifest publication failure")
            return original_replace(source, destination)

        with mock.patch.object(ai_kit.os, "replace", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "publication failure"):
                self.generate(refresh=True)
        manifest = json.loads((self.artifact_root() / "manifest.json").read_text())
        self.assertEqual(manifest["generation_id"], old_generation)
        recovered = self.generate(refresh=True)
        self.assertNotEqual(recovered["manifest"]["generation_id"], old_generation)
        ai_kit.cmd_artifact_validate(ns(state=str(self.state_file)))

    def test_concurrent_refresh_finishes_with_one_valid_generation(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: self.generate(refresh=True), range(2)))
        self.assertEqual(len(results), 2)
        validated = ai_kit.cmd_artifact_validate(ns(state=str(self.state_file)))
        self.assertTrue(validated["valid"])
        self.assertIn(validated["generation_id"], {item["manifest"]["generation_id"] for item in results})

    def test_observation_contract_and_proposed_edge_exclusion(self) -> None:
        with self.assertRaises(ai_kit.EngineError):
            ai_kit._architecture_observation("observed", "config", ["x"], confidence=.9)
        with self.assertRaises(ai_kit.EngineError):
            ai_kit._architecture_observation("inferred", "convention", ["x"], confidence=.7)
        with self.assertRaises(ai_kit.EngineError):
            ai_kit._architecture_observation("proposed", "decision", ["adr.md"], confidence=.5, rationale="candidate")

        result = self.generate(refresh=True)
        payloads = copy.deepcopy(result["payloads"])
        observed = ai_kit._architecture_observation("observed", "config", ["contexts.yaml"], confidence=1)
        proposed = ai_kit._architecture_observation(
            "proposed", "decision", ["adr.md"], confidence=.5, rationale="candidate boundary", proposer="architect"
        )
        payloads["modules.json"]["data"]["items"] = [
            {"id": "module:a", "depends_on": [], "observation": observed},
            {"id": "module:b", "depends_on": [], "observation": observed},
        ]
        edge = {"id": "dependency:proposal", "from": "module:a", "to": "module:b", "active": False, "observation": proposed}
        payloads["dependencies.json"]["data"]["items"] = [edge]
        payloads["architecture.json"]["data"].update({
            "contexts": [], "module_refs": ["module:a", "module:b"],
            "dependency_refs": [edge["id"]], "active_dependency_refs": [],
            "impact": {
                "module:a": {"direct_dependents": [], "all_dependents": [], "affected_task_refs": []},
                "module:b": {"direct_dependents": [], "all_dependents": [], "affected_task_refs": []},
            },
        })
        payloads["ownership.json"]["data"] = {"owners": {}, "unowned": ["module:a", "module:b"]}
        self.assertTrue(ai_kit._validate_artifact_payloads(payloads)["valid"])
        edge["active"] = True
        payloads["architecture.json"]["data"]["active_dependency_refs"] = [edge["id"]]
        with self.assertRaisesRegex(ai_kit.EngineError, "proposed dependency"):
            ai_kit._validate_artifact_payloads(payloads)

    def test_events_are_bounded_projection_and_divergence_is_risk(self) -> None:
        self.init_workflow()
        state = json.loads(self.state_file.read_text())
        state["events"] = [
            {"ts": f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z", "action": "tick", "task": None,
             "actor": "system", "from": None, "to": None, "detail": str(index)}
            for index in range(205)
        ]
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        archive = ai_kit.workspace(self.state_file) / "logs" / "events.jsonl"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(json.dumps({"different": True}) + "\n", encoding="utf-8")
        result = self.generate(refresh=True)
        events = result["payloads"]["events.json"]["data"]
        self.assertEqual(events["total"], 205)
        self.assertEqual(len(events["events"]), 200)
        self.assertTrue(events["truncated"])
        risks = result["payloads"]["risks.json"]["data"]["items"]
        self.assertIn("event_history_divergence", {item["kind"] for item in risks})

    def test_non_git_project_has_safe_git_projection(self) -> None:
        result = self.generate(refresh=True)
        git_data = result["payloads"]["git.json"]["data"]
        self.assertFalse(git_data["repository"])
        self.assertNotIn("remote", git_data)
        self.assertNotIn("diff", git_data)

    def test_legacy_projection_is_derived_from_canonical_bundle(self) -> None:
        ai_kit.VISUALIZER_DIR = self.root / ".visualizer"
        ai_kit.VISUALIZER_DIR.mkdir()
        result = self.generate(refresh=True)
        expected = ai_kit._legacy_visualizer_projection(result["payloads"])
        self.assertEqual(result["legacy"]["dag.json"], expected["dag.json"])
        self.assertEqual(json.loads((ai_kit.VISUALIZER_DIR / "board.json").read_text()), expected["board.json"])

    def test_canonical_dag_uses_stable_task_references(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.add_task("T2", needs=["T1"])
        result = self.generate(refresh=True)
        workflow_id = result["manifest"]["workflow_id"]
        dag = result["payloads"]["dag.json"]["data"]
        first, second = f"task:{workflow_id}:T1", f"task:{workflow_id}:T2"
        self.assertEqual({item["id"] for item in dag["tasks"]}, {first, second})
        self.assertEqual(dag["edges"][0]["from"], first)
        self.assertEqual(dag["edges"][0]["to"], second)
        self.assertEqual(result["legacy"]["dag.json"]["edges"][0]["from"], "T1")

    def test_cross_artifact_reference_validation(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        evidence_path = ai_kit.workspace(self.state_file) / "evidence" / "note.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps({"kind": "note", "task": "T1"}), encoding="utf-8")
        source = self.generate(refresh=True)["payloads"]
        ai_kit._validate_artifact_payloads(source)

        broken = copy.deepcopy(source)
        broken["dag.json"]["data"]["ready"] = ["task:unknown:T9"]
        with self.assertRaisesRegex(ai_kit.EngineError, "DAG ready"):
            ai_kit._validate_artifact_payloads(broken)

        broken = copy.deepcopy(source)
        broken["ownership.json"]["data"]["owners"].setdefault("backend", {"modules": [], "contracts": [], "tasks": []})["tasks"] = ["task:unknown:T9"]
        with self.assertRaisesRegex(ai_kit.EngineError, "unknown reference"):
            ai_kit._validate_artifact_payloads(broken)

        broken = copy.deepcopy(source)
        broken["evidence.json"]["data"]["items"][0]["task_ref"] = "task:unknown:T9"
        with self.assertRaisesRegex(ai_kit.EngineError, "unknown task reference"):
            ai_kit._validate_artifact_payloads(broken)

        enriched = copy.deepcopy(source)
        task_ref = enriched["tasks.json"]["data"]["items"][0]["id"]
        contract_ref = "contract:sample@1.0.0"
        contract = {
            "id": contract_ref, "contract_id": "sample", "version": "1.0.0", "owner": "architect",
            "kind": "api", "represents": "sample", "path": "contracts/sample.json", "status": "approved",
            "content_hash": "abc", "compatibility": "backward-compatible", "supersedes": None,
            "generated_outputs": [],
        }
        relationship = {"id": "sample", "version": "1.0.0", "relation": "implements", "contract_ref": contract_ref}
        enriched["contracts.json"]["data"].update({
            "items": [contract], "contract_refs": [contract_ref],
            "edges": [{
                "from": task_ref, "to": contract_ref, "relation": "implements",
                "observation": ai_kit._architecture_observation("observed", "source", [str(self.state_file)], confidence=1),
            }],
        })
        enriched["tasks.json"]["data"]["items"][0]["contract_refs"] = [relationship]
        enriched["dag.json"]["data"]["tasks"][0]["contract_refs"] = [relationship]
        enriched["ownership.json"]["data"]["owners"]["architect"] = {
            "modules": [], "contracts": [contract_ref], "tasks": [],
        }
        ai_kit._validate_artifact_payloads(enriched)
        enriched["contracts.json"]["data"]["edges"][0]["to"] = "contract:missing@1.0.0"
        with self.assertRaisesRegex(ai_kit.EngineError, "unknown endpoint"):
            ai_kit._validate_artifact_payloads(enriched)

        broken = copy.deepcopy(source)
        modules = broken["modules.json"]["data"]["items"]
        if modules:
            modules[0]["task_refs"] = ["task:unknown:T9"]
            with self.assertRaisesRegex(ai_kit.EngineError, "unknown task reference"):
                ai_kit._validate_artifact_payloads(broken)

    def test_auxiliary_evidence_input_does_not_collide_with_canonical_evidence(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        review_root = ai_kit.workspace(self.state_file) / "evidence" / "review"
        review_root.mkdir(parents=True)
        (review_root / "T1.input.json").write_text(
            json.dumps({"task": "T1", "decision": "approve"}), encoding="utf-8"
        )
        canonical = review_root / "T1.recommendation.json"
        canonical.write_text(
            json.dumps({"kind": "review", "task": "T1", "decision": "approve"}), encoding="utf-8"
        )
        state = ai_kit.load(self.state_file)
        ai_kit.task_map(state)["T1"]["evidence"].append(str(canonical.resolve()))
        ai_kit.save(state, self.state_file, state["revision"])

        result = self.generate(refresh=True)
        items = result["payloads"]["evidence.json"]["data"]["items"]
        ids = [item["id"] for item in items]
        self.assertEqual(len(ids), len(set(ids)))
        canonical_item = next(item for item in items if item["path"].endswith("T1.recommendation.json"))
        auxiliary_item = next(item for item in items if item["path"].endswith("T1.input.json"))
        self.assertEqual(canonical_item["id"], "evidence:review:T1")
        self.assertTrue(auxiliary_item["id"].startswith("evidence:review:T1:"))

    def test_public_cli_surface(self) -> None:
        parsed = ai_kit.parser().parse_args(["--state", str(self.state_file), "artifact", "generate", "--refresh"])
        self.assertIs(parsed.fn, ai_kit.cmd_artifact_generate)
        self.assertTrue(parsed.refresh)
        parsed = ai_kit.parser().parse_args(["artifact", "show", "modules"])
        self.assertIs(parsed.fn, ai_kit.cmd_artifact_show)
        parsed = ai_kit.parser().parse_args(["visualizer", "serve", "--port", "0"])
        self.assertIs(parsed.fn, ai_kit.cmd_visualizer_serve)

    def test_authoritative_mutation_auto_regenerates_bundle(self) -> None:
        ai_kit.AUTO_ARTIFACT_GENERATION = True
        self.init_workflow()
        first = json.loads((self.artifact_root() / "manifest.json").read_text(encoding="utf-8"))

        self.add_task("T1")

        second = json.loads((self.artifact_root() / "manifest.json").read_text(encoding="utf-8"))
        tasks = json.loads((self.artifact_root() / "tasks.json").read_text(encoding="utf-8"))
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertNotEqual(first["generation_id"], second["generation_id"])
        self.assertGreater(second["state_revision"], first["state_revision"])
        self.assertEqual(second["state_revision"], state["revision"])
        self.assertEqual([item["task_id"] for item in tasks["data"]["items"]], ["T1"])
