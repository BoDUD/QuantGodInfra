#!/usr/bin/env python3
"""Install QuantGod macOS launchd background automation.

The launchd layer keeps local services and evidence loops running.  It is not a
trading permission layer: it does not write live presets, send orders, close
positions, or bypass QuantGod safety gates.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

INFRA_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = INFRA_ROOT / "workspace" / "quantgod.workspace.json"
LAUNCH_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
PRIVATE_ROOT = Path.home() / ".quantgod"
BIN_DIR = PRIVATE_ROOT / "bin"
LOG_DIR = PRIVATE_ROOT / "logs"
ENV_PATH = PRIVATE_ROOT / "launchd.env"
USER_DOMAIN = f"gui/{os.getuid()}"
LABEL_PREFIX = "com.quantgod"

SERVICES: dict[str, dict[str, Any]] = {
    "backend-api": {
        "label": f"{LABEL_PREFIX}.backend-api",
        "wrapper": "quantgod-backend-api.sh",
        "kind": "keepalive",
        "description": "Backend Node API and /vue static server",
    },
    "frontend-dev": {
        "label": f"{LABEL_PREFIX}.frontend-dev",
        "wrapper": "quantgod-frontend-dev.sh",
        "kind": "keepalive",
        "description": "Frontend Vite dev server",
    },
    "daily-autopilot": {
        "label": f"{LABEL_PREFIX}.daily-autopilot",
        "wrapper": "quantgod-daily-autopilot.sh",
        "kind": "interval",
        "interval": 300,
        "description": "QuantGod Agent v2.5 USDJPY live loop and autonomous daily review",
    },
    "usdjpy-history-sync": {
        "label": f"{LABEL_PREFIX}.usdjpy-history-sync",
        "wrapper": "quantgod-usdjpy-history-sync.sh",
        "kind": "interval",
        "interval": 3600,
        "description": "USDJPY MT5 historical K-line sync into SQLite",
    },
    "ai-telegram-monitor": {
        "label": f"{LABEL_PREFIX}.ai-telegram-monitor",
        "wrapper": "quantgod-ai-telegram-monitor.sh",
        "kind": "interval",
        "interval": 900,
        "description": "MT5/AI/DeepSeek advisory push-only Telegram monitor",
    },
}


def resolve_path(base: Path, raw: Any) -> Path:
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def load_workspace(path: Path) -> dict[str, Path]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    workspace_root = resolve_path(path.parent, payload.get("workspaceRoot", "."))
    return {
        "backend": resolve_path(workspace_root, payload["backend"]),
        "frontend": resolve_path(workspace_root, payload["frontend"]),
        "infra": resolve_path(workspace_root, payload.get("infra", INFRA_ROOT)),
        "docs": resolve_path(workspace_root, payload["docs"]),
    }


def quote_shell(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def which(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


def default_mt5_files_dir() -> Path:
    return (
        Path.home()
        / "Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Files"
    )


def default_mt5_terminal_path() -> Path:
    return (
        Path.home()
        / "Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/terminal64.exe"
    )


def render_env(paths: dict[str, Path]) -> str:
    mt5_files = default_mt5_files_dir()
    python_bin = which("python3", "/usr/bin/python3")
    runtime_dir = mt5_files if mt5_files.exists() else paths["backend"] / "Dashboard"
    rows = {
        "QG_BACKEND_ROOT": str(paths["backend"]),
        "QG_FRONTEND_ROOT": str(paths["frontend"]),
        "QG_INFRA_ROOT": str(paths["infra"]),
        "QG_DOCS_ROOT": str(paths["docs"]),
        "QG_NODE_BIN": which("node", "/usr/bin/node"),
        "QG_NPM_BIN": which("npm", "/usr/bin/npm"),
        "QG_PYTHON_BIN": python_bin,
        "QG_MT5_PYTHON_BIN": python_bin,
        "QG_MT5_TERMINAL_PATH": str(default_mt5_terminal_path()),
        "QG_DASHBOARD_HOST": "127.0.0.1",
        "QG_DASHBOARD_PORT": "8080",
        "QG_DASHBOARD_FILES_DIR": str(paths["backend"] / "Dashboard"),
        "QG_RUNTIME_DIR": str(runtime_dir),
        "QG_MT5_FILES_DIR": str(runtime_dir),
        "QG_MAC_RUNTIME_SOURCE": "mt5" if runtime_dir == mt5_files else "local",
        "QG_PARAMLAB_HFM_ROOT": str(paths["backend"] / "runtime/ParamLab_Tester_Sandbox/live_hfm_placeholder"),
        "QG_PARAMLAB_TESTER_ROOT": str(paths["backend"] / "runtime/HFM_MT5_Tester_Isolated"),
        "QG_MT5_TESTER_ROOT": str(paths["backend"] / "runtime/HFM_MT5_Tester_Isolated"),
        "QG_DAILY_AUTOPILOT_INTERVAL_MINUTES": "60",
        "QG_DAILY_AUTOPILOT_MAX_TASKS": "8",
        "QG_DAILY_AUTOPILOT_ALLOW_TESTER_RUN": "1",
        "QG_LEGACY_DAILY_AUTOPILOT_ENABLED": "0",
        "QG_AGENT_V25_INTERVAL_SECONDS": "300",
        "QG_AGENT_V25_SEND_TELEGRAM": "1",
        "QG_AGENT_V25_TELEGRAM_TIMEOUT_SECONDS": "60",
        "QG_AGENT_V25_FAST_TELEGRAM_GATEWAY": "0",
        "QG_AGENT_V25_HEAVY_TELEGRAM_GATEWAY": "1",
        "QG_AGENT_OPS_HEALTH_ENABLED": "1",
        "QG_PRODUCTION_BURN_IN_ENABLED": "1",
        "QG_PRODUCTION_BURN_IN_INTERVAL_SECONDS": "300",
        "QG_PRODUCTION_BURN_IN_SAMPLE_INTERVAL_MINUTES": "5",
        "QG_PRODUCTION_BURN_IN_WINDOW_HOURS": "72",
        "QG_PRODUCTION_BURN_IN_MAX_STALE_MINUTES": "15",
        "QG_USDJPY_HISTORY_SYNC_ENABLED": "1",
        "QG_USDJPY_HISTORY_INTERVAL_SECONDS": "3600",
        "QG_USDJPY_HISTORY_MONTHS": "12",
        "QG_USDJPY_HISTORY_TIMEFRAMES": "M1,M5,M15,H1",
        "QG_USDJPY_HISTORY_MAX_BARS": "700000",
        "QG_USDJPY_HISTORY_MAX_LAG_HOURS": "96",
        "QG_USDJPY_MT5_SYMBOL": "USDJPYc",
        "QG_FOCUS_SYMBOL": "USDJPYc",
        "QG_ALLOWED_SYMBOLS": "USDJPYc",
        "QG_DISABLE_NON_FOCUS_SYMBOLS": "1",
        "QG_ACCOUNT_MODE": "cent",
        "QG_ACCOUNT_CURRENCY_UNIT": "USC",
        "QG_CENT_ACCOUNT_ACCELERATION": "1",
        "QG_TELEGRAM_PUSH_ALLOWED": "1",
        "QG_TELEGRAM_COMMANDS_ALLOWED": "0",
        "QG_MT5_AI_DEEPSEEK_ENABLED": "1",
        "QG_AUTOMATION_SYMBOLS": "USDJPYc",
        "QG_MT5_AI_MONITOR_SYMBOLS": "USDJPYc",
        "QG_MT5_AI_MONITOR_TIMEFRAMES": "M15,H1,H4,D1",
        "QG_MT5_AI_MONITOR_MIN_INTERVAL_SECONDS": "900",
    }
    lines = [
        "# QuantGod private launchd environment.",
        "# This file is generated locally and must never be committed.",
        'export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"',
    ]
    lines.extend(f"export {key}={quote_shell(value)}" for key, value in rows.items())
    return "\n".join(lines) + "\n"


COMMON_WRAPPER = r'''#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${QG_LAUNCHD_ENV_FILE:-__ENV_PATH__}"

load_env_file() {
  local env_file="$1"
  local line key value
  [[ -f "$env_file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#$'\xef\xbb\xbf'}"
    line="${line#export }"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    value="${value%$'\r'}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$env_file"
}

load_env_file "$ENV_FILE"
load_env_file "${QG_BACKEND_ROOT}/.env.local"
load_env_file "${QG_BACKEND_ROOT}/.env.telegram.local"
load_env_file "${QG_BACKEND_ROOT}/.env.deepseek.local"

export PYTHONPATH="${QG_BACKEND_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "__LOG_DIR__"
'''


def wrapper_header() -> str:
    return COMMON_WRAPPER.replace("__ENV_PATH__", str(ENV_PATH)).replace("__LOG_DIR__", str(LOG_DIR))


def render_wrappers() -> dict[str, str]:
    header = wrapper_header()
    return {
        "quantgod-backend-api.sh": header
        + r'''
cd "$QG_BACKEND_ROOT"
exec "$QG_NODE_BIN" Dashboard/dashboard_server.js
''',
        "quantgod-frontend-dev.sh": header
        + r'''
cd "$QG_FRONTEND_ROOT"
exec "$QG_NPM_BIN" run dev -- --host 127.0.0.1 --port 5173
''',
        "quantgod-daily-autopilot.sh": header
        + r'''
cd "$QG_BACKEND_ROOT"
exec bash tools/run_mac_agent_v25_loop.sh --once
''',
        "quantgod-usdjpy-history-sync.sh": header
        + r'''
cd "$QG_BACKEND_ROOT"
exec bash tools/run_mac_usdjpy_history_sync_loop.sh --once
''',
        "quantgod-ai-telegram-monitor.sh": header
        + r'''
cd "$QG_BACKEND_ROOT"
exec "$QG_PYTHON_BIN" tools/run_mt5_ai_telegram_monitor.py scan-once \
  --send \
  --kind deepseek_insight \
  --runtime-dir "$QG_RUNTIME_DIR" \
  --repo-root "$QG_BACKEND_ROOT" \
  --env-file "$QG_BACKEND_ROOT/.env.telegram.local" \
  --deepseek-env-file "$QG_BACKEND_ROOT/.env.deepseek.local" \
  --symbols "${QG_MT5_AI_MONITOR_SYMBOLS:-USDJPYc}" \
  --timeframes "${QG_MT5_AI_MONITOR_TIMEFRAMES:-M15,H1,H4,D1}" \
  --min-interval-seconds "${QG_MT5_AI_MONITOR_MIN_INTERVAL_SECONDS:-900}"
''',
    }


def plist_path(label: str) -> Path:
    return LAUNCH_AGENT_DIR / f"{label}.plist"


def render_plist(service: dict[str, Any]) -> dict[str, Any]:
    label = service["label"]
    payload: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", str(BIN_DIR / service["wrapper"])],
        "WorkingDirectory": str(PRIVATE_ROOT),
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / f"{label}.out.log"),
        "StandardErrorPath": str(LOG_DIR / f"{label}.err.log"),
        "EnvironmentVariables": {
            "QG_LAUNCHD_ENV_FILE": str(ENV_PATH),
            "PYTHONIOENCODING": "utf-8",
        },
    }
    if service["kind"] == "keepalive":
        payload["KeepAlive"] = {"SuccessfulExit": False}
    else:
        payload["StartInterval"] = int(service["interval"])
    return payload


def run_launchctl(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=check)


def bootout(label: str) -> None:
    run_launchctl(["bootout", USER_DOMAIN, str(plist_path(label))], check=False)


def bootstrap(label: str) -> None:
    result = run_launchctl(["bootstrap", USER_DOMAIN, str(plist_path(label))], check=False)
    if result.returncode != 0 and "service already loaded" in (result.stderr or "").lower():
        bootout(label)
        result = run_launchctl(["bootstrap", USER_DOMAIN, str(plist_path(label))], check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"launchctl bootstrap failed for {label}")


def write_files(workspace: Path) -> dict[str, Path]:
    paths = load_workspace(workspace)
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text(render_env(paths), encoding="utf-8")
    ENV_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    for name, content in render_wrappers().items():
        write_executable(BIN_DIR / name, content)
    for service in SERVICES.values():
        with plist_path(service["label"]).open("wb") as handle:
            plistlib.dump(render_plist(service), handle, fmt=plistlib.FMT_XML, sort_keys=True)
    return paths


def install(args: argparse.Namespace) -> int:
    paths = write_files(args.workspace)
    labels = [service["label"] for service in SERVICES.values()]
    for label in labels:
        bootout(label)
    if not args.no_load:
        for label in labels:
            bootstrap(label)
    print("QuantGod macOS launchd automation installed.")
    print(f"Backend:  {paths['backend']}")
    print(f"Frontend: {paths['frontend']}")
    print(f"Env:      {ENV_PATH}")
    print(f"Logs:     {LOG_DIR}")
    if args.no_load:
        print("LaunchAgents were written but not loaded because --no-load was used.")
    else:
        print("Loaded services: " + ", ".join(labels))
    return 0


def uninstall(_args: argparse.Namespace) -> int:
    for service in SERVICES.values():
        label = service["label"]
        bootout(label)
        path = plist_path(label)
        if path.exists():
            path.unlink()
    print("QuantGod macOS LaunchAgents unloaded and removed. Private env/logs were kept under ~/.quantgod.")
    return 0


def status(_args: argparse.Namespace) -> int:
    for service in SERVICES.values():
        label = service["label"]
        result = run_launchctl(["print", f"{USER_DOMAIN}/{label}"], check=False)
        state = "loaded" if result.returncode == 0 else "not loaded"
        print(f"{label}: {state} - {service['description']}")
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("state =") or stripped.startswith("last exit code ="):
                    print(f"  {stripped}")
        else:
            err = (result.stderr or result.stdout or "").strip()
            if err:
                print(f"  {err.splitlines()[0]}")
    return 0


def doctor(args: argparse.Namespace) -> int:
    paths = load_workspace(args.workspace)
    print("QuantGod macOS launchd doctor")
    for key, path in paths.items():
        marker = "OK" if path.exists() else "MISSING"
        print(f"{key:8s} {marker:7s} {path}")
    print(f"node     {which('node', 'missing')}")
    print(f"npm      {which('npm', 'missing')}")
    python_bin = which("python3", "missing")
    print(f"python3  {python_bin}")
    print(f"mt5Files {'OK' if default_mt5_files_dir().exists() else 'MISSING'} {default_mt5_files_dir()}")
    print(f"mt5Term  {'OK' if default_mt5_terminal_path().exists() else 'MISSING'} {default_mt5_terminal_path()}")
    print(f"mt5Py    {_mt5_python_marker(python_bin)} {python_bin}")
    print(f"telegram {'OK' if (paths['backend'] / '.env.telegram.local').exists() else 'MISSING'}")
    print(f"deepseek {'OK' if (paths['backend'] / '.env.deepseek.local').exists() else 'MISSING'}")
    return 0


def _mt5_python_marker(python_bin: str) -> str:
    if python_bin == "missing":
        return "MISSING"
    result = subprocess.run(
        [python_bin, "-c", "import MetaTrader5"],
        text=True,
        capture_output=True,
        check=False,
    )
    return "OK" if result.returncode == 0 else "MISSING"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install QuantGod macOS launchd automation")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE, help="QuantGodInfra workspace JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install", help="Write and load LaunchAgents")
    install_parser.add_argument("--no-load", action="store_true", help="Write files without bootstrapping launchd")
    install_parser.set_defaults(func=install)
    sub.add_parser("uninstall", help="Unload and remove LaunchAgents").set_defaults(func=uninstall)
    sub.add_parser("status", help="Show launchd status").set_defaults(func=status)
    sub.add_parser("doctor", help="Check local paths and command dependencies").set_defaults(func=doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv or sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
