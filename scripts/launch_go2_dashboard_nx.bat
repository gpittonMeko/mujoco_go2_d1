@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."
set PYTHONUTF8=1
title Go2 Dashboard NX — avvio
echo.
echo === Go2 Dashboard su NX (deploy + log) ===
echo Cartella repo: %CD%
echo.
python "%~dp0launch_go2_dashboard_nx.py" %*
set ERR=%ERRORLEVEL%
echo.
if %ERR% neq 0 (
  echo Terminato con codice %ERR%.
) else (
  echo Terminato OK.
)
echo Premi un tasto per chiudere...
pause >nul
endlocal
exit /b %ERR%
