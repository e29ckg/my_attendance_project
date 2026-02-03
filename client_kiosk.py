import sys
import cv2
import time
import requests
import winsound
import os
import threading
import numpy as np
from datetime import datetime
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from gtts import gTTS
import pygame

# --- CONFIG ---
# ⚠️ เปลี่ยน localhost เป็น IP ของเครื่อง Server (เช่น http://192.168.1.50:9876)
SERVER_URL = "http://localhost:9876" 
CAMERA_INDEX = 1  # 0 = กล้องหลัก, 1 = กล้องเสริม
CHECK_INTERVAL = 5  # เช็ค Server ทุก 5 วินาที

# เริ่มระบบเสียง
try:
    pygame.mixer.init()
except:
    pass

# --- GLOBAL FUNCTION: เล่นเสียงทักทาย ---
def play_greeting(name):
    """
    ฟังก์ชันพูดชื่อ: เช็คไฟล์ -> ถ้าไม่มีให้สร้าง -> เล่นเสียง
    """
    try:
        if not os.path.exists("sounds"):
            os.makedirs("sounds")
            
        filename = f"sounds/{name}.mp3"
        
        # ถ้ายังไม่มีไฟล์เสียง ให้ Google สร้างให้
        if not os.path.exists(filename):
            # print(f"🔊 สร้างเสียงใหม่สำหรับ: {name}")
            tts = gTTS(text=f"สวัสดีค่ะ คุณ{name}", lang='th')
            tts.save(filename)
            
        # รอให้ channel ว่างก่อนเล่น (ป้องกันเสียงตีกัน)
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)
            
        pygame.mixer.music.load(filename)
        pygame.mixer.music.play()
        
    except Exception as e:
        print(f"TTS Error: {e}")
        winsound.Beep(1000, 200)

# --- WORKER: เช็คสถานะ Server (Heartbeat) ---
class ServerStatusThread(QThread):
    status_signal = pyqtSignal(bool, str) # (Online?, Latency)

    def run(self):
        while True:
            try:
                start_time = time.time()
                response = requests.get(f"{SERVER_URL}/health", timeout=2)
                if response.status_code == 200:
                    latency = int((time.time() - start_time) * 1000)
                    self.status_signal.emit(True, f"{latency} ms")
                else:
                    self.status_signal.emit(False, "Error")
            except:
                self.status_signal.emit(False, "Timeout")
            
            self.sleep(CHECK_INTERVAL)

# --- WORKER: ส่งภาพสแกน (Auto Scan) ---
class NetworkThread(QThread):
    result_ready = pyqtSignal(dict)
    
    def __init__(self):
        super().__init__()
        self.frame_to_send = None
        self.is_busy = False

    def request_scan(self, frame):
        if not self.is_busy:
            # ย่อภาพก่อนส่ง
            h, w = frame.shape[:2]
            target_width = 640
            if w > target_width:
                scale = target_width / w
                frame = cv2.resize(frame, (0,0), fx=scale, fy=scale)
            
            self.frame_to_send = frame
            self.start()

    def run(self):
        if self.frame_to_send is not None:
            self.is_busy = True
            try:
                _, img_encoded = cv2.imencode('.jpg', self.frame_to_send)
                files = {'file': ('image.jpg', img_encoded.tobytes(), 'image/jpeg')}
                
                response = requests.post(f"{SERVER_URL}/scan", files=files, timeout=10)
                
                if response.status_code == 200:
                    self.result_ready.emit(response.json())
            except Exception as e:
                print(f"Scan Network Error: {e}")
            finally:
                self.is_busy = False

