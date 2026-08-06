# Changelog

All notable repository changes should be summarized here.

## Unreleased

- Added repository governance files for the QuantGod split repository workspace.
- Added a stdlib-only governance checker and GitHub Actions workflow.
- Added hourly, fail-closed QuantGod disk maintenance with exact local roots,
  bounded retention and deletion budgets, invocation-bound report freshness,
  cross-state pressure validation, and explicit exclusion of general macOS/user
  data; Backend/MT5 log and JSONL archives remain exclusively owned by the
  existing log-maintenance service.
- Reduced each managed runtime's log-archive and JSONL-archive launchd caps from
  1024 MiB to 512 MiB for safer long-running low-disk operation.
- Limited successful Frontend publication to one immediately previous verified
  `vue-dist` copy while preserving atomic-promotion rollback behavior.

## Notes

Use concise entries grouped by feature, fix, documentation, infrastructure, and security where appropriate.
