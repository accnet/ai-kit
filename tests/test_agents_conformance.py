"""Conformance tests: AGENTS.md's normative claims vs. what the engine does.

This kit's recurring failure mode is not ordinary bugs -- it is documentation
declaring a capability the engine never implements. Four separate instances
were found and fixed by hand:

  * `.ai/workflows/feature/manifest.json` declared allowed_transitions and
    gate_requirements; no code ever read the file.
  * `registry.yaml`'s `ai_triggers:` block was described in
    skill-router/SKILL.md as live stack-conditional routing; no code ever
    read it either.
  * Six of the ten rows in AGENTS.md's mandatory-concerns table had no
    trigger behind them, so those concerns never routed regardless of task
    content.
  * The install template's CI ran a test suite the installer does not ship.

AGENTS.md itself forbids exactly this ("Do not describe a prompt convention
as an engine capability without this evidence"), so the rule deserves an
executable check rather than reviewer vigilance. These tests parse the two
routing tables straight out of AGENTS.md and assert the registry actually
implements every row -- meaning a future row cannot be documented as
mandatory without an implementation behind it, and a trigger cannot be
renamed or narrowed until it silently stops firing.
"""
from __future__ import annotations

import argparse
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = REPO_ROOT / ".ai" / "engine"
sys.path.insert(0, str(ENGINE_DIR))
import ai_kit  # noqa: E402

# The root file is a convenience symlink in POSIX checkouts. Git for Windows
# may materialize it as an inaccessible reparse point when symlink privilege
# is unavailable, while the installer always copies this canonical template
# as a regular project-root file.
AGENTS_MD = REPO_ROOT / ".ai" / "install" / "AGENTS.md"
MANDATORY_MARKER = "These concerns are mandatory when their trigger is present:"
AI_MARKER = "AI trigger routing (registry-backed) is mandatory when matched by task content:"


def parse_table(marker: str) -> list[tuple[str, str]]:
    """Return [(trigger description, requirement cell)] for the markdown table
    that follows `marker` in AGENTS.md."""
    text = AGENTS_MD.read_text(encoding="utf-8")
    if marker not in text:
        raise AssertionError(f"AGENTS.md no longer contains the marker: {marker!r}")
    body = text.split(marker, 1)[1]
    rows: list[tuple[str, str]] = []
    for line in body.splitlines():
        if line.startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == 2 and not cells[0].lower().startswith("trigger"):
                rows.append((cells[0], cells[1]))
        elif rows and not line.startswith("|"):
            break
    return rows


def candidate_phrases(description: str) -> list[str]:
    """Split a table's prose trigger description into probe phrases.

    The description is prose ("Auth, untrusted input, sensitive data,
    permissions"), not a keyword list, so not every fragment is expected to
    match -- the assertion below is that at least one does.
    """
    return [p.strip().lower() for p in re.split(r",|/| or ", description) if len(p.strip()) > 2]


def skills_reachable_for(phrase: str) -> tuple[set[str], set[str]]:
    """(core skills, technology skill names) the registry routes for a phrase.

    Mirrors cmd_route's trigger matching: a trigger fires when one of its
    match terms appears in the task text.
    """
    core: set[str] = set()
    tech: set[str] = set()
    for trigger in ai_kit._load_skill_triggers().values():
        if any(term and term in phrase for term in trigger["match"]):
            core.update(trigger["core_skills"])
            tech.update(ref.split("/")[-1] for ref in trigger["technology_skills"])
    return core, tech


