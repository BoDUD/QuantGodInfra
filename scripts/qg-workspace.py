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
LEGACY_REPO_NAME = "QuantGod"
LOCAL_TOOL_PREFIXES = (".codex/",)
LIVE_LANE_PRESET = "MQL5/Presets/QuantGod_MT5_HFM_LivePilot.set"
LIVE_LANE_POLICY_BUILDER = "tools/usdjpy_strategy_lab/policy_builder.py"
LEGACY_PRESET = LIVE_LANE_PRESET
LEGACY_POLICY_BUILDER = LIVE_LANE_POLICY_BUILDER
LIVE_LANE_ALLOWED_STRATEGIES_MARKERS = (
    'LIVE_ELIGIBLE_STRATEGIES = {"RSI_Reversal"}',
    "LIVE_ELIGIBLE_STRATEGIES = {'RSI_Reversal'}",
)
LIVE_LANE_ALLOWED_DIRECTION_MARKERS = (
    'LIVE_ELIGIBLE_DIRECTION = "LONG"',
    "LIVE_ELIGIBLE_DIRECTION = 'LONG'",
)
LIVE_LANE_FORBIDDEN_POLICY_NAMES = (
    "MA_Cross",
    "USDJPY_NIGHT_REVERSION_SAFE",
    "BB_Triple",
    "MACD_Divergence",
    "SR_Breakout",
)
LIVE_LANE_UNSAFE_PRESET_MARKERS = {
    "EnableNonRsiLegacyLiveAuthorization=true": "non-RSI legacy live authorization is enabled",
    "EnablePilotMA=true": "MA_Cross live switch is enabled in live preset",
    "EnablePilotBBH1Live=true": "BB_Triple live switch is enabled in live preset",
    "EnablePilotMacdH1Live=true": "MACD_Divergence live switch is enabled in live preset",
    "EnablePilotSRM15Live=true": "SR_Breakout live switch is enabled in live preset",
}
COMMON_LOCAL_IGNORE_PATTERNS = (
    ".codex/",
    ".DS_Store",
    ".env",
    ".env.*",
    "*.log",
    "__pycache__/",
    "*.pyc",
    "runtime/",
    "exports/",
    "cache/",
    ".cache/",
)
REPO_LOCAL_IGNORE_PATTERNS = {
    "backend": (
        "Dashboard/vue-dist/",
        "Dashboard/QuantGod_*.json",
        "Dashboard/QuantGod_*.csv",
        "archive/backtests/runs/",
        "archive/param-lab/runs/",
    ),
    "frontend": ("dist/", ".vite/", "coverage/"),
    "infra": ("workspace/*.local.json", ".wrangler/"),
    "docs": (),
}


def infra_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def resolve_workspace_path(
    raw_workspace: str,
    *,
    cwd: pathlib.Path | None = None,
    script_root: pathlib.Path | None = None,
) -> pathlib.Path:
    """Resolve workspace config from cwd, with a script-local default fallback."""

    raw_path = pathlib.Path(str(raw_workspace)).expanduser()
    if raw_path.is_absolute():
        return raw_path

    cwd = pathlib.Path.cwd() if cwd is None else pathlib.Path(cwd)
    cwd_candidate = cwd / raw_path
    if cwd_candidate.exists() or str(raw_workspace) != DEFAULT_WORKSPACE:
        return cwd_candidate

    root = infra_root() if script_root is None else pathlib.Path(script_root)
    script_candidate = root / raw_path
    if script_candidate.exists():
        return script_candidate
    return cwd_candidate


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


def run_capture(cmd: Sequence[str], cwd: pathlib.Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    printable = " ".join(str(part) for part in cmd)
    print(f"\n$ {printable}\n  cwd={cwd}")
    proc = subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout.rstrip())
        if proc.stderr:
            print(proc.stderr.rstrip(), file=sys.stderr)
    if check and proc.returncode != 0:
        fail(f"command failed with exit code {proc.returncode}: {printable}", proc.returncode)
    return proc


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


