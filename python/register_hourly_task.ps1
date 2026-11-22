# Registers (or re-registers) the Windows Task Scheduler task "NordicCart Hourly Pipeline" - DISABLED.
# Hourly at minute 5, current user, runs python\run_pipeline_hourly.cmd; no overlapping instances; 50-minute limit.
# Enable only after explicit authorization:  Enable-ScheduledTask -TaskName "NordicCart Hourly Pipeline"
$ErrorActionPreference = "Stop"
$name = "NordicCart Hourly Pipeline"
$repo = Split-Path -Parent $PSScriptRoot
$start = (Get-Date).Date.AddHours((Get-Date).Hour + 1).AddMinutes(5)
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$repo\python\run_pipeline_hourly.cmd`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 50) `
    -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$task = Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "NordicCart hourly pipeline: engine tick + ETL (raw, cleaned, analytics). Disabled until authorized." -Force
Disable-ScheduledTask -TaskName $name | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
