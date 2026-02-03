import uvicorn
import shutil
import os
import sqlite3
import cv2
import numpy as np
import threading
import requests
import json
from typing import Optional
from datetime import datetime
from dotenv import load_dotenv # โหลดค่าจากไฟล์ .env

# รวม import ของ FastAPI ไว้ด้วยกัน
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from deepface import DeepFace

# --- [เพิ่ม] LOAD .ENV ---
load_dotenv() # โหลดค่าจากไฟล์ .env เข้าสู่ระบบ

# --- CONFIG (ดึงจาก .env) ---
DB_FILE = os.getenv("DB_FILE", "attendance.db")
THRESHOLD = float(os.getenv("THRESHOLD", 0.3)) # แปลงเป็น float

# การแปลงค่า True/False จาก String
ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "False").lower() == "true"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

os.makedirs("images", exist_ok=True)
os.makedirs("attendance_images", exist_ok=True) # สร้างโฟลเดอร์รอไว้เลย

# Port สำหรับรัน Server
SERVER_PORT = int(os.getenv("PORT", 9876))
SERVER_HOST = os.getenv("HOST", "0.0.0.0")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # อนุญาตทุกเว็บ (สำหรับใช้งานภายใน)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("images", exist_ok=True)
app.mount("/images", StaticFiles(directory="images"), name="images")
app.mount("/attendance_images", StaticFiles(directory="attendance_images"), name="attendance_images")


# Global Variables
known_embeddings = []
known_ids = []
known_names = []

# --- DATABASE & INIT ---
def get_db_conn():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn
    except: return None