# --- UI MAIN WINDOW ---
class ClientWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Smart Attendance Kiosk")
        self.setFixedSize(1000, 750) # เพิ่มความสูงเผื่อส่วน Manual
        
        self.last_greeted_name = None 
        self.is_manual_mode = False # สถานะโหมดฉุกเฉิน
        self.current_frame = None # เก็บภาพปัจจุบันไว้ส่ง Manual
        
        # GUI Setup
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Header
        header_layout = QHBoxLayout()
        title = QLabel("📷 ระบบลงเวลาทำงาน (Face Recognition)")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        header_layout.addWidget(title)
        
        self.lbl_server_status = QLabel("⚪ Connecting...")
        self.lbl_server_status.setStyleSheet("font-size: 14px; padding: 5px; border: 1px solid #ccc; border-radius: 5px;")
        self.lbl_server_status.setAlignment(Qt.AlignmentFlag.AlignRight)
        header_layout.addWidget(self.lbl_server_status)
        main_layout.addLayout(header_layout)

        # Content
        content_layout = QHBoxLayout()
        
        # --- Left: Camera & Manual Input ---
        left_layout = QVBoxLayout()
        self.video = QLabel()
        self.video.setFixedSize(640, 480)
        self.video.setStyleSheet("background: #000; border: 2px solid #555;")
        left_layout.addWidget(self.video, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_action = QLabel("กรุณามองกล้อง...")
        self.lbl_action.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_action.setStyleSheet("font-size: 24px; font-weight: bold; color: #333; margin-top: 10px;")
        left_layout.addWidget(self.lbl_action)

        # [NEW] Manual Input Section (ซ่อนอยู่)
        self.manual_widget = QWidget()
        self.manual_widget.setStyleSheet("background: #f0f0f0; border-radius: 10px; margin-top: 10px;")
        man_layout = QHBoxLayout(self.manual_widget)
        
        self.txt_manual_id = QLineEdit()
        self.txt_manual_id.setPlaceholderText("กรอกรหัสพนักงาน...")
        self.txt_manual_id.setFont(QFont("Tahoma", 14))
        self.txt_manual_id.setStyleSheet("padding: 5px;")
        
        btn_send_manual = QPushButton("📸 บันทึก")
        btn_send_manual.setStyleSheet("background: #28a745; color: white; font-weight: bold; padding: 8px;")
        btn_send_manual.clicked.connect(self.submit_manual)
        
        btn_cancel_manual = QPushButton("❌ ยกเลิก")
        btn_cancel_manual.setStyleSheet("background: #dc3545; color: white; padding: 8px;")
        btn_cancel_manual.clicked.connect(self.toggle_manual_mode)
        
        man_layout.addWidget(self.txt_manual_id)
        man_layout.addWidget(btn_send_manual)
        man_layout.addWidget(btn_cancel_manual)
        
        left_layout.addWidget(self.manual_widget)
        self.manual_widget.hide() # ซ่อนก่อน

        # [NEW] Toggle Button
        self.btn_toggle_manual = QPushButton("⌨️ ระบบฉุกเฉิน (กรณีสแกนไม่ติด)")
        self.btn_toggle_manual.setStyleSheet("background: #ffc107; padding: 10px; font-weight: bold; border: none;")
        self.btn_toggle_manual.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_manual.clicked.connect(self.toggle_manual_mode)
        left_layout.addWidget(self.btn_toggle_manual)

        content_layout.addLayout(left_layout)

        # --- Right: Clock & Table ---
        right_layout = QVBoxLayout()
        self.lbl_time = QLabel("00:00:00")
        self.lbl_time.setStyleSheet("font-size: 50px; font-weight: bold; color: #0078d7;")
        self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.lbl_time)
        
        self.table = QTableWidget(10, 2)
        self.table.setHorizontalHeaderLabels(["ชื่อ-สกุล", "เวลาที่บันทึก"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setFont(QFont("Tahoma", 12))
        right_layout.addWidget(self.table)
        content_layout.addLayout(right_layout)

        main_layout.addLayout(content_layout)

        # --- SYSTEM INIT ---
        self.cap = cv2.VideoCapture(CAMERA_INDEX)
        self.face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        
        # Threads
        self.net_worker = NetworkThread()
        self.net_worker.result_ready.connect(self.on_scan_result)
        
        self.status_worker = ServerStatusThread()
        self.status_worker.status_signal.connect(self.update_server_status)
        self.status_worker.start()

        # Timer Loop
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_camera)
        self.timer.start(30)
        
        self.last_scan_time = 0
        self.server_online = False

    def update_server_status(self, is_online, msg):
        self.server_online = is_online
        if is_online:
            self.lbl_server_status.setText(f"🟢 Online ({msg})")
            self.lbl_server_status.setStyleSheet("background: #e6fffa; color: green; border: 1px solid green; padding:5px; border-radius:5px; font-weight:bold;")
        else:
            self.lbl_server_status.setText(f"🔴 Offline ({msg})")
            self.lbl_server_status.setStyleSheet("background: #ffe6e6; color: red; border: 1px solid red; padding:5px; border-radius:5px; font-weight:bold;")

    def toggle_manual_mode(self):
        """สลับโหมด Manual / Auto"""
        self.is_manual_mode = not self.is_manual_mode
        if self.is_manual_mode:
            self.manual_widget.show()
            self.btn_toggle_manual.hide()
            self.lbl_action.setText("กรุณากรอกรหัสพนักงาน...")
            self.lbl_action.setStyleSheet("color: #d35400; font-size: 24px; font-weight: bold; margin-top: 10px;")
            self.txt_manual_id.setFocus()
        else:
            self.manual_widget.hide()
            self.btn_toggle_manual.show()
            self.lbl_action.setText("กรุณามองกล้อง...")
            self.lbl_action.setStyleSheet("color: #333; font-size: 24px; font-weight: bold; margin-top: 10px;")
            self.txt_manual_id.clear()

    def submit_manual(self):
        """ส่งข้อมูล Manual ไป Server"""
        emp_id = self.txt_manual_id.text().strip()
        if not emp_id:
            QMessageBox.warning(self, "แจ้งเตือน", "กรุณากรอกรหัสพนักงาน")
            return
            
        if self.current_frame is None:
            return

        # UI Feedback
        self.lbl_action.setText("⏳ กำลังส่งข้อมูล...")
        QApplication.processEvents()

        try:
            # ย่อรูป
            small = cv2.resize(self.current_frame, (0,0), fx=0.5, fy=0.5)
            _, img_encoded = cv2.imencode('.jpg', small)
            
            files = {'file': ('manual.jpg', img_encoded.tobytes(), 'image/jpeg')}
            data = {'employee_id': emp_id}
            
            # ส่งไป API Manual
            res = requests.post(f"{SERVER_URL}/manual_scan", data=data, files=files, timeout=5)
            
            if res.status_code == 200:
                result = res.json()
                if result['status'] == 'OK':
                    QMessageBox.information(self, "สำเร็จ", f"บันทึก: {result['name']}")
                    self.toggle_manual_mode() # กลับสู่โหมดปกติ
                else:
                    QMessageBox.critical(self, "ผิดพลาด", result.get('message', 'Unknown Error'))
            else:
                QMessageBox.critical(self, "Error", "Server Error")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
        
        # คืนค่า Text
        if self.is_manual_mode:
            self.lbl_action.setText("กรุณากรอกรหัสพนักงาน...")

    def update_camera(self):
        self.lbl_time.setText(datetime.now().strftime("%H:%M:%S"))
        
        ret, frame = self.cap.read()
        if ret:
            frame = cv2.flip(frame, 1) # กลับด้านกระจก
            self.current_frame = frame.copy() # เก็บภาพไว้ใช้ Manual

            # --- Auto Mode Logic ---
            if not self.is_manual_mode:
                # Detect Face
                small = cv2.resize(frame, (0,0), fx=0.5, fy=0.5)
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                faces = self.face_cascade.detectMultiScale(gray, 1.2, 5)
                
                face_found = False
                for (x, y, w, h) in faces:
                    rx, ry, rw, rh = x*2, y*2, w*2, h*2
                    color = (0, 255, 0) if self.server_online else (0, 0, 255)
                    cv2.rectangle(frame, (rx, ry), (rx+rw, ry+rh), color, 2)
                    face_found = True

                # ส่ง Scan
                if face_found and self.server_online and not self.net_worker.is_busy:
                    if (time.time() - self.last_scan_time) > 2.5:
                        self.lbl_action.setText("⏳ กำลังตรวจสอบ...")
                        self.net_worker.request_scan(frame)
                        self.last_scan_time = time.time()
                elif not self.server_online:
                    self.lbl_action.setText("❌ Server ไม่เชื่อมต่อ")
                elif not face_found:
                    self.lbl_action.setText("กรุณามองกล้อง...")
                    if (time.time() - self.last_scan_time) > 5.0:
                        self.last_greeted_name = None
            
            # Show Video
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame.shape
            qimg = QImage(frame.data, w, h, ch*w, QImage.Format.Format_RGB888)
            self.video.setPixmap(QPixmap.fromImage(qimg).scaled(640, 480, Qt.AspectRatioMode.KeepAspectRatio))

    def on_scan_result(self, data):
        if data['status'] == 'OK':
            name = data['name']
            
            # ทักทาย
            if name != self.last_greeted_name:
                threading.Thread(target=play_greeting, args=(name,), daemon=True).start()
                self.last_greeted_name = name
            else:
                winsound.Beep(2000, 100) 

            # Update UI
            self.lbl_action.setText(f"✅ ยินดีต้อนรับ: {name}")
            self.lbl_action.setStyleSheet("font-size: 24px; font-weight: bold; color: green; margin-top: 10px;")
            
            # Update Table
            now = datetime.now()
            thai_datetime = f"{now.day:02}/{now.month:02}/{now.year+543} {now.strftime('%H:%M:%S')}"
            self.table.insertRow(0)
            self.table.setItem(0, 0, QTableWidgetItem(name))
            self.table.setItem(0, 1, QTableWidgetItem(thai_datetime))
            
        else:
            self.lbl_action.setText("❌ ไม่พบข้อมูล / กรุณาลองใหม่")
            self.lbl_action.setStyleSheet("font-size: 24px; font-weight: bold; color: red; margin-top: 10px;")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ClientWindow()
    win.show()
    sys.exit(app.exec())