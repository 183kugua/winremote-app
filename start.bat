@echo off
rem WinRemote App daemon launcher (connect B-end; dev via source)
cd /d "%~dp0"
set PYTHONPATH=src
".venv\Scripts\python.exe" -m winremote_app --daemon -c config.yaml
if errorlevel 1 pause
