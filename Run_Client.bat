@echo off
title AI Face Scanner [CLIENT KIOSK]
cd /d "%~dp0"

echo ====================================================
echo   AI Face Client Kiosk Launcher
echo ====================================================

:: 1. ตรวจสอบและ Activate venv
if exist venv\Scripts\activate.bat (
    echo [INFO] Found Virtual Environment. Activating...
    call venv\Scripts\activate
) else (
    echo [WARNING] "venv" folder not found!
    echo System will try to use Global Python instead.
)

echo.
echo [INFO] Launching Kiosk Interface...
echo ----------------------------------------------------

:: 2. รันโปรแกรม Client
python client_kiosk.py

:: 3. ถ้าโปรแกรมปิดเอง (Crash) ให้แจ้งเตือน
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Client stopped unexpectedly.
    echo Please check the error message above.
)

pause