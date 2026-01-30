import os, json, shutil, time, sqlite3
import pandas as pd
from datetime import datetime
from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn

# --- CONFIG & DB ---
DB_FILE = "attendance.db"

def get_db_conn():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn
    except: return None

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# สร้างโฟลเดอร์ที่จำเป็น
os.makedirs("attendance_images", exist_ok=True)
os.makedirs("images", exist_ok=True)

app.mount("/attendance_images", StaticFiles(directory="attendance_images"), name="attendance_images")
app.mount("/images", StaticFiles(directory="images"), name="images")

# --- FRONTEND PAGES ---
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/manage_users", response_class=HTMLResponse)
async def manage_users(request: Request):
    return templates.TemplateResponse("user_management.html", {"request": request})

# --- API: DASHBOARD & STATS ---
@app.get("/api/daily_stats")
async def get_stats():
    conn = get_db_conn()
    if not conn: return {"total": 0, "present": 0, "absent": 0}
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) as total FROM employees")
    total = cur.fetchone()['total']
    
    # นับคนที่มาทำงานวันนี้ (นับเฉพาะคนที่มี log_type='IN')
    cur.execute("SELECT COUNT(DISTINCT employee_id) as present FROM attendance_logs WHERE date(check_time) = date('now', 'localtime')")
    present = cur.fetchone()['present']
    
    conn.close()
    return {"total": total, "present": present, "absent": max(0, total-present)}

@app.get("/api/current_shifts")
async def get_current_shifts():
    """แสดงตารางเวรวันนี้ และสถานะล่าสุดว่าเข้าหรือออก"""
    conn = get_db_conn()
    if not conn: return []
    cur = conn.cursor()
    # ดึงข้อมูลคนที่เข้าเวรวันนี้ + สถานะล่าสุด (IN/OUT)
    query = """
        SELECT e.employee_id, e.name, s.role_name, 
               (SELECT log_type FROM attendance_logs 
                WHERE employee_id = e.employee_id AND date(check_time) = date('now', 'localtime') 
                ORDER BY id DESC LIMIT 1) as current_status,
               (SELECT status FROM attendance_logs 
                WHERE employee_id = e.employee_id AND date(check_time) = date('now', 'localtime') 
                ORDER BY id DESC LIMIT 1) as remark
        FROM employees e
        JOIN employee_schedules s ON e.employee_id = s.employee_id
        WHERE date('now', 'localtime') BETWEEN s.start_date AND s.end_date
    """
    cur.execute(query)
    data = cur.fetchall()
    conn.close()
    return data

# --- API: EMPLOYEES ---
@app.get("/api/employees")
async def get_employees():
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees")
    data = cur.fetchall(); conn.close()
    return data

@app.post("/api/register")
async def register(name:str=Form(...), emp_id:str=Form(...), role:str=Form(...), file:UploadFile=File(...)):
    path = f"images/{emp_id}.jpg"
    with open(path, "wb") as b: shutil.copyfileobj(file.file, b)
    
    conn = get_db_conn(); cur = conn.cursor()
    # เมื่ออัปโหลดรูปใหม่ ให้เคลียร์ embedding เป็น NULL เพื่อให้ Scanner คำนวณใหม่
    cur.execute("""
        INSERT OR REPLACE INTO employees (employee_id, name, role, image_path, embedding) 
        VALUES (?, ?, ?, ?, NULL)
    """, (emp_id, name, role, path))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/api/employees/update")
async def update_employee(emp_id: str=Form(...), name: str=Form(...), role: str=Form(...), file: UploadFile=File(None)):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("UPDATE employees SET name=?, role=? WHERE employee_id=?", (name, role, emp_id))
    
    if file:
        path = f"images/{emp_id}.jpg"
        with open(path, "wb") as b: shutil.copyfileobj(file.file, b)
        # เคลียร์ค่า AI เพื่อให้คำนวณใหม่จากรูปใหม่
        cur.execute("UPDATE employees SET image_path=?, embedding=NULL WHERE employee_id=?", (path, emp_id))
    
    conn.commit(); conn.close()
    return {"status": "success"}

@app.delete("/api/employees/delete/{emp_id}")
async def delete_employee(emp_id: str):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM employees WHERE employee_id = ?", (emp_id,))
    conn.commit(); conn.close()
    return {"status": "success"}

# --- API: WORK RULES (SHIFTS) ---
@app.get("/api/work_rules")
async def get_work_rules():
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM work_rules")
    data = cur.fetchall(); conn.close()
    return data

@app.post("/api/work_rules/save")
async def save_work_rule(role_name: str=Form(...), start_time: str=Form(...), late_threshold: str=Form(...), end_time: str=Form(...)):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("""INSERT OR REPLACE INTO work_rules (role_name, start_time, late_threshold, end_time) 
                   VALUES (?, ?, ?, ?)""", (role_name, start_time, late_threshold, end_time))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.delete("/api/work_rules/delete/{role_name}")
async def delete_work_rule(role_name: str):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM work_rules WHERE role_name = ?", (role_name,))
    conn.commit(); conn.close()
    return {"status": "success"}

# --- API: SCHEDULES ---
@app.get("/api/schedules")
async def get_schedules():
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM employee_schedules ORDER BY start_date DESC")
    data = cur.fetchall(); conn.close()
    return data

@app.post("/api/schedule/assign")
async def assign_shift(emp_id:str=Form(...), role_name:str=Form(...), start_date:str=Form(...), end_date:str=Form(...)):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO employee_schedules (employee_id, role_name, start_date, end_date) VALUES (?,?,?,?)",
                (emp_id, role_name, start_date, end_date))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.delete("/api/schedules/delete/{schedule_id}")
async def delete_schedule(schedule_id: int):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM employee_schedules WHERE id = ?", (schedule_id,))
    conn.commit(); conn.close()
    return {"status": "success"}

# --- API: EXPORT & MANUAL ---
@app.get("/api/report/export_excel")
async def export_excel():
    """ดึงข้อมูล Logs ทั้งหมดออกมาเป็น Excel"""
    try:
        conn = get_db_conn()
        query = """
            SELECT employee_id, employee_name, check_time, log_type, status 
            FROM attendance_logs 
            ORDER BY check_time DESC
        """
        # ใช้ pandas อ่าน SQL และสร้าง Excel
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        filename = f"Attendance_Report_{datetime.now().strftime('%Y%m%d')}.xlsx"
        df.to_excel(filename, index=False)
        return FileResponse(filename, filename=filename)
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/attendance/manual")
async def manual_attendance(emp_id: str = Form(...), log_type: str = Form(...), check_time: str = Form(...), reason: str = Form(...)):
    """สำหรับ Admin บันทึกมือ (กรณีลืมสแกน/ลาป่วย)"""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM employees WHERE employee_id = ?", (emp_id,))
    emp = cur.fetchone()
    if emp:
        cur.execute("""
            INSERT INTO attendance_logs (employee_id, employee_name, check_time, log_type, status, evidence_image) 
            VALUES (?, ?, ?, ?, ?, ?)
        """, (emp_id, emp['name'], check_time, log_type, reason, 'manual_admin'))
        conn.commit()
    conn.close()
    return {"status": "success"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9876)