def git_lines(repo: pathlib.Path, args: Sequence[str]) -> list[str]:
    proc = subprocess.run(
        ["git", *[str(part) for part in args]],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def git_status_porcelain(repo: pathlib.Path) -> list[str]:
    return git_lines(repo, ["status", "--porcelain"])


def tracked_local_tool_files(repo: pathlib.Path) -> list[str]:
    tracked = git_lines(repo, ["ls-files", *LOCAL_TOOL_PREFIXES])
    return [path for path in tracked if any(path.startswith(prefix) for prefix in LOCAL_TOOL_PREFIXES)]


def is_git_repo(repo: pathlib.Path) -> bool:
    return (repo / ".git").exists()


def _ignore_patterns(repo: pathlib.Path) -> set[str]:
    gitignore = repo / ".gitignore"
    if not gitignore.exists():
        return set()
    patterns: set[str] = set()
    for raw in gitignore.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.add(line)
    return patterns


def local_artifact_ignore_issues(paths: dict[str, pathlib.Path]) -> list[str]:
    issues: list[str] = []
    for name, path in paths.items():
        if not is_git_repo(path):
            continue
        patterns = _ignore_patterns(path)
        required = set(COMMON_LOCAL_IGNORE_PATTERNS)
        required.update(REPO_LOCAL_IGNORE_PATTERNS.get(name, ()))
        missing = sorted(pattern for pattern in required if pattern not in patterns)
        if missing:
            issues.append(f"{name} repo .gitignore missing local artifact patterns: {', '.join(missing)}")
    return issues


def check_local_artifact_ignores(paths: dict[str, pathlib.Path]) -> None:
    issues = local_artifact_ignore_issues(paths)
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}")
        fail("workspace local artifact ignore policy failed")
    print("OK: active repo .gitignore files cover local artifacts")


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


def active_dirty_issues(paths: dict[str, pathlib.Path]) -> list[str]:
    issues: list[str] = []
    for name, path in paths.items():
        dirty = git_status_porcelain(path)
        if dirty:
            sample = ", ".join(dirty[:8])
            suffix = "" if len(dirty) <= 8 else f", ... +{len(dirty) - 8}"
            issues.append(f"{name} repo has uncommitted changes: {sample}{suffix}")
    return issues


def check_active_repos_clean(paths: dict[str, pathlib.Path]) -> None:
    issues = active_dirty_issues(paths)
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}")
        fail("active workspace repositories must be clean before verify passes")
    print("OK: active workspace repositories are clean")


def check_tracked_local_tools(paths: dict[str, pathlib.Path]) -> None:
    issues: list[str] = []
    for name, path in paths.items():
        tracked = tracked_local_tool_files(path)
        if tracked:
            sample = ", ".join(tracked[:8])
            suffix = "" if len(tracked) <= 8 else f", ... +{len(tracked) - 8}"
            issues.append(f"{name} repo tracks local Codex/tool files: {sample}{suffix}")
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}")
        fail("workspace contains tracked local tool files")
    print("OK: no active repo tracks .codex local tool files")


def legacy_repo_path(ws: dict[str, Any], paths: dict[str, pathlib.Path]) -> pathlib.Path:
    raw = str(ws.get("legacy") or ws.get("legacyMonorepo") or "").strip()
    if raw:
        path = pathlib.Path(raw).expanduser()
        if path.is_absolute():
            return path.resolve()
        return (workspace_root(ws) / path).resolve()
    return (paths["infra"].parent / LEGACY_REPO_NAME).resolve()


def live_lane_safety_issues(repo: pathlib.Path, *, repo_label: str = "repo") -> list[str]:
    issues: list[str] = []
    preset = repo / LIVE_LANE_PRESET
    if preset.exists():
        text = preset.read_text(encoding="utf-8", errors="ignore")
        for marker, message in LIVE_LANE_UNSAFE_PRESET_MARKERS.items():
            if marker in text:
                issues.append(f"{repo_label} {preset}: {message}")

    policy = repo / LIVE_LANE_POLICY_BUILDER
    if policy.exists():
        text = policy.read_text(encoding="utf-8", errors="ignore")
        if "LIVE_ELIGIBLE_STRATEGIES" in text and any(
            strategy_name in text for strategy_name in LIVE_LANE_FORBIDDEN_POLICY_NAMES
        ):
            issues.append(f"{repo_label} {policy}: live eligibility includes non-RSI strategy names")
        if not any(marker in text for marker in LIVE_LANE_ALLOWED_STRATEGIES_MARKERS):
            issues.append(f"{repo_label} {policy}: live eligibility is not locked to RSI_Reversal only")
        if "LIVE_ELIGIBLE_DIRECTION" in text and not any(
            marker in text for marker in LIVE_LANE_ALLOWED_DIRECTION_MARKERS
        ):
            issues.append(f"{repo_label} {policy}: live direction is not locked to LONG")
    return issues


