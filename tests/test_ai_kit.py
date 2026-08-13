"""Unit tests for the AI-Kit v2 control-plane engine (.ai/engine/ai_kit.py).

Every test runs against a throwaway temp directory: ai_kit.ROOT and the
module-level path constants derived from it are monkeypatched per test so
nothing here ever touches this repository's real .ai-work/, .ai-config/, or
.visualizer/ state.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ENGINE_DIR = Path(__file__).resolve().parents[1] / ".ai" / "engine"
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_DIR))
import ai_kit  # noqa: E402


def bash_command(script: Path, *args: str) -> list[str]:
    """Build a Git-Bash command that works with Windows drive paths too."""
    executable = shutil.which("bash") or shutil.which("bash.exe")
    if executable is None and os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidate = Path(program_files) / "Git" / "bin" / "bash.exe"
        if candidate.exists():
            executable = str(candidate)
    executable = executable or "bash"
    script_arg = script.as_posix() if os.name == "nt" else str(script)
    return [executable, script_arg, *args]


def ns(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace with the fields ai_kit's cmd_* functions expect."""
    defaults = dict(
        state=None, actor=None, detail=None, evidence=None,
        expected_revision=None, agent_id=None, context=None, epic=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def run_capture(command: object, *, cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    """Capture a child process without Windows PIPE reader threads."""
    with tempfile.TemporaryFile(mode="w+b") as stdout, tempfile.TemporaryFile(mode="w+b") as stderr:
        completed = subprocess.run(command, cwd=str(cwd) if cwd is not None else None,
                                   stdout=stdout, stderr=stderr, check=False)
        stdout.seek(0); stderr.seek(0)
        return subprocess.CompletedProcess(
            command, completed.returncode,
            stdout.read().decode("utf-8", errors="replace"),
            stderr.read().decode("utf-8", errors="replace"),
        )


class EngineTestCase(unittest.TestCase):
    """Base case: builds an isolated temp ROOT with the minimal skeleton
    validate()/role_names()/workflow_names() need, and points every
    ai_kit module-level path constant at it."""

    ROLES = ("planner", "backend", "qa", "reviewer")
    WORKFLOWS = ("feature",)
    _canonical_root: Path | None = None

    def setUp(self) -> None:
        # Windows CI can briefly lose a temporary directory while a previous
        # subprocess is releasing a handle; ensure the fixture root exists
        # before creating its skeleton.
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.root.mkdir(parents=True, exist_ok=True)
        for role in self.ROLES:
            (self.root / ".ai" / "agents" / role).mkdir(parents=True, exist_ok=True)
        for workflow in self.WORKFLOWS:
            (self.root / ".ai" / "workflows" / workflow).mkdir(parents=True, exist_ok=True)
        (self.root / ".ai-config").mkdir(parents=True, exist_ok=True)

        self._patched = {
            name: getattr(ai_kit, name)
            for name in ("ROOT", "WORK", "STATE", "CURRENT", "EVENT_LOG", "VISUALIZER_DIR", "AUTO_ARTIFACT_GENERATION")
        }
        ai_kit.ROOT = self.root
        ai_kit.WORK = self.root / ".ai-work-unused"
        ai_kit.STATE = ai_kit.WORK / "state" / "workflow.json"
        ai_kit.CURRENT = ai_kit.WORK / "state" / "current.json"
        ai_kit.EVENT_LOG = ai_kit.WORK / "logs" / "events.jsonl"
        # Artifact publication has focused integration tests. Disable automatic
        # regeneration in the lifecycle fixture so the broad engine suite stays
        # fast and never writes into this repository's real runtime workspace.
        ai_kit.VISUALIZER_DIR = self.root / ".visualizer-unused"
        ai_kit.AUTO_ARTIFACT_GENERATION = False

        self.state_file = self.root / "work" / "state" / "workflow.json"

    def tearDown(self) -> None:
        for name, value in self._patched.items():
            setattr(ai_kit, name, value)
        self._tmp.cleanup()

    def _use_canonical_kit_root(self) -> None:
        """Copy installed routing inputs into this test's Git-free root."""
        if EngineTestCase._canonical_root is None:
            canonical = Path(tempfile.mkdtemp(prefix="ai-kit-canonical-"))
            shutil.copytree(REPO_ROOT / ".ai" / "install" / "config",
                            canonical / ".ai" / "install" / "config")

            def ignore_skill_docs(_directory: str, names: list[str]) -> list[str]:
                # Routing reads metadata and entrypoints only; omitting the
                # long reference documents keeps the shared fixture small.
                keep = {"skill.meta.yaml", "SKILL.md", "overview.md"}
                return [name for name in names
                        if name not in keep and not (Path(_directory) / name).is_dir()]

            shutil.copytree(REPO_ROOT / ".ai" / "skills",
                            canonical / ".ai" / "skills",
                            ignore=ignore_skill_docs)
            shutil.copytree(REPO_ROOT / ".ai" / "agents",
                            canonical / ".ai" / "agents")
            shutil.copytree(REPO_ROOT / ".ai" / "workflows",
                            canonical / ".ai" / "workflows")
            (canonical / ".ai-config").mkdir(parents=True, exist_ok=True)
            # Keep project discovery bounded; these routing tests exercise the
            # canonical registry/skills, not source-tree scanning.
            (canonical / ".ai-config" / "kit.yaml").write_text(
                "project:\n  stack: []\n  source_dirs: []\n",
                encoding="utf-8",
            )
            EngineTestCase._canonical_root = canonical
        ai_kit.ROOT = EngineTestCase._canonical_root

    # -- helpers -----------------------------------------------------------
    def init_workflow(self, title: str = "Test workflow", workflow: str = "feature") -> None:
        ai_kit.cmd_init(ns(state=str(self.state_file), title=title, workflow=workflow,
                           actor="planner", force=False))

    def add_task(self, task_id: str, owner: str = "backend", phase: str = "build",
                 needs: list[str] | None = None, acceptance: list[str] | None = None,
                 **extra) -> dict:
        args = ns(
            state=str(self.state_file), id=task_id, title=f"Task {task_id}", owner=owner,
            phase=phase, needs=needs or [], depends_on=[],
            acceptance=[acceptance or [f"{task_id} works"]],
            files=[], tags=[], actor="planner",
        )
        for key, value in extra.items():
            setattr(args, key, value)
        return ai_kit.cmd_add_task(args)

    def transition(self, task_id: str, action: str, actor: str, **extra) -> dict:
        args = ns(state=str(self.state_file), id=task_id, action=action, actor=actor)
        for key, value in extra.items():
            setattr(args, key, value)
        return ai_kit.cmd_transition(args)

    def write_evidence(self, kind: str, task_id: str, **fields) -> str:
        payload = {"kind": kind, "task": task_id, "ts": ai_kit.now(), **fields}
        path = self.root / "evidence" / f"{kind}_{task_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def qa_evidence(self, task_id: str, status: str = "pass") -> str:
        return self.write_evidence("qa", task_id, status=status)

    def review_evidence(self, task_id: str, verdict: str = "approve") -> str:
        return self.write_evidence("review", task_id, verdict=verdict)


class StateMachineTests(EngineTestCase):
    def test_full_happy_path_reaches_done(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.transition("T1", "start", actor="backend")
        self.transition("T1", "complete", actor="backend")
        self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])
        self.transition("T1", "review-approve", actor="reviewer", evidence=[self.review_evidence("T1")])
        task = self.transition("T1", "close", actor="reviewer")
        self.assertEqual(task["status"], "done")

    def test_invalid_transition_from_todo_is_rejected(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        with self.assertRaises(ai_kit.EngineError):
            self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])

    def test_start_blocked_by_unfinished_dependency(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.add_task("T2", needs=["T1"])
        with self.assertRaises(ai_kit.EngineError):
            self.transition("T2", "start", actor="backend")

    def test_start_allowed_once_dependency_done(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.add_task("T2", needs=["T1"])
        self.transition("T1", "start", actor="backend")
        self.transition("T1", "complete", actor="backend")
        self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])
        self.transition("T1", "review-approve", actor="reviewer", evidence=[self.review_evidence("T1")])
        self.transition("T1", "close", actor="reviewer")
        task = self.transition("T2", "start", actor="backend")
        self.assertEqual(task["status"], "in-progress")

    def test_block_and_reject_require_detail(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.transition("T1", "start", actor="backend")
        with self.assertRaises(ai_kit.EngineError):
            self.transition("T1", "block", actor="backend", detail=None)
        task = self.transition("T1", "block", actor="backend", detail="waiting on infra")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["blocked_reason"], "waiting on infra")

    def test_ready_lists_only_runnable_tasks(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.add_task("T2", needs=["T1"])
        ready_ids = {item["id"] for item in ai_kit.cmd_ready(ns(state=str(self.state_file)))}
        self.assertEqual(ready_ids, {"T1"})
        self.transition("T1", "start", actor="backend")
        ready_ids = {item["id"] for item in ai_kit.cmd_ready(ns(state=str(self.state_file)))}
        self.assertEqual(ready_ids, set())


class GovernedControlPlaneTests(EngineTestCase):
    """Version-5 authority, design/contract gates, and delivery semantics."""

    def _quality_command(self, command: str = "exit 0") -> None:
        (self.root / ".ai-config" / "kit.yaml").write_text(
            f"project:\n  stack: []\nverification:\n  test_command: {command}\n",
            encoding="utf-8",
        )

    def _implementation_complete(self, task_id: str = "T1") -> dict:
        self.transition(task_id, "start", actor="backend")
        return self.transition(task_id, "complete", actor="backend")

    def test_schema_v5_new_task_and_old_task_safe_default(self) -> None:
        self.init_workflow(); task = self.add_task("T1")
        self.assertEqual(ai_kit.load(self.state_file)["version"], 5)
        self.assertEqual(task["task_kind"], "general")
        self.assertIsNotNone(task["governance_baseline"])
        legacy = ai_kit.new_state("legacy", "feature")
        legacy["version"] = 4
        legacy["tasks"] = [{key: value for key, value in task.items() if key not in {"task_kind", "required_capabilities", "contract_refs", "assignment", "governance_baseline"}}]
        ai_kit.validate(legacy)
        self.assertEqual(legacy["version"], 5)
        self.assertIsNone(legacy["tasks"][0]["governance_baseline"])

    def test_design_missing_hard_evidence_rejects_implementation(self) -> None:
        self._quality_command(); self.init_workflow()
        self.add_task("T1", task_kind="implementation")
        self._implementation_complete()
        result = ai_kit.cmd_qa_run(ns(state=str(self.state_file), id="T1"))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(ai_kit.task_map(ai_kit.load(self.state_file))["T1"]["status"], "todo")

    def test_governed_qa_review_and_not_applicable_delivery(self) -> None:
        self._quality_command(); self.init_workflow(); self.add_task("T1")
        self._implementation_complete()
        state = ai_kit.load(self.state_file); task = ai_kit.task_map(state)["T1"]
        task["assignment"] = {"runner": "executor", "model": "m1", "agent_id": "exec-1", "worktree": str(self.root), "base_commit": None}
        ai_kit.save(state, self.state_file, state["revision"])
        qa = ai_kit.cmd_qa_run(ns(state=str(self.state_file), id="T1"))
        self.assertEqual(qa["lifecycle"], "qa-passed")
        recommendation_input = self.root / "recommendation.json"
        recommendation_input.write_text(json.dumps({"task": "T1", "decision": "approve", "findings": [], "evidence": [], "runner": "reviewer", "model": "m2", "agent_id": "review-1"}), encoding="utf-8")
        ai_kit.cmd_review_submit(ns(state=str(self.state_file), id="T1", input=str(recommendation_input)))
        reviewed = ai_kit.cmd_review_apply(ns(state=str(self.state_file), id="T1", evidence=None))
        self.assertEqual(reviewed["lifecycle"], "review-approved")
        closed = ai_kit.cmd_delivery_close(ns(state=str(self.state_file), id="T1", evidence=None))
        self.assertEqual(closed["status"], "done")

    def test_worker_cannot_take_privileged_transition_after_assignment(self) -> None:
        self.init_workflow(); self.add_task("T1"); self._implementation_complete()
        state = ai_kit.load(self.state_file); ai_kit.task_map(state)["T1"]["assignment"] = {"runner": "executor", "model": "m", "agent_id": "a"}
        ai_kit.save(state, self.state_file, state["revision"])
        with self.assertRaises(ai_kit.EngineError):
            self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])

    def test_contract_approval_unlocks_implementation(self) -> None:
        contract_file = self.root / "order-api.json"; contract_file.write_text('{"type":"order"}\n', encoding="utf-8")
        ai_kit.cmd_contract_add(ns(id="order-api", version="1.0.0", owner="architect", kind="api", represents="ordering", path=str(contract_file), compatibility="backward-compatible", supersedes=None, actor="architect"))
        self.init_workflow()
        self.add_task("T1", task_kind="implementation", contract_ref=["implements:order-api@1.0.0"])
        task = ai_kit.task_map(ai_kit.load(self.state_file))["T1"]
        self.assertFalse(ai_kit.runnable(task, {"T1": task}))
        ai_kit.cmd_contract_transition(ns(id="order-api", version="1.0.0", action="propose", actor="architect", evidence=None, migration=None, confirmed_by_user=False))
        evidence = self.root / "approval.json"; evidence.write_text("{}", encoding="utf-8")
        ai_kit.cmd_contract_transition(ns(id="order-api", version="1.0.0", action="approve", actor="reviewer", evidence=str(evidence), migration=None, confirmed_by_user=False))
        self.assertTrue(ai_kit.runnable(task, {"T1": task}))

    def test_contract_registration_rejects_a_missing_source_file(self) -> None:
        missing = self.root / "contracts" / "missing.json"
        with self.assertRaisesRegex(ai_kit.EngineError, "file not found"):
            ai_kit.cmd_contract_add(ns(
                id="missing-api", version="1.0.0", owner="architect", kind="api",
                represents="missing", path=str(missing), compatibility="backward-compatible",
                supersedes=None, actor="architect",
            ))

    def test_runner_selection_is_capability_priority_and_capacity_aware(self) -> None:
        (self.root / ".ai-config" / "runners.yaml").write_text(
            "runners:\n"
            "  low:\n    command: \"true {prompt}\"\n    capabilities: [implementation]\n    roles: [backend]\n    task_kinds: [implementation]\n    priority: 10\n    max_parallel: 1\n"
            "  high:\n    command: \"true {prompt}\"\n    capabilities: [implementation, testing]\n    roles: [backend]\n    task_kinds: [implementation]\n    priority: 100\n    max_parallel: 1\n",
            encoding="utf-8",
        )
        self.init_workflow(); self.add_task("T1", task_kind="implementation", required_capability=["testing"])
        state = ai_kit.load(self.state_file); task = ai_kit.task_map(state)["T1"]
        name, _entry, _model = ai_kit._select_runner_for_task(task, state, None, None)
        self.assertEqual(name, "high")
        task["status"] = "in-progress"; task["assignment"] = {"runner": "high"}
        self.add_task("T2", task_kind="implementation", required_capability=["testing"])
        state = ai_kit.load(self.state_file)
        ai_kit.task_map(state)["T1"].update(task)
        with self.assertRaises(ai_kit.EngineError):
            ai_kit._select_runner_for_task(ai_kit.task_map(state)["T2"], state, "high", None)

    def test_design_core_override_requires_rationale(self) -> None:
        (self.root / ".ai-config" / "design-policy.json").write_text(
            json.dumps({"schema_version": 1, "project_identity": {}, "rules": [], "overrides": [{"id": "DG-MINIMAL-CHANGE", "level": "SHOULD"}]}),
            encoding="utf-8",
        )
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit._merged_design_policy()
        self.assertIn("requires rationale", str(ctx.exception))

    def test_evidence_fingerprint_changes_when_diff_content_changes(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        source = self.root / "app.py"; source.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        task = {"id": "T1", "base_commit": base, "contract_hash": "contract", "assignment": {"worktree": str(self.root), "base_commit": base}}
        source.write_text("value = 2\n", encoding="utf-8")
        first = ai_kit._evidence_fingerprint(task, self.root)
        source.write_text("value = 3\n", encoding="utf-8")
        second = ai_kit._evidence_fingerprint(task, self.root)
        self.assertEqual(first["changed_paths_hash"], second["changed_paths_hash"])
        self.assertNotEqual(first["worktree_diff_hash"], second["worktree_diff_hash"])

    def test_committing_the_verified_bytes_does_not_stale_qa_fingerprint(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        source = self.root / "app.py"
        source.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        task = {"id": "T1", "base_commit": base, "contract_hash": "contract", "assignment": {"worktree": str(self.root), "base_commit": base}}
        source.write_text("value = 2\n", encoding="utf-8")
        before_commit = ai_kit._evidence_fingerprint(task, self.root)
        subprocess.run(["git", "-C", str(self.root), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "implementation"], check=True)
        after_commit = ai_kit._evidence_fingerprint(task, self.root)
        self.assertEqual(before_commit, after_commit)

    def test_python_cache_created_by_qa_is_not_a_task_change(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        source = self.root / "backend" / "orders.py"
        source.parent.mkdir(parents=True)
        source.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "backend/orders.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        source.write_text("value = 2\n", encoding="utf-8")
        cache = self.root / "backend" / "__pycache__" / "orders.cpython-39.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"compiled")
        task = {"id": "T1", "base_commit": base, "assignment": {"worktree": str(self.root), "base_commit": base}}
        self.assertEqual(ai_kit._task_changed_paths(task, self.root), ["backend/orders.py"])

    def test_delivery_cleanup_removes_worktree_with_only_python_cache(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        source = self.root / "app.py"
        source.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        linked = self.root.parent / f"{self.root.name}-linked"
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-qb", "agent/test", str(linked)], check=True)
        cache = linked / "__pycache__" / "app.cpython-39.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"compiled")

        result = ai_kit._cleanup_task_worktree({"assignment": {"worktree": str(linked)}})

        self.assertTrue(result["removed"])
        self.assertFalse(linked.exists())

    def test_delivery_cleanup_preserves_worktree_with_source_changes(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        source = self.root / "app.py"
        source.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        linked = self.root.parent / f"{self.root.name}-linked"
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-qb", "agent/test", str(linked)], check=True)
        (linked / "notes.txt").write_text("keep me\n", encoding="utf-8")

        result = ai_kit._cleanup_task_worktree({"assignment": {"worktree": str(linked)}})

        self.assertFalse(result["removed"])
        self.assertIn("notes.txt", result["dirty_paths"])
        self.assertTrue(linked.exists())
        subprocess.run(["git", "-C", str(self.root), "worktree", "remove", "--force", str(linked)], check=True)

    def test_public_contract_activation_is_rejected(self) -> None:
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit.cmd_contract_transition(ns(id="x", version="1.0.0", action="activate", actor="architect", evidence=None, migration=None, confirmed_by_user=False))
        self.assertIn("integration", str(ctx.exception))

    def test_isolated_dispatch_uses_absolute_schema_v2_handoff(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        isolated = self.root / "linked-worktree"
        isolated.mkdir()
        runner = {
            "command": "runner {prompt}", "input": "json-file",
            "capabilities": ["implementation"], "roles": ["backend"],
            "task_kinds": ["general"],
        }
        assignment = {
            "runner": "test-runner", "model": None, "agent_id": "worker-1",
            "capabilities": ["implementation"], "branch": "agent/test/T1",
            "worktree": str(isolated), "base_commit": None,
            "assigned_at": ai_kit.now(), "state_path": str(self.state_file.resolve()),
            "claim_id": None, "lease_expires_at": None,
        }
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.object(ai_kit, "_select_runner_for_task", return_value=("test-runner", runner, None)),
            mock.patch.object(ai_kit, "_ensure_task_worktree", return_value=assignment),
            mock.patch.object(ai_kit, "cmd_route", return_value={"skills": [], "skill_details": [], "loading_instructions": []}),
            mock.patch("subprocess.run", side_effect=fake_run),
        ):
            ai_kit.cmd_dispatch(ns(state=str(self.state_file), id="T1", runner="test-runner", model=None, agent_id="worker-1"))

        handoff = ai_kit.workspace(self.state_file) / "handoffs" / "T1.json"
        self.assertEqual(json.loads(handoff.read_text(encoding="utf-8"))["schema_version"], 2)
        self.assertIn(str(handoff.resolve()), captured["command"])
        self.assertEqual(captured["cwd"], str(isolated))

    def test_new_assignment_uses_current_head_not_planning_provenance(self) -> None:
        ai_kit.ROOT = self.root / "repo"
        ai_kit.ROOT.mkdir()
        planning_base = "a" * 40
        integration_head = "b" * 40
        task = {"id": "T1", "base_commit": planning_base, "assignment": None, "claim_id": "claim-1", "claim_expires_at": "2099-01-01T00:00:00Z"}
        state = {"workflow_id": "workflow-1234"}
        runner = {"capabilities": ["implementation"]}
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if "rev-parse" in command:
                return subprocess.CompletedProcess(command, 0, stdout="true\n", stderr="")
            if "show-ref" in command:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with (
            mock.patch.object(ai_kit, "_git_head", return_value=integration_head),
            mock.patch.object(ai_kit, "_load_runners", return_value={"runner": runner}),
            mock.patch("subprocess.run", side_effect=fake_run),
        ):
            assignment = ai_kit._ensure_task_worktree(state, task, "runner", None, "agent-1", self.state_file)

        self.assertEqual(assignment["base_commit"], integration_head)
        add_command = next(command for command in commands if "worktree" in command and "add" in command)
        self.assertEqual(add_command[-1], integration_head)

    def test_git_head_skips_subprocess_outside_a_repository(self) -> None:
        with mock.patch("subprocess.run", side_effect=AssertionError("git must not run")):
            self.assertIsNone(ai_kit._git_head())

    def test_git_head_is_cached_for_the_current_repository(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        original_root = ai_kit.ROOT
        ai_kit.ROOT = repo
        try:
            with mock.patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(["git"], 0, stdout="abc123\n", stderr=""),
            ) as run:
                self.assertEqual(ai_kit._git_head(), "abc123")
                self.assertEqual(ai_kit._git_head(), "abc123")
                run.assert_called_once()
        finally:
            ai_kit.ROOT = original_root


class ShowTaskDetailTests(EngineTestCase):
    """`ai-kit show <id>` is the CLI's advertised way to debug a stuck
    lifecycle, so it must resolve one task's full detail -- not just dump
    the whole state (that stays available via `ai-kit show` with no id)."""

    def test_no_id_still_returns_whole_state(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        result = ai_kit.cmd_show(ns(state=str(self.state_file), id=None))
        self.assertIn("tasks", result)
        self.assertIn("events", result)

    def test_unknown_id_raises(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        with self.assertRaises(ai_kit.EngineError):
            ai_kit.cmd_show(ns(state=str(self.state_file), id="T99"))

    def test_known_id_returns_task_deps_acceptance_evidence_and_events(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.add_task("T2", needs=["T1"])
        self.transition("T1", "start", actor="backend")
        self.transition("T1", "complete", actor="backend")
        self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])
        self.transition("T1", "review-approve", actor="reviewer", evidence=[self.review_evidence("T1")])
        self.transition("T1", "close", actor="reviewer")

        result = ai_kit.cmd_show(ns(state=str(self.state_file), id="T1"))
        self.assertEqual(result["task"]["id"], "T1")
        self.assertEqual(result["task"]["status"], "done")
        self.assertEqual(result["needs"], [])
        self.assertEqual([d["id"] for d in result["dependents"]], ["T2"])
        self.assertEqual(result["acceptance"], ["T1 works"])
        self.assertTrue(result["evidence"])
        self.assertIn("drift", result)
        self.assertTrue(result["events"], "expected T1's own transitions in its event history")
        self.assertTrue(all(e["task"] == "T1" for e in result["events"]))
        self.assertLessEqual(len(result["events_recent"]), 10)

    def test_needs_resolves_dependency_status(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.add_task("T2", needs=["T1"])
        result = ai_kit.cmd_show(ns(state=str(self.state_file), id="T2"))
        self.assertEqual(result["needs"], [{"id": "T1", "title": "Task T1", "status": "todo"}])

    def test_cli_accepts_task_id(self) -> None:
        """Reproduces the reported bug: `ai-kit show T1` must not be
        rejected as an unrecognized argument."""
        self.init_workflow()
        self.add_task("T1")
        args = ai_kit.parser().parse_args(["--state", str(self.state_file), "show", "T1"])
        result = args.fn(args)
        self.assertEqual(result["task"]["id"], "T1")


class SeparationOfDutiesTests(EngineTestCase):
    def test_executor_cannot_qa_pass_own_work(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.transition("T1", "start", actor="backend")
        self.transition("T1", "complete", actor="backend")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            self.transition("T1", "qa-pass", actor="backend", evidence=[self.qa_evidence("T1")])
        self.assertIn("must differ from executor", str(ctx.exception))

    def test_executor_cannot_review_approve_own_work(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.transition("T1", "start", actor="backend")
        self.transition("T1", "complete", actor="backend")
        self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])
        with self.assertRaises(ai_kit.EngineError) as ctx:
            self.transition("T1", "review-approve", actor="backend",
                             evidence=[self.review_evidence("T1")])
        self.assertIn("must differ from executor", str(ctx.exception))

    def test_different_actor_may_qa_pass(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.transition("T1", "start", actor="backend")
        self.transition("T1", "complete", actor="backend")
        task = self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])
        self.assertEqual(task["status"], "qa-passed")

    def test_separation_check_compares_role_not_agent_instance_suffix(self) -> None:
        """claimed_by may carry a '#agent_id' suffix; the same *role* under a
        different agent id must still be blocked from qa-passing its own work."""
        self.init_workflow()
        self.add_task("T1")
        claim = self.transition("T1", "start", actor="backend", agent_id="worker-1")
        self.transition("T1", "complete", actor="backend", agent_id="worker-1",
                        claim_id=claim["claim_id"])
        with self.assertRaises(ai_kit.EngineError):
            self.transition("T1", "qa-pass", actor="backend", agent_id="worker-2",
                             evidence=[self.qa_evidence("T1")])


class EvidenceGateTests(EngineTestCase):
    def _to_implementation_complete(self) -> None:
        self.add_task("T1")
        self.transition("T1", "start", actor="backend")
        self.transition("T1", "complete", actor="backend")

    def test_qa_pass_requires_evidence_argument(self) -> None:
        self.init_workflow()
        self._to_implementation_complete()
        with self.assertRaises(ai_kit.EngineError) as ctx:
            self.transition("T1", "qa-pass", actor="qa", evidence=None)
        self.assertIn("requires at least one --evidence", str(ctx.exception))

    def test_qa_pass_rejects_missing_evidence_file(self) -> None:
        self.init_workflow()
        self._to_implementation_complete()
        with self.assertRaises(ai_kit.EngineError):
            self.transition("T1", "qa-pass", actor="qa",
                             evidence=[str(self.root / "nope.json")])

    def test_qa_pass_rejects_evidence_for_wrong_task(self) -> None:
        self.init_workflow()
        self._to_implementation_complete()
        self.add_task("T2")
        wrong_evidence = self.qa_evidence("T2")
        with self.assertRaises(ai_kit.EngineError):
            self.transition("T1", "qa-pass", actor="qa", evidence=[wrong_evidence])

    def test_qa_pass_rejects_non_passing_evidence(self) -> None:
        self.init_workflow()
        self._to_implementation_complete()
        failing = self.qa_evidence("T1", status="fail")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            self.transition("T1", "qa-pass", actor="qa", evidence=[failing])
        self.assertIn("not passing", str(ctx.exception))

    def test_review_approve_rejects_non_approve_verdict(self) -> None:
        self.init_workflow()
        self._to_implementation_complete()
        self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])
        rejected = self.review_evidence("T1", verdict="reject")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            self.transition("T1", "review-approve", actor="reviewer", evidence=[rejected])
        self.assertIn("not approved", str(ctx.exception))

    def test_qa_pass_rejects_non_json_evidence(self) -> None:
        self.init_workflow()
        self._to_implementation_complete()
        text_file = self.root / "notes.txt"
        text_file.write_text("looks good to me", encoding="utf-8")
        with self.assertRaises(ai_kit.EngineError):
            self.transition("T1", "qa-pass", actor="qa", evidence=[str(text_file)])


class ValidateInvariantTests(EngineTestCase):
    def test_unknown_dependency_rejected(self) -> None:
        self.init_workflow()
        with self.assertRaises(ai_kit.EngineError):
            self.add_task("T1", needs=["ghost"])

    def test_self_dependency_rejected(self) -> None:
        self.init_workflow()
        with self.assertRaises(ai_kit.EngineError):
            self.add_task("T1", needs=["T1"])

    def test_dependency_cycle_rejected(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.add_task("T2", needs=["T1"])
        # Manually rewrite state on disk to introduce a cycle (T1 <- T2 <- T1)
        # since add_task's own validate() call would refuse to create it.
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        state["tasks"][0]["needs"] = ["T2"]
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit.cmd_ready(ns(state=str(self.state_file)))
        self.assertIn("cycle", str(ctx.exception))

    def test_unknown_owner_rejected(self) -> None:
        self.init_workflow()
        with self.assertRaises(ai_kit.EngineError):
            self.add_task("T1", owner="nonexistent-role")

    def test_missing_acceptance_rejected(self) -> None:
        self.init_workflow()
        with self.assertRaises(ai_kit.EngineError):
            args = ns(state=str(self.state_file), id="T1", title="T1", owner="backend",
                       phase="build", needs=[], depends_on=[], acceptance=[[]],
                       files=[], tags=[], actor="planner")
            ai_kit.cmd_add_task(args)

    def test_g3_review_required_blocks_done_without_review_evidence(self) -> None:
        """rules.yaml's review_required gate (G3) is enforced by validate(),
        which runs at the top of every command -- so a task forced to 'done'
        without review evidence fails on the next read, not silently."""
        (self.root / ".ai-config" / "rules.yaml").write_text(
            "review_required: true\n", encoding="utf-8"
        )
        self.init_workflow()
        self.add_task("T1")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        state["tasks"][0]["status"] = "done"
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit.cmd_ready(ns(state=str(self.state_file)))
        self.assertIn("G3 review_required", str(ctx.exception))

    def test_g3_review_required_can_be_disabled_via_rules_yaml(self) -> None:
        (self.root / ".ai-config" / "rules.yaml").write_text(
            "review_required: false\n", encoding="utf-8"
        )
        self.init_workflow()
        self.add_task("T1")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        state["tasks"][0]["status"] = "done"
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        # Should not raise now that the gate is off.
        ai_kit.cmd_ready(ns(state=str(self.state_file)))


class ConcurrencyTests(EngineTestCase):
    def test_stale_expected_revision_is_rejected(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        stale_revision = state["revision"]
        # A concurrent writer bumps the revision first.
        self.add_task("T2")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            self.transition("T1", "start", actor="backend", expected_revision=stale_revision)
        self.assertIn("changed concurrently", str(ctx.exception))

    def test_matching_expected_revision_succeeds(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        current_revision = state["revision"]
        task = self.transition("T1", "start", actor="backend", expected_revision=current_revision)
        self.assertEqual(task["status"], "in-progress")

    def test_retry_transition_recovers_from_lost_race(self) -> None:
        """_retry_transition reloads state fresh on each attempt, so a caller
        racing another writer eventually succeeds instead of raising."""
        self.init_workflow()
        self.add_task("T1")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        stale_revision = state["revision"]
        self.add_task("T2")  # simulate a concurrent write that bumps the revision
        args = ns(state=str(self.state_file), id="T1", action="start", actor="backend",
                   expected_revision=stale_revision)
        with self.assertRaises(ai_kit.EngineError):
            ai_kit.cmd_transition(args)  # direct call still fails
        args.expected_revision = None
        task = ai_kit._retry_transition(args)
        self.assertEqual(task["status"], "in-progress")


class DagPayloadTests(EngineTestCase):
    """_generate_dag_payload() backs the visualizer's DAG tab: edges, layering
    (wave number), lifecycle stage, ready set, and the weighted critical path."""

    def _load_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_empty_workflow_yields_empty_dag(self) -> None:
        self.init_workflow()
        dag = ai_kit._generate_dag_payload(self._load_state())
        self.assertEqual(dag, {"tasks": [], "edges": [], "waves": 0, "ready": [], "critical_path": []})

    def test_layering_and_edges_on_a_branching_graph(self) -> None:
        # T0 -> T1 -> T2, plus a parallel T3 depending on T0 only.
        self.init_workflow()
        self.add_task("T0")
        self.add_task("T1", needs=["T0"])
        self.add_task("T2", needs=["T1"])
        self.add_task("T3", needs=["T0"])
        dag = ai_kit._generate_dag_payload(self._load_state())
        by_id = {t["id"]: t for t in dag["tasks"]}
        self.assertEqual(by_id["T0"]["layer"], 0)
        self.assertEqual(by_id["T1"]["layer"], 1)
        self.assertEqual(by_id["T2"]["layer"], 2)
        self.assertEqual(by_id["T3"]["layer"], 1)
        self.assertEqual(dag["waves"], 3)
        self.assertIn({"from": "T0", "to": "T1", "unlocked": False}, dag["edges"])
        self.assertIn({"from": "T1", "to": "T2", "unlocked": False}, dag["edges"])
        self.assertIn({"from": "T0", "to": "T3", "unlocked": False}, dag["edges"])
        self.assertEqual(dag["ready"], ["T0"])

    def test_unlocked_flips_once_upstream_is_done(self) -> None:
        self.init_workflow()
        self.add_task("T0")
        self.add_task("T1", needs=["T0"])
        self.transition("T0", "start", actor="backend")
        self.transition("T0", "complete", actor="backend")
        self.transition("T0", "qa-pass", actor="qa", evidence=[self.qa_evidence("T0")])
        self.transition("T0", "review-approve", actor="reviewer", evidence=[self.review_evidence("T0")])
        self.transition("T0", "close", actor="reviewer")
        dag = ai_kit._generate_dag_payload(self._load_state())
        edge = next(e for e in dag["edges"] if e["from"] == "T0" and e["to"] == "T1")
        self.assertTrue(edge["unlocked"])
        self.assertIn("T1", dag["ready"])
        by_id = {t["id"]: t for t in dag["tasks"]}
        self.assertEqual(by_id["T0"]["stage"], 5)
        self.assertIn("todo", by_id["T0"]["history"])
        self.assertIn("done", by_id["T0"]["history"])

    def test_blocked_task_has_no_stage_but_max_weight(self) -> None:
        self.init_workflow()
        self.add_task("T0")
        self.transition("T0", "start", actor="backend")
        self.transition("T0", "block", actor="backend", detail="waiting on infra")
        dag = ai_kit._generate_dag_payload(self._load_state())
        task = dag["tasks"][0]
        self.assertEqual(task["stage"], -1)
        self.assertEqual(task["blocked_reason"], "waiting on infra")

    def test_critical_path_prefers_chain_with_more_remaining_work(self) -> None:
        # T0 -> T1 -> T2  (long chain, all still `todo`)
        # T0 -> T3        (short chain)
        # Finishing T0 shouldn't make the short branch "critical" just
        # because it touches a completed task; the long chain still has
        # more remaining stages and should win.
        self.init_workflow()
        self.add_task("T0")
        self.add_task("T1", needs=["T0"])
        self.add_task("T2", needs=["T1"])
        self.add_task("T3", needs=["T0"])
        self.transition("T0", "start", actor="backend")
        self.transition("T0", "complete", actor="backend")
        self.transition("T0", "qa-pass", actor="qa", evidence=[self.qa_evidence("T0")])
        self.transition("T0", "review-approve", actor="reviewer", evidence=[self.review_evidence("T0")])
        self.transition("T0", "close", actor="reviewer")
        dag = ai_kit._generate_dag_payload(self._load_state())
        self.assertEqual(dag["critical_path"], ["T1", "T2"])

    def test_diamond_dependency_layers_by_longest_path(self) -> None:
        # T0 -> T1 -> T3
        # T0 -> T2 -> T3   (T3 needs both T1 and T2; longest path wins)
        self.init_workflow()
        self.add_task("T0")
        self.add_task("T1", needs=["T0"])
        self.add_task("T2", needs=["T0"])
        self.add_task("T3", needs=["T1", "T2"])
        dag = ai_kit._generate_dag_payload(self._load_state())
        by_id = {t["id"]: t for t in dag["tasks"]}
        self.assertEqual(by_id["T3"]["layer"], 2)
        self.assertEqual(dag["waves"], 3)


class VisualizerManifestTests(EngineTestCase):
    """.visualizer/artifacts.json is the schema-version manifest a consumer
    checks before parsing board/architecture/impact/events/dag.json -- see
    AGENTS.md's 'Artifact Schema Versioning'. Unlike EngineTestCase's default
    VISUALIZER_DIR (deliberately nonexistent so most tests never touch disk),
    these tests point it at a real temp directory to exercise the actual
    write path."""

    def setUp(self) -> None:
        super().setUp()
        ai_kit.VISUALIZER_DIR = self.root / ".visualizer"
        ai_kit.VISUALIZER_DIR.mkdir(parents=True, exist_ok=True)

    def _manifest(self) -> dict:
        return json.loads((ai_kit.VISUALIZER_DIR / "artifacts.json").read_text(encoding="utf-8"))

    def test_manifest_written_when_no_workflow_state_exists_yet(self) -> None:
        ai_kit._generate_visualizer_data(str(self.state_file))
        manifest = self._manifest()
        self.assertEqual(manifest["schema_version"], ai_kit.VISUALIZER_MANIFEST_SCHEMA_VERSION)
        self.assertEqual(manifest["artifacts"], ai_kit.VISUALIZER_ARTIFACT_VERSIONS)

    def test_manifest_written_alongside_real_payloads(self) -> None:
        self.init_workflow()
        self.add_task("T0")
        payloads = ai_kit._generate_visualizer_data(str(self.state_file))
        self.assertIn("artifacts.json", payloads)
        manifest = self._manifest()
        self.assertEqual(manifest["artifacts"], ai_kit.VISUALIZER_ARTIFACT_VERSIONS)

    def test_manifest_lists_exactly_the_other_generated_payloads(self) -> None:
        """Every artifact the manifest names must be a file the generator
        actually writes, and vice versa -- otherwise the manifest could drift
        from reality on either side."""
        self.init_workflow()
        payloads = ai_kit._generate_visualizer_data(str(self.state_file))
        written = set(payloads) - {"artifacts.json"}
        self.assertEqual(written, set(ai_kit.VISUALIZER_ARTIFACT_VERSIONS))

    def test_validator_rejects_missing_schema_version(self) -> None:
        with self.assertRaises(ai_kit.EngineError):
            ai_kit._validate_visualizer_manifest({"artifacts": {"dag.json": 1}})

    def test_validator_rejects_non_int_schema_version(self) -> None:
        with self.assertRaises(ai_kit.EngineError):
            ai_kit._validate_visualizer_manifest({"schema_version": "1", "artifacts": {"dag.json": 1}})

    def test_validator_rejects_empty_artifacts(self) -> None:
        with self.assertRaises(ai_kit.EngineError):
            ai_kit._validate_visualizer_manifest({"schema_version": 1, "artifacts": {}})

    def test_validator_rejects_non_int_artifact_version(self) -> None:
        with self.assertRaises(ai_kit.EngineError):
            ai_kit._validate_visualizer_manifest({"schema_version": 1, "artifacts": {"dag.json": "1"}})

    def test_validator_accepts_the_real_manifest(self) -> None:
        ai_kit._validate_visualizer_manifest(ai_kit._visualizer_manifest())


class RoutingAndSkillMetadataTests(EngineTestCase):
    def _write_skill(self, relative: str) -> None:
        skill_dir = self.root / relative
        skill_dir.mkdir(parents=True, exist_ok=True)
        docs = {
            "overview.md": "# Overview\n",
            "patterns.md": "# Patterns\n",
            "best-practices.md": "# Best\n",
            "pitfalls.md": "# Pitfalls\n",
            "examples.md": "# Examples\n",
        }
        for name, body in docs.items():
            (skill_dir / name).write_text(body, encoding="utf-8")
        (skill_dir / "skill.meta.yaml").write_text(
            "\n".join(
                [
                    f"name: {skill_dir.name}",
                    f"domain: {skill_dir.parent.name}",
                    "version: 1.0.0",
                    "status: active",
                    "owner: backend",
                    "reviewed_at: 2026-08-01",
                    "reviewers: [reviewer, backend]",
                    "depends_on: []",
                    "triggers: []",
                    "documents: [overview.md, patterns.md, best-practices.md, pitfalls.md, examples.md]",
                    "deprecated: false",
                    f"entrypoint: {relative}/overview.md",
                    f"path: {relative}",
                ]
            ) + "\n",
            encoding="utf-8",
        )

    def _write_core_skill(self, name: str) -> None:
        core_path = self.root / ".ai" / "skills" / "core" / name
        core_path.mkdir(parents=True, exist_ok=True)
        (core_path / "SKILL.md").write_text(
            "\n".join(
                [
                    "---",
                    f"name: {name}",
                    "description: test core skill",
                    "version: 1.0.0",
                    "tier: core",
                    "stack: [any]",
                    "owner: reviewer",
                    "gates: [G2]",
                    "related: []",
                    "---",
                    "",
                    f"# Skill: {name}",
                    "",
                    "## Purpose",
                    "test",
                ]
            ) + "\n",
            encoding="utf-8",
        )

    def setUp(self) -> None:
        super().setUp()
        for core_name in [
            "skill-router",
            "api-contract",
            "observability",
            "threat-modeling",
            "security-review",
            "performance-profiling",
            "test-and-validation",
            "e2e-testing",
            "integration-contracts",
            "contract-testing",
            "webhooks-and-retries",
            "architecture-decisions",
        ]:
            self._write_core_skill(core_name)
        for rel in [
            ".ai/skills/ai/openai",
            ".ai/skills/ai/llm-application",
            ".ai/skills/ai/ai-safety",
            ".ai/skills/ai/rag",
            ".ai/skills/ai/embeddings",
            ".ai/skills/ai/vector-search",
            ".ai/skills/ai/model-evaluation",
            ".ai/skills/database/pgvector",
            ".ai/skills/database/qdrant",
            ".ai/skills/frontend/vue",
        ]:
            self._write_skill(rel)

        (self.root / ".ai-config" / "kit.yaml").write_text(
            "project:\n  stack: [rag, pgvector]\n", encoding="utf-8"
        )
        (self.root / ".ai-config" / "registry.yaml").write_text(
            "\n".join(
                [
                    "owners:",
                    "  backend: [backend, database, ai]",
                    "skill_triggers:",
                    "  prompt-injection:",
                    "    match: [\"prompt injection\"]",
                    "    core_skills: [\"threat-modeling\", \"security-review\"]",
                    "    technology_skills: [\"ai/ai-safety\"]",
                    "    reason: \"Prompt attack risk\"",
                    "  rag-retrieval:",
                    "    match: [\"rag\", \"retrieval\"]",
                    "    core_skills: []",
                    "    technology_skills: [\"ai/rag\", \"ai/embeddings\", \"ai/vector-search\", \"ai/model-evaluation\"]",
                    "    reason: \"RAG path\"",
                    "  llm-model:",
                    "    match: [\"llm\"]",
                    "    core_skills: [\"performance-profiling\", \"observability\"]",
                    "    technology_skills: [\"ai/openai\", \"ai/llm-application\"]",
                    "    reason: \"LLM path\"",
                ]
            ) + "\n",
            encoding="utf-8",
        )

    def test_route_trigger_selection_and_structured_documents(self) -> None:
        self.init_workflow()
        self.add_task(
            "T1",
            owner="backend",
            tags=["rag", "pgvector", "llm"],
            acceptance=["Handle prompt injection safely"],
            files=["src/retrieval.py"],
        )
        state = ai_kit.cmd_route(ns(state=str(self.state_file), id="T1", explain=True))
        self.assertIn("skills", state)
        self.assertIn("skill_details", state)
        self.assertIn("trigger_matches", state)
        self.assertIn("explain", state)
        entries = {item["name"]: item for item in state["skill_details"]}
        self.assertIn("ai/ai-safety", entries)
        self.assertIn("ai/rag", entries)
        self.assertIn("database/pgvector", entries)
        self.assertIn("threat-modeling", entries)
        self.assertIn("security-review", entries)
        self.assertEqual(entries["ai/rag"]["documents"][0], ".ai/skills/ai/rag/overview.md")
        orders = [item["loading_order"] for item in state["skill_details"]]
        self.assertEqual(orders, sorted(orders))
        trigger_ids = {item["id"] for item in state["trigger_matches"]}
        self.assertTrue({"prompt-injection", "rag-retrieval", "llm-model"} <= trigger_ids)

    def test_route_excludes_unrelated_ai_skills_when_no_trigger(self) -> None:
        self.init_workflow()
        self.add_task("T2", owner="backend", tags=["mysql"], acceptance=["schema update"], files=["db/schema.sql"])
        payload = ai_kit.cmd_route(ns(state=str(self.state_file), id="T2", explain=False))
        names = {item["name"] for item in payload["skill_details"]}
        self.assertIn("api-contract", names)
        self.assertNotIn("ai/ai-safety", names)
        self.assertNotIn("database/qdrant", names)

    def test_handoff_payload_contains_selected_skills(self) -> None:
        self.init_workflow()
        task = self.add_task("T3", owner="backend", tags=["llm"], acceptance=["safe output"])
        route_payload = ai_kit.cmd_route(ns(state=str(self.state_file), id="T3", explain=False))
        handoff = ai_kit._write_task_handoff(
            task=task,
            route_payload=route_payload,
            state_arg=str(self.state_file),
            runner_name="dummy",
            runner={"provider": "test"},
            model="test-model",
            agent_id="agent1",
        )
        data = json.loads(Path(handoff).read_text(encoding="utf-8"))
        self.assertIn("routing", data)
        self.assertIn("skills", data["routing"])
        self.assertIn("skill_details", data["routing"])
        selected_names = {item["name"] for item in data["routing"]["skill_details"]}
        self.assertTrue({"ai/openai", "ai/llm-application"} & selected_names)


class CheckSkillsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        scripts = self.root / ".ai" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        source = Path(__file__).resolve().parents[1] / ".ai" / "scripts" / "check-skills.sh"
        shutil.copy2(source, scripts / "check-skills.sh")
        self.script = scripts / "check-skills.sh"
        self._mk_core("skill-router")
        self._mk_core("threat-modeling")
        self._mk_core("security-review")
        self._mk_core("performance-profiling")
        self._mk_core("observability")
        self._mk_core("test-and-validation")
        self._mk_core("e2e-testing")
        self._mk_core("integration-contracts")
        self._mk_core("contract-testing")
        self._mk_core("webhooks-and-retries")
        self._mk_core("architecture-decisions")
        self._mk_tech("ai", "openai")
        self._mk_tech("backend", "python")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mk_core(self, name: str, malformed: bool = False) -> None:
        path = self.root / ".ai" / "skills" / "core" / name
        path.mkdir(parents=True, exist_ok=True)
        if malformed:
            text = "---\nname: bad\n---\n"
        else:
            text = (
                "---\n"
                f"name: {name}\n"
                "description: test\n"
                "version: 1.0.0\n"
                "tier: core\n"
                "stack: [any]\n"
                "owner: reviewer\n"
                "gates: [G2]\n"
                "related: []\n"
                "---\n\n"
                f"# Skill: {name}\n"
            )
        (path / "SKILL.md").write_text(text, encoding="utf-8")

    def _mk_tech(self, domain: str, name: str, placeholder: bool = False, broken_meta: bool = False) -> None:
        path = self.root / ".ai" / "skills" / domain / name
        path.mkdir(parents=True, exist_ok=True)
        body = "PLACEHOLDER text\n" if placeholder else "# content\n"
        for doc in ["overview.md", "patterns.md", "best-practices.md", "pitfalls.md", "examples.md"]:
            (path / doc).write_text(body, encoding="utf-8")
        if broken_meta:
            meta = "name: bad\n"
        else:
            meta = (
                f"name: {name}\n"
                f"domain: {domain}\n"
                "version: 1.0.0\n"
                "status: active\n"
                "owner: backend\n"
                "reviewed_at: 2026-08-01\n"
                "reviewers: [reviewer]\n"
                "depends_on: []\n"
                "triggers: []\n"
                "documents: [overview.md, patterns.md, best-practices.md, pitfalls.md, examples.md]\n"
                "deprecated: false\n"
                f"entrypoint: .ai/skills/{domain}/{name}/overview.md\n"
                f"path: .ai/skills/{domain}/{name}\n"
            )
        (path / "skill.meta.yaml").write_text(meta, encoding="utf-8")

    def _run(self, mode: str | None = None) -> subprocess.CompletedProcess:
        args = (mode,) if mode else ()
        return run_capture(bash_command(self.script, *args), cwd=self.root)

    def test_default_mode_is_all_and_detects_placeholder(self) -> None:
        self._mk_tech("database", "redis", placeholder=True)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contains placeholder markers", result.stderr)

    def test_ai_mode_ignores_non_ai_technology_failures(self) -> None:
        self._mk_tech("database", "redis", placeholder=True)
        result = self._run("ai")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_core_mode_fails_on_malformed_front_matter(self) -> None:
        self._mk_core("release-management", malformed=True)
        result = self._run("core")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing front matter field", result.stderr)

    def test_all_mode_fails_on_broken_metadata(self) -> None:
        self._mk_tech("devops", "terraform", broken_meta=True)
        result = self._run("all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required field", result.stderr)


class SkillMetadataTests(unittest.TestCase):
    """Verify skill.meta.yaml exists and has correct content for all technology skills."""

    SKILLS_ROOT = REPO_ROOT / ".ai" / "skills"
    REQUIRED_FIELDS = ("name", "domain", "version", "owner", "reviewed_at",
                       "documents", "entrypoint", "path")

    def _tech_dirs(self) -> list[Path]:
        dirs = []
        for domain_dir in self.SKILLS_ROOT.iterdir():
            if not domain_dir.is_dir() or domain_dir.name == "core":
                continue
            for tech_dir in domain_dir.iterdir():
                if tech_dir.is_dir():
                    dirs.append(tech_dir)
        return sorted(dirs)

    def test_all_tech_skills_have_meta(self) -> None:
        missing = [d for d in self._tech_dirs() if not (d / "skill.meta.yaml").exists()]
        self.assertEqual(missing, [], f"Missing skill.meta.yaml in: {missing}")

    def test_meta_has_required_fields(self) -> None:
        for tech_dir in self._tech_dirs():
            meta = (tech_dir / "skill.meta.yaml").read_text(encoding="utf-8")
            for field in self.REQUIRED_FIELDS:
                self.assertIn(f"{field}:", meta,
                              f"{tech_dir.relative_to(REPO_ROOT)}/skill.meta.yaml missing field '{field}'")

    def test_reviewed_at_format(self) -> None:
        pattern = re.compile(r"^reviewed_at:\s*['\"]?(\d{4}-\d{2}-\d{2})['\"]?", re.MULTILINE)
        for tech_dir in self._tech_dirs():
            meta = (tech_dir / "skill.meta.yaml").read_text(encoding="utf-8")
            m = pattern.search(meta)
            self.assertIsNotNone(m, f"{tech_dir.name}/skill.meta.yaml: reviewed_at missing or wrong format")

    def test_domain_matches_directory(self) -> None:
        for tech_dir in self._tech_dirs():
            expected_domain = tech_dir.parent.name
            meta = (tech_dir / "skill.meta.yaml").read_text(encoding="utf-8")
            m = re.search(r"^domain:\s*['\"]?(\S+?)['\"]?\s*$", meta, re.MULTILINE)
            self.assertIsNotNone(m, f"{tech_dir.name}: domain field not found")
            self.assertEqual(m.group(1), expected_domain,
                             f"{tech_dir.name}: domain '{m.group(1)}' != dir '{expected_domain}'")

    def test_name_matches_directory(self) -> None:
        for tech_dir in self._tech_dirs():
            expected_name = tech_dir.name
            meta = (tech_dir / "skill.meta.yaml").read_text(encoding="utf-8")
            m = re.search(r"^name:\s*['\"]?(\S+?)['\"]?\s*$", meta, re.MULTILINE)
            self.assertIsNotNone(m, f"{tech_dir.name}: name field not found")
            self.assertEqual(m.group(1), expected_name,
                             f"{tech_dir.name}: name '{m.group(1)}' != dir '{expected_name}'")


class SkillContentTests(unittest.TestCase):
    """Verify that technology skill documents contain no placeholder markers."""

    SKILLS_ROOT = REPO_ROOT / ".ai" / "skills"
    PLACEHOLDER_PATTERN = re.compile(r"PLACEHOLDER|not yet written|generic kit template", re.IGNORECASE)

    def _tech_docs(self) -> list[Path]:
        docs = []
        for domain_dir in self.SKILLS_ROOT.iterdir():
            if not domain_dir.is_dir() or domain_dir.name == "core":
                continue
            for tech_dir in domain_dir.iterdir():
                if not tech_dir.is_dir():
                    continue
                for doc in ("overview", "patterns", "best-practices", "pitfalls", "examples"):
                    path = tech_dir / f"{doc}.md"
                    if path.exists():
                        docs.append(path)
        return sorted(docs)

    def test_no_placeholder_in_skill_docs(self) -> None:
        flagged = []
        for path in self._tech_docs():
            text = path.read_text(encoding="utf-8")
            if self.PLACEHOLDER_PATTERN.search(text):
                flagged.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(flagged, [], f"Placeholder content found in: {flagged}")


class UnterminatedListGuardTests(EngineTestCase):
    """Every YAML reader here is line-based, so a `[...]` array wrapped onto
    a second line is not unsupported-but-obvious: it is silently stored as
    the first line's raw text, and the affected trigger/role simply stops
    matching with no error. These assert it now raises instead."""

    def _registry(self, body: str) -> None:
        (self.root / ".ai-config" / "registry.yaml").write_text(body, encoding="utf-8")

    def test_wrapped_trigger_match_list_raises(self) -> None:
        self._registry(
            "skill_triggers:\n"
            "  demo:\n"
            '    match: ["one", "two",\n'
            '            "three"]\n'
            '    core_skills: ["security-review"]\n'
        )
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit._load_skill_triggers()
        self.assertIn("demo.match", str(ctx.exception))
        self.assertIn("one line", str(ctx.exception))

    def test_wrapped_owners_list_raises(self) -> None:
        """A wrapped owners list used to fail the regex and drop the role
        entirely, so that role silently routed no technology skills."""
        self._registry("owners:\n  backend: [backend,\n            database]\n")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit._load_registry()
        self.assertIn("owners.backend", str(ctx.exception))

    def test_wrapped_skill_meta_list_raises(self) -> None:
        skill_dir = self.root / ".ai" / "skills" / "backend" / "demo"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.meta.yaml").write_text(
            "name: demo\ndomain: backend\ndocuments: [overview.md,\n           patterns.md]\n",
            encoding="utf-8",
        )
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit._load_skill_metadata(skill_dir)
        self.assertIn("documents", str(ctx.exception))

    def test_single_line_lists_still_parse(self) -> None:
        self._registry(
            "skill_triggers:\n"
            "  demo:\n"
            '    match: ["one", "two", "three"]\n'
            '    core_skills: ["security-review"]\n'
        )
        triggers = ai_kit._load_skill_triggers()
        self.assertEqual(triggers["demo"]["match"], ["one", "two", "three"])


class VerificationGateTests(EngineTestCase):
    """G2 requires evidence the acceptance criteria hold. With every
    verification command left at kit.yaml's 'true' sentinel, nothing
    functional runs -- verify used to report passed=True on the strength of
    a secret scan alone, which let `pipeline` auto-approve QA, auto-approve
    review, and close the task with no functional evidence at all."""

    def _kit_yaml(self, verification: str) -> None:
        (self.root / ".ai-config" / "kit.yaml").write_text(
            f"project:\n  stack: []\n\nverification:\n{verification}", encoding="utf-8"
        )

    def _task_at_implementation_complete(self) -> None:
        self.init_workflow()
        self.add_task("T1")
        self.transition("T1", "start", actor="backend")
        self.transition("T1", "complete", actor="backend")

    def test_no_configured_checks_is_inconclusive_not_passed(self) -> None:
        self._kit_yaml(
            "  test_command: true\n  typecheck_command: true\n"
            "  build_command: true\n  lint_command: true\n"
        )
        self._task_at_implementation_complete()
        report = ai_kit.cmd_verify(ns(state=str(self.state_file), id="T1"))
        self.assertFalse(report["passed"])
        self.assertTrue(report["inconclusive"])
        self.assertIn("warning", report)
        self.assertTrue(all(c["status"] == "skipped" for c in report["checks"]
                            if c["name"].endswith("_command")))

    def test_a_configured_passing_check_is_conclusive(self) -> None:
        self._kit_yaml(
            "  test_command: true\n  typecheck_command: true\n"
            "  build_command: true\n  lint_command: exit 0\n"
        )
        self._task_at_implementation_complete()
        report = ai_kit.cmd_verify(ns(state=str(self.state_file), id="T1"))
        self.assertTrue(report["passed"])
        self.assertNotIn("inconclusive", report)

    def test_a_configured_failing_check_fails_not_inconclusive(self) -> None:
        """A real failure must stay distinguishable from 'nothing ran'."""
        self._kit_yaml(
            "  test_command: exit 1\n  typecheck_command: true\n"
            "  build_command: true\n  lint_command: true\n"
        )
        self._task_at_implementation_complete()
        report = ai_kit.cmd_verify(ns(state=str(self.state_file), id="T1"))
        self.assertFalse(report["passed"])
        self.assertNotIn("inconclusive", report)


class VerifyExitCodeTests(unittest.TestCase):
    """`ai-kit verify` must exit non-zero unless the report says passed.

    It used to exit 0 for every verdict, because main() only returns non-zero
    on EngineError and cmd_verify reports a verdict rather than raising. That
    made it useless as a shell gate: dispatch-full.sh's
    `if ! "$AI_KIT" verify ...` never fired, so a task whose checks FAILED was
    auto-approved through QA and review and closed at `done` -- the same
    vacuous-gate bug already fixed inside `pipeline`, but in the shell path.

    Driven through the real CLI (subprocess), since the exit code is the whole
    point and an in-process call to cmd_verify would not exercise it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for role in ("planner", "backend", "qa", "reviewer"):
            (self.root / ".ai" / "agents" / role).mkdir(parents=True, exist_ok=True)
        (self.root / ".ai" / "workflows" / "feature").mkdir(parents=True, exist_ok=True)
        (self.root / ".ai" / "engine").mkdir(parents=True, exist_ok=True)
        (self.root / ".ai" / "engine" / "ai_kit.py").write_bytes(
            (ENGINE_DIR / "ai_kit.py").read_bytes())
        (self.root / ".ai-config").mkdir(parents=True, exist_ok=True)
        self.state = self.root / "work" / "state" / "workflow.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return run_capture(
            [sys.executable, str(self.root / ".ai" / "engine" / "ai_kit.py"),
             "--state", str(self.state), *args],
            cwd=self.root,
        )

    def _prepare(self, verification: str) -> None:
        (self.root / ".ai-config" / "kit.yaml").write_text(
            f"project:\n  stack: []\n\nverification:\n{verification}", encoding="utf-8")
        self._run("init", "--title", "t", "--workflow", "feature", "--actor", "planner")
        self._run("add-task", "T1", "--title", "t", "--owner", "backend",
                  "--phase", "build", "--acceptance", "ok")
        self._run("transition", "T1", "start", "--actor", "backend")
        self._run("transition", "T1", "complete", "--actor", "backend")

    def test_exits_zero_when_passed(self) -> None:
        self._prepare("  test_command: exit 0\n  typecheck_command: true\n"
                      "  build_command: true\n  lint_command: true\n")
        result = self._run("verify", "T1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"passed": true', result.stdout)

    def test_exits_nonzero_when_a_check_fails(self) -> None:
        self._prepare("  test_command: exit 1\n  typecheck_command: true\n"
                      "  build_command: true\n  lint_command: true\n")
        result = self._run("verify", "T1")
        self.assertNotEqual(result.returncode, 0,
                            "verify reported FAIL but exited 0; every shell gate on it is a no-op")
        self.assertIn('"passed": false', result.stdout)

    def test_exits_nonzero_when_inconclusive(self) -> None:
        """Nothing functional ran, so there is no G2 evidence to proceed on."""
        self._prepare("  test_command: true\n  typecheck_command: true\n"
                      "  build_command: true\n  lint_command: true\n")
        result = self._run("verify", "T1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('"inconclusive": true', result.stdout)

    def test_report_is_still_printed_in_full_on_failure(self) -> None:
        """The exit code changed; the stdout contract did not."""
        self._prepare("  test_command: exit 1\n  typecheck_command: true\n"
                      "  build_command: true\n  lint_command: true\n")
        report = json.loads(self._run("verify", "T1").stdout)
        self.assertEqual(report["task"], "T1")
        self.assertTrue(report["checks"])

    def test_other_commands_still_exit_zero(self) -> None:
        """Only verify's exit status is verdict-dependent."""
        self._prepare("  test_command: exit 1\n  typecheck_command: true\n"
                      "  build_command: true\n  lint_command: true\n")
        for command in (("show",), ("ready",), ("status",), ("timeline",)):
            with self.subTest(command=command[0]):
                self.assertEqual(self._run(*command).returncode, 0)


class ContainerRuntimeDetectionTests(EngineTestCase):
    """`onboard` resolves where services -- notably the database -- actually
    run, by reading the repo.

    Whether the database is a Compose service or a host process decides where
    a migration executes and which host a connection string should point at.
    That is discoverable, so it belongs in configuration resolved once at
    onboard time rather than in a question repeated on every task.
    """

    def _write(self, name: str, body: str) -> None:
        (self.root / name).write_text(textwrap.dedent(body), encoding="utf-8")

    def test_no_compose_file_reports_no_container_database(self) -> None:
        runtime = ai_kit._detect_container_runtime()
        self.assertIsNone(runtime["compose_file"])
        self.assertFalse(runtime["database_in_compose"])
        self.assertEqual(runtime["database_services"], [])

    def test_detects_postgres_service_in_compose(self) -> None:
        self._write("docker-compose.yml", """\
            services:
              api:
                build: .
              db:
                image: postgres:16.2
            """)
        runtime = ai_kit._detect_container_runtime()
        self.assertEqual(runtime["compose_file"], "docker-compose.yml")
        self.assertTrue(runtime["database_in_compose"])
        self.assertEqual(
            runtime["database_services"],
            [{"service": "db", "image": "postgres:16.2", "technology": "postgresql"}],
        )

    def test_detects_multiple_datastores_and_alternate_filename(self) -> None:
        self._write("compose.yaml", """\
            services:
              store:
                image: mariadb:11
              cache:
                image: redis:7-alpine
            """)
        runtime = ai_kit._detect_container_runtime()
        self.assertEqual(runtime["compose_file"], "compose.yaml")
        self.assertEqual(
            sorted(s["technology"] for s in runtime["database_services"]),
            ["mysql", "redis"],
        )

    def test_app_only_compose_is_not_reported_as_a_database(self) -> None:
        self._write("docker-compose.yml", """\
            services:
              web:
                image: nginx:alpine
            """)
        runtime = ai_kit._detect_container_runtime()
        self.assertTrue(runtime["compose_file"])
        self.assertFalse(runtime["database_in_compose"])

    def test_onboard_puts_detected_runtime_into_the_stack(self) -> None:
        """The stack is what actually routes skills, so detection is only
        useful if it lands there."""
        self._write("docker-compose.yml", """\
            services:
              db:
                image: postgres:16.2
            """)
        (self.root / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        proposal = ai_kit.cmd_onboard(ns(apply=False))
        self.assertIn("docker", proposal["stack"])
        self.assertIn("compose", proposal["stack"])
        self.assertIn("postgresql", proposal["stack"])
        self.assertTrue(proposal["container_runtime"]["database_in_compose"])


class ProjectAnalyzerTests(EngineTestCase):
    """`ai-kit analyze` (Project Analyzer + Knowledge Graph Builder): a
    read-only snapshot combining onboard's stack/runtime detection with the
    module/ownership graph declared in contexts.yaml, plus static-analysis
    risk signals. Deliberately scoped to what contexts.yaml declares -- not a
    language-aware code analyzer."""

    def _add_context(self, name: str, path: str = "src/*", owner: str = "backend",
                      depends_on: list[str] | None = None, force: bool = False) -> dict:
        return ai_kit.cmd_context_add(argparse.Namespace(
            state=str(self.state_file), name=name, path=path, owner=owner,
            depends_on=depends_on, force=force,
        ))

    def _analyze(self) -> dict:
        return ai_kit.cmd_analyze(ns(state=str(self.state_file)))

    def test_empty_registry_yields_empty_graph_with_no_risks(self) -> None:
        summary = self._analyze()
        self.assertEqual(summary["modules"], {})
        self.assertEqual(summary["ownership"], {})
        self.assertEqual(summary["schema_version"], ai_kit.ANALYZE_SCHEMA_VERSION)

    def test_modules_and_ownership_reflect_registered_contexts(self) -> None:
        self._add_context("ordering", path="src/ordering/*", owner="backend")
        self._add_context("ui", path="src/ui/*", owner="frontend")
        summary = self._analyze()
        self.assertEqual(summary["modules"]["ordering"]["owner"], "backend")
        self.assertEqual(summary["modules"]["ordering"]["path"], "src/ordering/*")
        self.assertEqual(summary["ownership"]["backend"], ["ordering"])
        self.assertEqual(summary["ownership"]["frontend"], ["ui"])

    def test_dependency_appears_in_module_graph(self) -> None:
        self._add_context("core", owner="backend")
        self._add_context("api", owner="backend", depends_on=["core"])
        summary = self._analyze()
        self.assertEqual(summary["modules"]["api"]["depends_on"], ["core"])
        self.assertEqual(summary["risks"], [
            {"kind": "no_verification_command",
             "detail": "no test/lint/build command detected; verify will report inconclusive"},
        ])

    def test_unowned_context_is_flagged_as_a_risk(self) -> None:
        path = self.root / ".ai-config" / "contexts.yaml"
        path.write_text("contexts:\n  legacy:\n    path: legacy/*\n", encoding="utf-8")
        summary = self._analyze()
        risk_kinds = {r["kind"] for r in summary["risks"]}
        self.assertIn("unowned_context", risk_kinds)

    def test_dangling_dependency_is_flagged_as_a_risk(self) -> None:
        """cmd_context_add's own cycle/existence checks stop this via the
        CLI, but contexts.yaml is a hand-editable YAML file, so a
        static-analysis pass must catch a dependency left dangling by a
        manual edit -- not assume the registry was only ever built through
        the CLI."""
        path = self.root / ".ai-config" / "contexts.yaml"
        path.write_text(
            "contexts:\n  api:\n    path: src/api/*\n    owner: backend\n"
            "    depends_on: [ghost]\n",
            encoding="utf-8",
        )
        summary = self._analyze()
        dangling = [r for r in summary["risks"] if r["kind"] == "dangling_dependency"]
        self.assertEqual(len(dangling), 1)
        self.assertEqual(dangling[0]["context"], "api")

    def test_missing_verification_command_is_flagged_as_a_risk(self) -> None:
        summary = self._analyze()
        self.assertTrue(any(r["kind"] == "no_verification_command" for r in summary["risks"]))

    def test_summary_is_persisted_under_the_workspace(self) -> None:
        self._analyze()
        written = json.loads((self.root / "work" / "analysis" / "project-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(written["schema_version"], ai_kit.ANALYZE_SCHEMA_VERSION)

    def test_stack_and_container_runtime_come_from_onboard_detection(self) -> None:
        (self.root / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        summary = self._analyze()
        self.assertIn("node", summary["stack"])
        self.assertIn("dockerfile", summary["container_runtime"])


class StackSkillsRoutingTests(EngineTestCase):
    """registry.yaml's `stack_skills:` maps a skill to the stack tags that
    should select it. Nothing read it, so routing matched a technology skill
    only by its own directory name or domain -- leaving every skill whose name
    differs from its tag (docker-compose-local, nestjs-core, react-vite, ...)
    unreachable through `kit.yaml project.stack`."""

    def setUp(self) -> None:
        super().setUp()
        self._use_canonical_kit_root()

    def test_stack_skills_section_is_parsed(self) -> None:
        mapping = ai_kit._load_stack_skills()
        self.assertTrue(mapping, "stack_skills parsed as empty; the format likely changed")
        self.assertEqual(mapping[".ai/skills/devops/docker-compose-local"], ["docker", "compose"])

    def test_tag_selects_a_skill_whose_directory_name_differs(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Set up the local compose stack", owner="devops",
                      tags=["docker", "compose"])
        details = ai_kit.cmd_route(ns(state=str(self.state_file), id="T1", explain=False))
        selected = [d for d in details["skill_details"]
                    if "docker-compose-local" in d["entrypoint"]]
        self.assertTrue(selected, f"docker-compose-local not routed: {details['skills']}")
        self.assertTrue(any(r.startswith("stack:") for r in selected[0]["selection_reasons"]))

    def test_pgvector_is_not_pulled_in_by_plain_postgresql(self) -> None:
        """pgvector declared `postgresql` as one of its tags, so implementing
        stack_skills verbatim would hand a Postgres extension to every plain
        Postgres project."""
        mapping = ai_kit._load_stack_skills()
        self.assertNotIn("postgresql", mapping[".ai/skills/database/pgvector"])


class DatabaseChangeRoutingTests(EngineTestCase):
    """data-migration carries the G1 plan requirement and the G5 destructive-op
    discipline, but had no trigger: it reached only tasks already owned by the
    database role, so a backend-owned migration task got none of it."""

    def setUp(self) -> None:
        super().setUp()
        self._use_canonical_kit_root()

    def _skills(self, title: str, owner: str) -> list[str]:
        self.add_task(f"T{abs(hash(title)) % 9999}", title=title, owner=owner)
        task_id = self.last_task_id
        return ai_kit.cmd_route(ns(state=str(self.state_file), id=task_id, explain=False))["skills"]

    def add_task(self, task_id: str, **kwargs):  # type: ignore[override]
        self.last_task_id = task_id
        return super().add_task(task_id, **kwargs)

    def test_backend_owned_migration_task_gets_data_migration(self) -> None:
        self.init_workflow()
        skills = self._skills("Add a migration to drop the legacy users table", "backend")
        self.assertTrue(any("data-migration" in s for s in skills),
                        f"data-migration not routed to a backend migration task: {skills}")

    def test_backfill_and_seed_also_route(self) -> None:
        self.init_workflow()
        for title in ("Backfill the normalized_status column",
                      "Add seed data for the demo tenant"):
            with self.subTest(title=title):
                skills = self._skills(title, "backend")
                self.assertTrue(any("data-migration" in s for s in skills), skills)

    def test_unrelated_task_does_not_get_data_migration(self) -> None:
        self.init_workflow()
        skills = self._skills("Fix a typo in the order confirmation email", "backend")
        self.assertFalse(any("data-migration" in s for s in skills), skills)


class DataMigrationContentTests(unittest.TestCase):
    """The skill must tell the agent to establish the migration target from the
    repo rather than assume it (or interrogate the user about it)."""

    SKILL = REPO_ROOT / ".ai" / "skills" / "core" / "data-migration" / "SKILL.md"

    def test_covers_identifying_the_target_database(self) -> None:
        text = self.SKILL.read_text(encoding="utf-8").lower()
        for token in ("database_url", "compose", "host and port", "inside the container"):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_target_confirmation_is_in_the_checklist(self) -> None:
        text = self.SKILL.read_text(encoding="utf-8").lower()
        checklist = text.split("## checklist", 1)[1].split("##", 1)[0]
        self.assertIn("target database identified", checklist)


class LocalScriptContractTests(unittest.TestCase):
    """The helper scripts in .ai/scripts/ are the kit's local QA surface."""

    SCRIPTS = REPO_ROOT / ".ai" / "scripts"

    def test_new_task_and_next_task_are_not_duplicates(self) -> None:
        """new-task.sh used to be a byte-for-byte duplicate of next-task.sh:
        it ran `ai-kit ready`, listing existing work and creating nothing,
        despite its name."""
        new_task = (self.SCRIPTS / "new-task.sh").read_text(encoding="utf-8")
        next_task = (self.SCRIPTS / "next-task.sh").read_text(encoding="utf-8")
        self.assertNotEqual(new_task, next_task)
        self.assertIn("add-task", new_task, "new-task.sh should create a task")
        self.assertIn("ready", next_task, "next-task.sh should list ready work")

    def test_new_task_rejects_missing_arguments(self) -> None:
        result = run_capture(
            bash_command(self.SCRIPTS / "new-task.sh", "T9"),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_dispatch_full_gates_on_the_verify_verdict(self) -> None:
        """Guards the fix: the script must not treat verify's output as a
        pass without checking it."""
        script = (self.SCRIPTS / "dispatch-full.sh").read_text(encoding="utf-8")
        self.assertIn('"passed": true', script,
                      "dispatch-full.sh must inspect the verify verdict, not just its exit code")


class RealRegistryTriggerTests(unittest.TestCase):
    """Exercises the canonical install-template registry, not a synthetic fixture -- because the bug this guards against
    lives in the YAML content itself. _load_yaml_registry() is a simple
    line-based parser: a `match`/`core_skills`/`technology_skills` array
    split across multiple physical lines is silently mis-parsed into a
    single unmatchable string, with no error. A synthetic single-line
    fixture (see RoutingAndSkillMetadataTests) would never catch that
    regression, since nothing forces it to stay in sync with how someone
    actually edits the real file.
    """

    REGISTRY_FILES = (REPO_ROOT / ".ai" / "install" / "config" / "registry.yaml",)

    # AGENTS.md's mandatory-concerns table, plus the split-out AI-cost
    # trigger: each entry is (trigger id, a phrase it must match, a core
    # skill it must pull in).
    EXPECTED_TRIGGERS = [
        ("auth-security", "oauth", "security-review"),
        ("auth-security", "credential", "threat-modeling"),
        ("ui-interaction", "button", "accessibility"),
        ("ui-interaction", "accessibility", "frontend-core"),
        ("dependency-change", "upgrade", "dependency-management"),
        ("performance-latency", "latency", "performance-profiling"),
        ("performance-latency", "throughput", "observability"),
        ("ai-cost-token", "token budget", "performance-profiling"),
        ("ai-cost-token", "llm cost", "observability"),
        ("coordination-handoff", "handoff", "workflow-orchestration"),
        ("coordination-handoff", "parallel task", "workflow-orchestration"),
        ("user-journey-boundary", "user journey", "e2e-testing"),
        ("user-journey-boundary", "public api", "contract-testing"),
        ("architecture-tradeoff", "cross-cutting", "architecture-decisions"),
        ("architecture-tradeoff", "trade-off", "architecture-decisions"),
    ]

    def _load_triggers_from(self, registry_path: Path) -> dict:
        """Parse skill_triggers from an arbitrary registry.yaml using the
        engine's own real parser, by staging it where _config_path expects
        the active project's config (so this exercises the exact code path
        `route` uses in production, not a reimplementation of it)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ai-config").mkdir()
            (root / ".ai-config" / "registry.yaml").write_bytes(registry_path.read_bytes())
            saved_root = ai_kit.ROOT
            ai_kit.ROOT = root
            try:
                return ai_kit._load_skill_triggers()
            finally:
                ai_kit.ROOT = saved_root

    def test_expected_triggers_present_with_working_match_terms(self) -> None:
        for registry_path in self.REGISTRY_FILES:
            triggers = self._load_triggers_from(registry_path)
            for trigger_id, phrase, core_skill in self.EXPECTED_TRIGGERS:
                with self.subTest(registry=registry_path.relative_to(REPO_ROOT), trigger=trigger_id, phrase=phrase):
                    self.assertIn(trigger_id, triggers, f"trigger '{trigger_id}' missing from {registry_path}")
                    trigger = triggers[trigger_id]
                    self.assertIn(
                        phrase, trigger["match"],
                        f"'{phrase}' not in {trigger_id}.match -- if this trigger's YAML array was "
                        f"line-wrapped, the parser silently drops everything after the first line",
                    )
                    self.assertIn(core_skill, trigger["core_skills"])

    def test_auth_trigger_does_not_pull_in_ai_cost_skills(self) -> None:
        """Regression: a single "latency-cost-token" trigger used to match
        bare "token", so an OAuth task mentioning "token refresh" pulled in
        ai/ai-cost-management purely by accident."""
        for registry_path in self.REGISTRY_FILES:
            triggers = self._load_triggers_from(registry_path)
            auth_matches = triggers["auth-security"]["match"]
            for term in auth_matches:
                for ai_trigger_id in ("ai-cost-token",):
                    ai_terms = triggers[ai_trigger_id]["match"]
                    self.assertFalse(
                        any(term in ai_term or ai_term in term for ai_term in ai_terms),
                        f"auth-security match term '{term}' overlaps with {ai_trigger_id} "
                        f"match terms {ai_terms} in {registry_path}",
                    )

    def test_performance_latency_trigger_has_no_ai_technology_skills(self) -> None:
        """The generic Performance row in AGENTS.md's mandatory-concerns
        table does not require AI skills; only ai-cost-token (LLM-specific
        token/cost phrasing) should pull those in."""
        for registry_path in self.REGISTRY_FILES:
            triggers = self._load_triggers_from(registry_path)
            self.assertEqual(triggers["performance-latency"]["technology_skills"], [])
            self.assertTrue(triggers["ai-cost-token"]["technology_skills"])

    def test_no_dead_ai_triggers_block(self) -> None:
        """ai_triggers: was documented in skill-router/SKILL.md as live
        engine behavior ("routes the ai domain... automatically when the
        stack includes an AI technology") but no script or engine code ever
        read it -- skills-for.sh and _load_registry() both resolve AI
        routing from the static owners: list instead. Removed as dead
        config; this pins it gone so it can't quietly come back without
        someone also wiring it up."""
        for registry_path in self.REGISTRY_FILES:
            text = registry_path.read_text(encoding="utf-8")
            self.assertNotIn("ai_triggers:", text, f"dead ai_triggers: block reintroduced in {registry_path}")

    def test_ai_owner_roles_are_present_in_the_canonical_seed(self) -> None:
        owners = self._load_owners_from(self.REGISTRY_FILES[0])
        for role in ("architect", "qa", "security", "integration", "performance"):
            self.assertIn("ai", owners.get(role, []), f"role '{role}' missing ai domain")

    def _load_owners_from(self, registry_path: Path) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ai-config").mkdir()
            (root / ".ai-config" / "registry.yaml").write_bytes(registry_path.read_bytes())
            saved_root = ai_kit.ROOT
            ai_kit.ROOT = root
            try:
                return ai_kit._load_registry()["owners"]
            finally:
                ai_kit.ROOT = saved_root


class RegistryEndToEndRoutingTests(EngineTestCase):
    """Runs cmd_route against the canonical registry seed and REAL .ai/skills
    tree (only the workflow state file is isolated), so these assert what
    an actual `ai-kit route` invocation would return -- not what a
    synthetic fixture says it should."""

    def setUp(self) -> None:
        super().setUp()
        # Use a Git-free copy of the canonical kit inputs; only workflow state
        # remains test-local, so routing cannot contend with the repository's
        # Windows Git process while still exercising real registry/skills.
        self._use_canonical_kit_root()

    def _route(self, task_id: str) -> dict:
        return ai_kit.cmd_route(ns(state=str(self.state_file), id=task_id))

    def test_auth_task_routes_to_security(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Add OAuth login endpoint with token refresh", owner="backend")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("security-review" in s for s in skills), skills)
        self.assertTrue(any("threat-modeling" in s for s in skills), skills)
        self.assertFalse(any("/ai/" in s for s in skills), f"unexpected AI skills: {skills}")

    def test_ui_task_routes_to_accessibility(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Redesign checkout button and modal interaction", owner="frontend")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("accessibility" in s for s in skills), skills)

    def test_dependency_task_routes_to_dependency_management(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Bump lodash and axios to latest versions", owner="devops")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("dependency-management" in s for s in skills), skills)

    def test_generic_latency_task_does_not_pull_ai_skills(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Optimize slow dashboard query p95 latency", owner="backend")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("performance-profiling" in s for s in skills), skills)
        self.assertFalse(any("/ai/" in s for s in skills), f"unexpected AI skills: {skills}")

    def test_planner_owned_task_routes_to_requirement_decomposer(self) -> None:
        """Task decomposition is a base planner responsibility, loaded
        unconditionally like requirements-intake -- not something a keyword
        trigger should gate."""
        self.init_workflow()
        self.add_task("T1", title="Break the checkout redesign brief into tasks", owner="planner")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("requirement-decomposer" in s for s in skills), skills)
        self.assertTrue(any("requirements-intake" in s for s in skills), skills)

    def test_architect_owned_task_routes_to_system_designer(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Define module boundaries for the new billing service", owner="architect")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("system-designer" in s for s in skills), skills)

    def test_llm_cost_task_still_pulls_ai_skills(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Reduce LLM token budget and inference cost per request", owner="backend")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("ai-cost-management" in s for s in skills), skills)
        self.assertTrue(any("llm-observability" in s for s in skills), skills)

    def test_parallel_handoff_task_routes_to_workflow_orchestration(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Coordinate three parallel workers with a handoff after retry", owner="backend")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("workflow-orchestration" in s for s in skills), skills)

    def test_user_journey_task_routes_to_e2e_and_contract_testing(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Verify the checkout user journey across the public API boundary", owner="qa")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("e2e-testing" in s for s in skills), skills)
        self.assertTrue(any("contract-testing" in s for s in skills), skills)

    def test_architecture_tradeoff_task_routes_to_architecture_decisions(self) -> None:
        self.init_workflow()
        self.add_task("T1", title="Decide on a cross-cutting architectural trade-off for caching", owner="architect")
        skills = self._route("T1")["skills"]
        self.assertTrue(any("architecture-decisions" in s for s in skills), skills)

    def test_ai_owner_roles_get_ai_domain_skills_when_relevant(self) -> None:
        """The canonical registry seed assigns AI-capable roles to the AI domain."""
        self.init_workflow()
        for role in ("architect", "qa", "security", "integration", "performance"):
            registry = ai_kit._load_registry()
            self.assertIn("ai", registry["owners"].get(role, []), f"role '{role}' missing 'ai' in owners")


class PostCompletionConfigTests(EngineTestCase):
    """_load_post_completion_config / _post_completion_enabled: opt-in switch
    read from .ai-config/automation.yaml, defaulting to disabled."""

    def _write_automation(self, body: str) -> None:
        (self.root / ".ai-config" / "automation.yaml").write_text(body, encoding="utf-8")

    def test_missing_automation_yaml_defaults_disabled(self) -> None:
        self.assertFalse(ai_kit._post_completion_enabled())

    def test_missing_post_completion_section_defaults_disabled(self) -> None:
        self._write_automation("roles:\n  qa:\n    runner: x\n  reviewer:\n    runner: y\n")
        self.assertFalse(ai_kit._post_completion_enabled())

    def test_enabled_true_is_parsed(self) -> None:
        self._write_automation(
            "roles:\n  qa:\n    runner: x\n  reviewer:\n    runner: y\n"
            "post_completion:\n  enabled: true\n"
        )
        self.assertTrue(ai_kit._post_completion_enabled())

    def test_enabled_false_is_parsed(self) -> None:
        self._write_automation(
            "roles:\n  qa:\n    runner: x\n  reviewer:\n    runner: y\n"
            "post_completion:\n  enabled: false\n"
        )
        self.assertFalse(ai_kit._post_completion_enabled())

    def test_malformed_enabled_value_defaults_disabled(self) -> None:
        self._write_automation(
            "roles:\n  qa:\n    runner: x\n  reviewer:\n    runner: y\n"
            "post_completion:\n  enabled: maybe\n"
        )
        self.assertFalse(ai_kit._post_completion_enabled())


class TaskLockTests(EngineTestCase):
    """_acquire_task_lock / _release_task_lock: best-effort exclusive lock
    file used to serialize post-completion runs per task."""

    def test_acquire_then_release_allows_reacquire(self) -> None:
        lock_path = self.root / "locks" / "T1.lock"
        self.assertTrue(ai_kit._acquire_task_lock(lock_path))
        self.assertFalse(ai_kit._acquire_task_lock(lock_path))
        ai_kit._release_task_lock(lock_path)
        self.assertTrue(ai_kit._acquire_task_lock(lock_path))

    def test_release_missing_lock_is_a_noop(self) -> None:
        lock_path = self.root / "locks" / "missing.lock"
        ai_kit._release_task_lock(lock_path)  # must not raise


class AutomationRunnersTestCase(EngineTestCase):
    """Base fixture: a distinct executor/qa/reviewer runner identity in
    .ai-config/runners.yaml + automation.yaml, and a task started through to
    'implementation-complete' so _dispatch_approval/_run_post_completion/
    cmd_pipeline tests can begin from a realistic starting point."""

    def setUp(self) -> None:
        super().setUp()
        (self.root / ".ai-config" / "runners.yaml").write_text(
            "default_executor: \"executor-runner\"\n"
            "default_model: \"exec-model\"\n"
            "\n"
            "runners:\n"
            "  executor-runner:\n"
            "    command: \"true {prompt} {model}\"\n"
            "    models: [\"exec-model\"]\n"
            "  qa-runner:\n"
            "    command: \"true {prompt} {model}\"\n"
            "    models: [\"qa-model\"]\n"
            "  reviewer-runner:\n"
            "    command: \"true {prompt} {model}\"\n"
            "    models: [\"reviewer-model\"]\n",
            encoding="utf-8",
        )
        self._write_automation_roles(qa="qa-runner", qa_model="qa-model",
                                      reviewer="reviewer-runner", reviewer_model="reviewer-model")
        self.init_workflow()
        self.add_task("T1", owner="backend")
        # These classes exercise the retained v4 compatibility pipeline.
        # Governed v5 QA/review/delivery authority has dedicated tests below.
        state = ai_kit.load(self.state_file)
        ai_kit.task_map(state)["T1"]["governance_baseline"] = None
        ai_kit.save(state, self.state_file, state["revision"])
        contract_path = ai_kit.workspace(self.state_file) / "tasks" / "T1.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["governance_baseline"] = None
        contract_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")

    def _write_automation_roles(self, qa: str, qa_model: str, reviewer: str, reviewer_model: str) -> None:
        (self.root / ".ai-config" / "automation.yaml").write_text(
            "roles:\n"
            f"  qa:\n    runner: {qa}\n    model: {qa_model}\n"
            f"  reviewer:\n    runner: {reviewer}\n    model: {reviewer_model}\n",
            encoding="utf-8",
        )

    def bring_to_implementation_complete(self, task_id: str = "T1") -> None:
        self.transition(task_id, "start", actor="backend")
        self.transition(task_id, "complete", actor="backend")


class DispatchApprovalTests(AutomationRunnersTestCase):
    """_dispatch_approval: writes a handoff, shells out to the configured
    qa/reviewer runner, and requires the runner to have moved the task's
    status itself (never fabricates a verdict)."""

    def setUp(self) -> None:
        super().setUp()
        self.bring_to_implementation_complete()

    def test_qa_dispatch_success_when_runner_calls_approve(self) -> None:
        def fake_run(cmd, **kwargs):
            ai_kit.cmd_approve(ns(state=str(self.state_file), id="T1", role="qa", status="pass",
                                   reason="looks good", runner="qa-runner", model="qa-model", agent_id="a1"))
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            task = ai_kit._dispatch_approval("T1", "qa", str(self.state_file))
        self.assertEqual(task["status"], "qa-passed")

    def test_review_dispatch_success_when_runner_calls_approve(self) -> None:
        self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])

        def fake_run(cmd, **kwargs):
            ai_kit.cmd_approve(ns(state=str(self.state_file), id="T1", role="review", status="approve",
                                   reason="looks good", runner="reviewer-runner", model="reviewer-model",
                                   agent_id="a1"))
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            task = ai_kit._dispatch_approval("T1", "review", str(self.state_file))
        self.assertEqual(task["status"], "review-approved")

    def test_review_dispatch_success_when_runner_rejects(self) -> None:
        self.transition("T1", "qa-pass", actor="qa", evidence=[self.qa_evidence("T1")])

        def fake_run(cmd, **kwargs):
            ai_kit.cmd_transition(ns(state=str(self.state_file), id="T1", action="reject",
                                      actor="reviewer", detail="missing tests"))
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            task = ai_kit._dispatch_approval("T1", "review", str(self.state_file))
        self.assertEqual(task["status"], "todo")

    def test_raises_when_runner_exits_nonzero(self) -> None:
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess("x", 1)):
            with self.assertRaises(ai_kit.EngineError) as ctx:
                ai_kit._dispatch_approval("T1", "qa", str(self.state_file))
        self.assertIn("exited with code 1", str(ctx.exception))

    def test_raises_when_runner_exits_zero_without_acting(self) -> None:
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess("x", 0)):
            with self.assertRaises(ai_kit.EngineError) as ctx:
                ai_kit._dispatch_approval("T1", "qa", str(self.state_file))
        self.assertIn("must call", str(ctx.exception))

    def test_raises_for_unsupported_role(self) -> None:
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit._dispatch_approval("T1", "bogus", str(self.state_file))
        self.assertIn("unsupported approval role", str(ctx.exception))

    def test_raises_when_task_not_in_expected_status_for_role(self) -> None:
        # T1 is 'implementation-complete'; review approval expects 'qa-passed'.
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit._dispatch_approval("T1", "review", str(self.state_file))
        self.assertIn("expected qa-passed", str(ctx.exception))

    def test_raises_when_qa_identity_collides_with_executor(self) -> None:
        self._write_automation_roles(qa="executor-runner", qa_model="exec-model",
                                      reviewer="reviewer-runner", reviewer_model="reviewer-model")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit._dispatch_approval("T1", "qa", str(self.state_file))
        self.assertIn("must run under a different runner or model", str(ctx.exception))


class RunPostCompletionTests(AutomationRunnersTestCase):
    """_run_post_completion: verify -> independent QA -> independent review
    -> close, resumable and idempotent."""

    def _prepare_task_at(self, status: str, task_id: str = "T1") -> None:
        if status == "todo":
            return
        self.bring_to_implementation_complete(task_id)
        if status == "implementation-complete":
            return
        self.transition(task_id, "qa-pass", actor="qa", evidence=[self.qa_evidence(task_id)])
        if status == "qa-passed":
            return
        self.transition(task_id, "review-approve", actor="reviewer", evidence=[self.review_evidence(task_id)])
        if status == "review-approved":
            return
        self.transition(task_id, "close", actor="reviewer")

    def test_noop_when_already_done(self) -> None:
        self._prepare_task_at("done")
        result = ai_kit._run_post_completion("T1", str(self.state_file))
        self.assertEqual(result["post_completion"], "noop-already-done")

    def test_noop_when_status_not_eligible(self) -> None:
        self._prepare_task_at("todo")
        result = ai_kit._run_post_completion("T1", str(self.state_file))
        self.assertEqual(result["post_completion"], "noop-status-todo")

    def test_raises_for_unknown_task(self) -> None:
        with self.assertRaises(ai_kit.EngineError):
            ai_kit._run_post_completion("does-not-exist", str(self.state_file))

    def test_verify_failure_leaves_task_at_implementation_complete(self) -> None:
        self._prepare_task_at("implementation-complete")
        fail_report = {"task": "T1", "checks": [], "passed": False, "inconclusive": False}
        with mock.patch.object(ai_kit, "cmd_verify", return_value=fail_report):
            result = ai_kit._run_post_completion("T1", str(self.state_file))
        self.assertEqual(result["post_completion"], "verify-failed")
        task = ai_kit.task_map(ai_kit.load(self.state_file))["T1"]
        self.assertEqual(task["status"], "implementation-complete")

    def test_verify_inconclusive_is_treated_as_failure(self) -> None:
        self._prepare_task_at("implementation-complete")
        inconclusive_report = {"task": "T1", "checks": [], "passed": True, "inconclusive": True}
        with mock.patch.object(ai_kit, "cmd_verify", return_value=inconclusive_report):
            result = ai_kit._run_post_completion("T1", str(self.state_file))
        self.assertEqual(result["post_completion"], "verify-failed")
        task = ai_kit.task_map(ai_kit.load(self.state_file))["T1"]
        self.assertEqual(task["status"], "implementation-complete")

    def test_qa_dispatch_error_stops_pipeline(self) -> None:
        self._prepare_task_at("implementation-complete")
        pass_report = {"task": "T1", "checks": [], "passed": True, "inconclusive": False}
        with mock.patch.object(ai_kit, "cmd_verify", return_value=pass_report), \
             mock.patch.object(ai_kit, "_dispatch_approval", side_effect=ai_kit.EngineError("qa runner boom")):
            result = ai_kit._run_post_completion("T1", str(self.state_file))
        self.assertEqual(result["post_completion"], "qa-error")
        task = ai_kit.task_map(ai_kit.load(self.state_file))["T1"]
        self.assertEqual(task["status"], "implementation-complete")

    def test_qa_rejected_stops_before_review(self) -> None:
        self._prepare_task_at("implementation-complete")
        pass_report = {"task": "T1", "checks": [], "passed": True, "inconclusive": False}

        def fake_reject(task_id, role, state_arg, agent_id=None):
            return ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action="reject",
                                             actor="qa", detail="not good enough"))

        with mock.patch.object(ai_kit, "cmd_verify", return_value=pass_report), \
             mock.patch.object(ai_kit, "_dispatch_approval", side_effect=fake_reject):
            result = ai_kit._run_post_completion("T1", str(self.state_file))
        self.assertEqual(result["post_completion"], "qa-rejected")
        self.assertEqual(result["status"], "todo")

    def test_review_rejected_stops_before_close(self) -> None:
        self._prepare_task_at("qa-passed")

        def fake_reject_review(task_id, role, state_arg, agent_id=None):
            self.assertEqual(role, "review")
            return ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action="reject",
                                             actor="reviewer", detail="regression risk"))

        with mock.patch.object(ai_kit, "_dispatch_approval", side_effect=fake_reject_review):
            result = ai_kit._run_post_completion("T1", str(self.state_file))
        self.assertEqual(result["post_completion"], "review-rejected")
        self.assertEqual(result["status"], "todo")

    def test_full_chain_reaches_done(self) -> None:
        self._prepare_task_at("implementation-complete")
        pass_report = {"task": "T1", "checks": [], "passed": True, "inconclusive": False}

        def fake_dispatch(task_id, role, state_arg, agent_id=None):
            action = "qa-pass" if role == "qa" else "review-approve"
            actor = "qa" if role == "qa" else "reviewer"
            evidence = self.qa_evidence(task_id) if role == "qa" else self.review_evidence(task_id)
            return ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action=action, actor=actor,
                                             evidence=[evidence]))

        with mock.patch.object(ai_kit, "cmd_verify", return_value=pass_report), \
             mock.patch.object(ai_kit, "_dispatch_approval", side_effect=fake_dispatch):
            result = ai_kit._run_post_completion("T1", str(self.state_file))
        self.assertEqual(result["post_completion"], "done")
        self.assertEqual(result["status"], "done")

    def test_resumes_from_qa_passed_without_reverifying(self) -> None:
        self._prepare_task_at("qa-passed")

        def fake_dispatch(task_id, role, state_arg, agent_id=None):
            self.assertEqual(role, "review")
            return ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action="review-approve",
                                             actor="reviewer", evidence=[self.review_evidence(task_id)]))

        with mock.patch.object(ai_kit, "cmd_verify") as verify_mock, \
             mock.patch.object(ai_kit, "_dispatch_approval", side_effect=fake_dispatch):
            result = ai_kit._run_post_completion("T1", str(self.state_file))
        verify_mock.assert_not_called()
        self.assertEqual(result["post_completion"], "done")

    def test_resumes_from_review_approved_by_only_closing(self) -> None:
        self._prepare_task_at("review-approved")
        with mock.patch.object(ai_kit, "cmd_verify") as verify_mock, \
             mock.patch.object(ai_kit, "_dispatch_approval") as dispatch_mock:
            result = ai_kit._run_post_completion("T1", str(self.state_file))
        verify_mock.assert_not_called()
        dispatch_mock.assert_not_called()
        self.assertEqual(result["post_completion"], "done")
        self.assertEqual(result["status"], "done")

    def test_returns_already_running_when_lock_is_held(self) -> None:
        self._prepare_task_at("implementation-complete")
        lock_path = ai_kit._post_completion_lock_path("T1", str(self.state_file))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
        result = ai_kit._run_post_completion("T1", str(self.state_file))
        self.assertEqual(result, {"task": "T1", "post_completion": "already-running"})

    def test_lock_is_released_after_run_completes(self) -> None:
        self._prepare_task_at("done")
        ai_kit._run_post_completion("T1", str(self.state_file))
        lock_path = ai_kit._post_completion_lock_path("T1", str(self.state_file))
        self.assertFalse(lock_path.exists())


class PipelineOrchestrationTests(AutomationRunnersTestCase):
    """cmd_pipeline: dispatch -> verify -> QA -> review -> close, refusing to
    run QA/review under the same identity as the executor."""

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(state=str(self.state_file), id="T1", agent_id=None)
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_raises_when_qa_identity_collides_with_executor(self) -> None:
        self._write_automation_roles(qa="executor-runner", qa_model="exec-model",
                                      reviewer="reviewer-runner", reviewer_model="reviewer-model")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit.cmd_pipeline(self._args())
        self.assertIn("role 'qa'", str(ctx.exception))

    def test_raises_when_reviewer_identity_collides_with_executor(self) -> None:
        self._write_automation_roles(qa="qa-runner", qa_model="qa-model",
                                      reviewer="executor-runner", reviewer_model="exec-model")
        with self.assertRaises(ai_kit.EngineError) as ctx:
            ai_kit.cmd_pipeline(self._args())
        self.assertIn("role 'reviewer'", str(ctx.exception))

    def test_dispatches_executor_when_task_is_todo_then_reaches_done(self) -> None:
        def fake_dispatch(args) -> dict:
            ai_kit.cmd_transition(ns(state=args.state, id=args.id, action="start", actor="backend"))
            ai_kit.cmd_transition(ns(state=args.state, id=args.id, action="complete", actor="backend"))
            return {"task": args.id, "status": "dispatched"}

        def fake_post_completion(task_id, state_arg, agent_id=None) -> dict:
            ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action="qa-pass", actor="qa",
                                      evidence=[self.qa_evidence(task_id)]))
            ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action="review-approve", actor="reviewer",
                                      evidence=[self.review_evidence(task_id)]))
            ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action="close", actor="reviewer"))
            return {"task": task_id, "post_completion": "done", "status": "done"}

        with mock.patch.object(ai_kit, "cmd_dispatch", side_effect=fake_dispatch) as dispatch_mock, \
             mock.patch.object(ai_kit, "_run_post_completion", side_effect=fake_post_completion):
            result = ai_kit.cmd_pipeline(self._args())
        dispatch_mock.assert_called_once()
        self.assertEqual(result["status"], "done")
        self.assertIn("executor-runner/exec-model", result["executor"])
        self.assertIn("qa-runner/qa-model", result["qa"])
        self.assertIn("reviewer-runner/reviewer-model", result["reviewer"])

    def test_does_not_redispatch_when_already_past_dispatch(self) -> None:
        self.bring_to_implementation_complete()

        def fake_post_completion(task_id, state_arg, agent_id=None) -> dict:
            ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action="qa-pass", actor="qa",
                                      evidence=[self.qa_evidence(task_id)]))
            ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action="review-approve", actor="reviewer",
                                      evidence=[self.review_evidence(task_id)]))
            ai_kit.cmd_transition(ns(state=state_arg, id=task_id, action="close", actor="reviewer"))
            return {"task": task_id, "post_completion": "done", "status": "done"}

        with mock.patch.object(ai_kit, "cmd_dispatch") as dispatch_mock, \
             mock.patch.object(ai_kit, "_run_post_completion", side_effect=fake_post_completion):
            result = ai_kit.cmd_pipeline(self._args())
        dispatch_mock.assert_not_called()
        self.assertEqual(result["status"], "done")

    def test_raises_when_pipeline_stalls_before_done(self) -> None:
        self.bring_to_implementation_complete()
        with mock.patch.object(ai_kit, "_run_post_completion",
                                return_value={"post_completion": "qa-rejected", "status": "todo"}):
            with self.assertRaises(ai_kit.EngineError) as ctx:
                ai_kit.cmd_pipeline(self._args())
        self.assertIn("implementation-complete", str(ctx.exception))
        self.assertIn("qa-rejected", str(ctx.exception))


class TransitionCompletePostCompletionIntegrationTests(EngineTestCase):
    """cmd_transition('complete') only chains into _run_post_completion when
    .ai-config/automation.yaml opts in via post_completion.enabled: true."""

    def test_complete_does_not_trigger_post_completion_by_default(self) -> None:
        self.init_workflow()
        self.add_task("T1", owner="backend")
        self.transition("T1", "start", actor="backend")
        with mock.patch.object(ai_kit, "_run_post_completion") as post_mock:
            task = self.transition("T1", "complete", actor="backend")
        post_mock.assert_not_called()
        self.assertEqual(task["status"], "implementation-complete")

    def test_complete_triggers_post_completion_when_enabled(self) -> None:
        (self.root / ".ai-config" / "automation.yaml").write_text(
            "post_completion:\n  enabled: true\n", encoding="utf-8")
        self.init_workflow()
        self.add_task("T1", owner="backend")
        self.transition("T1", "start", actor="backend")
        with mock.patch.object(ai_kit, "_run_post_completion",
                                return_value={"post_completion": "noop-status-implementation-complete"}) as post_mock:
            self.transition("T1", "complete", actor="backend")
        post_mock.assert_called_once()
        self.assertEqual(post_mock.call_args.args[0], "T1")

    def test_complete_records_event_when_post_completion_raises(self) -> None:
        (self.root / ".ai-config" / "automation.yaml").write_text(
            "post_completion:\n  enabled: true\n", encoding="utf-8")
        self.init_workflow()
        self.add_task("T1", owner="backend")
        self.transition("T1", "start", actor="backend")
        with mock.patch.object(ai_kit, "_run_post_completion", side_effect=ai_kit.EngineError("boom")):
            task = self.transition("T1", "complete", actor="backend")
        self.assertEqual(task["status"], "implementation-complete")
        state = ai_kit.load(self.state_file)
        actions = [item["action"] for item in state["events"]]
        self.assertIn("post-completion-failed", actions)


if __name__ == "__main__":
    unittest.main()
