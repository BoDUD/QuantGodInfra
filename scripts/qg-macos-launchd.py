#!/usr/bin/env python3
"""Install QuantGod macOS launchd background automation.

The launchd layer keeps local services and evidence loops running.  It is not a
trading permission layer: it can publish the reviewed read-only secondary
observer preset, but it does not write live-enabled presets, send orders, close
positions, or bypass QuantGod safety gates.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import plistlib
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INFRA_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = INFRA_ROOT / "workspace" / "quantgod.workspace.json"
LAUNCH_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
PRIVATE_ROOT = Path.home() / ".quantgod"
BIN_DIR = PRIVATE_ROOT / "bin"
LOG_DIR = PRIVATE_ROOT / "logs"
LOCK_DIR = PRIVATE_ROOT / "locks"
STATUS_DIR = PRIVATE_ROOT / "status"
BACKUP_DIR = PRIVATE_ROOT / "backups" / "sqlite"
ENV_PATH = PRIVATE_ROOT / "launchd.env"
CAPABILITY_PATH = STATUS_DIR / "launchd-capabilities.json"
USER_DOMAIN = f"gui/{os.getuid()}"
LABEL_PREFIX = "com.quantgod"
MT5_EXPECTED_BROKER_SERVER = "HFMarketsGlobal-Live12"
MT5_SECONDARY_EXPECTED_BROKER_SERVER = "HFMarketsGlobal-Live16"
MT5_SECONDARY_PRESET_SOURCE_RELATIVE = Path(
    "MQL5/Presets/QuantGod_MT5_HFM_LiveSecondary.set"
)
MT5_SECONDARY_REQUIRED_PRESET_VALUES = {
    "Watchlist": "USDJPY",
    "PreferredSymbolSuffix": "AUTO",
    "ShadowMode": "true",
    "ReadOnlyMode": "true",
    "EnablePilotAutoTrading": "false",
    "EnablePilotRsiH1Live": "false",
    "EnablePilotBBH1Live": "false",
    "EnablePilotMacdH1Live": "false",
    "EnablePilotSRM15Live": "false",
    "EnableNonRsiLegacyLiveAuthorization": "false",
}
MT5_SECONDARY_PRESET_DEPLOYABLE_REASONS = frozenset(
    {
        "MT5_SHADOW_PRESET_MISSING",
        "MT5_SECONDARY_SHADOW_PRESET_SOURCE_MISMATCH",
        *(
            f"MT5_SHADOW_PRESET_{key.upper()}_INVALID"
            for key in MT5_SECONDARY_REQUIRED_PRESET_VALUES
        ),
        "MT5_SHADOW_PRESET_ORDERSENDALLOWED_DUPLICATE",
        "MT5_SHADOW_PRESET_ORDERSENDALLOWED_FORBIDDEN",
        "MT5_SHADOW_PRESET_BROKEREXECUTIONALLOWED_DUPLICATE",
        "MT5_SHADOW_PRESET_BROKEREXECUTIONALLOWED_FORBIDDEN",
        "MT5_SHADOW_PRESET_LIVEPRESETMUTATIONALLOWED_DUPLICATE",
        "MT5_SHADOW_PRESET_LIVEPRESETMUTATIONALLOWED_FORBIDDEN",
        "MT5_SHADOW_PRESET_ENABLELIVETRADING_DUPLICATE",
        "MT5_SHADOW_PRESET_ENABLELIVETRADING_FORBIDDEN",
    }
)
MT5_TERMINAL_PROCESS_PATTERN = (
    r"[/](wine64-preloader|wine-preloader|wine64|wine)[[:space:]]+"
    r"[t]erminal64\.exe([[:space:]]|$)"
)
PRIVATE_DIRECTORY_MODE = stat.S_IRWXU
PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
PRIVATE_EXECUTABLE_MODE = stat.S_IRWXU

SERVICES: dict[str, dict[str, Any]] = {
    "backend-api": {
        "label": f"{LABEL_PREFIX}.backend-api",
        "wrapper": "quantgod-backend-api.sh",
        "kind": "always",
        "throttle": 10,
        "description": "Backend Node API and /vue static server",
    },
    "frontend-dev": {
        "label": f"{LABEL_PREFIX}.frontend-dev",
        "wrapper": "quantgod-frontend-dev.sh",
        "kind": "keepalive",
        "description": "Frontend Vite dev server",
    },
    "frontend-dist-build": {
        "label": f"{LABEL_PREFIX}.frontend-dist-build",
        "wrapper": "quantgod-frontend-dist-build.sh",
        "kind": "oneshot",
        "description": "Build and atomically publish the compiled Frontend for Backend /vue",
    },
    "mt5-shadow-supervisor": {
        "label": f"{LABEL_PREFIX}.mt5-shadow-supervisor",
        "wrapper": "quantgod-mt5-shadow-supervisor.sh",
        "kind": "always",
        "throttle": 60,
        "description": "Strict single-instance MT5 Shadow/ReadOnly supervisor; Live is forbidden",
    },
    "mt5-secondary-shadow-supervisor": {
        "label": f"{LABEL_PREFIX}.mt5-secondary-shadow-supervisor",
        "wrapper": "quantgod-mt5-secondary-shadow-supervisor.sh",
        "kind": "always",
        "throttle": 60,
        "description": "Strict secondary MT5 Shadow/ReadOnly supervisor for the isolated Live16 prefix",
    },
    "daily-autopilot": {
        "label": f"{LABEL_PREFIX}.daily-autopilot",
        "wrapper": "quantgod-daily-autopilot.sh",
        "kind": "interval",
        "interval": 300,
        "description": "Deprecated Agent v2.5 loop; blocked until required-stage failures propagate",
    },
    "usdjpy-history-sync": {
        "label": f"{LABEL_PREFIX}.usdjpy-history-sync",
        "wrapper": "quantgod-usdjpy-history-sync.sh",
        "kind": "interval",
        "interval": 3600,
        "description": "USDJPY MT5 historical K-line sync into SQLite",
    },
    "automation-chain": {
        "label": f"{LABEL_PREFIX}.automation-chain",
        "wrapper": "quantgod-automation-chain.sh",
        "kind": "interval",
        "interval": 300,
        "description": "USDJPY advisory-only automation chain with required-step exit validation",
    },
    "health-maintenance": {
        "label": f"{LABEL_PREFIX}.health-maintenance",
        "wrapper": "quantgod-health-maintenance.sh",
        "kind": "interval",
        "interval": 60,
        "description": "Local AgentOps/runtime health evidence refresh",
    },
    "log-maintenance": {
        "label": f"{LABEL_PREFIX}.log-maintenance",
        "wrapper": "quantgod-log-maintenance.sh",
        "kind": "interval",
        "interval": 3600,
        "description": "Independent local runtime and launchd log rotation",
    },
    "sqlite-backup": {
        "label": f"{LABEL_PREFIX}.sqlite-backup",
        "wrapper": "quantgod-sqlite-backup.sh",
        "kind": "interval",
        "interval": 86400,
        "description": "Verified online backup of local QuantGod SQLite stores",
    },
    "ai-telegram-monitor": {
        "label": f"{LABEL_PREFIX}.ai-telegram-monitor",
        "wrapper": "quantgod-ai-telegram-monitor.sh",
        "kind": "interval",
        "interval": 900,
        "description": "MT5/AI/DeepSeek advisory push-only Telegram monitor",
    },
}

SERVICE_PROFILES: dict[str, tuple[str, ...]] = {
    # Installation must be safe when the operator supplies no optional flags.
    # Research jobs and outbound notification services are explicit opt-ins.
    "core": ("backend-api", "frontend-dev"),
    "local-shadow": (
        "frontend-dist-build",
        "backend-api",
        "mt5-shadow-supervisor",
        "usdjpy-history-sync",
        "automation-chain",
        "health-maintenance",
        "log-maintenance",
        "sqlite-backup",
    ),
    "local-dual-shadow": (
        "frontend-dist-build",
        "backend-api",
        "mt5-shadow-supervisor",
        "mt5-secondary-shadow-supervisor",
        "usdjpy-history-sync",
        "automation-chain",
        "health-maintenance",
        "log-maintenance",
        "sqlite-backup",
    ),
    "research": (
        "backend-api",
        "frontend-dev",
        "usdjpy-history-sync",
        "automation-chain",
        "health-maintenance",
        "log-maintenance",
        "sqlite-backup",
    ),
    "all": tuple(SERVICES),
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


def atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    """Publish one complete managed file without exposing partial contents."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlink for QuantGod managed file: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str, mode: int) -> None:
    atomic_write_bytes(path, content.encode("utf-8"), mode)


def write_executable(path: Path, content: str) -> None:
    atomic_write_text(path, content, PRIVATE_EXECUTABLE_MODE)


