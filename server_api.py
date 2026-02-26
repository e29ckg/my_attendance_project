import uvicorn
import shutil
import os
import sqlite3
import cv2
import numpy as np
import threading
import requests
import json
import psutil
import time
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from deepface import DeepFace
import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv

# --- CONFIG LOADING ---
load_dotenv()
DB_FILE = os.getenv("DB_FILE", "attendance.db")
THRESHOLD = float(os.getenv("THRESHOLD", 0.3))
ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "False").lower() == "true"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
KEEP_IMAGE_DAYS = int(os.getenv("KEEP_IMAGE_DAYS", 60))
SERVER_PORT = int(os.getenv("PORT", 9876))
SERVER_HOST = os.getenv("HOST", "0.0.0.0")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("images", exist_ok=True)
os.makedirs("attendance_images", exist_ok=True)

app.mount("/images", StaticFiles(directory="images"), name="images")
app.mount("/attendance_images", StaticFiles(directory="attendance_images"), name="attendance_images")

# Global Cache
known_embeddings = []
known_ids = []
known_names = []

# --- ADMIN AUTHENTICATION ---
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "123456")
security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, ADMIN_USER)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# --- จัดการกรณี Login หน้า Admin ไม่ผ่าน (กด Cancel) ---
@app.exception_handler(HTTPException)
async def auth_exception_handler(request: Request, exc: HTTPException):
    # ถ้าเป็น Error 401 (ไม่ได้ล็อกอิน หรือกด Cancel)
    if exc.status_code == 401:
        # ส่ง Header ไปบอกเบราว์เซอร์ให้เปิดหน้าต่างกรอกรหัส
        headers = exc.headers or {"WWW-Authenticate": "Basic"}
        
        # ถ้าผู้ใช้กดยกเลิก เบราว์เซอร์จะอ่านโค้ดบรรทัดนี้ และเด้งกลับไปที่หน้า / (Webscan) ทันที
        html_redirect = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta http-equiv="refresh" content="0; url=/" />
            <script>window.location.replace("/");</script>
        </head>
        <body style="background:#2c3e50; color:white; text-align:center; padding:50px; font-family:sans-serif;">
            <h3>กำลังกลับสู่หน้าหลัก...</h3>
        </body>
        </html>
        """
        return HTMLResponse(content=html_redirect, status_code=401, headers=headers)
    
    # ถ้าเป็น Error อื่นๆ ให้คืนค่าเป็น JSON ปกติ
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

# --- DATABASE & INIT ---
def get_db_conn():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn
    except: return None

def init_system():
    conn = get_db_conn()
    if conn:
        cur = conn.cursor()
        
        # 1. ตารางพนักงาน (เพิ่ม department)
        # ตรวจสอบว่ามี column department หรือยัง ถ้าไม่มีให้เพิ่ม
        cur.execute("""CREATE TABLE IF NOT EXISTS employees (
            employee_id TEXT PRIMARY KEY, 
            name TEXT, 
            role TEXT, 
            department TEXT,  -- [ใหม่] ตำแหน่ง/แผนก เช่น หัวหน้าวิศวะ
            image_path TEXT, 
            embedding TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        
        # Check column department exists (Migration logic simple)
        try:
            cur.execute("SELECT department FROM employees LIMIT 1")
        except:
            print(">>> 🛠️ Migrating DB: Adding 'department' column...")
            cur.execute("ALTER TABLE employees ADD COLUMN department TEXT")

        # 2. ตาราง Logs
        cur.execute("""CREATE TABLE IF NOT EXISTS attendance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            employee_id TEXT, 
            employee_name TEXT, 
            check_time DATETIME, 
            evidence_image TEXT, 
            log_type TEXT DEFAULT 'SCAN', 
            status TEXT DEFAULT '-'
        )""")

        try:
            cur.execute("ALTER TABLE attendance_logs ADD COLUMN client_ip TEXT")
        except:
            pass
        
        # 3. ตาราง Remarks
        cur.execute("""CREATE TABLE IF NOT EXISTS daily_remarks (
            date_str TEXT, 
            employee_id TEXT, 
            remark TEXT, 
            PRIMARY KEY (date_str, employee_id)
        )""")

        # 4. ตาราง Roles (ประเภทพนักงาน)
        cur.execute("""CREATE TABLE IF NOT EXISTS roles (role_name TEXT PRIMARY KEY)""")
        
        # 5. [ใหม่] ตาราง Departments (ตำแหน่งงาน)
        cur.execute("""CREATE TABLE IF NOT EXISTS departments (dep_name TEXT PRIMARY KEY)""")

        # Seed Data (ข้อมูลเริ่มต้น)
        # default_roles = ["พนักงานทั่วไป", "วิศวะ", "แม่บ้าน", "รปภ.", "ธุรการ"]
        # for r in default_roles:
        #     cur.execute("INSERT OR IGNORE INTO roles (role_name) VALUES (?)", (r,))
            
        # default_deps = ["หัวหน้าวิศวะ", "ช่างไฟฟ้า", "ช่างทั่วไป", "หัวหน้าแม่บ้าน", "แม่บ้าน", "เจ้าหน้าที่รปภ."]
        # for d in default_deps:
        #     cur.execute("INSERT OR IGNORE INTO departments (dep_name) VALUES (?)", (d,))

        conn.commit()
        conn.close()
    
    load_faces()

def load_faces():
    global known_embeddings, known_ids, known_names
    print(">>> 🔄 Loading AI Models & Faces...")
    conn = get_db_conn()
    if not conn: return
    cur = conn.cursor()
    cur.execute("SELECT employee_id, name, embedding FROM employees")
    rows = cur.fetchall()
    
    known_embeddings, known_ids, known_names = [], [], []
    for r in rows:
        if r['embedding']:
            try:
                known_embeddings.append(json.loads(r['embedding']))
                known_ids.append(r['employee_id'])
                known_names.append(r['name'])
            except: pass
    conn.close()
    print(f">>> ✅ Loaded {len(known_names)} faces.")

@app.on_event("startup")
async def startup_event():
    init_system()

# --- PAGE ROUTES ---
@app.get("/")
async def index(): 
    """เปลี่ยนหน้าหลักเป็นระบบสแกน Web Scanner"""
    return FileResponse("webscan.html")

# หน้าต่างๆ ที่ต้องใช้รหัสผ่าน (เพิ่ม Depends)
@app.get("/admin")
async def view_admin(username: str = Depends(verify_admin)): 
    return FileResponse("admin.html")

@app.get("/report")
async def view_report(username: str = Depends(verify_admin)): 
    return FileResponse("report_daily.html")

@app.get("/monitor")
async def view_monitor(username: str = Depends(verify_admin)): 
    return FileResponse("monitor.html")

@app.get("/print")
async def view_print(username: str = Depends(verify_admin)): 
    return FileResponse("report_print.html")

@app.get("/health")
async def health_check():
    """API สำหรับเช็คว่า Server ยังรอดอยู่ไหม"""
    return {"status": "online"}

@app.get("/webscan")
async def view_webscan():
    """เปิดหน้าระบบสแกนใบหน้าผ่าน Web Browser"""
    return FileResponse("webscan.html")



# --- UTILS ---
# 1. เพิ่ม client_ip="Unknown" ตรงวงเล็บนี้ครับ 👇
def send_telegram_thread(name, time_str, img_path, client_ip="Unknown"):
    if not ENABLE_TELEGRAM: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        
        # 2. ตอนนี้ระบบจะรู้จักตัวแปร client_ip แล้วครับ
        caption = f"✅ <b>ลงเวลาสำเร็จ</b>\n👤 <b>ชื่อ:</b> {name}\n⏰ <b>เวลา:</b> {time_str}\n🌐 <b>IP เครื่อง:</b> {client_ip}"
        
        with open(img_path, 'rb') as f:
            requests.post(url, files={'photo': f}, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'})
            
    except Exception as e: 
        print(f"Telegram Error: {e}")

# เพิ่ม parameter client_ip
def save_log(emp_id, name, frame, type="SCAN", client_ip="Unknown"):
    now = datetime.now()
    conn = get_db_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("SELECT check_time FROM attendance_logs WHERE employee_id=? ORDER BY id DESC LIMIT 1", (emp_id,))
        last = cur.fetchone()
        if last:
            last_time = datetime.strptime(last['check_time'], "%Y-%m-%d %H:%M:%S.%f")
            if (now - last_time).total_seconds() < 60: return

        if not os.path.exists("attendance_images"): os.makedirs("attendance_images")
        img_path = f"attendance_images/{emp_id}_{now.strftime('%H%M%S')}.jpg"
        
        # ==========================================
        # 🟢 เพิ่มส่วนนี้: ฝังลายน้ำ (เวลา และ IP) ลงบนรูป
        # ==========================================
        timestamp_str = now.strftime('%Y-%m-%d %H:%M:%S')
        watermark_text = f"Time: {timestamp_str} | IP: {client_ip}"
        
        # ตั้งค่าฟอนต์
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.5
        thickness = 1
        
        # คำนวณขนาดข้อความเพื่อวาดกรอบพื้นหลังสีดำ (ให้อ่านง่ายขึ้น)
        (text_w, text_h), _ = cv2.getTextSize(watermark_text, font, font_scale, thickness)
        
        # พิกัดสำหรับวาด (มุมซ้ายล่างของภาพ)
        x, y = 10, frame.shape[0] - 15
        
        # วาดกล่องดำทึบเป็นพื้นหลัง (ป้องกันกลืนกับสีเสื้อหรือฉากหลัง)
        cv2.rectangle(frame, (x - 5, y - text_h - 5), (x + text_w + 5, y + 5), (0, 0, 0), -1)
        
        # วาดตัวหนังสือสีเขียวมะนาวทับลงไป
        cv2.putText(frame, watermark_text, (x, y), font, font_scale, (0, 255, 0), thickness, cv2.LINE_AA)
        # ==========================================

        # บันทึกรูปลงโฟลเดอร์ (รูปนี้จะมีลายน้ำติดไปด้วย)
        cv2.imwrite(img_path, frame)
        
        status_txt = "บันทึกแล้ว" if type == "SCAN" else "บันทึกมือ"
        
        # อัปเดตคำสั่ง Insert ให้มี client_ip
        cur.execute("INSERT INTO attendance_logs (employee_id, employee_name, check_time, evidence_image, log_type, status, client_ip) VALUES (?,?,?,?,?,?,?)",
                    (emp_id, name, now, img_path, type, status_txt, client_ip))
        conn.commit()

        if ENABLE_TELEGRAM:
            # ส่งค่า client_ip เข้า Thread ของ Telegram ด้วย (รูปที่ส่งไปก็จะมีลายน้ำด้วย)
            threading.Thread(target=send_telegram_thread, args=(f"{name} ({type})", now.strftime("%H:%M:%S"), img_path, client_ip)).start()
    except Exception as e: 
        print(f"DB Error: {e}")
    finally: 
        conn.close()

# --- CORE API ---
# เพิ่ม request: Request เข้าไปในวงเล็บตรงนี้ครับ 👇
@app.post("/scan")
async def scan_face(request: Request, file: UploadFile = File(...)):
    try:
        # ตอนนี้ระบบจะรู้จัก request แล้วครับ จะสามารถดึง IP ได้
        client_ip = request.headers.get('X-Forwarded-For', request.client.host)
        
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        objs = DeepFace.represent(img_path=frame, model_name="Facenet512", enforce_detection=False)
        found_name, status = "Unknown", "FAIL"
        
        if objs:
            target_emb = objs[0]["embedding"]
            min_dist, idx = 100, -1
            
            for i, known_emb in enumerate(known_embeddings):
                dist = 1 - (np.dot(target_emb, known_emb) / (np.linalg.norm(target_emb) * np.linalg.norm(known_emb)))
                if dist < min_dist: min_dist, idx = dist, i
            
            if min_dist < THRESHOLD and idx != -1:
                # ส่ง client_ip ไปให้ save_log บันทึกต่อ
                save_log(known_ids[idx], known_names[idx], frame, client_ip=client_ip)
                found_name = known_names[idx]
                status = "OK"
                
        return {"status": status, "name": found_name, "time": datetime.now().strftime("%H:%M:%S")}
    except: 
        return {"status": "ERROR", "name": "System Error"}

# 1. เพิ่ม request: Request เข้าไปในวงเล็บ 👇
@app.post("/manual_scan")
async def manual_scan(request: Request, employee_id: str = Form(...), file: UploadFile = File(...)):
    try:
        # 2. ดึง IP ของเครื่องที่กำลังใช้งาน
        client_ip = request.headers.get('X-Forwarded-For', request.client.host)
        
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT name FROM employees WHERE employee_id = ?", (employee_id,))
        emp = cur.fetchone()
        conn.close()
        
        if not emp: return {"status": "FAIL", "message": "ไม่พบรหัสพนักงาน"}
        
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        # --- [เพิ่มเช็ครูปเสียตรงนี้] ---
        if frame is None:
            return {"status": "ERROR", "message": "ไฟล์รูปภาพไม่ถูกต้องหรืออ่านไม่ได้"}
        # ---------------------------
        
        # 3. เพิ่ม client_ip=client_ip เข้าไปในวงเล็บของ save_log 👇
        save_log(employee_id, emp['name'], frame, type="MANUAL", client_ip=client_ip)
        
        return {"status": "OK", "name": emp['name'], "time": datetime.now().strftime("%H:%M:%S")}
    except Exception as e: 
        return {"status": "ERROR", "message": str(e)}

# --- EMPLOYEE MANAGEMENT ---

@app.get("/api/employees")
async def get_employees():
    conn = get_db_conn()
    cur = conn.cursor()
    # ดึง department มาด้วย
    cur.execute("SELECT employee_id, name, role, department, image_path FROM employees")
    rows = cur.fetchall()
    conn.close()
    return rows

@app.post("/api/register")
async def register(
    name: str = Form(...),
    emp_id: str = Form(...),
    role: str = Form(...),
    department: str = Form(...), # [ใหม่] รับค่า department
    file: UploadFile = File(...)
):
    try:
        file_path = f"images/{emp_id}.jpg"
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        embedding_json = None
        try:
            objs = DeepFace.represent(img_path=file_path, model_name="Facenet512", enforce_detection=False)
            if objs: embedding_json = json.dumps(objs[0]["embedding"])
        except: pass

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO employees (employee_id, name, role, department, image_path, embedding)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (emp_id, name, role, department, file_path, embedding_json))
        conn.commit()
        conn.close()

        load_faces()
        return {"status": "success", "message": f"ลงทะเบียน {name} เรียบร้อย"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.post("/api/employees/update")
async def update_employee(
    emp_id: str = Form(...),
    name: str = Form(...),
    role: str = Form(...),
    department: str = Form(...), # [ใหม่] รับค่า department
    file: Optional[UploadFile] = File(None)
):
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        if file:
            file_path = f"images/{emp_id}.jpg"
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            embedding_json = None
            try:
                objs = DeepFace.represent(img_path=file_path, model_name="Facenet512", enforce_detection=False)
                if objs: embedding_json = json.dumps(objs[0]["embedding"])
            except: pass
            
            cur.execute("""
                UPDATE employees SET name=?, role=?, department=?, image_path=?, embedding=? WHERE employee_id=?
            """, (name, role, department, file_path, embedding_json, emp_id))
        else:
            cur.execute("""
                UPDATE employees SET name=?, role=?, department=? WHERE employee_id=?
            """, (name, role, department, emp_id))

        conn.commit()
        conn.close()
        load_faces()
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.delete("/api/employees/delete/{emp_id}")
async def delete_employee(emp_id: str):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT image_path FROM employees WHERE employee_id = ?", (emp_id,))
        row = cur.fetchone()
        if row and row['image_path'] and os.path.exists(row['image_path']):
            os.remove(row['image_path'])
        
        cur.execute("DELETE FROM employees WHERE employee_id = ?", (emp_id,))
        conn.commit()
        conn.close()
        load_faces()
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}

# --- SETTINGS: ROLES & DEPARTMENTS ---

@app.get("/api/roles")
async def get_roles():
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT role_name FROM roles ORDER BY role_name")
    data = [r['role_name'] for r in cur.fetchall()]
    conn.close(); return data

@app.post("/api/roles")
async def add_role(role_name: str = Form(...)):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO roles (role_name) VALUES (?)", (role_name.strip(),))
    conn.commit(); conn.close(); return {"status": "success"}

@app.delete("/api/roles/{role_name}")
async def delete_role(role_name: str):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM roles WHERE role_name=?", (role_name,))
    conn.commit(); conn.close(); return {"status": "success"}

# [ใหม่] API สำหรับ Departments
@app.get("/api/departments")
async def get_departments():
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT dep_name FROM departments ORDER BY dep_name")
    data = [r['dep_name'] for r in cur.fetchall()]
    conn.close(); return data

@app.post("/api/departments")
async def add_department(dep_name: str = Form(...)):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO departments (dep_name) VALUES (?)", (dep_name.strip(),))
    conn.commit(); conn.close(); return {"status": "success"}

@app.delete("/api/departments/{dep_name}")
async def delete_department(dep_name: str):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM departments WHERE dep_name=?", (dep_name,))
    conn.commit(); conn.close(); return {"status": "success"}

# --- REPORTS ---
# (ส่วน report daily, remark, print, system status, cleanup เหมือนเดิม ใช้โค้ดเดิมได้เลยครับ หรือให้ผมแปะซ้ำบอกได้ครับ)
# เพื่อความกระชับ ผมละไว้ในฐานที่เข้าใจว่าเหมือนเดิมนะครับ แต่ถ้าจะให้แปะเต็มๆ บอกได้ครับ

# --- REPORT API (Updated for Department) ---
@app.get("/api/report/daily")
async def get_daily_report(date: str, role: str = "all"):
    conn = get_db_conn()
    if not conn: return []
    cur = conn.cursor()

    # ดึง Department มาโชว์ใน report ด้วย
    sql = "SELECT employee_id, name, role, department FROM employees"
    if role != "all":
        sql += " WHERE role = ?"
        cur.execute(sql, (role,))
    else:
        cur.execute(sql)
    employees = cur.fetchall()

    # ... (ส่วนดึง Logs เหมือนเดิม) ...
    cur.execute("SELECT employee_id, check_time, evidence_image FROM attendance_logs WHERE date(check_time) = ? ORDER BY check_time ASC", (date,))
    all_logs = cur.fetchall()
    
    logs_by_emp = {}
    for log in all_logs:
        eid = log['employee_id']
        if eid not in logs_by_emp: logs_by_emp[eid] = []
        logs_by_emp[eid].append({"time": log['check_time'], "img": log['evidence_image']})

    cur.execute("SELECT employee_id, remark FROM daily_remarks WHERE date_str = ?", (date,))
    remarks_map = {r['employee_id']: r['remark'] for r in cur.fetchall()}

    report_data = []
    for emp in employees:
        e_id, e_name = emp['employee_id'], emp['name']
        logs = logs_by_emp.get(e_id, [])
        time_in, img_in, time_out, img_out = "-", "", "-", ""

        if logs:
            try:
                t_in = logs[0]['time'].split(".")[0]
                time_in = datetime.strptime(t_in, "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S")
                img_in = logs[0]['img']
                if len(logs) > 1:
                    t_out = logs[-1]['time'].split(".")[0]
                    time_out = datetime.strptime(t_out, "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S")
                    img_out = logs[-1]['img']
            except: pass

        report_data.append({
            "employee_id": e_id, "name": e_name, "role": emp['role'],
            "department": emp['department'], # [ใหม่] ส่ง dep ไปหน้า report
            "time_in": time_in, "img_in": img_in, "time_out": time_out, "img_out": img_out,
            "remark": remarks_map.get(e_id, "")
        })
    conn.close()
    return report_data

@app.post("/api/report/remark")
async def update_remark(date: str = Form(...), employee_id: str = Form(...), remark: str = Form("")):
    try:
        conn = get_db_conn(); cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO daily_remarks (date_str, employee_id, remark) VALUES (?, ?, ?)", (date, employee_id, remark))
        conn.commit(); conn.close()
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}

