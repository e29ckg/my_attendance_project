@echo off
cd /d "D:\project\my_attendance_project"

:: เปิด Web Server ย่อหน้าต่างลง
start "Web Server" /min cmd /c "call venv\Scripts\activate && python server\server_api.py"

:: รอ 5 วินาทีให้เว็บรันเสร็จก่อน
timeout /t 5

:: เปิด Scanner หน้าจอปกติ
start "Face Scanner" cmd /c "call venv\Scripts\activate && python client\client_kiosk.py && pause"