def legacy_safety_issues(legacy: pathlib.Path) -> list[str]:
    return live_lane_safety_issues(legacy, repo_label="legacy")


def active_backend_live_lane_issues(backend: pathlib.Path) -> list[str]:
    return live_lane_safety_issues(backend, repo_label="active backend")


def check_active_backend_live_lane(backend: pathlib.Path) -> None:
    issues = active_backend_live_lane_issues(backend)
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}")
        fail("active backend live lane safety guard failed")
    print("OK: active backend live lane is locked to RSI_Reversal LONG only")


def print_legacy_status(ws: dict[str, Any], paths: dict[str, pathlib.Path]) -> None:
    legacy = legacy_repo_path(ws, paths)
    print(f"\n== legacy quarantine: {legacy}")
    if not legacy.exists():
        print("OK: legacy QuantGod monorepo not present")
        return
    print("QUARANTINE: legacy QuantGod is not an active workspace repo")
    origin = normalize_remote_url(git_origin_url(legacy))
    if origin:
        print(f"remote.origin={origin}")
    dirty = git_status_porcelain(legacy)
    if dirty:
        print("dirty:")
        for line in dirty[:12]:
            print(f"  {line}")
        if len(dirty) > 12:
            print(f"  ... +{len(dirty) - 12}")
    issues = legacy_safety_issues(legacy)
    if issues:
        print("unsafe legacy markers:")
        for issue in issues:
            print(f"  {issue}")


def check_legacy_quarantine(ws: dict[str, Any], paths: dict[str, pathlib.Path]) -> None:
    legacy = legacy_repo_path(ws, paths)
    if not legacy.exists():
        print("OK: legacy QuantGod monorepo absent")
        return
    print(f"WARN: legacy QuantGod monorepo present and quarantined :: {legacy}")
    issues = legacy_safety_issues(legacy)
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}")
        fail("legacy QuantGod quarantine has unsafe live eligibility residue")
    print("OK: legacy QuantGod quarantine has no unsafe non-RSI live markers")


def cmd_status(ws: dict[str, Any]) -> None:
    paths = repo_paths(ws)
    for name, path in paths.items():
        print(f"\n== {name}: {path}")
        if not path.exists():
            print("MISSING")
            continue
        run(["git", "status", "--short", "--branch"], path, check=False)
    print_legacy_status(ws, paths)


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


def run_docs_api_contract_strict(docs: pathlib.Path, backend: pathlib.Path) -> None:
    contract_check = docs / "scripts" / "check_api_contract_matches_backend.py"
    contract = docs / "docs" / "contracts" / "api-contract.json"
    if not contract_check.exists() or not contract.exists():
        print(f"skip docs API contract strict check: {contract_check} or {contract} not found")
        return
    run(
        [
            sys.executable,
            str(contract_check),
            "--contract",
            str(contract),
            "--backend",
            str(backend),
            "--strict-extra",
            "--min-endpoints",
            "100",
        ],
        docs,
    )


def run_backend_runtime_integrity_verify(backend: pathlib.Path) -> None:
    runtime_integrity = backend / "tools" / "run_runtime_evidence_integrity.py"
    if not runtime_integrity.exists():
        print(f"skip backend runtime evidence integrity verify: {runtime_integrity} not found")
        return
    proc = run_capture(
        [sys.executable, str(runtime_integrity), "--runtime-dir", "./runtime", "verify"],
        backend,
    )
    print_runtime_integrity_summary(proc.stdout)


