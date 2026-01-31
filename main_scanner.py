import sys, cv2, numpy as np, os, json, sqlite3, psutil, requests, time
import winsound
from datetime import datetime
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from deepface import DeepFace
from PIL import Image, ImageDraw, ImageFont

# --- 1. CONFIG & DB ---
CONFIG_FILE = "config.json"
DB_FILE = "attendance.db"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default_conf = {
            "camera_index": 0, "threshold": 0.35, 
            "enable_telegram": True, "telegram_bot_token": "", "telegram_chat_id": ""
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(default_conf, f, indent=4)
        return default_conf
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)

current_config = load_config()

def get_db_conn():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn
    except: return None

def init_db():
    conn = get_db_conn()
    if conn:
        cur = conn.cursor()
        # ตารางพนักงาน
        cur.execute("""CREATE TABLE IF NOT EXISTS employees (employee_id TEXT PRIMARY KEY, name TEXT, role TEXT, image_path TEXT, embedding TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # ตาราง Logs (ตัด status/log_type ที่ซับซ้อนออก เหลือแค่พื้นฐาน)
        cur.execute("""CREATE TABLE IF NOT EXISTS attendance_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id TEXT, employee_name TEXT, check_time DATETIME, evidence_image TEXT, log_type TEXT DEFAULT 'SCAN', status TEXT DEFAULT '-')""")
        
        # Migration: เผื่อ DB เก่าไม่มีคอลัมน์ embedding
        try: cur.execute("SELECT embedding FROM employees LIMIT 1")
        except: cur.execute("ALTER TABLE employees ADD COLUMN embedding TEXT")
        conn.commit(); conn.close()

def get_thai_date():
    months = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    n = datetime.now()
    return f"{n.day} {months[n.month]} {n.year + 543}"

def draw_thai_text(img, text, position, font_size=25, color=(0, 255, 0)):
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    try: font = ImageFont.truetype("tahoma.ttf", font_size)
    except: font = ImageFont.load_default()
    draw.text(position, text, font=font, fill=color)
    return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

# --- 2. AI WORKER ---
class FaceAIThread(QThread):
    result_ready = pyqtSignal(list)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.current_frame = None
        self.is_running = True
        self.known_embeddings, self.known_ids, self.known_names = [], [], []
        self.last_recorded = {} # เก็บเวลาล่าสุดที่สแกนของแต่ละคน
        
        # [NEW] ตัวแปรสำหรับนับเฟรมเพื่อข้ามการประมวลผล
        self.frame_counter = 0
        self.skip_limit = config.get("frame_skip", 5) # ค่า Default คือข้าม 5 เฟรม ทำ 1 เฟรม

    def load_faces(self):
        print(">>> Loading Faces...")
        conn = get_db_conn()
        if not conn: return
        cur = conn.cursor(); cur.execute("SELECT employee_id, name, image_path, embedding FROM employees")
        rows = cur.fetchall()
        self.known_embeddings, self.known_ids, self.known_names = [], [], []
        for r in rows:
            if r['embedding']:
                try:
                    self.known_embeddings.append(json.loads(r['embedding']))
                    self.known_ids.append(r['employee_id']); self.known_names.append(r['name'])
                except: pass
            elif os.path.exists(r['image_path']):
                try:
                    objs = DeepFace.represent(img_path=r['image_path'], model_name="Facenet512", enforce_detection=False)
                    if objs:
                        emb = objs[0]["embedding"]
                        self.known_embeddings.append(emb); self.known_ids.append(r['employee_id']); self.known_names.append(r['name'])
                        cur.execute("UPDATE employees SET embedding = ? WHERE employee_id = ?", (json.dumps(emb), r['employee_id'])); conn.commit()
                except: pass
        conn.close()

    def run(self):
        self.load_faces()
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        
        while self.is_running:
            if self.current_frame is not None:
                
                # [NEW] Logic 1: Auto-Skip Frame (ลดภาระ CPU)
                self.frame_counter += 1
                if self.frame_counter < self.skip_limit:
                    self.current_frame = None
                    self.msleep(20) # พักสั้นๆ แล้ววนลูปใหม่เลย ไม่ต้องทำ AI
                    continue
                
                self.frame_counter = 0 # ครบรอบแล้ว รีเซ็ตตัวนับ

                frame = self.current_frame.copy()

                # [NEW] Logic 2: ลดขนาดภาพเหลือ 25% (0.25) เพื่อให้ AI เร็วขึ้น
                small = cv2.resize(frame, (0,0), fx=0.25, fy=0.25)
                
                faces = face_cascade.detectMultiScale(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), 1.2, 5)
                res = []
                for (x, y, w, h) in faces:
                    # [NEW] ปรับตัวคูณพิกัดกลับ (คูณ 4 เพราะเราย่อลงมา 0.25)
                    rx, ry, rw, rh = x*4, y*4, w*4, h*4 
                    try:
                        face_rgb = cv2.cvtColor(frame[ry:ry+rh, rx:rx+rw], cv2.COLOR_BGR2RGB)
                        rep = DeepFace.represent(img_path=face_rgb, model_name="Facenet512", enforce_detection=False)
                        if rep:
                            dists = [1-(np.dot(rep[0]["embedding"], e)/(np.linalg.norm(rep[0]["embedding"])*np.linalg.norm(e))) for e in self.known_embeddings]
                            if dists and min(dists) < self.config.get("threshold", 0.3):
                                idx = dists.index(min(dists))
                                # เจอหน้า -> ส่งไปประมวลผลการบันทึก
                                self.process_scan(self.known_ids[idx], self.known_names[idx], frame)
                                res.append((rx, ry, rw, rh, self.known_names[idx], "OK"))
                            else: res.append((rx, ry, rw, rh, "Unknown", "FAIL"))
                    except: pass
                self.result_ready.emit(res); self.current_frame = None
            
            # ลดเวลาพักลงเล็กน้อยเพื่อให้ UI ตอบสนองไวขึ้นในจังหวะที่ไม่ได้ประมวลผล
            self.msleep(10)

    def process_scan(self, emp_id, name, frame):
        now = datetime.now()
        
        # 1. เช็ค Cooldown 1 นาที (60 วินาที)
        # ถ้าเคยสแกนไปแล้วภายใน 60 วิ ให้ข้าม (ไม่ทำอะไร)
        if emp_id in self.last_recorded:
            if (now - self.last_recorded[emp_id]).total_seconds() < 60:
                return 

        conn = get_db_conn()
        if conn:
            try:
                cur = conn.cursor()
                
                # 2. ตรวจสอบว่าเป็น "ครั้งแรกของวัน" หรือไม่?
                cur.execute("SELECT COUNT(*) FROM attendance_logs WHERE employee_id=? AND date(check_time)=date('now','localtime')", (emp_id,))
                count_today = cur.fetchone()[0]
                is_first_time = (count_today == 0)

                # 3. บันทึกข้อมูล (บันทึกหมด ไม่สนเงื่อนไขอื่น)
                if not os.path.exists("attendance_images"): os.makedirs("attendance_images")
                img_path = f"attendance_images/{emp_id}_{now.strftime('%H%M%S')}.jpg"
                cv2.imwrite(img_path, frame)
                
                # log_type='SCAN' คือค่ากลางๆ ให้เว็บไปคำนวณเอาเอง
                cur.execute("INSERT INTO attendance_logs (employee_id, employee_name, check_time, evidence_image, log_type, status) VALUES (?,?,?,?,?,?)",
                            (emp_id, name, now, img_path, "SCAN", "บันทึกแล้ว"))
                conn.commit()
                
                # เสียงตอบรับ (ติ๊ดเดียวพอ)
                winsound.Beep(1000, 200)

                # อัปเดตเวลาล่าสุดใน Memory
                self.last_recorded[emp_id] = now
                print(f"✅ บันทึก: {name} (First Time: {is_first_time})")

                # 4. ส่ง Telegram (เฉพาะครั้งแรกของวัน)
                if is_first_time and self.config.get("enable_telegram"):
                    self.send_tg(name, now, img_path)

            except Exception as e: print(f"❌ DB Error: {e}")
            finally: conn.close()

    def send_tg(self, name, t, img_path):
        token = self.config.get("telegram_bot_token"); chat_id = self.config.get("telegram_chat_id")
        if not token: return
        caption = f"☀️ สวัสดีตอนเช้า/เริ่มงาน\n👤 พนักงาน: {name}\n⏰ เวลา: {t.strftime('%H:%M:%S')}\n✅ บันทึกเวลาเรียบร้อย"
        try:
            with open(img_path, 'rb') as photo:
                requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", files={'photo': photo}, data={'chat_id': chat_id, 'caption': caption}, timeout=5)
        except: pass

# --- 3. UI (CLEAN LAYOUT) ---
class ScannerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Time Recorder (Scan Only)"); self.setFixedSize(1000, 700)
        central = QWidget(); self.setCentralWidget(central); main_layout = QHBoxLayout(central)

        # ซ้าย: กล้อง
        left_layout = QVBoxLayout()
        self.video = QLabel(); self.video.setFixedSize(640, 480)
        self.video.setStyleSheet("background: #000; border: 3px solid #333; border-radius: 10px;")
        left_layout.addWidget(self.video, alignment=Qt.AlignmentFlag.AlignCenter)
        
        # สถานะด้านล่างกล้อง
        self.lbl_status = QLabel("พร้อมใช้งาน..."); self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setStyleSheet("font-size: 18px; color: #555; margin-top: 10px;")
        left_layout.addWidget(self.lbl_status)
        main_layout.addLayout(left_layout)

        # ขวา: นาฬิกาและรายการ
        right_layout = QVBoxLayout()
        
        # นาฬิกา
        clock_box = QFrame(); clock_box.setStyleSheet("background: #f8f9fa; border-radius: 10px; border: 1px solid #ddd;")
        c_layout = QVBoxLayout(clock_box)
        self.lbl_time = QLabel("00:00"); self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_time.setStyleSheet("font-size: 50px; font-weight: bold; color: #2c3e50;")
        self.lbl_date = QLabel("..."); self.lbl_date.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_date.setStyleSheet("font-size: 20px; color: #7f8c8d;")
        c_layout.addWidget(self.lbl_time); c_layout.addWidget(self.lbl_date)
        right_layout.addWidget(clock_box)

        # ตาราง
        self.table = QTableWidget(10, 2); self.table.setHorizontalHeaderLabels(["ชื่อ", "เวลา"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False); self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setStyleSheet("QTableWidget{font-size:16px;} QHeaderView::section{font-size:16px; font-weight:bold;}")
        right_layout.addWidget(self.table)

        main_layout.addLayout(right_layout)

        # System Start
        self.cap = cv2.VideoCapture(current_config.get("camera_index", 0))
        self.ai = FaceAIThread(current_config)
        self.ai.result_ready.connect(self.on_ai)
        self.ai.start()
        
        self.timer = QTimer(); self.timer.timeout.connect(self.update_frame); self.timer.start(30)
        self.last_res = []; self.raw_f = None

    def update_frame(self):
        self.lbl_time.setText(datetime.now().strftime("%H:%M:%S")); self.lbl_date.setText(get_thai_date())
        ret, frame = self.cap.read()
        if ret:
            if self.ai.current_frame is None: self.ai.current_frame = frame.copy()
            for (x,y,w,h,n,s) in self.last_res:
                col = (0,255,0) if s=="OK" else (0,0,255)
                cv2.rectangle(frame, (x,y), (x+w,y+h), col, 2)
                frame = draw_thai_text(frame, n, (x, y-35))
            
            # แปลงภาพแสดงผล
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            bytes_per_line = ch * w
            qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
            self.video.setPixmap(QPixmap.fromImage(qimg).scaled(640, 480, Qt.AspectRatioMode.KeepAspectRatio))

    def on_ai(self, res):
        self.last_res = res
        # อัปเดตตารางแสดงผล (ดึง 10 รายการล่าสุด)
        conn = get_db_conn()
        if conn:
            cur = conn.cursor()
            # 1. แก้ SQL: ดึง check_time แบบเต็ม (เดิมดึงแค่ HH:MM:SS)
            cur.execute("SELECT employee_name, check_time FROM attendance_logs ORDER BY id DESC LIMIT 10")
            data = cur.fetchall()
            self.table.setRowCount(len(data))
            
            for i, r in enumerate(data):
                # ชื่อพนักงาน
                self.table.setItem(i, 0, QTableWidgetItem(r['employee_name']))
                
                # 2. แปลงเวลาเป็นรูปแบบไทย (วัน/เดือน/ปี เวลา)
                try:
                    ts = r['check_time']
                    # ตัดเศษวินาทีทิ้งถ้ามี (เช่น .123456)
                    if "." in ts: ts = ts.split(".")[0]
                    
                    # แปลงข้อความ DB เป็นตัวแปร datetime
                    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    
                    # จัดฟอร์แมตใหม่: 27/01/2569 08:30:05
                    thai_time_str = f"{dt.day:02}/{dt.month:02}/{dt.year+543} {dt.strftime('%H:%M:%S')}"
                except Exception as e:
                    thai_time_str = r['check_time'] # กรณีผิดพลาดให้โชว์ค่าเดิม
                
                # ใส่ลงตาราง
                self.table.setItem(i, 1, QTableWidgetItem(thai_time_str))
                
            conn.close()

if __name__ == "__main__":
    init_db()
    app = QApplication(sys.argv); win = ScannerWindow(); win.show(); sys.exit(app.exec())