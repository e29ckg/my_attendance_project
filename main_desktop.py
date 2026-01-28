import sys
import cv2
import threading
import numpy as np
import os
import json
import requests
import time
import psutil
import mysql.connector
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QMessageBox)
from PyQt6.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QFont
from deepface import DeepFace

# --- 1. Worker Thread สำหรับ AI ประมวลผลหนักๆ ---
class FaceAIThread(QThread):
    result_ready = pyqtSignal(list)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.current_frame = None
        self.is_running = True
        self.known_embeddings = []
        self.known_names = []
        self.last_recorded = {}
        self.load_known_faces()

    def get_db_conn(self):
        return mysql.connector.connect(
            host=self.config.get("db_host", "localhost"),
            user=self.config.get("db_user", "root"),
            password=self.config.get("db_password", ""),
            database=self.config.get("db_name", "attendance_system")
        )

    def load_known_faces(self):
        try:
            conn = self.get_db_conn()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT name, image_path FROM employees")
            rows = cursor.fetchall()
            self.known_embeddings, self.known_names = [], []
            for row in rows:
                if os.path.exists(row['image_path']):
                    embed = DeepFace.represent(img_path=row['image_path'], model_name="Facenet512", enforce_detection=False)[0]["embedding"]
                    self.known_embeddings.append(embed)
                    self.known_names.append(row['name'])
            conn.close()
        except Exception as e: print(f"DB Error: {e}")

    def find_match(self, target_embed):
        if not self.known_embeddings: return None
        threshold = self.config.get("threshold", 0.30)
        distances = [1 - (np.dot(target_embed, e) / (np.linalg.norm(target_embed) * np.linalg.norm(e))) for e in self.known_embeddings]
        min_dist = min(distances)
        return self.known_names[distances.index(min_dist)] if min_dist < threshold else None

    def run(self):
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        while self.is_running:
            if self.current_frame is not None:
                frame = self.current_frame.copy()
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(gray, 1.3, 5)
                
                results_data = []
                for (x, y, w, h) in faces:
                    face_img = frame[y:y+h, x:x+w]
                    try:
                        face_rgb = cv2.cvtColor(face_img, cv2.COLOR_BGR2RGB)
                        rep = DeepFace.represent(img_path=face_rgb, model_name="Facenet512", enforce_detection=False)
                        if rep:
                            name = self.find_match(rep[0]["embedding"])
                            if name:
                                self.process_attendance(name, frame)
                                results_data.append((x, y, w, h, name, "OK"))
                            else:
                                results_data.append((x, y, w, h, "Unknown", "FAIL"))
                    except: pass
                
                self.result_ready.emit(results_data)
                self.current_frame = None
            self.msleep(100)

    def process_attendance(self, name, frame):
        now = datetime.now()
        # Cooldown check
        if name in self.last_recorded:
            if (now - self.last_recorded[name]).total_seconds() < self.config.get("cooldown_seconds", 3600):
                return

        # Save to DB & Notify
        try:
            ts = now.strftime('%Y%m%d_%H%M%S')
            ev_path = f"attendance_images/{name}_{ts}.jpg"
            cv2.imwrite(ev_path, frame)
            
            conn = self.get_db_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO attendance_logs (employee_name, check_time, evidence_image) VALUES (%s, %s, %s)", (name, now, ev_path))
            conn.commit()
            conn.close()
            self.last_recorded[name] = now
            
            if self.config.get("enable_telegram"):
                self.send_tg(name, now, ev_path)
        except: pass

    def send_tg(self, name, check_time, img_path):
        token = self.config.get("telegram_bot_token")
        chat_id = self.config.get("telegram_chat_id")
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        try:
            with open(img_path, 'rb') as f:
                requests.post(url, data={'chat_id': chat_id, 'caption': f"✅ {name} เข้างานแล้ว\n⏰ {check_time.strftime('%H:%M:%S')}"}, files={'photo': f})
        except: pass

