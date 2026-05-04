@echo off
setlocal

cd /d C:\Users\Lenovo\PycharmProjects\F\f3

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set RUN_TS=%%i

python -u scripts\remote_codex_bot.py 1>> logs\remote_codex_bot.%RUN_TS%.out.log 2>> logs\remote_codex_bot.%RUN_TS%.err.log
