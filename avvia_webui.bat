@echo off
rem inc-bench: avvia la web UI. Se il processo si chiude da solo, lo riavvia e annota il motivo in data\webui_eventi.log.
title inc-bench web UI
cd /d "%~dp0"
if not exist data mkdir data
rem Python: variabile PYTHON se impostata, altrimenti il launcher "py", altrimenti "python" nel PATH
if defined PYTHON (set "PY=%PYTHON%") else (where py >nul 2>nul && (set "PY=py -3") || (set "PY=python"))
set ARGS=

:loop
echo [%date% %time%] avvio web UI >> data\webui_eventi.log
%PY% -u -X faulthandler webui.py %ARGS%
echo [%date% %time%] web UI uscita con codice %errorlevel% >> data\webui_eventi.log
echo.
echo La web UI si e' chiusa (codice %errorlevel%). Riavvio fra 5 secondi... (chiudi questa finestra per uscire)
set ARGS=--no-browser
timeout /t 5 /nobreak >nul
goto loop
