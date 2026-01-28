import sys, cv2, threading, numpy as np, os, json, requests, time, psutil, mysql.connector, shutil
from datetime import datetime
from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from deepface import DeepFace
from PIL import Image, ImageDraw, ImageFont # สำหรับวาดภาษาไทย
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

# --- 1. CONFIG & DB CORE ---
CONFIG_FILE = "config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "db_host": "localhost", "db_user": "root", "db_password": "", "db_name": "attendance_system",
            "threshold": 0.30, "cooldown_seconds": 3600, "camera_index": 0, "enable_liveness": True,
            "telegram_bot_token": "", "telegram_chat_id": "", "enable_telegram": False
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(default, f, indent=4, ensure_ascii=False)
        return default
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

current_config = load_config()

def get_db_conn():
    """ฟังก์ชันเชื่อมต่อฐานข้อมูล (ชื่อที่ถูกต้องคือ get_db_conn)"""
    try:
        return mysql.connector.connect(
            host=current_config.get("db_host"),
            user=current_config.get("db_user"),
            password=current_config.get("db_password"),
            database=current_config.get("db_name"),
            connect_timeout=3
        )
    except:
        return None
    
# --- 1. ระบบส่งอีเมล (Email Function) ---
def send_email_report(file_path, target_email):
    # ดึงค่า SMTP จาก Config (ควรตั้งค่าใน config.json)
    smtp_server = "smtp.gmail.com"
    smtp_port = 587
    sender_email = current_config.get("sender_email", "your-email@gmail.com")
    sender_password = current_config.get("sender_password", "your-app-password")

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = target_email
    msg['Subject'] = f"รายงานสรุปการเข้างานประจำเดือน {datetime.now().strftime('%m/%Y')}"

    body = "เรียนฝ่ายบุคคล,\n\nแนบไฟล์รายงานสรุปการเข้างานของพนักงานประจำเดือนนี้มาพร้อมกับอีเมลฉบับนี้ครับ"
    msg.attach(MIMEText(body, 'plain'))

    with open(file_path, "rb") as attachment:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename= {os.path.basename(file_path)}")
        msg.attach(part)

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"Email Error: {e}")
        return False

# --- 2. ฟังก์ชันวาดภาษาไทย (Thai Text Support) ---
def draw_thai_text(img, text, position, font_size=25, color=(0, 255, 0)):
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    try:
        font = ImageFont.truetype("tahoma.ttf", font_size) # ใช้ Tahoma จาก Windows
    except:
        font = ImageFont.load_default()
    draw.text(position, text, font=font, fill=color)
    return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

# --- 3. FASTAPI BACKEND (WEB DASHBOARD) ---
web_app = FastAPI()
templates = Jinja2Templates(directory="templates")
if not os.path.exists("attendance_images"): os.makedirs("attendance_images")
web_app.mount("/attendance_images", StaticFiles(directory="attendance_images"), name="attendance_images")

@web_app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@web_app.get("/api/daily_stats") # แก้ไข Error 404
async def get_daily_stats():
    conn = get_db_conn()
    if not conn: return {"total":0,"present":0,"late":0,"absent":0}
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM employees"); total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT employee_id) FROM attendance_logs WHERE DATE(check_time) = CURDATE()")
    present = cur.fetchone()[0]
    conn.close()
    return {"total": total, "present": present, "late": 0, "absent": max(0, total-present)}

# --- API สำหรับจัดการพนักงาน (Web Management) ---
@web_app.get("/manage_users", response_class=HTMLResponse)
async def manage_users_page(request: Request):
    return templates.TemplateResponse("user_management.html", {"request": request})

@web_app.get("/api/employees")
async def get_all_employees():
    conn = get_db_conn() # ใช้ชื่อฟังก์ชันที่ถูกต้อง
    if not conn: return []
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM employees")
    users = cur.fetchall()
    conn.close()
    return users

@web_app.post("/api/schedule/assign")
async def assign_shift(emp_id: str = Form(...), role_name: str = Form(...), start_date: str = Form(...), end_date: str = Form(...)):
    conn = get_db_conn()
    if not conn: return {"status": "error"}
    cur = conn.cursor()
    # บันทึกตารางเวรรายบุคคล
    cur.execute("""
        INSERT INTO employee_schedules (employee_id, role_name, start_date, end_date) 
        VALUES (%s, %s, %s, %s)
    """, (emp_id, role_name, start_date, end_date))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Assign Shift Completed"}

