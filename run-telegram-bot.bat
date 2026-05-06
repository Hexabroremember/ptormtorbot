@echo off
setlocal
cd /d "%~dp0"
REM Stop any python already running THIS bot module (avoids Telegram getUpdates Conflict).
REM Matches "app.telegram_bot" in the process command line (works even if path has no "ptormtorbot").
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | Where-Object { $c = $_.CommandLine; $c -and ($c -match 'app\.telegram_bot') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 4 /nobreak >nul
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m app.telegram_bot
) else (
  python -m app.telegram_bot
)
if errorlevel 1 pause
endlocal
