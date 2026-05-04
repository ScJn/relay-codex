@echo off
setlocal

cd /d C:\Users\Lenovo\PycharmProjects\F\f3

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set BOT_LOCK=%CD%\.remote_codex_bot.lock

if not exist logs mkdir logs

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set RUN_TS=%%i

if not exist "%BOT_LOCK%" goto start_bot

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference = 'SilentlyContinue';" ^
    "$lockPath = $env:BOT_LOCK;" ^
    "$botPidText = (Get-Content -Encoding UTF8 -Raw -LiteralPath $lockPath).Trim();" ^
    "if ($botPidText -match '^\d+$') {" ^
    "  $botPid = [int]$botPidText;" ^
    "  $process = Get-CimInstance Win32_Process -Filter \"ProcessId=$botPid\";" ^
    "  if ($process -and $process.CommandLine -match 'remote_codex_bot\.py') {" ^
    "    taskkill /PID $botPid /T /F | Out-Null;" ^
    "    Wait-Process -Id $botPid -Timeout 10;" ^
    "  }" ^
    "}" ^
    "Remove-Item -Force -LiteralPath $lockPath"

:start_bot

python -u scripts\remote_codex_bot.py 1>> logs\remote_codex_bot.%RUN_TS%.out.log 2>> logs\remote_codex_bot.%RUN_TS%.err.log
