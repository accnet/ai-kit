"""Contract tests between the engine's visualizer payloads and the pages that read them.

`.visualizer/dag.html` is plain JS reading `dag.json` by field name, so a
rename or drop on the engine side breaks the page silently -- nothing
errors, the graph just renders blank or without a feature. These tests
scrape the field names the page actually dereferences and assert the engine
still emits every one of them, in both the repo copy and the install
template copy that ships to new projects.

Deliberately dependency-free (no browser, no JS engine) so it runs in the
same `python -m unittest discover -s tests` step as everything else. The
browser-level behaviour is covered separately by tests/test_dag_browser.py.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = REPO_ROOT / ".ai" / "engine"
sys.path.insert(0, str(ENGINE_DIR))
import ai_kit  # noqa: E402

DAG_PAGES = (
    REPO_ROOT / ".visualizer" / "dag.html",
    REPO_ROOT / ".ai" / "install" / "templates" / ".visualizer" / "dag.html",
)


def sample_state() -> dict:
    """A minimal but structurally complete workflow: one done task unlocking
    a blocked one and a ready one, so every payload field is populated."""
    return {
        "version": 2, "revision": 5, "title": "contract", "workflow": "feature",
        "created_at": "2026-01-01T00:00:00Z",
        "phases": [],
        "events": [
            {"ts": "2026-01-01T00:00:00Z", "action": "add-task", "task": "T1", "actor": "planner", "from": None, "to": "todo", "detail": ""},
            {"ts": "2026-01-01T00:01:00Z", "action": "close", "task": "T1", "actor": "reviewer", "from": "review-approved", "to": "done", "detail": ""},
            {"ts": "2026-01-01T00:02:00Z", "action": "add-task", "task": "T2", "actor": "planner", "from": None, "to": "todo", "detail": ""},
            {"ts": "2026-01-01T00:03:00Z", "action": "block", "task": "T3", "actor": "backend", "from": "todo", "to": "blocked", "detail": "waiting"},
        ],
        "tasks": [
            {"id": "T1", "title": "done upstream", "owner": "planner", "phase": "plan",
             "needs": [], "status": "done", "acceptance": ["a"], "files": [], "tags": [],
             "attempts": 1, "evidence": [], "blocked_reason": None, "claimed_by": "planner",
             "context": "core", "epic": "e1", "base_commit": None, "context_revision": 1,
             "epic_revision": 1, "depends_on": [], "contract_hashes": {},
             "upstream_context_revisions": {}},
            {"id": "T2", "title": "ready downstream", "owner": "backend", "phase": "build",
             "needs": ["T1"], "status": "todo", "acceptance": ["a"], "files": [], "tags": [],
             "attempts": 0, "evidence": [], "blocked_reason": None, "claimed_by": None,
             "context": "api", "epic": "e1", "base_commit": None, "context_revision": 1,
             "epic_revision": 1, "depends_on": [], "contract_hashes": {},
             "upstream_context_revisions": {}},
            {"id": "T3", "title": "blocked one", "owner": "backend", "phase": "build",
             "needs": ["T2"], "status": "blocked", "acceptance": ["a"], "files": [], "tags": [],
             "attempts": 1, "evidence": [], "blocked_reason": "waiting", "claimed_by": "backend",
             "context": "api", "epic": "e1", "base_commit": None, "context_revision": 1,
             "epic_revision": 1, "depends_on": [], "contract_hashes": {},
             "upstream_context_revisions": {}},
        ],
    }


def scrape(page: str, pattern: str) -> set[str]:
    return set(re.findall(pattern, page))


class DagPayloadContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = ai_kit._generate_dag_payload(sample_state())

    def test_pages_exist_and_are_identical(self) -> None:
        """The install template must not drift from the repo's own copy, or
        newly-installed projects silently get an older DAG page."""
        for page in DAG_PAGES:
            self.assertTrue(page.is_file(), f"missing DAG page: {page}")
        first, second = (p.read_text(encoding="utf-8") for p in DAG_PAGES)
        self.assertEqual(first, second, ".visualizer/dag.html and its install template copy differ")

    def test_page_fetches_dag_json(self) -> None:
        page = DAG_PAGES[0].read_text(encoding="utf-8")
        self.assertIn("fetch('dag.json'", page)

    def test_every_top_level_field_the_page_reads_is_emitted(self) -> None:
        page = DAG_PAGES[0].read_text(encoding="utf-8")
        referenced = scrape(page, r"\bdata\.([a-z_]+)")
        self.assertTrue(referenced, "scraped no data.<field> references; the scrape pattern is stale")
        missing = referenced - set(self.payload)
        self.assertFalse(missing, f"dag.html reads payload fields the engine does not emit: {sorted(missing)}")

    def test_every_task_field_the_page_reads_is_emitted(self) -> None:
        page = DAG_PAGES[0].read_text(encoding="utf-8")
        referenced = scrape(page, r"\bt\.([a-z_]+)") | scrape(page, r"\btask\.([a-z_]+)")
        self.assertTrue(referenced, "scraped no task field references; the scrape pattern is stale")
        emitted = set(self.payload["tasks"][0])
        missing = referenced - emitted
        self.assertFalse(missing, f"dag.html reads task fields the engine does not emit: {sorted(missing)}")

    def test_every_edge_field_the_page_reads_is_emitted(self) -> None:
        page = DAG_PAGES[0].read_text(encoding="utf-8")
        referenced = scrape(page, r"\be\.(from|to|unlocked)\b")
        self.assertTrue(referenced, "scraped no edge field references; the scrape pattern is stale")
        emitted = set(self.payload["edges"][0])
        missing = referenced - emitted
        self.assertFalse(missing, f"dag.html reads edge fields the engine does not emit: {sorted(missing)}")

    def test_payload_shape_is_stable(self) -> None:
        """Pins the payload's own contract, so a field the page doesn't
        happen to read today still can't be dropped without a test failing."""
        self.assertEqual(
            set(self.payload),
            {"tasks", "edges", "waves", "ready", "critical_path"},
        )
        self.assertEqual(
            set(self.payload["tasks"][0]),
            {"id", "title", "owner", "context", "epic", "phase", "status",
             "stage", "needs", "layer", "ready", "blocked_reason", "history",
             "task_kind", "assignment", "contract_refs"},
        )
        self.assertEqual(set(self.payload["edges"][0]), {"from", "to", "unlocked"})

    def test_sample_state_exercises_every_render_branch(self) -> None:
        """Guards the fixture itself: if it stops covering done/ready/blocked,
        the contract tests above would pass on a payload missing real values."""
        statuses = {t["status"] for t in self.payload["tasks"]}
        self.assertIn("done", statuses)
        self.assertIn("blocked", statuses)
        self.assertEqual(self.payload["ready"], ["T2"])
        self.assertTrue(any(e["unlocked"] for e in self.payload["edges"]))
        self.assertTrue(any(not e["unlocked"] for e in self.payload["edges"]))
        blocked = next(t for t in self.payload["tasks"] if t["status"] == "blocked")
        self.assertEqual(blocked["stage"], -1)
        self.assertTrue(any(t["history"] for t in self.payload["tasks"]))