# --- 2. หน้าจอหลัก UI ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Face Attendance Professional (Desktop)")
        self.setFixedSize(1280, 720)
        self.load_config()

        # UI Layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)

        # Left: Video & Stats
        left_box = QVBoxLayout()
        self.video_label = QLabel()
        self.video_label.setFixedSize(800, 600)
        self.video_label.setStyleSheet("background: black; border-radius: 10px;")
        left_box.addWidget(self.video_label)

        self.status_bar = QLabel("System: Loading...")
        self.status_bar.setFont(QFont("Sarabun", 12))
        left_box.addWidget(self.status_bar)
        layout.addLayout(left_box)

        # Right: Logs Table
        right_box = QVBoxLayout()
        right_box.addWidget(QLabel("🕒 ประวัติการเข้างานล่าสุด"))
        self.table = QTableWidget(15, 2)
        self.table.setHorizontalHeaderLabels(["ชื่อ", "เวลา"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right_box.addWidget(self.table)
        
        btn_reload = QPushButton("🔄 รีเฟรชฐานข้อมูล AI")
        btn_reload.clicked.connect(lambda: self.ai_worker.load_known_faces())
        right_box.addWidget(btn_reload)
        layout.addLayout(right_box)

        # Camera Setup
        self.cap = cv2.VideoCapture(self.config.get("camera_index", 0), cv2.CAP_DSHOW)
        
        # AI Worker
        self.ai_worker = FaceAIThread(self.config)
        self.ai_worker.result_ready.connect(self.on_ai_result)
        self.ai_worker.start()

        # Timers
        self.cam_timer = QTimer()
        self.cam_timer.timeout.connect(self.update_video)
        self.cam_timer.start(30)

        self.sys_timer = QTimer()
        self.sys_timer.timeout.connect(self.update_sys_status)
        self.sys_timer.start(2000)

        self.last_ai_results = []

    def load_config(self):
        try:
            with open("config.json", "r") as f: self.config = json.load(f)
        except: self.config = {"camera_index":0, "threshold":0.3}

    def update_video(self):
        ret, frame = self.cap.read()
        if ret:
            # ส่งเฟรมให้ AI ประมวลผล (ถ้าว่าง)
            if self.ai_worker.current_frame is None:
                self.ai_worker.current_frame = frame.copy()

            # วาดกรอบบนหน้าจอ UI (ลื่นไหลเพราะวาดคนละส่วนกับ AI)
            for (x, y, w, h, name, status) in self.last_ai_results:
                color = (0, 255, 0) if status == "OK" else (0, 0, 255)
                cv2.rectangle(frame, (x, y), (x+w, y+h), color, 2)
                cv2.putText(frame, name, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

            rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb_image.shape
            qt_img = QImage(rgb_image.data, w, h, ch * w, QImage.Format.Format_RGB888)
            self.video_label.setPixmap(QPixmap.fromImage(qt_img).scaled(800, 600, Qt.AspectRatioMode.KeepAspectRatio))

    def on_ai_result(self, results):
        self.last_ai_results = results
        self.update_table_data()

    def update_table_data(self):
        try:
            conn = self.ai_worker.get_db_conn()
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT employee_name, check_time FROM attendance_logs ORDER BY check_time DESC LIMIT 10")
            for i, row in enumerate(cur.fetchall()):
                self.table.setItem(i, 0, QTableWidgetItem(row['employee_name']))
                self.table.setItem(i, 1, QTableWidgetItem(row['check_time'].strftime('%H:%M:%S')))
            conn.close()
        except: pass

    def update_sys_status(self):
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        self.status_bar.setText(f"🖥️ CPU: {cpu}% | RAM: {ram}% | AI Model: Facenet512 Active")

    def closeEvent(self, event):
        self.ai_worker.is_running = False
        self.cap.release()
        event.accept()

if __name__ == "__main__":
    # ตรวจสอบโฟลเดอร์รูปภาพ
    if not os.path.exists("attendance_images"): os.makedirs("attendance_images")
    
    app = QApplication(sys.argv)
    app.setStyle("Fusion") # ให้หน้าตาดูโมเดิร์นทุก OS
    window = MainWindow()
    window.show()
    sys.exit(app.exec())