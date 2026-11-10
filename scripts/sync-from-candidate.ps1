# Sync engine + manifests from uve-v4-v6-candidate into this github checkout.
# No logs, local state, or training-only trees.

$ErrorActionPreference = "Stop"
$labRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$candidate = Join-Path $labRoot "uve-v4-v6-candidate"
$github = Split-Path $PSScriptRoot -Parent

if (-not (Test-Path $candidate)) {
    throw "Missing candidate root: $candidate"
}

function Sync-Tree {
    param(
        [string]$Relative,
        [string[]]$ExcludeFiles = @(),
        [string[]]$ExcludeDirs = @()
    )
    $src = Join-Path $candidate $Relative
    $dst = Join-Path $github $Relative
    if (-not (Test-Path $src)) {
        Write-Warning "Skip missing: $Relative"
        return
    }
    if (-not (Test-Path $dst)) {
        New-Item -ItemType Directory -Path $dst -Force | Out-Null
    }
    $xf = @("uve_engine_state.json") + $ExcludeFiles
    $xd = @("logs", "__pycache__") + $ExcludeDirs
    $xfArgs = $xf | ForEach-Object { "/XF", $_ }
    $xdArgs = $xd | ForEach-Object { "/XD", $_ }
    robocopy $src $dst /E /NFL /NDL /NJH /NJS /NC /NS @xfArgs @xdArgs | Out-Null
}

# Remove local evidence (never uploaded)
$logRoot = Join-Path $github "logs"
if (Test-Path $logRoot) {
    Remove-Item -LiteralPath $logRoot -Recurse -Force
}

Sync-Tree "agent_training\uve"
@(
    "issue_store.py",
    "manifest_loader.py",
    "paths.py",
    "content_hash.py"
) | ForEach-Object {
    Copy-Item -LiteralPath (Join-Path $candidate "agent_training\$_") `
        -Destination (Join-Path $github "agent_training\$_") -Force
}
Sync-Tree "agent_training\lab"

Sync-Tree "config\uve"
$manifestDst = Join-Path $github "config\manifests"
if (-not (Test-Path $manifestDst)) {
    New-Item -ItemType Directory -Path $manifestDst -Force | Out-Null
}
Copy-Item -LiteralPath (Join-Path $candidate "config\manifests\triage_rules_manifest.json") `
    -Destination (Join-Path $manifestDst "triage_rules_manifest.json") -Force

@(
    "test_uve_deep_flow.py",
    "test_ui_navigation_recovery.py"
) | ForEach-Object {
    Copy-Item -LiteralPath (Join-Path $candidate "tests\$_") `
        -Destination (Join-Path $github "tests\$_") -Force
}

$fixtureDst = Join-Path $github "fixtures"
if (-not (Test-Path $fixtureDst)) {
    New-Item -ItemType Directory -Path $fixtureDst -Force | Out-Null
}
Copy-Item -LiteralPath (Join-Path $candidate "fixtures\prepare_reference_sources.py") `
    -Destination (Join-Path $fixtureDst "prepare_reference_sources.py") -Force
@("build_probe.py", "state_probe.cpp") | ForEach-Object {
    $src = Join-Path $candidate "fixtures\workstation\$_"
    if (Test-Path $src) {
        $dir = Join-Path $fixtureDst "workstation"
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Copy-Item -LiteralPath $src -Destination (Join-Path $dir $_) -Force
    }
}

Copy-Item -LiteralPath (Join-Path $candidate "config\uve\uve_witness_manifest.json") `
    -Destination (Join-Path $github "config\uve\uve_witness_manifest.json") -Force

# Standalone github repo must not ship ProTrainer-specific trees.
@(
    "config\uve\targets\protrainer",
    "config\uve\fixtures\deep_protrainer_comments",
    "config\uve\fixtures\deep_protrainer_video",
    "fixtures\protrainer"
) | ForEach-Object {
    $path = Join-Path $github $_
    if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
@(
    "config\uve\profiles\protrainer_development.json",
    "config\uve\profiles\deep_protrainer_comments.json",
    "config\uve\profiles\deep_protrainer_video.json"
) | ForEach-Object {
    $path = Join-Path $github $_
    if (Test-Path $path) { Remove-Item -LiteralPath $path -Force }
}

Write-Host "Sync complete: $github"
