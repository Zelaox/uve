param([string]$LabRoot = (Join-Path $PSScriptRoot '../..'), [switch]$Register)

$ErrorActionPreference = 'Stop'
$lab = (Resolve-Path -LiteralPath $LabRoot).Path
$storage = Get-Content -Raw -LiteralPath (Join-Path $lab 'config/uve_triage_storage_manifest.json') | ConvertFrom-Json
$lifecycle = Get-Content -Raw -LiteralPath (Join-Path $lab $storage.storage_lifecycle_ref) | ConvertFrom-Json
$schedule = Get-Content -Raw -LiteralPath (Join-Path $lab $lifecycle.scheduler_ref) | ConvertFrom-Json
$platform = Get-Content -Raw -LiteralPath (Join-Path $lab $lifecycle.windows_platform_ref) | ConvertFrom-Json
if ($schedule.action.operation -ne 'scan' -or $schedule.action.mode -ne 'dry_run' -or
    $schedule.action.mode -ne $lifecycle.default_mode -or $schedule.trigger.type -ne 'Daily') {
    throw 'Task installation is limited to the declared daily dry-run inventory.'
}
if ($platform.storage_sense_authority -or $platform.windows_idle_is_lab_activity) {
    throw 'Windows idle conditions cannot govern Lab activity or retention.'
}
$runner = (Resolve-Path -LiteralPath (Join-Path $lab $schedule.runner_ref)).Path
if (-not $runner.StartsWith($lab + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The runner must be under the Lab root.'
}
$time = [DateTime]::ParseExact($schedule.trigger.at_local, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture).ToString('HH:mm:ss')
$start = [DateTime]::ParseExact($schedule.trigger.start_date_local, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture).ToString('yyyy-MM-dd')
$shell = [Security.SecurityElement]::Escape($schedule.shell_command)
$workingDir = [Security.SecurityElement]::Escape($lab)
$argv = [Security.SecurityElement]::Escape("-NoProfile -NonInteractive -File `"$runner`" -LabRoot `"$lab`"")
$principal = [Security.SecurityElement]::Escape([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$idle = ([bool]$schedule.conditions.run_only_if_idle).ToString().ToLowerInvariant()
$available = ([bool]$schedule.conditions.start_when_available).ToString().ToLowerInvariant()
$acOnly = (-not [bool]$schedule.conditions.run_on_battery).ToString().ToLowerInvariant()
$xml = @"
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><CalendarTrigger><StartBoundary>${start}T$time</StartBoundary><Enabled>true</Enabled>
    <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id="Inventory"><UserId>$principal</UserId><LogonType>$($platform.logon_type)</LogonType>
    <RunLevel>$($platform.run_level)</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>$acOnly</DisallowStartIfOnBatteries><RunOnlyIfIdle>$idle</RunOnlyIfIdle>
    <StartWhenAvailable>$available</StartWhenAvailable><Enabled>true</Enabled></Settings>
  <Actions Context="Inventory"><Exec><Command>$shell</Command><Arguments>$argv</Arguments>
    <WorkingDirectory>$workingDir</WorkingDirectory></Exec></Actions>
</Task>
"@
[void][xml]$xml
if ($Register) {
    if (-not $lifecycle.enabled) {
        throw 'Lifecycle activation is disabled by the Lab manifest.'
    }
    Register-ScheduledTask -TaskName $schedule.task_name -Xml $xml -Force
} else {
    $xml
}
