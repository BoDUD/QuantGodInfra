#!/usr/bin/env python3
"""QuantGod repository governance file checker.

This checker is intentionally small and stdlib-only so every QuantGod
repository can run it in GitHub Actions without extra dependencies.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
]

REQUIRED_HEADERS = {
    "SECURITY.md": ["# Security Policy", "## Scope", "## Reporting"],
    "CONTRIBUTING.md": ["# Contributing to QuantGod", "## Repository boundaries", "## Safety rules"],
    "CODE_OF_CONDUCT.md": ["# Code of Conduct", "## Expected behavior", "## Enforcement"],
    "CHANGELOG.md": ["# Changelog", "## Unreleased"],
}

SECRET_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)telegram[_-]?bot[_-]?token\s*[:=]\s*['\"][^'\"]+['\"]"),
    re.compile(r"(?i)openrouter[_-]?api[_-]?key\s*[:=]\s*['\"][^'\"]+['\"]"),
]

BOUNDARY_MARKERS = {
    "QuantGodBackend": {
        "allowed": ["MQL5", "Dashboard", "tools", "tests"],
        "forbidden": ["frontend/src"],
    },
    "QuantGodFrontend": {
        "allowed": ["src", "package.json", "vite.config.js"],
        "forbidden": ["MQL5", "Dashboard", "tools/ai_analysis"],
    },
    "QuantGodInfra": {
        "allowed": ["scripts", "workspace"],
        "forbidden": ["MQL5", "src/App.vue", "tools/ai_analysis"],
    },
    "QuantGodDocs": {
        "allowed": ["docs", "scripts", "tests"],
        "forbidden": ["MQL5", "Dashboard", "src/App.vue"],
    },
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)


def check_required_files(errors: list[str]) -> None:
    for rel in REQUIRED_FILES:
        path = ROOT / rel
        if not path.exists():
            fail(f"missing required governance file: {rel}", errors)
            continue
        text = read_text(path)
        if len(text.strip()) < 120:
            fail(f"governance file is too short: {rel}", errors)
        if "\r" in text:
            fail(f"governance file contains CR line endings: {rel}", errors)

    for rel, headers in REQUIRED_HEADERS.items():
        path = ROOT / rel
        if not path.exists():
            continue
        text = read_text(path)
        for header in headers:
            if header not in text:
                fail(f"{rel} missing required heading: {header}", errors)


def check_license(errors: list[str]) -> None:
    path = ROOT / "LICENSE"
    if not path.exists():
        return
    text = read_text(path)
    accepted = [
        "All Rights Reserved",
        "MIT License",
    ]
    if not any(marker in text for marker in accepted):
        fail("LICENSE must be either All Rights Reserved or MIT License", errors)


def check_repo_manifest(errors: list[str]) -> None:
    manifest = ROOT / "repo-manifest.json"
    if not manifest.exists():
        return
    try:
        json.loads(read_text(manifest))
    except json.JSONDecodeError as exc:
        fail(f"repo-manifest.json is not valid JSON: {exc}", errors)


def check_boundaries(errors: list[str]) -> None:
    repo = ROOT.name
    rules = BOUNDARY_MARKERS.get(repo)
    if not rules:
        return
    for rel in rules["forbidden"]:
        if (ROOT / rel).exists():
            fail(f"split boundary violation in {repo}: forbidden path exists: {rel}", errors)


def check_secret_hygiene(errors: list[str]) -> None:
    targets = [ROOT / rel for rel in REQUIRED_FILES if (ROOT / rel).exists()]
    for path in targets:
        text = read_text(path)
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                fail(f"possible secret-like value in {path.relative_to(ROOT)}", errors)


def main() -> int:
    errors: list[str] = []
    check_required_files(errors)
    check_license(errors)
    check_repo_manifest(errors)
    check_boundaries(errors)
    check_secret_hygiene(errors)

    if errors:
        print("QuantGod repository governance check FAILED", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"QuantGod repository governance check OK: {ROOT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
