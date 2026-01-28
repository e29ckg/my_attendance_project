import os, json, shutil, time, pandas as pd, mysql.connector, smtplib
from datetime import datetime
from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn

# --- CONFIG & DB ---
def load_config():
    if not os.path.exists("config.json"): return {}
    with open("config.json", 'r', encoding='utf-8') as f: return json.load(f)

current_config = load_config()

def get_db_conn():
    try:
        return mysql.connector.connect(
            host=current_config.get("db_host", "localhost"),
            user=current_config.get("db_user", "root"),
            password=current_config.get("db_password", ""),
            database=current_config.get("db_name", "attendance_system")
        )
    except: return None

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# สร้างโฟลเดอร์อัตโนมัติถ้ายังไม่มี
os.makedirs("attendance_images", exist_ok=True)
os.makedirs("images", exist_ok=True)

app.mount("/attendance_images", StaticFiles(directory="attendance_images"), name="attendance_images")
app.mount("/images", StaticFiles(directory="images"), name="images")

# --- PAGE ROUTES ---
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/manage_users", response_class=HTMLResponse)
async def manage_users(request: Request):
    return templates.TemplateResponse("user_management.html", {"request": request})

# --- API: DASHBOARD STATS ---
@app.get("/api/daily_stats")
async def get_stats():
    conn = get_db_conn()
    if not conn: return {"total": 0, "present": 0, "absent": 0}
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT COUNT(*) as total FROM employees")
    total = cur.fetchone()['total']
    cur.execute("SELECT COUNT(DISTINCT employee_id) as present FROM attendance_logs WHERE DATE(check_time) = CURDATE()")
    present = cur.fetchone()['present']
    conn.close()
    return {"total": total, "present": present, "absent": max(0, total-present)}

@app.get("/api/current_shifts") # เพิ่ม: สำหรับแสดงตารางเวรหน้า Dashboard
async def get_current_shifts():
    conn = get_db_conn()
    if not conn: return []
    cur = conn.cursor(dictionary=True)
    # Query นี้จะดูว่าวันนี้ใครมีเวร และสแกนมาหรือยัง
    query = """
        SELECT e.employee_id, e.name, s.role_name, 
               (SELECT status FROM attendance_logs 
                WHERE employee_id = e.employee_id AND DATE(check_time) = CURDATE() 
                ORDER BY check_time DESC LIMIT 1) as current_status
        FROM employees e
        JOIN employee_schedules s ON e.employee_id = s.employee_id
        WHERE CURDATE() BETWEEN s.start_date AND s.end_date
    """
    cur.execute(query)
    data = cur.fetchall()
    conn.close()
    return data

# --- API: EMPLOYEE MANAGEMENT ---
@app.get("/api/employees") # เพิ่ม: เพื่อให้ตารางรายชื่อพนักงานแสดงผล
async def get_employees():
    conn = get_db_conn()
    if not conn: return []
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM employees")
    data = cur.fetchall()
    conn.close()
    return data

@app.post("/api/register")
async def register(name:str=Form(...), emp_id:str=Form(...), role:str=Form(...), file:UploadFile=File(...)):
    path = f"images/{emp_id}.jpg"
    with open(path, "wb") as b: shutil.copyfileobj(file.file, b)
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO employees (employee_id, name, role, image_path) VALUES (%s,%s,%s,%s) 
                   ON DUPLICATE KEY UPDATE name=%s, role=%s, image_path=%s""", 
                (emp_id, name, role, path, name, role, path))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.delete("/api/employees/delete/{emp_id}") # เพิ่ม: ปุ่มลบพนักงาน
async def delete_employee(emp_id: str):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM employees WHERE employee_id = %s", (emp_id,))
    conn.commit(); conn.close()
    return {"status": "success"}

# [ในไฟล์ main_web.py]

@app.post("/api/employees/update")
async def update_employee(
    emp_id: str = Form(...),
    name: str = Form(...),
    role: str = Form(...),
    file: UploadFile = File(None) # เพิ่ม: รับไฟล์ (ใส่ None คือไม่บังคับ)
):
    conn = get_db_conn()
    cur = conn.cursor()
    
    # 1. อัปเดตข้อมูลชื่อและตำแหน่งก่อน
    cur.execute("UPDATE employees SET name=%s, role=%s WHERE employee_id=%s", (name, role, emp_id))
    
    # 2. ถ้ามีการแนบไฟล์รูปมาด้วย ให้บันทึกทับรูปเดิม
    if file:
        path = f"images/{emp_id}.jpg"
        with open(path, "wb") as b:
            shutil.copyfileobj(file.file, b)
        # อัปเดต path ใน DB เพื่อความชัวร์
        cur.execute("UPDATE employees SET image_path=%s WHERE employee_id=%s", (path, emp_id))
    
    conn.commit()
    conn.close()
    return {"status": "success"}

# --- API: WORK RULES & SHIFTS ---
@app.get("/api/work_rules") # เพิ่ม: ดึงข้อมูลกะไปใส่ Dropdown
async def get_work_rules():
    conn = get_db_conn()
    if not conn: return []
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM work_rules")
    data = cur.fetchall()
    conn.close()
    return data

@app.post("/api/work_rules/save") # เพิ่ม: บันทึกกะใหม่
async def save_work_rule(role_name: str = Form(...), start_time: str = Form(...), late_threshold: str = Form(...), end_time: str = Form(...)):
    conn = get_db_conn(); cur = conn.cursor()
    sql = """INSERT INTO work_rules (role_name, start_time, late_threshold, end_time) 
             VALUES (%s, %s, %s, %s) 
             ON DUPLICATE KEY UPDATE start_time=%s, late_threshold=%s, end_time=%s"""
    cur.execute(sql, (role_name, start_time, late_threshold, end_time, start_time, late_threshold, end_time))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.delete("/api/work_rules/delete/{role_name}")
async def delete_work_rule(role_name: str):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM work_rules WHERE role_name = %s", (role_name,))
    conn.commit(); conn.close()
    return {"status": "success"}

# --- API: SCHEDULE ASSIGNMENT ---
@app.get("/api/schedules") # เพิ่ม: ดึงตารางเวรมาแสดง
async def get_schedules():
    conn = get_db_conn()
    if not conn: return []
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM employee_schedules ORDER BY start_date DESC")
    data = cur.fetchall()
    conn.close()
    return data

@app.post("/api/schedule/assign")
async def assign_shift(emp_id:str=Form(...), role_name:str=Form(...), start_date:str=Form(...), end_date:str=Form(...)):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO employee_schedules (employee_id, role_name, start_date, end_date) VALUES (%s,%s,%s,%s)",
                (emp_id, role_name, start_date, end_date))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.delete("/api/schedules/delete/{schedule_id}")
async def delete_schedule(schedule_id: int):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM employee_schedules WHERE id = %s", (schedule_id,))
    conn.commit(); conn.close()
    return {"status": "success"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9876)