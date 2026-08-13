"""Tests for the AI-Kit v2 skill routing and validation system.

Covers:
- skills-for.sh reads project.stack from kit.yaml by default
- Explicit stack argument overrides project.stack
- Missing / empty / malformed stack config is handled safely
- AI skills route correctly for intended roles and AI stack
- Non-AI routing remains stable
- Placeholder skill content is rejected by check-skills.sh
- Registry/path assumptions remain valid
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_FOR = REPO_ROOT / ".ai" / "scripts" / "skills-for.sh"
CHECK_SKILLS = REPO_ROOT / ".ai" / "scripts" / "check-skills.sh"
REGISTRY = REPO_ROOT / ".ai" / "install" / "config" / "registry.yaml"


def run_skills_for(args: list[str], root: Path | None = None) -> tuple[int, str]:
    """Run skills-for.sh and return (returncode, stdout).

    Pass root to override the repo root (uses SKILLS_FOR_ROOT env var).
    """
    env = os.environ.copy()
    if root is not None:
        env["SKILLS_FOR_ROOT"] = str(root)
    result = subprocess.run(
        ["bash", str(SKILLS_FOR)] + args,
        capture_output=True, text=True, env=env,
        cwd=str(root or REPO_ROOT),
    )
    return result.returncode, result.stdout.strip()


def run_check_skills(root: Path | None = None) -> tuple[int, str, str]:
    """Run check-skills.sh and return (returncode, stdout, stderr).

    Pass root to override the repo root (uses CHECK_SKILLS_ROOT env var).
    """
    env = os.environ.copy()
    if root is not None:
        env["CHECK_SKILLS_ROOT"] = str(root)
    result = subprocess.run(
        ["bash", str(CHECK_SKILLS)],
        capture_output=True, text=True, env=env,
        cwd=str(root or REPO_ROOT),
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def make_minimal_skill_tree(root: Path) -> None:
    """Create the minimal directory skeleton that scripts expect."""
    (root / ".ai-config").mkdir(parents=True, exist_ok=True)
    (root / ".ai" / "skills" / "core").mkdir(parents=True, exist_ok=True)


@unittest.skipIf(os.name == "nt", "bash subprocess tests are unreliable on the Windows runner")
class SkillsForStackRoutingTests(unittest.TestCase):
    """skills-for.sh stack resolution from kit.yaml and explicit override."""

    def test_explicit_stack_arg_selects_ai_domain(self) -> None:
        """Explicit second argument 'ai' selects the ai domain skills."""
        _, out = run_skills_for(["backend", "ai"])
        lines = out.splitlines()
        ai_lines = [l for l in lines if "/ai/" in l]
        self.assertTrue(len(ai_lines) >= 2, f"Expected >=2 ai skill lines, got: {lines}")

    def test_explicit_stack_openai_selects_openai_skill(self) -> None:
        """Explicit stack 'openai' (technology name) yields the openai overview."""
        _, out = run_skills_for(["backend", "openai"])
        self.assertIn(".ai/skills/ai/openai/overview.md", out,
                      f"Expected openai skill in output: {out!r}")

    def test_explicit_stack_rag_selects_rag_skill(self) -> None:
        """Explicit stack 'rag' (technology name) yields the rag overview."""
        _, out = run_skills_for(["backend", "rag"])
        self.assertIn(".ai/skills/ai/rag/overview.md", out,
                      f"Expected rag skill in output: {out!r}")

    def test_explicit_override_only_includes_ai_domain(self) -> None:
        """When stack='ai' is overridden, only ai domain skills appear (not other domains)."""
        _, out = run_skills_for(["backend", "ai"])
        # With override='ai', only ai domain tech skills (not backend/database)
        lines = [l for l in out.splitlines() if "/skills/" in l and "/core/" not in l]
        for line in lines:
            self.assertIn("/ai/", line, f"Unexpected non-ai skill with override='ai': {line}")

    def test_kit_yaml_stack_is_read_when_no_override(self) -> None:
        """skills-for.sh reads project.stack from a custom kit.yaml."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_minimal_skill_tree(tmp_path)
            (tmp_path / ".ai" / "skills" / "ai" / "openai").mkdir(parents=True)
            (tmp_path / ".ai" / "skills" / "ai" / "openai" / "overview.md").write_text(
                "# openai Overview\n"
            )

            (tmp_path / ".ai-config" / "kit.yaml").write_text(
                textwrap.dedent("""\
                    kit:
                      id: ai-kit-v2
                    project:
                      stack: [openai]
                      source_dirs: []
                    verification:
                      test_command: true
                """)
            )
            (tmp_path / ".ai-config" / "registry.yaml").write_text(
                textwrap.dedent("""\
                    version: 2.0.0
                    owners:
                      backend: [backend, ai]
                """)
            )

            rc, out = run_skills_for(["backend"], root=tmp_path)
            self.assertEqual(rc, 0)
            self.assertIn(".ai/skills/ai/openai/overview.md", out,
                          f"Expected openai skill in output: {out!r}")

    def test_empty_stack_in_kit_yaml_falls_back_to_role_domains(self) -> None:
        """Empty project.stack in kit.yaml falls back to registry owners."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_minimal_skill_tree(tmp_path)
            (tmp_path / ".ai" / "skills" / "backend" / "mything").mkdir(parents=True)
            (tmp_path / ".ai" / "skills" / "backend" / "mything" / "overview.md").write_text(
                "# mything\n"
            )

            (tmp_path / ".ai-config" / "kit.yaml").write_text(
                textwrap.dedent("""\
                    project:
                      stack: []
                      source_dirs: []
                """)
            )
            (tmp_path / ".ai-config" / "registry.yaml").write_text(
                textwrap.dedent("""\
                    version: 2.0.0
                    owners:
                      backend: [backend]
                """)
            )

            rc, out = run_skills_for(["backend"], root=tmp_path)
            self.assertEqual(rc, 0)
            self.assertIn(".ai/skills/backend/mything/overview.md", out,
                          f"Expected backend skill in output: {out!r}")

    def test_missing_kit_yaml_is_safe(self) -> None:
        """skills-for.sh does not crash if kit.yaml does not exist."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_minimal_skill_tree(tmp_path)
            (tmp_path / ".ai-config" / "registry.yaml").write_text(
                textwrap.dedent("""\
                    version: 2.0.0
                    owners:
                      backend: []
                """)
            )
            # No kit.yaml at all
            rc, out = run_skills_for(["backend"], root=tmp_path)
            self.assertEqual(rc, 0)

    def test_malformed_stack_in_kit_yaml_is_safe(self) -> None:
        """skills-for.sh does not crash on malformed kit.yaml content."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_minimal_skill_tree(tmp_path)
            (tmp_path / ".ai-config" / "kit.yaml").write_text(":::invalid yaml:::\n")
            (tmp_path / ".ai-config" / "registry.yaml").write_text(
                textwrap.dedent("""\
                    version: 2.0.0
                    owners:
                      backend: []
                """)
            )
            rc, out = run_skills_for(["backend"], root=tmp_path)
            self.assertEqual(rc, 0)

    def test_explicit_override_takes_priority_over_kit_yaml_stack(self) -> None:
        """Explicit second arg overrides project.stack from kit.yaml."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_minimal_skill_tree(tmp_path)
            (tmp_path / ".ai" / "skills" / "ai" / "rag").mkdir(parents=True)
            (tmp_path / ".ai" / "skills" / "ai" / "rag" / "overview.md").write_text("# rag\n")
            (tmp_path / ".ai" / "skills" / "ai" / "openai").mkdir(parents=True)
            (tmp_path / ".ai" / "skills" / "ai" / "openai" / "overview.md").write_text(
                "# openai\n"
            )

            # kit.yaml says openai; explicit arg says rag
            (tmp_path / ".ai-config" / "kit.yaml").write_text(
                textwrap.dedent("""\
                    project:
                      stack: [openai]
                """)
            )
            (tmp_path / ".ai-config" / "registry.yaml").write_text(
                textwrap.dedent("""\
                    version: 2.0.0
                    owners:
                      backend: [ai]
                """)
            )

            rc, out = run_skills_for(["backend", "rag"], root=tmp_path)
            self.assertEqual(rc, 0)
            self.assertIn(".ai/skills/ai/rag/overview.md", out,
                          f"Expected rag in output: {out!r}")
            self.assertNotIn(".ai/skills/ai/openai/overview.md", out,
                             f"openai should NOT appear when rag is explicit override: {out!r}")


