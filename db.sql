CREATE DATABASE attendance_system;
USE attendance_system;

-- ตารางพนักงาน
CREATE TABLE employees (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    image_path VARCHAR(255) NOT NULL
);

-- ตารางบันทึกเวลา
CREATE TABLE attendance_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    employee_name VARCHAR(100),
    check_time DATETIME
);