def harden_private_directory(path: Path) -> None:
    try:
        relative = path.relative_to(PRIVATE_ROOT)
    except ValueError:
        relative = None
    if relative is None:
        if path.is_symlink():
            raise RuntimeError(f"refusing symlink for QuantGod private directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(PRIVATE_DIRECTORY_MODE)
        return

    current = PRIVATE_ROOT
    for part in ((), *relative.parts):
        if part:
            current /= part
        if current.is_symlink():
            raise RuntimeError(f"refusing symlink in QuantGod private directory chain: {current}")
        current.mkdir(exist_ok=True)
        current.chmod(PRIVATE_DIRECTORY_MODE)


def harden_private_file(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing symlink for QuantGod private file: {path}")
    path.chmod(PRIVATE_FILE_MODE)


def harden_log_files() -> None:
    for path in LOG_DIR.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"refusing symlink in QuantGod private log directory: {path}")
        if path.is_dir():
            path.chmod(PRIVATE_DIRECTORY_MODE)
        elif path.is_file():
            path.chmod(PRIVATE_FILE_MODE)


def which(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


def local_user_environment() -> dict[str, str]:
    """Return the minimal non-secret user context required by Wine under launchd."""

    account = pwd.getpwuid(os.getuid())
    tmp_raw = os.environ.get("TMPDIR") or tempfile.gettempdir()
    lang = os.environ.get("LANG") or "en_US.UTF-8"
    return {
        "QG_USER_HOME": str(Path(account.pw_dir).expanduser().resolve()),
        "QG_LOCAL_USER": account.pw_name,
        "QG_USER_TMPDIR": str(Path(tmp_raw).expanduser().resolve()),
        "QG_USER_LANG": lang,
    }


def local_user_environment_issues(values: dict[str, str] | None = None) -> list[str]:
    """Validate generated Wine user context without accepting ambient launchd gaps."""

    try:
        account = pwd.getpwuid(os.getuid())
        rows = values or local_user_environment()
    except (KeyError, OSError, RuntimeError):
        return ["MT5_LOCAL_USER_LOOKUP_FAILED"]

    issues: list[str] = []
    if rows.get("QG_LOCAL_USER") != account.pw_name:
        issues.append("MT5_LOCAL_USER_MISMATCH")

    expected_home = Path(account.pw_dir).expanduser().resolve()
    for key, code, expected in (
        ("QG_USER_HOME", "MT5_USER_HOME", expected_home),
        ("QG_USER_TMPDIR", "MT5_USER_TMPDIR", None),
    ):
        raw = str(rows.get(key) or "")
        path = Path(raw)
        if not raw or not path.is_absolute():
            issues.append(f"{code}_NOT_ABSOLUTE")
            continue
        try:
            resolved = path.resolve()
            metadata = path.stat()
        except (OSError, RuntimeError):
            issues.append(f"{code}_UNAVAILABLE")
            continue
        if path.is_symlink() or resolved != path:
            issues.append(f"{code}_SYMLINKED")
        if not path.is_dir():
            issues.append(f"{code}_NOT_DIRECTORY")
        if metadata.st_uid != os.getuid():
            issues.append(f"{code}_OWNER_MISMATCH")
        if expected is not None and resolved != expected:
            issues.append(f"{code}_ACCOUNT_MISMATCH")

    lang = str(rows.get("QG_USER_LANG") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", lang):
        issues.append("MT5_USER_LANG_INVALID")
    return sorted(set(issues))


def default_mt5_prefix() -> Path:
    return Path.home() / "Library/Application Support/net.metaquotes.wine.metatrader5"


def default_mt5_root() -> Path:
    return default_mt5_prefix() / "drive_c/Program Files/MetaTrader 5"


def default_mt5_files_dir() -> Path:
    return default_mt5_root() / "MQL5/Files"


def default_mt5_terminal_path() -> Path:
    return default_mt5_root() / "terminal64.exe"


def default_mt5_wine_bin() -> Path:
    return Path.home() / "Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64"


def default_mt5_shadow_config() -> Path:
    return default_mt5_prefix() / "drive_c/qg/QuantGod_MT5_HFM_Shadow_mac.ini"


def default_mt5_login_reference_config() -> Path:
    return default_mt5_prefix() / "drive_c/qg/QuantGod_MT5_LoginOnly_mac.ini"


def default_mt5_shadow_preset() -> Path:
    return default_mt5_root() / "MQL5/Presets/QuantGod_MT5_HFM_Shadow.set"


def default_mt5_verified_ea_source() -> Path:
    return default_mt5_prefix() / "drive_c/qg/QuantGod_MultiStrategy.mq5"


def default_mt5_verified_ea_binary() -> Path:
    return default_mt5_prefix() / "drive_c/qg/QuantGod_MultiStrategy.ex5"


def default_mt5_verified_ea_compile_log() -> Path:
    return default_mt5_prefix() / "drive_c/qg/compile.log"


def default_mt5_secondary_prefix() -> Path:
    return Path.home() / "Library/Application Support/net.metaquotes.wine.metatrader5-live16"


def default_mt5_secondary_root() -> Path:
    return default_mt5_secondary_prefix() / "drive_c/Program Files/MetaTrader 5"


def default_mt5_secondary_files_dir() -> Path:
    return default_mt5_secondary_root() / "MQL5/Files"


def default_mt5_secondary_terminal_path() -> Path:
    return default_mt5_secondary_root() / "terminal64.exe"


def default_mt5_secondary_shadow_config() -> Path:
    return default_mt5_secondary_prefix() / "drive_c/qg/QuantGod_MT5_HFM_SecondaryShadow_mac.ini"


def default_mt5_secondary_login_reference_config() -> Path:
    return default_mt5_secondary_prefix() / "drive_c/qg/QuantGod_MT5_LoginOnly_mac.ini"


def default_mt5_secondary_shadow_preset() -> Path:
    return default_mt5_secondary_root() / "MQL5/Presets/QuantGod_MT5_HFM_Shadow.set"


def backend_mt5_secondary_shadow_preset(paths: dict[str, Path]) -> Path:
    """Return the tracked source for the isolated USD-denominated observer preset."""

    return paths["backend"] / MT5_SECONDARY_PRESET_SOURCE_RELATIVE


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_key_value_lists(path: Path) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key.strip().lower(), []).append(value.strip())
    return values


def _preset_value_issues(
    preset_values: dict[str, list[str]],
    required_values: dict[str, str],
    *,
    code_prefix: str,
) -> list[str]:
    issues: list[str] = []
    for key, expected in required_values.items():
        candidates = preset_values.get(key.lower(), [])
        if len(candidates) != 1 or candidates[0].lower() != expected.lower():
            issues.append(f"{code_prefix}_{key.upper()}_INVALID")
    forbidden_true_keys = (
        "OrderSendAllowed",
        "BrokerExecutionAllowed",
        "LivePresetMutationAllowed",
        "EnableLiveTrading",
    )
    for key in forbidden_true_keys:
        candidates = preset_values.get(key.lower(), [])
        if len(candidates) > 1:
            issues.append(f"{code_prefix}_{key.upper()}_DUPLICATE")
        if any(value.lower() in {"1", "true", "yes", "on"} for value in candidates):
            issues.append(f"{code_prefix}_{key.upper()}_FORBIDDEN")
    return issues


def secondary_shadow_preset_source_issues(source: Path) -> list[str]:
    """Validate the Backend-owned LiveSecondary source without exposing its content."""

    issues: list[str] = []
    if source.name != MT5_SECONDARY_PRESET_SOURCE_RELATIVE.name:
        issues.append("MT5_SECONDARY_PRESET_SOURCE_NAME_INVALID")
    if not source.is_absolute():
        issues.append("MT5_SECONDARY_PRESET_SOURCE_PATH_INVALID")
    elif source.is_symlink() or (
        source.parent.exists() and source.parent.resolve() != source.parent.absolute()
    ):
        issues.append("MT5_SECONDARY_PRESET_SOURCE_SYMLINKED")
    if not source.is_file():
        issues.append("MT5_SECONDARY_PRESET_SOURCE_MISSING")
    if issues:
        return issues
    try:
        values = _read_key_value_lists(source)
    except OSError:
        return ["MT5_SECONDARY_PRESET_SOURCE_UNREADABLE"]
    return _preset_value_issues(
        values,
        MT5_SECONDARY_REQUIRED_PRESET_VALUES,
        code_prefix="MT5_SECONDARY_PRESET_SOURCE",
    )


def secondary_shadow_preset_directory_issues(target: Path) -> list[str]:
    if not target.is_absolute() or not target.parent.is_dir():
        return ["MT5_SECONDARY_PRESET_DIRECTORY_INVALID"]
    if target.parent.is_symlink() or target.parent.resolve() != target.parent.absolute():
        return ["MT5_SECONDARY_PRESET_DIRECTORY_SYMLINKED"]
    return []


def shadow_preset_guard_issues(
    config: Path,
    preset: Path,
    login_reference: Path | None = None,
    *,
    expected_server: str = MT5_EXPECTED_BROKER_SERVER,
    expected_config_name: str = "QuantGod_MT5_HFM_Shadow_mac.ini",
    required_preset_values: dict[str, str] | None = None,
) -> list[str]:
    """Return non-secret reason codes when the installed MT5 lane is not strictly Shadow."""

    login_reference = login_reference or config.with_name("QuantGod_MT5_LoginOnly_mac.ini")
    issues: list[str] = []
    if config.name != expected_config_name:
        issues.append("MT5_SHADOW_CONFIG_NAME_INVALID")
    if preset.name != "QuantGod_MT5_HFM_Shadow.set":
        issues.append("MT5_SHADOW_PRESET_NAME_INVALID")
    if login_reference.name != "QuantGod_MT5_LoginOnly_mac.ini":
        issues.append("MT5_LOGIN_REFERENCE_NAME_INVALID")
    if config.is_symlink():
        issues.append("MT5_SHADOW_CONFIG_SYMLINKED")
    if preset.is_symlink():
        issues.append("MT5_SHADOW_PRESET_SYMLINKED")
    if login_reference.is_symlink():
        issues.append("MT5_LOGIN_REFERENCE_SYMLINKED")
    if not config.is_file():
        issues.append("MT5_SHADOW_CONFIG_MISSING")
    if not preset.is_file():
        issues.append("MT5_SHADOW_PRESET_MISSING")
    if not login_reference.is_file():
        issues.append("MT5_LOGIN_REFERENCE_MISSING")
    if issues:
        return issues

    config_values = _read_key_value_lists(config)
    preset_values = _read_key_value_lists(preset)
    login_reference_values = _read_key_value_lists(login_reference)
    broker_servers = config_values.get("server", [])
    if broker_servers != [expected_server]:
        issues.append("MT5_SHADOW_CONFIG_SERVER_INVALID")
    if login_reference_values.get("server", []) != [expected_server]:
        issues.append("MT5_LOGIN_REFERENCE_SERVER_INVALID")
    shadow_logins = config_values.get("login", [])
    reference_logins = login_reference_values.get("login", [])
    shadow_login_valid = len(shadow_logins) == 1 and bool(re.fullmatch(r"[0-9]+", shadow_logins[0]))
    reference_login_valid = len(reference_logins) == 1 and bool(
        re.fullmatch(r"[0-9]+", reference_logins[0])
    )
    if not shadow_login_valid:
        issues.append("MT5_SHADOW_CONFIG_LOGIN_INVALID")
    if not reference_login_valid:
        issues.append("MT5_LOGIN_REFERENCE_LOGIN_INVALID")
    if shadow_login_valid and reference_login_valid and not hmac.compare_digest(
        shadow_logins[0], reference_logins[0]
    ):
        issues.append("MT5_SHADOW_LOGIN_MISMATCH")
    required_config = {
        "AllowLiveTrading": "0",
        "AllowDllImport": "0",
        "ExpertParameters": "QuantGod_MT5_HFM_Shadow.set",
    }
    required_preset = required_preset_values or {
        "ShadowMode": "true",
        "ReadOnlyMode": "true",
        "EnablePilotAutoTrading": "false",
    }
    for key, expected in required_config.items():
        candidates = config_values.get(key.lower(), [])
        if len(candidates) != 1 or candidates[0].lower() != expected.lower():
            issues.append(f"MT5_SHADOW_CONFIG_{key.upper()}_INVALID")
    issues.extend(
        _preset_value_issues(
            preset_values,
            required_preset,
            code_prefix="MT5_SHADOW_PRESET",
        )
    )
    forbidden_true_keys = (
        "OrderSendAllowed",
        "BrokerExecutionAllowed",
        "LivePresetMutationAllowed",
        "EnableLiveTrading",
    )
    for source_name, source_values in (("CONFIG", config_values),):
        for key in forbidden_true_keys:
            candidates = source_values.get(key.lower(), [])
            if len(candidates) > 1:
                issues.append(f"MT5_SHADOW_{source_name}_{key.upper()}_DUPLICATE")
            if any(value.lower() in {"1", "true", "yes", "on"} for value in candidates):
                issues.append(f"MT5_SHADOW_{source_name}_{key.upper()}_FORBIDDEN")
    return issues


def _path_issue(path: Path, code: str, *, executable: bool = False, directory: bool = False) -> str | None:
    if directory:
        return None if path.is_dir() else code
    if not path.is_file():
        return code
    if executable and not os.access(path, os.X_OK):
        return code
    return None


def service_capability(
    name: str,
    paths: dict[str, Path],
    *,
    workspace: Path = DEFAULT_WORKSPACE,
) -> dict[str, Any]:
    """Describe whether a generated service has reviewed, local-only dependencies."""

    backend = paths["backend"]
    frontend = paths["frontend"]
    infra = paths["infra"]
    runtime = default_mt5_files_dir() if default_mt5_files_dir().is_dir() else backend / "Dashboard"
    issues: list[str] = []

    def require(path: Path, code: str, *, executable: bool = False, directory: bool = False) -> None:
        issue = _path_issue(path, code, executable=executable, directory=directory)
        if issue:
            issues.append(issue)

    if name == "backend-api":
        require(backend / "Dashboard/dashboard_server.js", "BACKEND_SERVER_MISSING")
        require(backend / "Dashboard/vue-dist/index.html", "COMPILED_FRONTEND_MISSING")
        if shutil.which("node") is None:
            issues.append("NODE_MISSING")
    elif name == "frontend-dev":
        require(frontend / "package.json", "FRONTEND_PACKAGE_MISSING")
        require(frontend / "node_modules/.bin/vite", "FRONTEND_VITE_MISSING", executable=True)
        if shutil.which("npm") is None:
            issues.append("NPM_MISSING")
    elif name == "frontend-dist-build":
        require(frontend / "package.json", "FRONTEND_PACKAGE_MISSING")
        require(frontend / "node_modules/.bin/vite", "FRONTEND_VITE_MISSING", executable=True)
        require(infra / "scripts/qg-workspace.py", "WORKSPACE_HELPER_MISSING")
        require(workspace, "WORKSPACE_CONFIG_MISSING")
        if shutil.which("npm") is None:
            issues.append("NPM_MISSING")
    elif name == "mt5-shadow-supervisor":
        require(default_mt5_wine_bin(), "MT5_WINE_MISSING", executable=True)
        require(default_mt5_terminal_path(), "MT5_TERMINAL_MISSING")
        require(default_mt5_root() / "MQL5/Experts/QuantGod_MultiStrategy.mq5", "MT5_SHADOW_EA_SOURCE_MISSING")
        require(default_mt5_root() / "MQL5/Experts/QuantGod_MultiStrategy.ex5", "MT5_SHADOW_EA_BINARY_MISSING")
        require(default_mt5_verified_ea_source(), "MT5_VERIFIED_EA_SOURCE_MISSING")
        require(default_mt5_verified_ea_binary(), "MT5_VERIFIED_EA_BINARY_MISSING")
        require(default_mt5_verified_ea_compile_log(), "MT5_VERIFIED_EA_COMPILE_LOG_MISSING")
        issues.extend(shadow_preset_guard_issues(default_mt5_shadow_config(), default_mt5_shadow_preset()))
        issues.extend(local_user_environment_issues())
    elif name == "mt5-secondary-shadow-supervisor":
        secondary_source = backend_mt5_secondary_shadow_preset(paths)
        secondary_target = default_mt5_secondary_shadow_preset()
        require(default_mt5_wine_bin(), "MT5_WINE_MISSING", executable=True)
        require(default_mt5_secondary_terminal_path(), "MT5_SECONDARY_TERMINAL_MISSING")
        require(
            default_mt5_secondary_root() / "MQL5/Experts/QuantGod_MultiStrategy.mq5",
            "MT5_SECONDARY_SHADOW_EA_SOURCE_MISSING",
        )
        require(
            default_mt5_secondary_root() / "MQL5/Experts/QuantGod_MultiStrategy.ex5",
            "MT5_SECONDARY_SHADOW_EA_BINARY_MISSING",
        )
        require(default_mt5_verified_ea_source(), "MT5_VERIFIED_EA_SOURCE_MISSING")
        require(default_mt5_verified_ea_binary(), "MT5_VERIFIED_EA_BINARY_MISSING")
        require(default_mt5_verified_ea_compile_log(), "MT5_VERIFIED_EA_COMPILE_LOG_MISSING")
        source_issues = secondary_shadow_preset_source_issues(secondary_source)
        issues.extend(source_issues)
        issues.extend(secondary_shadow_preset_directory_issues(secondary_target))
        issues.extend(
            shadow_preset_guard_issues(
                default_mt5_secondary_shadow_config(),
                secondary_target,
                default_mt5_secondary_login_reference_config(),
                expected_server=MT5_SECONDARY_EXPECTED_BROKER_SERVER,
                expected_config_name="QuantGod_MT5_HFM_SecondaryShadow_mac.ini",
                required_preset_values=MT5_SECONDARY_REQUIRED_PRESET_VALUES,
            )
        )
        if not source_issues and secondary_target.is_file():
            try:
                source_matches_target = hmac.compare_digest(
                    secondary_source.read_bytes(), secondary_target.read_bytes()
                )
            except OSError:
                issues.append("MT5_SECONDARY_SHADOW_PRESET_UNREADABLE")
            else:
                if not source_matches_target:
                    issues.append("MT5_SECONDARY_SHADOW_PRESET_SOURCE_MISMATCH")
        issues.extend(local_user_environment_issues())
    elif name == "usdjpy-history-sync":
        require(backend / "tools/run_usdjpy_strategy_backtest.py", "HISTORY_SYNC_RUNNER_MISSING")
        require(default_mt5_terminal_path(), "MT5_TERMINAL_MISSING")
        if shutil.which("python3") is None:
            issues.append("PYTHON3_MISSING")
    elif name == "automation-chain":
        for relative in (
            "tools/run_automation_chain.py",
            "tools/run_adaptive_policy.py",
            "tools/run_dynamic_sltp.py",
            "tools/run_entry_trigger_lab.py",
            "tools/run_usdjpy_strategy_lab.py",
            "tools/run_usdjpy_live_loop.py",
        ):
            require(backend / relative, f"AUTOMATION_DEPENDENCY_MISSING:{relative}")
    elif name == "health-maintenance":
        require(backend / "tools/run_agent_ops_health.py", "HEALTH_RUNNER_MISSING")
        require(backend / "Dashboard/health_api_routes.js", "HEALTH_ENDPOINT_ROUTES_MISSING")
    elif name == "log-maintenance":
        require(backend / "tools/maintain_runtime_logs.py", "LOG_MAINTENANCE_RUNNER_MISSING")
    elif name == "sqlite-backup":
        require(backend / "tools/run_local_shadow_backup.py", "SQLITE_BACKUP_RUNNER_MISSING")
        require(runtime / "QuantGod_MT5Platform.db", "PLATFORM_DB_MISSING")
        require(runtime / "backtest/usdjpy.sqlite", "HISTORY_DB_MISSING")
    elif name == "daily-autopilot":
        issues.append("LEGACY_AGENT_FAILURE_CONTRACT_UNSAFE")
    elif name == "ai-telegram-monitor":
        require(backend / "tools/run_mt5_ai_telegram_monitor.py", "AI_MONITOR_RUNNER_MISSING")
    else:
        issues.append("UNKNOWN_SERVICE")

    return {
        "service": name,
        "label": SERVICES[name]["label"],
        "status": "READY" if not issues else "BLOCKED",
        "ready": not issues,
        "reasonCodes": sorted(set(issues)),
    }


def build_capability_report(
    paths: dict[str, Path],
    *,
    workspace: Path = DEFAULT_WORKSPACE,
) -> dict[str, Any]:
    services = {
        name: service_capability(name, paths, workspace=workspace)
        for name in SERVICES
    }
    return {
        "schema": "quantgod.launchd_capabilities.v1",
        "generatedAt": _utc_now_iso(),
        "safety": {
            "localOnly": True,
            "shadowOnly": True,
            "liveAllowed": False,
            "orderSendAllowed": False,
            "livePresetMutationAllowed": False,
        },
        "services": services,
    }


def render_env(
    paths: dict[str, Path],
    *,
    workspace: Path = DEFAULT_WORKSPACE,
    secondary_shadow_enabled: bool = False,
) -> str:
    mt5_files = default_mt5_files_dir()
    python_bin = which("python3", "/usr/bin/python3")
    runtime_dir = mt5_files if mt5_files.exists() else paths["backend"] / "Dashboard"
    rows = {
        "QG_BACKEND_ROOT": str(paths["backend"]),
        "QG_FRONTEND_ROOT": str(paths["frontend"]),
        "QG_INFRA_ROOT": str(paths["infra"]),
        "QG_DOCS_ROOT": str(paths["docs"]),
        "QG_WORKSPACE_FILE": str(workspace.expanduser().resolve()),
        "QG_PRIVATE_ROOT": str(PRIVATE_ROOT),
        "QG_LAUNCHD_LOG_ROOT": str(LOG_DIR),
        "QG_LAUNCHD_LOCK_ROOT": str(LOCK_DIR),
        "QG_LAUNCHD_STATUS_ROOT": str(STATUS_DIR),
        "QG_LOCAL_SQLITE_BACKUP_ROOT": str(BACKUP_DIR),
        "QG_NODE_BIN": which("node", "/usr/bin/node"),
        "QG_NPM_BIN": which("npm", "/usr/bin/npm"),
        "QG_PYTHON_BIN": python_bin,
        "QG_MT5_PYTHON_BIN": python_bin,
        "QG_MT5_WINE_PREFIX": str(default_mt5_prefix()),
        "QG_MT5_ROOT": str(default_mt5_root()),
        "QG_MT5_WINE_BIN": str(default_mt5_wine_bin()),
        "QG_MT5_TERMINAL_PATH": str(default_mt5_terminal_path()),
        "QG_MT5_SHADOW_CONFIG": str(default_mt5_shadow_config()),
        "QG_MT5_LOGIN_REFERENCE_CONFIG": str(default_mt5_login_reference_config()),
        "QG_MT5_SHADOW_PRESET": str(default_mt5_shadow_preset()),
        "QG_MT5_VERIFIED_EA_SOURCE": str(default_mt5_verified_ea_source()),
        "QG_MT5_VERIFIED_EA_BINARY": str(default_mt5_verified_ea_binary()),
        "QG_MT5_VERIFIED_EA_COMPILE_LOG": str(default_mt5_verified_ea_compile_log()),
        "QG_MT5_SHADOW_CONFIG_WINDOWS": r"C:\qg\QuantGod_MT5_HFM_Shadow_mac.ini",
        "QG_MT5_EXPECTED_SERVER": MT5_EXPECTED_BROKER_SERVER,
        "QG_MT5_PEER_ROOT": str(default_mt5_secondary_root()),
        "QG_MT5_PEER_TERMINAL_PATH": str(default_mt5_secondary_terminal_path()),
        "QG_MT5_PEER_SHADOW_CONFIG_WINDOWS": r"C:\qg\QuantGod_MT5_HFM_SecondaryShadow_mac.ini",
        "QG_EXECUTION_MODE": "SHADOW",
        "QG_MT5_START_MODE": "shadow",
        "QG_MT5_LIVE_LAUNCH_ALLOWED": "0",
        "QG_MT5_SECONDARY_ENABLED": "0",
        "QG_MT5_SECONDARY_SHADOW_ENABLED": "1" if secondary_shadow_enabled else "0",
        "QG_MT5_SECONDARY_ALLOW_LIVE_TRADING": "0",
        "QG_MT5_SECONDARY_WINE_PREFIX": str(default_mt5_secondary_prefix()),
        "QG_MT5_SECONDARY_ROOT": str(default_mt5_secondary_root()),
        "QG_MT5_SECONDARY_FILES_DIR": str(default_mt5_secondary_files_dir()),
        "QG_MT5_SECONDARY_TERMINAL_PATH": str(default_mt5_secondary_terminal_path()),
        "QG_MT5_SECONDARY_SHADOW_CONFIG": str(default_mt5_secondary_shadow_config()),
        "QG_MT5_SECONDARY_LOGIN_REFERENCE_CONFIG": str(
            default_mt5_secondary_login_reference_config()
        ),
        "QG_MT5_SECONDARY_SHADOW_PRESET": str(default_mt5_secondary_shadow_preset()),
        "QG_MT5_SECONDARY_SHADOW_PRESET_SOURCE": str(
            backend_mt5_secondary_shadow_preset(paths)
        ),
        "QG_MT5_SECONDARY_SHADOW_CONFIG_WINDOWS": r"C:\qg\QuantGod_MT5_HFM_SecondaryShadow_mac.ini",
        "QG_MT5_SECONDARY_EXPECTED_SERVER": MT5_SECONDARY_EXPECTED_BROKER_SERVER,
        "QG_MT5_SECONDARY_PEER_ROOT": str(default_mt5_root()),
        "QG_MT5_SECONDARY_PEER_TERMINAL_PATH": str(default_mt5_terminal_path()),
        "QG_MT5_SECONDARY_PEER_SHADOW_CONFIG_WINDOWS": r"C:\qg\QuantGod_MT5_HFM_Shadow_mac.ini",
        "QG_ORDER_SEND_ALLOWED": "0",
        "QG_BROKER_EXECUTION_ALLOWED": "0",
        "QG_LIVE_PRESET_MUTATION_ALLOWED": "0",
        "QG_WRITES_MT5_ORDER_REQUEST": "0",
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
        "QG_AGENT_V25_SEND_TELEGRAM": "0",
        "QG_AGENT_V25_TELEGRAM_TIMEOUT_SECONDS": "60",
        "QG_AGENT_V25_FAST_TELEGRAM_GATEWAY": "0",
        "QG_AGENT_V25_HEAVY_TELEGRAM_GATEWAY": "0",
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
        "QG_TELEGRAM_PUSH_ALLOWED": "0",
        "QG_TELEGRAM_COMMANDS_ALLOWED": "0",
        "QG_MT5_AI_DEEPSEEK_ENABLED": "0",
        "QG_AUTOMATION_SYMBOLS": "USDJPYc",
        "QG_AUTOMATION_MAX_AGE_SECONDS": "180",
        "QG_SQLITE_BACKUP_KEEP": "3",
        "QG_RUNTIME_LOG_MAX_MB": "32",
        "QG_RUNTIME_LOG_ARCHIVE_MAX_MB": "1024",
        "QG_RUNTIME_LOG_RETENTION_DAYS": "14",
        "QG_RUNTIME_JSONL_MAX_MB": "8",
        "QG_RUNTIME_JSONL_ARCHIVE_MAX_MB": "1024",
        "QG_RUNTIME_JSONL_KEEP_LINES": "5000",
        "QG_MT5_AI_MONITOR_SYMBOLS": "USDJPYc",
        "QG_MT5_AI_MONITOR_TIMEFRAMES": "M15,H1,H4,D1",
        "QG_MT5_AI_MONITOR_MIN_INTERVAL_SECONDS": "900",
    }
    rows.update(local_user_environment())
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
QG_SERVICE_NAME="__SERVICE_NAME__"
QG_SERVICE_FINAL_STATUS=0
QG_SERVICE_LOCK=""
QG_SERVICE_CHILD_PID=""

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

export PYTHONPATH="${QG_BACKEND_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "__LOG_DIR__" "__LOCK_DIR__" "__STATUS_DIR__"

record_service_status() {
  local state="$1"
  local code="$2"
  local detail="${3:-}"
  local observed_health="${4:-}"
  local observed_readiness="${5:-}"
  local supervised_pid="${QG_SERVICE_CHILD_PID:-}"
  "$QG_PYTHON_BIN" - "__STATUS_DIR__/${QG_SERVICE_NAME}.json" "$QG_SERVICE_NAME" "$state" "$code" "$detail" "$observed_health" "$observed_readiness" "$supervised_pid" <<'PY'
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

target = Path(sys.argv[1])
task_status = sys.argv[3]
observed_health = sys.argv[6] or None
observed_readiness = sys.argv[7] or None
supervised_pid_raw = sys.argv[8]
supervised_pid = int(supervised_pid_raw) if supervised_pid_raw.isdigit() else None
effective_status = task_status
if observed_health:
    effective_status = observed_health
if observed_readiness == "NOT_READY":
    effective_status = "BLOCKED"
payload = {
    "schema": "quantgod.launchd_service_status.v2",
    "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "service": sys.argv[2],
    "status": effective_status,
    "taskStatus": task_status,
    "observedHealth": observed_health,
    "observedReadiness": observed_readiness,
    "code": sys.argv[4],
    "detail": sys.argv[5],
    "pid": os.getppid(),
    "supervisedPid": supervised_pid if task_status in {"RUNNING", "STOPPING"} else None,
}
target.parent.mkdir(parents=True, exist_ok=True)
temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, target)
PY
}

release_singleton_lock() {
  [[ -n "$QG_SERVICE_LOCK" && -d "$QG_SERVICE_LOCK" ]] || return 0
  local owner=""
  [[ -f "$QG_SERVICE_LOCK/pid" ]] && owner="$(<"$QG_SERVICE_LOCK/pid")"
  if [[ "$owner" == "$$" ]]; then
    rm -f "$QG_SERVICE_LOCK/pid" "$QG_SERVICE_LOCK/started" "$QG_SERVICE_LOCK/service"
    rmdir "$QG_SERVICE_LOCK" 2>/dev/null || true
  fi
}

service_exit() {
  local rc=$?
  if [[ "$rc" -ne 0 && "$QG_SERVICE_FINAL_STATUS" -eq 0 ]]; then
    record_service_status "FAILED" "COMMAND_EXIT_NONZERO" "exitCode=$rc"
  fi
  release_singleton_lock
}

trap service_exit EXIT

finish_service() {
  local state="$1"
  local code="$2"
  local detail="${3:-}"
  record_service_status "$state" "$code" "$detail"
  QG_SERVICE_FINAL_STATUS=1
}

finish_service_observed() {
  local state="$1"
  local code="$2"
  local detail="$3"
  local observed_health="$4"
  local observed_readiness="${5:-}"
  record_service_status "$state" "$code" "$detail" "$observed_health" "$observed_readiness"
  QG_SERVICE_FINAL_STATUS=1
}

mark_service() {
  local state="$1"
  local code="$2"
  local detail="${3:-}"
  record_service_status "$state" "$code" "$detail"
}

block_service() {
  local code="$1"
  local detail="$2"
  finish_service "BLOCKED" "$code" "$detail"
  printf 'QuantGod service %s blocked: %s (%s)\n' "$QG_SERVICE_NAME" "$detail" "$code" >&2
  exit 78
}

require_file() {
  local path="$1"
  local code="$2"
  [[ -f "$path" ]] || block_service "$code" "required file is unavailable"
}

require_directory() {
  local path="$1"
  local code="$2"
  [[ -d "$path" ]] || block_service "$code" "required directory is unavailable"
}

require_executable() {
  local path="$1"
  local code="$2"
  [[ -x "$path" ]] || block_service "$code" "required executable is unavailable"
}

require_positive_integer_env() {
  local key="$1"
  local code="$2"
  local value="${!key-}"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || \
    block_service "$code" "$key must be a positive integer"
}

canonical_directory() {
  "$QG_PYTHON_BIN" - "$1" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.is_absolute():
    raise SystemExit("path is not absolute")
resolved = path.resolve(strict=True)
if not resolved.is_dir():
    raise SystemExit("path is not a directory")
print(resolved)
PY
}

acquire_singleton_lock() {
  local lock_root="${QG_LAUNCHD_LOCK_ROOT:-__LOCK_DIR__}"
  local lock_path="$lock_root/${QG_SERVICE_NAME}.lock"
  mkdir -p "$lock_root"
  local started=""
  if mkdir "$lock_path" 2>/dev/null; then
    QG_SERVICE_LOCK="$lock_path"
    if ! started="$(/bin/ps -p "$$" -o lstart= 2>/dev/null)" || [[ -z "${started//[[:space:]]/}" ]]; then
      block_service "SINGLETON_IDENTITY_QUERY_FAILED" "could not identify the new singleton owner"
    fi
    printf '%s\n' "$$" > "$lock_path/pid"
    printf '%s\n' "$started" > "$lock_path/started"
    printf '%s\n' "$QG_SERVICE_NAME" > "$lock_path/service"
    return 0
  fi

  [[ -f "$lock_path/pid" && -f "$lock_path/started" && -f "$lock_path/service" ]] || \
    block_service "SINGLETON_IDENTITY_INCOMPLETE" "an existing singleton lock is still initializing or has incomplete identity metadata"
  local owner saved_started saved_service current_started
  owner="$(<"$lock_path/pid")"
  saved_started="$(<"$lock_path/started")"
  saved_service="$(<"$lock_path/service")"
  [[ "$owner" =~ ^[0-9]+$ ]] || block_service "SINGLETON_IDENTITY_INVALID" "singleton lock owner pid is invalid"
  [[ -n "${saved_started//[[:space:]]/}" ]] || block_service "SINGLETON_IDENTITY_INVALID" "singleton lock start identity is empty"
  [[ "$saved_service" == "$QG_SERVICE_NAME" ]] || block_service "SINGLETON_IDENTITY_MISMATCH" "lock belongs to an unexpected service identity"
  if kill -0 "$owner" 2>/dev/null; then
    if ! current_started="$(/bin/ps -p "$owner" -o lstart= 2>/dev/null)" || [[ -z "${current_started//[[:space:]]/}" ]]; then
      block_service "SINGLETON_IDENTITY_QUERY_FAILED" "could not verify the live lock owner"
    fi
    if [[ "$current_started" == "$saved_started" ]]; then
      block_service "SINGLETON_ALREADY_RUNNING" "another supervised instance is already running"
    fi
  fi
  rm -f "$lock_path/pid" "$lock_path/started" "$lock_path/service"
  rmdir "$lock_path" 2>/dev/null || block_service "SINGLETON_LOCK_INVALID" "stale lock could not be reclaimed"
  mkdir "$lock_path" || block_service "SINGLETON_LOCK_RACE" "singleton lock acquisition raced"
  QG_SERVICE_LOCK="$lock_path"
  if ! started="$(/bin/ps -p "$$" -o lstart= 2>/dev/null)" || [[ -z "${started//[[:space:]]/}" ]]; then
    block_service "SINGLETON_IDENTITY_QUERY_FAILED" "could not identify the reclaimed singleton owner"
  fi
  printf '%s\n' "$$" > "$lock_path/pid"
  printf '%s\n' "$started" > "$lock_path/started"
  printf '%s\n' "$QG_SERVICE_NAME" > "$lock_path/service"
}

assert_shadow_env_value() {
  local key="$1"
  local expected="$2"
  local actual="${!key-}"
  [[ "$actual" == "$expected" ]] || block_service "SHADOW_ENV_GUARD_FAILED" "$key must equal $expected"
}

enforce_shadow_contract() {
  assert_shadow_env_value "QG_EXECUTION_MODE" "SHADOW"
  assert_shadow_env_value "QG_MT5_START_MODE" "shadow"
  assert_shadow_env_value "QG_MT5_LIVE_LAUNCH_ALLOWED" "0"
  assert_shadow_env_value "QG_MT5_SECONDARY_ENABLED" "0"
  [[ "${QG_MT5_SECONDARY_SHADOW_ENABLED-}" =~ ^[01]$ ]] || \
    block_service "SHADOW_ENV_GUARD_FAILED" "QG_MT5_SECONDARY_SHADOW_ENABLED must equal 0 or 1"
  assert_shadow_env_value "QG_MT5_SECONDARY_ALLOW_LIVE_TRADING" "0"
  assert_shadow_env_value "QG_ORDER_SEND_ALLOWED" "0"
  assert_shadow_env_value "QG_BROKER_EXECUTION_ALLOWED" "0"
  assert_shadow_env_value "QG_LIVE_PRESET_MUTATION_ALLOWED" "0"
  assert_shadow_env_value "QG_WRITES_MT5_ORDER_REQUEST" "0"
  assert_shadow_env_value "QG_TELEGRAM_COMMANDS_ALLOWED" "0"
  assert_shadow_env_value "QG_TELEGRAM_PUSH_ALLOWED" "0"
  assert_shadow_env_value "QG_AGENT_V25_SEND_TELEGRAM" "0"
  assert_shadow_env_value "QG_MT5_AI_DEEPSEEK_ENABLED" "0"
  assert_shadow_env_value "QG_DASHBOARD_HOST" "127.0.0.1"
  assert_shadow_env_value "QG_PRIVATE_ROOT" "__PRIVATE_ROOT__"
  assert_shadow_env_value "QG_LAUNCHD_LOG_ROOT" "__LOG_DIR__"
  assert_shadow_env_value "QG_LAUNCHD_LOCK_ROOT" "__LOCK_DIR__"
  assert_shadow_env_value "QG_LAUNCHD_STATUS_ROOT" "__STATUS_DIR__"
  assert_shadow_env_value "QG_LOCAL_SQLITE_BACKUP_ROOT" "__BACKUP_DIR__"
}

assert_file_value() {
  local path="$1"
  local key="$2"
  local expected="$3"
  if ! "$QG_PYTHON_BIN" - "$path" "$key" "$expected" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
expected_key = sys.argv[2]
expected_value = sys.argv[3].lower()
values = {}
for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
    line = raw_line.strip()
    if not line or line.startswith(("#", ";", "[")) or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values.setdefault(key.strip().lower(), []).append(value.strip())
candidates = values.get(expected_key.lower(), [])
raise SystemExit(0 if len(candidates) == 1 and candidates[0].lower() == expected_value else 1)
PY
  then
    block_service "MT5_SHADOW_FILE_GUARD_FAILED" "$key does not match the reviewed Shadow contract"
  fi
}

assert_file_value_exact() {
  local path="$1"
  local key="$2"
  local expected="$3"
  if ! "$QG_PYTHON_BIN" - "$path" "$key" "$expected" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
expected_key = sys.argv[2]
expected_value = sys.argv[3]
values = {}
for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
    line = raw_line.strip()
    if not line or line.startswith(("#", ";", "[")) or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values.setdefault(key.strip().lower(), []).append(value.strip())
candidates = values.get(expected_key.lower(), [])
raise SystemExit(0 if candidates == [expected_value] else 1)
PY
  then
    block_service "MT5_SHADOW_SERVER_GUARD_FAILED" "$key must exactly match the reviewed HFM broker server"
  fi
}

assert_private_login_match() {
  local shadow_config="$1"
  local login_reference="$2"
  if ! "$QG_PYTHON_BIN" - "$shadow_config" "$login_reference" <<'PY'
import hmac
from pathlib import Path
import re
import sys

def private_login(path_raw):
    path = Path(path_raw)
    values = {}
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key.strip().lower(), []).append(value.strip())
    candidates = values.get("login", [])
    if len(candidates) != 1 or not re.fullmatch(r"[0-9]+", candidates[0]):
        raise SystemExit(1)
    return candidates[0]

shadow_login = private_login(sys.argv[1])
reference_login = private_login(sys.argv[2])
raise SystemExit(0 if hmac.compare_digest(shadow_login, reference_login) else 1)
PY
  then
    block_service "MT5_SHADOW_LOGIN_GUARD_FAILED" "Shadow Login identity does not match the private reviewed login reference"
  fi
}

