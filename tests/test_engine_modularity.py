"""Contract tests for the incremental engine modularization boundary."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_ai_kit import ai_kit
from kit_engine.domain.tasks import runnable as domain_runnable, task_map, transitive_needs
from kit_engine.planning import generate_dag_payload
from kit_engine.contracts import ContractGraphBuilder, normalize_contract_refs, operation_semantic_breaks, schema_shape_breaks
from kit_engine.execution import entry_list, entry_models, parse_inline_list, ready_tasks, render_runner_command, resolve_runner, split_runner_reference, supports as runner_supports
from kit_engine.architecture import build_observation, extract_python_imports, map_task_to_module, owning_module
from kit_engine.artifact import artifact_envelope, json_bytes, publish, sha256_bytes, validate_bundle_envelopes
from kit_engine.context import reference_stats, requested_level, task_text, tokenize_query, tokenize_task
from kit_engine.config import load_yaml_subset, validate_runtime_config
from kit_engine.cli import exit_code_for_result, render_result
from kit_engine.quality import classify_qa_failure, evidence_fingerprint, not_applicable_reason, reviewer_identity_error
from kit_engine.execution import safe_git_component


class EngineModularityTests(unittest.TestCase):
    def test_runtime_derives_custom_workspace_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            state = root / ".ai-work" / "state" / "workflow.json"
            runtime = ai_kit.Runtime.from_state(root, state)
            self.assertEqual(runtime.root, root.resolve())
            self.assertEqual(runtime.workspace, (root / ".ai-work").resolve())
            self.assertEqual(runtime.artifact_root, (root / ".ai-work" / "artifacts" / "project").resolve())

    def test_runtime_derives_standalone_custom_state_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            state = root / "states" / "demo.json"
            runtime = ai_kit.Runtime.from_state(root, state)
            self.assertEqual(runtime.workspace, (root / "states" / "demo").resolve())

    def test_storage_atomic_json_preserves_valid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "payload.json"
            ai_kit.atomic_write_json(target, {"schema_version": 1, "ok": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"schema_version": 1, "ok": True})
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_artifact_primitives_are_stable_and_lifecycle_agnostic(self) -> None:
        payload = artifact_envelope("tasks", "g1", "2026-01-01T00:00:00Z", "wf", {"items": []})
        self.assertEqual(payload["generation_id"], "g1")
        self.assertEqual(payload["workflow_id"], "wf")
        self.assertEqual(sha256_bytes(json_bytes(payload)), sha256_bytes(json_bytes(payload)))

    def test_artifact_publisher_replaces_manifest_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts" / "project"
            calls = []
            def acquire(_lock, _owner, **_kwargs):
                calls.append("lock")
                return True
            payload = artifact_envelope("tasks", "g1", "2026-01-01T00:00:00Z", None, {})
            publish(root, {"tasks.json": payload}, {"generation_id": "g1"}, ["tasks.json"], acquire)
            self.assertTrue((root / "manifest.json").exists())
            self.assertEqual(calls, ["lock"])

    def test_artifact_envelope_validator_detects_generation_mismatch(self) -> None:
        payload = artifact_envelope("tasks", "g1", "2026-01-01T00:00:00Z", None, {})
        self.assertEqual(validate_bundle_envelopes({"tasks.json": payload}, None, ["tasks.json"], 1, 1, 1, lambda value: sha256_bytes(json_bytes(value))), "g1")
        payload["generation_id"] = None
        with self.assertRaises(ValueError):
            validate_bundle_envelopes({"tasks.json": payload}, None, ["tasks.json"], 1, 1, 1, lambda value: sha256_bytes(json_bytes(value)))

    def test_quality_failure_taxonomy_and_fingerprint_are_deterministic(self) -> None:
        classification = classify_qa_failure({"status": "fail", "checks": [{"name": "test_command", "result": "fail"}]})
        self.assertEqual(classification[:3], ("test_regression", "retry-worker", True))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("same", encoding="utf-8")
            task = {"contract_hash": "c1", "base_commit": "b1", "governance_baseline": {"contract_snapshots": {}}}
            first = evidence_fingerprint(task, root, ["a.txt"], "d1")
            second = evidence_fingerprint(task, root, ["a.txt"], "d1")
            self.assertEqual(first, second)

    def test_quality_review_and_delivery_decisions_are_pure(self) -> None:
        assignment = {"runner": "codex", "model": "m1", "agent_id": "exec"}
        self.assertEqual(reviewer_identity_error({"runner": "claude", "model": "m2", "agent_id": "review"}, assignment), None)
        self.assertIn("differ", reviewer_identity_error({"runner": "codex", "model": "m1", "agent_id": "review"}, assignment))
        self.assertEqual(not_applicable_reason({"files": [".ai-work/tasks/T1.json"]}, []), "task scope contains only AI-Kit control-plane artifacts")

    def test_legacy_facade_still_exposes_engine_error_and_artifact_root(self) -> None:
        self.assertTrue(issubclass(ai_kit.EngineError, Exception))
        self.assertTrue(callable(ai_kit._artifact_root))

    def test_task_domain_is_pure_and_facade_delegates(self) -> None:
        state = {"tasks": [
            {"id": "A", "status": "done", "needs": []},
            {"id": "B", "status": "todo", "needs": ["A"]},
        ]}
        tasks = task_map(state)
        ready = domain_runnable(
            tasks["B"], tasks,
            dependency_satisfying_statuses={"done"},
            contract_refs_ready=lambda _task: (True, None),
        )
        self.assertTrue(ready)
        self.assertEqual(transitive_needs("B", tasks), {"A"})
        # The facade wrapper uses the same domain implementation and applies
        # the control-plane's contract readiness policy.
        self.assertTrue(ai_kit.runnable(tasks["B"], tasks))

    def test_planning_dag_projection_matches_facade(self) -> None:
        state = {"tasks": [
            {"id": "A", "title": "A", "owner": "backend", "phase": "build", "status": "done", "needs": [], "task_kind": "general"},
            {"id": "B", "title": "B", "owner": "backend", "phase": "build", "status": "todo", "needs": ["A"], "task_kind": "cleanup"},
        ], "events": []}
        expected = generate_dag_payload(
            state,
            task_map=task_map,
            runnable=lambda task, tasks: domain_runnable(
                task, tasks, dependency_satisfying_statuses={"done"}, contract_refs_ready=lambda _task: (True, None)
            ),
            dependency_satisfying_statuses={"done"},
            remaining_stages=lambda status: 0 if status == "done" else 1,
            task_stage=lambda status: status,
            task_history=lambda _state: {},
        )
        self.assertEqual(expected["waves"], 2)
        self.assertEqual(expected["ready"], ["B"])

    def test_contract_and_execution_boundaries_are_pure(self) -> None:
        refs = normalize_contract_refs(["implements:orders@1.0.0"], {"defines", "implements", "consumes", "verifies"})
        self.assertEqual(refs, [{"id": "orders", "version": "1.0.0", "relation": "implements"}])
        state = {"tasks": [
            {"id": "B", "status": "todo", "needs": [], "context": "orders", "epic": "checkout"},
            {"id": "A", "status": "todo", "needs": [], "context": "other", "epic": "checkout"},
        ]}
        tasks = task_map(state)
        result = ready_tasks(
            state, tasks, runnable=lambda task, _tasks: task["status"] == "todo",
            context="orders", epic="checkout",
        )
        self.assertEqual([task["id"] for task in result], ["B"])

    def test_contract_semantic_predicates_detect_breaking_changes(self) -> None:
        self.assertTrue(schema_shape_breaks({"type": "string"}, {"type": "integer"}))
        findings = operation_semantic_breaks(
            "GET:/orders",
            {"request": {"required": False, "content": [{"media_type": "application/json", "schema": {"type": "string"}}]}, "responses": []},
            {"request": {"required": True, "content": [{"media_type": "application/json", "schema": {"type": "string"}}]}, "responses": []},
        )
        self.assertEqual(findings[0]["kind"], "request-body-now-required")

    def test_contract_graph_builder_has_stable_edge_ids(self) -> None:
        graph = ContractGraphBuilder()
        graph.add_node("contract:x@1", "contract", "x@1")
        graph.add_node("domain:x", "domain", "x")
        graph.add_edge("contract:x@1", "domain:x", "represents")
        nodes, edges, counts = graph.projection()
        self.assertEqual([node["id"] for node in nodes], ["contract:x@1", "domain:x"])
        self.assertTrue(edges[0]["id"].startswith("contract-impact-edge:"))
        self.assertEqual(counts["contract"], 1)

    def test_runner_capability_and_capacity_predicate_is_pure(self) -> None:
        entry_list = lambda entry, key: list(entry.get(key) or [])
        task = {"owner": "backend", "task_kind": "implementation", "required_capabilities": ["testing"], "status": "todo"}
        self.assertEqual(
            runner_supports("local", {"roles": ["backend"], "task_kinds": ["implementation"], "capabilities": ["testing"], "max_parallel": 1}, task, {"tasks": []}, entry_list),
            (True, None),
        )
        self.assertFalse(runner_supports("local", {"roles": ["frontend"], "capabilities": ["testing"]}, task, {"tasks": []}, entry_list)[0])
        with self.assertRaises(ValueError):
            runner_supports("local", {"capabilities": ["testing"], "max_parallel": "bad"}, task, {"tasks": []}, entry_list)

    def test_runner_command_rendering_quotes_prompt_and_model(self) -> None:
        rendered = render_runner_command("runner -m {model} {prompt}", "literal {model} && unsafe", "gpt model")
        self.assertIn("'gpt model'", rendered)
        self.assertIn("'literal {model} && unsafe'", rendered)
        with self.assertRaises(ValueError):
            render_runner_command("runner -m {model} {prompt}", "task", None)

    def test_runner_profile_normalization_preserves_legacy_shapes(self) -> None:
        self.assertEqual(parse_inline_list("[one, 'two']"), ["one", "two"])
        self.assertEqual(entry_list({"capabilities": "[implementation, testing]"}, "capabilities"), ["implementation", "testing"])
        self.assertEqual(entry_models({"model": "m1,m2,m1"}), ["m1", "m2"])

    def test_runner_resolution_handles_alias_and_default_model(self) -> None:
        self.assertEqual(split_runner_reference("local:m1"), ("local", "m1"))
        result = resolve_runner(
            "fast", None, "local", "m1", {"fast": "local:m1"},
            {"local": {"command": "run {model}", "models": ["m1"]}}, entry_models,
        )
        self.assertEqual((result[0], result[2]), ("local", "m1"))

    def test_architecture_observation_boundary_preserves_provenance_rules(self) -> None:
        observation = build_observation(
            "inferred", "convention", ["src/layout"], confidence=.7,
            rationale="folder convention", classifications={"observed", "inferred", "proposed"},
        )
        self.assertEqual(observation["classification"], "inferred")
        with self.assertRaises(ValueError):
            build_observation(
                "observed", "source", ["src/layout"], confidence=.7,
                classifications={"observed", "inferred", "proposed"},
            )

    def test_discovery_ownership_and_import_adapters_are_deterministic(self) -> None:
        self.assertEqual(owning_module(Path("src/orders/api.py"), {"src/orders": "orders"}), "orders")
        self.assertEqual(map_task_to_module({"context": None, "files": ["src/orders/api.py"]}, {"orders": {"path": "src/orders"}}), "orders")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.py"
            path.write_text("from .domain import Order\n", encoding="utf-8")
            self.assertEqual(extract_python_imports(path), [("domain", 1)])

    def test_context_query_boundary_is_deterministic(self) -> None:
        task = {"title": "Order API", "tags": ["contract"], "acceptance": ["Response is versioned"]}
        self.assertIn("order", tokenize_task(task, lambda: {"python"}))
        self.assertIn("versioned", task_text(task))
        self.assertEqual(tokenize_query("Update OrderAPI_without_cache"), {"order", "api", "cache"})

    def test_context_level_and_reference_metadata_are_pure(self) -> None:
        self.assertEqual(requested_level("fix a typo", None, None, tokenize_query), 2)
        self.assertEqual(
            requested_level("fix a typo", {"task_kind": "contract"}, None, tokenize_query),
            3,
        )
        with self.assertRaises(ValueError):
            requested_level("anything", None, 4, tokenize_query)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("hello", encoding="utf-8")
            self.assertEqual(reference_stats("README.md", root)["estimated_tokens"], 2)
            self.assertTrue(reference_stats("*.md", root)["pattern"])

    def test_runtime_config_validation_isolated_from_file_io(self) -> None:
        config = {
            "version": 1,
            "runners": {
                "default": {"name": "local", "model": "m1"},
                "profiles": {"local": {"command": "run {model}", "models": ["m1"]}},
                "aliases": {},
            },
            "automation": {
                "enabled": True,
                "planning": {"auto_execute": {"enabled": False}},
                "execution": {"mode": "sequential"},
                "quality": {"qa": {"mode": "local"}, "review": {"mode": "manual"}},
                "failure": {"qa": {"strategy": "manual"}, "review": {"strategy": "manual"}},
            },
        }
        self.assertIs(validate_runtime_config(config), config)
        self.assertEqual(config["runners"]["profiles"]["local"]["models"], ["m1"])
        with self.assertRaises(ValueError):
            validate_runtime_config({**config, "version": 2})

    def test_runtime_yaml_subset_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("version: 1\nitems:\n  - one\n  - \"two # literal\"\n", encoding="utf-8")
            parsed = load_yaml_subset(path, str)
            self.assertEqual(parsed, {"version": 1, "items": ["one", "two # literal"]})

    def test_cli_output_boundary_preserves_json_and_gate_exit_policy(self) -> None:
        self.assertEqual(json.loads(render_result({"passed": True})), {"passed": True})
        command = lambda _args: None
        self.assertEqual(exit_code_for_result({"passed": False}, command, {command}), 1)
        self.assertEqual(exit_code_for_result({"passed": False}, command, set()), 0)

    def test_worktree_identity_is_stable_and_safe(self) -> None:
        self.assertEqual(safe_git_component("workflow / task"), "workflow-task")
        self.assertEqual(safe_git_component("..."), "task")
