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
import threading
import requests
import time
import psutil
# ❌ ลบ import mediapipe ออกไปเลยครับ ไม่ต้องใช้แล้ว

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

# --- Default Settings ---
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
    "camera_width": 640,
    "camera_height": 480,
    "process_interval": 15,
    "camera_index": 0,
    "telegram_bot_token": "", 
    "telegram_chat_id": "",
    "enable_telegram": True,
    "enable_liveness": True
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

# --- Database Connection ---
def get_db_connection():
    return mysql.connector.connect(
        host=current_config["db_host"],
        user=current_config["db_user"],
        password=current_config["db_password"],
        database=current_config["db_name"]
    )

# --- Threaded Camera ---
class ThreadedCamera:
    def __init__(self, src=0, width=640, height=480):
        self.capture = cv2.VideoCapture(src, cv2.CAP_DSHOW)
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.thread = threading.Thread(target=self.update, args=())
        self.thread.daemon = True
        self.status = False
        self.frame = None
        self.is_running = True
        self.start()

    def start(self):
        self.status, self.frame = self.capture.read()
        self.thread.start()

    def update(self):
        while self.is_running:
            if self.capture.isOpened():
                (self.status, self.frame) = self.capture.read()
                if not self.status: time.sleep(0.01)
            else: time.sleep(0.01)
            
    def get_frame(self):
        return self.status, self.frame

    def stop(self):
        self.is_running = False
        if self.capture.isOpened(): self.capture.release()

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
        for row in cursor.fetchall(): last_recorded[row['employee_name']] = row['last_time']
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
    except Exception as e: print(f"❌ DB/Face Error: {e}")

try:
    load_known_faces()
    load_todays_attendance()
except: pass

