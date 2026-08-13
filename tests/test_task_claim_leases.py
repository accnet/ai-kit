from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ai_kit_claims", ROOT / ".ai/engine/ai_kit.py")
assert SPEC and SPEC.loader
ENGINE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(ENGINE)

if os.name == "nt":
    raise unittest.SkipTest("temporary lease state fixtures are unreliable on the Windows runner")


class TaskClaimLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state" / "workflow.json"
        state = ENGINE.new_state("claims", "feature")
        state["tasks"] = [{"id":"T1","title":"claim","owner":"backend","phase":"build","needs":[],"status":"todo","acceptance":["safe"],"files":[],"tags":[],"attempts":0,"evidence":[],"blocked_reason":None,"claimed_by":None,"context":None,"epic":None,"base_commit":None,"context_revision":None,"epic_revision":None,"upstream_context_revisions":{},"depends_on":[],"contract_hashes":{},"contract_revision":None,"contract_hash":None,"superseded_by":None}]
        ENGINE.sync_phases(state); ENGINE.save(state, self.state)

    def tearDown(self): self.temp.cleanup()

    def transition(self, action, agent=None, claim=None):
        args = argparse.Namespace(state=str(self.state), id="T1", action=action, actor="backend", detail="reason", evidence=None, expected_revision=None, agent_id=agent, claim_id=claim, by=None)
        with patch.object(ENGINE, "_post_completion_enabled", return_value=False):
            return ENGINE.cmd_transition(args)

    def test_claim_blocks_wrong_executor_and_allows_matching_lease(self):
        claimed = self.transition("start", "agent-a")
        self.assertTrue(claimed["claim_id"])
        with self.assertRaisesRegex(ENGINE.EngineError, "active --claim-id"):
            self.transition("complete", "agent-b", claimed["claim_id"])
        with self.assertRaisesRegex(ENGINE.EngineError, "active --claim-id"):
            self.transition("complete", "agent-a", "wrong")
        completed = self.transition("complete", "agent-a", claimed["claim_id"])
        self.assertEqual(completed["status"], "implementation-complete")

    def test_expired_lease_can_be_reclaimed(self):
        claimed = self.transition("start", "agent-a")
        state = ENGINE.load(self.state); state["tasks"][0]["claim_expires_at"] = "2000-01-01T00:00:00Z"; ENGINE.save(state, self.state, state["revision"])
        reclaimed = self.transition("reclaim", "agent-b")
        self.assertNotEqual(reclaimed["claim_id"], claimed["claim_id"])
        self.assertEqual(reclaimed["claimed_by"], "backend#agent-b")


if __name__ == "__main__": unittest.main()
