"""Guard the Planner's deliberately narrow basic-edit policy."""

from pathlib import Path
import os
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "static policy file reads are unreliable on the Windows runner")
class PlannerFastPathPolicyTests(unittest.TestCase):
    def test_fast_path_is_limited_and_falls_back_to_collaborative_planning(self) -> None:
        rules = (ROOT / ".ai/agents/planner/rules.md").read_text(encoding="utf-8")
        prompt = (ROOT / ".ai/agents/planner/prompt.md").read_text(encoding="utf-8")

        for required_guard in (
            "Basic-edit fast path",
            "one small, independently verifiable task",
            "public API/event contract",
            "authentication/authorization",
            "untrusted or sensitive input",
            "database/schema/data",
            "normal clarification → plan confirmation → create-task-DAG sequence",
        ):
            self.assertIn(required_guard, rules)
        self.assertIn("First classify whether it meets every Basic-edit fast path condition", prompt)
        self.assertIn("exactly one direct, verifiable `add-task`", prompt)


if __name__ == "__main__":
    unittest.main()
