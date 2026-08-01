from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "qg-workspace.py"
spec = importlib.util.spec_from_file_location("qg_workspace", MODULE_PATH)
assert spec is not None and spec.loader is not None
qgw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qgw)


class WorkspaceHelperTest(unittest.TestCase):
    def _write_no_execution_backend_fixture(self, backend: pathlib.Path) -> None:
        expert = backend / qgw.BACKEND_MQL5_ROOT / "Experts" / "QuantGod_MultiStrategy.mq5"
        config = backend / qgw.BACKEND_CONFIG_ROOT / "QuantGod_MT5_Start.ini"
        preset = backend / qgw.BACKEND_PRESET_ROOT / "QuantGod_MT5_HFM_Shadow.set"
        schema = backend / qgw.BACKEND_LIVE_LOOP_SCHEMA
        trading_client = backend / qgw.BACKEND_RETIRED_TRADING_CLIENT
        mac_launcher = backend / qgw.BACKEND_MAC_LAUNCHER
        for path in (expert, config, preset, schema, trading_client, mac_launcher):
            path.parent.mkdir(parents=True, exist_ok=True)
        expert.write_text("bool IsPilotLiveMode() { return false; }\n", encoding="utf-8")
        config.write_text("[Experts]\nAllowLiveTrading=0\n", encoding="utf-8")
        preset.write_text(
            "\n".join(
                [
                    "ShadowMode=true",
                    "ReadOnlyMode=true",
                    "EnablePilotAutoTrading=false",
                    "EnablePilotRsiH1Live=false",
                    "EnableNonRsiLegacyLiveAuthorization=false",
                    "PilotCloseOnKillSwitch=false",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        schema.write_text(
            'SAFE_EVIDENCE_BOUNDARY = {"executionLaneExists": False, "existingEaOwnsExecution": False}\n',
            encoding="utf-8",
        )
        trading_client.write_text(
            "EXECUTION_LANE_EXISTS = False\n",
            encoding="utf-8",
        )
        mac_launcher.write_text(
            "\n".join(
                [
                    "case \"$QG_MT5_START_MODE\" in",
                    "  shadow|off)",
                    "assert_shadow_readonly_ea_source \"$EA_SOURCE\"",
                    'cp MQL5/Presets/QuantGod_MT5_HFM_Shadow.set "$MT5_PRESETS/QuantGod_MT5_HFM_Shadow.set"',
                    'mv -f "$EA_INSTALLED_OUTPUT" "$EA_DISABLED_OUTPUT"',
                    '"$EA_BUILD_OUTPUT" -nt "$EA_COMPILE_MARKER"',
                    'mv -f "$EA_INSTALL_TMP" "$EA_INSTALLED_OUTPUT"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        for relative in qgw.BACKEND_WINDOWS_LAUNCHERS:
            launcher = backend / relative
            launcher.write_text("@echo off\necho retired\nexit /b 2\n", encoding="utf-8")

    def test_example_is_portable(self) -> None:
        example = json.loads((ROOT / "workspace/quantgod.workspace.example.json").read_text(encoding="utf-8"))
        self.assertIn("workspaceRoot", example)
        serialized = json.dumps(example)
        for marker in ("/Users/", "C:\\Users\\", "Desktop/Quard"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, serialized)

    def test_relative_workspace_paths_resolve_from_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            infra = root / "QuantGodInfra"
            workspace_dir = infra / "workspace"
            workspace_dir.mkdir(parents=True)
            for repo in ("QuantGodBackend", "QuantGodFrontend", "QuantGodDocs"):
                (root / repo).mkdir()
            config = {
                "schemaVersion": 1,
                "workspaceRoot": "../..",
                "backend": "QuantGodBackend",
                "frontend": "QuantGodFrontend",
                "infra": "QuantGodInfra",
                "docs": "QuantGodDocs",
            }
            config_path = workspace_dir / "quantgod.workspace.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            ws = qgw.load_workspace(config_path)
            paths = qgw.repo_paths(ws)
            self.assertEqual(paths["backend"], root / "QuantGodBackend")
            self.assertEqual(paths["frontend"], root / "QuantGodFrontend")
            self.assertEqual(paths["infra"], root / "QuantGodInfra")
            self.assertEqual(paths["docs"], root / "QuantGodDocs")

    def test_default_workspace_falls_back_to_script_root_when_run_from_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            infra = root / "QuantGodInfra"
            workspace_dir = infra / "workspace"
            workspace_dir.mkdir(parents=True)
            config_path = workspace_dir / "quantgod.workspace.json"
            config_path.write_text("{}", encoding="utf-8")

            resolved = qgw.resolve_workspace_path(
                qgw.DEFAULT_WORKSPACE,
                cwd=root,
                script_root=infra,
            )

            self.assertEqual(resolved, config_path)

    def test_explicit_workspace_path_stays_relative_to_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            infra = root / "QuantGodInfra"
            (infra / "workspace").mkdir(parents=True)
            explicit = "missing/custom.workspace.json"

            resolved = qgw.resolve_workspace_path(
                explicit,
                cwd=root,
                script_root=infra,
            )

            self.assertEqual(resolved, root / explicit)

    def test_backend_node_tests_are_enumerated_and_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = pathlib.Path(tmp) / "QuantGodBackend"
            node_dir = backend / "tests" / "node"
            node_dir.mkdir(parents=True)
            test_file = node_dir / "api_contract.mjs"
            test_file.write_text("import test from 'node:test';\n", encoding="utf-8")

            with mock.patch.object(qgw, "run") as run_mock:
                qgw.run_backend_node_tests(backend)

            self.assertEqual(run_mock.call_count, 1)
            args, kwargs = run_mock.call_args
            command = [str(part) for part in args[0]]
            self.assertEqual(command[:2], ["node", "--test"])
            self.assertIn(str(test_file), command)
            self.assertNotIn("tests/node/*.mjs", command)
            self.assertNotEqual(kwargs.get("check"), False)

    def test_parser_exposes_closed_loop_command(self) -> None:
        parser = qgw.build_parser()
        args = parser.parse_args(["closed-loop", "--workspace", "workspace/quantgod.workspace.json"])
        self.assertEqual(args.command, "closed-loop")

    def test_parser_exposes_integrity_and_release_verify_commands(self) -> None:
        parser = qgw.build_parser()
        self.assertEqual(parser.parse_args(["verify-integrity"]).command, "verify-integrity")
        self.assertEqual(parser.parse_args(["verify-release"]).command, "verify-release")

    def test_closed_loop_runs_quality_build_sync_and_backend_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            backend = root / "QuantGodBackend"
            frontend = root / "QuantGodFrontend"
            infra = root / "QuantGodInfra"
            docs = root / "QuantGodDocs"
            (backend / "tools").mkdir(parents=True)
            (backend / "MQL5").mkdir()
            self._write_no_execution_backend_fixture(backend)
            (frontend / "src").mkdir(parents=True)
            (frontend / "dist").mkdir()
            (frontend / "dist" / "index.html").write_text("new frontend", encoding="utf-8")
            (infra / "scripts").mkdir(parents=True)
            (infra / "scripts" / "qg-workspace.py").write_text("", encoding="utf-8")
            (docs / "docs" / "architecture").mkdir(parents=True)
            (docs / "docs" / "architecture" / "repo-split.md").write_text("ok", encoding="utf-8")
            ws = {
                "backend": str(backend),
                "frontend": str(frontend),
                "infra": str(infra),
                "docs": str(docs),
                "frontendDist": "dist",
                "backendVueDist": "Dashboard/vue-dist",
            }
            calls: list[str] = []

            def remember(name: str):
                def inner(*_args, **_kwargs):
                    calls.append(name)

                return inner

            with (
                mock.patch.object(qgw, "run_frontend_quality", remember("frontend_quality")),
                mock.patch.object(qgw, "run_frontend_build", remember("frontend_build")),
                mock.patch.object(qgw, "run_backend_python_tests", remember("backend_python")),
                mock.patch.object(qgw, "run_backend_node_tests", remember("backend_node")),
                mock.patch.object(qgw, "run_docs_checks", remember("docs")),
            ):
                qgw.cmd_closed_loop(ws)

            self.assertEqual(
                calls,
                ["frontend_quality", "frontend_build", "backend_python", "backend_node", "docs"],
            )
            self.assertTrue((backend / "Dashboard" / "vue-dist").exists())

    def test_sync_frontend_dist_is_verified_atomic_and_preserves_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            backend = root / "QuantGodBackend"
            frontend = root / "QuantGodFrontend"
            infra = root / "QuantGodInfra"
            docs = root / "QuantGodDocs"
            frontend_dist = frontend / "dist"
            backend_dist = backend / "Dashboard" / "vue-dist"
            frontend_dist.mkdir(parents=True)
            backend_dist.mkdir(parents=True)
            infra.mkdir()
            docs.mkdir()
            (frontend_dist / "index.html").write_text("new frontend", encoding="utf-8")
            (backend_dist / "index.html").write_text("old frontend", encoding="utf-8")
            ws = {
                "backend": str(backend),
                "frontend": str(frontend),
                "infra": str(infra),
                "docs": str(docs),
            }

            qgw.cmd_sync_frontend_dist(ws)

            self.assertEqual((backend_dist / "index.html").read_text(encoding="utf-8"), "new frontend")
            backups = list((backend / "Dashboard").glob("vue-dist.previous-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "index.html").read_text(encoding="utf-8"), "old frontend")
            self.assertEqual(list((backend / "Dashboard").glob(".vue-dist.staging-*")), [])

    def test_sync_frontend_dist_restores_current_when_atomic_promotion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            backend = root / "QuantGodBackend"
            frontend = root / "QuantGodFrontend"
            infra = root / "QuantGodInfra"
            docs = root / "QuantGodDocs"
            frontend_dist = frontend / "dist"
            backend_dist = backend / "Dashboard" / "vue-dist"
            frontend_dist.mkdir(parents=True)
            backend_dist.mkdir(parents=True)
            infra.mkdir()
            docs.mkdir()
            (frontend_dist / "index.html").write_text("new frontend", encoding="utf-8")
            (backend_dist / "index.html").write_text("old frontend", encoding="utf-8")
            ws = {
                "backend": str(backend),
                "frontend": str(frontend),
                "infra": str(infra),
                "docs": str(docs),
            }
            original_rename = qgw._rename_path

            def fail_staging_promotion(source: pathlib.Path, destination: pathlib.Path) -> None:
                if source.name.startswith(".vue-dist.staging-"):
                    raise OSError("simulated atomic promotion failure")
                original_rename(source, destination)

            with mock.patch.object(qgw, "_rename_path", side_effect=fail_staging_promotion):
                with self.assertRaisesRegex(OSError, "simulated atomic promotion failure"):
                    qgw.cmd_sync_frontend_dist(ws)

            self.assertEqual((backend_dist / "index.html").read_text(encoding="utf-8"), "old frontend")
            self.assertEqual(list((backend / "Dashboard").glob(".vue-dist.staging-*")), [])

    def test_sync_frontend_dist_rejects_paths_outside_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name in ("QuantGodBackend", "QuantGodFrontend", "QuantGodInfra", "QuantGodDocs"):
                (root / name).mkdir()
            (root / "QuantGodFrontend" / "dist").mkdir()
            (root / "QuantGodFrontend" / "dist" / "index.html").write_text("ok", encoding="utf-8")
            ws = {
                "backend": str(root / "QuantGodBackend"),
                "frontend": str(root / "QuantGodFrontend"),
                "infra": str(root / "QuantGodInfra"),
                "docs": str(root / "QuantGodDocs"),
                "backendVueDist": "../outside",
            }

            with self.assertRaises(SystemExit):
                qgw.cmd_sync_frontend_dist(ws)

    def test_cmd_verify_checks_split_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            backend = root / "QuantGodBackend"
            frontend = root / "QuantGodFrontend"
            infra = root / "QuantGodInfra"
            docs = root / "QuantGodDocs"
            (backend / "tools").mkdir(parents=True)
            (backend / "MQL5").mkdir()
            self._write_no_execution_backend_fixture(backend)
            (frontend / "src").mkdir(parents=True)
            (infra / "scripts").mkdir(parents=True)
            (infra / "scripts" / "qg-workspace.py").write_text("", encoding="utf-8")
            (docs / "docs" / "architecture").mkdir(parents=True)
            (docs / "docs" / "architecture" / "repo-split.md").write_text("ok", encoding="utf-8")
            ws = {
                "backend": str(backend),
                "frontend": str(frontend),
                "infra": str(infra),
                "docs": str(docs),
            }
            qgw.cmd_verify(ws)

    def test_cmd_verify_runs_split_path_guard_with_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            backend = root / "QuantGodBackend"
            frontend = root / "QuantGodFrontend"
            infra = root / "QuantGodInfra"
            docs = root / "QuantGodDocs"
            (backend / "tools").mkdir(parents=True)
            (backend / "MQL5").mkdir()
            self._write_no_execution_backend_fixture(backend)
            (frontend / "src").mkdir(parents=True)
            (infra / "scripts").mkdir(parents=True)
            split_guard = infra / "scripts" / "qg-split-path-guard.py"
            split_guard.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            (infra / "scripts" / "qg-workspace.py").write_text("", encoding="utf-8")
            (docs / "docs" / "architecture").mkdir(parents=True)
            (docs / "docs" / "architecture" / "repo-split.md").write_text("ok", encoding="utf-8")
            ws = {
                "backend": str(backend),
                "frontend": str(frontend),
                "infra": str(infra),
                "docs": str(docs),
            }

            with mock.patch.object(qgw, "run") as run_mock:
                qgw.cmd_verify(ws)

            run_mock.assert_called_once_with(
                ["python3", str(split_guard), "--root", str(root), "--include-codex-automations"],
                infra,
            )

    def test_cmd_verify_runs_contract_and_runtime_integrity_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            backend = root / "QuantGodBackend"
            frontend = root / "QuantGodFrontend"
            infra = root / "QuantGodInfra"
            docs = root / "QuantGodDocs"
            (backend / "tools").mkdir(parents=True)
            (backend / "MQL5").mkdir()
            self._write_no_execution_backend_fixture(backend)
            (backend / "tools" / "run_runtime_evidence_integrity.py").write_text("", encoding="utf-8")
            (frontend / "src").mkdir(parents=True)
            (frontend / "package.json").write_text(
                json.dumps({"scripts": {"contract": "node scripts/frontend_api_contract_guard.mjs"}}),
                encoding="utf-8",
            )
            (infra / "scripts").mkdir(parents=True)
            (infra / "scripts" / "qg-workspace.py").write_text("", encoding="utf-8")
            (docs / "docs" / "architecture").mkdir(parents=True)
            (docs / "docs" / "architecture" / "repo-split.md").write_text("ok", encoding="utf-8")
            (docs / "scripts").mkdir(parents=True)
            (docs / "scripts" / "check_api_contract_matches_backend.py").write_text("", encoding="utf-8")
            (docs / "docs" / "contracts").mkdir(parents=True)
            (docs / "docs" / "contracts" / "api-contract.json").write_text("{}", encoding="utf-8")
            ws = {
                "backend": str(backend),
                "frontend": str(frontend),
                "infra": str(infra),
                "docs": str(docs),
            }

            runtime_payload = json.dumps(
                {
                    "status": "PASS",
                    "promotionGateStatus": "BLOCKED",
                    "promotionBlockers": ["historyProductionStatus:M1:freshness_not_ok"],
                    "promotionRecoveryQueue": [
                        {
                            "kind": "history_freshness",
                            "timeframe": "M1",
                            "status": "FRESHNESS_STALE",
                            "priority": "HIGH",
                            "nextActionZh": "刷新 M1 history freshness。",
                            "refreshCommand": "python3 tools/run_usdjpy_strategy_backtest.py --runtime-dir ./runtime sync-klines --months 12 --timeframes M1,M5,M15,H1",
                        }
                    ],
                }
            )
            runtime_result = mock.Mock(stdout=runtime_payload, returncode=0)

            with mock.patch.object(qgw, "run") as run_mock, mock.patch.object(
                qgw,
                "run_capture",
                return_value=runtime_result,
            ) as capture_mock:
                qgw.cmd_verify(ws)

            calls = [call.args for call in run_mock.call_args_list]
            frontend_contract_call = next(args for args in calls if args[0] == ["npm", "run", "contract"])
            contract_call = next(args for args in calls if "check_api_contract_matches_backend.py" in str(args[0]))
            integrity_call = capture_mock.call_args.args
            self.assertEqual(frontend_contract_call[1], frontend)
            contract_command = [str(part) for part in contract_call[0]]
            integrity_command = [str(part) for part in integrity_call[0]]
            self.assertIn("--strict-extra", contract_command)
            self.assertIn("--min-endpoints", contract_command)
            self.assertIn("100", contract_command)
            self.assertEqual(integrity_command[-2:], ["./runtime", "verify"])

    def test_runtime_integrity_summary_lines_compact_success_payload(self) -> None:
        payload = {
            "status": "PASS",
            "promotionGateStatus": "BLOCKED",
            "promotionBlockers": [
                "historyProductionStatus:M1:freshness_not_ok",
                "caseMemoryArtifactManifest:missing_category:MISSED_OPPORTUNITY",
            ],
            "promotionRecoveryQueue": [
                {
                    "kind": "history_freshness",
                    "timeframe": "M1",
                    "status": "FRESHNESS_STALE",
                    "priority": "HIGH",
                    "copyRatesExportFreshnessStatus": "STALE",
                    "copyRatesExportLatestLagHours": 263.4,
                    "continuousSyncStatus": "MISSING",
                    "continuousSyncRunning": False,
                    "continuousSyncMatchingProcessCount": 0,
                    "continuousSyncNextActionZh": "启动只读 history sync loop，并先刷新 MQL5 CopyRates exporter。",
                    "nextActionZh": "M1 覆盖和密度已满足，但 latestLagHours 超过阈值。",
                    "refreshCommand": "python3 tools/run_usdjpy_strategy_backtest.py --runtime-dir ./runtime sync-klines --months 12 --timeframes M1,M5,M15,H1",
                },
                {
                    "kind": "case_memory_category",
                    "category": "MISSED_OPPORTUNITY",
                    "status": "MISSING_CATEGORY",
                    "priority": "HIGH",
                    "nextActionZh": "收集高分影子机会被挡住后继续走盈利方向的样本。",
                    "collectionCommand": "python3 tools/run_usdjpy_bar_replay.py --runtime-dir ./runtime entry --write",
                    "caseMemoryBuildCommand": "python3 tools/run_case_memory.py --runtime-dir ./runtime build --write --limit 8",
                    "verifyCommand": "python3 tools/run_runtime_evidence_integrity.py --runtime-dir ./runtime verify",
                },
            ],
            "nextActionZh": "核心证据完整，但 promotion gate 仍阻断晋级。",
        }

        lines = qgw.runtime_integrity_summary_lines(payload)
        text = "\n".join(lines)

        self.assertIn("status=PASS", text)
        self.assertIn("promotionGate=BLOCKED", text)
        self.assertIn("promotionBlockers=2", text)
        self.assertIn("recoveryQueue=2", text)
        self.assertIn("history:M1", text)
        self.assertIn("CopyRates=STALE", text)
        self.assertIn("latestLag=263.4h", text)
        self.assertIn("SyncLoop=MISSING", text)
        self.assertIn("syncMatches=0", text)
        self.assertIn("sync: 启动只读 history sync loop", text)
        self.assertIn("case:MISSED_OPPORTUNITY", text)
        self.assertIn("command: python3 tools/run_usdjpy_strategy_backtest.py", text)
        self.assertIn("command: python3 tools/run_usdjpy_bar_replay.py", text)
        self.assertNotIn('"artifacts"', text)

    def test_release_runtime_integrity_rejects_blocked_stale_and_missing(self) -> None:
        payload = {
            "status": "PASS",
            "ok": True,
            "promotionGateStatus": "BLOCKED",
            "promotionGatePassed": False,
            "promotionBlockers": ["historyProductionStatus:M1:freshness_not_ok"],
            "promotionRecoveryQueue": [
                {
                    "kind": "history_freshness",
                    "timeframe": "M1",
                    "status": "FRESHNESS_STALE",
                    "continuousSyncStatus": "MISSING",
                }
            ],
            "blockers": [],
        }

        issues = qgw.release_runtime_integrity_issues(payload)
        text = "\n".join(issues)

        self.assertIn("promotion gate is not PASS", text)
        self.assertIn("promotion blockers remain", text)
        self.assertIn("FRESHNESS_STALE", text)
        self.assertIn("continuousSyncStatus=MISSING", text)

    def test_release_runtime_integrity_accepts_explicit_pass(self) -> None:
        payload = {
            "status": "PASS",
            "ok": True,
            "promotionGateStatus": "PASS",
            "promotionGatePassed": True,
            "promotionBlockers": [],
            "promotionRecoveryQueue": [],
            "blockers": [],
        }

        self.assertEqual(qgw.release_runtime_integrity_issues(payload), [])

    def test_release_required_checks_fail_closed_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.assertRaises(SystemExit):
                qgw.run_frontend_contract_guard(root / "frontend", required=True)
            with self.assertRaises(SystemExit):
                qgw.run_docs_api_contract_strict(root / "docs", root / "backend", required=True)
            with self.assertRaises(SystemExit):
                qgw.run_backend_runtime_integrity_verify(root / "backend", required=True)

    def test_verify_release_command_fails_when_promotion_gate_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            backend = root / "QuantGodBackend"
            frontend = root / "QuantGodFrontend"
            infra = root / "QuantGodInfra"
            docs = root / "QuantGodDocs"
            (backend / "tools").mkdir(parents=True)
            (backend / "MQL5").mkdir()
            self._write_no_execution_backend_fixture(backend)
            (backend / "tools" / "run_runtime_evidence_integrity.py").write_text("", encoding="utf-8")
            (frontend / "src").mkdir(parents=True)
            (frontend / "package.json").write_text(
                json.dumps({"scripts": {"contract": "node scripts/frontend_api_contract_guard.mjs"}}),
                encoding="utf-8",
            )
            (infra / "scripts").mkdir(parents=True)
            (infra / "scripts" / "qg-workspace.py").write_text("", encoding="utf-8")
            (infra / "scripts" / "qg-split-path-guard.py").write_text("", encoding="utf-8")
            (docs / "docs" / "architecture").mkdir(parents=True)
            (docs / "docs" / "architecture" / "repo-split.md").write_text("ok", encoding="utf-8")
            (docs / "scripts").mkdir(parents=True)
            (docs / "scripts" / "check_api_contract_matches_backend.py").write_text("", encoding="utf-8")
            (docs / "docs" / "contracts").mkdir(parents=True)
            (docs / "docs" / "contracts" / "api-contract.json").write_text("{}", encoding="utf-8")
            ws = {
                "backend": str(backend),
                "frontend": str(frontend),
                "infra": str(infra),
                "docs": str(docs),
            }
            blocked_payload = json.dumps(
                {
                    "status": "PASS",
                    "ok": True,
                    "promotionGateStatus": "BLOCKED",
                    "promotionGatePassed": False,
                    "promotionBlockers": ["historyProductionStatus:M1:freshness_not_ok"],
                    "promotionRecoveryQueue": [
                        {"kind": "history_freshness", "timeframe": "M1", "status": "FRESHNESS_STALE"}
                    ],
                    "blockers": [],
                }
            )
            runtime_result = mock.Mock(stdout=blocked_payload, returncode=0)

            with mock.patch.object(qgw, "run"), mock.patch.object(
                qgw,
                "run_capture",
                return_value=runtime_result,
            ):
                with self.assertRaises(SystemExit):
                    qgw.cmd_verify_release(ws)

    def test_manifest_remote_issues_accept_current_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "repo-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "repos": {
                            "backend": {
                                "name": "BoDUD/QuantGodBackend",
                                "url": "https://github.com/BoDUD/QuantGodBackend.git",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            issues = qgw.manifest_remote_issues(
                manifest,
                {"backend": "https://github.com/BoDUD/QuantGodBackend"},
            )

            self.assertEqual([], issues)

    def test_manifest_remote_issues_reject_owner_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "repo-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "repos": {
                            "backend": {
                                "name": "Boowenn/QuantGodBackend",
                                "url": "https://github.com/Boowenn/QuantGodBackend",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            issues = qgw.manifest_remote_issues(
                manifest,
                {"backend": "https://github.com/BoDUD/QuantGodBackend"},
            )

            self.assertTrue(any("repos.backend.url" in issue for issue in issues))
            self.assertTrue(any("repos.backend.name" in issue for issue in issues))

    def test_active_dirty_issues_reports_workspace_dirty_state(self) -> None:
        paths = {
            "backend": pathlib.Path("/tmp/QuantGodBackend"),
            "frontend": pathlib.Path("/tmp/QuantGodFrontend"),
        }

        def fake_status(repo: pathlib.Path) -> list[str]:
            if repo.name == "QuantGodBackend":
                return [" M tools/example.py"]
            return []

        with mock.patch.object(qgw, "git_status_porcelain", side_effect=fake_status):
            issues = qgw.active_dirty_issues(paths)

        self.assertEqual(1, len(issues))
        self.assertIn("backend repo has uncommitted changes", issues[0])
        self.assertIn("tools/example.py", issues[0])

    def test_workspace_governance_status_lines_cover_status_level_guards(self) -> None:
        paths = {
            "backend": pathlib.Path("/tmp/QuantGodBackend"),
            "frontend": pathlib.Path("/tmp/QuantGodFrontend"),
            "infra": pathlib.Path("/tmp/QuantGodInfra"),
            "docs": pathlib.Path("/tmp/QuantGodDocs"),
        }
        ws = {"legacy": "/tmp/missing-legacy"}

        with (
            mock.patch.object(qgw, "active_dirty_issues", return_value=["backend repo has uncommitted changes"]),
            mock.patch.object(qgw, "tracked_local_tool_issues", return_value=[]),
            mock.patch.object(qgw, "local_artifact_ignore_issues", return_value=[]),
            mock.patch.object(qgw, "active_backend_no_execution_issues", return_value=[]),
            mock.patch.object(qgw, "workspace_manifest_remote_issues", return_value=[]),
            mock.patch.object(qgw, "split_path_guard_issues", return_value=["legacy absolute path found"]),
        ):
            lines = qgw.workspace_governance_status_lines(ws, paths)

        text = "\n".join(lines)
        self.assertIn("ISSUE: active repo dirty state", text)
        self.assertIn("OK: tracked local tool files", text)
        self.assertIn("ISSUE: split path guard / old-path contamination", text)
        self.assertIn("OK: legacy quarantine", text)

    def test_tracked_local_tool_files_filters_codex_paths(self) -> None:
        with mock.patch.object(
            qgw,
            "git_lines",
            return_value=[
                ".codex/skills/quantgod-trading-agent/SKILL.md",
                "tools/normal.py",
            ],
        ):
            tracked = qgw.tracked_local_tool_files(pathlib.Path("/tmp/QuantGodBackend"))

        self.assertEqual([".codex/skills/quantgod-trading-agent/SKILL.md"], tracked)

    def test_local_artifact_ignore_issues_report_missing_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            backend = root / "QuantGodBackend"
            backend.mkdir()
            (backend / ".git").mkdir()
            (backend / ".gitignore").write_text(".env\n*.log\n", encoding="utf-8")

            issues = qgw.local_artifact_ignore_issues({"backend": backend})

        self.assertEqual(1, len(issues))
        self.assertIn("backend repo .gitignore missing local artifact patterns", issues[0])
        self.assertIn(".codex/", issues[0])
        self.assertIn("runtime/", issues[0])
        self.assertIn("exports/", issues[0])

    def test_local_artifact_ignore_issues_accept_required_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            frontend = root / "QuantGodFrontend"
            frontend.mkdir()
            (frontend / ".git").mkdir()
            patterns = [
                *qgw.COMMON_LOCAL_IGNORE_PATTERNS,
                *qgw.REPO_LOCAL_IGNORE_PATTERNS["frontend"],
            ]
            (frontend / ".gitignore").write_text("\n".join(patterns) + "\n", encoding="utf-8")

            issues = qgw.local_artifact_ignore_issues({"frontend": frontend})

        self.assertEqual([], issues)

    def test_active_backend_no_execution_issues_detect_broker_mutation_and_unsafe_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = pathlib.Path(tmp) / "QuantGodBackend"
            self._write_no_execution_backend_fixture(backend)
            expert = backend / qgw.BACKEND_MQL5_ROOT / "Experts" / "QuantGod_MultiStrategy.mq5"
            expert.write_text(
                "#include <Trade\\Trade.mqh>\n"
                "void Unsafe() { OrderSend(request, result); trade.PositionOpen(); }\n",
                encoding="utf-8",
            )
            config = backend / qgw.BACKEND_CONFIG_ROOT / "QuantGod_MT5_Start.ini"
            config.write_text(
                "[Experts]\nallowlivetrading=1\nAllowLiveTrading=0\n",
                encoding="utf-8",
            )
            tester_config = backend / qgw.BACKEND_CONFIG_ROOT / "BacktestLab" / "Unsafe.ini"
            tester_config.parent.mkdir(parents=True)
            tester_config.write_text("[StartUp]\nAllowLiveTrading=1\n", encoding="utf-8")
            preset = backend / qgw.BACKEND_PRESET_ROOT / "QuantGod_MT5_HFM_Shadow.set"
            preset.write_text(
                "ShadowMode=false\nReadOnlyMode=false\nEnablePilotAutoTrading=true\n"
                "EnablePilotRsiH1Live=true\nEnableNonRsiLegacyLiveAuthorization=true\n"
                "PilotCloseOnKillSwitch=true\n",
                encoding="utf-8",
            )
            schema = backend / qgw.BACKEND_LIVE_LOOP_SCHEMA
            schema.write_text(
                '# SAFE_EVIDENCE_BOUNDARY = {"executionLaneExists": False, "existingEaOwnsExecution": False}\n'
                'SAFE_EVIDENCE_BOUNDARY = {"executionLaneExists": True, "existingEaOwnsExecution": True}\n',
                encoding="utf-8",
            )
            trading_client = backend / qgw.BACKEND_RETIRED_TRADING_CLIENT
            trading_client.write_text(
                "EXECUTION_LANE_EXISTS = True\nmt5.order_send({})\n",
                encoding="utf-8",
            )

            issues = qgw.active_backend_no_execution_issues(backend)

        joined = "\n".join(issues)
        self.assertIn("Trade.mqh include", joined)
        self.assertIn("OrderSend call", joined)
        self.assertIn("broker mutation method", joined)
        self.assertIn("AllowLiveTrading must occur once and equal 0", joined)
        self.assertIn("isolated tester config requires [Tester]", joined)
        self.assertIn("tester config must not contain [StartUp]", joined)
        self.assertIn("ShadowMode must occur once and equal true", joined)
        self.assertIn("EnablePilotAutoTrading must occur once and equal false", joined)
        self.assertIn("enablepilotrsih1live", joined.lower())
        self.assertIn("EnableNonRsiLegacyLiveAuthorization must not be true", joined)
        self.assertIn("PilotCloseOnKillSwitch must not mutate positions", joined)
        self.assertIn("SAFE_EVIDENCE_BOUNDARY.executionLaneExists must be false", joined)
        self.assertIn("EXECUTION_LANE_EXISTS must be literal false", joined)
        self.assertIn("forbidden Python broker mutation call: order_send", joined)

    def test_active_backend_no_execution_issues_fail_closed_when_boundaries_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = pathlib.Path(tmp) / "QuantGodBackend"
            backend.mkdir()
            issues = qgw.active_backend_no_execution_issues(backend)

        joined = "\n".join(issues)
        self.assertIn("MQL safety boundary is missing", joined)
        self.assertIn("non-Tester MT5 configs are missing", joined)
        self.assertIn("non-Backtest MT5 presets are missing", joined)
        self.assertIn("no-execution safety schema is missing", joined)
        self.assertIn("retired trading client is missing", joined)
        self.assertIn("tracked macOS launcher is missing", joined)
        self.assertIn("tracked Windows launcher is missing", joined)

    def test_active_backend_no_execution_issues_accept_complete_static_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = pathlib.Path(tmp) / "QuantGodBackend"
            self._write_no_execution_backend_fixture(backend)
            issues = qgw.active_backend_no_execution_issues(backend)

        self.assertEqual([], issues)


if __name__ == "__main__":
    unittest.main()