assert_file_value_not_true() {
  local path="$1"
  local key="$2"
  if ! "$QG_PYTHON_BIN" - "$path" "$key" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
expected_key = sys.argv[2].lower()
values = {}
for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
    line = raw_line.strip()
    if not line or line.startswith(("#", ";", "[")) or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values.setdefault(key.strip().lower(), []).append(value.strip().lower())
candidates = values.get(expected_key, [])
raise SystemExit(1 if len(candidates) > 1 or any(value in {"1", "true", "yes", "on"} for value in candidates) else 0)
PY
  then
    block_service "MT5_SHADOW_FILE_GUARD_FAILED" "$key must never be enabled in the Shadow preset"
  fi
}

assert_path_has_no_symlink_components() {
  local path="$1"
  if ! "$QG_PYTHON_BIN" - "$path" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1]).expanduser().absolute()
for candidate in (path, *path.parents):
    if candidate.is_symlink():
        raise SystemExit(1)
PY
  then
    block_service "PRIVATE_PATH_SYMLINK_FORBIDDEN" "managed private path contains a symlink component"
  fi
}

assert_private_identity_file() {
  local path="$1"
  if ! "$QG_PYTHON_BIN" - "$path" <<'PY'
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
metadata = path.lstat()
if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
    raise SystemExit(1)
if metadata.st_uid != os.getuid():
    raise SystemExit(1)
if stat.S_IMODE(metadata.st_mode) & 0o077:
    raise SystemExit(1)
PY
  then
    block_service "MT5_PRIVATE_IDENTITY_FILE_INVALID" "private MT5 identity/config file ownership or permissions are unsafe"
  fi
}

