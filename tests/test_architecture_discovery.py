"""Unit tests for the Architecture Discovery capability
(`ai-kit architecture discover`, projected by `ai-kit artifact generate`).

Like tests/test_ai_kit.py, every test runs against a throwaway temp
directory with ai_kit's module-level path constants monkeypatched onto it,
so nothing here ever touches this repository's real `.ai-config/` or
`.visualizer/` state.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1] / ".ai" / "engine"
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_DIR))
import ai_kit  # noqa: E402


def ns(**kwargs) -> argparse.Namespace:
    defaults = dict(state=None)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class ArchitectureDiscoveryTestCase(unittest.TestCase):
    """Builds an isolated temp ROOT with a writable .ai-config/ so discovery
    can read kit.yaml/contexts.yaml and scan a fixture source tree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".ai-config").mkdir(parents=True, exist_ok=True)

        self._patched = {
            name: getattr(ai_kit, name)
            for name in ("ROOT", "WORK", "STATE", "CURRENT", "EVENT_LOG", "VISUALIZER_DIR")
        }
        ai_kit.ROOT = self.root
        ai_kit.WORK = self.root / ".ai-work-unused"
        ai_kit.STATE = ai_kit.WORK / "state" / "workflow.json"
        ai_kit.CURRENT = ai_kit.WORK / "state" / "current.json"
        ai_kit.EVENT_LOG = ai_kit.WORK / "logs" / "events.jsonl"
        ai_kit.VISUALIZER_DIR = self.root / ".visualizer-unused"

    def tearDown(self) -> None:
        for name, value in self._patched.items():
            setattr(ai_kit, name, value)
        self._tmp.cleanup()

    # -- fixture helpers -----------------------------------------------
    def write_kit_yaml(self, source_dirs: list[str], stack: list[str] | None = None) -> None:
        stack = stack or ["node"]
        (self.root / ".ai-config" / "kit.yaml").write_text(
            "kit:\n  id: ai-kit-v2\n  version: 2.0.0\n  entrypoint: AGENTS.md\n"
            "  work_dir: .ai-work\n  skills_dir: .ai/skills\n  registry: .ai-config/registry.yaml\n"
            f"project:\n  stack: [{', '.join(stack)}]\n  source_dirs: [{', '.join(source_dirs)}]\n"
            "verification:\n  test_command: true\n  typecheck_command: true\n"
            "  build_command: true\n  lint_command: true\n",
            encoding="utf-8",
        )

    def write_contexts_yaml(self, raw: str) -> None:
        (self.root / ".ai-config" / "contexts.yaml").write_text(f"contexts:\n{raw}", encoding="utf-8")

    def write_file(self, rel_path: str, content: str = "") -> Path:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def discover(self) -> dict:
        return ai_kit.cmd_architecture_discover(ns())


class DeclaredContextTests(ArchitectureDiscoveryTestCase):
    def test_declared_context_read_accurately(self) -> None:
        self.write_kit_yaml(["src"])
        self.write_contexts_yaml(
            "  api:\n    path: src/api/*\n    owner: backend\n    depends_on: [\"storage\"]\n"
            "  storage:\n    path: src/storage/*\n    owner: backend\n"
        )
        self.write_file("src/api/index.ts", "")
        artifact = self.discover()
        self.assertEqual(artifact["contexts"]["api"]["path"], "src/api/*")
        self.assertEqual(artifact["contexts"]["api"]["owner"], "backend")
        self.assertEqual(artifact["contexts"]["api"]["depends_on"], ["storage"])
        self.assertEqual(artifact["modules"]["api"]["source"], "declared")
        self.assertEqual(artifact["modules"]["api"]["kind"], "bounded-context")
        self.assertIn({"from": "api", "to": "storage", "kind": "declared", "confidence": 1.0}, artifact["edges"])