# --- SYSTEM MONITOR & CLEANUP ---
@app.get("/api/system/status")
async def system_status():
    """เช็คสถานะรวมของระบบ + CPU/RAM"""
    
    # ดึงข้อมูล Memory
    mem = psutil.virtual_memory()
    
    status = {
        "server": "Online",
        "time": datetime.now().strftime("%H:%M:%S"),
        
        # --- [ใหม่] ข้อมูล CPU & RAM ---
        "cpu": {
            "percent": psutil.cpu_percent(interval=None), # % การทำงาน CPU
            "cores": psutil.cpu_count() # จำนวน Core
        },
        "ram": {
            "percent": mem.percent, # % การใช้แรม
            "used": f"{mem.used // (1024**3)} GB",
            "total": f"{mem.total // (1024**3)} GB",
            "free": f"{mem.available // (1024**3)} GB"
        },
        # -----------------------------

        "database": {"status": "Unknown", "employees": 0, "logs": 0},
        "storage": {"total": 0, "used": 0, "free": 0, "percent": 0},
        "ai_model": {"status": "Not Loaded", "faces_loaded": 0},
        "telegram": {"enabled": ENABLE_TELEGRAM, "token_status": "Unknown"}
    }

    # ... (ส่วนเช็ค Database, AI, Storage, Telegram ของเดิม คงไว้เหมือนเดิม) ...
    # 1. เช็ค Database
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT Count(*) FROM employees")
        status["database"]["employees"] = cur.fetchone()[0]
        cur.execute("SELECT Count(*) FROM attendance_logs")
        status["database"]["logs"] = cur.fetchone()[0]
        conn.close()
        status["database"]["status"] = "OK"
    except Exception as e:
        status["database"]["status"] = f"Error: {str(e)}"

    # 2. เช็ค AI Model
    status["ai_model"]["faces_loaded"] = len(known_names)
    status["ai_model"]["status"] = "Ready" if len(known_names) > 0 else "Idle"

    # 3. เช็ค Disk
    try:
        total, used, free = shutil.disk_usage(".")
        status["storage"] = {
            "total": f"{total // (2**30)} GB",
            "used": f"{used // (2**30)} GB",
            "percent": round((used / total) * 100, 1)
        }
    except: pass

    # 4. เช็ค Telegram
    status["telegram"]["token_status"] = "Configured" if ENABLE_TELEGRAM else "Disabled"

    return status