prepare_wine_user_environment() {
  if ! "$QG_PYTHON_BIN" - \
    "${QG_USER_HOME-}" \
    "${QG_LOCAL_USER-}" \
    "${QG_USER_TMPDIR-}" \
    "${QG_USER_LANG-}" <<'PY'
import os
from pathlib import Path
import pwd
import re
import sys

home_raw, local_user, tmp_raw, lang = sys.argv[1:5]
account = pwd.getpwuid(os.getuid())
if local_user != account.pw_name:
    raise SystemExit("local user does not match the launchd uid")

def validate_directory(raw, label, expected=None):
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise SystemExit(f"{label} is not absolute")
    try:
        resolved = path.resolve()
        metadata = path.stat()
    except OSError as exc:
        raise SystemExit(f"{label} is unavailable: {exc}") from exc
    if path.is_symlink() or resolved != path:
        raise SystemExit(f"{label} contains a symlink or unresolved component")
    if not path.is_dir():
        raise SystemExit(f"{label} is not a directory")
    if metadata.st_uid != os.getuid():
        raise SystemExit(f"{label} is not owned by the launchd uid")
    if expected is not None and resolved != expected:
        raise SystemExit(f"{label} does not match the local account")

validate_directory(home_raw, "QG_USER_HOME", Path(account.pw_dir).expanduser().resolve())
validate_directory(tmp_raw, "QG_USER_TMPDIR")
if not re.fullmatch(r"[A-Za-z0-9_.@-]+", lang):
    raise SystemExit("QG_USER_LANG is invalid")
PY
  then
    block_service "WINE_USER_ENV_GUARD_FAILED" "HOME/USER/TMPDIR/LANG did not match the local launchd account"
  fi
  export HOME="$QG_USER_HOME"
  export USER="$QG_LOCAL_USER"
  export LOGNAME="$QG_LOCAL_USER"
  export TMPDIR="$QG_USER_TMPDIR"
  export LANG="$QG_USER_LANG"
}
'''


AUTOMATION_CHAIN_VALIDATOR = r'''import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("schema") != "quantgod.automation_chain.v1":
    raise SystemExit("automation chain returned an unexpected schema")
steps = payload.get("steps")
if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
    raise SystemExit("automation chain did not return a non-empty step list")
required_steps = [step for step in steps if step.get("required") is True]
if not required_steps:
    raise SystemExit("automation chain did not return any required steps")

expected_required_ids = {
    "adaptive_policy",
    "dynamic_sltp",
    "entry_trigger",
    "usdjpy_strategy_policy",
    "usdjpy_ea_dry_run",
    "usdjpy_live_loop",
}

def required_step_identity(step):
    name = step.get("name")
    legacy_id = step.get("id")
    if name is not None and (not isinstance(name, str) or not name):
        raise SystemExit("automation chain required step has an invalid name identity")
    if legacy_id is not None and (not isinstance(legacy_id, str) or not legacy_id):
        raise SystemExit("automation chain required step has an invalid id identity")
    if name is None and legacy_id is None:
        raise SystemExit("automation chain required step is missing a name/id identity")
    if name is not None and legacy_id is not None and name != legacy_id:
        raise SystemExit("automation chain required step has conflicting name/id identities")
    return name if name is not None else legacy_id

required_ids = [required_step_identity(step) for step in required_steps]
if len(required_ids) != len(set(required_ids)):
    raise SystemExit("automation chain returned duplicate required step identities")
unknown_ids = set(required_ids) - expected_required_ids
if unknown_ids:
    raise SystemExit("automation chain returned unknown required step identities")
missing_ids = expected_required_ids - set(required_ids)
if missing_ids:
    raise SystemExit("automation chain required step identity set is incomplete")
failed = [
    identity
    for identity, step in zip(required_ids, required_steps)
    if step.get("ok") is not True
]
if failed:
    raise SystemExit("required automation steps failed: " + ", ".join(failed))
if payload.get("runStatus") != "COMPLETED":
    raise SystemExit("automation chain did not confirm runStatus=COMPLETED")
if payload.get("requiredStepCount") != len(required_steps) or payload.get("requiredFailedCount") != 0:
    raise SystemExit("automation chain required-step counters are inconsistent")
safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
for key in ("orderSendAllowed", "brokerExecutionAllowed", "livePresetMutationAllowed"):
    if safety.get(key) is not False:
        raise SystemExit(f"automation safety contract missing {key}=false")
'''


def wrapper_header(service_name: str, *service_env_files: str) -> str:
    header = (
        COMMON_WRAPPER.replace("__ENV_PATH__", str(ENV_PATH))
        .replace("__LOG_DIR__", str(LOG_DIR))
        .replace("__LOCK_DIR__", str(LOCK_DIR))
        .replace("__STATUS_DIR__", str(STATUS_DIR))
        .replace("__BACKUP_DIR__", str(BACKUP_DIR))
        .replace("__PRIVATE_ROOT__", str(PRIVATE_ROOT))
        .replace("__SERVICE_NAME__", service_name)
    )
    for filename in service_env_files:
        header += f'load_env_file "${{QG_BACKEND_ROOT}}/{filename}"\n'
    header += "enforce_shadow_contract\nacquire_singleton_lock\n"
    return header


def render_wrappers() -> dict[str, str]:
    wrappers = {
        "quantgod-backend-api.sh": wrapper_header("backend-api", ".env.local")
        + r'''
require_file "$QG_BACKEND_ROOT/Dashboard/dashboard_server.js" "BACKEND_SERVER_MISSING"
require_file "$QG_BACKEND_ROOT/Dashboard/vue-dist/index.html" "COMPILED_FRONTEND_MISSING"
require_executable "$QG_NODE_BIN" "NODE_MISSING"
mark_service "RUNNING" "BACKEND_START" "serving loopback API and compiled /vue"
cd "$QG_BACKEND_ROOT"
exec "$QG_NODE_BIN" Dashboard/dashboard_server.js
''',
        "quantgod-frontend-dev.sh": wrapper_header("frontend-dev")
        + r'''
require_file "$QG_FRONTEND_ROOT/package.json" "FRONTEND_PACKAGE_MISSING"
require_executable "$QG_FRONTEND_ROOT/node_modules/.bin/vite" "FRONTEND_VITE_MISSING"
mark_service "RUNNING" "FRONTEND_DEV_START" "development-only Vite server"
cd "$QG_FRONTEND_ROOT"
exec "$QG_NPM_BIN" run dev -- --host 127.0.0.1 --port 5173
''',
        "quantgod-frontend-dist-build.sh": wrapper_header("frontend-dist-build")
        + r'''
require_file "$QG_FRONTEND_ROOT/package.json" "FRONTEND_PACKAGE_MISSING"
require_executable "$QG_FRONTEND_ROOT/node_modules/.bin/vite" "FRONTEND_VITE_MISSING"
require_file "$QG_INFRA_ROOT/scripts/qg-workspace.py" "WORKSPACE_HELPER_MISSING"
require_file "$QG_WORKSPACE_FILE" "WORKSPACE_CONFIG_MISSING"
mark_service "RUNNING" "FRONTEND_BUILD_START" "building compiled Frontend"
cd "$QG_FRONTEND_ROOT"
"$QG_NPM_BIN" run build
cd "$QG_INFRA_ROOT"
"$QG_PYTHON_BIN" scripts/qg-workspace.py --workspace "$QG_WORKSPACE_FILE" sync-frontend-dist
finish_service "PASS" "FRONTEND_DIST_PUBLISHED" "compiled Frontend published atomically"
''',
        "quantgod-mt5-shadow-supervisor.sh": wrapper_header("mt5-shadow-supervisor")
        + r'''
require_executable "$QG_MT5_WINE_BIN" "MT5_WINE_MISSING"
require_file "$QG_MT5_TERMINAL_PATH" "MT5_TERMINAL_MISSING"
require_file "$QG_MT5_SHADOW_CONFIG" "MT5_SHADOW_CONFIG_MISSING"
require_file "$QG_MT5_LOGIN_REFERENCE_CONFIG" "MT5_LOGIN_REFERENCE_MISSING"
require_file "$QG_MT5_SHADOW_PRESET" "MT5_SHADOW_PRESET_MISSING"
if [[ "$QG_SERVICE_NAME" == "mt5-secondary-shadow-supervisor" ]]; then
  require_file "$QG_MT5_SHADOW_PRESET_SOURCE" "MT5_SECONDARY_PRESET_SOURCE_MISSING"
fi
require_file "$QG_MT5_ROOT/MQL5/Experts/QuantGod_MultiStrategy.mq5" "MT5_SHADOW_EA_SOURCE_MISSING"
require_file "$QG_MT5_ROOT/MQL5/Experts/QuantGod_MultiStrategy.ex5" "MT5_SHADOW_EA_BINARY_MISSING"
require_file "$QG_MT5_VERIFIED_EA_SOURCE" "MT5_VERIFIED_EA_SOURCE_MISSING"
require_file "$QG_MT5_VERIFIED_EA_BINARY" "MT5_VERIFIED_EA_BINARY_MISSING"
require_file "$QG_MT5_VERIFIED_EA_COMPILE_LOG" "MT5_VERIFIED_EA_COMPILE_LOG_MISSING"
for reviewed_path in \
  "$QG_MT5_WINE_PREFIX" \
  "$QG_MT5_ROOT" \
  "$QG_MT5_TERMINAL_PATH" \
  "$QG_MT5_SHADOW_CONFIG" \
  "$QG_MT5_LOGIN_REFERENCE_CONFIG" \
  "$QG_MT5_SHADOW_PRESET" \
  "$QG_MT5_ROOT/MQL5/Experts/QuantGod_MultiStrategy.mq5" \
  "$QG_MT5_ROOT/MQL5/Experts/QuantGod_MultiStrategy.ex5" \
  "$QG_MT5_VERIFIED_EA_SOURCE" \
  "$QG_MT5_VERIFIED_EA_BINARY" \
  "$QG_MT5_VERIFIED_EA_COMPILE_LOG"; do
  assert_path_has_no_symlink_components "$reviewed_path"
done
if [[ "$QG_SERVICE_NAME" == "mt5-secondary-shadow-supervisor" ]]; then
  assert_path_has_no_symlink_components "$QG_MT5_SHADOW_PRESET_SOURCE"
fi
if [[ "$QG_MT5_SECONDARY_SHADOW_ENABLED" == "1" ]]; then
  require_file "$QG_MT5_PEER_TERMINAL_PATH" "MT5_PEER_TERMINAL_MISSING"
  assert_path_has_no_symlink_components "$QG_MT5_PEER_ROOT"
  assert_path_has_no_symlink_components "$QG_MT5_PEER_TERMINAL_PATH"
