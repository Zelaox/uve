param(
    [Parameter(Mandatory=$true)][string]$CandidateRoot,
    [Parameter(Mandatory=$true)][string]$LabRoot
)
$ErrorActionPreference='Stop'
python -B (Join-Path $PSScriptRoot 'sync_from_sources.py') --candidate-root $CandidateRoot --lab-root $LabRoot
if ($LASTEXITCODE -ne 0) { throw 'Source export failed; inspect its output before staging files.' }