def _short_text(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def runtime_recovery_label(row: dict[str, Any]) -> str:
    if row.get("timeframe"):
        return f"history:{row.get('timeframe')}"
    if row.get("category"):
        return f"case:{row.get('category')}"
    return str(row.get("kind") or row.get("artifactId") or "recovery")


def runtime_recovery_command(row: dict[str, Any]) -> str:
    for key in ("collectionCommand", "refreshCommand", "caseMemoryBuildCommand", "verifyCommand"):
        value = _short_text(row.get(key), 220)
        if value:
            return value
    return ""


def _runtime_hour_label(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.1f}h"


def runtime_recovery_context(row: dict[str, Any]) -> str:
    parts: list[str] = []
    copyrates_status = row.get("copyRatesExportFreshnessStatus")
    if not copyrates_status and row.get("copyRatesExportStale") is True:
        copyrates_status = "STALE"
    if copyrates_status:
        parts.append(f"CopyRates={copyrates_status}")
    latest_lag = _runtime_hour_label(row.get("copyRatesExportLatestLagHours") or row.get("latestLagHours"))
    if latest_lag:
        parts.append(f"latestLag={latest_lag}")

    sync_status = row.get("continuousSyncStatus")
    if not sync_status and row.get("continuousSyncRunning") is True:
        sync_status = "RUNNING"
    elif not sync_status and row.get("continuousSyncRunning") is False and row.get("continuousSyncScript"):
        sync_status = "MISSING"
    if sync_status:
        parts.append(f"SyncLoop={sync_status}")
    sync_count = row.get("continuousSyncMatchingProcessCount")
    if sync_count is not None:
        parts.append(f"syncMatches={sync_count}")
    return " ".join(parts)


def runtime_integrity_summary_lines(payload: dict[str, Any], *, recovery_limit: int = 7) -> list[str]:
    status = str(payload.get("status") or "UNKNOWN")
    gate = str(payload.get("promotionGateStatus") or "UNKNOWN")
    blockers = payload.get("promotionBlockers") if isinstance(payload.get("promotionBlockers"), list) else []
    queue = payload.get("promotionRecoveryQueue") if isinstance(payload.get("promotionRecoveryQueue"), list) else []
    lines = [
        (
            "Backend runtime evidence integrity: "
            f"status={status}, promotionGate={gate}, "
            f"promotionBlockers={len(blockers)}, recoveryQueue={len(queue)}"
        )
    ]
    next_action = _short_text(payload.get("nextActionZh"))
    if next_action:
        lines.append(f"  nextActionZh: {next_action}")
    if blockers:
        shown = ", ".join(str(item) for item in blockers[:8])
        suffix = f" (+{len(blockers) - 8} more)" if len(blockers) > 8 else ""
        lines.append(f"  promotionBlockers: {shown}{suffix}")
    if queue:
        lines.append("  promotionRecoveryQueue:")
        for row in queue[:recovery_limit]:
            if not isinstance(row, dict):
                continue
            label = runtime_recovery_label(row)
            status_text = str(row.get("status") or "UNKNOWN")
            priority = str(row.get("priority") or "")
            action = _short_text(row.get("nextActionZh"), 140)
            detail = f"    - {label}: {status_text}"
            if priority:
                detail += f" priority={priority}"
            context = runtime_recovery_context(row)
            if context:
                detail += f" [{context}]"
            if action:
                detail += f" :: {action}"
            lines.append(detail)
            sync_action = _short_text(row.get("continuousSyncNextActionZh"), 160)
            if sync_action:
                lines.append(f"      sync: {sync_action}")
            command = runtime_recovery_command(row)
            if command:
                lines.append(f"      command: {command}")
        hidden = len(queue) - min(len(queue), recovery_limit)
        if hidden > 0:
            lines.append(f"    ... {hidden} more recovery rows")
    return lines


def print_runtime_integrity_summary(stdout: str) -> None:
    text = str(stdout or "").strip()
    if not text:
        print("Backend runtime evidence integrity: no stdout payload")
        return
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        print("Backend runtime evidence integrity: non-JSON stdout")
        print(_short_text(text, 1000))
        return
    if not isinstance(payload, dict):
        print("Backend runtime evidence integrity: unexpected non-object JSON")
        return
    for line in runtime_integrity_summary_lines(payload):
        print(line)


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
        run(
            ["python3", str(split_guard), "--root", str(paths["infra"].parent), "--include-codex-automations"],
            paths["infra"],
        )
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
    check_active_repos_clean(paths)
    check_tracked_local_tools(paths)
    check_local_artifact_ignores(paths)
    check_active_backend_live_lane(paths["backend"])
    check_legacy_quarantine(ws, paths)
    check_manifest_remotes(paths)
    run_docs_api_contract_strict(paths["docs"], paths["backend"])
    run_backend_runtime_integrity_verify(paths["backend"])
    split_guard = paths["infra"] / "scripts" / "qg-split-path-guard.py"
    if split_guard.exists():
        run(
            ["python3", str(split_guard), "--root", str(paths["infra"].parent), "--include-codex-automations"],
            paths["infra"],
        )
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
    ws = load_workspace(resolve_workspace_path(args.workspace))
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
