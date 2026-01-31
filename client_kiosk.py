import sys, cv2, time, requests, winsound, os, threading
import sys, cv2, time, requests, winsound
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from datetime import datetime
from gtts import gTTS       # <--- เพิ่ม (สร้างเสียง)
import pygame.mixer  # <--- เพิ่ม (เล่นเสียง)   

# เริ่มระบบเสียง
pygame.mixer.init()

# --- CONFIG ---
# ⚠️ อย่าลืมแก้ IP ให้ตรงกับเครื่อง Server
SERVER_URL = "http://localhost:9876" 
CHECK_INTERVAL = 5 # เช็ค Server ทุกๆ 5 วินาที

def play_greeting(name):
    try:
        if not os.path.exists("sounds"): os.makedirs("sounds")
        filename = f"sounds/{name}.mp3"
        if not os.path.exists(filename):
            print(f"🔊 สร้างเสียง: {name}")
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
    status_signal = pyqtSignal(bool, str) # ส่งค่า (Online/Offline, ข้อความ Latency)

    def run(self):
        while True:
            try:
                start_time = time.time()
                # ยิงไปที่ /health
                response = requests.get(f"{SERVER_URL}/health", timeout=2)
                
                if response.status_code == 200:
                    latency = int((time.time() - start_time) * 1000)
                    self.status_signal.emit(True, f"{latency} ms")
                else:
                    self.status_signal.emit(False, "Error")
            except:
                self.status_signal.emit(False, "Timeout")
            
            self.sleep(CHECK_INTERVAL)

# --- WORKER: ส่งภาพสแกน (เหมือนเดิม) ---
class NetworkThread(QThread):
    result_ready = pyqtSignal(dict)
    
    def __init__(self):
        super().__init__()
        self.frame_to_send = None
        self.is_busy = False

    def request_scan(self, frame):
        if not self.is_busy:
            # แก้ไขจุดที่ 1: ย่อรูปก่อนส่ง (Resize) ช่วยให้ Server ตอบกลับไวขึ้นมาก
            # ย่อเหลือกว้าง 640px (รักษาอัตราส่วน)
            h, w = frame.shape[:2]
            scale = 640 / w
            resized_frame = cv2.resize(frame, (0,0), fx=scale, fy=scale)
            
            self.frame_to_send = resized_frame 
            self.start()

    def run(self):
        if self.frame_to_send is not None:
            self.is_busy = True
            try:
                _, img_encoded = cv2.imencode('.jpg', self.frame_to_send)
                files = {'file': ('image.jpg', img_encoded.tobytes(), 'image/jpeg')}
                
                # แก้ไขจุดที่ 2: เพิ่ม timeout เป็น 15 วินาที
                response = requests.post(f"{SERVER_URL}/scan", files=files, timeout=15)
                
                if response.status_code == 200:
                    self.result_ready.emit(response.json())
            except Exception as e:
                print(f"Scan Error: {e}")
            finally:
                self.is_busy = False

