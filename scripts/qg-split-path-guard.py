#!/usr/bin/env python3
"""Check that split QuantGod repositories do not point to the old monorepo."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUTOMATIONS = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "automations"

OLD_PATH_PATTERNS = [
    re.compile(r"/Users/[^\s\"']*/Desktop/Quard/QuantGod(?!Backend|Frontend|Infra|Docs)"),
    re.compile(r"Desktop/Quard/QuantGod(?!Backend|Frontend|Infra|Docs)"),
    re.compile(r"cwds\s*=\s*\[\s*\"/Users/[^\"]*/Desktop/Quard/QuantGod\"\s*\]"),
    re.compile(r"C:\\QuantGod\\QuantGod(?!Backend|Frontend|Infra|Docs)"),
    re.compile(r"QuantGod/(frontend|docs)(/|\b)"),
]

SKIP_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "vue-dist",
    "__pycache__",
    ".pytest_cache",
    ".venv",
}

SKIP_FILES = {
    "qg-split-path-guard.py",
    "test_split_path_guard.py",
}

TEXT_SUFFIXES = {
    ".bat",
    ".cjs",
    ".css",
    ".env",
    ".example",
    ".html",
    ".js",
    ".json",
    ".jsonc",
    ".md",
    ".mjs",
    ".plist",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".vue",
    ".xml",
    ".yml",
    ".yaml",
}


def should_scan(path: Path) -> bool:
    if path.name in SKIP_FILES:
        return False
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    if path.name.startswith(".env.") and not path.name.endswith(".example"):
        return False
    if path.name == ".env.local":
        return False
    if path.suffix in TEXT_SUFFIXES:
        return True
    return path.name in {"README", "LICENSE", "Dockerfile"}


def iter_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if should_scan(root):
                files.append(root)
            continue
        for path in root.rglob("*"):
            if path.is_file() and should_scan(path):
                files.append(path)
    return files


def find_issues(paths: list[Path]) -> list[str]:
    issues: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern in OLD_PATH_PATTERNS:
                if pattern.search(line):
                    issues.append(f"{path}:{line_number}: {line.strip()[:240]}")
                    break
    return issues


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check split-repo old path residue")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Parent directory containing QuantGod* repos")
    parser.add_argument("--include-codex-automations", action="store_true", help="Also scan ~/.codex/automations")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    roots = [
        args.root / "QuantGodBackend",
        args.root / "QuantGodFrontend",
        args.root / "QuantGodInfra",
        args.root / "QuantGodDocs",
    ]
    if args.include_codex_automations:
        roots.append(DEFAULT_AUTOMATIONS)
    issues = find_issues(iter_files(roots))
    if issues:
        print("QuantGod split path guard failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("QuantGod split path guard OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