fi
assert_private_identity_file "$QG_MT5_SHADOW_CONFIG"
assert_private_identity_file "$QG_MT5_LOGIN_REFERENCE_CONFIG"
expected_shadow_config="$QG_MT5_WINE_PREFIX/drive_c/qg/QuantGod_MT5_HFM_Shadow_mac.ini"
expected_login_reference="$QG_MT5_WINE_PREFIX/drive_c/qg/QuantGod_MT5_LoginOnly_mac.ini"
expected_shadow_preset="$QG_MT5_ROOT/MQL5/Presets/QuantGod_MT5_HFM_Shadow.set"
[[ "$QG_MT5_SHADOW_CONFIG" == "$expected_shadow_config" ]] || block_service "MT5_SHADOW_CONFIG_PATH_INVALID" "only the exact reviewed Shadow config path is allowed"
[[ "$QG_MT5_LOGIN_REFERENCE_CONFIG" == "$expected_login_reference" ]] || block_service "MT5_LOGIN_REFERENCE_PATH_INVALID" "only the exact private LoginOnly reference path is allowed"
[[ "$QG_MT5_SHADOW_PRESET" == "$expected_shadow_preset" ]] || block_service "MT5_SHADOW_PRESET_PATH_INVALID" "only the exact reviewed Shadow preset path is allowed"
[[ "$QG_MT5_SHADOW_CONFIG_WINDOWS" == 'C:\qg\QuantGod_MT5_HFM_Shadow_mac.ini' ]] || block_service "MT5_SHADOW_WINDOWS_CONFIG_INVALID" "the terminal config argument is not the exact reviewed Shadow config"
[[ "$QG_MT5_EXPECTED_SERVER" == 'HFMarketsGlobal-Live12' ]] || block_service "MT5_EXPECTED_SERVER_INVALID" "the generated broker-server contract was overridden"
assert_file_value_exact "$QG_MT5_SHADOW_CONFIG" "Server" "$QG_MT5_EXPECTED_SERVER"
assert_file_value_exact "$QG_MT5_LOGIN_REFERENCE_CONFIG" "Server" "$QG_MT5_EXPECTED_SERVER"
assert_private_login_match "$QG_MT5_SHADOW_CONFIG" "$QG_MT5_LOGIN_REFERENCE_CONFIG"
assert_file_value "$QG_MT5_SHADOW_CONFIG" "AllowLiveTrading" "0"
assert_file_value "$QG_MT5_SHADOW_CONFIG" "AllowDllImport" "0"
assert_file_value "$QG_MT5_SHADOW_CONFIG" "ExpertParameters" "QuantGod_MT5_HFM_Shadow.set"
assert_file_value_not_true "$QG_MT5_SHADOW_CONFIG" "OrderSendAllowed"
assert_file_value_not_true "$QG_MT5_SHADOW_CONFIG" "BrokerExecutionAllowed"
assert_file_value_not_true "$QG_MT5_SHADOW_CONFIG" "LivePresetMutationAllowed"
assert_file_value_not_true "$QG_MT5_SHADOW_CONFIG" "EnableLiveTrading"
assert_file_value "$QG_MT5_SHADOW_PRESET" "ShadowMode" "true"
assert_file_value "$QG_MT5_SHADOW_PRESET" "ReadOnlyMode" "true"
assert_file_value "$QG_MT5_SHADOW_PRESET" "EnablePilotAutoTrading" "false"
if [[ "$QG_SERVICE_NAME" == "mt5-secondary-shadow-supervisor" ]]; then
  expected_secondary_source="$QG_BACKEND_ROOT/MQL5/Presets/QuantGod_MT5_HFM_LiveSecondary.set"
  [[ "$QG_MT5_SHADOW_PRESET_SOURCE" == "$expected_secondary_source" ]] || block_service "MT5_SECONDARY_PRESET_SOURCE_PATH_INVALID" "the secondary observer source is outside the reviewed Backend preset path"
  /usr/bin/cmp -s "$QG_MT5_SHADOW_PRESET_SOURCE" "$QG_MT5_SHADOW_PRESET" || block_service "MT5_SECONDARY_SHADOW_PRESET_SOURCE_MISMATCH" "the deployed secondary observer preset differs from its reviewed source"
  assert_file_value "$QG_MT5_SHADOW_PRESET" "Watchlist" "USDJPY"
  assert_file_value "$QG_MT5_SHADOW_PRESET" "PreferredSymbolSuffix" "AUTO"
  assert_file_value "$QG_MT5_SHADOW_PRESET" "EnablePilotRsiH1Live" "false"
  assert_file_value "$QG_MT5_SHADOW_PRESET" "EnablePilotBBH1Live" "false"
  assert_file_value "$QG_MT5_SHADOW_PRESET" "EnablePilotMacdH1Live" "false"
  assert_file_value "$QG_MT5_SHADOW_PRESET" "EnablePilotSRM15Live" "false"
  assert_file_value "$QG_MT5_SHADOW_PRESET" "EnableNonRsiLegacyLiveAuthorization" "false"
fi
assert_file_value_not_true "$QG_MT5_SHADOW_PRESET" "OrderSendAllowed"
assert_file_value_not_true "$QG_MT5_SHADOW_PRESET" "BrokerExecutionAllowed"
assert_file_value_not_true "$QG_MT5_SHADOW_PRESET" "LivePresetMutationAllowed"
assert_file_value_not_true "$QG_MT5_SHADOW_PRESET" "EnableLiveTrading"
if ! "$QG_PYTHON_BIN" - \
  "$QG_MT5_ROOT/MQL5/Experts/QuantGod_MultiStrategy.mq5" \
  "$QG_MT5_ROOT/MQL5/Experts/QuantGod_MultiStrategy.ex5" \
  "$QG_MT5_VERIFIED_EA_SOURCE" \
  "$QG_MT5_VERIFIED_EA_BINARY" \
  "$QG_MT5_VERIFIED_EA_COMPILE_LOG" <<'PY'
from hashlib import sha256
from pathlib import Path
import hmac
import re
import sys

lane_source, lane_binary, verified_source, verified_binary, compile_log = map(Path, sys.argv[1:])

def digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

source_text = lane_source.read_text(encoding="utf-8-sig", errors="strict")
for pattern in (
    r"#include\s*<Trade/Trade\.mqh>",
    r"\bCTrade\b|\bg_trade\b",
    r"\bOrderSend(?:Async)?\s*\(",
    r"\.(?:Buy|Sell|PositionClose|PositionModify|OrderDelete|OrderModify)\s*\(",
    r"TRADE_ACTION_(?:DEAL|PENDING|SLTP|MODIFY|REMOVE)",
):
    if re.search(pattern, source_text):
        raise SystemExit("the installed EA source exposes broker mutation")

for installed, verified in ((lane_source, verified_source), (lane_binary, verified_binary)):
    if not hmac.compare_digest(digest(installed), digest(verified)):
        raise SystemExit("the installed EA does not match the verified build staging artifact")

raw_log = compile_log.read_bytes()
encoding = "utf-16" if raw_log.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
log_text = raw_log.decode(encoding, errors="strict")
if re.search(r"^\s*Result:\s*0 errors,\s*0 warnings(?:,|$)", log_text, flags=re.MULTILINE) is None:
    raise SystemExit("the verified EA compile log is not clean")
normalized_log = log_text.replace("/", "\\")
if re.search(
    r"^C:\\qg\\QuantGod_MultiStrategy\.mq5\s+:\s+information:\s+compiling\s+"
    r"C:\\qg\\QuantGod_MultiStrategy\.mq5\s*$",
    normalized_log,
    flags=re.MULTILINE,
) is None:
    raise SystemExit("the verified EA compile log does not identify the reviewed staging source")
for pattern in (
    r"Trade[/\\]Trade\.mqh",
    r"\bCTrade\b|\bg_trade\b",
    r"\bOrderSend(?:Async)?\s*\(",
    r"TRADE_ACTION_(?:DEAL|PENDING|SLTP|MODIFY|REMOVE)",
):
    if re.search(pattern, log_text):
        raise SystemExit("the verified EA compile log references a broker mutation surface")

source_mtime = verified_source.stat().st_mtime_ns
binary_mtime = verified_binary.stat().st_mtime_ns
log_mtime = compile_log.stat().st_mtime_ns
if binary_mtime < source_mtime or log_mtime < source_mtime:
    raise SystemExit("the verified EA binary or compile log predates the reviewed source")
if abs(binary_mtime - log_mtime) > 120 * 1_000_000_000:
    raise SystemExit("the verified EA binary and compile log timestamps do not form one build event")
PY
then
  block_service "MT5_SHADOW_EA_PROVENANCE_INVALID" "the installed Shadow EA failed source/build provenance validation"
fi
if [[ "$QG_MT5_SECONDARY_SHADOW_ENABLED" == "1" ]] && \
   ! "$QG_PYTHON_BIN" - "$QG_MT5_TERMINAL_PATH" "$QG_MT5_PEER_TERMINAL_PATH" <<'PY'
from hashlib import sha256
from pathlib import Path
import hmac
import sys

def digest(path: str) -> str:
    target = Path(path)
    with target.open("rb") as stream:
        value = sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

if not hmac.compare_digest(digest(sys.argv[1]), digest(sys.argv[2])):
    raise SystemExit("the reviewed MT5 terminal binaries differ")
PY
then
  block_service "MT5_TERMINAL_BINARY_MISMATCH" "the two reviewed Wine prefixes do not use the same terminal binary"
fi
find_mt5_terminal_pids_for_root() {
  # Two reviewed Wine prefixes may run side by side.  Classify the terminal by
  # its current working directory so a process in the peer prefix is allowed,
  # while a duplicate in this prefix or an unknown third MT5 is rejected.
  local process_list="" process_rc=0 pid="" cwd_record="" cwd="" command_line="" found_same=0
  if process_list="$(pgrep -f '__MT5_TERMINAL_PROCESS_PATTERN__')"; then
    :
  else
    process_rc=$?
    [[ "$process_rc" -eq 1 ]] && return 1
    return 2
  fi
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || return 3
    if cwd_record="$(/usr/sbin/lsof -a -p "$pid" -d cwd -Fn 2>/dev/null)"; then
      cwd="$(printf '%s\n' "$cwd_record" | awk '/^n/ {print substr($0, 2); exit}')"
    else
      kill -0 "$pid" 2>/dev/null || continue
      return 3
    fi
    [[ -n "$cwd" ]] || return 3
    if ! command_line="$(/bin/ps -p "$pid" -o command= 2>/dev/null)"; then
      kill -0 "$pid" 2>/dev/null || continue
      return 3
    fi
    if [[ "$cwd" == "$QG_MT5_ROOT" ]]; then
      [[ "$command_line" == *"/config:${QG_MT5_SHADOW_CONFIG_WINDOWS}"* ]] || return 4
      printf '%s\n' "$pid"
      found_same=1
    elif [[ "$cwd" == "$QG_MT5_PEER_ROOT" ]]; then
      [[ "$QG_MT5_SECONDARY_SHADOW_ENABLED" == "1" ]] || return 4
      [[ "$command_line" == *"/config:${QG_MT5_PEER_SHADOW_CONFIG_WINDOWS}"* ]] || return 4
    else
      return 4
    fi
  done <<< "$process_list"
  [[ "$found_same" -eq 1 ]]
}
existing_mt5=""
if existing_mt5="$(find_mt5_terminal_pids_for_root)"; then
  block_service "EXISTING_MT5_PROCESS" "an MT5 terminal already exists in this Wine prefix"
else
  pgrep_rc=$?
  case "$pgrep_rc" in
    1) ;;
    2|3) block_service "MT5_PROCESS_QUERY_FAILED" "could not safely identify an existing MT5 terminal" ;;
    4) block_service "UNREVIEWED_MT5_PROCESS" "an MT5 terminal outside the two reviewed Wine prefixes is running" ;;
    *) block_service "MT5_PROCESS_QUERY_FAILED" "unexpected MT5 process query result" ;;
  esac
fi
require_directory "$QG_MT5_ROOT" "MT5_ROOT_MISSING"
current_root_real="$(canonical_directory "$QG_MT5_ROOT")" || block_service "MT5_ROOT_INVALID" "the MT5 root could not be resolved"
if [[ "$QG_MT5_SECONDARY_SHADOW_ENABLED" == "1" ]]; then
  require_directory "$QG_MT5_PEER_ROOT" "MT5_PEER_ROOT_MISSING"
  peer_root_real="$(canonical_directory "$QG_MT5_PEER_ROOT")" || block_service "MT5_PEER_ROOT_INVALID" "the peer MT5 root could not be resolved"
  [[ "$current_root_real" != "$peer_root_real" ]] || block_service "MT5_PREFIX_COLLISION" "primary and secondary MT5 roots must be distinct"
fi
prepare_wine_user_environment
cd "$QG_MT5_ROOT"
export WINEPREFIX="$QG_MT5_WINE_PREFIX"
QG_MT5_STOP_SIGNAL=""
QG_MT5_SIGNAL_FORWARD_FAILED=0
QG_MT5_STOP_RECORDED=0

forward_mt5_signal() {
  local signal_name="$1"
  local child_pid="${QG_SERVICE_CHILD_PID:-}"
  QG_MT5_STOP_SIGNAL="$signal_name"
  if [[ "$child_pid" =~ ^[0-9]+$ ]] && kill -0 "$child_pid" 2>/dev/null; then
    if ! kill -s "$signal_name" "$child_pid" 2>/dev/null && kill -0 "$child_pid" 2>/dev/null; then
      QG_MT5_SIGNAL_FORWARD_FAILED=1
    fi
  fi
}

trap 'forward_mt5_signal TERM' TERM
trap 'forward_mt5_signal INT' INT

"$QG_MT5_WINE_BIN" terminal64.exe /portable "/config:${QG_MT5_SHADOW_CONFIG_WINDOWS}" &
QG_SERVICE_CHILD_PID="$!"
if [[ -n "$QG_MT5_STOP_SIGNAL" ]]; then
  forward_mt5_signal "$QG_MT5_STOP_SIGNAL"
fi
if [[ -n "$QG_MT5_STOP_SIGNAL" ]]; then
  mark_service "STOPPING" "MT5_SHADOW_STOPPING" "supervisor is waiting for the signalled terminal child to exit"
  QG_MT5_STOP_RECORDED=1
elif kill -0 "$QG_SERVICE_CHILD_PID" 2>/dev/null; then
  mark_service "RUNNING" "MT5_SHADOW_START" "strict Shadow/ReadOnly config verified; supervised terminal child started"
fi

set +e
wait "$QG_SERVICE_CHILD_PID"
mt5_child_rc=$?
set -e
if [[ -n "$QG_MT5_STOP_SIGNAL" && "$QG_MT5_STOP_RECORDED" -eq 0 ]]; then
  mark_service "STOPPING" "MT5_SHADOW_STOPPING" "supervisor is waiting for the signalled terminal child to exit"
  QG_MT5_STOP_RECORDED=1
fi
while kill -0 "$QG_SERVICE_CHILD_PID" 2>/dev/null && [[ "$QG_MT5_SIGNAL_FORWARD_FAILED" -eq 0 ]]; do
  set +e
  wait "$QG_SERVICE_CHILD_PID"
  mt5_child_rc=$?
  set -e
done

if [[ "$QG_MT5_SIGNAL_FORWARD_FAILED" -ne 0 ]]; then
  finish_service "FAILED" "MT5_SIGNAL_FORWARD_FAILED" "could not forward the supervisor signal to the exact terminal child"
  exit 70
fi

detached_mt5=""
if detached_mt5="$(find_mt5_terminal_pids_for_root)"; then
  finish_service "FAILED" "MT5_CHILD_DETACHED" "the supervised Wine child exited while an MT5 terminal remained"
  exit 70
else
  detached_query_rc=$?
  case "$detached_query_rc" in
    1) ;;
    2|3) finish_service "FAILED" "MT5_PROCESS_QUERY_FAILED" "could not verify terminal shutdown after the supervised child exited"; exit 70 ;;
    4) finish_service "FAILED" "UNREVIEWED_MT5_PROCESS" "an MT5 terminal outside the two reviewed Wine prefixes is running"; exit 70 ;;
    *) finish_service "FAILED" "MT5_PROCESS_QUERY_FAILED" "unexpected MT5 process query result"; exit 70 ;;
  esac
fi

if [[ -n "$QG_MT5_STOP_SIGNAL" ]]; then
  finish_service "STOPPED" "MT5_SHADOW_SIGNALLED" "supervisor forwarded $QG_MT5_STOP_SIGNAL to the terminal child; childExitCode=$mt5_child_rc"
  case "$QG_MT5_STOP_SIGNAL" in
    INT) exit 130 ;;
    TERM) exit 143 ;;
    *) exit 1 ;;
  esac