@unittest.skipIf(os.name == "nt", "bash subprocess tests are unreliable on the Windows runner")
class SkillsForRoleRoutingTests(unittest.TestCase):
    """skills-for.sh role-based domain routing from the real registry.

    These assert step (c) of skills-for.sh's documented stack-resolution
    order -- "role's domain list from owners: in registry.yaml" -- which by
    definition only applies when no stack narrows the result first. They
    therefore run against a root that shares the canonical install registry and the
    REAL .ai/skills tree (so the owner mappings under test are the live
    ones) but pins project.stack to empty.

    They used to run against the repo root directly and passed only because
    this repo happened to ship `project.stack: []`. Once the kit started
    source fixture config, step (b) took priority
    and six of these broke -- correct engine behavior, an accidental test
    dependency on ambient project config.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / ".ai").mkdir(parents=True, exist_ok=True)
        (root / ".ai-config").mkdir(parents=True, exist_ok=True)
        # Share the real skill tree and registry; only project.stack differs.
        (root / ".ai" / "skills").symlink_to(REPO_ROOT / ".ai" / "skills")
        (root / ".ai-config" / "registry.yaml").write_bytes(REGISTRY.read_bytes())
        (root / ".ai-config" / "kit.yaml").write_text(
            "project:\n  stack: []\n  source_dirs: []\n", encoding="utf-8"
        )
        cls.root = root

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _for(self, role: str) -> str:
        return run_skills_for([role], root=self.root)[1]

    def test_backend_includes_ai_skills_by_default(self) -> None:
        """backend role includes ai domain by default per registry.yaml."""
        out = self._for("backend")
        self.assertIn(".ai/skills/ai/openai/overview.md", out)
        self.assertIn(".ai/skills/ai/rag/overview.md", out)

    def test_security_includes_ai_skills(self) -> None:
        """security role includes ai domain per registry.yaml owners."""
        self.assertIn(".ai/skills/ai/openai/overview.md", self._for("security"))

    def test_architect_includes_ai_skills(self) -> None:
        """architect role includes ai domain per registry.yaml owners."""
        self.assertIn(".ai/skills/ai/", self._for("architect"))

    def test_qa_includes_ai_skills(self) -> None:
        """qa role includes ai domain per registry.yaml owners."""
        self.assertIn(".ai/skills/ai/", self._for("qa"))

    def test_integration_includes_ai_skills(self) -> None:
        """integration role includes ai domain."""
        self.assertIn(".ai/skills/ai/", self._for("integration"))

    def test_performance_includes_ai_skills(self) -> None:
        """performance role includes ai domain."""
        self.assertIn(".ai/skills/ai/", self._for("performance"))

    def test_frontend_does_not_include_ai_skills(self) -> None:
        """frontend role does NOT include ai domain (unrelated)."""
        non_core_ai = [l for l in self._for("frontend").splitlines()
                       if "/ai/" in l and "/core/" not in l]
        self.assertEqual(non_core_ai, [],
                         f"frontend should not include ai skills: {non_core_ai}")

    def test_devops_does_not_include_ai_skills(self) -> None:
        """devops role does NOT include ai domain (unrelated)."""
        non_core_ai = [l for l in self._for("devops").splitlines()
                       if "/ai/" in l and "/core/" not in l]
        self.assertEqual(non_core_ai, [],
                         f"devops should not include ai skills: {non_core_ai}")

    def test_backend_core_skills_still_present(self) -> None:
        """backend core skills (api-contract, observability) remain unaffected."""
        out = self._for("backend")
        self.assertIn(".ai/skills/core/api-contract/SKILL.md", out)
        self.assertIn(".ai/skills/core/observability/SKILL.md", out)

    def test_configured_stack_takes_priority_over_role_owners(self) -> None:
        """A generated project config with a stack narrows before role owners."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ai").symlink_to(REPO_ROOT / ".ai")
            (root / ".ai-config").mkdir()
            (root / ".ai-config" / "kit.yaml").write_text(
                "project:\n  stack: [backend]\n", encoding="utf-8"
            )
            (root / ".ai-config" / "registry.yaml").write_bytes(REGISTRY.read_bytes())
            out = run_skills_for(["security"], root=root)[1]
        self.assertNotIn(".ai/skills/ai/openai/overview.md", out)
        self.assertIn(".ai/skills/core/security-review/SKILL.md", out)