class MandatoryConcernsTableTests(unittest.TestCase):
    """Every row of AGENTS.md's general mandatory-concerns table must be
    reachable through the trigger registry."""

    def setUp(self) -> None:
        self.rows = parse_table(MANDATORY_MARKER)

    def test_table_is_still_parseable(self) -> None:
        """If AGENTS.md is restructured and this stops finding rows, the rest
        of the suite would pass vacuously."""
        self.assertGreaterEqual(len(self.rows), 10, f"parsed only {len(self.rows)} rows")

    def test_every_named_skill_exists(self) -> None:
        """Catches a renamed or deleted skill that the table still names."""
        for description, requirement in self.rows:
            for skill in re.findall(r"`([^`]+)`", requirement):
                with self.subTest(row=description, skill=skill):
                    self.assertTrue(
                        (REPO_ROOT / ".ai" / "skills" / "core" / skill / "SKILL.md").is_file(),
                        f"AGENTS.md requires core skill '{skill}' for '{description}', "
                        f"but .ai/skills/core/{skill}/SKILL.md does not exist",
                    )

    def test_every_row_is_reachable_through_a_trigger(self) -> None:
        """The core assertion: for each row, some phrasing drawn from the
        row's own description must route to all the skills it requires."""
        for description, requirement in self.rows:
            required = set(re.findall(r"`([^`]+)`", requirement))
            working = [p for p in candidate_phrases(description)
                       if required <= skills_reachable_for(p)[0]]
            with self.subTest(row=description):
                self.assertTrue(
                    working,
                    f"AGENTS.md declares '{description}' mandatory (requires {sorted(required)}), "
                    f"but no phrasing from that description routes to those skills. "
                    f"Add or widen a skill_triggers entry in registry.yaml, or the row is "
                    f"documentation with no implementation behind it.",
                )


class AiTriggerTableTests(unittest.TestCase):
    """Same contract for AGENTS.md's AI trigger routing table, which names
    both core skills and AI technology skills per row."""

    def setUp(self) -> None:
        self.rows = parse_table(AI_MARKER)

    def test_table_is_still_parseable(self) -> None:
        self.assertGreaterEqual(len(self.rows), 7, f"parsed only {len(self.rows)} rows")

    def test_every_row_is_reachable_through_a_trigger(self) -> None:
        for description, requirement in self.rows:
            core_part = re.search(r"Core ([^;]+)", requirement)
            ai_part = re.search(r"AI ([^;]+)", requirement)
            need_core = set(re.findall(r"`([^`]+)`", core_part.group(1))) if core_part else set()
            need_ai = set(re.findall(r"`([^`]+)`", ai_part.group(1))) if ai_part else set()
            working = []
            for phrase in candidate_phrases(description):
                core, tech = skills_reachable_for(phrase)
                # AI rows list alternatives ("openai OR llm-application"), so
                # require an intersection rather than the full set.
                if need_core <= core and (not need_ai or tech & need_ai):
                    working.append(phrase)
            with self.subTest(row=description):
                self.assertTrue(
                    working,
                    f"AGENTS.md declares AI routing for '{description}' mandatory "
                    f"(core={sorted(need_core)}, ai={sorted(need_ai)}), but no phrasing from "
                    f"that description routes to them.",
                )

    def test_ai_technology_skills_named_in_the_table_exist(self) -> None:
        ai_root = REPO_ROOT / ".ai" / "skills" / "ai"
        known = {d.name for d in ai_root.iterdir() if d.is_dir()}
        # Names appearing after "AI " in a requirement cell; skip the core ones
        # and the prose qualifiers around them.
        for description, requirement in self.rows:
            ai_part = re.search(r"AI ([^;]+)", requirement)
            if not ai_part:
                continue
            for name in re.findall(r"`([^`]+)`", ai_part.group(1)):
                if "/" in name:  # e.g. `database/pgvector`
                    continue
                with self.subTest(row=description, skill=name):
                    self.assertIn(
                        name, known,
                        f"AGENTS.md names AI skill '{name}' for '{description}', "
                        f"but .ai/skills/ai/{name}/ does not exist",
                    )


CAPABILITY_MAP_MARKER = "| Role | Capability | `ai-kit` command / artifact |"


def parse_capability_map() -> list[tuple[str, str, str]]:
    """Return [(role, capability, command/artifact cell)] for the Platform
    Capability Map table in AGENTS.md."""
    text = AGENTS_MD.read_text(encoding="utf-8")
    if CAPABILITY_MAP_MARKER not in text:
        raise AssertionError(f"AGENTS.md no longer contains the marker: {CAPABILITY_MAP_MARKER!r}")
    body = text.split(CAPABILITY_MAP_MARKER, 1)[1]
    rows: list[tuple[str, str, str]] = []
    for line in body.splitlines():
        if line.startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == 3:
                rows.append((cells[0], cells[1], cells[2]))
        elif rows and not line.startswith("|"):
            break
    return rows


