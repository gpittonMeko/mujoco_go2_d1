@echo off
REM Apre PowerShell elevato e lancia fix_windows_printing_admin.ps1
REM IMPORTANTE: spegni la stampante HP ENVY prima (se no Windows la reinstalla via rete).

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_fix_printing_elevated.ps1"
echo.
echo Fine. Premi un tasto.
pause >nul