@unittest.skipIf(os.name == "nt", "bash subprocess tests are unreliable on the Windows runner")
class CheckSkillsPlaceholderTests(unittest.TestCase):
    """check-skills.sh rejects placeholder content in ai domain."""

    def test_real_repo_passes_check_skills(self) -> None:
        """check-skills.sh passes on the real repository after ai content is written."""
        rc, out, _ = run_check_skills()
        self.assertEqual(rc, 0, f"check-skills.sh failed: {out}")
        self.assertIn("check-skills[all]: valid", out)

    def test_placeholder_in_ai_overview_is_rejected(self) -> None:
        """check-skills.sh rejects a placeholder marker in ai/*/overview.md."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_minimal_skill_tree(tmp_path)
            ai_dir = tmp_path / ".ai" / "skills" / "ai" / "testskill"
            ai_dir.mkdir(parents=True)

            for doc in ["overview", "patterns", "best-practices", "pitfalls", "examples"]:
                content = (
                    "⚠️ PLACEHOLDER — not yet written for testskill.\n"
                    if doc == "overview"
                    else "Real content here.\n"
                )
                (ai_dir / f"{doc}.md").write_text(f"# testskill {doc}\n\n{content}")
            (ai_dir / "skill.meta.yaml").write_text(
                "name: testskill\n"
                "domain: ai\n"
                "version: 1.0.0\n"
                "owner: backend\n"
                "reviewed_at: 2026-01-01\n"
                "entrypoint: .ai/skills/ai/testskill/overview.md\n"
                "path: .ai/skills/ai/testskill\n"
                "documents: [overview.md, patterns.md, best-practices.md, pitfalls.md, examples.md]\n"
            )

            rc, out, err = run_check_skills(root=tmp_path)
            self.assertNotEqual(rc, 0, "check-skills.sh should fail on placeholder ai skill")
            self.assertIn("placeholder", (out + err).lower())

    def test_non_placeholder_ai_skill_passes(self) -> None:
        """check-skills.sh passes for an ai skill with real content and valid metadata."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_minimal_skill_tree(tmp_path)
            ai_dir = tmp_path / ".ai" / "skills" / "ai" / "testskill"
            ai_dir.mkdir(parents=True)

            for doc in ["overview", "patterns", "best-practices", "pitfalls", "examples"]:
                (ai_dir / f"{doc}.md").write_text(
                    f"# testskill {doc}\n\nProduction-ready guidance here.\n"
                )
            (ai_dir / "skill.meta.yaml").write_text(
                "name: testskill\n"
                "domain: ai\n"
                "version: 1.0.0\n"
                "owner: backend\n"
                "reviewed_at: 2026-01-01\n"
                "entrypoint: .ai/skills/ai/testskill/overview.md\n"
                "path: .ai/skills/ai/testskill\n"
                "documents: [overview.md, patterns.md, best-practices.md, pitfalls.md, examples.md]\n"
            )

            rc, out, _ = run_check_skills(root=tmp_path)
            self.assertEqual(rc, 0, f"check-skills.sh failed unexpectedly: {out}")

    def test_placeholder_in_non_ai_skill_is_rejected(self) -> None:
        """Placeholder markers outside the ai domain also fail check-skills.sh."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_minimal_skill_tree(tmp_path)
            other_dir = tmp_path / ".ai" / "skills" / "backend" / "somephp"
            other_dir.mkdir(parents=True)

            for doc in ["overview", "patterns", "best-practices", "pitfalls", "examples"]:
                (other_dir / f"{doc}.md").write_text(
                    f"# somephp {doc}\n\n"
                    "⚠️ PLACEHOLDER — not yet written.\n"
                    "Real guidance pending.\n"
                )
            (other_dir / "skill.meta.yaml").write_text(
                "name: somephp\n"
                "domain: backend\n"
                "version: 1.0.0\n"
                "owner: backend\n"
                "reviewed_at: 2026-01-01\n"
                "entrypoint: .ai/skills/backend/somephp/overview.md\n"
                "path: .ai/skills/backend/somephp\n"
                "documents: [overview.md, patterns.md, best-practices.md, pitfalls.md, examples.md]\n"
            )

            rc, out, err = run_check_skills(root=tmp_path)
            self.assertNotEqual(rc, 0,
                                f"Placeholder in non-ai skill should also fail: {out}")
            self.assertIn("placeholder", (out + err).lower())


class RegistryValidityTests(unittest.TestCase):
    """Registry paths and assumptions remain valid."""

    def test_registry_yaml_exists(self) -> None:
        self.assertTrue(REGISTRY.exists(), "registry.yaml must exist")

    def test_ai_domain_skills_exist(self) -> None:
        """Both ai skill directories exist with all required documents."""
        ai_skills_root = REPO_ROOT / ".ai" / "skills" / "ai"
        self.assertTrue(ai_skills_root.is_dir())
        for tech in ["openai", "rag"]:
            tech_dir = ai_skills_root / tech
            self.assertTrue(tech_dir.is_dir(), f"ai/{tech} skill directory must exist")
            for doc in ["overview", "patterns", "best-practices", "pitfalls", "examples"]:
                path = tech_dir / f"{doc}.md"
                self.assertTrue(path.is_file(), f"{path} must exist")
                self.assertGreater(path.stat().st_size, 100,
                                   f"{path} appears to be too small")

    def test_ai_skills_have_no_placeholder_markers(self) -> None:
        """AI skill files must not contain placeholder markers."""
        ai_skills_root = REPO_ROOT / ".ai" / "skills" / "ai"
        for md_file in sorted(ai_skills_root.rglob("*.md")):
            content = md_file.read_text(encoding="utf-8").lower()
            for marker in ["placeholder", "not yet written", "generic kit template"]:
                self.assertNotIn(
                    marker, content,
                    f"Placeholder marker '{marker}' found in "
                    f"{md_file.relative_to(REPO_ROOT)}"
                )

    def test_registry_owners_include_ai_for_expected_roles(self) -> None:
        """registry.yaml lists ai in owners for architect, backend, security,
        integration, performance, qa."""
        registry_text = REGISTRY.read_text(encoding="utf-8")
        expected_ai_roles = ["architect", "backend", "security", "integration",
                              "performance", "qa"]
        for role in expected_ai_roles:
            found = False
            for line in registry_text.splitlines():
                stripped = line.strip()
                if stripped.startswith(f"{role}:") and "ai" in stripped:
                    found = True
                    break
            self.assertTrue(
                found,
                f"Role '{role}' should have 'ai' in its owners list in registry.yaml"
            )

    def test_skill_router_skill_md_references_kit_yaml(self) -> None:
        """skill-router/SKILL.md documents kit.yaml stack resolution."""
        skill_router = REPO_ROOT / ".ai" / "skills" / "core" / "skill-router" / "SKILL.md"
        content = skill_router.read_text(encoding="utf-8")
        self.assertIn("kit.yaml", content,
                      "skill-router/SKILL.md should document kit.yaml stack resolution")


if __name__ == "__main__":
    unittest.main()
