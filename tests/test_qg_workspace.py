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

    def test_closed_loop_runs_quality_build_sync_and_backend_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            backend = root / "QuantGodBackend"
            frontend = root / "QuantGodFrontend"
            infra = root / "QuantGodInfra"
            docs = root / "QuantGodDocs"
            (backend / "tools").mkdir(parents=True)
            (backend / "MQL5").mkdir()
            (frontend / "src").mkdir(parents=True)
            (frontend / "dist").mkdir()
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

    def test_cmd_verify_checks_split_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            backend = root / "QuantGodBackend"
            frontend = root / "QuantGodFrontend"
            infra = root / "QuantGodInfra"
            docs = root / "QuantGodDocs"
            (backend / "tools").mkdir(parents=True)
            (backend / "MQL5").mkdir()
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
            (backend / "cloudflare").mkdir()
            with self.assertRaises(SystemExit):
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

    def test_legacy_safety_issues_detect_non_rsi_live_residue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = pathlib.Path(tmp) / "QuantGod"
            preset = legacy / qgw.LEGACY_PRESET
            policy = legacy / qgw.LEGACY_POLICY_BUILDER
            preset.parent.mkdir(parents=True)
            policy.parent.mkdir(parents=True)
            preset.write_text(
                "\n".join(
                    [
                        "EnablePilotMA=true",
                        "EnablePilotBBH1Live=true",
                        "EnableNonRsiLegacyLiveAuthorization=true",
                    ]
                ),
                encoding="utf-8",
            )
            policy.write_text(
                'LIVE_ELIGIBLE_STRATEGIES = {"RSI_Reversal", "MA_Cross", "USDJPY_NIGHT_REVERSION_SAFE"}\n',
                encoding="utf-8",
            )

            issues = qgw.legacy_safety_issues(legacy)

        self.assertTrue(any("MA_Cross live switch" in issue for issue in issues))
        self.assertTrue(any("non-RSI legacy live authorization" in issue for issue in issues))
        self.assertTrue(any("live eligibility includes non-RSI" in issue for issue in issues))

    def test_legacy_safety_issues_accept_rsi_only_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = pathlib.Path(tmp) / "QuantGod"
            preset = legacy / qgw.LEGACY_PRESET
            policy = legacy / qgw.LEGACY_POLICY_BUILDER
            preset.parent.mkdir(parents=True)
            policy.parent.mkdir(parents=True)
            preset.write_text(
                "\n".join(
                    [
                        "EnablePilotMA=false",
                        "EnablePilotBBH1Live=false",
                        "EnableNonRsiLegacyLiveAuthorization=false",
                    ]
                ),
                encoding="utf-8",
            )
            policy.write_text('LIVE_ELIGIBLE_STRATEGIES = {"RSI_Reversal"}\n', encoding="utf-8")

            issues = qgw.legacy_safety_issues(legacy)

        self.assertEqual([], issues)

    def test_active_backend_live_lane_issues_detect_non_rsi_live_residue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = pathlib.Path(tmp) / "QuantGodBackend"
            preset = backend / qgw.LIVE_LANE_PRESET
            policy = backend / qgw.LIVE_LANE_POLICY_BUILDER
            preset.parent.mkdir(parents=True)
            policy.parent.mkdir(parents=True)
            preset.write_text(
                "\n".join(
                    [
                        "EnablePilotMA=true",
                        "EnablePilotMacdH1Live=true",
                        "EnableNonRsiLegacyLiveAuthorization=true",
                    ]
                ),
                encoding="utf-8",
            )
            policy.write_text(
                "\n".join(
                    [
                        'LIVE_ELIGIBLE_STRATEGIES = {"RSI_Reversal", "MA_Cross"}',
                        'LIVE_ELIGIBLE_DIRECTION = "SHORT"',
                    ]
                ),
                encoding="utf-8",
            )

            issues = qgw.active_backend_live_lane_issues(backend)

        self.assertTrue(any("active backend" in issue for issue in issues))
        self.assertTrue(any("MA_Cross live switch" in issue for issue in issues))
        self.assertTrue(any("MACD_Divergence live switch" in issue for issue in issues))
        self.assertTrue(any("non-RSI legacy live authorization" in issue for issue in issues))
        self.assertTrue(any("live eligibility includes non-RSI" in issue for issue in issues))
        self.assertTrue(any("live direction is not locked to LONG" in issue for issue in issues))

    def test_active_backend_live_lane_issues_accept_rsi_long_only_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = pathlib.Path(tmp) / "QuantGodBackend"
            preset = backend / qgw.LIVE_LANE_PRESET
            policy = backend / qgw.LIVE_LANE_POLICY_BUILDER
            preset.parent.mkdir(parents=True)
            policy.parent.mkdir(parents=True)
            preset.write_text(
                "\n".join(
                    [
                        "EnablePilotMA=false",
                        "EnablePilotBBH1Live=false",
                        "EnablePilotMacdH1Live=false",
                        "EnablePilotSRM15Live=false",
                        "EnableNonRsiLegacyLiveAuthorization=false",
                    ]
                ),
                encoding="utf-8",
            )
            policy.write_text(
                "\n".join(
                    [
                        'LIVE_ELIGIBLE_STRATEGIES = {"RSI_Reversal"}',
                        'LIVE_ELIGIBLE_DIRECTION = "LONG"',
                    ]
                ),
                encoding="utf-8",
            )

            issues = qgw.active_backend_live_lane_issues(backend)

        self.assertEqual([], issues)


if __name__ == "__main__":
    unittest.main()
