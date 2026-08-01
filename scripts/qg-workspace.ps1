param(
  [string]$Workspace = "workspace\quantgod.workspace.json",
  [ValidateSet("status", "pull", "test", "build-frontend", "sync-frontend-dist", "verify", "verify-integrity", "verify-release", "closed-loop")]
  [string]$Command = "status"
)
python "$PSScriptRoot\qg-workspace.py" --workspace $Workspace $Command