# --- API ตรวจสอบตารางเวรรายวัน ---
@web_app.get("/api/current_shifts")
async def get_current_shifts():
    conn = get_db_conn() # ใช้ฟังก์ชันเชื่อมต่อที่ถูกต้อง
    if not conn: return []
    cur = conn.cursor(dictionary=True)
    
    # Query หาว่าวันนี้ใครต้องเข้ากะไหน และสถานะการสแกนล่าสุด
    query = """
        SELECT e.employee_id, e.name, s.role_name, r.start_time, r.end_time,
               (SELECT status FROM attendance_logs 
                WHERE employee_id = e.employee_id AND DATE(check_time) = CURDATE() 
                ORDER BY check_time DESC LIMIT 1) as current_status
        FROM employees e
        JOIN employee_schedules s ON e.employee_id = s.employee_id
        JOIN work_rules r ON s.role_name = r.role_name
        WHERE CURDATE() BETWEEN s.start_date AND s.end_date
    """
    cur.execute(query)
    shifts = cur.fetchall()
    conn.close()
    return shifts

@web_app.get("/edit_attendance", response_class=HTMLResponse)
async def edit_page(request: Request):
    return templates.TemplateResponse("attendance_edit.html", {"request": request})

@web_app.post("/api/attendance/manual_add")
async def manual_add(emp_id: str = Form(...), check_time: str = Form(...), status: str = Form(...)):
    conn = get_db_conn()
    cur = conn.cursor(dictionary=True)
    # ดึงชื่อพนักงานก่อนบันทึก
    cur.execute("SELECT name FROM employees WHERE employee_id = %s", (emp_id,))
    user = cur.fetchone()
    if user:
        cur.execute("""
            INSERT INTO attendance_logs (employee_id, employee_name, check_time, status, evidence_image) 
            VALUES (%s, %s, %s, %s, 'manual_entry.jpg')
        """, (emp_id, user['name'], check_time, status))
        conn.commit()
    conn.close()
    return {"status": "success"}

# --- 3. API ส่งออกรายงานรายเดือน ---
@web_app.get("/api/report/monthly_email")
async def export_monthly_report(email: str):
    conn = get_db_conn()
    query = "SELECT employee_id, employee_name, check_time, status FROM attendance_logs WHERE MONTH(check_time) = MONTH(CURDATE())"
    df = pd.read_sql(query, conn)
    conn.close()

    file_name = f"Report_Monthly_{datetime.now().strftime('%Y%m')}.xlsx"
    df.to_excel(file_name, index=False)
    
    success = send_email_report(file_name, email)
    return {"status": "success" if success else "error"}

# --- [API: การจัดการกะการทำงาน - Work Rules] ---
@web_app.get("/api/work_rules")
async def get_work_rules():
    conn = get_db_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM work_rules")
    rules = cur.fetchall()
    conn.close()
    return rules

@web_app.post("/api/work_rules/save")
async def save_work_rule(role_name: str = Form(...), start_time: str = Form(...), late_threshold: str = Form(...), end_time: str = Form(...)):
    conn = get_db_conn()
    cur = conn.cursor()
    # ใช้ ON DUPLICATE KEY UPDATE เพื่อรองรับทั้งการเพิ่มและแก้ไขในคำสั่งเดียว
    sql = """INSERT INTO work_rules (role_name, start_time, late_threshold, end_time) 
             VALUES (%s, %s, %s, %s) 
             ON DUPLICATE KEY UPDATE start_time=%s, late_threshold=%s, end_time=%s"""
    cur.execute(sql, (role_name, start_time, late_threshold, end_time, start_time, late_threshold, end_time))
    conn.commit()
    conn.close()
    return {"status": "success"}

@web_app.delete("/api/work_rules/delete/{role_name}")
async def delete_work_rule(role_name: str):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM work_rules WHERE role_name = %s", (role_name,))
    conn.commit()
    conn.close()
    return {"status": "success"}

# --- [API: การจัดการพนักงาน - Employees] ---
@web_app.delete("/api/employees/delete/{emp_id}")
async def delete_employee(emp_id: str):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM employees WHERE employee_id = %s", (emp_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}

# --- [API: การจัดการตารางเวร - Schedules] ---
@web_app.get("/api/schedules")
async def get_schedules():
    conn = get_db_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM employee_schedules ORDER BY start_date DESC")
    data = cur.fetchall()
    conn.close()
    return data

