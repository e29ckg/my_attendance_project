import sys, cv2, numpy as np, os, json, mysql.connector, psutil, shutil, requests
import winsound
from datetime import datetime
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from deepface import DeepFace
from PIL import Image, ImageDraw, ImageFont

# --- 1. CONFIG & DB CORE ---
CONFIG_FILE = "config.json"

def load_config():
    print(">>> [1/5] กำลังโหลดไฟล์ตั้งค่า (config.json)...")
    if not os.path.exists(CONFIG_FILE):
        print("!!! ไม่พบไฟล์ config.json กรุณาตรวจสอบ")
        sys.exit()
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        conf = json.load(f)
        print(f"--- โหลด Config สำเร็จ (กล้อง Index: {conf.get('camera_index')})")
        return conf

current_config = load_config()

def get_db_conn():
    try:
        return mysql.connector.connect(
            host=current_config.get("db_host"),
            user=current_config.get("db_user"),
            password=current_config.get("db_password"),
            database=current_config.get("db_name"),
            connect_timeout=3
        )
    except Exception as e:
        print(f"!!! Error: เชื่อมต่อ Database ล้มเหลว: {e}")
        return None

# --- 2. UTILS ---
def draw_thai_text(img, text, position, font_size=25, color=(0, 255, 0)):
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    try: font = ImageFont.truetype("tahoma.ttf", font_size)
    except: font = ImageFont.load_default()
    draw.text(position, text, font=font, fill=color)
    return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

def get_available_cameras():
    # แก้ไข: ลบ cv2.CAP_DSHOW ออกเพื่อให้ค้นหากล้องได้ทุกประเภท
    available_indices = []
    for i in range(5):
        cap = cv2.VideoCapture(i) 
        if cap.isOpened():
            available_indices.append(i); cap.release()
    return available_indices

# --- 3. DIALOGS (Register & Admin) ---
class RegisterDialog(QDialog):
    def __init__(self, frame=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📸 ลงทะเบียนพนักงานใหม่")
        self.setFixedSize(450, 700)
        self.frame = frame; self.parent = parent; self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        self.img_label = QLabel(); self.img_label.setFixedSize(400, 300)
        self.img_label.setStyleSheet("border: 2px dashed #aaa; background: #eee;")
        if self.frame is not None: self.update_preview(self.frame)
        layout.addWidget(self.img_label, alignment=Qt.AlignmentFlag.AlignCenter)

        btn_browse = QPushButton("📁 เลือกรูปภาพจากเครื่อง"); btn_browse.clicked.connect(self.browse)
        layout.addWidget(btn_browse)

        form = QFormLayout()
        self.id_in = QLineEdit(); self.name_in = QLineEdit()
        self.role_in = QComboBox(); self.role_in.addItems(["พนักงานทั่วไป", "รปภ", "แม่บ้าน", "นักศึกษา"])
        form.addRow("รหัสพนักงาน:", self.id_in); form.addRow("ชื่อ-นามสกุล:", self.name_in); form.addRow("ประเภท:", self.role_in)
        layout.addLayout(form)

        btn_save = QPushButton("💾 บันทึกข้อมูล"); btn_save.setFixedHeight(50)
        btn_save.setStyleSheet("background: #27ae60; color: white; font-weight: bold;")
        btn_save.clicked.connect(self.save_data); layout.addWidget(btn_save)

    def update_preview(self, f):
        rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
        img = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.shape[1]*3, QImage.Format.Format_RGB888)
        self.img_label.setPixmap(QPixmap.fromImage(img).scaled(400, 300, Qt.AspectRatioMode.KeepAspectRatio))

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "เลือกรูป", "", "Images (*.jpg *.png)")
        if path: self.frame = cv2.imread(path); self.update_preview(self.frame)

    def save_data(self):
        emp_id = self.id_in.text(); name = self.name_in.text()
        if not emp_id or not name or self.frame is None: 
            QMessageBox.warning(self, "เตือน", "กรุณากรอกข้อมูลให้ครบ"); return
        
        if not os.path.exists("images"): os.makedirs("images")
        path = f"images/{emp_id}.jpg"; cv2.imwrite(path, self.frame)
        
        conn = get_db_conn()
        if conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO employees (employee_id, name, role, image_path) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE name=%s",
                        (emp_id, name, self.role_in.currentText(), path, name))
            conn.commit(); conn.close()
            self.parent.ai.load_faces(); self.accept()

class AdminWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ ตั้งค่ากล้องและระบบ"); self.setFixedSize(400, 300); self.parent = parent
        layout = QVBoxLayout(self); form = QFormLayout()
        
        self.cam_combo = QComboBox()
        for idx in get_available_cameras(): self.cam_combo.addItem(f"Camera {idx}", idx)
        
        self.threshold = QDoubleSpinBox(); self.threshold.setRange(0.1, 0.8); self.threshold.setValue(current_config.get("threshold", 0.3))
        
        form.addRow("เลือกกล้อง:", self.cam_combo); form.addRow("AI Threshold:", self.threshold)
        layout.addLayout(form)
        
        btn = QPushButton("💾 บันทึกและสลับกล้อง"); btn.clicked.connect(self.save); layout.addWidget(btn)

    def save(self):
        current_config["camera_index"] = self.cam_combo.currentData()
        current_config["threshold"] = self.threshold.value()
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(current_config, f, indent=4)
        self.parent.restart_camera(current_config["camera_index"])
        self.accept()

# --- 4. AI THREAD ---
class FaceAIThread(QThread):
    result_ready = pyqtSignal(list)

    def __init__(self, config):
        super().__init__()
        self.config = config; self.current_frame = None; self.is_running = True
        self.known_embeddings, self.known_ids, self.known_names = [], [], []
        self.last_recorded = {}

    def load_faces(self):
        print(">>> [2/5] กำลังโหลดใบหน้าพนักงานจาก Database...")
        conn = get_db_conn()
        if not conn: return
        
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT employee_id, name, image_path FROM employees")
        rows = cur.fetchall()
        
        self.known_embeddings, self.known_ids, self.known_names = [], [], []
        
        for i, r in enumerate(rows):
            if os.path.exists(r['image_path']):
                try:
                    # ใส่ Try-Except เพื่อกันโปรแกรมหลุดเวลาเจอรูปเสีย
                    print(f"    ({i+1}/{len(rows)}) ประมวลผล: {r['name']}")
                    
                    objs = DeepFace.represent(
                        img_path=r['image_path'], 
                        model_name="Facenet512", 
                        enforce_detection=False
                    )
                    
                    if len(objs) > 0:
                        emb = objs[0]["embedding"]
                        self.known_embeddings.append(emb)
                        self.known_ids.append(r['employee_id'])
                        self.known_names.append(r['name'])
                        
                except Exception as e:
                    # แจ้งเตือนแต่ไม่ปิดโปรแกรม
                    print(f"    !!! ข้ามพนักงาน {r['name']} เนื่องจากเกิดข้อผิดพลาด: {e}")
            else:
                print(f"    !!! ไม่พบไฟล์รูปภาพของ: {r['name']}")
                
        conn.close()
        print(f">>> [3/5] โหลดใบหน้าเสร็จสิ้น! (สำเร็จ {len(self.known_names)}/{len(rows)} คน)")

    def run(self):
        print(">>> [4/5] กำลังโหลด AI Model (Facenet512)...")
        self.load_faces()
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        while self.is_running:
            if self.current_frame is not None:
                frame = self.current_frame.copy()
                small = cv2.resize(frame, (0,0), fx=0.5, fy=0.5)
                faces = face_cascade.detectMultiScale(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), 1.2, 5)
                res = []
                for (x, y, w, h) in faces:
                    rx, ry, rw, rh = x*2, y*2, w*2, h*2 
                    try:
                        face_rgb = cv2.cvtColor(frame[ry:ry+rh, rx:rx+rw], cv2.COLOR_BGR2RGB)
                        rep = DeepFace.represent(img_path=face_rgb, model_name="Facenet512", enforce_detection=False)
                        if rep:
                            dists = [1-(np.dot(rep[0]["embedding"], e)/(np.linalg.norm(rep[0]["embedding"])*np.linalg.norm(e))) for e in self.known_embeddings]
                            if dists and min(dists) < self.config.get("threshold", 0.3):
                                idx = dists.index(min(dists))
                                self.record_attendance(self.known_ids[idx], self.known_names[idx], frame)
                                res.append((rx, ry, rw, rh, self.known_names[idx], "OK"))
                            else: res.append((rx, ry, rw, rh, "ไม่พบข้อมูล", "FAIL"))
                    except: pass
                self.result_ready.emit(res); self.current_frame = None
            self.msleep(100)

    def record_attendance(self, emp_id, name, frame):
        now = datetime.now()
        if self.config.get("enable_cooldown") and emp_id in self.last_recorded:
            if (now - self.last_recorded[emp_id]).total_seconds() < self.config.get("cooldown_seconds", 3600): return
        
        conn = get_db_conn()
        if conn:
            try:
                cur = conn.cursor() 
                if not os.path.exists("attendance_images"): os.makedirs("attendance_images")
                img_path = f"attendance_images/{emp_id}_{now.strftime('%H%M%S')}.jpg"; cv2.imwrite(img_path, frame)
                cur.execute("INSERT INTO attendance_logs (employee_id, employee_name, check_time, evidence_image) VALUES (%s,%s,%s,%s)",
                            (emp_id, name, now, img_path))
                conn.commit()
                winsound.Beep(1000, 300) # เสียงติ๊ด
                self.last_recorded[emp_id] = now
                print(f"✅ บันทึกสำเร็จ: {name}")
                if self.config.get("enable_telegram"): self.send_tg(name, now, img_path)
            except Exception as e: print(f"❌ DB Error: {e}")
            finally: conn.close()

    def send_tg(self, name, t, img_path):
        token = self.config.get("telegram_bot_token"); chat_id = self.config.get("telegram_chat_id")
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        caption = f"🔔 แจ้งเตือนการสแกนใบหน้า\n👤 พนักงาน: {name}\n⏰ เวลา: {t.strftime('%H:%M:%S')}\n✅ บันทึกข้อมูลเรียบร้อย"
        try:
            with open(img_path, 'rb') as photo:
                requests.post(url, files={'photo': photo}, data={'chat_id': chat_id, 'caption': caption}, timeout=10)
        except Exception as e: print(f"⚠️ Telegram Error: {e}")

