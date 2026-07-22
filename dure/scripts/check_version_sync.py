#!/usr/bin/env python3
"""Check that all source and packaging version declarations agree."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


class VersionSyncError(ValueError):
    """Raised when a required version declaration is missing or inconsistent."""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise VersionSyncError(f"cannot read {path}: {error}") from error


def _match_version(path: Path, pattern: str, label: str) -> str:
    match = re.search(pattern, _read(path), flags=re.MULTILINE)
    if match is None:
        raise VersionSyncError(f"cannot find {label} version in {path}")
    return match.group(1)


def _project_version(path: Path) -> str:
    in_project_section = False
    for line in _read(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project_section = stripped == "[project]"
            continue
        if in_project_section:
            match = re.fullmatch(r'version\s*=\s*"([^"]+)"', stripped)
            if match is not None:
                return match.group(1)
    raise VersionSyncError(f"cannot find [project] version in {path}")


def _setup_version(path: Path) -> str:
    try:
        tree = ast.parse(_read(path), filename=str(path))
    except SyntaxError as error:
        raise VersionSyncError(f"cannot parse {path}: {error}") from error

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = node.func.id if isinstance(node.func, ast.Name) else ""
        if function_name != "setup":
            continue
        for keyword in node.keywords:
            if keyword.arg != "version":
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
            raise VersionSyncError(f"setup version in {path} must be a string literal")
    raise VersionSyncError(f"cannot find setup version in {path}")


def collect_versions(project_root: Path) -> dict[str, str]:
    """Return the four version declarations used by Dure's source and packages."""

    return {
        "runtime": _match_version(
            project_root / "src/dure/__init__.py",
            r'^__version__\s*=\s*"([^"]+)"\s*$',
            "runtime",
        ),
        "pyproject": _project_version(project_root / "pyproject.toml"),
        "setup": _setup_version(project_root / "setup.py"),
        "debian": _match_version(
            project_root / "debian/changelog",
            r"^dure \(([^)]+)\)",
            "Debian",
        ).rsplit("-", 1)[0],
    }


def validate_versions(project_root: Path) -> dict[str, str]:
    """Return synchronized versions or raise a concise error describing the mismatch."""

    versions = collect_versions(project_root)
    expected = versions["runtime"]
    mismatches = [
        f"{label}={version}" for label, version in versions.items() if version != expected
    ]
    if mismatches:
        rendered = " ".join(f"{label}={version}" for label, version in versions.items())
        raise VersionSyncError(f"version mismatch: {rendered}")
    return versions


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check Dure source and packaging version synchronization"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Dure project directory (default: script parent)",
    )
    options = parser.parse_args(arguments)

    try:
        versions = validate_versions(options.project_root.resolve())
    except VersionSyncError as error:
        print(f"Version synchronization failed: {error}", file=sys.stderr)
        return 1

    print(f"Versions synchronized: {versions['runtime']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() in subprocess tests.
    raise SystemExit(main())
