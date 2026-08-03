# QuantGodInfra

QuantGodInfra owns the local workspace automation, deployment support, dist synchronization, macOS LaunchAgent setup, and split-repository validation for QuantGod.

It does not own trading logic, Vue components, MT5 presets, or product documentation. Its role is to keep the four repositories operating as one controlled local system.

## Repository Role

| Area | Path | Responsibility |
|---|---|---|
| Workspace helper | `scripts/qg-workspace.py` | Multi-repository status, tests, frontend build, dist sync, closed-loop verification |
| macOS automation | `scripts/qg-macos-launchd.py` | LaunchAgent generation for the local API/UI, strict MT5 Shadow supervisor, advisory loops, health, logs, and backups |
| Local Docker | `docker/`, `scripts/qg-docker-local.py` | Optional local backend/frontend Compose stack |
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
python3 scripts/qg-workspace.py --workspace workspace/quantgod.workspace.json verify-release
python3 scripts/qg-workspace.py --workspace workspace/quantgod.workspace.json closed-loop
```

`closed-loop` performs the local operator-workbench path:

1. Run frontend guards.
2. Build Vue.
3. Sync `dist/` into backend `Dashboard/vue-dist/`.
4. Run backend and docs verification.

It does not modify tracked MT5 presets, credentials, wallet state, or broker configuration.

`verify` (also available as `verify-integrity`) is the backward-compatible, read-only structure and evidence-integrity check. Before accepting evidence it scans every tracked Backend `.mq5`/`.mqh` source for broker-mutation surfaces, verifies all ordinary MT5 configs and presets remain Shadow/ReadOnly, confirms tracked launchers fail closed, and requires the upper safety contract to declare `executionLaneExists=false` and `existingEaOwnsExecution=false`. It then runs the Docs strict API contract check against Backend with `--strict-extra --min-endpoints 100` and verifies Backend core runtime evidence integrity with `tools/run_runtime_evidence_integrity.py --runtime-dir ./runtime verify`. It does not authorize a release or trading execution.

`verify-release` is the explicit fail-closed release acceptance gate. Required contract/integrity guards must exist, the no-execution static boundary must pass, runtime evidence must return valid JSON, and both integrity and `promotionGateStatus` must be `PASS`; blockers, stale evidence, or missing evidence return a non-zero exit. Neither verification command writes MT5 order request/receipt files, mutates tracked presets, or enables broker execution.

`sync-frontend-dist` copies the built frontend into a verified staging directory, compares SHA-256 manifests, then atomically promotes it. An existing backend dist is preserved as a sibling `vue-dist.previous-*` directory, and a failed promotion restores the active dist.

## macOS LaunchAgents

Install local background services:

```bash
cd /Users/bowen/Desktop/Quard/QuantGodInfra
python3 scripts/qg-macos-launchd.py --workspace workspace/quantgod.workspace.json doctor
python3 scripts/qg-macos-launchd.py --workspace workspace/quantgod.workspace.json install
python3 scripts/qg-macos-launchd.py status
```

The default `core` profile loads only the local Backend and Vite development
Frontend. The production-style single-account profile is `local-shadow`; the
optional two-prefix observer profile is `local-dual-shadow`. Preflight and
inspect it without changing any managed file or loaded service first:

```bash
python3 scripts/qg-macos-launchd.py --workspace workspace/quantgod.workspace.json install --profile local-shadow --no-load
python3 scripts/qg-macos-launchd.py --workspace workspace/quantgod.workspace.json install --profile local-dual-shadow --no-load
python3 scripts/qg-macos-launchd.py --workspace workspace/quantgod.workspace.json doctor
```

`--no-load` is a pure capability preflight. It writes no environment, wrapper,
plist, status, or preview file and does not call `bootout`, `bootstrap`,
`kickstart`, or restart a process. A real `local-shadow` install is refused
before any managed-file or launchd state change if a selected capability is
`BLOCKED`. During a real install, Frontend build completes before Backend is
stopped; atomic `/vue` publication then completes while it is stopped and before
restart. Managed files are published with `fsync` plus atomic replace; if final
capability or service activation fails, the previous `/vue`, managed files,
secondary observer preset, and previously loaded QuantGod profile are restored.
For `local-dual-shadow`, a safe Backend preset source can appear as
`READY_AFTER_PRESET_DEPLOY` in `--no-load`; preflight remains read-only and the
copy occurs only after both MT5 supervisors have been stopped during a real
transactional install.

`local-shadow` uses the compiled Frontend served by Backend `/vue`; it does not
load the Vite development server, Daily Autopilot, Telegram, or DeepSeek. It
adds the strict MT5 Shadow supervisor, direct fail-closed history sync,
advisory-only automation chain, health refresh, independent log maintenance,
and verified local SQLite backup. The MT5 wrapper accepts only
`QuantGod_MT5_HFM_Shadow_mac.ini` plus `QuantGod_MT5_HFM_Shadow.set`, verifies
the config contains exactly one `Server=HFMarketsGlobal-Live12`, verifies
`AllowLiveTrading=0`, `ShadowMode=true`, `ReadOnlyMode=true`, and
`EnablePilotAutoTrading=false`, and refuses a duplicate or unreviewed MT5 terminal.
The generated private env and MT5 plist pin the same non-secret expected-server
value; capability preflight and the final wrapper check both fail closed on a
missing, duplicate, differently-cased, or synthetic server. Login and password
values are neither generated nor logged by Infra.
The Shadow `Login` must also be one non-empty numeric value and silently match
the same Wine prefix's private `QuantGod_MT5_LoginOnly_mac.ini`; that reference
must name the same exact HFM server. The generated environment/plist contain
only the reference path, never the Login value. A missing, malformed, duplicate,
or mismatched identity is reported only by a non-secret reason code and blocks
Wine before process launch.
Because macOS launchd does not inherit the interactive login environment, the
generated private env and MT5 plist also carry the minimal non-secret
`QG_USER_HOME`, `QG_LOCAL_USER`, `QG_USER_TMPDIR`, and `QG_USER_LANG` values.
Immediately before Wine starts, the wrapper requires absolute, existing,
non-symlinked HOME/TMPDIR paths owned by the launchd uid, verifies the passwd
home and username, validates LANG, and only then exports
`HOME/USER/LOGNAME/TMPDIR/LANG`. Any mismatch is `BLOCKED`; no credential file
is loaded and the Shadow contract is unchanged.

The MT5 wrapper remains the launchd-owned supervisor instead of replacing
itself with Wine. It records the exact terminal child PID, forwards only
`TERM`/`INT` to that child, waits for its exit, and atomically replaces
`RUNNING` with `STOPPED` or `FAILED`. A Wine launcher that exits while another
`terminal64.exe` remains is treated as `MT5_CHILD_DETACHED` and fails closed;
the supervisor never uses broad `pkill` or kills an unrelated MT5 process.
Both the pre-launch duplicate check and post-exit detach check require a Wine
launcher/preloader whose immediate payload is `terminal64.exe`; a Python worker
that merely carries `--terminal-path /.../terminal64.exe` is not an MT5 process.
`local-dual-shadow` adds an isolated Live16 observer with the unique
`QuantGod_MT5_HFM_SecondaryShadow_mac.ini` signature. Each supervisor classifies
processes by canonical Wine-prefix root and exact config argument, permits only
the other reviewed prefix, and keeps separate singleton/status files. Installed
secondary preset content is sourced only from Backend
`MQL5/Presets/QuantGod_MT5_HFM_LiveSecondary.set` and atomically published as
`QuantGod_MT5_HFM_Shadow.set` inside the isolated Live16 prefix. The filename is
kept because the private Shadow config references it, but the full path and
source are independent from the Live12 cent preset. Capability and runtime
guards require an exact source match plus `Watchlist=USDJPY`,
`PreferredSymbolSuffix=AUTO`, `ShadowMode=true`, `ReadOnlyMode=true`,
`EnablePilotAutoTrading=false`, and every reviewed live-route authorization to
remain false; `USDJPYc`, suffix `c`, preset drift, or any trading enablement
blocks Wine before launch. Installed EA source and binary must match the same
clean verified build staging artifacts;
the source is scanned for Trade.mqh, CTrade, OrderSend, and raw trade actions
before either terminal starts. The private `drive_c/qg/compile.log` must identify
the reviewed staging source, report zero errors and warnings, contain no broker
mutation surface, and belong to the same build-time window as the staged binary.
The legacy `QG_MT5_SECONDARY_ENABLED` execution
switch stays `0`; only the read-only `QG_MT5_SECONDARY_SHADOW_ENABLED` observer
flag becomes `1` in this profile.
An existing singleton lock is reclaimed only when all owner identity fields are
complete and the recorded process identity is stale. Empty or partially written
locks are treated as in-progress/unsafe and block instead of being deleted.

To add the read-only USDJPY research services to the development profile,
install with `--profile research`.
`--profile all` remains visible for compatibility, but capability preflight
currently blocks it because the legacy Agent v2.5 loop can hide required-stage
failures. `local-shadow` uses the validated automation-chain service instead.
Telegram sending and DeepSeek remain disabled in the generated environment.

Generated agents:

| Agent | Purpose |
|---|---|
| `com.quantgod.backend-api` | Backend `/api/*` and static `/vue/` server |
| `com.quantgod.frontend-dev` | Vite workbench at `http://127.0.0.1:5173/vue/` |
| `com.quantgod.frontend-dist-build` | Build Frontend and atomically publish it to Backend `Dashboard/vue-dist/` |
| `com.quantgod.mt5-shadow-supervisor` | Single-instance MT5 supervisor locked to the reviewed Shadow/ReadOnly config |
| `com.quantgod.mt5-secondary-shadow-supervisor` | Optional isolated Live16 observer locked to a distinct Shadow/ReadOnly config; enabled only by `local-dual-shadow` |
| `com.quantgod.daily-autopilot` | Deprecated compatibility definition; explicitly `BLOCKED` until stage failures propagate non-zero |
| `com.quantgod.usdjpy-history-sync` | Hourly USDJPY MT5 K-line sync into `runtime/backtest/usdjpy.sqlite` |
| `com.quantgod.automation-chain` | Five-minute advisory-only automation chain with required-step result validation |
| `com.quantgod.health-maintenance` | Minute-level local AgentOps/runtime health evidence refresh |
| `com.quantgod.log-maintenance` | Hourly rotation for Backend `runtime/`, the active MT5 evidence root, and launchd logs, with explicit 32 MB active / 1024 MB archive caps |
| `com.quantgod.sqlite-backup` | Daily online SQLite backup followed by `quick_check`, SHA-256 verification, and retention of the latest 3 verified backup sets |
| `com.quantgod.ai-telegram-monitor` | DeepSeek-assisted MT5 advisory push-only monitor |

`qg-macos-launchd.py status` reports scheduling lifecycle separately from the
last wrapper result. A loaded interval job is expected to be `not running`
between invocations: a zero last-exit code is shown as `IDLE_OK`, no completed
invocation yet is `IDLE_PENDING`, and a non-zero last-exit code remains
`FAILED`. This lifecycle classification does not override `runtimeStatus`,
`observedHealth`, or `observedReadiness`; those fields continue to fail closed
on stale, blocked, or failed evidence.

Private environment values live in `~/.quantgod/launchd.env`. Logs are written
to `~/.quantgod/logs/`; capability and per-service runtime states are under
`~/.quantgod/status/`; verified local backups are under
`~/.quantgod/backups/sqlite/`. Local backup protects against an individual DB
failure but is not a second-device disaster backup. The installer enforces mode
`0700` on private directories and `0600` on environment/status/log files,
refuses symlink targets, and sets launchd `Umask=077` for future files. Core
wrappers do not load Telegram or DeepSeek secret files; those files are visible
only to the explicitly opted-in outbound monitor that needs them.

Log maintenance canonicalizes all three roots, rejects symlink components and
root collisions, and de-duplicates Backend runtime versus active MT5 evidence
when they resolve to the same directory. SQLite backup creates and validates the
new set before pruning; only backup sets with verified identity are eligible for
the keep-3 retention cleanup.

Uninstall:

```bash
python3 scripts/qg-macos-launchd.py uninstall
```

## Local Shadow Launch Policy

Infra does not start Agent v2.5 or the legacy daily autopilot loop. That
compatibility service is fail-closed because its required-stage failure
contract is unsafe; `local-shadow` runs the validated advisory automation chain.

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
QG_AGENT_V25_SEND_TELEGRAM=0
QG_AGENT_V25_HEAVY_TELEGRAM_GATEWAY=0
QG_AGENT_OPS_HEALTH_ENABLED=1
QG_PRODUCTION_BURN_IN_ENABLED=1
QG_PRODUCTION_BURN_IN_INTERVAL_SECONDS=300
QG_PRODUCTION_BURN_IN_SAMPLE_INTERVAL_MINUTES=5
QG_PRODUCTION_BURN_IN_WINDOW_HOURS=72
QG_MT5_TERMINAL_PATH=<local MetaTrader 5 terminal64.exe>
QG_MT5_PYTHON_BIN=<python3 with optional MetaTrader5 package>
QG_MT5_EXPECTED_SERVER=HFMarketsGlobal-Live12
QG_MT5_LOGIN_REFERENCE_CONFIG=<private same-prefix QuantGod_MT5_LoginOnly_mac.ini>
QG_MT5_SECONDARY_ENABLED=0
QG_MT5_SECONDARY_SHADOW_ENABLED=<0 for local-shadow; 1 for local-dual-shadow>
QG_MT5_SECONDARY_ALLOW_LIVE_TRADING=0
QG_MT5_SECONDARY_SHADOW_PRESET_SOURCE=<Backend MQL5/Presets/QuantGod_MT5_HFM_LiveSecondary.set>
QG_USDJPY_HISTORY_SYNC_ENABLED=1
QG_USDJPY_HISTORY_INTERVAL_SECONDS=3600
QG_TELEGRAM_PUSH_ALLOWED=0
QG_MT5_AI_DEEPSEEK_ENABLED=0
QG_USDJPY_HISTORY_MONTHS=12
QG_USDJPY_HISTORY_TIMEFRAMES=M1,M5,M15,H1
```

`com.quantgod.usdjpy-history-sync` directly runs Backend
`tools/run_usdjpy_strategy_backtest.py sync-klines`, validates its JSON/`ok`
contract, then runs and validates the `quality` stage. It intentionally bypasses
the legacy shell loop that can hide command failures. When the local Python
environment has the `MetaTrader5` package and `QG_MT5_TERMINAL_PATH` points at
the HFM/MT5 terminal, it uses `copy_rates_range`; otherwise Backend can ingest
the read-only MQL5 CopyRates CSV export or the current runtime snapshot and
reports remaining coverage/freshness blockers explicitly.

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

## Validation

```bash
cd /Users/bowen/Desktop/Quard/QuantGodInfra
python3 -m unittest discover tests -v
python3 scripts/qg-split-path-guard.py --root /Users/bowen/Desktop/Quard --include-codex-automations
```

Focused launchd tests:

```bash
python3 -m unittest discover -s tests -p 'test_macos_launchd.py' -v
```

## Safety Boundaries

Infra may start processes and synchronize static assets. It must not:

- Write MT5 broker-execution decisions or introduce an execution lane.
- Store Telegram, DeepSeek, broker, wallet, or private-key secrets in Git.
- Add Telegram command execution.
- Mutate tracked Shadow/ReadOnly preset safety settings.
- Add any remote snapshot upload or public ingress path; this system is local-only.
