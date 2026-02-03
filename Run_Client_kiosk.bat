@echo off
title AI Face Scanner Station
cd /d "D:\project\my_attendance_project"
call venv\Scripts\activate
python client_kiosk.py
pause