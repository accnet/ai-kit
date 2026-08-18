"""Installer contract for project-owned AI-Kit configuration.

The source kit tracks only `.ai/install/config/`.  Its installer materializes
that directory as `.ai-config/` in a consuming project and must never require
or recreate a source-repository `.ai-config/` tree.
"""
from __future__ import annotations

import argparse
import re
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / ".ai" / "engine"))
import ai_kit  # noqa: E402

TEMPLATE_CONFIG = REPO_ROOT / ".ai" / "install" / "config"
LIVE_VISUALIZER = REPO_ROOT / ".visualizer"
TEMPLATE_VISUALIZER = REPO_ROOT / ".ai" / "install" / "templates" / ".visualizer"

PROJECT_OWNED_CONFIGS = ("contexts.yaml", "epics.yaml")
EXPECTED_CONFIGS = {
    "config.yaml", "contexts.yaml", "epics.yaml", "kit.yaml",
    "registry.yaml", "rules.yaml", "design-policy.json", "contracts.json", "delivery.json",
    "architecture.json", "architecture-fitness.json", "truth.yaml",
}


def run_capture(command, *, cwd=None):
    """Capture installer subprocess output without Windows PIPE reader threads."""
    with tempfile.TemporaryFile(mode="w+b") as stdout, tempfile.TemporaryFile(mode="w+b") as stderr:
        completed = subprocess.run(command, cwd=cwd, stdout=stdout, stderr=stderr, check=False)
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(
            command,
            completed.returncode,
            stdout.read().decode("utf-8", errors="replace"),
            stderr.read().decode("utf-8", errors="replace"),
        )