def _subcommand_tree(p: object) -> dict:
    """Recursively map an argparse parser's subcommand names to their own
    subcommand trees, so 'context impact' can be validated as a chain."""
    tree: dict = {}
    for action in getattr(p, "_actions", []):
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                tree[name] = _subcommand_tree(subparser)
    return tree


def _is_command_candidate(span: str) -> bool:
    """A backtick span is a command reference if it starts with 'ai-kit ' or
    is a single bare subcommand-shaped word (e.g. a follow-on alternative like
    `dispatch-ready` in a sentence that already said 'ai-kit' once)."""
    return span.startswith("ai-kit ") or bool(re.fullmatch(r"[a-z][a-z-]*", span))


def _resolves_to_command(tree: dict, span: str) -> bool:
    """Walk leading identifier-shaped tokens (subcommand names) as a chain
    through the parser's subcommand tree, stopping at the first flag or
    placeholder token (e.g. '[--apply]', '<name>'). A word-shaped token that
    fails to match is a hard failure, not just a stopping point -- otherwise
    'context imapct <name>' would falsely "resolve" on the 'context' prefix
    alone."""
    body = span[len("ai-kit "):] if span.startswith("ai-kit ") else span
    node = tree
    matched_any = False
    for tok in body.split():
        if not re.fullmatch(r"[a-z][a-z-]*", tok):
            break
        if tok not in node:
            return False
        node = node[tok]
        matched_any = True
    return matched_any


ARTIFACT_TOKEN_RE = re.compile(r"[.\w/<>-]+\.(?:json|jsonl|html)")


class PlatformCapabilityMapTests(unittest.TestCase):
    """AGENTS.md's Platform Capability Map maps architect-role vocabulary onto
    concrete ai-kit commands and artifacts. Every command cell must name a
    subcommand that actually exists in the parser, and every artifact
    filename must be one the engine (or a checked-in visualizer file) really
    produces -- otherwise this table repeats the exact
    documented-but-unimplemented failure mode the rest of this file guards
    against, just with prettier names."""

    def setUp(self) -> None:
        self.rows = parse_capability_map()
        self.tree = _subcommand_tree(ai_kit.parser())

    def test_table_is_still_parseable(self) -> None:
        self.assertGreaterEqual(len(self.rows), 7, f"parsed only {len(self.rows)} rows")

    def test_every_command_reference_is_a_real_subcommand(self) -> None:
        for role, capability, cell in self.rows:
            for span in re.findall(r"`([^`]+)`", cell):
                if not _is_command_candidate(span):
                    continue
                with self.subTest(role=role, command=span):
                    self.assertTrue(
                        _resolves_to_command(self.tree, span),
                        f"AGENTS.md's Platform Capability Map cites '{span}' for {role!r}, "
                        f"but no such ai-kit subcommand exists in ai_kit.parser().",
                    )

    def test_every_artifact_filename_is_actually_produced(self) -> None:
        engine_source = (ENGINE_DIR / "ai_kit.py").read_text(encoding="utf-8")
        for role, capability, cell in self.rows:
            for token in ARTIFACT_TOKEN_RE.findall(cell):
                if "<" in token:  # placeholder path segment, e.g. <task-id>.json
                    continue
                with self.subTest(role=role, artifact=token):
                    if token.endswith(".html"):
                        self.assertTrue(
                            (REPO_ROOT / token).is_file(),
                            f"AGENTS.md cites artifact '{token}' for {role!r}, but that file does not exist",
                        )
                    else:
                        basename = token.rsplit("/", 1)[-1]
                        self.assertIn(
                            f'"{basename}"', engine_source,
                            f"AGENTS.md cites artifact '{token}' for {role!r}, but ai_kit.py never "
                            f"writes a file literally named {basename!r}",
                        )