fi
if [[ "$mt5_child_rc" -eq 0 ]]; then
  finish_service "STOPPED" "MT5_SHADOW_EXITED" "terminal child exited normally"
  exit 0
fi
finish_service "FAILED" "MT5_SHADOW_EXIT_NONZERO" "terminal child exited with code $mt5_child_rc"
exit "$mt5_child_rc"
''',
        "quantgod-daily-autopilot.sh": wrapper_header("daily-autopilot")
        + r'''
block_service \
  "LEGACY_AGENT_FAILURE_CONTRACT_UNSAFE" \
  "legacy Agent loop can report completion after required-stage failures; use automation-chain"
''',
        "quantgod-usdjpy-history-sync.sh": wrapper_header("usdjpy-history-sync")
        + r'''
require_file "$QG_BACKEND_ROOT/tools/run_usdjpy_strategy_backtest.py" "HISTORY_SYNC_RUNNER_MISSING"
require_file "$QG_MT5_TERMINAL_PATH" "MT5_TERMINAL_MISSING"
mark_service "RUNNING" "HISTORY_SYNC_START" "running direct fail-closed sync and quality stages"
sync_tmp="$QG_LAUNCHD_STATUS_ROOT/.history-sync.tmp-$$"
quality_tmp="$QG_LAUNCHD_STATUS_ROOT/.history-quality.tmp-$$"
cd "$QG_BACKEND_ROOT"
"$QG_MT5_PYTHON_BIN" tools/run_usdjpy_strategy_backtest.py \
  --runtime-dir "$QG_RUNTIME_DIR" \
  sync-klines \
  --months "$QG_USDJPY_HISTORY_MONTHS" \
  --timeframes "$QG_USDJPY_HISTORY_TIMEFRAMES" \
  --symbol "$QG_USDJPY_MT5_SYMBOL" \
  --terminal-path "$QG_MT5_TERMINAL_PATH" \
  --max-bars-per-timeframe "$QG_USDJPY_HISTORY_MAX_BARS" \
  --max-latest-lag-hours "$QG_USDJPY_HISTORY_MAX_LAG_HOURS" > "$sync_tmp"
"$QG_PYTHON_BIN" - "$sync_tmp" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("schema") != "quantgod.usdjpy_historical_kline_sync_report.v1":
    raise SystemExit("history sync returned an unexpected schema")
if payload.get("ok") is not True:
    raise SystemExit("history sync did not confirm ok=true")
PY
"$QG_MT5_PYTHON_BIN" tools/run_usdjpy_strategy_backtest.py --runtime-dir "$QG_RUNTIME_DIR" quality > "$quality_tmp"
quality_status="$("$QG_PYTHON_BIN" - "$quality_tmp" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("schema") != "quantgod.strategy_backtest_quality.v1":
    raise SystemExit("history quality returned an unexpected schema")
status = str(payload.get("status") or "UNKNOWN").upper()
if status not in {"PASS", "BLOCKED", "WARN", "STALE"}:
    raise SystemExit("history quality returned an invalid status")
print(status)
PY
)"
mv "$sync_tmp" "$QG_LAUNCHD_STATUS_ROOT/history-sync-report.json"
mv "$quality_tmp" "$QG_LAUNCHD_STATUS_ROOT/history-quality-report.json"
finish_service_observed "PASS" "HISTORY_SYNC_COMPLETE" "sync completed; qualityStatus=$quality_status" "$quality_status"
''',
        "quantgod-automation-chain.sh": wrapper_header("automation-chain")
        + r'''
require_file "$QG_BACKEND_ROOT/tools/run_automation_chain.py" "AUTOMATION_RUNNER_MISSING"
report_tmp="$QG_LAUNCHD_STATUS_ROOT/.automation-chain-report.tmp-$$"
mark_service "RUNNING" "AUTOMATION_CHAIN_START" "running advisory-only chain"
cd "$QG_BACKEND_ROOT"
"$QG_PYTHON_BIN" tools/run_automation_chain.py \
  --runtime-dir "$QG_RUNTIME_DIR" \
  --symbols "$QG_AUTOMATION_SYMBOLS" \
  --max-age-seconds "$QG_AUTOMATION_MAX_AGE_SECONDS" \
  once > "$report_tmp"
"$QG_PYTHON_BIN" - "$report_tmp" <<'PY'
'''
        + AUTOMATION_CHAIN_VALIDATOR
        + r'''
PY
mv "$report_tmp" "$QG_LAUNCHD_STATUS_ROOT/automation-chain-report.json"
finish_service "PASS" "AUTOMATION_CHAIN_COMPLETE" "all required advisory stages completed"
''',
        "quantgod-health-maintenance.sh": wrapper_header("health-maintenance")
        + r'''
require_file "$QG_BACKEND_ROOT/tools/run_agent_ops_health.py" "HEALTH_RUNNER_MISSING"
require_file "$QG_BACKEND_ROOT/Dashboard/health_api_routes.js" "HEALTH_ENDPOINT_ROUTES_MISSING"
health_tmp="$QG_LAUNCHD_STATUS_ROOT/.agent-ops-health.tmp-$$"
endpoint_tmp="$QG_LAUNCHD_STATUS_ROOT/.endpoint-health.tmp-$$"
mark_service "RUNNING" "HEALTH_REFRESH_START" "refreshing local health evidence"
cd "$QG_BACKEND_ROOT"
"$QG_PYTHON_BIN" tools/run_agent_ops_health.py \
  --runtime-dir "$QG_RUNTIME_DIR" \
  --repo-root "$QG_BACKEND_ROOT" \
  status --write > "$health_tmp"
observed_status="$("$QG_PYTHON_BIN" - "$health_tmp" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
status = str(payload.get("overallStatus") or "UNKNOWN").upper()
if status not in {"PASS", "WARN", "BLOCKED", "STALE", "UNKNOWN"}:
    raise SystemExit("health report returned an invalid overallStatus")
print(status)
PY
)"
"$QG_PYTHON_BIN" - "$QG_DASHBOARD_PORT" > "$endpoint_tmp" <<'PY'
from datetime import datetime, timezone
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base = f"http://127.0.0.1:{int(sys.argv[1])}"

def fetch(path):
    request = Request(base + path, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8")
    return {"httpStatus": status, "body": json.loads(body)}

health = fetch("/healthz")
ready = fetch("/readyz")
overview = fetch("/api/operator/overview")
if health["httpStatus"] != 200 or health["body"].get("ok") is not True:
    raise SystemExit("/healthz did not confirm process liveness")
health_safety = health["body"].get("safety") if isinstance(health["body"].get("safety"), dict) else {}
if health_safety.get("orderSendAllowed") is not False or health_safety.get("executionLaneExists") is not False:
    raise SystemExit("/healthz safety contract is incomplete")
if ready["httpStatus"] not in {200, 503} or ready["body"].get("status") not in {"READY", "NOT_READY"}:
    raise SystemExit("/readyz returned an invalid readiness contract")
if overview["httpStatus"] != 200 or overview["body"].get("ok") is not True:
    raise SystemExit("operator overview is unavailable")
overview_payload = overview["body"].get("payload") if isinstance(overview["body"].get("payload"), dict) else {}
overview_safety = overview_payload.get("safety") if isinstance(overview_payload.get("safety"), dict) else {}
if overview_payload.get("mode") != "SHADOW_READONLY":
    raise SystemExit("operator overview did not confirm SHADOW_READONLY mode")
if overview_safety.get("orderSendAllowed") is not False or overview_payload.get("mt5", {}).get("tradingReady") is not False:
    raise SystemExit("operator overview safety contract is incomplete")
print(json.dumps({
    "schema": "quantgod.launchd_endpoint_health.v1",
    "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "health": health,
    "readiness": ready,
    "operatorOverview": overview,
}, ensure_ascii=False, indent=2, sort_keys=True))
PY
readiness_status="$("$QG_PYTHON_BIN" - "$endpoint_tmp" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["readiness"]["body"]["status"])
PY
)"
mv "$health_tmp" "$QG_LAUNCHD_STATUS_ROOT/agent-ops-health.json"
mv "$endpoint_tmp" "$QG_LAUNCHD_STATUS_ROOT/endpoint-health.json"
finish_service_observed \
  "PASS" \
  "HEALTH_REFRESH_COMPLETE" \
  "health collection completed; agentOpsStatus=$observed_status readinessStatus=$readiness_status" \
  "$observed_status" \
  "$readiness_status"
''',
        "quantgod-log-maintenance.sh": wrapper_header("log-maintenance")
        + r'''
require_file "$QG_BACKEND_ROOT/tools/maintain_runtime_logs.py" "LOG_MAINTENANCE_RUNNER_MISSING"
backend_runtime="$QG_BACKEND_ROOT/runtime"
require_directory "$QG_BACKEND_ROOT" "BACKEND_ROOT_MISSING"
require_directory "$backend_runtime" "BACKEND_RUNTIME_DIR_MISSING"
require_directory "$QG_RUNTIME_DIR" "RUNTIME_DIR_MISSING"
require_directory "$QG_MT5_FILES_DIR" "MT5_FILES_DIR_MISSING"
require_directory "$QG_LAUNCHD_LOG_ROOT" "LAUNCHD_LOG_ROOT_MISSING"
for root in "$QG_BACKEND_ROOT" "$backend_runtime" "$QG_RUNTIME_DIR" "$QG_MT5_FILES_DIR" "$QG_LAUNCHD_LOG_ROOT"; do
  assert_path_has_no_symlink_components "$root"
done

if ! backend_root_real="$(canonical_directory "$QG_BACKEND_ROOT")" || \
   ! backend_runtime_real="$(canonical_directory "$backend_runtime")" || \
   ! runtime_real="$(canonical_directory "$QG_RUNTIME_DIR")" || \
   ! mt5_files_real="$(canonical_directory "$QG_MT5_FILES_DIR")" || \
   ! launchd_log_real="$(canonical_directory "$QG_LAUNCHD_LOG_ROOT")"; then
  block_service "LOG_MAINTENANCE_PATH_RESOLVE_FAILED" "log maintenance roots could not be canonicalized"
fi
[[ "$backend_runtime_real" == "$backend_root_real/runtime" ]] || \
  block_service "BACKEND_RUNTIME_BOUNDARY_FAILED" "Backend runtime root escaped the Backend repository"
[[ "$runtime_real" == "$mt5_files_real" ]] || \
  block_service "MT5_RUNTIME_BOUNDARY_FAILED" "QG_RUNTIME_DIR must match QG_MT5_FILES_DIR"
[[ "$runtime_real" != "/" && "$backend_runtime_real" != "/" && "$launchd_log_real" != "/" ]] || \
  block_service "LOG_MAINTENANCE_ROOT_FORBIDDEN" "filesystem root cannot be maintained"
if [[ "$launchd_log_real" == "$backend_runtime_real" || "$launchd_log_real" == "$runtime_real" ]]; then
  block_service "LOG_MAINTENANCE_ROOT_COLLISION" "launchd logs must not overlap a runtime evidence root"
fi

for key_code in \
  "QG_RUNTIME_LOG_MAX_MB:LOG_MAX_ACTIVE_INVALID" \
  "QG_RUNTIME_LOG_ARCHIVE_MAX_MB:LOG_ARCHIVE_MAX_INVALID" \
  "QG_RUNTIME_LOG_RETENTION_DAYS:LOG_RETENTION_INVALID" \
  "QG_RUNTIME_JSONL_MAX_MB:JSONL_MAX_ACTIVE_INVALID" \
  "QG_RUNTIME_JSONL_ARCHIVE_MAX_MB:JSONL_ARCHIVE_MAX_INVALID" \
  "QG_RUNTIME_JSONL_KEEP_LINES:JSONL_KEEP_LINES_INVALID"; do
  require_positive_integer_env "${key_code%%:*}" "${key_code#*:}"
done

maintain_runtime_target() {
  local target="$1"
  "$QG_PYTHON_BIN" "$QG_BACKEND_ROOT/tools/maintain_runtime_logs.py" \
    --runtime-root "$target" \
    --max-active-mb "$QG_RUNTIME_LOG_MAX_MB" \
    --archive-max-mb "$QG_RUNTIME_LOG_ARCHIVE_MAX_MB" \
    --retention-days "$QG_RUNTIME_LOG_RETENTION_DAYS" \
    --max-jsonl-mb "$QG_RUNTIME_JSONL_MAX_MB" \
    --jsonl-archive-max-mb "$QG_RUNTIME_JSONL_ARCHIVE_MAX_MB" \
    --jsonl-keep-lines "$QG_RUNTIME_JSONL_KEEP_LINES"
}
mark_service "RUNNING" "LOG_MAINTENANCE_START" "rotating Backend runtime, MT5 evidence, and launchd logs"
maintain_runtime_target "$backend_runtime_real"
if [[ "$runtime_real" != "$backend_runtime_real" ]]; then
  maintain_runtime_target "$runtime_real"
fi
"$QG_PYTHON_BIN" "$QG_BACKEND_ROOT/tools/maintain_runtime_logs.py" \
  --runtime-root "$launchd_log_real" \
  --max-active-mb "$QG_RUNTIME_LOG_MAX_MB" \
  --archive-max-mb "$QG_RUNTIME_LOG_ARCHIVE_MAX_MB" \
  --retention-days "$QG_RUNTIME_LOG_RETENTION_DAYS" \
  --no-jsonl-maintenance
finish_service "PASS" "LOG_MAINTENANCE_COMPLETE" "Backend runtime, MT5 evidence, and launchd log maintenance completed"
''',
        "quantgod-sqlite-backup.sh": wrapper_header("sqlite-backup")
        + r'''
require_file "$QG_BACKEND_ROOT/tools/run_local_shadow_backup.py" "SQLITE_BACKUP_RUNNER_MISSING"
assert_path_has_no_symlink_components "$QG_LOCAL_SQLITE_BACKUP_ROOT"
mark_service "RUNNING" "SQLITE_BACKUP_START" "starting verified online SQLite backups"
backup_tmp="$QG_LAUNCHD_STATUS_ROOT/.sqlite-backup.tmp-$$"
verify_tmp="$QG_LAUNCHD_STATUS_ROOT/.sqlite-backup-verify.tmp-$$"
"$QG_PYTHON_BIN" "$QG_BACKEND_ROOT/tools/run_local_shadow_backup.py" \
  --runtime-dir "$QG_RUNTIME_DIR" \
  --backup-root "$QG_LOCAL_SQLITE_BACKUP_ROOT" \
  backup > "$backup_tmp"
backup_path="$("$QG_PYTHON_BIN" - "$backup_tmp" "$QG_LOCAL_SQLITE_BACKUP_ROOT" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(sys.argv[2]).expanduser().resolve()
target = Path(str(payload.get("backupPath") or "")).expanduser().resolve()
if payload.get("schema") != "quantgod.local_shadow_backup.v1":
    raise SystemExit("backup returned an unexpected schema")
if payload.get("mode") != "SHADOW_READONLY":
    raise SystemExit("backup did not confirm SHADOW_READONLY mode")
safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
if safety.get("orderSendAllowed") is not False or safety.get("livePresetMutationAllowed") is not False:
    raise SystemExit("backup safety contract is incomplete")
if target.parent != root or not target.is_dir():
    raise SystemExit("backup path is outside the configured local backup root")