@unittest.skipIf(os.name == "nt", "installer subprocess/tree fixtures are unreliable on the Windows runner")
class InstallConfigTests(unittest.TestCase):
    def test_source_repository_has_no_project_config_directory(self) -> None:
        self.assertFalse((REPO_ROOT / ".ai-config").exists())

    def test_cursor_adapter_is_not_shipped(self) -> None:
        self.assertFalse((REPO_ROOT / ".cursor").exists())
        self.assertFalse((REPO_ROOT / ".ai" / "install" / "templates" / ".cursor").exists())
        for path in (REPO_ROOT / "AGENTS.md", REPO_ROOT / ".ai" / "install" / "AGENTS.md"):
            with self.subTest(document=path):
                self.assertNotIn("Cursor", path.read_text(encoding="utf-8"))

    def test_templates_are_the_complete_canonical_seed_set(self) -> None:
        self.assertEqual({p.name for p in TEMPLATE_CONFIG.iterdir() if p.is_file()}, EXPECTED_CONFIGS)

    def test_installer_materializes_project_config_from_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            result = run_capture(
                ["bash", str(REPO_ROOT / ".ai" / "install" / "install.sh"), "--target", str(project)],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = project / ".ai-config"
            self.assertEqual({p.name for p in installed.iterdir() if p.is_file()}, EXPECTED_CONFIGS)
            for name in EXPECTED_CONFIGS:
                self.assertEqual((installed / name).read_bytes(), (TEMPLATE_CONFIG / name).read_bytes())
            source_files = {
                path.relative_to(REPO_ROOT / ".ai").as_posix()
                for path in (REPO_ROOT / ".ai").rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            }
            installed_files = {
                path.relative_to(project / ".ai").as_posix()
                for path in (project / ".ai").rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            }
            self.assertEqual(installed_files, source_files)
            self.assertFalse((project / ".ai-work").exists(), "installer must copy templates only")
            ignore = (project / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".ai-work/", ignore)
            self.assertIn(".visualizer/*.json", ignore)

    def test_bootstrap_creates_initial_exact_artifact_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            installed = run_capture(
                ["bash", str(REPO_ROOT / ".ai" / "install" / "install.sh"), "--target", str(project)],
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertFalse((project / ".ai-work").exists())

            bootstrapped = run_capture(["bash", ".ai/scripts/bootstrap.sh"], cwd=project)
            self.assertEqual(bootstrapped.returncode, 0, bootstrapped.stderr)
            artifact_root = project / ".ai-work" / "artifacts" / "project"
            expected = {"manifest.json", *ai_kit.ARTIFACT_PAYLOAD_FILES}
            self.assertEqual({path.name for path in artifact_root.iterdir() if path.is_file()}, expected)
            validated = run_capture(
                [sys.executable, ".ai/engine/ai_kit.py", "artifact", "validate"],
                cwd=project,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_installed_project_can_create_store_pilot_from_shipped_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            installed = run_capture(
                ["bash", str(REPO_ROOT / ".ai" / "install" / "install.sh"), "--target", str(project)],
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            scaffolded = run_capture(
                [sys.executable, ".ai/engine/ai_kit.py", "scaffold", "store-pilot"],
                cwd=project,
            )
            self.assertEqual(scaffolded.returncode, 0, scaffolded.stderr)
            self.assertTrue((project / "architecture" / "VERSION.yaml").is_file())
            self.assertFalse((project / "architecture" / "truth.yaml").exists())
            self.assertTrue((project / "contracts" / "generated" / "sdk" / "contracts.ts").is_file())
            self.assertTrue((project / "worker" / "store_lifecycle.py").is_file())

    def test_runtime_seed_uses_local_qa_and_explicit_review_waiver(self) -> None:
        """The centralized seed automates deterministic QA without fabricating
        an independent reviewer identity."""
        roles = ai_kit._load_automation_roles()
        self.assertTrue(roles["qa"]["enabled"])
        self.assertEqual(roles["qa"]["mode"], "local")
        self.assertFalse(roles["reviewer"]["enabled"])
        self.assertEqual(roles["reviewer"]["mode"], "not-required")
        self.assertTrue(ai_kit._load_post_completion_config()["enabled"])
        validated = ai_kit.cmd_config_validate(argparse.Namespace())
        self.assertTrue(validated["passed"])

    def test_project_owned_configs_ship_empty(self) -> None:
        """contexts.yaml and epics.yaml describe one specific project. The
        template must not carry real entries -- an earlier release shipped a
        contexts.yaml containing another project's module registry."""
        for name in PROJECT_OWNED_CONFIGS:
            with self.subTest(config=name):
                template = (TEMPLATE_CONFIG / name).read_text(encoding="utf-8")
                entries = [ln for ln in template.splitlines()
                           if re.match(r"^  \S+:", ln) and not ln.lstrip().startswith("#")]
                self.assertEqual(
                    entries, [],
                    f".ai/install/config/{name} ships with real entries {entries}; new "
                    f"projects would inherit another project's data",
                )


@unittest.skipIf(os.name == "nt", "installer tree parity is unreliable on the Windows runner")
class VisualizerParityTests(unittest.TestCase):
    """The visualizer ships to installed projects, so its source files must
    not drift from the template copy. Generated payloads (*.json) are
    gitignored runtime artifacts and are deliberately excluded."""

    @staticmethod
    def _tracked_sources(directory: Path) -> set[str]:
        return {p.name for p in directory.iterdir()
                if p.is_file() and p.suffix != ".json"}

    def test_same_source_file_set(self) -> None:
        self.assertEqual(
            self._tracked_sources(LIVE_VISUALIZER),
            self._tracked_sources(TEMPLATE_VISUALIZER),
            "a visualizer source file exists in one copy but not the other",
        )

    def test_source_files_are_byte_identical(self) -> None:
        for name in sorted(self._tracked_sources(LIVE_VISUALIZER)):
            with self.subTest(file=name):
                self.assertEqual(
                    (LIVE_VISUALIZER / name).read_bytes(),
                    (TEMPLATE_VISUALIZER / name).read_bytes(),
                    f".visualizer/{name} differs from its install-template copy; a new "
                    f"install would get a stale visualizer",
                )

    def test_generated_payloads_are_not_committed(self) -> None:
        """*.json under .visualizer/ are regenerated on every transition; the
        template must never carry a snapshot of this repo's workflow state."""
        stray = sorted(p.name for p in TEMPLATE_VISUALIZER.glob("*.json"))
        self.assertEqual(stray, [], f"generated visualizer payloads in the template: {stray}")


if __name__ == "__main__":
    unittest.main()