# --- 5. MAIN WINDOW ---
class ScannerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        print(">>> [5/5] กำลังเริ่มหน้าจอหลัก..."); self.setWindowTitle("AI Face Scanner Station"); self.setFixedSize(1200, 750)
        central = QWidget(); self.setCentralWidget(central); layout = QHBoxLayout(central)
        left = QVBoxLayout(); self.video = QLabel(); self.video.setFixedSize(800, 600)
        self.video.setStyleSheet("background: #000; border-radius: 10px;"); left.addWidget(self.video)
        self.status = QLabel("Ready"); left.addWidget(self.status); layout.addLayout(left)
        right = QVBoxLayout(); right.addWidget(QLabel("🕒 ประวัติล่าสุด"))
        self.table = QTableWidget(15, 2); self.table.setHorizontalHeaderLabels(["ชื่อ", "เวลา"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); right.addWidget(self.table)
        self.btn_reg = QPushButton("➕ เพิ่มพนักงาน (Snapshot)"); self.btn_reg.setFixedHeight(60)
        self.btn_reg.setStyleSheet("background: #4361ee; color: white; font-weight: bold;"); self.btn_reg.clicked.connect(self.take_snapshot); right.addWidget(self.btn_reg)
        self.btn_admin = QPushButton("⚙️ ตั้งค่ากล้อง"); self.btn_admin.clicked.connect(self.open_admin); right.addWidget(self.btn_admin); layout.addLayout(right)
        
        # แก้ไข: ใช้ค่าเริ่มต้นของ OpenCV แทนการบังคับ DSHOW
        self.cap = cv2.VideoCapture(current_config.get("camera_index", 0))
        
        self.ai = FaceAIThread(current_config); self.ai.result_ready.connect(self.on_ai); self.ai.start()
        self.timer = QTimer(); self.timer.timeout.connect(self.update_frame); self.timer.start(30); self.last_res = []; self.raw_f = None

    def update_frame(self):
        ret, frame = self.cap.read()
        if ret:
            self.raw_f = frame.copy()
            if self.ai.current_frame is None: self.ai.current_frame = frame.copy()
            for (x,y,w,h,n,s) in self.last_res:
                c = (0,255,0) if s=="OK" else (0,0,255); cv2.rectangle(frame, (x,y), (x+w,y+h), c, 2)
                frame = draw_thai_text(frame, n, (x, y-35))
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.shape[1]*3, QImage.Format.Format_RGB888)
            self.video.setPixmap(QPixmap.fromImage(img).scaled(800, 600))
        self.status.setText(f"🖥️ CPU: {psutil.cpu_percent()}% | RAM: {psutil.virtual_memory().percent}%")

    def on_ai(self, res):
        self.last_res = res; conn = get_db_conn()
        if conn:
            cur = conn.cursor(dictionary=True); cur.execute("SELECT employee_name, DATE_FORMAT(check_time, '%H:%i:%s') as t FROM attendance_logs ORDER BY id DESC LIMIT 15")
            for i, r in enumerate(cur.fetchall()):
                self.table.setItem(i, 0, QTableWidgetItem(r['employee_name'])); self.table.setItem(i, 1, QTableWidgetItem(r['t']))
            conn.close()

    def take_snapshot(self):
        if self.raw_f is not None: RegisterDialog(self.raw_f, self).exec()

    def open_admin(self):
        AdminWindow(self).exec()

    def restart_camera(self, idx):
        if self.cap.isOpened(): self.cap.release()
        # แก้ไข: ใช้ค่าเริ่มต้นของ OpenCV
        self.cap = cv2.VideoCapture(idx)

if __name__ == "__main__":
    app = QApplication(sys.argv); win = ScannerWindow(); win.show(); sys.exit(app.exec())