@web_app.delete("/api/schedules/delete/{schedule_id}")
async def delete_schedule(schedule_id: int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM employee_schedules WHERE id = %s", (schedule_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}

# --- [API: แก้ไขข้อมูลพนักงาน] ---
@web_app.post("/api/employees/update")
async def update_employee(emp_id: str = Form(...), name: str = Form(...), role: str = Form(...)):
    conn = get_db_conn()
    cur = conn.cursor()
    # อัปเดตข้อมูลชื่อและประเภทพนักงานตาม ID
    cur.execute("UPDATE employees SET name=%s, role=%s WHERE employee_id=%s", (name, role, emp_id))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Updated successfully"}

# --- 4. ADMIN WINDOW (จัดการจาก Desktop) ---
class AdminWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ Admin Management")
        self.setFixedSize(500, 700)
        self.parent = parent
        layout = QVBoxLayout(self)
        
        # ส่วนจัดการ User
        layout.addWidget(QLabel("👥 ลงทะเบียนพนักงานใหม่"))
        self.emp_id = QLineEdit(); self.emp_id.setPlaceholderText("รหัสพนักงาน")
        self.emp_name = QLineEdit(); self.emp_name.setPlaceholderText("ชื่อ-นามสกุล")
        btn_reg = QPushButton("📸 เลือกรูปและบันทึก"); btn_reg.clicked.connect(self.register_user)
        layout.addWidget(self.emp_id); layout.addWidget(self.emp_name); layout.addWidget(btn_reg)
        
        self.table = QTableWidget(0, 2); self.table.setHorizontalHeaderLabels(["ID", "Name"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table); self.load_users()

        btn_save = QPushButton("💾 บันทึกและรีสตาร์ท"); btn_save.clicked.connect(self.save_all)
        layout.addWidget(btn_save)
        
    def load_users(self):
        conn = get_db_conn()
        if not conn: return
        cur = conn.cursor(dictionary=True); cur.execute("SELECT employee_id, name FROM employees")
        rows = cur.fetchall(); self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(r['employee_id']))
            self.table.setItem(i, 1, QTableWidgetItem(r['name']))
        conn.close()

    def register_user(self):
        f, _ = QFileDialog.getOpenFileName(self, "เลือกรูป", "", "Images (*.jpg *.png)")
        if f and self.emp_id.text():
            if not os.path.exists("images"): os.makedirs("images")
            path = f"images/{self.emp_id.text()}.jpg"
            shutil.copy(f, path)
            conn = get_db_conn(); cur = conn.cursor()
            cur.execute("INSERT INTO employees (employee_id, name, image_path) VALUES (%s,%s,%s)", (self.emp_id.text(), self.emp_name.text(), path))
            conn.commit(); conn.close(); self.load_users()
            self.parent.ai.load_faces()

    def save_all(self):
        # บันทึกค่าลง config.json
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_config, f, indent=4, ensure_ascii=False)
        QMessageBox.information(self, "สำเร็จ", "บันทึกการตั้งค่าแล้ว")

# --- 5. AI WORKER (แก้ไขกรอบไม่ตรง) ---
class FaceAIThread(QThread):
    result_ready = pyqtSignal(list)
    db_error = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config; self.current_frame = None; self.is_running = True
        self.known_embeddings, self.known_ids, self.known_names = [], [], []
        self.last_recorded = {}; self.load_faces()

    def load_faces(self):
        conn = get_db_conn()
        if not conn: 
            self.db_error.emit("เชื่อมต่อ DB ไม่ได้")
            return
        cur = conn.cursor(dictionary=True); cur.execute("SELECT employee_id, name, image_path FROM employees")
        for r in cur.fetchall():
            if os.path.exists(r['image_path']):
                emb = DeepFace.represent(img_path=r['image_path'], model_name="Facenet512", enforce_detection=False)[0]["embedding"]
                self.known_embeddings.append(emb); self.known_ids.append(r['employee_id']); self.known_names.append(r['name'])
        conn.close()

    def run(self):
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        while self.is_running:
            if self.current_frame is not None:
                frame = self.current_frame.copy()
                small = cv2.resize(frame, (0,0), fx=0.5, fy=0.5)
                faces = face_cascade.detectMultiScale(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), 1.2, 5)
                res_list = []
                for (x, y, w, h) in faces:
                    # ⚠️ แก้ปัญหาพิกัด: คูณ 2 กลับเพื่อให้กรอบตรงหน้า
                    real_x, real_y, real_w, real_h = x*2, y*2, w*2, h*2
                    try:
                        face_rgb = cv2.cvtColor(frame[real_y:real_y+real_h, real_x:real_x+real_w], cv2.COLOR_BGR2RGB)
                        rep = DeepFace.represent(img_path=face_rgb, model_name="Facenet512", enforce_detection=False)
                        if rep:
                            dists = [1-(np.dot(rep[0]["embedding"], e)/(np.linalg.norm(rep[0]["embedding"])*np.linalg.norm(e))) for e in self.known_embeddings]
                            if dists and min(dists) < self.config.get("threshold", 0.3):
                                idx = dists.index(min(dists))
                                name = self.known_names[idx]
                                self.record_attendance(self.known_ids[idx], name, frame)
                                res_list.append((real_x, real_y, real_w, real_h, name, "OK"))
                            else: 
                                res_list.append((real_x, real_y, real_w, real_h, "ไม่ทราบชื่อ", "FAIL"))
                    except: pass
                self.result_ready.emit(res_list); self.current_frame = None
            self.msleep(100)

    def record_attendance(self, emp_id, name, frame):
        now = datetime.now()
        if emp_id in self.last_recorded and (now - self.last_recorded[emp_id]).total_seconds() < 60: return
        conn = get_db_conn()
        if not conn: return
        cur = conn.cursor()
        path = f"attendance_images/{emp_id}_{now.strftime('%H%M%S')}.jpg"
        cv2.imwrite(path, frame)
        cur.execute("INSERT INTO attendance_logs (employee_id, employee_name, check_time) VALUES (%s,%s,%s)", (emp_id, name, now))
        conn.commit(); conn.close(); self.last_recorded[emp_id] = now

