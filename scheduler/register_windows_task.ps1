# Ежедневный запуск update.py в Планировщике Windows.
# powershell -ExecutionPolicy Bypass -File scheduler\register_windows_task.ps1
# Удаление: Unregister-ScheduledTask -TaskName "goszakup-daily-update" -Confirm:$false

$project = Split-Path -Parent $PSScriptRoot
$python  = (Get-Command python).Source
$log     = Join-Path $project "logs\update.log"
New-Item -ItemType Directory -Force (Join-Path $project "logs") | Out-Null

$action  = New-ScheduledTaskAction -Execute "cmd.exe" `
           -Argument "/c chcp 65001 >nul && `"$python`" update.py >> `"$log`" 2>&1" `
           -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At 3:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName "goszakup-daily-update" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Инкрементальная загрузка OWS v3 и пересчёт витрин" -Force
Write-Host "Задача зарегистрирована: ежедневно в 03:00, лог — $log"