class VisualizerPayloadKeysTests(unittest.TestCase):
    """The generator writes one file per payload key; index.html/app.js and
    dag.html fetch those exact filenames."""

    def test_generator_declares_dag_json(self) -> None:
        source = (ENGINE_DIR / "ai_kit.py").read_text(encoding="utf-8")
        self.assertIn('"dag.json"', source)

    def test_index_html_exposes_a_dag_tab(self) -> None:
        for index in (REPO_ROOT / ".visualizer" / "index.html",
                      REPO_ROOT / ".ai" / "install" / "templates" / ".visualizer" / "index.html"):
            markup = index.read_text(encoding="utf-8")
            self.assertIn('data-view="dag"', markup, f"{index} has no DAG tab")
            self.assertIn('id="viewDag"', markup, f"{index} has no DAG view panel")
            self.assertIn('src="dag.html"', markup, f"{index} does not embed dag.html")

    def test_dashboard_exposes_project_and_contract_views(self) -> None:
        for index in (REPO_ROOT / ".visualizer" / "index.html",
                      REPO_ROOT / ".ai" / "install" / "templates" / ".visualizer" / "index.html"):
            markup = index.read_text(encoding="utf-8")
            for view in ("project", "contracts"):
                self.assertIn(f'data-view="{view}"', markup)
                self.assertIn(f'id="view{view.title()}"', markup)
            self.assertIn('id="contractGraph"', markup)
        app = (REPO_ROOT / ".visualizer" / "app.js").read_text(encoding="utf-8")
        self.assertIn("contract-lifecycle", app)
        for relation in ("producer/consumer/verifier", "edge.relation"):
            self.assertIn(relation, app)

    def test_app_is_manifest_first_with_complete_bundle_and_legacy_fallback(self) -> None:
        app = (REPO_ROOT / ".visualizer" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/artifacts/project/manifest.json", app)
        self.assertIn("payload.generation_id !== manifest.generation_id", app)
        for filename in ai_kit.ARTIFACT_PAYLOAD_FILES:
            self.assertIn(filename, app)
        self.assertIn("loadLegacyArtifacts", app)
        self.assertIn("if (hasLoaded) return false", app)

    def test_dag_is_canonical_first_with_legacy_fallback(self) -> None:
        page = (REPO_ROOT / ".visualizer" / "dag.html").read_text(encoding="utf-8")
        canonical = page.index("/artifacts/project/manifest.json")
        legacy = page.index("fetch('dag.json'")
        self.assertLess(canonical, legacy)
        self.assertIn("artifact.generation_id !== manifest.generation_id", page)


if __name__ == "__main__":
    unittest.main()
