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
import shutil
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QMessageBox, QDialog, 
                             QFormLayout, QLineEdit, QFileDialog)
from PyQt6.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QFont
from deepface import DeepFace

# --- 1. หน้าต่างตั้งค่า (Admin Window) ---
class AdminWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ ระบบจัดการหลังบ้าน (Admin)")
        self.setFixedSize(500, 750)
        self.parent = parent
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()

        # ส่วนตั้งค่า Database
        self.db_host = QLineEdit(self.parent.config.get("db_host", "localhost"))
        self.db_user = QLineEdit(self.parent.config.get("db_user", "root"))
        self.db_pass = QLineEdit(self.parent.config.get("db_password", ""))
        self.db_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.db_name = QLineEdit(self.parent.config.get("db_name", "attendance_system"))

        btn_test = QPushButton("⚡ ทดสอบการเชื่อมต่อ (Test Connection)")
        btn_test.clicked.connect(self.test_connection)
        btn_test.setStyleSheet("background-color: #f39c12; color: white; font-weight: bold; height: 35px;")

        form.addRow("Database Host:", self.db_host)
        form.addRow("User:", self.db_user)
        form.addRow("Password:", self.db_pass)
        form.addRow("DB Name:", self.db_name)
        layout.addLayout(form)
        layout.addWidget(btn_test)

        layout.addWidget(QLabel("<hr>"))

        # ส่วนจัดการพนักงาน
        layout.addWidget(QLabel("👥 ลงทะเบียนพนักงานใหม่"))
        self.emp_name = QLineEdit()
        self.emp_name.setPlaceholderText("ระบุชื่อ-นามสกุล")
        
        btn_reg = QPushButton("📸 ลงทะเบียนด้วยภาพถ่าย (Register Face)")
        btn_reg.clicked.connect(self.register_face)
        btn_reg.setStyleSheet("background-color: #27ae60; color: white; height: 35px;")

        layout.addWidget(self.emp_name)
        layout.addWidget(btn_reg)

        self.emp_table = QTableWidget(0, 3)
        self.emp_table.setHorizontalHeaderLabels(["ID", "ชื่อ", "จัดการ"])
        self.emp_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.emp_table)

        btn_save = QPushButton("💾 บันทึกการตั้งค่า (Save Settings)")
        btn_save.clicked.connect(self.save_settings)
        btn_save.setStyleSheet("background-color: #2980b9; color: white; height: 45px; font-weight: bold;")
        layout.addWidget(btn_save)

        self.load_employees()

    def test_connection(self):
        """ทดสอบการเชื่อมต่อ Database"""
        try:
            conn = mysql.connector.connect(
                host=self.db_host.text(),
                user=self.db_user.text(),
                password=self.db_pass.text(),
                database=self.db_name.text(),
                connect_timeout=3
            )
            if conn.is_connected():
                QMessageBox.information(self, "สำเร็จ", "เชื่อมต่อฐานข้อมูลได้ปกติ! ✅")
                conn.close()
        except Exception as e:
            QMessageBox.critical(self, "ล้มเหลว", f"ไม่สามารถเชื่อมต่อได้: {str(e)} ❌")

    def register_face(self):
        """ลงทะเบียนใบหน้าใหม่"""
        name = self.emp_name.text()
        if not name: return QMessageBox.warning(self, "เตือน", "กรุณากรอกชื่อ")

        file_path, _ = QFileDialog.getOpenFileName(self, "เลือกรูปพนักงาน", "", "Images (*.jpg *.png)")
        if file_path:
            save_dir = "images"
            if not os.path.exists(save_dir): os.makedirs(save_dir)
            save_path = os.path.join(save_dir, f"{name}_{int(time.time())}.jpg")
            shutil.copy(file_path, save_path)

            try:
                conn = self.parent.ai_worker.get_db_conn()
                if not conn: raise Exception("ต่อ DB ไม่ได้")
                cur = conn.cursor()
                cur.execute("INSERT INTO employees (name, image_path) VALUES (%s, %s)", (name, save_path))
                conn.commit()
                conn.close()
                QMessageBox.information(self, "สำเร็จ", f"ลงทะเบียนคุณ {name} แล้ว")
                self.load_employees()
                self.parent.ai_worker.load_known_faces()
            except Exception as e: QMessageBox.critical(self, "ผิดพลาด", str(e))

    def load_employees(self):
        """ดึงรายชื่อพนักงานมาแสดง"""
        try:
            conn = self.parent.ai_worker.get_db_conn()
            if not conn: return
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT id, name FROM employees ORDER BY id DESC")
            rows = cur.fetchall()
            self.emp_table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                self.emp_table.setItem(i, 0, QTableWidgetItem(str(row['id'])))
                self.emp_table.setItem(i, 1, QTableWidgetItem(row['name']))
                btn_del = QPushButton("ลบ")
                btn_del.clicked.connect(lambda ch, r=row['id']: self.delete_emp(r))
                self.emp_table.setCellWidget(i, 2, btn_del)
            conn.close()
        except: pass

    def delete_emp(self, emp_id):
        if QMessageBox.question(self, "ยืนยัน", "ลบพนักงานคนนี้?") == QMessageBox.StandardButton.Yes:
            try:
                conn = self.parent.ai_worker.get_db_conn()
                cur = conn.cursor()
                cur.execute("DELETE FROM employees WHERE id = %s", (emp_id,))
                conn.commit()
                conn.close()
                self.load_employees()
                self.parent.ai_worker.load_known_faces()
            except: pass

    def save_settings(self):
        """บันทึก Config"""
        self.parent.config.update({
            "db_host": self.db_host.text(),
            "db_user": self.db_user.text(),
            "db_password": self.db_pass.text(),
            "db_name": self.db_name.text()
        })
        with open("config.json", "w") as f: json.dump(self.parent.config, f, indent=4)
        QMessageBox.information(self, "สำเร็จ", "บันทึกการตั้งค่าแล้ว")

