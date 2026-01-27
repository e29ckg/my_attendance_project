import cv2
import numpy as np
import mysql.connector
import os
import shutil
import pandas as pd
from datetime import datetime
from fastapi import FastAPI, Request, File, UploadFile, Form
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

# นำเข้า DeepFace
from deepface import DeepFace

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- Config ---
UPLOAD_DIR = "images"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

db_config = {
    "host": "localhost",
    "user": "root",
    "password": "", # <--- แก้รหัสผ่าน
    "database": "attendance_system"
}

def get_db_connection():
    return mysql.connector.connect(**db_config)

# --- Global Variables ---
known_embeddings = [] # เปลี่ยนจาก encoding เป็น embedding
known_names = []
last_recorded = {}
last_notified_id = 0

# --- Load Faces (ใช้ DeepFace) ---
def load_known_faces():
    global known_embeddings, known_names
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT name, image_path FROM employees")
    rows = cursor.fetchall()
    
    temp_embeddings = []
    temp_names = []
    
    print("Loading faces with DeepFace... (อาจใช้เวลาสักครู่)")
    for row in rows:
        try:
            if os.path.exists(row['image_path']):
                # ใช้ Model ชื่อ Facenet512 (เร็วและแม่นยำ)
                embedding = DeepFace.represent(img_path=row['image_path'], model_name="Facenet512", enforce_detection=False)[0]["embedding"]
                temp_embeddings.append(embedding)
                temp_names.append(row['name'])
        except Exception as e:
            print(f"Error loading {row['name']}: {e}")
            
    known_embeddings = temp_embeddings
    known_names = temp_names
    conn.close()
    print(f"Loaded {len(known_names)} faces.")

# โหลดครั้งแรก
try:
    load_known_faces()
except:
    print("ยังไม่มีข้อมูลใบหน้า หรือ Database ยังไม่พร้อม")

# --- ฟังก์ชันคำนวณความเหมือน (Cosine Similarity) ---
def find_match(target_embedding, threshold=0.4): # Threshold ยิ่งน้อยยิ่งต้องเหมือนเป๊ะ
    if len(known_embeddings) == 0:
        return None
    
    # คำนวณระยะห่าง (Cosine Distance)
    distances = []
    for embed in known_embeddings:
        a = np.array(target_embedding)
        b = np.array(embed)
        # สูตร Cosine Distance
        dist = 1 - (np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        distances.append(dist)
    
    min_dist = min(distances)
    if min_dist < threshold:
        index = distances.index(min_dist)
        return known_names[index]
    return None

# --- Video Logic ---
# --- Video Logic (Optimized: แก้แลค) ---
def gen_frames():
    camera = cv2.VideoCapture(0)
    
    # ลดความละเอียดกล้องลงหน่อยเพื่อให้เร็วขึ้น (640x480)
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    
    frame_count = 0        # ตัวนับเฟรม
    process_interval = 30  # กำหนดให้วิเคราะห์หน้าทุกๆ 30 เฟรม (ประมาณ 1 วิ)
    
    # ตัวแปรจำค่าล่าสุดไว้โชว์ตอนที่ไม่ได้วิเคราะห์
    last_face_locations = [] 
    last_face_names = []

    while True:
        success, frame = camera.read()
        if not success:
            break
            
        # เพิ่มตัวนับเฟรม
        frame_count += 1
        
        # เตรียมภาพสำหรับวาดกรอบ (Copy มาเพื่อไม่ให้กระทบภาพต้นฉบับ)
        display_frame = frame.copy()
        
        # --- 1. ทำงานเฉพาะรอบที่กำหนด (เช่น ทุกๆ 30 เฟรม) ---
        if frame_count % process_interval == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, 1.1, 4)
            
            last_face_locations = []
            last_face_names = []

            for (x, y, w, h) in faces:
                last_face_locations.append((x, y, w, h))
                
                # ตัดภาพหน้าไปวิเคราะห์
                face_img = frame[y:y+h, x:x+w]
                
                try:
                    # ใช้ model "VGG-Face" แทน (เร็วกว่า Facenet512 เล็กน้อยแต่อาจแม่นน้อยกว่านิดนึง)
                    results = DeepFace.represent(img_path=face_img, model_name="VGG-Face", enforce_detection=False)
                    
                    if results:
                        target_embedding = results[0]["embedding"]
                        name = find_match(target_embedding) # เรียกฟังก์ชันเปรียบเทียบหน้า
                        
                        if name:
                            last_face_names.append(name)
                            
                            # บันทึกเวลาทันทีที่เจอ
                            now = datetime.now()
                            # เช็ค Cooldown 30 วินาที
                            if name not in last_recorded or (now - last_recorded[name]).total_seconds() > 30:
                                conn = get_db_connection()
                                cursor = conn.cursor()
                                cursor.execute("INSERT INTO attendance_logs (employee_name, check_time) VALUES (%s, %s)", (name, now))
                                conn.commit()
                                conn.close()
                                last_recorded[name] = now
                                print(f"Recorded: {name}")
                        else:
                            last_face_names.append("Unknown")
                    else:
                        last_face_names.append("Unknown")

                except Exception as e:
                    last_face_names.append("Error")

        # --- 2. วาดภาพตามข้อมูลล่าสุดที่จำไว้ (เฟรมไหนไม่ได้วิเคราะห์ ก็ใช้ข้อมูลเก่ามาวาด) ---
        # หมายเหตุ: การวาดแบบนี้ตำแหน่งกรอบอาจจะดีเลย์นิดหน่อยถ้าคนขยับเร็ว แต่แลกกับความลื่นครับ
        if len(last_face_locations) == len(last_face_names): # เช็คกันพลาด
            for (x, y, w, h), name in zip(last_face_locations, last_face_names):
                color = (0, 255, 0) if name != "Unknown" and name != "Error" else (0, 0, 255)
                
                cv2.rectangle(display_frame, (x, y), (x+w, y+h), color, 2)
                cv2.putText(display_frame, name, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)

        # ส่งภาพกลับไปที่หน้าเว็บ
        ret, buffer = cv2.imencode('.jpg', display_frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

# --- Routes (คงเดิม) ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(gen_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.post("/register_snapshot")
async def register_snapshot(name: str = Form(...), file: UploadFile = File(...)):
    safe_name = name.replace(" ", "_")
    file_path = f"{UPLOAD_DIR}/{safe_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO employees (name, image_path) VALUES (%s, %s)", (name, file_path))
    conn.commit()
    conn.close()
    
    # Reload Memory
    load_known_faces()
    return {"status": "success", "message": f"ลงทะเบียนคุณ {name} เรียบร้อย!"}

@app.get("/recent_attendance")
async def recent_attendance():
    global last_notified_id
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, employee_name, check_time FROM attendance_logs ORDER BY check_time DESC LIMIT 10")
    results = cursor.fetchall()
    conn.close()
    
    new_entry = False
    latest_name = ""
    if results:
        if results[0]['id'] > last_notified_id:
            new_entry = True
            last_notified_id = results[0]['id']
            latest_name = results[0]['employee_name']

    for row in results:
        row['check_time'] = row['check_time'].strftime('%H:%M:%S | %d-%m-%Y')

    return {"data": results, "new_entry": new_entry, "latest_name": latest_name}

@app.get("/export_excel")
async def export_excel():
    conn = get_db_connection()
    df = pd.read_sql("SELECT employee_name, check_time FROM attendance_logs ORDER BY check_time DESC", conn)
    conn.close()
    filename = "Attendance_Report.xlsx"
    df.to_excel(filename, index=False)
    return FileResponse(path=filename, filename=f"Report.xlsx")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9876)