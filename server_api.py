import uvicorn
import shutil
from fastapi import FastAPI, UploadFile, File, Form # <--- เพิ่ม Form
from fastapi.staticfiles import StaticFiles # <--- เพิ่ม StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import cv2
import numpy as np
import os
import json
import sqlite3
from datetime import datetime
from deepface import DeepFace

# --- CONFIG ---
DB_FILE = "attendance.db"
# ค่าความเหมือน (ยิ่งน้อยยิ่งเข้มงวด)
THRESHOLD = 0.3

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

@app.get("/api/roles")
async def get_roles():
    """ดึงตำแหน่งทั้งหมด (สำหรับ Dropdown)"""
    conn = get_db_conn()
    if not conn: return []
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT role FROM employees WHERE role IS NOT NULL AND role != ''")
    rows = cur.fetchall()
    conn.close()
    return [r['role'] for r in rows]

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

@app.get("/health")
async def health_check():
    """API สำหรับเช็คว่า Server ยังรอดอยู่ไหม"""
    return {"status": "online"}

if __name__ == "__main__":
    # รัน Server ที่ Port 9876
    uvicorn.run(app, host="0.0.0.0", port=9876)