class MultiStagePipelineDocsTests(unittest.TestCase):
    """AGENTS.md's 'Multi-Stage Planning Pipeline' section makes two checkable
    claims: every `ai-kit <command>` it names is real, and its description of
    `cmd_pipeline` (single task, synchronous, no cross-phase retry/resume)
    still matches the function's own docstring. Prose sections aren't caught
    by the table-parsing tests above, so this pins them the same way."""

    MARKER = "## Multi-Stage Planning Pipeline"

    def _section(self) -> str:
        text = AGENTS_MD.read_text(encoding="utf-8")
        self.assertIn(self.MARKER, text, "AGENTS.md no longer has the Multi-Stage Planning Pipeline section")
        body = text.split(self.MARKER, 1)[1]
        return body.split("\n## ", 1)[0]

    def test_every_command_reference_is_a_real_subcommand(self) -> None:
        """Unlike the Platform Capability Map table, this is free-form prose
        that also names skills ('system-designer'), statuses ('done'), and
        cross-references bare command names already introduced in full
        elsewhere ('pipeline', 'dispatch-ready') -- those aren't command
        introductions, so only a span with the explicit 'ai-kit ' prefix
        counts here; the bare-word heuristic used for the table above would
        misfire on this prose."""
        tree = _subcommand_tree(ai_kit.parser())
        section = self._section()
        for span in re.findall(r"`([^`]+)`", section):
            if not span.startswith("ai-kit "):
                continue
            with self.subTest(command=span):
                self.assertTrue(
                    _resolves_to_command(tree, span),
                    f"AGENTS.md's Multi-Stage Planning Pipeline cites '{span}', "
                    f"but no such ai-kit subcommand exists in ai_kit.parser().",
                )

    def test_pipeline_limitation_claim_matches_the_function_docstring(self) -> None:
        doc = (ai_kit.cmd_pipeline.__doc__ or "").lower()
        for phrase in ("synchronous", "retry", "resume"):
            with self.subTest(phrase=phrase):
                self.assertIn(
                    phrase, doc,
                    f"AGENTS.md describes cmd_pipeline as lacking {phrase!r}-related "
                    f"capability, but cmd_pipeline's own docstring no longer says so -- "
                    f"either the implementation changed or the doc claim is stale.",
                )


class CoreSkillsRegistryParityTests(unittest.TestCase):
    """registry.yaml's `core_skills.names` is presented as the authoritative
    core skill list, but ai_kit.py never actually reads it -- `check-skills.sh`
    discovers core skills straight off the filesystem, and `cmd_route`
    selects role-core skills from the separate hardcoded CORE_BY_ROLE dict.
    Left alone, this list is exactly the same failure mode as the dead
    `stack_skills:`/`ai_triggers:` sections fixed earlier this session, just
    quieter: nothing ever contradicts it, so it drifts the moment a skill
    directory is added or renamed without anyone noticing. Enforcing parity
    here doesn't make the list load-bearing, but it does keep it honest."""

    def test_registry_names_match_the_real_core_skill_directories(self) -> None:
        registry = ai_kit._load_registry()
        declared = set(registry["core_skills"]["names"])
        actual = {p.name for p in (REPO_ROOT / ".ai" / "skills" / "core").iterdir() if p.is_dir()}
        self.assertEqual(
            declared, actual,
            f"registry.yaml core_skills.names has drifted from .ai/skills/core/: "
            f"missing={sorted(actual - declared)} stray={sorted(declared - actual)}",
        )


class DeclaredCapabilityTests(unittest.TestCase):
    """Pins the specific documented-but-unimplemented artifacts that were
    removed, so they cannot quietly return without an implementation."""

    def test_no_workflow_manifest_json(self) -> None:
        """manifest.json declared transitions/gates the engine never read;
        TRANSITIONS in ai_kit.py is the single source of truth."""
        stray = sorted((REPO_ROOT / ".ai" / "workflows").rglob("manifest.json"))
        self.assertEqual(stray, [], f"dead workflow manifest(s) reintroduced: {stray}")

    def test_skill_router_does_not_claim_ai_triggers(self) -> None:
        """skill-router/SKILL.md described `ai_triggers` as live routing while
        no code read it."""
        doc = (REPO_ROOT / ".ai" / "skills" / "core" / "skill-router" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("ai_triggers", doc)

    def test_engine_reads_every_registry_section_agents_md_relies_on(self) -> None:
        """`skill_triggers` and `owners` are the two registry sections the
        routing tables depend on; both must actually be parsed."""
        source = (ENGINE_DIR / "ai_kit.py").read_text(encoding="utf-8")
        self.assertIn('"skill_triggers"', source)
        self.assertIn('owners:', source)


if __name__ == "__main__":
    unittest.main()