class NestJsDiscoveryTests(ArchitectureDiscoveryTestCase):
    def test_nestjs_feature_modules_detected(self) -> None:
        self.write_kit_yaml(["apps/api/src"])
        self.write_contexts_yaml("  api:\n    path: apps/api/src/*\n    owner: backend\n")
        self.write_file("apps/api/src/downloads/downloads.module.ts", "export class DownloadsModule {}\n")
        self.write_file("apps/api/src/storage/storage.module.ts", "export class StorageModule {}\n")
        artifact = self.discover()
        self.assertIn("downloads", artifact["modules"])
        self.assertIn("storage", artifact["modules"])
        module = artifact["modules"]["downloads"]
        self.assertEqual(module["source"], "discovered")
        self.assertEqual(module["kind"], "feature")
        self.assertEqual(module["framework"], "nestjs")
        self.assertEqual(module["parent"], "api")
        self.assertGreater(module["confidence"], 0.5)


class ReactDiscoveryTests(ArchitectureDiscoveryTestCase):
    def test_react_feature_folders_detected(self) -> None:
        self.write_kit_yaml(["apps/web"])
        self.write_contexts_yaml("  web:\n    path: apps/web/*\n    owner: frontend\n")
        self.write_file("apps/web/src/features/checkout/index.tsx", "export const Checkout = () => null;\n")
        self.write_file("apps/web/src/pages/home/index.tsx", "export const Home = () => null;\n")
        artifact = self.discover()
        self.assertIn("checkout", artifact["modules"])
        self.assertEqual(artifact["modules"]["checkout"]["framework"], "react")
        self.assertEqual(artifact["modules"]["checkout"]["parent"], "web")
        self.assertIn("home", artifact["modules"])


class PythonDiscoveryTests(ArchitectureDiscoveryTestCase):
    def test_python_packages_detected(self) -> None:
        self.write_kit_yaml(["src"], stack=["python"])
        self.write_contexts_yaml("  core:\n    path: src/*\n    owner: backend\n")
        self.write_file("src/__init__.py", "")
        self.write_file("src/auth/__init__.py", "")
        self.write_file("src/auth/service.py", "")
        artifact = self.discover()
        self.assertIn("auth", artifact["modules"])
        self.assertEqual(artifact["modules"]["auth"]["framework"], "python")
        self.assertEqual(artifact["modules"]["auth"]["parent"], "core")


class IgnoreRuleTests(ArchitectureDiscoveryTestCase):
    def test_ignored_directories_are_not_discovered(self) -> None:
        self.write_kit_yaml(["."], stack=["node"])
        self.write_file("node_modules/some-dep/package.module.ts", "export class X {}\n")
        self.write_file("dist/build.module.ts", "export class Y {}\n")
        self.write_file("src/real/real.module.ts", "export class RealModule {}\n")
        artifact = self.discover()
        names = set(artifact["modules"])
        self.assertNotIn("some-dep", names)
        self.assertNotIn("package", names)
        self.assertNotIn("build", names)
        self.assertIn("real", names)