# --- UI MAIN WINDOW ---
class ClientWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kiosk Client + Health Check")
        self.setFixedSize(1000, 700)

        self.last_greeted_name = None
        
        # Widget หลัก
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central) # เปลี่ยนเป็นแนวตั้งหลักก่อน

        # --- ส่วน HEADER: แสดงสถานะ Server ---
        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel("📷 ระบบลงเวลาทำงาน"))
        
        self.lbl_server_status = QLabel("⚪ กำลังเชื่อมต่อ Server...")
        self.lbl_server_status.setStyleSheet("font-size: 16px; font-weight: bold; color: gray; border: 1px solid #ccc; padding: 5px; border-radius: 5px;")
        self.lbl_server_status.setAlignment(Qt.AlignmentFlag.AlignRight)
        header_layout.addWidget(self.lbl_server_status)
        
        main_layout.addLayout(header_layout)

        # --- ส่วนเนื้อหา (กล้อง + ตาราง) ---
        content_layout = QHBoxLayout()
        
        # ฝั่งซ้าย: กล้อง
        left_layout = QVBoxLayout()
        self.video = QLabel()
        self.video.setFixedSize(640, 480)
        self.video.setStyleSheet("background: #000; border: 2px solid #555;")
        left_layout.addWidget(self.video, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_action = QLabel("กรุณามองกล้อง...")
        self.lbl_action.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_action.setStyleSheet("font-size: 22px; font-weight: bold; color: #333; margin-top: 10px;")
        left_layout.addWidget(self.lbl_action)
        content_layout.addLayout(left_layout)

        # ฝั่งขวา: นาฬิกา + ตาราง
        right_layout = QVBoxLayout()
        self.lbl_time = QLabel("00:00:00")
        self.lbl_time.setStyleSheet("font-size: 50px; font-weight: bold; color: #0078d7;")
        self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.lbl_time)
        
        self.table = QTableWidget(10, 2)
        self.table.setHorizontalHeaderLabels(["ชื่อ-สกุล", "เวลา"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right_layout.addWidget(self.table)
        content_layout.addLayout(right_layout)

        main_layout.addLayout(content_layout)

        # --- SYSTEM SETUP ---
        self.cap = cv2.VideoCapture(1) # เปลี่ยนเป็น 0 หรือ 1 ตามกล้องที่ใช้
        self.face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        
        # Thread: ส่งรูปสแกน
        self.net_worker = NetworkThread()
        self.net_worker.result_ready.connect(self.on_scan_result)
        
        # Thread: เช็คสถานะ Server (Heartbeat)
        self.status_worker = ServerStatusThread()
        self.status_worker.status_signal.connect(self.update_server_status)
        self.status_worker.start()

        # Timer: อัปเดตกล้อง
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_camera)
        self.timer.start(30)
        
        self.last_scan_time = 0
        self.server_online = False # ตัวแปรเก็บสถานะจริง

    def update_server_status(self, is_online, msg):
        self.server_online = is_online
        if is_online:
            self.lbl_server_status.setText(f"🟢 Server Online (Ping: {msg})")
            self.lbl_server_status.setStyleSheet("font-size: 14px; font-weight: bold; color: green; border: 1px solid green; padding: 5px; border-radius: 5px; background: #e6fffa;")
        else:
            self.lbl_server_status.setText(f"🔴 Server Offline ({msg})")
            self.lbl_server_status.setStyleSheet("font-size: 14px; font-weight: bold; color: white; border: 1px solid red; padding: 5px; border-radius: 5px; background: #ff4d4d;")

    def update_camera(self):
        self.lbl_time.setText(datetime.now().strftime("%H:%M:%S"))
        ret, frame = self.cap.read()
        if ret:
            # Face Detection (Client Side)
            small = cv2.resize(frame, (0,0), fx=0.5, fy=0.5)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = self.face_cascade.detectMultiScale(gray, 1.2, 5)
            
            face_found = False
            for (x, y, w, h) in faces:
                rx, ry, rw, rh = x*2, y*2, w*2, h*2
                # วาดกรอบ: สีเขียวถ้า Server พร้อม / สีแดงถ้า Server ดับ
                color = (0, 255, 0) if self.server_online else (0, 0, 255)
                cv2.rectangle(frame, (rx, ry), (rx+rw, ry+rh), color, 2)
                face_found = True

            # Logic ส่งสแกน (ต้องเจอหน้า + Server Online + ไม่ Busy + ไม่รัว)
            if face_found and self.server_online and not self.net_worker.is_busy:
                if (time.time() - self.last_scan_time) > 2.0:
                    self.lbl_action.setText("⏳ กำลังตรวจสอบ...")
                    self.net_worker.request_scan(frame)
                    self.last_scan_time = time.time()
            elif not self.server_online:
                self.lbl_action.setText("❌ เชื่อมต่อ Server ไม่ได้")
            elif not face_found:
                self.lbl_action.setText("กรุณามองกล้อง...")

            # Show Video
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame.shape
            qimg = QImage(frame.data, w, h, ch*w, QImage.Format.Format_RGB888)
            self.video.setPixmap(QPixmap.fromImage(qimg).scaled(640, 480))   

    def on_scan_result(self, data):
        if data['status'] == 'OK':
            name = data['name']
            
            # --- ส่วนจัดการเสียง (Logic เดิม) ---
            if name != self.last_greeted_name:
                threading.Thread(target=play_greeting, args=(name,), daemon=True).start()
                self.last_greeted_name = name
            else:
                winsound.Beep(1500, 100) 

            # --- ส่วนแสดงผล ---
            self.lbl_action.setText(f"✅ ยินดีต้อนรับ: {name}")
            self.lbl_action.setStyleSheet("font-size: 22px; font-weight: bold; color: green; margin-top: 10px;")
            
            # [แก้ไขตรงนี้] สร้าง string วันที่และเวลาปัจจุบันแบบไทย
            now = datetime.now()
            thai_datetime = f"{now.day:02}/{now.month:02}/{now.year+543} {now.strftime('%H:%M:%S')}"

            # ลงตาราง
            self.table.insertRow(0)
            self.table.setItem(0, 0, QTableWidgetItem(name))
            self.table.setItem(0, 1, QTableWidgetItem(thai_datetime)) # ใส่ค่าที่จัด Format แล้ว
            
        else:
            winsound.Beep(500, 500)
            self.lbl_action.setText("❌ ไม่พบข้อมูล / ลองใหม่อีกครั้ง")
            self.lbl_action.setStyleSheet("font-size: 22px; font-weight: bold; color: red; margin-top: 10px;")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ClientWindow()
    win.show()
    sys.exit(app.exec())