@app.post("/api/system/test-telegram")
async def test_telegram():
    """ปุ่มกดทดสอบส่งข้อความเข้า Telegram"""
    if not ENABLE_TELEGRAM:
        return {"status": "error", "message": "Telegram ไม่ได้เปิดใช้งานใน .env"}
    
    try:
        msg = f"🔔 <b>System Test</b>\nทดสอบการเชื่อมต่อ Telegram สำเร็จ!\nเวลา: {datetime.now().strftime('%H:%M:%S')}"
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        
        resp = requests.post(url, data=data, timeout=5)
        if resp.status_code == 200:
            return {"status": "success", "message": "ส่งข้อความทดสอบสำเร็จ"}
        else:
            return {"status": "error", "message": f"Telegram API Error: {resp.text}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    
def cleanup_old_data():
    """ทำงานเบื้องหลัง: ลบรูปและ Log ที่เก่ากว่ากำหนด"""
    while True:
        if KEEP_IMAGE_DAYS > 0:
            print(f">>> 🧹 Running Cleanup Task (Keep {KEEP_IMAGE_DAYS} days)...")
            try:
                cutoff_time = datetime.now().timestamp() - (KEEP_IMAGE_DAYS * 86400)
                
                # 1. ลบไฟล์รูปภาพ
                folder = "attendance_images"
                if os.path.exists(folder):
                    for f in os.listdir(folder):
                        f_path = os.path.join(folder, f)
                        # เช็คว่าไฟล์เก่ากว่า cutoff หรือไม่
                        if os.path.isfile(f_path) and os.path.getmtime(f_path) < cutoff_time:
                            os.remove(f_path)
                            print(f"Deleted old image: {f}")
                
                # 2. (Optional) ลบข้อมูลใน Database ด้วย
                conn = get_db_conn()
                cur = conn.cursor()
                # คำนวณวันที่ย้อนหลัง
                date_cutoff = (datetime.now() - timedelta(days=KEEP_IMAGE_DAYS)).strftime("%Y-%m-%d")
                cur.execute("DELETE FROM attendance_logs WHERE date(check_time) < ?", (date_cutoff,))
                conn.commit()
                conn.close()
                
            except Exception as e:
                print(f"Cleanup Error: {e}")
        
        # รอ 24 ชั่วโมงค่อยทำงานใหม่ (86400 วินาที)
        time.sleep(86400)

@app.delete("/api/system/reset-attendance")
async def reset_attendance_data(username: str = Depends(verify_admin)):
    """ล้างข้อมูลประวัติลงเวลาและรูปภาพสแกนทั้งหมด (แต่ข้อมูลพนักงานยังอยู่)"""
    try:
        # 1. ล้างข้อมูลใน Database
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM attendance_logs")
        cur.execute("DELETE FROM daily_remarks")
        # รีเซ็ตตัวนับ ID ให้กลับไปเริ่มที่ 1 ใหม่
        cur.execute("DELETE FROM sqlite_sequence WHERE name='attendance_logs'")
        conn.commit()
        conn.close()

        # 2. ลบรูปภาพสแกนทั้งหมดในโฟลเดอร์ attendance_images
        folder = "attendance_images"
        if os.path.exists(folder):
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                except Exception as e:
                    pass

        return {"status": "success", "message": "ล้างประวัติและรูปสแกนทั้งหมดเรียบร้อยแล้ว"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/api/system/cleanup-old")
async def cleanup_old_data_api(days: int = 45, username: str = Depends(verify_admin)):
    """ล้างข้อมูลประวัติและรูปภาพที่เก่ากว่า x วัน (ค่าเริ่มต้น 45 วัน)"""
    try:
        # คำนวณหาวันที่และเวลาตัดยอด (ย้อนหลัง 45 วัน)
        cutoff_datetime = datetime.now() - timedelta(days=days)
        cutoff_date_str = cutoff_datetime.strftime("%Y-%m-%d")
        cutoff_timestamp = cutoff_datetime.timestamp()

        # 1. ลบประวัติจาก Database ที่เก่ากว่าวันที่ตัดยอด
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM attendance_logs WHERE date(check_time) < ?", (cutoff_date_str,))
        deleted_logs = cur.rowcount  # นับจำนวนแถวที่ถูกลบ
        cur.execute("DELETE FROM daily_remarks WHERE date_str < ?", (cutoff_date_str,))
        conn.commit()
        conn.close()

        # 2. ลบไฟล์รูปภาพที่เก่ากว่าเวลาตัดยอด
        folder = "attendance_images"
        deleted_files = 0
        if os.path.exists(folder):
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                try:
                    if os.path.isfile(file_path):
                        # เช็คว่าไฟล์นี้ถูกสร้างก่อน cutoff_timestamp หรือไม่
                        if os.path.getmtime(file_path) < cutoff_timestamp:
                            os.unlink(file_path)
                            deleted_files += 1
                except Exception as e:
                    pass

        return {
            "status": "success", 
            "message": f"ลบประวัติไป {deleted_logs} รายการ และรูปภาพ {deleted_files} รูป เรียบร้อยแล้ว"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    print(f">>> 🚀 Starting Server on Port {SERVER_PORT}...")
    threading.Thread(target=cleanup_old_data, daemon=True).start()
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)