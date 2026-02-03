@echo off
title AI Face System [SERVER]
cd /d "%~dp0"

echo ====================================================
echo   AI Face Recognition Server Launcher
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
echo [INFO] Your Local IP Address:
ipconfig | findstr "IPv4"
echo.
echo ----------------------------------------------------
echo  Server is starting on Port 9876...
echo  Press Ctrl+C to Stop
echo ----------------------------------------------------
echo.

:: 2. รัน Server
python server_api.py

:: 3. ถ้าโปรแกรมปิดเอง (Crash) ให้หยุดหน้าจอไว้ดู Error
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Server stopped unexpectedly. Please check the error message above.
)

pause