print(target)
PY
)"
"$QG_PYTHON_BIN" "$QG_BACKEND_ROOT/tools/run_local_shadow_backup.py" \
  --runtime-dir "$QG_RUNTIME_DIR" \
  --backup-root "$QG_LOCAL_SQLITE_BACKUP_ROOT" \
  verify --backup-dir "$backup_path" > "$verify_tmp"
"$QG_PYTHON_BIN" - \
  "$backup_tmp" \
  "$verify_tmp" \
  "$QG_LOCAL_SQLITE_BACKUP_ROOT" \
  "$QG_SQLITE_BACKUP_KEEP" \
  "$QG_LAUNCHD_STATUS_ROOT" <<'PY'
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

backup_payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
verification = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
root = Path(sys.argv[3]).expanduser().resolve()
keep = max(1, int(sys.argv[4]))
status_root = Path(sys.argv[5]).expanduser().resolve()
current = Path(str(backup_payload.get("backupPath") or "")).expanduser().resolve()
if verification.get("schema") != "quantgod.local_shadow_backup_verification.v1" or verification.get("ok") is not True:
    raise SystemExit("backup verification did not confirm ok=true")
if Path(str(verification.get("backupPath") or "")).expanduser().resolve() != current:
    raise SystemExit("backup verification path does not match the created backup")
safety = verification.get("safety") if isinstance(verification.get("safety"), dict) else {}
if safety.get("orderSendAllowed") is not False or safety.get("mutatesMt5") is not False:
    raise SystemExit("backup verification safety contract is incomplete")
if current.parent != root or not current.is_dir():
    raise SystemExit("verified backup path escaped the configured backup root")

def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

atomic_json(current / "verification.json", verification)
atomic_json(status_root / "sqlite-backup-report.json", backup_payload)
atomic_json(status_root / "sqlite-backup-verification.json", verification)

