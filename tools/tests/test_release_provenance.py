from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


TOOLS_ROOT = Path(__file__).resolve().parents[1]
TOOL = TOOLS_ROOT / "release_provenance.py"
SPEC = importlib.util.spec_from_file_location("mirror_release_provenance", TOOL)
assert SPEC is not None and SPEC.loader is not None
release_provenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_provenance)


class MirrorReleaseProvenanceTests(unittest.TestCase):
    def _arguments(self, package: Path) -> dict[str, object]:
        return {
            "package": package,
            "version": "0.4.16",
            "source_repository": "https://github.com/madcamp-official/legendary-super-ultra-black-dragon",
            "source_tag": "v0.4.16",
            "source_commit": "a" * 40,
            "builder_repository": "https://github.com/chek737/dure",
            "workflow_run_url": "https://github.com/chek737/dure/actions/runs/123",
            "mirror_url": "https://chek737.github.io/dure",
            "signing_key_fingerprint": "E1F952F8B23E7A1B884CB5A33EC5C8CAE53AFA01",
        }

    def test_create_and_verify_bind_exact_source_and_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "dure_0.4.16_all.deb"
            package.write_bytes(b"mirror package bytes")
            arguments = self._arguments(package)
            manifest_path = root / "release-provenance.json"
            release_provenance.write_manifest(
                manifest_path, release_provenance.create_manifest(**arguments)
            )

            verified = release_provenance.verify_manifest(
                manifest_path=manifest_path, **arguments
            )

        self.assertEqual(verified["source"]["commit"], "a" * 40)
        self.assertEqual(verified["builder"]["repository"], "https://github.com/chek737/dure")

    def test_verify_rejects_substituted_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "dure_0.4.16_all.deb"
            package.write_bytes(b"mirror package bytes")
            arguments = self._arguments(package)
            manifest_path = root / "release-provenance.json"
            release_provenance.write_manifest(
                manifest_path, release_provenance.create_manifest(**arguments)
            )
            package.write_bytes(b"substituted bytes")

            with self.assertRaisesRegex(
                release_provenance.ProvenanceError,
                "does not match the expected release package",
            ):
                release_provenance.verify_manifest(manifest_path=manifest_path, **arguments)

    def test_create_rejects_tag_or_builder_run_that_does_not_match_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "dure_0.4.16_all.deb"
            package.write_bytes(b"mirror package bytes")
            arguments = self._arguments(package)
            arguments["source_tag"] = "v0.4.17"

            with self.assertRaisesRegex(release_provenance.ProvenanceError, "must exactly match"):
                release_provenance.create_manifest(**arguments)

            arguments = self._arguments(package)
            arguments["workflow_run_url"] = "https://github.com/madcamp-official/legendary-super-ultra-black-dragon/actions/runs/123"
            with self.assertRaisesRegex(release_provenance.ProvenanceError, "builder repository"):
                release_provenance.create_manifest(**arguments)

    def test_verify_rejects_unrecognized_manifest_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "dure_0.4.16_all.deb"
            package.write_bytes(b"mirror package bytes")
            arguments = self._arguments(package)
            manifest_path = root / "release-provenance.json"
            manifest = release_provenance.create_manifest(**arguments)
            manifest["untrusted"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                release_provenance.ProvenanceError,
                "does not match the expected release package",
            ):
                release_provenance.verify_manifest(manifest_path=manifest_path, **arguments)