class DependencyDetectionTests(ArchitectureDiscoveryTestCase):
    def test_internal_dependency_detected(self) -> None:
        self.write_kit_yaml(["apps/api/src"])
        self.write_contexts_yaml("  api:\n    path: apps/api/src/*\n    owner: backend\n")
        self.write_file(
            "apps/api/src/downloads/downloads.module.ts",
            "import { StorageService } from '../storage/storage.service';\nexport class DownloadsModule {}\n",
        )
        self.write_file("apps/api/src/storage/storage.module.ts", "export class StorageModule {}\n")
        artifact = self.discover()
        edge = next((e for e in artifact["edges"] if e["from"] == "downloads" and e["to"] == "storage"), None)
        self.assertIsNotNone(edge, f"expected downloads -> storage edge, got {artifact['edges']}")
        self.assertEqual(edge["kind"], "source-import")
        self.assertGreater(edge["confidence"], 0)

    def test_external_package_not_treated_as_internal_dependency(self) -> None:
        self.write_kit_yaml(["apps/api/src"])
        self.write_contexts_yaml("  api:\n    path: apps/api/src/*\n    owner: backend\n")
        self.write_file(
            "apps/api/src/downloads/downloads.module.ts",
            "import { Injectable } from '@nestjs/common';\nimport axios from 'axios';\nexport class DownloadsModule {}\n",
        )
        artifact = self.discover()
        self.assertEqual(artifact["edges"], [])

    def test_python_relative_import_dependency_detected(self) -> None:
        self.write_kit_yaml(["src"], stack=["python"])
        self.write_contexts_yaml("  core:\n    path: src/*\n    owner: backend\n")
        self.write_file("src/__init__.py", "")
        self.write_file("src/auth/__init__.py", "")
        self.write_file("src/auth/service.py", "from ..storage import client\n")
        self.write_file("src/storage/__init__.py", "")
        artifact = self.discover()
        edge = next((e for e in artifact["edges"] if e["from"] == "auth" and e["to"] == "storage"), None)
        self.assertIsNotNone(edge, f"expected auth -> storage edge, got {artifact['edges']}")

    def test_python_import_in_comment_or_string_does_not_create_dependency(self) -> None:
        self.write_kit_yaml(["src"], stack=["python"])
        self.write_contexts_yaml("  core:\n    path: src/*\n    owner: backend\n")
        self.write_file("src/__init__.py", "")
        self.write_file("src/api/__init__.py", "")
        self.write_file("src/storage/__init__.py", "")
        self.write_file("src/api/service.py", '# from ..storage import client\nexample = "from ..storage import client"\n')
        artifact = self.discover()
        self.assertFalse(any(edge["from"] == "api" and edge["to"] == "storage" for edge in artifact["edges"]))


class WarningTests(ArchitectureDiscoveryTestCase):
    def test_duplicate_module_path_is_warned(self) -> None:
        self.write_kit_yaml(["apps/api/src"])
        self.write_contexts_yaml("  api:\n    path: apps/api/src/*\n    owner: backend\n")
        # Same directory matches both the NestJS (*.module.ts) and would-be
        # generic conventions with two different candidate names; force a
        # collision by pre-seeding a module with the same name discovery
        # would also produce for a different path.
        self.write_file("apps/api/src/api/api.module.ts", "export class ApiModule {}\n")
        artifact = self.discover()
        # 'api' collides with the declared context named 'api'.
        kinds = {w["kind"] for w in artifact["warnings"]}
        self.assertIn("duplicate_module_path", kinds)
        self.assertIn("api-2", artifact["modules"])

    def test_module_without_owner_is_warned(self) -> None:
        self.write_contexts_yaml("  api:\n    path: apps/api/src/*\n    owner: backend\n")
        self.write_file("standalone/standalone.module.ts", "export class StandaloneModule {}\n")
        # standalone/ is outside apps/api/src, so it must be a configured
        # source dir in its own right to be scanned at all, and it isn't
        # claimed by any declared context's path glob.
        self.write_kit_yaml(["apps/api/src", "standalone"])
        artifact = self.discover()
        kinds = {w["kind"] for w in artifact["warnings"]}
        self.assertIn("module_missing_owner", kinds)
        self.assertIn("module_outside_context", kinds)

    def test_source_root_missing_is_warned(self) -> None:
        self.write_kit_yaml(["does/not/exist"])
        artifact = self.discover()
        kinds = {w["kind"] for w in artifact["warnings"]}
        self.assertIn("source_root_missing", kinds)


