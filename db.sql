CREATE DATABASE IF NOT EXISTS attendance_system;
USE attendance_system;

-- ตารางพนักงาน
CREATE TABLE IF NOT EXISTS employees (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    image_path VARCHAR(255) NOT NULL
);

-- ตารางบันทึกเวลา (เพิ่ม evidence_image)
CREATE TABLE IF NOT EXISTS attendance_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    employee_name VARCHAR(100),
    check_time DATETIME,
    evidence_image VARCHAR(255)
);