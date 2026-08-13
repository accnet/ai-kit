"""Installer contract for project-owned AI-Kit configuration.

The source kit tracks only `.ai/install/config/`.  Its installer materializes
that directory as `.ai-config/` in a consuming project and must never require
or recreate a source-repository `.ai-config/` tree.
"""
from __future__ import annotations

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
    "automation.yaml", "contexts.yaml", "epics.yaml", "kit.yaml",
    "registry.yaml", "rules.yaml", "runners.yaml", "design-policy.json", "contracts.json", "delivery.json",
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

    def test_automation_seed_has_manual_roles_with_valid_runner_models(self) -> None:
        """A fresh install must not dispatch QA/review until enabled, and its
        configured primary/backup runner:model pairs must resolve."""
        roles = ai_kit._load_automation_roles()
        self.assertFalse(roles["qa"]["enabled"])
        self.assertFalse(roles["reviewer"]["enabled"])
        self.assertFalse(
            ai_kit._load_post_completion_config()["enabled"],
            "fresh installs must not auto-run QA/review/retry while both roles are manual",
        )
        for role in ("qa", "reviewer"):
            config = roles[role]
            for runner_key, model_key in (("runner", "model"), ("backup_runner", "backup_model")):
                runner = config.get(runner_key)
                if runner:
                    resolved_name, _entry, resolved_model = ai_kit._resolve_runner(runner, config.get(model_key))
                    self.assertEqual(resolved_name, runner)
                    self.assertEqual(resolved_model, config.get(model_key))

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