class TaskMappingTests(ArchitectureDiscoveryTestCase):
    def setUp(self) -> None:
        super().setUp()
        for role in ("planner", "backend", "qa", "reviewer"):
            (self.root / ".ai" / "agents" / role).mkdir(parents=True, exist_ok=True)
        (self.root / ".ai" / "workflows" / "feature").mkdir(parents=True, exist_ok=True)

    def _init_state_with_tasks(self) -> Path:
        state_path = self.root / ".ai-work" / "state" / "workflow.json"
        sys.path  # keep import used
        from ai_kit import new_state  # local import to avoid unused warnings
        state = new_state("test", "feature")
        state["tasks"] = [
            {
                "id": "T1", "title": "context mapped", "owner": "backend", "phase": "build",
                "needs": [], "status": "todo", "acceptance": ["a"], "files": [], "tags": [],
                "attempts": 0, "evidence": [], "blocked_reason": None, "claimed_by": None,
                "context": "api", "epic": None, "base_commit": None, "context_revision": 1,
                "epic_revision": 1, "depends_on": [], "contract_hashes": {}, "upstream_context_revisions": {},
            },
            {
                "id": "T2", "title": "file mapped", "owner": "backend", "phase": "build",
                "needs": [], "status": "todo", "acceptance": ["a"], "files": ["apps/api/src/downloads/downloads.module.ts"],
                "tags": [], "attempts": 0, "evidence": [], "blocked_reason": None, "claimed_by": None,
                "context": None, "epic": None, "base_commit": None, "context_revision": 1,
                "epic_revision": 1, "depends_on": [], "contract_hashes": {}, "upstream_context_revisions": {},
            },
        ]
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return state_path

    def test_task_mapped_by_context_and_by_file_path(self) -> None:
        self.write_kit_yaml(["apps/api/src"])
        self.write_contexts_yaml("  api:\n    path: apps/api/src/*\n    owner: backend\n")
        self.write_file("apps/api/src/downloads/downloads.module.ts", "export class DownloadsModule {}\n")
        state_path = self._init_state_with_tasks()
        artifact = ai_kit.cmd_architecture_discover(ns(state=str(state_path)))
        self.assertIn("T1", artifact["modules"]["api"]["related_tasks"])
        self.assertIn("T2", artifact["modules"]["downloads"]["related_tasks"])

    def test_module_without_tasks_warned_when_tasks_exist(self) -> None:
        self.write_kit_yaml(["apps/api/src"])
        self.write_contexts_yaml("  api:\n    path: apps/api/src/*\n    owner: backend\n")
        self.write_file("apps/api/src/downloads/downloads.module.ts", "export class DownloadsModule {}\n")
        self.write_file("apps/api/src/storage/storage.module.ts", "export class StorageModule {}\n")
        state_path = self._init_state_with_tasks()
        artifact = ai_kit.cmd_architecture_discover(ns(state=str(state_path)))
        details = " ".join(w["detail"] for w in artifact["warnings"] if w["kind"] == "module_without_tasks")
        self.assertIn("storage", details)


class SchemaAndArtifactTests(ArchitectureDiscoveryTestCase):
    def test_artifact_has_schema_version(self) -> None:
        self.write_kit_yaml(["src"])
        artifact = self.discover()
        self.assertEqual(artifact["schema_version"], ai_kit.ARCHITECTURE_DISCOVERY_SCHEMA_VERSION)
        self.assertIn("generated_at", artifact)
        self.assertEqual(set(artifact), {"schema_version", "generated_at", "contexts", "modules", "edges", "warnings"})

    def test_manifest_declares_discovered_architecture_version(self) -> None:
        self.assertIn("discovered-architecture.json", ai_kit.VISUALIZER_ARTIFACT_VERSIONS)

    def test_empty_project_runs_successfully(self) -> None:
        self.write_kit_yaml(["src"])
        artifact = self.discover()
        self.assertEqual(artifact["modules"], {})
        self.assertEqual(artifact["edges"], [])

    def test_invalid_context_path_type_raises_engine_error(self) -> None:
        self.write_kit_yaml(["src"])
        (self.root / ".ai-config" / "contexts.yaml").write_text(
            'contexts:\n  api:\n    path: ["not", "a", "string"]\n    owner: backend\n', encoding="utf-8",
        )
        with self.assertRaises(ai_kit.EngineError):
            self.discover()

    def test_discover_never_publishes_even_when_visualizer_exists(self) -> None:
        ai_kit.VISUALIZER_DIR = self.root / ".visualizer"
        ai_kit.VISUALIZER_DIR.mkdir(parents=True, exist_ok=True)
        self.write_kit_yaml(["src"])
        self.discover()
        self.assertFalse((ai_kit.VISUALIZER_DIR / "discovered-architecture.json").exists())

    def test_not_written_when_visualizer_dir_absent(self) -> None:
        self.write_kit_yaml(["src"])
        self.discover()
        self.assertFalse((self.root / ".visualizer-unused").exists())