# --- 2. AI Thread ---
class FaceAIThread(QThread):
    result_ready = pyqtSignal(list)
    db_error = pyqtSignal(str)

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
        try:
            return mysql.connector.connect(
                host=self.config.get("db_host", "localhost"),
                user=self.config.get("db_user", "root"),
                password=self.config.get("db_password", ""),
                database=self.config.get("db_name", "attendance_system"),
                connect_timeout=3
            )
        except: return None

    def load_known_faces(self):
        conn = self.get_db_conn()
        if not conn:
            self.db_error.emit("❌ เชื่อมต่อ DB ไม่ได้ (AI โหลดข้อมูลไม่ได้)")
            return
        try:
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
        except: pass

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
                res = []
                for (x, y, w, h) in faces:
                    try:
                        face_img = frame[y:y+h, x:x+w]
                        rep = DeepFace.represent(img_path=cv2.cvtColor(face_img, cv2.COLOR_BGR2RGB), model_name="Facenet512", enforce_detection=False)
                        if rep:
                            name = self.find_match(rep[0]["embedding"])
                            if name:
                                self.record(name, frame)
                                res.append((x, y, w, h, name, "OK"))
                            else: res.append((x, y, w, h, "Unknown", "FAIL"))
                    except: pass
                self.result_ready.emit(res)
                self.current_frame = None
            self.msleep(100)

    def record(self, name, frame):
        now = datetime.now()
        if name in self.last_recorded and (now - self.last_recorded[name]).total_seconds() < self.config.get("cooldown_seconds", 3600):
            return
        try:
            ts = now.strftime('%Y%m%d_%H%M%S')
            path = f"attendance_images/{name}_{ts}.jpg"
            cv2.imwrite(path, frame)
            conn = self.get_db_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO attendance_logs (employee_name, check_time, evidence_image) VALUES (%s, %s, %s)", (name, now, path))
            conn.commit()
            conn.close()
            self.last_recorded[name] = now
        except: pass

# --- 3. Main Window ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Face Attendance System (PyQt6)")
        self.setFixedSize(1280, 720)
        self.load_config()

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)

        left = QVBoxLayout()
        self.video_label = QLabel()
        self.video_label.setFixedSize(800, 600)
        self.video_label.setStyleSheet("background: black; border-radius: 10px;")
        left.addWidget(self.video_label)
        self.status_bar = QLabel("System Status: Starting...")
        left.addWidget(self.status_bar)
        layout.addLayout(left)

        right = QVBoxLayout()
        right.addWidget(QLabel("🕒 ประวัติล่าสุด"))
        self.table = QTableWidget(12, 2)
        self.table.setHorizontalHeaderLabels(["ชื่อ", "เวลา"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right.addWidget(self.table)
        
        self.btn_admin = QPushButton("⚙️ ตั้งค่าระบบ (Admin)")
        self.btn_admin.clicked.connect(self.open_admin)
        self.btn_admin.setStyleSheet("height: 45px; font-weight: bold; background: #ecf0f1;")
        right.addWidget(self.btn_admin)
        layout.addLayout(right)

        self.cap = cv2.VideoCapture(self.config.get("camera_index", 0), cv2.CAP_DSHOW)
        self.ai_worker = FaceAIThread(self.config)
        self.ai_worker.result_ready.connect(self.on_ai_result)
        self.ai_worker.db_error.connect(lambda m: self.status_bar.setText(m))
        self.ai_worker.start()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_ui)
        self.timer.start(30)
        self.last_results = []

    def load_config(self):
        try:
            with open("config.json", "r") as f: self.config = json.load(f)
        except: self.config = {"camera_index":0, "threshold":0.3}

    def open_admin(self):
        AdminWindow(self).exec()

    def update_ui(self):
        ret, frame = self.cap.read()
        if ret:
            if self.ai_worker.current_frame is None: self.ai_worker.current_frame = frame.copy()
            for (x, y, w, h, n, s) in self.last_results:
                c = (0,255,0) if s=="OK" else (0,0,255)
                cv2.rectangle(frame, (x,y), (x+w,y+h), c, 2)
                cv2.putText(frame, n, (x, y-10), 0, 0.7, c, 2)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            img = QImage(rgb.data, w, h, ch*w, QImage.Format.Format_RGB888)
            self.video_label.setPixmap(QPixmap.fromImage(img).scaled(800,600, Qt.AspectRatioMode.KeepAspectRatio))
        
        cpu = psutil.cpu_percent()
        self.status_bar.setText(f"🖥️ CPU: {cpu}% | RAM: {psutil.virtual_memory().percent}%")

    def on_ai_result(self, res):
        self.last_results = res
        try:
            conn = self.ai_worker.get_db_conn()
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT employee_name, check_time FROM attendance_logs ORDER BY check_time DESC LIMIT 12")
            for i, row in enumerate(cur.fetchall()):
                self.table.setItem(i,0, QTableWidgetItem(row['employee_name']))
                self.table.setItem(i,1, QTableWidgetItem(row['check_time'].strftime('%H:%M:%S')))
            conn.close()
        except: pass

if __name__ == "__main__":
    if not os.path.exists("attendance_images"): os.makedirs("attendance_images")
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())