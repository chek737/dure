from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VERSION_TOOL = REPOSITORY_ROOT / "dure/scripts/check_version_sync.py"
PRE_COMMIT_HOOK = REPOSITORY_ROOT / ".githooks/pre-commit"


class VersionSynchronizationToolTests(unittest.TestCase):
    def _write_project(self, root: Path, version: str = "1.2.3") -> None:
        (root / "src/dure").mkdir(parents=True)
        (root / "debian").mkdir()
        (root / "src/dure/__init__.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8"
        )
        (root / "pyproject.toml").write_text(
            "[project]\nname = \"dure\"\n"
            f'version = "{version}"\n',
            encoding="utf-8",
        )
        (root / "setup.py").write_text(
            "from setuptools import setup\n"
            f'setup(name="dure", version="{version}")\n',
            encoding="utf-8",
        )
        (root / "debian/changelog").write_text(
            f"dure ({version}-1) unstable; urgency=medium\n",
            encoding="utf-8",
        )

    def _run_version_tool(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VERSION_TOOL), "--project-root", str(root)],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_version_tool_accepts_synchronized_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_project(root)

            result = self._run_version_tool(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Versions synchronized: 1.2.3")

    def test_version_tool_rejects_a_mismatched_packaging_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_project(root)
            (root / "setup.py").write_text(
                "from setuptools import setup\nsetup(name=\"dure\", version=\"9.9.9\")\n",
                encoding="utf-8",
            )

            result = self._run_version_tool(root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("Version synchronization failed: version mismatch", result.stderr)
        self.assertIn("setup=9.9.9", result.stderr)


class PreCommitHookTests(unittest.TestCase):
    def _run_hook(self, staged_path: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".githooks").mkdir()
            shutil.copy2(PRE_COMMIT_HOOK, root / ".githooks/pre-commit")
            (root / "dure/src").mkdir(parents=True)
            (root / "dure/tests").mkdir()
            (root / "dure/src/example.py").write_text("value = 1\n", encoding="utf-8")
            target = root / staged_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("configured = True\n", encoding="utf-8")

            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            subprocess.run(["git", "add", "dure/src/example.py", staged_path], cwd=root, check=True)
            environment = os.environ | {"DURE_PYTHON": sys.executable}
            return subprocess.run(
                ["sh", ".githooks/pre-commit"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

    def test_pre_commit_compiles_the_nested_dure_project(self):
        result = self._run_hook("dure/src/added.py")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pre_commit_rejects_a_staged_secret_configuration_file(self):
        result = self._run_hook("dure/.env")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to commit a secret/config", result.stderr)