class VisualizerGenerateIntegrationTests(ArchitectureDiscoveryTestCase):
    """`ai-kit visualizer generate` must keep writing every legacy artifact
    unchanged while also producing discovered-architecture.json."""

    def setUp(self) -> None:
        super().setUp()
        for role in ("planner", "backend", "qa", "reviewer"):
            (self.root / ".ai" / "agents" / role).mkdir(parents=True, exist_ok=True)
        (self.root / ".ai" / "workflows" / "feature").mkdir(parents=True, exist_ok=True)
        ai_kit.VISUALIZER_DIR = self.root / ".visualizer"
        ai_kit.VISUALIZER_DIR.mkdir(parents=True, exist_ok=True)

    def test_generate_still_writes_all_legacy_artifacts_plus_discovery(self) -> None:
        self.write_kit_yaml(["src"])
        self.write_contexts_yaml("  api:\n    path: src/*\n    owner: backend\n")
        payloads = ai_kit._generate_visualizer_data(str(self.root / ".ai-work" / "state" / "workflow.json"))
        self.assertEqual(
            set(payloads),
            {"board.json", "architecture.json", "impact.json", "events.json", "dag.json", "contracts.json",
             "discovered-architecture.json", "artifacts.json"},
        )
        for filename in payloads:
            self.assertTrue((ai_kit.VISUALIZER_DIR / filename).exists(), f"{filename} was not written")
        self.assertEqual(payloads["architecture.json"], ai_kit._load_contexts())


class VisualizerAppJsContractTests(unittest.TestCase):
    """Static assertions on the shipped visualizer JS/HTML: it must fetch the
    new artifact with a graceful fallback and must not hardcode module
    names, satisfying the 'fallback to architecture.json' and 'no
    hardcoded module names' requirements without needing a browser."""

    APP_JS = (REPO_ROOT / ".visualizer" / "app.js").read_text(encoding="utf-8")
    INDEX_HTML = (REPO_ROOT / ".visualizer" / "index.html").read_text(encoding="utf-8")

    def test_fetches_discovered_architecture_json(self) -> None:
        self.assertIn("discovered-architecture.json", self.APP_JS)

    def test_manifest_first_loader_has_legacy_fallback(self) -> None:
        self.assertIn("/artifacts/project/manifest.json", self.APP_JS)
        self.assertIn("loadCanonicalArtifacts", self.APP_JS)
        self.assertIn("loadLegacyArtifacts", self.APP_JS)
        self.assertIn("fetch('discovered-architecture.json').catch", self.APP_JS)

    def test_no_hardcoded_module_names(self) -> None:
        for banned in ("'downloads'", '"downloads"', "'crawler'", '"crawler"'):
            self.assertNotIn(banned, self.APP_JS)

    def test_template_copy_matches_repo_copy(self) -> None:
        template = (REPO_ROOT / ".ai" / "install" / "templates" / ".visualizer" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(self.APP_JS, template)
        template_html = (REPO_ROOT / ".ai" / "install" / "templates" / ".visualizer" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(self.INDEX_HTML, template_html)


if __name__ == "__main__":
    unittest.main()
