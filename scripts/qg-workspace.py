#!/usr/bin/env python3
"""Multi-repo workspace helper for QuantGod.

The helper lives in QuantGodInfra and coordinates the four split repositories:

- QuantGodBackend: MQL5, local Node API server, Python tools, tests.
- QuantGodFrontend: Vue operator workbench source.
- QuantGodInfra: local workspace/deployment automation.
- QuantGodDocs: canonical Markdown documentation hub.

All commands are intentionally local-only. Nothing in this helper sends orders,
changes MT5 presets, stores credentials, or exposes services to the public
internet.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

REPO_KEYS = ("backend", "frontend", "infra", "docs")
DEFAULT_WORKSPACE = "workspace/quantgod.workspace.json"


def fail(message: str, code: int = 1) -> None:
    print(f"QG_WORKSPACE_FAIL: {message}", file=sys.stderr)
    raise SystemExit(code)


def load_workspace(path: pathlib.Path) -> dict[str, Any]:
    """Load and validate a QuantGod workspace JSON file."""

    path = path.expanduser().resolve()
    if not path.exists():
        fail(f"workspace file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"workspace file is not valid JSON: {path} ({exc})")

    if not isinstance(data, dict):
        fail(f"workspace root must be a JSON object: {path}")

    for key in REPO_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            fail(f"workspace missing string key: {key}")

    data["_workspace_file"] = str(path)
    return data


def workspace_root(ws: dict[str, Any]) -> pathlib.Path:
    workspace_file = pathlib.Path(str(ws["_workspace_file"])) if "_workspace_file" in ws else pathlib.Path.cwd()
    raw_root = pathlib.Path(str(ws.get("workspaceRoot", "."))).expanduser()
    if raw_root.is_absolute():
        return raw_root.resolve()
    return (workspace_file.parent / raw_root).resolve()


def resolve_repo_path(ws: dict[str, Any], key: str) -> pathlib.Path:
    raw_path = pathlib.Path(str(ws[key])).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return (workspace_root(ws) / raw_path).resolve()


def repo_paths(ws: dict[str, Any]) -> dict[str, pathlib.Path]:
    return {key: resolve_repo_path(ws, key) for key in REPO_KEYS}


def run(cmd: Sequence[str], cwd: pathlib.Path, check: bool = True) -> int:
    printable = " ".join(str(part) for part in cmd)
    print(f"\n$ {printable}\n  cwd={cwd}")
    proc = subprocess.run([str(part) for part in cmd], cwd=str(cwd), text=True)
    if check and proc.returncode != 0:
        fail(f"command failed with exit code {proc.returncode}: {printable}", proc.returncode)
    return proc.returncode


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON: {path} ({exc})")
    if not isinstance(data, dict):
        fail(f"expected JSON object: {path}")
    return data


def has_npm_script(repo: pathlib.Path, script_name: str) -> bool:
    scripts = read_json(repo / "package.json").get("scripts", {})
    return isinstance(scripts, dict) and script_name in scripts


def npm_install(repo: pathlib.Path) -> None:
    if not (repo / "package.json").exists():
        print(f"skip npm install: package.json not found in {repo}")
        return
    if (repo / "node_modules").exists():
        return
    command = "ci" if (repo / "package-lock.json").exists() else "install"
    run(["npm", command], repo)


def assert_workspace_paths(paths: dict[str, pathlib.Path]) -> None:
    for name, path in paths.items():
        if not path.exists():
            fail(f"{name} repo path does not exist: {path}")
        if not path.is_dir():
            fail(f"{name} repo path is not a directory: {path}")


def normalize_remote_url(url: str) -> str:
    text = str(url or "").strip()
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text.removeprefix("git@github.com:")
    if text.endswith(".git"):
        text = text[:-4]
    return text.rstrip("/")


def repo_name_from_url(url: str) -> str:
    normalized = normalize_remote_url(url)
    marker = "github.com/"
    if marker not in normalized:
        return ""
    return normalized.split(marker, 1)[1]


def git_origin_url(repo: pathlib.Path) -> str:
    proc = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def manifest_remote_issues(manifest_path: pathlib.Path, actual_urls: dict[str, str]) -> list[str]:
    if not manifest_path.exists() or not actual_urls:
        return []
    manifest = read_json(manifest_path)
    issues: list[str] = []
    repos = manifest.get("repos")
    linked_repos = manifest.get("linkedRepos")
    if isinstance(repos, dict):
        for key, actual_url in actual_urls.items():
            entry = repos.get(key)
            if not isinstance(entry, dict):
                issues.append(f"{manifest_path}: missing repos.{key}")
                continue
            manifest_url = normalize_remote_url(str(entry.get("url", "")))
            if manifest_url != actual_url:
                issues.append(f"{manifest_path}: repos.{key}.url={manifest_url or 'MISSING'} != {actual_url}")
            actual_name = repo_name_from_url(actual_url)
            manifest_name = str(entry.get("name", "")).strip()
            if actual_name and manifest_name != actual_name:
                issues.append(f"{manifest_path}: repos.{key}.name={manifest_name or 'MISSING'} != {actual_name}")
        return issues
    if isinstance(linked_repos, dict):
        for key, actual_url in actual_urls.items():
            manifest_url = normalize_remote_url(str(linked_repos.get(key, "")))
            if manifest_url != actual_url:
                issues.append(f"{manifest_path}: linkedRepos.{key}={manifest_url or 'MISSING'} != {actual_url}")
    return issues


def check_manifest_remotes(paths: dict[str, pathlib.Path]) -> None:
    actual_urls: dict[str, str] = {}
    for key, path in paths.items():
        origin = normalize_remote_url(git_origin_url(path))
        if origin:
            actual_urls[key] = origin
    if not actual_urls:
        return
    issues: list[str] = []
    for path in paths.values():
        issues.extend(manifest_remote_issues(path / "repo-manifest.json", actual_urls))
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}")
        fail("workspace manifest remote verification failed")
    print("OK: workspace repo manifests match local origin remotes")


def cmd_status(ws: dict[str, Any]) -> None:
    for name, path in repo_paths(ws).items():
        print(f"\n== {name}: {path}")
        if not path.exists():
            print("MISSING")
            continue
        run(["git", "status", "--short", "--branch"], path, check=False)


def cmd_pull(ws: dict[str, Any]) -> None:
    paths = repo_paths(ws)
    assert_workspace_paths(paths)
    for name, path in paths.items():
        print(f"\n== pulling {name}: {path}")
        run(["git", "pull", "--ff-only"], path)


def run_backend_python_tests(backend: pathlib.Path) -> None:
    run([sys.executable, "-m", "unittest", "discover", "tests", "-v"], backend)
    ci_guard = backend / "tools" / "ci_guard.py"
    if ci_guard.exists():
        run([sys.executable, str(ci_guard)], backend)


def run_backend_node_tests(backend: pathlib.Path) -> None:
    """Run backend Node tests without shell globbing and with hard failure."""

    node_dir = backend / "tests" / "node"
    node_tests = sorted(node_dir.glob("*.mjs"))
    if not node_tests:
        print(f"skip backend node tests: no .mjs files under {node_dir}")
        return
    run(["node", "--test", *[str(path) for path in node_tests]], backend)


def run_frontend_build(frontend: pathlib.Path) -> None:
    npm_install(frontend)
    if not has_npm_script(frontend, "build"):
        fail("frontend package.json does not define scripts.build")
    run(["npm", "run", "build"], frontend)


def run_frontend_quality(frontend: pathlib.Path) -> None:
    """Run the smallest useful frontend quality loop before a build artifact is copied."""

    npm_install(frontend)
    for script_name in ("contract", "api-client", "code-splitting", "test"):
        if has_npm_script(frontend, script_name):
            run(["npm", "run", script_name], frontend)


def run_docs_checks(docs: pathlib.Path) -> None:
    docs_check = docs / "scripts" / "check_docs_links.py"
    if docs_check.exists():
        run([sys.executable, str(docs_check)], docs)
    else:
        print(f"skip docs link check: {docs_check} not found")


def cmd_test(ws: dict[str, Any]) -> None:
    """Run the cross-repo smoke test suite.

    Important: every subprocess here is a hard failure by default. The previous
    implementation used check=False for backend Node/API tests, which let broken
    route contracts pass the workspace-level test. Keep that class of bug out.
    """

    paths = repo_paths(ws)
    assert_workspace_paths(paths)
    cmd_verify(ws)
    run_backend_python_tests(paths["backend"])
    run_backend_node_tests(paths["backend"])
    run_frontend_quality(paths["frontend"])
    run_frontend_build(paths["frontend"])
    run_docs_checks(paths["docs"])
    print("QG_WORKSPACE_TEST_OK")


def cmd_build_frontend(ws: dict[str, Any]) -> None:
    run_frontend_build(repo_paths(ws)["frontend"])


def cmd_sync_frontend_dist(ws: dict[str, Any]) -> None:
    if not ws.get("copyFrontendDistToBackend", True):
        print("frontend dist sync disabled by workspace config")
        return

    paths = repo_paths(ws)
    frontend_dist = paths["frontend"] / str(ws.get("frontendDist", "dist"))
    backend_dist = paths["backend"] / str(ws.get("backendVueDist", "Dashboard/vue-dist"))
    if not frontend_dist.exists():
        fail(f"frontend dist missing; run build-frontend first: {frontend_dist}")
    if backend_dist.exists():
        shutil.rmtree(backend_dist)
    shutil.copytree(frontend_dist, backend_dist)
    print(f"synced {frontend_dist} -> {backend_dist}")


def cmd_closed_loop(ws: dict[str, Any]) -> None:
    """Build, copy, and verify the local operator workbench as one closed loop."""

    paths = repo_paths(ws)
    assert_workspace_paths(paths)
    cmd_verify(ws)
    run_frontend_quality(paths["frontend"])
    run_frontend_build(paths["frontend"])
    cmd_sync_frontend_dist(ws)
    run_backend_python_tests(paths["backend"])
    run_backend_node_tests(paths["backend"])
    run_docs_checks(paths["docs"])
    split_guard = paths["infra"] / "scripts" / "qg-split-path-guard.py"
    if split_guard.exists():
        run(["python3", str(split_guard), "--root", str(paths["infra"].parent)], paths["infra"])
    print("QG_WORKSPACE_CLOSED_LOOP_OK")


def check_path(path: pathlib.Path, should_exist: bool, label: str) -> bool:
    ok = path.exists() == should_exist
    print(f"{'OK' if ok else 'FAIL'}: {label} :: {path}")
    return ok


def cmd_verify(ws: dict[str, Any]) -> None:
    paths = repo_paths(ws)
    assert_workspace_paths(paths)
    checks = [
        (paths["backend"] / "tools", True, "backend tools present"),
        (paths["backend"] / "MQL5", True, "backend MQL5 present"),
        (paths["backend"] / "frontend", False, "backend frontend source removed"),
        (paths["backend"] / "cloudflare", False, "backend infra source removed"),
        (paths["frontend"] / "src", True, "frontend src present"),
        (paths["frontend"] / "Dashboard", False, "frontend has no backend Dashboard"),
        (paths["frontend"] / "MQL5", False, "frontend has no MQL5 source"),
        (paths["frontend"] / "tools", False, "frontend has no backend tools"),
        (paths["infra"] / "scripts" / "qg-workspace.py", True, "infra workspace helper present"),
        (paths["docs"] / "docs" / "architecture" / "repo-split.md", True, "docs split guide present"),
    ]

    failed = False
    for path, should_exist, label in checks:
        failed = not check_path(path, should_exist, label) or failed

    if failed:
        fail("workspace verification failed")
    check_manifest_remotes(paths)
    split_guard = paths["infra"] / "scripts" / "qg-split-path-guard.py"
    if split_guard.exists():
        run(["python3", str(split_guard), "--root", str(paths["infra"].parent)], paths["infra"])
    print("QG_WORKSPACE_VERIFY_OK")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QuantGod four-repo workspace helper")
    parser.add_argument(
        "command",
        choices=["status", "pull", "test", "build-frontend", "sync-frontend-dist", "verify", "closed-loop"],
    )
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    ws = load_workspace(pathlib.Path(args.workspace))
    {
        "status": cmd_status,
        "pull": cmd_pull,
        "test": cmd_test,
        "build-frontend": cmd_build_frontend,
        "sync-frontend-dist": cmd_sync_frontend_dist,
        "verify": cmd_verify,
        "closed-loop": cmd_closed_loop,
    }[args.command](ws)


if __name__ == "__main__":
    main()
