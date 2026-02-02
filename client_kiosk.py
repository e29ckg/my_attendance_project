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
CAMERA_INDEX = 0
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
            print(f"🔊 สร้างเสียงใหม่สำหรับ: {name}")
            tts = gTTS(text=f"สวัสดีค่ะ คุณ{name}", lang='th')
            tts.save(filename)
            
        # รอให้ channel ว่างก่อนเล่น (ป้องกันเสียงตีกัน)
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)
            
        pygame.mixer.music.load(filename)
        pygame.mixer.music.play()
        
    except Exception as e:
        print(f"TTS Error: {e}")
        # ถ้ามีปัญหาเรื่องเสียง ให้ Beep แทน
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

# --- WORKER: ส่งภาพสแกน ---
class NetworkThread(QThread):
    result_ready = pyqtSignal(dict)
    
    def __init__(self):
        super().__init__()
        self.frame_to_send = None
        self.is_busy = False

    def request_scan(self, frame):
        if not self.is_busy:
            # 1. ย่อภาพก่อนส่ง (Resize) เพื่อลดขนาดไฟล์และแก้ปัญหา Timeout
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
                
                # 2. เพิ่ม Timeout เป็น 15 วินาที (เผื่อ Server ประมวลผลนาน)
                response = requests.post(f"{SERVER_URL}/scan", files=files, timeout=15)
                
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
        self.setFixedSize(1000, 700)
        
        # ตัวแปรจำชื่อคนล่าสุด (เพื่อไม่ให้พูดซ้ำ)
        self.last_greeted_name = None 
        
        # GUI Setup
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Header Status
        header_layout = QHBoxLayout()
        title = QLabel("📷 ระบบลงเวลาทำงาน (Face Recognition)")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        header_layout.addWidget(title)
        
        self.lbl_server_status = QLabel("⚪ Connecting...")
        self.lbl_server_status.setStyleSheet("font-size: 14px; padding: 5px; border: 1px solid #ccc; border-radius: 5px;")
        self.lbl_server_status.setAlignment(Qt.AlignmentFlag.AlignRight)
        header_layout.addWidget(self.lbl_server_status)
        main_layout.addLayout(header_layout)

        # Content Layout
        content_layout = QHBoxLayout()
        
        # Left: Camera
        left_layout = QVBoxLayout()
        self.video = QLabel()
        self.video.setFixedSize(640, 480)
        self.video.setStyleSheet("background: #000; border: 2px solid #555;")
        left_layout.addWidget(self.video, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_action = QLabel("กรุณามองกล้อง...")
        self.lbl_action.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_action.setStyleSheet("font-size: 24px; font-weight: bold; color: #333; margin-top: 15px;")
        left_layout.addWidget(self.lbl_action)
        content_layout.addLayout(left_layout)

        # Right: Clock & Table
        right_layout = QVBoxLayout()
        self.lbl_time = QLabel("00:00:00")
        self.lbl_time.setStyleSheet("font-size: 50px; font-weight: bold; color: #0078d7;")
        self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.lbl_time)
        
        self.table = QTableWidget(10, 2)
        self.table.setHorizontalHeaderLabels(["ชื่อ-สกุล", "เวลาที่บันทึก"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # set font for table
        font = QFont("Tahoma", 12)
        self.table.setFont(font)
        right_layout.addWidget(self.table)
        content_layout.addLayout(right_layout)

        main_layout.addLayout(content_layout)

        # --- SYSTEM INIT ---
        self.cap = cv2.VideoCapture(CAMERA_INDEX)
        # ใช้ Haar Cascade ฝั่ง Client เพื่อประหยัดแรง
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
        self.timer.start(30) # 30ms (~33 FPS)
        
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

    def update_camera(self):
        # Update Clock
        self.lbl_time.setText(datetime.now().strftime("%H:%M:%S"))
        
        ret, frame = self.cap.read()
        if ret:
            # Face Detection (Client Side)
            # ย่อภาพเฉพาะตอน detect หน้า (เพื่อความเร็ว)
            small = cv2.resize(frame, (0,0), fx=0.5, fy=0.5)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = self.face_cascade.detectMultiScale(gray, 1.2, 5)
            
            face_found = False
            for (x, y, w, h) in faces:
                rx, ry, rw, rh = x*2, y*2, w*2, h*2
                
                # กรอบสีเขียวถ้า Server พร้อม / สีแดงถ้า Server ดับ
                color = (0, 255, 0) if self.server_online else (0, 0, 255)
                cv2.rectangle(frame, (rx, ry), (rx+rw, ry+rh), color, 2)
                face_found = True

            # Logic การส่งสแกน
            if face_found and self.server_online and not self.net_worker.is_busy:
                # ส่งทุก 2.5 วินาที
                if (time.time() - self.last_scan_time) > 2.5:
                    self.lbl_action.setText("⏳ กำลังตรวจสอบ...")
                    self.net_worker.request_scan(frame)
                    self.last_scan_time = time.time()
            elif not self.server_online:
                self.lbl_action.setText("❌ Server ไม่เชื่อมต่อ")
            elif not face_found:
                self.lbl_action.setText("กรุณามองกล้อง...")
                # ถ้านานเกิน 5 วิ ไม่เจอหน้า ให้รีเซ็ตคนล่าสุด เพื่อให้ทักใหม่ได้เมื่อกลับมา
                if (time.time() - self.last_scan_time) > 5.0:
                    self.last_greeted_name = None

            # แสดงผล video
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame.shape
            qimg = QImage(frame.data, w, h, ch*w, QImage.Format.Format_RGB888)
            self.video.setPixmap(QPixmap.fromImage(qimg).scaled(640, 480, Qt.AspectRatioMode.KeepAspectRatio))

    def on_scan_result(self, data):
        if data['status'] == 'OK':
            name = data['name']
            
            # --- Logic การทักทาย ---
            if name != self.last_greeted_name:
                # คนใหม่ -> พูดชื่อ
                threading.Thread(target=play_greeting, args=(name,), daemon=True).start()
                self.last_greeted_name = name
            else:
                # คนเดิม -> แค่ Beep เบาๆ
                winsound.Beep(2000, 100) 

            # --- Update UI ---
            self.lbl_action.setText(f"✅ ยินดีต้อนรับ: {name}")
            self.lbl_action.setStyleSheet("font-size: 24px; font-weight: bold; color: green; margin-top: 15px;")
            
            # Formatted Date/Time (Thai)
            now = datetime.now()
            thai_datetime = f"{now.day:02}/{now.month:02}/{now.year+543} {now.strftime('%H:%M:%S')}"

            # Insert Table
            self.table.insertRow(0)
            self.table.setItem(0, 0, QTableWidgetItem(name))
            self.table.setItem(0, 1, QTableWidgetItem(thai_datetime))
            
        else:
            # กรณีสแกนไม่ผ่าน
            winsound.Beep(500, 300)
            self.lbl_action.setText("❌ ไม่พบข้อมูล / กรุณาลองใหม่")
            self.lbl_action.setStyleSheet("font-size: 24px; font-weight: bold; color: red; margin-top: 15px;")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ClientWindow()
    win.show()
    sys.exit(app.exec())