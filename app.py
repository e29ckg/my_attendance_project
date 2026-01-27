import cv2
import mysql.connector
import numpy as np
import os
import shutil
import json
import pandas as pd
from datetime import datetime
from fastapi import FastAPI, Request, File, UploadFile, Form
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from deepface import DeepFace

app = FastAPI()

# --- Config & Directories ---
UPLOAD_DIR = "images"
LOG_IMAGE_DIR = "attendance_images"
CONFIG_FILE = "config.json"

if not os.path.exists(UPLOAD_DIR): os.makedirs(UPLOAD_DIR)
if not os.path.exists(LOG_IMAGE_DIR): os.makedirs(LOG_IMAGE_DIR)

app.mount("/attendance_images", StaticFiles(directory="attendance_images"), name="attendance_images")
app.mount("/images", StaticFiles(directory="images"), name="images")

templates = Jinja2Templates(directory="templates")

# --- Default Settings (รวม DB Config แล้ว) ---
default_config = {
    "cooldown_seconds": 3600,
    "threshold": 0.30,
    "late_time": "09:00",
    "enable_voice": True,
    "enable_cooldown": True,
    "db_host": "localhost",
    "db_user": "root",
    "db_password": "",
    "db_name": "attendance_system",
    "camera_width": 640,    # ความกว้าง
    "camera_height": 480,   # ความสูง
    "process_interval": 5,  # ความถี่สแกน (เลขน้อย = เร็วแต่กินเครื่อง, เลขมาก = ช้าแต่ลื่น)
    "camera_index": 0 # ดัชนีกล้อง (0 = กล้องหลัก, 1 = กล้องรอง, etc.)
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w') as f: json.dump(default_config, f, indent=4)
        return default_config
    try:
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    except: return default_config

def save_config(new_config):
    with open(CONFIG_FILE, 'w') as f: json.dump(new_config, f, indent=4)

current_config = load_config()

# --- Database Connection (อ่านจาก Config) ---
def get_db_connection():
    return mysql.connector.connect(
        host=current_config["db_host"],
        user=current_config["db_user"],
        password=current_config["db_password"],
        database=current_config["db_name"]
    )

# --- Globals ---
known_embeddings = []
known_names = []
last_recorded = {}
last_notified_id = 0

# --- Functions ---
def load_todays_attendance():
    global last_recorded
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT employee_name, MAX(check_time) as last_time FROM attendance_logs WHERE DATE(check_time) = CURDATE() GROUP BY employee_name")
        for row in cursor.fetchall():
            last_recorded[row['employee_name']] = row['last_time']
        conn.close()
    except: pass

def load_known_faces():
    global known_embeddings, known_names
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT name, image_path FROM employees")
        rows = cursor.fetchall()
        temp_embeddings, temp_names = [], []
        print("⏳ Loading faces...")
        for row in rows:
            if os.path.exists(row['image_path']):
                try:
                    embed = DeepFace.represent(img_path=row['image_path'], model_name="Facenet512", enforce_detection=False)[0]["embedding"]
                    temp_embeddings.append(embed)
                    temp_names.append(row['name'])
                except: pass
        known_embeddings = temp_embeddings
        known_names = temp_names
        conn.close()
        print(f"✅ Loaded {len(known_names)} faces")
    except Exception as e:
        print(f"❌ DB/Face Error: {e}")

# Init
try:
    load_known_faces()
    load_todays_attendance()
except: pass

# --- Core Logic ---
def find_match(target_embedding):
    if not known_embeddings: return None
    threshold = current_config["threshold"]
    distances = []
    for embed in known_embeddings:
        a, b = np.array(target_embedding), np.array(embed)
        dist = 1 - (np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        distances.append(dist)
    if not distances: return None
    min_dist = min(distances)
    if min_dist < threshold: return known_names[distances.index(min_dist)]
    return None

def gen_frames():
# อ่านค่าจาก Config ล่าสุด
    width = current_config.get("camera_width", 640)
    height = current_config.get("camera_height", 480)
    interval = current_config.get("process_interval", 5)

    cam_index = current_config.get("camera_index", 0) # <--- อ่านค่า Index กล้อง

    # เปิดกล้องตามเลขที่ตั้งไว้
    camera = cv2.VideoCapture(cam_index)
    
    # ตั้งค่ากล้องด้วยตัวแปร
    camera.set(3, width)  
    camera.set(4, height) 

    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    
    frame_count = 0
    process_interval = interval  # ใช้ค่าจาก Config
    last_ui_data = []


    while True:
        success, frame = camera.read()
        if not success: break
        frame_count += 1
        display_frame = frame.copy()
        
        if frame_count % process_interval == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, 1.2, 10, minSize=(100, 100))
            last_ui_data = []

            for (x, y, w, h) in faces:
                face_img = frame[y:y+h, x:x+w]
                name_found, status = "Unknown", "Unknown"
                try:
                    face_rgb = cv2.cvtColor(face_img, cv2.COLOR_BGR2RGB)
                    results = DeepFace.represent(img_path=face_rgb, model_name="Facenet512", enforce_detection=False)
                    if results:
                        name = find_match(results[0]["embedding"])
                        if name:
                            name_found = name
                            now = datetime.now()
                            is_cooldown = False
                            if current_config["enable_cooldown"] and name in last_recorded:
                                if (now - last_recorded[name]).total_seconds() < current_config["cooldown_seconds"]:
                                    is_cooldown = True
                            
                            if not is_cooldown:
                                status = "OK"
                                ts = now.strftime('%Y%m%d_%H%M%S')
                                ev_fname = f"{name}_{ts}.jpg"
                                ev_path = os.path.join(LOG_IMAGE_DIR, ev_fname)
                                cv2.imwrite(ev_path, frame)
                                
                                conn = get_db_connection()
                                cur = conn.cursor()
                                db_ev_path = f"{LOG_IMAGE_DIR}/{ev_fname}"
                                cur.execute("INSERT INTO attendance_logs (employee_name, check_time, evidence_image) VALUES (%s, %s, %s)", (name, now, db_ev_path))
                                conn.commit()
                                conn.close()
                                last_recorded[name] = now
                                print(f"✅ Recorded: {name}")
                            else:
                                status = "Checked"
                        else: status = "Unknown"
                except: status = "Error"
                last_ui_data.append((x, y, w, h, name_found, status))

        for (x, y, w, h, name, status) in last_ui_data:
            if status == "Error": continue
            color = (0, 0, 255)
            label = "Unknown"
            if status == "OK": color, label = (0, 255, 0), name
            elif status == "Checked": color, label = (0, 165, 255), f"{name} (Checked)"
            
            cv2.rectangle(display_frame, (x, y), (x+w, y+h), color, 2)
            if status != "Unknown":
                cv2.rectangle(display_frame, (x, y-30), (x+w, y), color, cv2.FILLED)
                cv2.putText(display_frame, label, (x+5, y-5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)

        ret, buffer = cv2.imencode('.jpg', display_frame)
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(gen_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.post("/register_snapshot")
async def register_snapshot(name: str = Form(...), file: UploadFile = File(...)):
    safe_name = name.replace(" ", "_")
    fpath = f"{UPLOAD_DIR}/{safe_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
    with open(fpath, "wb") as b: shutil.copyfileobj(file.file, b)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO employees (name, image_path) VALUES (%s, %s)", (name, fpath))
    conn.commit()
    conn.close()
    load_known_faces()
    return {"status": "success", "message": f"ลงทะเบียน {name} สำเร็จ"}

@app.get("/recent_attendance")
async def recent_attendance():
    global last_notified_id
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, employee_name, check_time, evidence_image FROM attendance_logs ORDER BY check_time DESC LIMIT 10")
        results = cur.fetchall()
        conn.close()
    except: results = []
    
    new_entry, latest_name = False, ""
    if results and results[0]['id'] > last_notified_id:
        new_entry = True
        last_notified_id = results[0]['id']
        latest_name = results[0]['employee_name']

    for row in results: row['check_time'] = row['check_time'].strftime('%H:%M:%S')
    return { "data": results, "new_entry": new_entry, "latest_name": latest_name, "voice_enabled": current_config["enable_voice"] }

@app.get("/export_excel")
async def export_excel():
    conn = get_db_connection()
    df = pd.read_sql("SELECT employee_name, check_time, evidence_image FROM attendance_logs ORDER BY check_time DESC", conn)
    conn.close()
    fname = "Attendance_Report.xlsx"
    df.to_excel(fname, index=False)
    return FileResponse(fname, filename=f"Report_{datetime.now().strftime('%Y%m%d')}.xlsx")

# --- Admin APIs ---
@app.get("/api/settings")
async def get_settings(): return current_config

@app.post("/api/save_settings")
async def save_settings(request: Request):
    global current_config
    data = await request.json()
    current_config.update(data)
    save_config(current_config)
    return {"status": "success", "message": "บันทึกการตั้งค่าเรียบร้อย"}

@app.get("/api/employees")
async def get_employees():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, name, image_path FROM employees ORDER BY id DESC")
    res = cur.fetchall()
    conn.close()
    return res

@app.delete("/api/delete_employee/{emp_id}")
async def delete_employee(emp_id: int):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT name, image_path FROM employees WHERE id = %s", (emp_id,))
    row = cur.fetchone()
    if row:
        if os.path.exists(row['image_path']): os.remove(row['image_path'])
        cur.execute("DELETE FROM employees WHERE id = %s", (emp_id,))
        conn.commit()
        load_known_faces()
    conn.close()
    return {"status": "success", "message": "ลบพนักงานสำเร็จ"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)