def verified_identity(candidate):
    if candidate.is_symlink() or not candidate.is_dir() or candidate.name.startswith("."):
        return False
    resolved = candidate.resolve()
    if resolved.parent != root:
        return False
    manifest_path = candidate / "manifest.json"
    verification_path = candidate / "verification.json"
    if not manifest_path.is_file() or not verification_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        prior = json.loads(verification_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    backup_id = str(manifest.get("backupId") or "")
    if manifest.get("schema") != "quantgod.local_shadow_backup.v1" or backup_id != candidate.name:
        return False
    if prior.get("schema") != "quantgod.local_shadow_backup_verification.v1" or prior.get("ok") is not True:
        return False
    if str(prior.get("backupId") or "") != backup_id:
        return False
    if Path(str(prior.get("backupPath") or "")).expanduser().resolve() != resolved:
        return False
    prior_safety = prior.get("safety") if isinstance(prior.get("safety"), dict) else {}
    if prior_safety.get("orderSendAllowed") is not False or prior_safety.get("mutatesMt5") is not False:
        return False
    files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    checks = prior.get("checks") if isinstance(prior.get("checks"), list) else []
    if not files or not checks or not all(isinstance(row, dict) and row.get("ok") is True for row in checks):
        return False
    manifest_paths = {str(row.get("relativePath") or "") for row in files if isinstance(row, dict)}
    checked_paths = {str(row.get("relativePath") or "") for row in checks if isinstance(row, dict)}
    return bool(manifest_paths) and manifest_paths == checked_paths

verified = []
for candidate in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
    if verified_identity(candidate):
        verified.append(candidate)
for candidate in verified[keep:]:
    resolved = candidate.resolve()
    if resolved.parent != root or resolved == current or not verified_identity(candidate):
        continue
    shutil.rmtree(resolved)

Path(sys.argv[1]).unlink(missing_ok=True)
Path(sys.argv[2]).unlink(missing_ok=True)
PY
finish_service "PASS" "SQLITE_BACKUP_COMPLETE" "all configured SQLite stores were verified and backed up"
''',
        "quantgod-ai-telegram-monitor.sh": wrapper_header(
            "ai-telegram-monitor",
            ".env.local", ".env.telegram.local", ".env.deepseek.local"
        )
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
    primary_mt5_wrapper = wrappers["quantgod-mt5-shadow-supervisor.sh"]
    secondary_mt5_wrapper = primary_mt5_wrapper.replace(
        'QG_SERVICE_NAME="mt5-shadow-supervisor"',
        'QG_SERVICE_NAME="mt5-secondary-shadow-supervisor"',
        1,
    ).replace(
        f"[[ \"$QG_MT5_EXPECTED_SERVER\" == '{MT5_EXPECTED_BROKER_SERVER}' ]]",
        f"[[ \"$QG_MT5_EXPECTED_SERVER\" == '{MT5_SECONDARY_EXPECTED_BROKER_SERVER}' ]]",
        1,
    ).replace(
        "QuantGod_MT5_HFM_Shadow_mac.ini",
        "QuantGod_MT5_HFM_SecondaryShadow_mac.ini",
    )
    secondary_aliases = r'''
[[ "$QG_MT5_SECONDARY_SHADOW_ENABLED" == "1" ]] || block_service "MT5_SECONDARY_SHADOW_DISABLED" "the optional secondary Shadow observer is not enabled by this profile"
QG_MT5_WINE_PREFIX="$QG_MT5_SECONDARY_WINE_PREFIX"
QG_MT5_ROOT="$QG_MT5_SECONDARY_ROOT"
QG_MT5_TERMINAL_PATH="$QG_MT5_SECONDARY_TERMINAL_PATH"
QG_MT5_SHADOW_CONFIG="$QG_MT5_SECONDARY_SHADOW_CONFIG"
QG_MT5_LOGIN_REFERENCE_CONFIG="$QG_MT5_SECONDARY_LOGIN_REFERENCE_CONFIG"
QG_MT5_SHADOW_PRESET="$QG_MT5_SECONDARY_SHADOW_PRESET"
QG_MT5_SHADOW_PRESET_SOURCE="$QG_MT5_SECONDARY_SHADOW_PRESET_SOURCE"
QG_MT5_SHADOW_CONFIG_WINDOWS="$QG_MT5_SECONDARY_SHADOW_CONFIG_WINDOWS"
QG_MT5_EXPECTED_SERVER="$QG_MT5_SECONDARY_EXPECTED_SERVER"
QG_MT5_PEER_ROOT="$QG_MT5_SECONDARY_PEER_ROOT"
QG_MT5_PEER_TERMINAL_PATH="$QG_MT5_SECONDARY_PEER_TERMINAL_PATH"
QG_MT5_PEER_SHADOW_CONFIG_WINDOWS="$QG_MT5_SECONDARY_PEER_SHADOW_CONFIG_WINDOWS"
'''
    supervisor_start = 'require_executable "$QG_MT5_WINE_BIN" "MT5_WINE_MISSING"'
    if supervisor_start not in secondary_mt5_wrapper:
        raise RuntimeError("MT5 supervisor template start marker is missing")
    secondary_mt5_wrapper = secondary_mt5_wrapper.replace(
        supervisor_start,
        secondary_aliases + supervisor_start,
        1,
    )
    wrappers["quantgod-mt5-secondary-shadow-supervisor.sh"] = secondary_mt5_wrapper

    return {
        name: content.replace("__MT5_TERMINAL_PROCESS_PATTERN__", MT5_TERMINAL_PROCESS_PATTERN)
        for name, content in wrappers.items()
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
        "Umask": 0o077,
        "ProcessType": "Background",
        "ExitTimeOut": 15,
        "EnvironmentVariables": {
            "QG_LAUNCHD_ENV_FILE": str(ENV_PATH),
            "PYTHONIOENCODING": "utf-8",
        },
    }
    mt5_labels = {
        SERVICES["mt5-shadow-supervisor"]["label"],
        SERVICES["mt5-secondary-shadow-supervisor"]["label"],
    }
    if label in mt5_labels:
        wine_user_environment = local_user_environment()
        issues = local_user_environment_issues(wine_user_environment)
        if issues:
            raise ValueError("invalid MT5 Wine user environment: " + ",".join(issues))
        payload["EnvironmentVariables"].update(wine_user_environment)
        if label == SERVICES["mt5-shadow-supervisor"]["label"]:
            payload["EnvironmentVariables"]["QG_MT5_EXPECTED_SERVER"] = MT5_EXPECTED_BROKER_SERVER
            payload["EnvironmentVariables"]["QG_MT5_LOGIN_REFERENCE_CONFIG"] = str(
                default_mt5_login_reference_config()
            )
        else:
            payload["EnvironmentVariables"]["QG_MT5_SECONDARY_EXPECTED_SERVER"] = (
                MT5_SECONDARY_EXPECTED_BROKER_SERVER
            )
            payload["EnvironmentVariables"]["QG_MT5_SECONDARY_LOGIN_REFERENCE_CONFIG"] = str(
                default_mt5_secondary_login_reference_config()
            )
    if service["kind"] == "keepalive":
        payload["KeepAlive"] = {"SuccessfulExit": False}
    elif service["kind"] == "always":
        payload["KeepAlive"] = True
    elif service["kind"] == "interval":
        payload["StartInterval"] = int(service["interval"])
    elif service["kind"] != "oneshot":
        raise ValueError(f"unsupported launchd service kind: {service['kind']}")
    if service.get("throttle"):
        payload["ThrottleInterval"] = int(service["throttle"])
    return payload


def run_launchctl(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=check)


def launchd_lifecycle(service: dict[str, Any], output: str) -> dict[str, Any]:
    values: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        for key in ("state", "last exit code"):
            prefix = f"{key} ="
            if key not in values and line.startswith(prefix):
                values[key] = line[len(prefix) :].strip()

    launchd_state = values.get("state", "unknown")
    last_exit_raw = values.get("last exit code")
    last_exit_code = (
        int(last_exit_raw)
        if last_exit_raw is not None and re.fullmatch(r"-?[0-9]+", last_exit_raw)
        else None
    )
    normalized_state = launchd_state.lower()
    if normalized_state == "running":
        lifecycle = "RUNNING"
    elif normalized_state == "not running" and service.get("kind") == "interval":
        if last_exit_code is None:
            lifecycle = "IDLE_PENDING"
        elif last_exit_code == 0:
            lifecycle = "IDLE_OK"
        else:
            lifecycle = "FAILED"
    elif normalized_state == "not running":
        lifecycle = "STOPPED"
    else:
        lifecycle = "UNKNOWN"
    return {
        "lifecycle": lifecycle,
        "launchdState": launchd_state,
        "lastExitCode": last_exit_code,
        "lastExitRaw": last_exit_raw,
    }


def launchd_load_state(label: str) -> str:
    result = run_launchctl(["print", f"{USER_DOMAIN}/{label}"], check=False)
    if result.returncode == 0:
        return "loaded"
    detail = "\n".join(part for part in (result.stderr, result.stdout) if part).lower()
    not_loaded_markers = (
        "could not find service",
        "could not find specified service",
        "service not found",
    )
    if any(marker in detail for marker in not_loaded_markers):
        return "not-loaded"
    raise RuntimeError(
        (result.stderr or result.stdout or "").strip()
        or f"launchctl state query failed for {label} with exit {result.returncode}"
    )


def bootout(label: str, *, verify: bool = False) -> None:
    result = run_launchctl(["bootout", USER_DOMAIN, str(plist_path(label))], check=False)
    if verify and launchd_load_state(label) != "not-loaded":
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"launchctl bootout did not unload {label}")


def bootstrap(label: str) -> None:
    result = run_launchctl(["bootstrap", USER_DOMAIN, str(plist_path(label))], check=False)
    if result.returncode != 0 and "service already loaded" in (result.stderr or "").lower():
        bootout(label, verify=True)
        result = run_launchctl(["bootstrap", USER_DOMAIN, str(plist_path(label))], check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"launchctl bootstrap failed for {label}")


def is_loaded(label: str) -> bool:
    return launchd_load_state(label) == "loaded"


def managed_config_paths() -> tuple[Path, ...]:
    return (
        ENV_PATH,
        CAPABILITY_PATH,
        *(BIN_DIR / service["wrapper"] for service in SERVICES.values()),
        *(plist_path(service["label"]) for service in SERVICES.values()),
    )


def snapshot_managed_files() -> dict[Path, tuple[bytes, int] | None]:
    snapshot: dict[Path, tuple[bytes, int] | None] = {}
    for path in managed_config_paths():
        if path.is_symlink():
            raise RuntimeError(f"refusing to snapshot symlinked QuantGod managed file: {path}")
        if path.exists() and not path.is_file():
            raise RuntimeError(f"refusing non-file QuantGod managed path: {path}")
        snapshot[path] = (
            (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            if path.is_file()
            else None
        )
    return snapshot


def restore_managed_files(snapshot: dict[Path, tuple[bytes, int] | None]) -> None:
    for path, previous in snapshot.items():
        if path.is_symlink():
            raise RuntimeError(f"refusing to restore over symlinked QuantGod managed file: {path}")
        if previous is None:
            if path.exists():
                if not path.is_file():
                    raise RuntimeError(f"refusing to remove non-file QuantGod managed path: {path}")
                path.unlink()
            continue
        content, mode = previous
        atomic_write_bytes(path, content, mode)


def build_frontend_dist(paths: dict[str, Path]) -> None:
    """Compile Frontend without changing the Backend's active /vue tree."""
    npm_bin = which("npm", "/usr/bin/npm")
    subprocess.run([npm_bin, "run", "build"], cwd=paths["frontend"], check=True)


def publish_frontend_dist(paths: dict[str, Path], workspace: Path) -> None:
    """Atomically publish the already-built Frontend after Backend is stopped."""

    python_bin = which("python3", "/usr/bin/python3")
    subprocess.run(
        [
            python_bin,
            str(paths["infra"] / "scripts/qg-workspace.py"),
            "--workspace",
            str(workspace.expanduser().resolve()),
            "sync-frontend-dist",
        ],
        cwd=paths["infra"],
        check=True,
    )


def stage_frontend_dist_rollback(paths: dict[str, Path]) -> tuple[Path, Path | None]:
    active = paths["backend"] / "Dashboard/vue-dist"
    rollback = active.with_name(f".vue-dist.launchd-rollback-{uuid.uuid4().hex}")
    if active.is_symlink():
        raise RuntimeError(f"refusing symlinked active Frontend dist: {active}")
    if active.exists() and not active.is_dir():
        raise RuntimeError(f"active Frontend dist is not a directory: {active}")
    if active.is_dir():
        os.replace(active, rollback)
        return active, rollback
    return active, None


def restore_frontend_dist(snapshot: tuple[Path, Path | None]) -> None:
    active, rollback = snapshot
    if active.is_symlink():
        raise RuntimeError(f"refusing to restore over symlinked Frontend dist: {active}")
    if active.exists():
        if not active.is_dir():
            raise RuntimeError(f"refusing to remove non-directory Frontend dist: {active}")
        shutil.rmtree(active)
    if rollback is not None:
        if rollback.is_symlink() or not rollback.is_dir():
            raise RuntimeError(f"Frontend rollback snapshot is unavailable: {rollback}")
        os.replace(rollback, active)


def discard_frontend_dist_rollback(snapshot: tuple[Path, Path | None]) -> None:
    _active, rollback = snapshot
    if rollback is None or not rollback.exists():
        return
    if rollback.is_symlink() or not rollback.is_dir():
        raise RuntimeError(f"refusing invalid Frontend rollback snapshot: {rollback}")
    shutil.rmtree(rollback)


def snapshot_secondary_shadow_preset() -> tuple[bytes, int] | None:
    """Capture only the isolated secondary preset for transactional rollback."""

    target = default_mt5_secondary_shadow_preset()
    directory_issues = secondary_shadow_preset_directory_issues(target)
    if directory_issues:
        raise RuntimeError(
            "secondary Shadow preset directory is unsafe: "
            + ",".join(directory_issues)
        )
    if target.is_symlink():
        raise RuntimeError(f"refusing symlinked secondary Shadow preset: {target}")
    if target.exists() and not target.is_file():
        raise RuntimeError(f"refusing non-file secondary Shadow preset: {target}")
    if not target.exists():
        return None
    return target.read_bytes(), stat.S_IMODE(target.stat().st_mode)


def restore_secondary_shadow_preset(snapshot: tuple[bytes, int] | None) -> None:
    target = default_mt5_secondary_shadow_preset()
    directory_issues = secondary_shadow_preset_directory_issues(target)
    if directory_issues:
        raise RuntimeError(
            "secondary Shadow preset directory is unsafe: "
            + ",".join(directory_issues)
        )
    if target.is_symlink():
        raise RuntimeError(f"refusing to restore over symlinked secondary Shadow preset: {target}")
    if snapshot is None:
        if target.exists():
            if not target.is_file():
                raise RuntimeError(f"refusing to remove non-file secondary Shadow preset: {target}")
            target.unlink()
        return
    content, mode = snapshot
    atomic_write_bytes(target, content, mode)


def deploy_secondary_shadow_preset(paths: dict[str, Path]) -> Path:
    """Atomically publish the reviewed USD/AUTO preset into the Live16 prefix."""

    source = backend_mt5_secondary_shadow_preset(paths)
    source_issues = secondary_shadow_preset_source_issues(source)
    if source_issues:
        raise RuntimeError(
            "secondary Shadow preset source is unsafe: " + ",".join(sorted(source_issues))
        )
    target = default_mt5_secondary_shadow_preset()
    directory_issues = secondary_shadow_preset_directory_issues(target)
    if directory_issues:
        raise RuntimeError(
            "secondary Shadow preset directory is unsafe: "
            + ",".join(directory_issues)
        )
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RuntimeError(f"secondary Shadow preset target is unsafe: {target}")
    content = source.read_bytes()
    atomic_write_bytes(target, content, PRIVATE_FILE_MODE)
    if not hmac.compare_digest(content, target.read_bytes()):
        raise RuntimeError("secondary Shadow preset publication verification failed")
    deployed_issues = _preset_value_issues(
        _read_key_value_lists(target),
        MT5_SECONDARY_REQUIRED_PRESET_VALUES,
        code_prefix="MT5_SHADOW_PRESET",
    )
    if deployed_issues:
        raise RuntimeError(
            "secondary Shadow preset publication violated its contract: "
            + ",".join(sorted(deployed_issues))
        )
    return target


def selected_capability_blockers(
    capabilities: dict[str, dict[str, Any]],
    selected_names: tuple[str, ...],
    *,
    allow_frontend_build_output: bool = False,
    allow_secondary_preset_deploy: bool = False,
) -> list[dict[str, Any]]:
    blocked: list[dict[str, Any]] = []
    frontend_builder_ready = (
        "frontend-dist-build" in selected_names
        and capabilities["frontend-dist-build"]["ready"]
    )
    for name in selected_names:
        row = capabilities[name]
        if row["ready"]:
            continue
        reasons = set(row["reasonCodes"])
        if (
            allow_frontend_build_output
            and name == "backend-api"
            and frontend_builder_ready
            and reasons == {"COMPILED_FRONTEND_MISSING"}
        ):
            continue
        if (
            allow_secondary_preset_deploy
            and name == "mt5-secondary-shadow-supervisor"
            and reasons
            and reasons <= MT5_SECONDARY_PRESET_DEPLOYABLE_REASONS
        ):
            continue
        blocked.append(row)
    return blocked


def write_files(
    workspace: Path,
    *,
    resolved_paths: dict[str, Path] | None = None,
    secondary_shadow_enabled: bool = False,
) -> dict[str, Path]:
    paths = resolved_paths or load_workspace(workspace)
    harden_private_directory(PRIVATE_ROOT)
    harden_private_directory(BIN_DIR)
    harden_private_directory(LOG_DIR)
    harden_private_directory(LOCK_DIR)
    harden_private_directory(STATUS_DIR)
    harden_private_directory(BACKUP_DIR)
    LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    if ENV_PATH.is_symlink():
        raise RuntimeError(f"refusing symlink for QuantGod private environment: {ENV_PATH}")
    atomic_write_text(
        ENV_PATH,
        render_env(
            paths,
            workspace=workspace,
            secondary_shadow_enabled=secondary_shadow_enabled,
        ),
        PRIVATE_FILE_MODE,
    )
    for name, content in render_wrappers().items():
        write_executable(BIN_DIR / name, content)
    for service in SERVICES.values():
        for stream in ("out", "err"):
            log_path = LOG_DIR / f"{service['label']}.{stream}.log"
            if log_path.is_symlink():
                raise RuntimeError(f"refusing symlink for QuantGod private log: {log_path}")
            log_path.touch(exist_ok=True)
            harden_private_file(log_path)
        service_plist = plist_path(service["label"])
        if service_plist.is_symlink():
            raise RuntimeError(f"refusing symlink for QuantGod LaunchAgent: {service_plist}")
        atomic_write_bytes(
            service_plist,
            plistlib.dumps(render_plist(service), fmt=plistlib.FMT_XML, sort_keys=True),
            PRIVATE_FILE_MODE,
        )
    capability_report = build_capability_report(paths, workspace=workspace)
    atomic_write_text(
        CAPABILITY_PATH,
        json.dumps(capability_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        PRIVATE_FILE_MODE,
    )
    harden_log_files()
    return paths


def install(args: argparse.Namespace) -> int:
    paths = load_workspace(args.workspace)
    all_labels = [service["label"] for service in SERVICES.values()]
    selected_names = SERVICE_PROFILES[args.profile]
    runtime_selected_names = tuple(name for name in selected_names if name != "frontend-dist-build")
    selected_labels = [SERVICES[name]["label"] for name in runtime_selected_names]
    secondary_preset_deploy_selected = (
        "mt5-secondary-shadow-supervisor" in runtime_selected_names
    )
    capabilities = build_capability_report(paths, workspace=args.workspace)["services"]
    if args.no_load:
        print("QuantGod macOS launchd preflight (no files or services changed).")
        print(f"Backend:  {paths['backend']}")
        print(f"Frontend: {paths['frontend']}")
        print(f"Profile:  {args.profile}")
        preview_blocked = selected_capability_blockers(
            capabilities,
            selected_names,
            allow_frontend_build_output=True,
            allow_secondary_preset_deploy=secondary_preset_deploy_selected,
        )
        preview_blocked_names = {row["service"] for row in preview_blocked}
        frontend_builder_ready = capabilities.get("frontend-dist-build", {}).get("ready") is True
        for row in (capabilities[name] for name in selected_names):
            reasons = ",".join(row["reasonCodes"]) or "reviewed dependencies available"
            status = row["status"]
            if (
                row["service"] == "backend-api"
                and frontend_builder_ready
                and set(row["reasonCodes"]) == {"COMPILED_FRONTEND_MISSING"}
            ):
                status = "READY_AFTER_BUILD"
                reasons = "compiled Frontend will be produced before service switch"
            elif (
                row["service"] == "mt5-secondary-shadow-supervisor"
                and set(row["reasonCodes"])
                and set(row["reasonCodes"]) <= MT5_SECONDARY_PRESET_DEPLOYABLE_REASONS
            ):
                status = "READY_AFTER_PRESET_DEPLOY"
                reasons = "reviewed Backend USDJPY/AUTO preset will be atomically deployed"
            print(f"  {row['service']}: {status} - {reasons}")
        print("--no-load performed capability inspection only; it wrote no env, wrapper, plist, or status file.")
        if preview_blocked_names:
            print("Preflight BLOCKED: " + ", ".join(sorted(preview_blocked_names)))
            return 2
        print("Preflight READY: all dependencies exist or are synchronously buildable/deployable.")
        return 0

    blocked = selected_capability_blockers(
        capabilities,
        selected_names,
        allow_frontend_build_output=True,
        allow_secondary_preset_deploy=secondary_preset_deploy_selected,
    )
    if blocked:
        details = "; ".join(
            f"{row['service']}={','.join(row['reasonCodes'])}"
            for row in blocked
        )
        raise RuntimeError(
            "refusing to change loaded services because the selected profile has blocked capabilities: "
            + details
        )

    frontend_build_selected = "frontend-dist-build" in selected_names
    if frontend_build_selected:
        build_frontend_dist(paths)
        capabilities = build_capability_report(paths, workspace=args.workspace)["services"]
        blocked = selected_capability_blockers(
            capabilities,
            selected_names,
            allow_frontend_build_output=True,
            allow_secondary_preset_deploy=secondary_preset_deploy_selected,
        )
        if blocked:
            details = "; ".join(
                f"{row['service']}={','.join(row['reasonCodes'])}"
                for row in blocked
            )
            raise RuntimeError(
                "frontend build completed but pre-switch capability preflight is blocked: "
                + details
            )

    previous_loaded = [label for label in all_labels if is_loaded(label)]
    previous_files = snapshot_managed_files()
    secondary_preset_snapshot = (
        snapshot_secondary_shadow_preset()
        if secondary_preset_deploy_selected
        else None
    )
    loaded_labels: list[str] = []
    frontend_snapshot: tuple[Path, Path | None] | None = None
    try:
        for label in all_labels:
            bootout(label, verify=True)
        if frontend_build_selected:
            frontend_snapshot = stage_frontend_dist_rollback(paths)
            publish_frontend_dist(paths, args.workspace)
        if secondary_preset_deploy_selected:
            deploy_secondary_shadow_preset(paths)
        if frontend_build_selected or secondary_preset_deploy_selected:
            capabilities = build_capability_report(paths, workspace=args.workspace)["services"]
            blocked = selected_capability_blockers(capabilities, selected_names)
            if blocked:
                details = "; ".join(
                    f"{row['service']}={','.join(row['reasonCodes'])}"
                    for row in blocked
                )
                raise RuntimeError(
                    "staged publication completed but final capability preflight is blocked: "
                    + details
                )
        write_files(
            args.workspace,
            resolved_paths=paths,
            secondary_shadow_enabled="mt5-secondary-shadow-supervisor" in runtime_selected_names,
        )
        for label in selected_labels:
            bootstrap(label)
            loaded_labels.append(label)
        if frontend_snapshot is not None:
            discard_frontend_dist_rollback(frontend_snapshot)
    except Exception as install_error:
        rollback_errors: list[str] = []
        for label in all_labels:
            try:
                bootout(label, verify=True)
            except Exception as exc:
                rollback_errors.append(f"service stop failed for {label}: {exc}")
        try:
            restore_managed_files(previous_files)
        except Exception as exc:
            rollback_errors.append(f"file restore failed: {exc}")
        if frontend_snapshot is not None:
            try:
                restore_frontend_dist(frontend_snapshot)
            except Exception as exc:
                rollback_errors.append(f"Frontend dist restore failed: {exc}")
        if secondary_preset_deploy_selected:
            try:
                restore_secondary_shadow_preset(secondary_preset_snapshot)
            except Exception as exc:
                rollback_errors.append(f"secondary Shadow preset restore failed: {exc}")
        stop_failed = any(message.startswith("service stop failed") for message in rollback_errors)
        if not stop_failed:
            for label in previous_loaded:
                try:
                    bootstrap(label)
                except Exception as exc:
                    rollback_errors.append(f"service restore failed for {label}: {exc}")
        if rollback_errors:
            raise RuntimeError(
                f"launchd install failed ({install_error}); rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from install_error
        raise

    print("QuantGod macOS launchd automation installed.")
    print(f"Backend:  {paths['backend']}")
    print(f"Frontend: {paths['frontend']}")
    print(f"Env:      {ENV_PATH}")
    print(f"Logs:     {LOG_DIR}")
    print(f"Capabilities: {CAPABILITY_PATH}")
    print(f"Generated profile: {args.profile}")
    for row in (capabilities[name] for name in selected_names):
        reasons = ",".join(row["reasonCodes"]) or "reviewed dependencies available"
        print(f"  {row['service']}: {row['status']} - {reasons}")
    print(f"Loaded profile: {args.profile}")
    print("Loaded services: " + ", ".join(selected_labels))
    if "frontend-dist-build" in selected_names:
        print("Preload stage completed: Frontend build and atomic Backend /vue publication.")
    disabled = [label for label in all_labels if label not in selected_labels]
    if disabled:
        print("Optional services left unloaded: " + ", ".join(disabled))
    return 0


def uninstall(_args: argparse.Namespace) -> int:
    for service in SERVICES.values():
        label = service["label"]
        bootout(label, verify=True)
        path = plist_path(label)
        if path.exists():
            path.unlink()
    print("QuantGod macOS LaunchAgents unloaded and removed. Private env/logs were kept under ~/.quantgod.")
    return 0


def status(args: argparse.Namespace) -> int:
    paths = load_workspace(args.workspace)
    capabilities = build_capability_report(paths, workspace=args.workspace)["services"]
    for name, service in SERVICES.items():
        label = service["label"]
        result = run_launchctl(["print", f"{USER_DOMAIN}/{label}"], check=False)
        state = "loaded" if result.returncode == 0 else "not loaded"
        print(f"{label}: {state} - {service['description']}")
        capability = capabilities[name]
        reasons = ",".join(capability["reasonCodes"]) or "reviewed dependencies available"
        print(f"  capability={capability['status']} {reasons}")
        runtime_status_path = STATUS_DIR / f"{name}.json"
        if runtime_status_path.is_file():
            try:
                runtime_status = json.loads(runtime_status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                print("  runtimeStatus=INVALID")
            else:
                print(
                    "  runtimeStatus="
                    f"{runtime_status.get('status', 'UNKNOWN')} "
                    f"taskStatus={runtime_status.get('taskStatus', runtime_status.get('status', 'UNKNOWN'))} "
                    f"observedHealth={runtime_status.get('observedHealth') or 'N/A'} "
                    f"observedReadiness={runtime_status.get('observedReadiness') or 'N/A'} "
                    f"code={runtime_status.get('code', 'UNKNOWN')} "
                    f"generatedAt={runtime_status.get('generatedAt', 'UNKNOWN')}"
                )
        if result.returncode == 0:
            lifecycle = launchd_lifecycle(service, result.stdout)
            last_exit = lifecycle["lastExitRaw"] or "unknown"
            print(
                f"  lifecycle={lifecycle['lifecycle']} "
                f"launchdState={lifecycle['launchdState']} "
                f"lastExit={last_exit}"
            )
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
    print("service capabilities")
    capabilities = build_capability_report(paths, workspace=args.workspace)["services"]
    for name, row in capabilities.items():
        reasons = ",".join(row["reasonCodes"]) or "reviewed dependencies available"
        print(f"  {name:24s} {row['status']:7s} {reasons}")
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
    install_parser = sub.add_parser("install", help="Preflight, atomically publish, and load LaunchAgents")
    install_parser.add_argument(
        "--no-load",
        action="store_true",
        help="Capability preflight only; write no active files and do not change launchd state",
    )
    install_parser.add_argument(
        "--profile",
        choices=tuple(SERVICE_PROFILES),
        default="core",
        help="Services to load; default core starts only the local Backend and Frontend",
    )
    install_parser.set_defaults(func=install)
    sub.add_parser("uninstall", help="Unload and remove LaunchAgents").set_defaults(func=uninstall)
    sub.add_parser("status", help="Show launchd status").set_defaults(func=status)
    sub.add_parser("doctor", help="Check local paths and command dependencies").set_defaults(func=doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