def find_match(target_embedding):
    if not known_embeddings: return None
    threshold = current_config.get("threshold", 0.30)
    distances = []
    for embed in known_embeddings:
        a, b = np.array(target_embedding), np.array(embed)
        dist = 1 - (np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        distances.append(dist)
    if not distances: return None
    min_dist = min(distances)
    if min_dist < threshold: return known_names[distances.index(min_dist)]
    return None

def send_telegram_notify(name, check_time, image_path):
    if not current_config.get("enable_telegram", True): return
    token = current_config.get("telegram_bot_token", "")
    chat_id = current_config.get("telegram_chat_id", "")
    if not token or not chat_id: return 
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    caption = f"✅ บันทึกเวลาเข้างาน\n👤 ชื่อ: {name}\n⏰ เวลา: {check_time.strftime('%H:%M:%S')}"
    try:
        with open(image_path, 'rb') as img_file:
            requests.post(url, data={'chat_id': chat_id, 'caption': caption}, files={'photo': img_file})
    except: pass

# --- Video Generator (OpenCV Native Liveness) ---
def gen_frames():
    width = current_config.get("camera_width", 640)
    height = current_config.get("camera_height", 480)
    interval = current_config.get("process_interval", 5)
    cam_index = current_config.get("camera_index", 0)

    try: camera = ThreadedCamera(cam_index, width=width, height=height)
    except: return

    # โหลดตัวจับหน้าและตัวจับตา (มีใน OpenCV อยู่แล้ว ไม่ต้องลงเพิ่ม)
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml') 
    
    frame_count = 0
    last_ui_data = []
    
    # ตัวแปร Liveness
    liveness_status = "WAIT" # WAIT -> BLINKED -> OK
    consecutive_open_eyes = 0
    consecutive_closed_eyes = 0
    is_real_person = False
    last_action_time = 0

    try:
        while True:
            success, frame = camera.get_frame()
            if not success or frame is None: continue
                
            frame_count += 1
            display_frame = frame.copy()
            
            # --- Liveness Logic (Haar Cascade) ---
            enable_liveness = current_config.get("enable_liveness", True)
            
            if enable_liveness:
                # เราต้องตรวจจับหน้า "ทุกเฟรม" แบบเร็วๆ เพื่อดูตา
                gray_live = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                # ใช้ scaleFactor ใหญ่หน่อยจะได้เร็ว (1.3)
                faces_live = face_cascade.detectMultiScale(gray_live, 1.3, 5)
                
                eyes_detected = False
                
                for (lx, ly, lw, lh) in faces_live:
                    roi_gray = gray_live[ly:ly+lh, lx:lx+lw]
                    # ตรวจจับตาเฉพาะในกรอบหน้า
                    eyes = eye_cascade.detectMultiScale(roi_gray, 1.1, 4)
                    
                    if len(eyes) >= 1: # เจอตาอย่างน้อย 1 ข้าง
                        eyes_detected = True
                    break # เอาแค่หน้าแรกที่เจอ
                
                # Logic การกระพริบตา
                if faces_live is not None and len(faces_live) > 0:
                    if eyes_detected:
                        consecutive_open_eyes += 1
                        # ถ้าเจอตากลับมาหลังจากที่ปิดไป -> ถือว่ากระพริบเสร็จ
                        if consecutive_closed_eyes > 1 and liveness_status == "WAIT":
                            liveness_status = "BLINKED"
                            last_action_time = time.time()
                        consecutive_closed_eyes = 0
                    else:
                        consecutive_closed_eyes += 1
                        consecutive_open_eyes = 0
                
                # Reset ถ้าหายนานเกิน
                if liveness_status == "BLINKED" and (time.time() - last_action_time) > 3.0:
                    liveness_status = "WAIT" # หมดเวลา ต้องกระพริบใหม่
                    
                if liveness_status == "BLINKED":
                    is_real_person = True
                    cv2.putText(display_frame, "Liveness: OK", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                else:
                    is_real_person = False
                    cv2.putText(display_frame, "Please Blink Eye", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            else:
                is_real_person = True

            # --- Face Recognition Logic (DeepFace) ---
            if frame_count % interval == 0:
                small_frame = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
                gray = cv2.cvtColor(small_frame, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(gray, 1.3, 8)
                last_ui_data = []

                for (x, y, w, h) in faces:
                    x, y, w, h = x*2, y*2, w*2, h*2
                    face_img = frame[y:y+h, x:x+w]
                    name_found, status = "Unknown", "Unknown"
                    
                    if is_real_person:
                        try:
                            face_rgb = cv2.cvtColor(face_img, cv2.COLOR_BGR2RGB)
                            results = DeepFace.represent(img_path=face_rgb, model_name="Facenet512", enforce_detection=False)
                            
                            if results:
                                name = find_match(results[0]["embedding"])
                                if name:
                                    name_found = name
                                    now = datetime.now()
                                    is_cooldown = False
                                    
                                    if current_config.get("enable_cooldown", True) and name in last_recorded:
                                        if (now - last_recorded[name]).total_seconds() < current_config.get("cooldown_seconds", 3600):
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
                                        threading.Thread(target=send_telegram_notify, args=(name, now, ev_path)).start()
                                        
                                        # สแกนผ่านแล้ว รีเซ็ตสถานะ รอคนต่อไป
                                        liveness_status = "WAIT"
                                        is_real_person = False 
                                    else:
                                        status = "Checked"
                                else: status = "Unknown"
                        except: status = "Error"
                    else:
                        status = "Liveness Fail"

                    last_ui_data.append((x, y, w, h, name_found, status))

            # Draw UI
            for (x, y, w, h, name, status) in last_ui_data:
                if status == "Error": continue
                color, label = (0, 0, 255), "Unknown"
                
                if status == "OK": color, label = (0, 255, 0), name
                elif status == "Checked": color, label = (0, 165, 255), f"{name} (Checked)"
                elif status == "Liveness Fail": color, label = (0, 0, 255), "Blink First"
                
                cv2.rectangle(display_frame, (x, y), (x+w, y+h), color, 2)
                if label:
                    cv2.rectangle(display_frame, (x, y-30), (x+w, y), color, cv2.FILLED)
                    cv2.putText(display_frame, label, (x+5, y-5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)

            ret, buffer = cv2.imencode('.jpg', display_frame)
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
    
    finally:
        camera.stop()

# --- Routes (ส่วนที่เหลือเหมือนเดิม 100%) ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request): return templates.TemplateResponse("index.html", {"request": request})
@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request): return templates.TemplateResponse("admin.html", {"request": request})
@app.get("/video_feed")
async def video_feed(): return StreamingResponse(gen_frames(), media_type="multipart/x-mixed-replace; boundary=frame")
@app.post("/register_snapshot")
async def register_snapshot(name: str = Form(...), file: UploadFile = File(...)):
    safe_name = name.replace(" ", "_")
    fpath = f"{UPLOAD_DIR}/{safe_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
    with open(fpath, "wb") as b: shutil.copyfileobj(file.file, b)
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO employees (name, image_path) VALUES (%s, %s)", (name, fpath))
    conn.commit(); conn.close(); load_known_faces()
    return {"status": "success", "message": f"ลงทะเบียน {name} สำเร็จ"}
@app.get("/recent_attendance")
async def recent_attendance():
    global last_notified_id
    try:
        conn = get_db_connection(); cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, employee_name, check_time, evidence_image FROM attendance_logs ORDER BY check_time DESC LIMIT 10")
        results = cur.fetchall(); conn.close()
    except: results = []
    new_entry, latest_name = False, ""
    if results and results[0]['id'] > last_notified_id: new_entry = True; last_notified_id = results[0]['id']; latest_name = results[0]['employee_name']
    for row in results: row['check_time'] = row['check_time'].strftime('%H:%M:%S')
    return { "data": results, "new_entry": new_entry, "latest_name": latest_name, "voice_enabled": current_config.get("enable_voice", True) }
@app.get("/export_excel")
async def export_excel():
    conn = get_db_connection()
    df = pd.read_sql("SELECT employee_name, check_time, evidence_image FROM attendance_logs ORDER BY check_time DESC", conn); conn.close()
    fname = "Attendance_Report.xlsx"; df.to_excel(fname, index=False)
    return FileResponse(fname, filename=f"Report_{datetime.now().strftime('%Y%m%d')}.xlsx")
@app.get("/api/settings")
async def get_settings(): return current_config
@app.post("/api/save_settings")
async def save_settings(request: Request):
    global current_config; data = await request.json(); current_config.update(data); save_config(current_config)
    return {"status": "success", "message": "Saved"}
@app.get("/api/employees")
async def get_employees():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True); cur.execute("SELECT id, name, image_path FROM employees ORDER BY id DESC"); res = cur.fetchall(); conn.close(); return res
@app.delete("/api/delete_employee/{emp_id}")
async def delete_employee(emp_id: int):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True); cur.execute("SELECT name, image_path FROM employees WHERE id = %s", (emp_id,)); row = cur.fetchone()
    if row:
        if os.path.exists(row['image_path']): os.remove(row['image_path'])
        cur.execute("DELETE FROM employees WHERE id = %s", (emp_id,)); conn.commit(); load_known_faces()
    conn.close(); return {"status": "success", "message": "Deleted"}
@app.get("/api/daily_stats")
async def get_daily_stats():
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM employees"); total_emp = cursor.fetchone()[0]
    cursor.execute("SELECT MIN(check_time) FROM attendance_logs WHERE DATE(check_time) = CURDATE() GROUP BY employee_name"); present_logs = cursor.fetchall(); conn.close()
    present_count = len(present_logs); absent_count = max(0, total_emp - present_count); late_count = 0; late_threshold_str = current_config.get("late_time", "09:00")
    try:
        threshold_time = datetime.strptime(late_threshold_str, "%H:%M").time()
        for (check_time,) in present_logs:
            if check_time.time() > threshold_time: late_count += 1
    except: pass
    return {"total": total_emp, "present": present_count, "late": late_count, "absent": absent_count}
# --- Server Status API ---
@app.get("/api/server_status")
async def get_server_status():
    # อ่านค่า CPU (ไม่รอ interval เพื่อความเร็ว)
    cpu = psutil.cpu_percent(interval=None)

    # อ่านค่า RAM
    ram = psutil.virtual_memory()

    # อ่านค่า Disk (Drive C: หรือ Root)
    disk = psutil.disk_usage('/')

    return {
        "cpu": cpu,
        "ram_percent": ram.percent,
        "ram_used": round(ram.used / (1024**3), 2), # GB
        "ram_total": round(ram.total / (1024**3), 2), # GB
        "disk_percent": disk.percent,
        "disk_free": round(disk.free / (1024**3), 2) # GB
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9876)