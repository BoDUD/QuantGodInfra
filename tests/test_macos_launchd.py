from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "qg-macos-launchd.py"
spec = importlib.util.spec_from_file_location("qg_macos_launchd", MODULE_PATH)
assert spec is not None and spec.loader is not None
launchd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launchd)


class MacLaunchdHelperTests(unittest.TestCase):
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
            self.assertNotIn("/QuantGod/", text)

    def test_daily_autopilot_wrapper_runs_agent_v25_once(self) -> None:
        wrappers = launchd.render_wrappers()
        daily = wrappers["quantgod-daily-autopilot.sh"]
        self.assertIn("tools/run_mac_agent_v25_loop.sh --once", daily)
        self.assertNotIn("tools/run_mac_daily_autopilot.sh --once", daily)
        self.assertIn("QG_BACKEND_ROOT", daily)

    def test_history_sync_wrapper_runs_sync_klines_once(self) -> None:
        wrappers = launchd.render_wrappers()
        history = wrappers["quantgod-usdjpy-history-sync.sh"]
        self.assertIn("tools/run_mac_usdjpy_history_sync_loop.sh --once", history)
        self.assertIn("QG_BACKEND_ROOT", history)

    def test_plists_keep_trading_mutation_out_of_launch_layer(self) -> None:
        labels = {name: service["label"] for name, service in launchd.SERVICES.items()}
        self.assertIn("usdjpy-history-sync", labels)
        for service in launchd.SERVICES.values():
            payload = launchd.render_plist(service)
            serialized = json.dumps(payload, sort_keys=True)
            self.assertIn(service["label"], serialized)
            self.assertNotIn("ORDER_SEND_ALLOWED=1", serialized)
            self.assertNotIn("LIVE_PRESET_MUTATION_ALLOWED=1", serialized)


if __name__ == "__main__":
    unittest.main()
