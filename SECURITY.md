# Security Policy

## Scope

This policy covers the QuantGod split repository workspace. The project is local-first and safety-first. The repositories must not store credentials, private account details, API secrets, broker login details, Telegram bot secrets, OpenRouter keys, GitHub tokens, or MT5 runtime credentials.

## Supported repositories

- `QuantGodBackend`: backend API, MT5 bridge, AI, Governance, ParamLab, tests.
- `QuantGodFrontend`: Vue operator workbench and API client.
- `QuantGodInfra`: local workspace automation and deployment helpers.
- `QuantGodDocs`: documentation, contracts, runbooks, phase status.

## Reporting

Report security issues privately to the repository owner. Do not publish exploit details or credential material in an issue, pull request, commit message, screenshot, or log attachment.

## Safety boundaries

- AI analysis is advisory only and cannot place trades.
- Vibe Coding is research/backtest only and cannot control MT5 directly.
- Telegram is push-only and cannot receive trading commands.
- Frontend reads runtime data through `/api/*` only.
- Kill Switch, authorization lock, dryRun, live preset mutation guard, and manual Version Gate authorization must not be bypassed.

## Secret handling

Use local environment variables, local ignored files, or external secret stores. Never commit actual secrets or screenshots that reveal secrets. Rotate any credential that may have been exposed.

## Dependency and CI expectations

Every repository should run its local tests and governance checker before pushing. Security-sensitive changes should preserve the existing safety envelopes and readonly boundaries.