# --- 6. MAIN WINDOW ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Face Station - Professional Hybrid")
        self.setFixedSize(1200, 750)
        
        central = QWidget(); self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # ฝั่งซ้าย: กล้อง
        left_layout = QVBoxLayout()
        self.video_label = QLabel(); self.video_label.setFixedSize(800, 600)
        self.video_label.setStyleSheet("background: black; border-radius: 10px; border: 2px solid #333;")
        left_layout.addWidget(self.video_label)
        self.status = QLabel("🖥️ CPU: 0% | Web: http://localhost:9876/dashboard")
        left_layout.addWidget(self.status)
        main_layout.addLayout(left_layout)

        # ฝั่งขวา: Sidebar
        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel("🕒 ประวัติการเข้างานล่าสุด"))
        self.log_table = QTableWidget(15, 2); self.log_table.setHorizontalHeaderLabels(["ชื่อ", "เวลา"])
        self.log_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right_layout.addWidget(self.log_table)
        
        btn_admin = QPushButton("⚙️ ตั้งค่าระบบ (Admin Mode)")
        btn_admin.setFixedHeight(50); btn_admin.clicked.connect(lambda: AdminWindow(self).exec())
        right_layout.addWidget(btn_admin)
        right_box = QWidget(); right_box.setLayout(right_layout); right_box.setFixedWidth(350)
        main_layout.addWidget(right_box)

        # เริ่มต้นกล้องและ AI
        self.cap = cv2.VideoCapture(current_config.get("camera_index", 0), cv2.CAP_DSHOW)
        self.ai = FaceAIThread(current_config)
        self.ai.result_ready.connect(self.update_res)
        self.ai.db_error.connect(lambda m: self.status.setText(m))
        self.ai.start()

        self.timer = QTimer(); self.timer.timeout.connect(self.update_frame); self.timer.start(30)
        self.last_res = []

    def update_frame(self):
        ret, frame = self.cap.read()
        if ret:
            if self.ai.current_frame is None: self.ai.current_frame = frame.copy()
            for (x,y,w,h,n,s) in self.last_res:
                color = (0, 255, 0) if s == "OK" else (0, 0, 255)
                cv2.rectangle(frame, (x, y), (x+w, y+h), color, 2)
                # วาดภาษาไทย
                frame = draw_thai_text(frame, n, (x, y-35), font_size=28, color=color)
            
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.shape[1]*3, QImage.Format.Format_RGB888)
            self.video_label.setPixmap(QPixmap.fromImage(img).scaled(800,600, Qt.AspectRatioMode.KeepAspectRatio))
        self.status.setText(f"🖥️ CPU: {psutil.cpu_percent()}% | RAM: {psutil.virtual_memory().percent}%")

    def update_res(self, res): # แก้ไข NameError
        self.last_res = res
        conn = get_db_conn() # ใช้ชื่อ get_db_conn แทน get_db_connection
        if conn:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT employee_name, DATE_FORMAT(check_time, '%H:%i:%s') as time FROM attendance_logs ORDER BY id DESC LIMIT 15")
            for i, r in enumerate(cur.fetchall()):
                self.log_table.setItem(i, 0, QTableWidgetItem(r['employee_name']))
                self.log_table.setItem(i, 1, QTableWidgetItem(r['time']))
            conn.close()

if __name__ == "__main__":
    if not os.path.exists("images"): os.makedirs("images")
    # รัน Web Server แยก Thread
    threading.Thread(target=lambda: uvicorn.run(web_app, host="0.0.0.0", port=9876), daemon=True).start()
    app = QApplication(sys.argv)
    win = MainWindow(); win.show(); sys.exit(app.exec())