param([Parameter(Mandatory)][string]$LabRoot)

$ErrorActionPreference = 'Stop'
$lab = (Resolve-Path -LiteralPath $LabRoot).Path
$storage = Get-Content -Raw -LiteralPath (Join-Path $lab 'config/uve_triage_storage_manifest.json') | ConvertFrom-Json
$lifecycle = Get-Content -Raw -LiteralPath (Join-Path $lab $storage.storage_lifecycle_ref) | ConvertFrom-Json
$schedule = Get-Content -Raw -LiteralPath (Join-Path $lab $lifecycle.scheduler_ref) | ConvertFrom-Json
$binding = Get-Content -Raw -LiteralPath (Join-Path $lab $storage.lab_binding_ref) | ConvertFrom-Json
$activity = Get-Content -Raw -LiteralPath (Join-Path $lab $lifecycle.activity_ref) | ConvertFrom-Json
if ($schedule.action.operation -ne 'scan' -or $schedule.action.mode -ne $lifecycle.default_mode -or
    $schedule.action.mode -ne 'dry_run' -or $schedule.scheduler_actor_ref -ne $activity.maintenance_actor_class) {
    throw 'Scheduled action must be a maintenance inventory.'
}
[Environment]::SetEnvironmentVariable($binding.lab_root_env_key, $lab, 'Process')
Push-Location -LiteralPath (Join-Path $lab $binding.training_pipeline_relative_from_repo)
try {
    & $schedule.python_command -B -m agent_training.uve.cli storage $schedule.action.operation --mode $schedule.action.mode --actor-class $schedule.scheduler_actor_ref
    $status = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $status
