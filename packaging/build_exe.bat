@echo off
rem ============================================================
rem  WinRemote App - one-click build (thin wrapper,
rem  forwards all args to build_exe.py)
rem  Usage (default, windowless background runner):
rem         double-click build_exe.bat
rem  More options: build_exe.bat --help
rem  Output: dist\winremote-app.exe
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 packaging\build_exe.py %*
) else (
    python packaging\build_exe.py %*
)

if errorlevel 1 (
    echo [ERROR] build failed
) else (
    echo.
    echo Done. Output in dist\ folder.
)
pause
endlocal