def init_system():
    # 1. สร้างตาราง DB
    conn = get_db_conn()
    if conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS employees (employee_id TEXT PRIMARY KEY, name TEXT, role TEXT, image_path TEXT, embedding TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS attendance_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id TEXT, employee_name TEXT, check_time DATETIME, evidence_image TEXT, log_type TEXT DEFAULT 'SCAN', status TEXT DEFAULT '-')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS daily_remarks (date_str TEXT, employee_id TEXT, remark TEXT, PRIMARY KEY (date_str, employee_id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS roles (role_name TEXT PRIMARY KEY)""")
        cur.execute("INSERT OR IGNORE INTO roles (role_name) SELECT DISTINCT role FROM employees WHERE role IS NOT NULL AND role != ''")
        conn.commit(); conn.close()
    
    # 2. โหลดหน้าเข้า RAM
    load_faces()

def load_faces():
    global known_embeddings, known_ids, known_names
    print(">>> 🔄 Loading AI Models & Faces...")
    conn = get_db_conn()
    if not conn: return
    cur = conn.cursor()
    cur.execute("SELECT employee_id, name, embedding, image_path FROM employees")
    rows = cur.fetchall()
    
    known_embeddings, known_ids, known_names = [], [], []
    
    for r in rows:
        if r['embedding']:
            try:
                known_embeddings.append(json.loads(r['embedding']))
                known_ids.append(r['employee_id'])
                known_names.append(r['name'])
            except: pass
        # ถ้ามีแต่รูป ยังไม่มี embedding ให้ gen ใหม่ (เผื่อไว้)
        elif r['image_path'] and os.path.exists(r['image_path']):
            try:
                objs = DeepFace.represent(img_path=r['image_path'], model_name="Facenet512", enforce_detection=False)
                if objs:
                    emb = objs[0]["embedding"]
                    known_embeddings.append(emb)
                    known_ids.append(r['employee_id'])
                    known_names.append(r['name'])
            except: pass
    conn.close()
    print(f">>> ✅ Loaded {len(known_names)} faces.")

# --- API ENDPOINTS ---

@app.on_event("startup")
async def startup_event():
    init_system()


# --- [เพิ่มส่วนนี้] WEB ROUTES (สำหรับเปิดหน้าเว็บ) ---

@app.get("/")
async def index():
    """หน้าแรก: รวมเมนู"""
    return FileResponse("index.html") # (เดี๋ยวเราสร้างไฟล์นี้เพิ่มเป็นเมนูรวม)

@app.get("/admin")
async def view_admin():
    """เปิดหน้าจัดการพนักงาน"""
    return FileResponse("admin.html")

@app.get("/report")
async def view_report():
    """เปิดหน้ารายงาน"""
    return FileResponse("report_daily.html")


# --- FACE SCAN API ---
@app.post("/scan")
async def scan_face(file: UploadFile = File(...)):
    """
    รับภาพจาก Client -> ประมวลผล AI -> บันทึก DB -> ส่งผลกลับ
    """
    try:
        # 1. แปลงไฟล์ภาพเป็น OpenCV Format
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        # 2. ใช้ DeepFace แปลงภาพเป็น Embedding (AI ส่วนที่หนักสุด)
        # ใช้ Facenet512 ตามเดิม
        objs = DeepFace.represent(img_path=frame, model_name="Facenet512", enforce_detection=False)
        
        found_name = "Unknown"
        status = "FAIL"
        
        if objs:
            target_emb = objs[0]["embedding"]
            
            # 3. คำนวณระยะห่าง (Cosine Distance logic)
            # (เขียนแบบ Loop ธรรมดาเพื่อให้เข้าใจง่าย)
            min_dist = 100
            idx = -1
            
            for i, known_emb in enumerate(known_embeddings):
                # Cosine distance formula
                dist = 1 - (np.dot(target_emb, known_emb) / (np.linalg.norm(target_emb) * np.linalg.norm(known_emb)))
                if dist < min_dist:
                    min_dist = dist
                    idx = i
            
            # 4. ตัดสินผลลัพธ์
            if min_dist < THRESHOLD and idx != -1:
                emp_id = known_ids[idx]
                found_name = known_names[idx]
                status = "OK"
                
                # 5. บันทึกขอมูลลง DB (เฉพาะกรณีเจอตัว)
                save_log(emp_id, found_name, frame)
        
        return {
            "status": status,
            "name": found_name,
            "time": datetime.now().strftime("%H:%M:%S")
        }

    except Exception as e:
        print(f"Error: {e}")
        return {"status": "ERROR", "name": "System Error"}
    
def send_telegram_thread(name, time_str, img_path):
    """ฟังก์ชันส่งไลน์/Telegram แยก Thread เพื่อไม่ให้ Server หน่วง"""
    if not ENABLE_TELEGRAM: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        caption = f"✅ <b>ลงเวลาสำเร็จ</b>\n👤 <b>ชื่อ:</b> {name}\n⏰ <b>เวลา:</b> {time_str}"
        
        # เปิดไฟล์รูปเพื่อส่ง
        with open(img_path, 'rb') as f:
            files = {'photo': f}
            data = {
                'chat_id': TELEGRAM_CHAT_ID, 
                'caption': caption, 
                'parse_mode': 'HTML'
            }
            requests.post(url, files=files, data=data)
            print(f">>> 🚀 Telegram sent for {name}")
            
    except Exception as e:
        print(f"Telegram Error: {e}")

def save_log(emp_id, name, frame):
    # บันทึกเหมือนเดิม แต่ทำที่ฝั่ง Server
    now = datetime.now()
    conn = get_db_conn()
    if not conn: return
    
    try:
        cur = conn.cursor()
        
        # Cooldown 1 นาที (เช็คที่ Server ชัวร์ที่สุด)
        cur.execute("SELECT check_time FROM attendance_logs WHERE employee_id=? ORDER BY id DESC LIMIT 1", (emp_id,))
        last = cur.fetchone()
        if last:
            last_time = datetime.strptime(last['check_time'], "%Y-%m-%d %H:%M:%S.%f")
            if (now - last_time).total_seconds() < 60:
                return # ติด Cooldown ไม่บันทึกซ้ำ

        # Save Image
        if not os.path.exists("attendance_images"): os.makedirs("attendance_images")
        img_path = f"attendance_images/{emp_id}_{now.strftime('%H%M%S')}.jpg"
        cv2.imwrite(img_path, frame)
        
        # Insert DB
        cur.execute("INSERT INTO attendance_logs (employee_id, employee_name, check_time, evidence_image, log_type, status) VALUES (?,?,?,?,?,?)",
                    (emp_id, name, now, img_path, "SCAN", "บันทึกแล้ว"))
        conn.commit()
        print(f"✅ Logged: {name}")

        # ส่ง Telegram แบบแยก Thread
        if ENABLE_TELEGRAM:
            time_str = now.strftime("%d/%m/%Y %H:%M:%S")
            # ใช้ Threading เพื่อให้ Server ตอบกลับ Client ทันทีโดยไม่ต้องรอ Telegram ส่งเสร็จ
            threading.Thread(target=send_telegram_thread, args=(name, time_str, img_path)).start()
    except Exception as e:
        print(f"DB Save Error: {e}")
    finally:
        conn.close()

        

# --- USER MANAGEMENT API ---

@app.get("/api/employees")
async def get_employees():
    """ดึงรายชื่อพนักงานทั้งหมด"""
    conn = get_db_conn()
    if not conn: return []
    cur = conn.cursor()
    cur.execute("SELECT * FROM employees")
    rows = cur.fetchall()
    conn.close()
    return rows

@app.post("/api/employees/update")
async def update_employee(
    emp_id: str = Form(...),
    name: str = Form(...),
    role: str = Form(...),
    file: Optional[UploadFile] = File(None) # รูปภาพเป็น Optional (ไม่ต้องส่งมาก็ได้)
):
    """แก้ไขข้อมูลพนักงาน (ถ้ารูปไม่ส่งมา ให้ใช้รูปเดิม)"""
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        # 1. ถ้ามีการอัปโหลดรูปใหม่ -> ทำ DeepFace ใหม่
        if file:
            file_path = f"images/{emp_id}.jpg"
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            embedding_json = None
            try:
                objs = DeepFace.represent(img_path=file_path, model_name="Facenet512", enforce_detection=False)
                if objs:
                    embedding_json = json.dumps(objs[0]["embedding"])
            except: pass
            
            # อัปเดตทุกอย่างรวมถึงรูปและ embedding
            cur.execute("""
                UPDATE employees 
                SET name=?, role=?, image_path=?, embedding=?
                WHERE employee_id=?
            """, (name, role, file_path, embedding_json, emp_id))
            
        else:
            # 2. ถ้าไม่มีรูปใหม่ -> อัปเดตแค่ชื่อและตำแหน่ง
            cur.execute("""
                UPDATE employees 
                SET name=?, role=?
                WHERE employee_id=?
            """, (name, role, emp_id))

        conn.commit()
        conn.close()

        # รีโหลดหน้าเข้า RAM
        load_faces()
        
        return {"status": "success", "message": f"อัปเดตข้อมูล {name} เรียบร้อย"}

    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- ROLE MANAGEMENT API ---

@app.get("/api/roles")
async def get_roles():
    """ดึงรายชื่อตำแหน่งทั้งหมดจากตาราง roles"""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT role_name FROM roles ORDER BY role_name")
    rows = cur.fetchall()
    conn.close()
    return [r['role_name'] for r in rows]

@app.post("/api/roles")
async def add_role(role_name: str = Form(...)):
    """เพิ่มตำแหน่งใหม่"""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO roles (role_name) VALUES (?)", (role_name.strip(),))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/api/roles/{role_name}")
async def delete_role(role_name: str):
    """ลบตำแหน่ง"""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM roles WHERE role_name = ?", (role_name,))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/register")
async def register(
    name: str = Form(...),
    emp_id: str = Form(...),
    role: str = Form(...),
    file: UploadFile = File(...)
):
    """ลงทะเบียนพนักงานใหม่ + สร้าง Embedding ทันที"""
    try:
        # 1. บันทึกไฟล์รูปภาพ
        file_path = f"images/{emp_id}.jpg"
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 2. สร้าง Embedding ทันที (เพื่อให้สแกนได้เลยไม่ต้องรอ)
        embedding_json = None
        try:
            objs = DeepFace.represent(img_path=file_path, model_name="Facenet512", enforce_detection=False)
            if objs:
                embedding_json = json.dumps(objs[0]["embedding"])
        except Exception as e:
            print(f"Embedding Error: {e}")

        # 3. บันทึกลงฐานข้อมูล
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO employees (employee_id, name, role, image_path, embedding)
            VALUES (?, ?, ?, ?, ?)
        """, (emp_id, name, role, file_path, embedding_json))
        conn.commit()
        conn.close()

        # 4. รีโหลดหน้าเข้า RAM (Hot Reload)
        load_faces()
        
        return {"status": "success", "message": f"ลงทะเบียน {name} เรียบร้อย"}

    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/api/employees/delete/{emp_id}")
async def delete_employee(emp_id: str):
    """ลบพนักงาน"""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        
        # ลบรูปภาพ
        cur.execute("SELECT image_path FROM employees WHERE employee_id = ?", (emp_id,))
        row = cur.fetchone()
        if row and row['image_path'] and os.path.exists(row['image_path']):
            os.remove(row['image_path'])
            
        # ลบจาก DB
        cur.execute("DELETE FROM employees WHERE employee_id = ?", (emp_id,))
        conn.commit()
        conn.close()

        # รีโหลดหน้าเข้า RAM (Hot Reload)
        load_faces()
        
        return {"status": "success", "message": f"ลบ {emp_id} เรียบร้อย"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    
# --- REPORT API ---

# ค้นหาฟังก์ชัน get_daily_report แล้วแก้ตามนี้ครับ

@app.get("/api/report/daily")
async def get_daily_report(date: str, role: str = "all"):
    """
    ดึงรายงานสรุปรายวัน: 
    - เวลาเข้า = สแกนครั้งแรก
    - เวลาออก = สแกนครั้งสุดท้าย
    - [NEW] เรียงลำดับตามเวลาเข้า (มาก่อนอยู่บน)
    """
    conn = get_db_conn()
    if not conn: return []
    cur = conn.cursor()
    
    # ... (ส่วนที่ 1-3 ดึง employees, logs, remarks เหมือนเดิมเป๊ะๆ) ...
    # 1. ดึงพนักงาน
    if role == "all":
        cur.execute("SELECT employee_id, name, role FROM employees")
    else:
        cur.execute("SELECT employee_id, name, role FROM employees WHERE role = ?", (role,))
    employees = cur.fetchall()
    
    # 2. ดึง Log
    cur.execute("""
        SELECT employee_id, check_time 
        FROM attendance_logs 
        WHERE date(check_time) = ? 
        ORDER BY check_time ASC
    """, (date,))
    all_logs = cur.fetchall()
    
    logs_by_emp = {}
    for log in all_logs:
        eid = log['employee_id']
        if eid not in logs_by_emp: logs_by_emp[eid] = []
        logs_by_emp[eid].append(log['check_time'])

    # 3. ดึงหมายเหตุ
    cur.execute("SELECT employee_id, remark FROM daily_remarks WHERE date_str = ?", (date,))
    remarks_db = cur.fetchall()
    remarks_map = {r['employee_id']: r['remark'] for r in remarks_db}

    report_data = []
    
    # 4. วนลูปคำนวณเวลา (เหมือนเดิม)
    for emp in employees:
        e_id = emp['employee_id']
        e_name = emp['name']
        
        times = logs_by_emp.get(e_id, [])
        
        time_in = "-"
        time_out = "-"
        
        if times:
            try:
                # เวลาเข้า
                t_str_in = times[0].split(".")[0]
                dt_in = datetime.strptime(t_str_in, "%Y-%m-%d %H:%M:%S")
                time_in = dt_in.strftime("%H:%M:%S")
                
                # เวลาออก (ต้องมีมากกว่า 1 ครั้ง)
                if len(times) > 1:
                    t_str_out = times[-1].split(".")[0]
                    dt_out = datetime.strptime(t_str_out, "%Y-%m-%d %H:%M:%S")
                    time_out = dt_out.strftime("%H:%M:%S")
            except: pass

        report_data.append({
            "employee_id": e_id,
            "name": e_name,
            "role": emp['role'],
            "time_in": time_in,
            "time_out": time_out,
            "remark": remarks_map.get(e_id, "")
        })
        
    conn.close()

    # --- [เพิ่มส่วนนี้] เรียงลำดับข้อมูลก่อนส่งกลับ ---
    # Logic: ถ้ามีเวลาให้ใช้เวลานั้น, ถ้าเป็น "-" ให้เป็นค่ามากสุด (เช่น "99:99:99") จะได้ไปอยู่ล่างสุด
    report_data.sort(key=lambda x: x['time_in'] if x['time_in'] != "-" else "99:99:99")

    return report_data

@app.post("/api/report/remark")
async def update_remark(
    date: str = Form(...),
    employee_id: str = Form(...),
    remark: str = Form(...)
):
    """อัปเดตหมายเหตุรายวัน"""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        # ใช้ Insert or Replace เพื่อบันทึกทับได้เลย
        cur.execute("""
            INSERT OR REPLACE INTO daily_remarks (date_str, employee_id, remark)
            VALUES (?, ?, ?)
        """, (date, employee_id, remark))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/print")
async def view_print():
    return FileResponse("report_print.html")

# --- SYSTEM MONITOR API ---

@app.get("/api/system/status")
async def system_status():
    """เช็คสถานะรวมของระบบ (Database, Disk, AI, Config)"""
    status = {
        "server": "Online",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "database": {"status": "Unknown", "employees": 0, "logs": 0},
        "storage": {"total": 0, "used": 0, "free": 0, "percent": 0},
        "ai_model": {"status": "Not Loaded", "faces_loaded": 0},
        "telegram": {"enabled": ENABLE_TELEGRAM, "token_status": "Unknown"}
    }

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
    status["ai_model"]["status"] = "Ready" if len(known_names) > 0 else "Idle/Empty"

    # 3. เช็ค Disk Space (Drive ที่รันโปรแกรม)
    try:
        total, used, free = shutil.disk_usage(".")
        status["storage"] = {
            "total": f"{total // (2**30)} GB",
            "used": f"{used // (2**30)} GB",
            "free": f"{free // (2**30)} GB",
            "percent": round((used / total) * 100, 1)
        }
    except: pass

    # 4. เช็ค Telegram Connection (Passive)
    if ENABLE_TELEGRAM:
        status["telegram"]["token_status"] = "Configured"
    else:
        status["telegram"]["token_status"] = "Disabled"

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

# เพิ่ม Route สำหรับเปิดหน้า Monitor
@app.get("/monitor")
async def view_monitor():
    return FileResponse("monitor.html")

@app.get("/health")
async def health_check():
    """API สำหรับเช็คว่า Server ยังรอดอยู่ไหม"""
    return {"status": "online"}

if __name__ == "__main__":
    print(f">>> 🚀 Starting Server on Port {SERVER_PORT}...")
    # ใช้ตัวแปรจาก .env
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)