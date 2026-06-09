# QuantGodInfra

QuantGodInfra owns the local workspace automation, deployment support, dist synchronization, macOS LaunchAgent setup, and split-repository validation for QuantGod.

It does not own trading logic, Vue components, MT5 presets, or product documentation. Its role is to keep the four repositories operating as one controlled local system.

## Repository Role

| Area | Path | Responsibility |
|---|---|---|
| Workspace helper | `scripts/qg-workspace.py` | Multi-repository status, tests, frontend build, dist sync, closed-loop verification |
| macOS automation | `scripts/qg-macos-launchd.py` | LaunchAgent generation for API, Vite, Agent v2.5, USDJPY history sync, and AI Telegram monitor |
| Local Docker | `docker/`, `scripts/qg-docker-local.py` | Optional local backend/frontend Compose stack |
| Cloud sync | `scripts/cloud-sync/` | Optional dashboard snapshot uploader |
| Guards | `scripts/qg-split-path-guard.py`, tests | Split-repo path and boundary validation |

Related repositories:

- Backend: `../QuantGodBackend`
- Frontend: `../QuantGodFrontend`
- Docs: `../QuantGodDocs`

## Workspace Configuration

Create a local workspace file:

```bash
cd /Users/bowen/Desktop/Quard/QuantGodInfra
cp workspace/quantgod.workspace.example.json workspace/quantgod.workspace.json
```

Recommended macOS layout:

```text
/Users/bowen/Desktop/Quard/QuantGodBackend
/Users/bowen/Desktop/Quard/QuantGodFrontend
/Users/bowen/Desktop/Quard/QuantGodInfra
/Users/bowen/Desktop/Quard/QuantGodDocs
```

Windows layouts are supported through the same workspace JSON.

## Workspace Commands

```bash
python3 scripts/qg-workspace.py --workspace workspace/quantgod.workspace.json status
python3 scripts/qg-workspace.py --workspace workspace/quantgod.workspace.json test
python3 scripts/qg-workspace.py --workspace workspace/quantgod.workspace.json build-frontend
python3 scripts/qg-workspace.py --workspace workspace/quantgod.workspace.json sync-frontend-dist
python3 scripts/qg-workspace.py --workspace workspace/quantgod.workspace.json verify
python3 scripts/qg-workspace.py --workspace workspace/quantgod.workspace.json closed-loop
```

`closed-loop` performs the local operator-workbench path:

1. Run frontend guards.
2. Build Vue.
3. Sync `dist/` into backend `Dashboard/vue-dist/`.
4. Run backend and docs verification.

It does not modify MT5 live presets, credentials, wallet state, or trading configuration.

## macOS LaunchAgents

Install local background services:

```bash
cd /Users/bowen/Desktop/Quard/QuantGodInfra
python3 scripts/qg-macos-launchd.py --workspace workspace/quantgod.workspace.json doctor
python3 scripts/qg-macos-launchd.py --workspace workspace/quantgod.workspace.json install
python3 scripts/qg-macos-launchd.py status
```

Generated agents:

| Agent | Purpose |
|---|---|
| `com.quantgod.backend-api` | Backend `/api/*` and static `/vue/` server |
| `com.quantgod.frontend-dev` | Vite workbench at `http://127.0.0.1:5173/vue/` |
| `com.quantgod.daily-autopilot` | Agent v2.5 USDJPY live-loop, policy, daily review, AgentOpsHealth, and P4-9 burn-in ledger |
| `com.quantgod.usdjpy-history-sync` | Hourly USDJPY MT5 K-line sync into `runtime/backtest/usdjpy.sqlite` |
| `com.quantgod.ai-telegram-monitor` | DeepSeek-assisted MT5 advisory push-only monitor |

Private environment values live in `~/.quantgod/launchd.env`. Logs are written to `~/.quantgod/logs/`.

Uninstall:

```bash
python3 scripts/qg-macos-launchd.py uninstall
```

## Agent v2.5 Launch Policy

Infra intentionally starts Agent v2.5, not the legacy daily autopilot loop.

Default launchd environment:

```text
QG_FOCUS_SYMBOL=USDJPYc
QG_ALLOWED_SYMBOLS=USDJPYc
QG_DISABLE_NON_FOCUS_SYMBOLS=1
QG_ACCOUNT_MODE=cent
QG_ACCOUNT_CURRENCY_UNIT=USC
QG_CENT_ACCOUNT_ACCELERATION=1
QG_LEGACY_DAILY_AUTOPILOT_ENABLED=0
QG_AGENT_V25_INTERVAL_SECONDS=300
QG_AGENT_OPS_HEALTH_ENABLED=1
QG_PRODUCTION_BURN_IN_ENABLED=1
QG_PRODUCTION_BURN_IN_INTERVAL_SECONDS=300
QG_PRODUCTION_BURN_IN_SAMPLE_INTERVAL_MINUTES=5
QG_PRODUCTION_BURN_IN_WINDOW_HOURS=72
QG_MT5_TERMINAL_PATH=<local MetaTrader 5 terminal64.exe>
QG_MT5_PYTHON_BIN=<python3 with optional MetaTrader5 package>
QG_USDJPY_HISTORY_SYNC_ENABLED=1
QG_USDJPY_HISTORY_INTERVAL_SECONDS=3600
QG_USDJPY_HISTORY_MONTHS=12
QG_USDJPY_HISTORY_TIMEFRAMES=M1,M5,M15,H1
```

`com.quantgod.usdjpy-history-sync` calls Backend `tools/run_mac_usdjpy_history_sync_loop.sh --once`.
When the local Python environment has the `MetaTrader5` package and `QG_MT5_TERMINAL_PATH` points at the HFM/MT5 terminal, it uses `copy_rates_range` to gradually fill 6-12 months of USDJPY bars into SQLite.
If MT5 Python is unavailable, Backend falls back to runtime snapshots and reports that full historical coverage is still pending.

Telegram and DeepSeek credentials remain in backend-local `.env.*.local` files and must not be committed.

## Local Docker

Docker is optional and is intended for local backend/frontend development only:

```bash
python3 scripts/qg-docker-local.py static-check
python3 scripts/qg-docker-local.py doctor
python3 scripts/qg-docker-local.py config
python3 scripts/qg-docker-local.py up
```

The local Docker stack does not introduce broker execution, public ingress, billing, user accounts, or credential storage.

## Cloudflare and Cloud Sync

Cloudflare deployment and Cloud Sync are optional. Local MT5/HFM operation does not depend on them.

Cloud Sync reads backend dashboard evidence and uploads selected snapshots when explicitly configured. Tokens must live in environment variables, Wrangler secrets, or ignored local config.

## Validation

```bash
cd /Users/bowen/Desktop/Quard/QuantGodInfra
python3 -m unittest discover tests -v
python3 scripts/qg-split-path-guard.py --root /Users/bowen/Desktop/Quard --include-codex-automations
```

Focused launchd tests:

```bash
python3 -m unittest tests.test_macos_launchd -v
```

## Safety Boundaries

Infra may start processes and synchronize static assets. It must not:

- Write MT5 live trading decisions.
- Store Telegram, DeepSeek, broker, wallet, or private-key secrets in Git.
- Add Telegram command execution.
- Mutate live preset risk settings.
- Convert optional Cloudflare tooling into a required runtime dependency.
