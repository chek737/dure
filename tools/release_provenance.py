#!/usr/bin/env python3
"""Create and verify a signed provenance manifest for the Dure APT mirror.

This tool records a mirror build's exact upstream source tag and commit alongside
the produced Debian package.  The detached signature proves that the mirror's
archive key approved the record; it does not claim approval by the upstream
source repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ProvenanceError(ValueError):
    """Raised when an APT mirror provenance claim is invalid or does not match."""


_COMMIT = re.compile(r"[0-9a-f]{40,64}")
_FINGERPRINT = re.compile(r"[0-9A-F]{40}(?:[0-9A-F]{24})?")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]*")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as artifact:
            for block in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ProvenanceError(f"cannot read artifact {path}: {error}") from error
    return digest.hexdigest()


def _github_repository(value: str, label: str) -> str:
    parsed = urlparse(value)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or len(parts) != 2
        or any(not part for part in parts)
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ProvenanceError(f"{label} must be an https://github.com/<owner>/<repository> URL")
    return value.rstrip("/")


def _fingerprint(value: str) -> str:
    normalized = value.replace(" ", "").upper()
    if _FINGERPRINT.fullmatch(normalized) is None:
        raise ProvenanceError("signing key fingerprint must contain 40 or 64 hexadecimal characters")
    return normalized


def _validate_inputs(
    *,
    package: Path,
    version: str,
    source_repository: str,
    source_tag: str,
    source_commit: str,
    builder_repository: str,
    workflow_run_url: str,
    mirror_url: str,
    signing_key_fingerprint: str,
) -> tuple[str, str, str, str]:
    if not package.is_file():
        raise ProvenanceError(f"release package does not exist: {package}")
    if _VERSION.fullmatch(version) is None:
        raise ProvenanceError(f"invalid Debian version: {version}")
    if source_tag != f"v{version}":
        raise ProvenanceError(f"source tag {source_tag!r} must exactly match Debian version v{version}")
    expected_names = {f"dure_{version}_all.deb", f"dure_{version}_amd64.deb"}
    if package.name not in expected_names:
        raise ProvenanceError(f"unexpected Dure Debian package filename: {package.name}")
    source = _github_repository(source_repository, "source repository")
    builder = _github_repository(builder_repository, "builder repository")
    if _COMMIT.fullmatch(source_commit) is None:
        raise ProvenanceError("source commit must be a 40-64 character lowercase hexadecimal hash")
    if not workflow_run_url.startswith(f"{builder}/actions/runs/"):
        raise ProvenanceError("workflow run URL must belong to the mirror builder repository")
    if not workflow_run_url.removeprefix(f"{builder}/actions/runs/").isdigit():
        raise ProvenanceError("workflow run URL must end with a numeric GitHub Actions run ID")
    parsed_mirror = urlparse(mirror_url)
    if parsed_mirror.scheme != "https" or not parsed_mirror.netloc or parsed_mirror.query:
        raise ProvenanceError("mirror URL must be an absolute https URL without a query")
    return source, builder, mirror_url.rstrip("/"), _fingerprint(signing_key_fingerprint)


def create_manifest(
    *,
    package: Path,
    version: str,
    source_repository: str,
    source_tag: str,
    source_commit: str,
    builder_repository: str,
    workflow_run_url: str,
    mirror_url: str,
    signing_key_fingerprint: str,
) -> dict[str, Any]:
    """Create a JSON-serializable manifest that describes one mirror package."""

    source, builder, distribution_url, fingerprint = _validate_inputs(
        package=package,
        version=version,
        source_repository=source_repository,
        source_tag=source_tag,
        source_commit=source_commit,
        builder_repository=builder_repository,
        workflow_run_url=workflow_run_url,
        mirror_url=mirror_url,
        signing_key_fingerprint=signing_key_fingerprint,
    )
    return {
        "schema_version": 1,
        "source": {"repository": source, "tag": source_tag, "commit": source_commit},
        "builder": {
            "repository": builder,
            "workflow": ".github/workflows/publish-apt.yml",
            "run_url": workflow_run_url,
        },
        "artifact": {
            "name": package.name,
            "sha256": _sha256(package),
            "size": package.stat().st_size,
        },
        "distribution": {
            "mirror_url": distribution_url,
            "suite": "stable",
            "component": "main",
            "architecture": "amd64",
            "signing_key_fingerprint": fingerprint,
        },
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as error:
        raise ProvenanceError(f"cannot write manifest {path}: {error}") from error


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(f"cannot read manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise ProvenanceError("mirror provenance manifest must contain a JSON object")
    return value


def verify_manifest(*, manifest_path: Path, **arguments: Any) -> dict[str, Any]:
    """Require the complete manifest schema to match an expected package build."""

    expected = create_manifest(**arguments)
    actual = _read_manifest(manifest_path)
    if actual != expected:
        raise ProvenanceError("mirror provenance manifest does not match the expected release package")
    return actual


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--package", type=Path, required=True, help="built dure .deb package")
    parser.add_argument("--version", required=True, help="Debian package version")
    parser.add_argument("--source-repository", required=True, help="canonical source repository URL")
    parser.add_argument("--source-tag", required=True, help="canonical source tag, exactly v<version>")
    parser.add_argument("--source-commit", required=True, help="resolved immutable source commit")
    parser.add_argument("--builder-repository", required=True, help="mirror build repository URL")
    parser.add_argument("--workflow-run-url", required=True, help="mirror GitHub Actions run URL")
    parser.add_argument("--mirror-url", required=True, help="published APT repository URL")
    parser.add_argument("--signing-key-fingerprint", required=True, help="mirror archive-key fingerprint")


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="create or verify Dure APT mirror provenance")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="write canonical mirror provenance JSON")
    _add_arguments(create)
    create.add_argument("--output", type=Path, required=True, help="manifest JSON path")
    verify = commands.add_parser("verify", help="verify mirror provenance against a package")
    _add_arguments(verify)
    verify.add_argument("--manifest", type=Path, required=True, help="manifest JSON path")
    options = parser.parse_args(arguments)

    values = vars(options).copy()
    command = values.pop("command")
    output = values.pop("output", None)
    manifest_path = values.pop("manifest", None)
    values["package"] = values["package"].resolve()
    try:
        if command == "create":
            manifest = create_manifest(**values)
            assert output is not None
            write_manifest(output, manifest)
            print(f"Wrote mirror provenance: {output}")
            return 0
        assert manifest_path is not None
        verify_manifest(manifest_path=manifest_path, **values)
        print(f"Verified mirror provenance: {manifest_path}")
        return 0
    except ProvenanceError as error:
        print(f"Mirror provenance failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through command tests.
    raise SystemExit(main())
