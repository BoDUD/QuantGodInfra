from __future__ import annotations

import importlib.util
import inspect
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "qg-macos-launchd.py"
spec = importlib.util.spec_from_file_location("qg_macos_launchd", MODULE_PATH)
assert spec is not None and spec.loader is not None
launchd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launchd)


class MacLaunchdHelperTests(unittest.TestCase):
    AUTOMATION_REQUIRED_STEP_IDS = (
        "adaptive_policy",
        "dynamic_sltp",
        "entry_trigger",
        "usdjpy_strategy_policy",
        "usdjpy_ea_dry_run",
        "usdjpy_live_loop",
    )

    def setUp(self) -> None:
        # Linux CI commonly exposes only the root-owned /tmp when TMPDIR is
        # absent. launchd on macOS supplies a per-user TMPDIR, so model that
        # contract explicitly instead of weakening the production owner check.
        self._launchd_user_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._launchd_user_tmp.cleanup)
        self._tmpdir_patch = mock.patch.dict(
            os.environ,
            {"TMPDIR": self._launchd_user_tmp.name},
        )
        self._tmpdir_patch.start()
        self.addCleanup(self._tmpdir_patch.stop)

    def _automation_payload(self, *, identity_field: str = "name") -> dict[str, object]:
        steps = [
            {identity_field: step_id, "required": True, "ok": True}
            for step_id in self.AUTOMATION_REQUIRED_STEP_IDS
        ]
        return {
            "schema": "quantgod.automation_chain.v1",
            "runStatus": "COMPLETED",
            "steps": steps,
            "requiredStepCount": len(steps),
            "requiredFailedCount": 0,
            "safety": {
                "orderSendAllowed": False,
                "brokerExecutionAllowed": False,
                "livePresetMutationAllowed": False,
            },
        }

    def _validate_automation_payload(
        self, payload: dict[str, object]
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            report = pathlib.Path(tmp) / "automation.json"
            report.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.run(
                [sys.executable, "-", str(report)],
                input=launchd.AUTOMATION_CHAIN_VALIDATOR,
                capture_output=True,
                text=True,
                timeout=5,
            )

    def _create_mt5_wrapper_fixture(
        self,
        root: pathlib.Path,
        wine_body: str,
        *,
        extra_env: dict[str, str] | None = None,
        pgrep_body: str = "#!/bin/bash\nexit 1\n",
    ) -> dict[str, pathlib.Path]:
        root = root.resolve()
        private_root = root / "private"
        bin_dir = private_root / "bin"
        log_dir = private_root / "logs"
        lock_dir = private_root / "locks"
        status_dir = private_root / "status"
        backup_dir = private_root / "backups" / "sqlite"
        env_path = private_root / "launchd.env"
        prefix = root / "mt5-prefix"
        mt5_root = prefix / "drive_c" / "Program Files" / "MetaTrader 5"
        peer_prefix = root / "mt5-peer-prefix"
        peer_root = peer_prefix / "drive_c" / "Program Files" / "MetaTrader 5"
        config_root = prefix / "drive_c" / "qg"
        shadow_config = config_root / "QuantGod_MT5_HFM_Shadow_mac.ini"
        login_reference = config_root / "QuantGod_MT5_LoginOnly_mac.ini"
        preset = mt5_root / "MQL5" / "Presets" / "QuantGod_MT5_HFM_Shadow.set"
        terminal = mt5_root / "terminal64.exe"
        peer_terminal = peer_root / "terminal64.exe"
        ea_source = mt5_root / "MQL5" / "Experts" / "QuantGod_MultiStrategy.mq5"
        ea_binary = mt5_root / "MQL5" / "Experts" / "QuantGod_MultiStrategy.ex5"
        verified_source = config_root / "QuantGod_MultiStrategy.mq5"
        verified_binary = config_root / "QuantGod_MultiStrategy.ex5"
        verified_compile_log = config_root / "compile.log"
        fake_bin = root / "fake-bin"
        fake_pgrep = fake_bin / "pgrep"
        fake_wine = root / "fake-wine"
        backend = root / "backend"
        for directory in (
            bin_dir,
            log_dir,
            lock_dir,
            status_dir,
            backup_dir,
            config_root,
            preset.parent,
            peer_root,
            ea_source.parent,
            backend,
            fake_bin,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        shadow_config.write_text(
            f"[Common]\nLogin=123456789\nServer={launchd.MT5_EXPECTED_BROKER_SERVER}\n"
            "[Experts]\nAllowLiveTrading=0\nAllowDllImport=0\n"
            "[StartUp]\nExpertParameters=QuantGod_MT5_HFM_Shadow.set\n",
            encoding="utf-8",
        )
        login_reference.write_text(
            f"[Common]\nLogin=123456789\nServer={launchd.MT5_EXPECTED_BROKER_SERVER}\n",
            encoding="utf-8",
        )
        shadow_config.chmod(0o600)
        login_reference.chmod(0o600)
        preset.write_text(
            "ShadowMode=true\nReadOnlyMode=true\nEnablePilotAutoTrading=false\n",
            encoding="utf-8",
        )
        terminal.write_bytes(b"test terminal")
        peer_terminal.write_bytes(b"test terminal")
        ea_source.write_text("#property strict\nvoid OnTick() {}\n", encoding="utf-8")
        ea_binary.write_bytes(b"verified shadow ea")
        verified_source.write_bytes(ea_source.read_bytes())
        verified_binary.write_bytes(ea_binary.read_bytes())
        verified_compile_log.write_text(
            "C:\\qg\\QuantGod_MultiStrategy.mq5 : information: compiling "
            "C:\\qg\\QuantGod_MultiStrategy.mq5\n"
            "Result: 0 errors, 0 warnings, 1 ms elapsed\n",
            encoding="utf-8",
        )
        fake_wine.write_text(wine_body, encoding="utf-8")
        fake_wine.chmod(0o700)
        fake_pgrep.write_text(pgrep_body, encoding="utf-8")
        fake_pgrep.chmod(0o700)

        env_values = {
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "QG_BACKEND_ROOT": str(backend),
            "QG_PYTHON_BIN": sys.executable,
            "QG_PRIVATE_ROOT": str(private_root),
            "QG_LAUNCHD_LOG_ROOT": str(log_dir),
            "QG_LAUNCHD_LOCK_ROOT": str(lock_dir),
            "QG_LAUNCHD_STATUS_ROOT": str(status_dir),
            "QG_LOCAL_SQLITE_BACKUP_ROOT": str(backup_dir),
            "QG_EXECUTION_MODE": "SHADOW",
            "QG_MT5_START_MODE": "shadow",
            "QG_MT5_LIVE_LAUNCH_ALLOWED": "0",
            "QG_MT5_SECONDARY_ENABLED": "0",
            "QG_MT5_SECONDARY_SHADOW_ENABLED": "1",
            "QG_MT5_SECONDARY_ALLOW_LIVE_TRADING": "0",
            "QG_ORDER_SEND_ALLOWED": "0",
            "QG_BROKER_EXECUTION_ALLOWED": "0",
            "QG_LIVE_PRESET_MUTATION_ALLOWED": "0",
            "QG_WRITES_MT5_ORDER_REQUEST": "0",
            "QG_TELEGRAM_COMMANDS_ALLOWED": "0",
            "QG_TELEGRAM_PUSH_ALLOWED": "0",
            "QG_AGENT_V25_SEND_TELEGRAM": "0",
            "QG_MT5_AI_DEEPSEEK_ENABLED": "0",
            "QG_DASHBOARD_HOST": "127.0.0.1",
            "QG_MT5_WINE_PREFIX": str(prefix),
            "QG_MT5_ROOT": str(mt5_root),
            "QG_MT5_WINE_BIN": str(fake_wine),
            "QG_MT5_TERMINAL_PATH": str(terminal),
            "QG_MT5_SHADOW_CONFIG": str(shadow_config),
            "QG_MT5_LOGIN_REFERENCE_CONFIG": str(login_reference),
            "QG_MT5_SHADOW_PRESET": str(preset),
            "QG_MT5_SHADOW_CONFIG_WINDOWS": r"C:\qg\QuantGod_MT5_HFM_Shadow_mac.ini",
            "QG_MT5_EXPECTED_SERVER": launchd.MT5_EXPECTED_BROKER_SERVER,
            "QG_MT5_PEER_ROOT": str(peer_root),
            "QG_MT5_PEER_TERMINAL_PATH": str(peer_terminal),
            "QG_MT5_PEER_SHADOW_CONFIG_WINDOWS": r"C:\qg\QuantGod_MT5_HFM_SecondaryShadow_mac.ini",
            "QG_MT5_VERIFIED_EA_SOURCE": str(verified_source),
            "QG_MT5_VERIFIED_EA_BINARY": str(verified_binary),
            "QG_MT5_VERIFIED_EA_COMPILE_LOG": str(verified_compile_log),
            **launchd.local_user_environment(),
            **(extra_env or {}),
        }
        env_path.write_text(
            "\n".join(f"export {key}={launchd.quote_shell(value)}" for key, value in env_values.items()) + "\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(launchd, "PRIVATE_ROOT", private_root),
            mock.patch.object(launchd, "BIN_DIR", bin_dir),
            mock.patch.object(launchd, "LOG_DIR", log_dir),
            mock.patch.object(launchd, "LOCK_DIR", lock_dir),
            mock.patch.object(launchd, "STATUS_DIR", status_dir),
            mock.patch.object(launchd, "BACKUP_DIR", backup_dir),
            mock.patch.object(launchd, "ENV_PATH", env_path),
        ):
            wrapper_text = launchd.render_wrappers()["quantgod-mt5-shadow-supervisor.sh"]
        wrapper_text = wrapper_text.replace(
            '/usr/sbin/lsof -a -p "$pid" -d cwd -Fn 2>/dev/null',
            'printf \'fcwd\\nn%s\\n\' "$QG_MT5_ROOT"',
        ).replace(
            '/bin/ps -p "$pid" -o command= 2>/dev/null',
            'printf \'wine64 terminal64.exe /portable /config:%s\\n\' "$QG_MT5_SHADOW_CONFIG_WINDOWS"',
        )
        wrapper = bin_dir / "quantgod-mt5-shadow-supervisor.sh"
        wrapper.write_text(wrapper_text, encoding="utf-8")
        wrapper.chmod(0o700)
        return {
            "wrapper": wrapper,
            "status": status_dir / "mt5-shadow-supervisor.json",
            "lock": lock_dir / "mt5-shadow-supervisor.lock",
            "verified_compile_log": verified_compile_log,
        }

    def test_rendered_env_uses_split_repo_paths_and_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            paths = {
                "backend": root / "QuantGodBackend",
                "frontend": root / "QuantGodFrontend",
                "infra": root / "QuantGodInfra",
                "docs": root / "QuantGodDocs",
            }
            text = launchd.render_env(paths)
            self.assertIn("QG_BACKEND_ROOT", text)
            self.assertIn("QuantGodBackend", text)
            self.assertIn("QG_DAILY_AUTOPILOT_ALLOW_TESTER_RUN='1'", text)
            self.assertIn("QG_LEGACY_DAILY_AUTOPILOT_ENABLED='0'", text)
            self.assertIn("QG_AGENT_V25_INTERVAL_SECONDS='300'", text)
            self.assertIn("QG_AGENT_V25_SEND_TELEGRAM='0'", text)
            self.assertIn("QG_SQLITE_BACKUP_KEEP='3'", text)
            self.assertIn("QG_AGENT_V25_HEAVY_TELEGRAM_GATEWAY='0'", text)
            self.assertIn("QG_AGENT_OPS_HEALTH_ENABLED='1'", text)
            self.assertIn("QG_PRODUCTION_BURN_IN_ENABLED='1'", text)
            self.assertIn("QG_PRODUCTION_BURN_IN_INTERVAL_SECONDS='300'", text)
            self.assertIn("QG_PRODUCTION_BURN_IN_SAMPLE_INTERVAL_MINUTES='5'", text)
            self.assertIn("QG_PRODUCTION_BURN_IN_WINDOW_HOURS='72'", text)
            self.assertIn("QG_MT5_TERMINAL_PATH", text)
            self.assertIn("QG_MT5_PYTHON_BIN", text)
            self.assertIn("QG_USDJPY_HISTORY_SYNC_ENABLED='1'", text)
            self.assertIn("QG_USDJPY_HISTORY_MONTHS='12'", text)
            self.assertIn("QG_USDJPY_HISTORY_TIMEFRAMES='M1,M5,M15,H1'", text)
            self.assertIn("QG_USDJPY_HISTORY_MAX_LAG_HOURS='96'", text)
            self.assertIn("QG_USDJPY_MT5_SYMBOL='USDJPYc'", text)
            self.assertIn("QG_FOCUS_SYMBOL='USDJPYc'", text)
            self.assertIn("QG_ALLOWED_SYMBOLS='USDJPYc'", text)
            self.assertIn("QG_ACCOUNT_MODE='cent'", text)
            self.assertIn("QG_TELEGRAM_COMMANDS_ALLOWED='0'", text)
            self.assertIn("QG_TELEGRAM_PUSH_ALLOWED='0'", text)
            self.assertIn("QG_MT5_AI_DEEPSEEK_ENABLED='0'", text)
            self.assertIn("QG_EXECUTION_MODE='SHADOW'", text)
            self.assertIn("QG_MT5_START_MODE='shadow'", text)
            self.assertIn("QG_MT5_LIVE_LAUNCH_ALLOWED='0'", text)
            self.assertIn("QG_MT5_SECONDARY_ENABLED='0'", text)
            self.assertIn("QG_MT5_SECONDARY_SHADOW_ENABLED='0'", text)
            self.assertIn("QG_MT5_SECONDARY_ALLOW_LIVE_TRADING='0'", text)
            self.assertIn("QuantGod_MT5_HFM_SecondaryShadow_mac.ini", text)
            self.assertIn(
                f"QG_MT5_EXPECTED_SERVER='{launchd.MT5_EXPECTED_BROKER_SERVER}'",
                text,
            )
            self.assertIn("QG_MT5_LOGIN_REFERENCE_CONFIG", text)
            self.assertIn("QG_ORDER_SEND_ALLOWED='0'", text)
            self.assertIn("QG_BROKER_EXECUTION_ALLOWED='0'", text)
            self.assertIn("QG_LIVE_PRESET_MUTATION_ALLOWED='0'", text)
            user_environment = launchd.local_user_environment()
            for key in ("QG_USER_HOME", "QG_LOCAL_USER", "QG_USER_TMPDIR", "QG_USER_LANG"):
                self.assertIn(f"{key}={launchd.quote_shell(user_environment[key])}", text)
            self.assertNotIn("export QG_MT5_LOGIN=", text)
            self.assertNotIn("export QG_MT5_PASSWORD=", text)
            self.assertNotIn("/QuantGod/", text)

    def test_local_wine_user_environment_is_explicit_and_fail_closed(self) -> None:
        values = launchd.local_user_environment()
        self.assertEqual(launchd.local_user_environment_issues(values), [])
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            actual = root / "actual"
            actual.mkdir()
            linked = root / "linked"
            linked.symlink_to(actual, target_is_directory=True)
            invalid = dict(values)
            invalid["QG_USER_HOME"] = "relative-home"
            invalid["QG_LOCAL_USER"] = values["QG_LOCAL_USER"] + "-mismatch"
            invalid["QG_USER_TMPDIR"] = str(linked)
            invalid["QG_USER_LANG"] = "bad lang;export"
            issues = launchd.local_user_environment_issues(invalid)
        self.assertIn("MT5_USER_HOME_NOT_ABSOLUTE", issues)
        self.assertIn("MT5_LOCAL_USER_MISMATCH", issues)
        self.assertIn("MT5_USER_TMPDIR_SYMLINKED", issues)
        self.assertIn("MT5_USER_LANG_INVALID", issues)

    def test_local_wine_user_environment_rejects_mocked_tmpdir_owner_mismatch(self) -> None:
        values = launchd.local_user_environment()
        tmpdir = pathlib.Path(values["QG_USER_TMPDIR"])
        original_stat = pathlib.Path.stat

        def stat_with_foreign_tmp_owner(path: pathlib.Path, *args, **kwargs):
            metadata = original_stat(path, *args, **kwargs)
            if path == tmpdir:
                fields = list(metadata)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return metadata

        with mock.patch.object(pathlib.Path, "stat", stat_with_foreign_tmp_owner):
            issues = launchd.local_user_environment_issues(values)

        self.assertIn("MT5_USER_TMPDIR_OWNER_MISMATCH", issues)

    def test_daily_autopilot_wrapper_is_explicitly_blocked_on_unsafe_failure_contract(self) -> None:
        wrappers = launchd.render_wrappers()
        daily = wrappers["quantgod-daily-autopilot.sh"]
        self.assertIn("LEGACY_AGENT_FAILURE_CONTRACT_UNSAFE", daily)
        self.assertNotIn("run_mac_agent_v25_loop.sh", daily)
        self.assertNotIn("|| echo", daily)

    def test_wrappers_only_load_secrets_for_services_that_need_them(self) -> None:
        wrappers = launchd.render_wrappers()
        backend = wrappers["quantgod-backend-api.sh"]
        frontend = wrappers["quantgod-frontend-dev.sh"]
        history = wrappers["quantgod-usdjpy-history-sync.sh"]
        daily = wrappers["quantgod-daily-autopilot.sh"]
        monitor = wrappers["quantgod-ai-telegram-monitor.sh"]

        for wrapper in (backend, frontend, history):
            self.assertNotIn('load_env_file "${QG_BACKEND_ROOT}/.env.telegram.local"', wrapper)
            self.assertNotIn('load_env_file "${QG_BACKEND_ROOT}/.env.deepseek.local"', wrapper)
        self.assertNotIn('load_env_file "${QG_BACKEND_ROOT}/.env.local"', frontend)
        self.assertNotIn('load_env_file "${QG_BACKEND_ROOT}/.env.local"', history)
        self.assertNotIn('load_env_file "${QG_BACKEND_ROOT}/.env.telegram.local"', daily)
        self.assertNotIn('load_env_file "${QG_BACKEND_ROOT}/.env.deepseek.local"', daily)
        self.assertIn('load_env_file "${QG_BACKEND_ROOT}/.env.telegram.local"', monitor)
        self.assertIn('load_env_file "${QG_BACKEND_ROOT}/.env.deepseek.local"', monitor)

    def test_default_install_profile_is_core_only(self) -> None:
        parser = launchd.build_parser()
        args = parser.parse_args(["install", "--no-load"])
        self.assertEqual(args.profile, "core")
        self.assertEqual(launchd.SERVICE_PROFILES[args.profile], ("backend-api", "frontend-dev"))
        self.assertNotIn("daily-autopilot", launchd.SERVICE_PROFILES[args.profile])
        self.assertNotIn("ai-telegram-monitor", launchd.SERVICE_PROFILES[args.profile])

    def test_local_shadow_profile_is_complete_and_excludes_dev_and_outbound_services(self) -> None:
        selected = launchd.SERVICE_PROFILES["local-shadow"]
        self.assertEqual(
            selected,
            (
                "frontend-dist-build",
                "backend-api",
                "mt5-shadow-supervisor",
                "usdjpy-history-sync",
                "automation-chain",
                "health-maintenance",
                "log-maintenance",
                "sqlite-backup",
            ),
        )
        self.assertNotIn("frontend-dev", selected)
        self.assertNotIn("daily-autopilot", selected)
        self.assertNotIn("ai-telegram-monitor", selected)

        dual_selected = launchd.SERVICE_PROFILES["local-dual-shadow"]
        self.assertEqual(
            dual_selected,
            (
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
        )
        dual_env = launchd.render_env(
            {
                "backend": pathlib.Path("b"),
                "frontend": pathlib.Path("f"),
                "infra": pathlib.Path("i"),
                "docs": pathlib.Path("d"),
            },
            secondary_shadow_enabled=True,
        )
        self.assertIn("QG_MT5_SECONDARY_SHADOW_ENABLED='1'", dual_env)
        self.assertIn("QG_MT5_SECONDARY_ENABLED='0'", dual_env)

    def test_install_rolls_back_services_loaded_before_bootstrap_failure(self) -> None:
        args = launchd.build_parser().parse_args(["install", "--profile", "core"])
        paths = {
            "backend": pathlib.Path("b"),
            "frontend": pathlib.Path("f"),
            "infra": pathlib.Path("i"),
            "docs": pathlib.Path("d"),
        }
        backend_label = launchd.SERVICES["backend-api"]["label"]
        frontend_label = launchd.SERVICES["frontend-dev"]["label"]
        bootstrap_calls: list[str] = []
        bootout_calls: list[str] = []

        def fake_bootstrap(label: str) -> None:
            bootstrap_calls.append(label)
            if label == frontend_label:
                raise RuntimeError("synthetic bootstrap failure")

        with (
            mock.patch.object(launchd, "load_workspace", return_value=paths),
            mock.patch.object(launchd, "write_files", return_value=paths),
            mock.patch.object(
                launchd,
                "build_capability_report",
                return_value={
                    "services": {
                        name: {"service": name, "ready": True, "status": "READY", "reasonCodes": []}
                        for name in launchd.SERVICES
                    }
                },
            ),
            mock.patch.object(launchd, "is_loaded", side_effect=lambda label: label == backend_label),
            mock.patch.object(launchd, "snapshot_managed_files", return_value={}) as snapshot,
            mock.patch.object(launchd, "restore_managed_files") as restore,
            mock.patch.object(launchd, "bootstrap", side_effect=fake_bootstrap),
            mock.patch.object(launchd, "bootout", side_effect=lambda label, **_kwargs: bootout_calls.append(label)),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic bootstrap failure"):
                launchd.install(args)

        snapshot.assert_called_once_with()
        restore.assert_called_once_with({})
        self.assertEqual(bootstrap_calls, [backend_label, frontend_label, backend_label])
        self.assertGreaterEqual(bootout_calls.count(backend_label), 2)

    def test_local_shadow_build_is_synchronous_before_service_switch(self) -> None:
        args = launchd.build_parser().parse_args(["install", "--profile", "local-shadow"])
        paths = {
            "backend": pathlib.Path("b"),
            "frontend": pathlib.Path("f"),
            "infra": pathlib.Path("i"),
            "docs": pathlib.Path("d"),
        }
        report = {
            "services": {
                name: {"service": name, "ready": True, "status": "READY", "reasonCodes": []}
                for name in launchd.SERVICES
            }
        }
        events: list[str] = []
        with (
            mock.patch.object(launchd, "load_workspace", return_value=paths),
            mock.patch.object(launchd, "build_capability_report", return_value=report),
            mock.patch.object(launchd, "build_frontend_dist", side_effect=lambda *_: events.append("build")),
            mock.patch.object(
                launchd,
                "stage_frontend_dist_rollback",
                side_effect=lambda *_: events.append("stage") or (pathlib.Path("active"), None),
            ),
            mock.patch.object(launchd, "publish_frontend_dist", side_effect=lambda *_: events.append("publish-dist")),
            mock.patch.object(launchd, "discard_frontend_dist_rollback", side_effect=lambda *_: events.append("discard")),
            mock.patch.object(launchd, "is_loaded", return_value=False),
            mock.patch.object(launchd, "snapshot_managed_files", return_value={}),
            mock.patch.object(launchd, "write_files", side_effect=lambda *_args, **_kwargs: events.append("publish") or paths),
            mock.patch.object(launchd, "bootout", side_effect=lambda _label, **_kwargs: events.append("bootout")),
            mock.patch.object(launchd, "bootstrap", side_effect=lambda label: events.append(f"bootstrap:{label}")),
        ):
            self.assertEqual(launchd.install(args), 0)

        backend_event = f"bootstrap:{launchd.SERVICES['backend-api']['label']}"
        self.assertLess(events.index("build"), events.index("bootout"))
        self.assertLess(events.index("bootout"), events.index("stage"))
        self.assertLess(events.index("stage"), events.index("publish-dist"))
        self.assertLess(events.index("publish-dist"), events.index("publish"))
        self.assertLess(events.index("publish"), events.index(backend_event))
        self.assertGreater(events.index("discard"), events.index(backend_event))
        self.assertFalse(any("frontend-dist-build" in event for event in events if event.startswith("bootstrap:")))

    def test_local_shadow_capability_drift_after_build_blocks_before_service_switch(self) -> None:
        args = launchd.build_parser().parse_args(["install", "--profile", "local-shadow"])
        paths = {
            "backend": pathlib.Path("b"),
            "frontend": pathlib.Path("f"),
            "infra": pathlib.Path("i"),
            "docs": pathlib.Path("d"),
        }
        ready = {
            name: {"service": name, "ready": True, "status": "READY", "reasonCodes": []}
            for name in launchd.SERVICES
        }
        drifted = {name: dict(row) for name, row in ready.items()}
        drifted["mt5-shadow-supervisor"] = {
            "service": "mt5-shadow-supervisor",
            "ready": False,
            "status": "BLOCKED",
            "reasonCodes": ["MT5_USER_TMPDIR_OWNER_MISMATCH"],
        }
        reports = [{"services": ready}, {"services": drifted}]
        with (
            mock.patch.object(launchd, "load_workspace", return_value=paths),
            mock.patch.object(launchd, "build_capability_report", side_effect=reports),
            mock.patch.object(launchd, "build_frontend_dist") as build,
            mock.patch.object(launchd, "is_loaded") as is_loaded,
            mock.patch.object(launchd, "snapshot_managed_files") as snapshot,
            mock.patch.object(launchd, "bootout") as bootout,
            mock.patch.object(launchd, "write_files") as write_files,
            mock.patch.object(launchd, "bootstrap") as bootstrap,
        ):
            with self.assertRaisesRegex(RuntimeError, "pre-switch capability preflight is blocked"):
                launchd.install(args)

        build.assert_called_once_with(paths)
        is_loaded.assert_not_called()
        snapshot.assert_not_called()
        bootout.assert_not_called()
        write_files.assert_not_called()
        bootstrap.assert_not_called()

    def test_local_shadow_activation_failure_restores_frontend_dist_and_old_profile(self) -> None:
        args = launchd.build_parser().parse_args(["install", "--profile", "local-shadow"])
        paths = {
            "backend": pathlib.Path("b"),
            "frontend": pathlib.Path("f"),
            "infra": pathlib.Path("i"),
            "docs": pathlib.Path("d"),
        }
        report = {
            "services": {
                name: {"service": name, "ready": True, "status": "READY", "reasonCodes": []}
                for name in launchd.SERVICES
            }
        }
        backend_label = launchd.SERVICES["backend-api"]["label"]
        frontend_snapshot = (pathlib.Path("active"), pathlib.Path("rollback"))
        bootstrap_calls: list[str] = []

        def fake_bootstrap(label: str) -> None:
            bootstrap_calls.append(label)
            if label == backend_label and bootstrap_calls.count(backend_label) == 1:
                raise RuntimeError("synthetic activation failure")

        with (
            mock.patch.object(launchd, "load_workspace", return_value=paths),
            mock.patch.object(launchd, "build_capability_report", return_value=report),
            mock.patch.object(launchd, "build_frontend_dist"),
            mock.patch.object(launchd, "stage_frontend_dist_rollback", return_value=frontend_snapshot),
            mock.patch.object(launchd, "publish_frontend_dist"),
            mock.patch.object(launchd, "discard_frontend_dist_rollback") as discard,
            mock.patch.object(launchd, "restore_frontend_dist") as restore_frontend,
            mock.patch.object(launchd, "is_loaded", side_effect=lambda label: label == backend_label),
            mock.patch.object(launchd, "snapshot_managed_files", return_value={}),
            mock.patch.object(launchd, "restore_managed_files") as restore_files,
            mock.patch.object(launchd, "write_files", return_value=paths),
            mock.patch.object(launchd, "bootout"),
            mock.patch.object(launchd, "bootstrap", side_effect=fake_bootstrap),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic activation failure"):
                launchd.install(args)

        restore_frontend.assert_called_once_with(frontend_snapshot)
        restore_files.assert_called_once_with({})
        discard.assert_not_called()
        self.assertEqual(bootstrap_calls, [backend_label, backend_label])

    def test_missing_compiled_frontend_is_buildable_before_final_preflight(self) -> None:
        selected = launchd.SERVICE_PROFILES["local-shadow"]
        capabilities = {
            name: {"service": name, "ready": True, "status": "READY", "reasonCodes": []}
            for name in launchd.SERVICES
        }
        capabilities["backend-api"] = {
            "service": "backend-api",
            "ready": False,
            "status": "BLOCKED",
            "reasonCodes": ["COMPILED_FRONTEND_MISSING"],
        }
        self.assertEqual(
            launchd.selected_capability_blockers(
                capabilities,
                selected,
                allow_frontend_build_output=True,
            ),
            [],
        )
        self.assertEqual(
            [row["service"] for row in launchd.selected_capability_blockers(capabilities, selected)],
            ["backend-api"],
        )

    def test_history_sync_wrapper_runs_sync_klines_once(self) -> None:
        wrappers = launchd.render_wrappers()
        history = wrappers["quantgod-usdjpy-history-sync.sh"]
        self.assertIn("tools/run_usdjpy_strategy_backtest.py", history)
        self.assertIn("sync-klines", history)
        self.assertIn(" quality", history)
        self.assertNotIn("run_mac_usdjpy_history_sync_loop.sh", history)
        self.assertNotIn("|| echo", history)
        self.assertIn("QG_BACKEND_ROOT", history)

    def test_log_maintenance_wrapper_covers_three_bounded_roots_with_explicit_caps(self) -> None:
        wrapper = launchd.render_wrappers()["quantgod-log-maintenance.sh"]
        self.assertIn('backend_runtime="$QG_BACKEND_ROOT/runtime"', wrapper)
        self.assertIn('maintain_runtime_target "$backend_runtime_real"', wrapper)
        self.assertIn('if [[ "$runtime_real" != "$backend_runtime_real" ]]', wrapper)
        self.assertIn('maintain_runtime_target "$runtime_real"', wrapper)
        self.assertIn('--runtime-root "$launchd_log_real"', wrapper)
        self.assertIn('[[ "$runtime_real" == "$mt5_files_real" ]]', wrapper)
        self.assertIn("LOG_MAINTENANCE_ROOT_COLLISION", wrapper)
        self.assertIn("assert_path_has_no_symlink_components", wrapper)
        for key in (
            "QG_RUNTIME_LOG_MAX_MB",
            "QG_RUNTIME_LOG_ARCHIVE_MAX_MB",
            "QG_RUNTIME_LOG_RETENTION_DAYS",
            "QG_RUNTIME_JSONL_MAX_MB",
            "QG_RUNTIME_JSONL_ARCHIVE_MAX_MB",
            "QG_RUNTIME_JSONL_KEEP_LINES",
        ):
            self.assertIn(f'require_positive_integer_env "${{key_code%%:*}}"', wrapper)
            self.assertIn(key, wrapper)
        self.assertEqual(wrapper.count('--max-active-mb "$QG_RUNTIME_LOG_MAX_MB"'), 2)
        self.assertEqual(wrapper.count('--archive-max-mb "$QG_RUNTIME_LOG_ARCHIVE_MAX_MB"'), 2)
        self.assertEqual(wrapper.count('--retention-days "$QG_RUNTIME_LOG_RETENTION_DAYS"'), 2)
        self.assertEqual(wrapper.count('--max-jsonl-mb "$QG_RUNTIME_JSONL_MAX_MB"'), 1)
        self.assertEqual(wrapper.count("--no-jsonl-maintenance"), 1)

    def test_local_shadow_wrappers_are_singleton_and_fail_closed(self) -> None:
        wrappers = launchd.render_wrappers()
        for name in launchd.SERVICE_PROFILES["local-shadow"]:
            wrapper = wrappers[launchd.SERVICES[name]["wrapper"]]
            self.assertIn("enforce_shadow_contract", wrapper)
            self.assertIn("acquire_singleton_lock", wrapper)
            self.assertNotIn("|| echo", wrapper)

        mt5 = wrappers["quantgod-mt5-shadow-supervisor.sh"]
        self.assertIn("QuantGod_MT5_HFM_Shadow_mac.ini", mt5)
        self.assertIn("QuantGod_MT5_LoginOnly_mac.ini", mt5)
        self.assertIn(
            f'''[[ "$QG_MT5_EXPECTED_SERVER" == '{launchd.MT5_EXPECTED_BROKER_SERVER}' ]]''',
            mt5,
        )
        self.assertIn(
            'assert_file_value_exact "$QG_MT5_SHADOW_CONFIG" "Server" "$QG_MT5_EXPECTED_SERVER"',
            mt5,
        )
        self.assertIn("MT5_SHADOW_SERVER_GUARD_FAILED", mt5)
        self.assertIn(
            'assert_private_login_match "$QG_MT5_SHADOW_CONFIG" "$QG_MT5_LOGIN_REFERENCE_CONFIG"',
            mt5,
        )
        self.assertIn("MT5_SHADOW_LOGIN_GUARD_FAILED", mt5)
        self.assertIn('assert_file_value "$QG_MT5_SHADOW_CONFIG" "AllowLiveTrading" "0"', mt5)
        self.assertIn('assert_file_value "$QG_MT5_SHADOW_PRESET" "ShadowMode" "true"', mt5)
        self.assertIn("EXISTING_MT5_PROCESS", mt5)
        self.assertIn("MT5_PROCESS_QUERY_FAILED", mt5)
        self.assertIn("SINGLETON_IDENTITY_QUERY_FAILED", mt5)
        self.assertIn("SINGLETON_IDENTITY_INCOMPLETE", mt5)
        self.assertIn('"$lock_path/started"', mt5)
        self.assertIn("prepare_wine_user_environment", mt5)
        self.assertIn("WINE_USER_ENV_GUARD_FAILED", mt5)
        self.assertIn("pwd.getpwuid(os.getuid())", mt5)
        self.assertIn("path.is_absolute()", mt5)
        self.assertIn("path.is_symlink()", mt5)
        self.assertIn("metadata.st_uid != os.getuid()", mt5)
        self.assertIn('export HOME="$QG_USER_HOME"', mt5)
        self.assertIn('export USER="$QG_LOCAL_USER"', mt5)
        self.assertIn('export LOGNAME="$QG_LOCAL_USER"', mt5)
        self.assertIn('export TMPDIR="$QG_USER_TMPDIR"', mt5)
        self.assertIn('export LANG="$QG_USER_LANG"', mt5)
        self.assertLess(mt5.index("MT5_SHADOW_SERVER_GUARD_FAILED"), mt5.index("EXISTING_MT5_PROCESS"))
        self.assertLess(mt5.index("MT5_SHADOW_LOGIN_GUARD_FAILED"), mt5.index("EXISTING_MT5_PROCESS"))
        self.assertIn("QG_SERVICE_CHILD_PID", mt5)
        self.assertIn("supervisedPid", mt5)
        self.assertIn("trap 'forward_mt5_signal TERM' TERM", mt5)
        self.assertIn("trap 'forward_mt5_signal INT' INT", mt5)
        self.assertIn('wait "$QG_SERVICE_CHILD_PID"', mt5)
        self.assertIn("MT5_SHADOW_EXITED", mt5)
        self.assertIn("MT5_SHADOW_EXIT_NONZERO", mt5)
        self.assertIn("MT5_SHADOW_STOPPING", mt5)
        self.assertIn("MT5_CHILD_DETACHED", mt5)
        self.assertIn(launchd.MT5_TERMINAL_PROCESS_PATTERN, mt5)
        self.assertIn('existing_mt5="$(find_mt5_terminal_pids_for_root)"', mt5)
        self.assertIn('detached_mt5="$(find_mt5_terminal_pids_for_root)"', mt5)
        self.assertIn("MT5_SHADOW_EA_PROVENANCE_INVALID", mt5)
        self.assertIn("MT5_TERMINAL_BINARY_MISMATCH", mt5)
        self.assertIn("UNREVIEWED_MT5_PROCESS", mt5)
        self.assertNotIn("pgrep -f '[t]erminal64\\.exe'", mt5)
        self.assertNotIn('exec "$QG_MT5_WINE_BIN"', mt5)
        self.assertNotIn("pkill", mt5)
        self.assertLess(
            mt5.index("prepare_wine_user_environment"),
            mt5.index('"$QG_MT5_WINE_BIN" terminal64.exe'),
        )
        self.assertNotIn("LivePilot", mt5)

        secondary_mt5 = wrappers["quantgod-mt5-secondary-shadow-supervisor.sh"]
        self.assertIn("QuantGod_MT5_HFM_SecondaryShadow_mac.ini", secondary_mt5)
        self.assertIn(launchd.MT5_SECONDARY_EXPECTED_BROKER_SERVER, secondary_mt5)
        self.assertNotIn(launchd.MT5_EXPECTED_BROKER_SERVER, secondary_mt5)
        self.assertIn('QG_SERVICE_NAME="mt5-secondary-shadow-supervisor"', secondary_mt5)

        automation = wrappers["quantgod-automation-chain.sh"]
        self.assertIn("did not return a non-empty step list", automation)
        self.assertIn("did not return any required steps", automation)
        self.assertIn("missing a name/id identity", automation)
        self.assertIn("conflicting name/id identities", automation)
        self.assertIn("duplicate required step identities", automation)
        self.assertIn("unknown required step identities", automation)
        self.assertIn("required step identity set is incomplete", automation)
        for step_id in (
            "adaptive_policy",
            "dynamic_sltp",
            "entry_trigger",
            "usdjpy_strategy_policy",
            "usdjpy_ea_dry_run",
            "usdjpy_live_loop",
        ):
            self.assertIn(f'"{step_id}"', automation)
        self.assertIn("required automation steps failed", automation)
        self.assertIn("runStatus=COMPLETED", automation)
        self.assertIn("required-step counters are inconsistent", automation)
        self.assertNotIn("--send", automation)

        frontend = wrappers["quantgod-frontend-dist-build.sh"]
        self.assertIn('"$QG_NPM_BIN" run build', frontend)
        self.assertIn("sync-frontend-dist", frontend)
        self.assertIn('scripts/qg-workspace.py --workspace "$QG_WORKSPACE_FILE" sync-frontend-dist', frontend)

        backup = wrappers["quantgod-sqlite-backup.sh"]
        self.assertIn("tools/run_local_shadow_backup.py", backup)
        self.assertIn("quantgod.local_shadow_backup_verification.v1", backup)
        self.assertIn("backup verification did not confirm ok=true", backup)
        self.assertIn("verified_identity", backup)
        self.assertIn("backupId", backup)
        self.assertIn("assert_path_has_no_symlink_components", backup)
        self.assertIn('finish_service "PASS"', backup)

        health = wrappers["quantgod-health-maintenance.sh"]
        self.assertIn('fetch("/healthz")', health)
        self.assertIn('fetch("/readyz")', health)
        self.assertIn('fetch("/api/operator/overview")', health)
        self.assertIn("SHADOW_READONLY", health)
        self.assertIn("finish_service_observed", health)
        self.assertIn("observedReadiness", health)

    def test_automation_validator_accepts_backend_name_contract_and_legacy_id(self) -> None:
        for identity_field in ("name", "id"):
            with self.subTest(identity_field=identity_field):
                result = self._validate_automation_payload(
                    self._automation_payload(identity_field=identity_field)
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_automation_validator_rejects_missing_duplicate_unknown_and_conflicting_identity(self) -> None:
        cases = []

        missing_identity = self._automation_payload()
        del missing_identity["steps"][0]["name"]
        cases.append((missing_identity, "missing a name/id identity"))

        duplicate_identity = self._automation_payload()
        duplicate_identity["steps"][1]["name"] = duplicate_identity["steps"][0]["name"]
        cases.append((duplicate_identity, "duplicate required step identities"))

        unknown_identity = self._automation_payload()
        unknown_identity["steps"][0]["name"] = "unreviewed_required_step"
        cases.append((unknown_identity, "unknown required step identities"))

        missing_expected_identity = self._automation_payload()
        missing_expected_identity["steps"].pop()
        missing_expected_identity["requiredStepCount"] -= 1
        cases.append((missing_expected_identity, "required step identity set is incomplete"))

        conflicting_identity = self._automation_payload()
        conflicting_identity["steps"][0]["id"] = "dynamic_sltp"
        cases.append((conflicting_identity, "conflicting name/id identities"))

        for payload, error in cases:
            with self.subTest(error=error):
                result = self._validate_automation_payload(payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)

    def test_mt5_supervisor_records_terminal_child_exit_states(self) -> None:
        for child_rc, expected_state, expected_code in (
            (0, "STOPPED", "MT5_SHADOW_EXITED"),
            (23, "FAILED", "MT5_SHADOW_EXIT_NONZERO"),
        ):
            with self.subTest(child_rc=child_rc), tempfile.TemporaryDirectory() as tmp:
                fixture = self._create_mt5_wrapper_fixture(
                    pathlib.Path(tmp),
                    f"#!/bin/bash\nexit {child_rc}\n",
                )
                result = subprocess.run(
                    ["/bin/bash", str(fixture["wrapper"])],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, child_rc, msg=result.stderr)
                status = json.loads(fixture["status"].read_text(encoding="utf-8"))
                self.assertEqual(status["taskStatus"], expected_state)
                self.assertEqual(status["code"], expected_code)
                self.assertIsNone(status["supervisedPid"])
                self.assertNotEqual(status["taskStatus"], "RUNNING")
                self.assertFalse(fixture["lock"].exists())

    def test_singleton_empty_lock_fails_closed_without_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._create_mt5_wrapper_fixture(
                pathlib.Path(tmp),
                "#!/bin/bash\nexit 0\n",
            )
            fixture["lock"].mkdir()
            result = subprocess.run(
                ["/bin/bash", str(fixture["wrapper"])],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 78)
            status = json.loads(fixture["status"].read_text(encoding="utf-8"))
            self.assertEqual(status["taskStatus"], "BLOCKED")
            self.assertEqual(status["code"], "SINGLETON_IDENTITY_INCOMPLETE")
            self.assertTrue(fixture["lock"].is_dir())
            self.assertEqual(list(fixture["lock"].iterdir()), [])

    def test_singleton_live_partial_identity_fails_closed_without_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._create_mt5_wrapper_fixture(
                pathlib.Path(tmp),
                "#!/bin/bash\nexit 0\n",
            )
            fixture["lock"].mkdir()
            owner_path = fixture["lock"] / "pid"
            owner_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            result = subprocess.run(
                ["/bin/bash", str(fixture["wrapper"])],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 78)
            status = json.loads(fixture["status"].read_text(encoding="utf-8"))
            self.assertEqual(status["taskStatus"], "BLOCKED")
            self.assertEqual(status["code"], "SINGLETON_IDENTITY_INCOMPLETE")
            self.assertTrue(fixture["lock"].is_dir())
            self.assertEqual(owner_path.read_text(encoding="utf-8").strip(), str(os.getpid()))

    def test_mt5_terminal_pattern_distinguishes_wine_from_terminal_path_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            wine_script = root / "wine64-preloader"
            wine_script.write_text(
                "#!/bin/bash\ntrap 'exit 0' TERM INT\nwhile :; do /bin/sleep 0.05; done\n",
                encoding="utf-8",
            )
            wine_script.chmod(0o700)
            history_process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(5)",
                    "--terminal-path",
                    str(root / "terminal64.exe"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wine_process = subprocess.Popen(
                [str(wine_script), "terminal64.exe", "/portable"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                matched_pids: set[int] = set()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    result = subprocess.run(
                        ["pgrep", "-f", launchd.MT5_TERMINAL_PROCESS_PATTERN],
                        capture_output=True,
                        text=True,
                    )
                    matched_pids = {
                        int(row) for row in result.stdout.splitlines() if row.strip().isdigit()
                    }
                    if wine_process.pid in matched_pids:
                        break
                    time.sleep(0.02)
                self.assertIn(wine_process.pid, matched_pids)
                self.assertNotIn(history_process.pid, matched_pids)
            finally:
                for process in (history_process, wine_process):
                    if process.poll() is None:
                        process.terminate()
                for process in (history_process, wine_process):
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)

    def test_history_terminal_path_argument_is_ignored_by_both_mt5_process_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._create_mt5_wrapper_fixture(
                pathlib.Path(tmp),
                "#!/bin/bash\nexit 0\n",
                extra_env={
                    "QG_FAKE_EXPECTED_MT5_PATTERN": launchd.MT5_TERMINAL_PROCESS_PATTERN,
                },
                pgrep_body=(
                    "#!/bin/bash\n"
                    "if [[ \"${2-}\" == \"$QG_FAKE_EXPECTED_MT5_PATTERN\" ]]; then exit 1; fi\n"
                    "printf '%s\\n' 77777\n"
                    "exit 0\n"
                ),
            )
            result = subprocess.run(
                ["/bin/bash", str(fixture["wrapper"])],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            status = json.loads(fixture["status"].read_text(encoding="utf-8"))
            self.assertEqual(status["taskStatus"], "STOPPED")
            self.assertEqual(status["code"], "MT5_SHADOW_EXITED")

    def test_real_wine_terminal_match_blocks_duplicate_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._create_mt5_wrapper_fixture(
                pathlib.Path(tmp),
                "#!/bin/bash\nexit 0\n",
                extra_env={
                    "QG_FAKE_EXPECTED_MT5_PATTERN": launchd.MT5_TERMINAL_PROCESS_PATTERN,
                },
                pgrep_body=(
                    "#!/bin/bash\n"
                    "[[ \"${2-}\" == \"$QG_FAKE_EXPECTED_MT5_PATTERN\" ]] || exit 2\n"
                    "printf '%s\\n' 77777\n"
                    "exit 0\n"
                ),
            )
            result = subprocess.run(
                ["/bin/bash", str(fixture["wrapper"])],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 78)
            status = json.loads(fixture["status"].read_text(encoding="utf-8"))
            self.assertEqual(status["taskStatus"], "BLOCKED")
            self.assertEqual(status["code"], "EXISTING_MT5_PROCESS")

    def test_mt5_supervisor_rejects_stale_trade_capable_compile_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._create_mt5_wrapper_fixture(
                pathlib.Path(tmp),
                "#!/bin/bash\nexit 0\n",
            )
            fixture["verified_compile_log"].write_text(
                "C:\\qg\\QuantGod_MultiStrategy.mq5 : information: compiling "
                "C:\\qg\\QuantGod_MultiStrategy.mq5\n"
                "unsafe_surface=Trade/Trade.mqh\n"
                "Result: 0 errors, 0 warnings, 1 ms elapsed\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["/bin/bash", str(fixture["wrapper"])],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 78)
            status = json.loads(fixture["status"].read_text(encoding="utf-8"))
            self.assertEqual(status["taskStatus"], "BLOCKED")
            self.assertEqual(status["code"], "MT5_SHADOW_EA_PROVENANCE_INVALID")

    def test_real_wine_terminal_match_detects_detached_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pgrep_state = root / "pgrep-state"
            fixture = self._create_mt5_wrapper_fixture(
                root,
                "#!/bin/bash\nexit 0\n",
                extra_env={
                    "QG_FAKE_EXPECTED_MT5_PATTERN": launchd.MT5_TERMINAL_PROCESS_PATTERN,
                    "QG_FAKE_PGREP_STATE": str(pgrep_state),
                },
                pgrep_body=(
                    "#!/bin/bash\n"
                    "[[ \"${2-}\" == \"$QG_FAKE_EXPECTED_MT5_PATTERN\" ]] || exit 2\n"
                    "count=0\n"
                    "[[ -f \"$QG_FAKE_PGREP_STATE\" ]] && count=\"$(<\"$QG_FAKE_PGREP_STATE\")\"\n"
                    "count=$((count + 1))\n"
                    "printf '%s\\n' \"$count\" > \"$QG_FAKE_PGREP_STATE\"\n"
                    "[[ \"$count\" -eq 1 ]] && exit 1\n"
                    "printf '%s\\n' 77777\n"
                    "exit 0\n"
                ),
            )
            result = subprocess.run(
                ["/bin/bash", str(fixture["wrapper"])],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 70, msg=result.stderr)
            status = json.loads(fixture["status"].read_text(encoding="utf-8"))
            self.assertEqual(status["taskStatus"], "FAILED")
            self.assertEqual(status["code"], "MT5_CHILD_DETACHED")
            self.assertEqual(pgrep_state.read_text(encoding="utf-8").strip(), "2")

    def test_mt5_supervisor_forwards_term_and_records_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            signal_marker = root / "signal-marker"
            fixture = self._create_mt5_wrapper_fixture(
                root,
                "#!/bin/bash\n"
                "trap 'printf TERM > \"$QG_FAKE_SIGNAL_MARKER\"; /bin/sleep 0.3; exit 0' TERM\n"
                "while :; do /bin/sleep 0.05; done\n",
                extra_env={"QG_FAKE_SIGNAL_MARKER": str(signal_marker)},
            )
            process = subprocess.Popen(
                ["/bin/bash", str(fixture["wrapper"])],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            running_status: dict[str, object] | None = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if fixture["status"].is_file():
                    candidate = json.loads(fixture["status"].read_text(encoding="utf-8"))
                    if candidate.get("taskStatus") == "RUNNING":
                        running_status = candidate
                        break
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(running_status)
            child_pid = int(running_status["supervisedPid"])
            process.terminate()
            stopping_status: dict[str, object] | None = None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                candidate = json.loads(fixture["status"].read_text(encoding="utf-8"))
                if candidate.get("taskStatus") == "STOPPING":
                    stopping_status = candidate
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(stopping_status)
            self.assertEqual(stopping_status["supervisedPid"], child_pid)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, msg=f"stdout={stdout}\nstderr={stderr}")
            status = json.loads(fixture["status"].read_text(encoding="utf-8"))
            self.assertEqual(status["taskStatus"], "STOPPED")
            self.assertEqual(status["code"], "MT5_SHADOW_SIGNALLED")
            self.assertIsNone(status["supervisedPid"])
            self.assertEqual(signal_marker.read_text(encoding="utf-8"), "TERM")
            self.assertFalse(fixture["lock"].exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_all_rendered_wrappers_pass_bash_syntax_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in launchd.render_wrappers().items():
                path = pathlib.Path(tmp) / name
                path.write_text(content, encoding="utf-8")
                result = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, msg=f"{name}: {result.stderr}")

    def test_all_embedded_python_blocks_compile(self) -> None:
        for name, content in launchd.render_wrappers().items():
            blocks = re.findall(r"<<'PY'\n(.*?)\nPY", content, flags=re.DOTALL)
            for index, block in enumerate(blocks):
                compile(block, f"{name}:python-block-{index}", "exec")

    def test_shadow_preset_guard_requires_exact_readonly_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = root / "QuantGod_MT5_HFM_Shadow_mac.ini"
            login_reference = root / "QuantGod_MT5_LoginOnly_mac.ini"
            preset = root / "QuantGod_MT5_HFM_Shadow.set"
            config.write_text(
                f"[Common]\nLogin=123456789\nServer={launchd.MT5_EXPECTED_BROKER_SERVER}\n"
                "[Experts]\nAllowLiveTrading=0\nAllowDllImport=0\n"
                "[StartUp]\nExpertParameters=QuantGod_MT5_HFM_Shadow.set\n",
                encoding="utf-8",
            )
            login_reference.write_text(
                f"[Common]\nLogin=123456789\nServer={launchd.MT5_EXPECTED_BROKER_SERVER}\n",
                encoding="utf-8",
            )
            preset.write_text(
                "ShadowMode=true\nReadOnlyMode=true\nEnablePilotAutoTrading=false\n",
                encoding="utf-8",
            )
            self.assertEqual(launchd.shadow_preset_guard_issues(config, preset), [])
            preset.write_text(
                "ShadowMode=false\nReadOnlyMode=true\nEnablePilotAutoTrading=false\nOrderSendAllowed=true\n",
                encoding="utf-8",
            )
            issues = launchd.shadow_preset_guard_issues(config, preset)
            self.assertIn("MT5_SHADOW_PRESET_SHADOWMODE_INVALID", issues)
            self.assertIn("MT5_SHADOW_PRESET_ORDERSENDALLOWED_FORBIDDEN", issues)
            preset.write_text(
                "ShadowMode=false\nShadowMode=true\nReadOnlyMode=true\n"
                "EnablePilotAutoTrading=false\nOrderSendAllowed=true\nOrderSendAllowed=false\n",
                encoding="utf-8",
            )
            issues = launchd.shadow_preset_guard_issues(config, preset)
            self.assertIn("MT5_SHADOW_PRESET_SHADOWMODE_INVALID", issues)
            self.assertIn("MT5_SHADOW_PRESET_ORDERSENDALLOWED_DUPLICATE", issues)
            self.assertIn("MT5_SHADOW_PRESET_ORDERSENDALLOWED_FORBIDDEN", issues)
            config.write_text(
                "[Experts]\nAllowLiveTrading=0\nAllowDllImport=0\n"
                "EnableLiveTrading=true\nEnableLiveTrading=false\n"
                "[StartUp]\nExpertParameters=QuantGod_MT5_HFM_Shadow.set\n",
                encoding="utf-8",
            )
            issues = launchd.shadow_preset_guard_issues(config, preset)
            self.assertIn("MT5_SHADOW_CONFIG_ENABLELIVETRADING_DUPLICATE", issues)
            self.assertIn("MT5_SHADOW_CONFIG_ENABLELIVETRADING_FORBIDDEN", issues)

    def test_shadow_preset_guard_requires_one_exact_hfm_broker_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = root / "QuantGod_MT5_HFM_Shadow_mac.ini"
            login_reference = root / "QuantGod_MT5_LoginOnly_mac.ini"
            preset = root / "QuantGod_MT5_HFM_Shadow.set"
            preset.write_text(
                "ShadowMode=true\nReadOnlyMode=true\nEnablePilotAutoTrading=false\n",
                encoding="utf-8",
            )
            login_reference.write_text(
                f"[Common]\nLogin=123456789\nServer={launchd.MT5_EXPECTED_BROKER_SERVER}\n",
                encoding="utf-8",
            )

            def write_config(server_rows: str) -> None:
                config.write_text(
                    f"[Common]\nLogin=123456789\n{server_rows}"
                    "[Experts]\nAllowLiveTrading=0\nAllowDllImport=0\n"
                    "[StartUp]\nExpertParameters=QuantGod_MT5_HFM_Shadow.set\n",
                    encoding="utf-8",
                )

            write_config(f"Server={launchd.MT5_EXPECTED_BROKER_SERVER}\n")
            self.assertNotIn(
                "MT5_SHADOW_CONFIG_SERVER_INVALID",
                launchd.shadow_preset_guard_issues(config, preset),
            )
            for invalid_server_rows in (
                "",
                "Server=SyntheticBroker-Demo\n",
                f"Server={launchd.MT5_EXPECTED_BROKER_SERVER.lower()}\n",
                f"Server={launchd.MT5_EXPECTED_BROKER_SERVER}\n"
                f"Server={launchd.MT5_EXPECTED_BROKER_SERVER}\n",
            ):
                with self.subTest(server_rows=invalid_server_rows):
                    write_config(invalid_server_rows)
                    self.assertIn(
                        "MT5_SHADOW_CONFIG_SERVER_INVALID",
                        launchd.shadow_preset_guard_issues(config, preset),
                    )

    def test_shadow_guard_silently_matches_private_login_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = root / "QuantGod_MT5_HFM_Shadow_mac.ini"
            login_reference = root / "QuantGod_MT5_LoginOnly_mac.ini"
            preset = root / "QuantGod_MT5_HFM_Shadow.set"
            preset.write_text(
                "ShadowMode=true\nReadOnlyMode=true\nEnablePilotAutoTrading=false\n",
                encoding="utf-8",
            )

            def write_config(login_rows: str) -> None:
                config.write_text(
                    f"[Common]\n{login_rows}Server={launchd.MT5_EXPECTED_BROKER_SERVER}\n"
                    "[Experts]\nAllowLiveTrading=0\nAllowDllImport=0\n"
                    "[StartUp]\nExpertParameters=QuantGod_MT5_HFM_Shadow.set\n",
                    encoding="utf-8",
                )

            login_reference.write_text(
                f"[Common]\nLogin=123456789\nServer={launchd.MT5_EXPECTED_BROKER_SERVER}\n",
                encoding="utf-8",
            )
            write_config("Login=123456789\n")
            self.assertEqual(launchd.shadow_preset_guard_issues(config, preset), [])

            write_config("Login=90000001\n")
            issues = launchd.shadow_preset_guard_issues(config, preset)
            self.assertIn("MT5_SHADOW_LOGIN_MISMATCH", issues)
            self.assertNotIn("123456789", " ".join(issues))
            self.assertNotIn("90000001", " ".join(issues))

            for invalid_login_rows in ("", "Login=not-a-login\n", "Login=1\nLogin=2\n"):
                with self.subTest(login_rows=invalid_login_rows):
                    write_config(invalid_login_rows)
                    self.assertIn(
                        "MT5_SHADOW_CONFIG_LOGIN_INVALID",
                        launchd.shadow_preset_guard_issues(config, preset),
                    )

            login_reference.write_text(
                "[Common]\nLogin=123456789\nServer=SyntheticBroker-Demo\n",
                encoding="utf-8",
            )
            write_config("Login=123456789\n")
            self.assertIn(
                "MT5_LOGIN_REFERENCE_SERVER_INVALID",
                launchd.shadow_preset_guard_issues(config, preset),
            )
            login_reference.unlink()
            self.assertIn(
                "MT5_LOGIN_REFERENCE_MISSING",
                launchd.shadow_preset_guard_issues(config, preset),
            )

    def test_daily_autopilot_capability_is_always_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            paths = {
                "backend": root / "QuantGodBackend",
                "frontend": root / "QuantGodFrontend",
                "infra": root / "QuantGodInfra",
                "docs": root / "QuantGodDocs",
            }
            capability = launchd.service_capability("daily-autopilot", paths)
        self.assertFalse(capability["ready"])
        self.assertEqual(capability["status"], "BLOCKED")
        self.assertEqual(capability["reasonCodes"], ["LEGACY_AGENT_FAILURE_CONTRACT_UNSAFE"])

    def test_no_load_never_changes_launchd_state(self) -> None:
        args = launchd.build_parser().parse_args(["install", "--profile", "local-shadow", "--no-load"])
        paths = {
            "backend": pathlib.Path("b"),
            "frontend": pathlib.Path("f"),
            "infra": pathlib.Path("i"),
            "docs": pathlib.Path("d"),
        }
        report = {
            "services": {
                name: {"service": name, "ready": False, "status": "BLOCKED", "reasonCodes": ["MISSING"]}
                for name in launchd.SERVICES
            }
        }
        with (
            mock.patch.object(launchd, "load_workspace", return_value=paths),
            mock.patch.object(launchd, "write_files") as write_files,
            mock.patch.object(launchd, "build_capability_report", return_value=report),
            mock.patch.object(launchd, "bootout") as bootout,
            mock.patch.object(launchd, "bootstrap") as bootstrap,
            mock.patch.object(launchd, "snapshot_managed_files") as snapshot,
        ):
            self.assertEqual(launchd.install(args), 2)
        write_files.assert_not_called()
        bootout.assert_not_called()
        bootstrap.assert_not_called()
        snapshot.assert_not_called()

    def test_no_load_reports_buildable_frontend_and_returns_ready(self) -> None:
        args = launchd.build_parser().parse_args(["install", "--profile", "local-shadow", "--no-load"])
        paths = {
            "backend": pathlib.Path("b"),
            "frontend": pathlib.Path("f"),
            "infra": pathlib.Path("i"),
            "docs": pathlib.Path("d"),
        }
        report = {
            "services": {
                name: {"service": name, "ready": True, "status": "READY", "reasonCodes": []}
                for name in launchd.SERVICES
            }
        }
        report["services"]["backend-api"] = {
            "service": "backend-api",
            "ready": False,
            "status": "BLOCKED",
            "reasonCodes": ["COMPILED_FRONTEND_MISSING"],
        }
        with (
            mock.patch.object(launchd, "load_workspace", return_value=paths),
            mock.patch.object(launchd, "build_capability_report", return_value=report),
            mock.patch.object(launchd, "write_files") as write_files,
            mock.patch.object(launchd, "build_frontend_dist") as build,
            mock.patch("builtins.print") as printer,
        ):
            self.assertEqual(launchd.install(args), 0)
        rendered = "\n".join(" ".join(str(arg) for arg in call.args) for call in printer.call_args_list)
        self.assertIn("backend-api: READY_AFTER_BUILD", rendered)
        self.assertIn("Preflight READY", rendered)
        write_files.assert_not_called()
        build.assert_not_called()

    def test_blocked_profile_refuses_to_change_launchd_state(self) -> None:
        args = launchd.build_parser().parse_args(["install", "--profile", "local-shadow"])
        paths = {
            "backend": pathlib.Path("b"),
            "frontend": pathlib.Path("f"),
            "infra": pathlib.Path("i"),
            "docs": pathlib.Path("d"),
        }
        report = {
            "services": {
                name: {
                    "service": name,
                    "ready": name != "mt5-shadow-supervisor",
                    "status": "BLOCKED" if name == "mt5-shadow-supervisor" else "READY",
                    "reasonCodes": ["MT5_SHADOW_PRESET_MISSING"] if name == "mt5-shadow-supervisor" else [],
                }
                for name in launchd.SERVICES
            }
        }
        with (
            mock.patch.object(launchd, "load_workspace", return_value=paths),
            mock.patch.object(launchd, "write_files") as write_files,
            mock.patch.object(launchd, "build_capability_report", return_value=report),
            mock.patch.object(launchd, "bootout") as bootout,
            mock.patch.object(launchd, "bootstrap") as bootstrap,
        ):
            with self.assertRaisesRegex(RuntimeError, "blocked capabilities"):
                launchd.install(args)
        write_files.assert_not_called()
        bootout.assert_not_called()
        bootstrap.assert_not_called()

    def test_managed_file_publication_is_atomic(self) -> None:
        source = inspect.getsource(launchd.atomic_write_bytes)
        self.assertIn("os.O_EXCL", source)
        self.assertIn("os.fsync", source)
        self.assertIn("os.replace", source)

    def test_launchd_state_query_and_bootout_fail_closed(self) -> None:
        label = launchd.SERVICES["backend-api"]["label"]
        not_loaded = subprocess.CompletedProcess(
            ["launchctl"],
            113,
            stdout="",
            stderr="Could not find service in domain for user",
        )
        query_error = subprocess.CompletedProcess(
            ["launchctl"],
            1,
            stdout="",
            stderr="Operation not permitted",
        )
        loaded = subprocess.CompletedProcess(["launchctl"], 0, stdout="state = running", stderr="")
        with mock.patch.object(launchd, "run_launchctl", return_value=not_loaded):
            self.assertEqual(launchd.launchd_load_state(label), "not-loaded")
        with mock.patch.object(launchd, "run_launchctl", return_value=query_error):
            with self.assertRaisesRegex(RuntimeError, "Operation not permitted"):
                launchd.launchd_load_state(label)
        with mock.patch.object(launchd, "run_launchctl", side_effect=[query_error, loaded]):
            with self.assertRaisesRegex(RuntimeError, "Operation not permitted"):
                launchd.bootout(label, verify=True)

    def test_interval_launchd_idle_is_not_misclassified_as_stopped(self) -> None:
        interval = launchd.SERVICES["usdjpy-history-sync"]
        cases = (
            ("state = running\n", "RUNNING"),
            ("state = not running\nlast exit code = 0\n", "IDLE_OK"),
            ("state = not running\nlast exit code = (never exited)\n", "IDLE_PENDING"),
            ("state = not running\nlast exit code = 7\n", "FAILED"),
        )
        for output, expected in cases:
            with self.subTest(expected=expected):
                observed = launchd.launchd_lifecycle(interval, output)
                self.assertEqual(observed["lifecycle"], expected)

        persistent = launchd.SERVICES["backend-api"]
        observed = launchd.launchd_lifecycle(
            persistent,
            "state = not running\nlast exit code = 0\n",
        )
        self.assertEqual(observed["lifecycle"], "STOPPED")

    def test_private_directory_chain_rejects_backup_ancestor_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            private_root = root / ".quantgod"
            outside = root / "outside"
            private_root.mkdir()
            outside.mkdir()
            (private_root / "backups").symlink_to(outside, target_is_directory=True)
            with mock.patch.object(launchd, "PRIVATE_ROOT", private_root):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    launchd.harden_private_directory(private_root / "backups" / "sqlite")

    def test_plists_keep_trading_mutation_out_of_launch_layer(self) -> None:
        labels = {name: service["label"] for name, service in launchd.SERVICES.items()}
        self.assertIn("usdjpy-history-sync", labels)
        for service in launchd.SERVICES.values():
            payload = launchd.render_plist(service)
            serialized = json.dumps(payload, sort_keys=True)
            self.assertIn(service["label"], serialized)
            self.assertNotIn("ORDER_SEND_ALLOWED=1", serialized)
            self.assertNotIn("LIVE_PRESET_MUTATION_ALLOWED=1", serialized)
            self.assertEqual(payload["Umask"], 0o077)

        mt5 = launchd.render_plist(launchd.SERVICES["mt5-shadow-supervisor"])
        self.assertIs(mt5["KeepAlive"], True)
        self.assertEqual(mt5["ThrottleInterval"], 60)
        user_environment = launchd.local_user_environment()
        for key, value in user_environment.items():
            self.assertEqual(mt5["EnvironmentVariables"][key], value)
        self.assertEqual(
            mt5["EnvironmentVariables"]["QG_MT5_EXPECTED_SERVER"],
            launchd.MT5_EXPECTED_BROKER_SERVER,
        )
        self.assertEqual(
            mt5["EnvironmentVariables"]["QG_MT5_LOGIN_REFERENCE_CONFIG"],
            str(launchd.default_mt5_login_reference_config()),
        )
        for key in ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG"):
            self.assertNotIn(key, mt5["EnvironmentVariables"])
        backend = launchd.render_plist(launchd.SERVICES["backend-api"])
        self.assertIs(backend["KeepAlive"], True)
        self.assertEqual(backend["ThrottleInterval"], 10)
        frontend_build = launchd.render_plist(launchd.SERVICES["frontend-dist-build"])
        self.assertNotIn("KeepAlive", frontend_build)
        self.assertNotIn("StartInterval", frontend_build)

    def test_mt5_plist_and_capability_block_invalid_user_environment(self) -> None:
        with mock.patch.object(
            launchd,
            "local_user_environment_issues",
            return_value=["MT5_USER_TMPDIR_OWNER_MISMATCH"],
        ):
            with self.assertRaisesRegex(ValueError, "MT5_USER_TMPDIR_OWNER_MISMATCH"):
                launchd.render_plist(launchd.SERVICES["mt5-shadow-supervisor"])
            with tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                capability = launchd.service_capability(
                    "mt5-shadow-supervisor",
                    {
                        "backend": root / "backend",
                        "frontend": root / "frontend",
                        "infra": root / "infra",
                        "docs": root / "docs",
                    },
                )
        self.assertFalse(capability["ready"])
        self.assertIn("MT5_USER_TMPDIR_OWNER_MISMATCH", capability["reasonCodes"])

    def test_write_files_hardens_private_directories_environment_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            private_root = root / ".quantgod"
            bin_dir = private_root / "bin"
            log_dir = private_root / "logs"
            lock_dir = private_root / "locks"
            status_dir = private_root / "status"
            backup_dir = private_root / "backups" / "sqlite"
            env_path = private_root / "launchd.env"
            capability_path = status_dir / "launchd-capabilities.json"
            launch_agent_dir = root / "LaunchAgents"
            workspace_root = root / "workspace-root"
            for repo in ("QuantGodBackend", "QuantGodFrontend", "QuantGodInfra", "QuantGodDocs"):
                (workspace_root / repo).mkdir(parents=True)
            workspace = root / "workspace.json"
            workspace.write_text(
                json.dumps(
                    {
                        "workspaceRoot": str(workspace_root),
                        "backend": "QuantGodBackend",
                        "frontend": "QuantGodFrontend",
                        "infra": "QuantGodInfra",
                        "docs": "QuantGodDocs",
                    }
                ),
                encoding="utf-8",
            )
            log_dir.mkdir(parents=True)
            old_log = log_dir / "existing.log"
            old_log.write_text("existing", encoding="utf-8")
            nested_log_dir = log_dir / "archive"
            nested_log_dir.mkdir()
            nested_log = nested_log_dir / "existing.log"
            nested_log.write_text("existing nested", encoding="utf-8")
            private_root.chmod(0o755)
            log_dir.chmod(0o755)
            old_log.chmod(0o644)
            nested_log_dir.chmod(0o755)
            nested_log.chmod(0o644)

            with (
                mock.patch.object(launchd, "PRIVATE_ROOT", private_root),
                mock.patch.object(launchd, "BIN_DIR", bin_dir),
                mock.patch.object(launchd, "LOG_DIR", log_dir),
                mock.patch.object(launchd, "LOCK_DIR", lock_dir),
                mock.patch.object(launchd, "STATUS_DIR", status_dir),
                mock.patch.object(launchd, "BACKUP_DIR", backup_dir),
                mock.patch.object(launchd, "ENV_PATH", env_path),
                mock.patch.object(launchd, "CAPABILITY_PATH", capability_path),
                mock.patch.object(launchd, "LAUNCH_AGENT_DIR", launch_agent_dir),
            ):
                launchd.write_files(workspace)

            self.assertEqual(stat.S_IMODE(private_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(bin_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(log_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(lock_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(status_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(backup_dir.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(capability_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(old_log.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(nested_log_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(nested_log.stat().st_mode), 0o600)
            for wrapper in bin_dir.iterdir():
                self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o700)
            for log_file in log_dir.glob("*.log"):
                self.assertEqual(stat.S_IMODE(log_file.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
