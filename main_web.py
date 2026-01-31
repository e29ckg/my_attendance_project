import os, json, shutil, sqlite3
import pandas as pd
from datetime import datetime, timedelta, time
from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn

DB_FILE = "attendance.db"
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# สร้างโฟลเดอร์
os.makedirs("attendance_images", exist_ok=True)
os.makedirs("images", exist_ok=True)
app.mount("/attendance_images", StaticFiles(directory="attendance_images"), name="attendance_images")
app.mount("/images", StaticFiles(directory="images"), name="images")

def get_db_conn():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn
    except: return None

# --- CORE LOGIC: คำนวณเวลา เข้า/ออก ตามเงื่อนไข ---
# --- [เพิ่ม] API ดึงตำแหน่งทั้งหมด (สำหรับ Dropdown) ---
@app.get("/api/roles")
async def get_roles():
    conn = get_db_conn()
    if not conn: return []
    
    # แก้ SQL: เพิ่ม TRIM(role) != '' เพื่อกันค่าว่างจริงๆ
    query = "SELECT DISTINCT role FROM employees WHERE role IS NOT NULL AND TRIM(role) != ''"
    roles_df = pd.read_sql(query, conn)
    conn.close()
    
    # แปลงเป็น list และกรองค่าว่างอีกรอบเพื่อความชัวร์
    role_list = [r for r in roles_df['role'].tolist() if r and r.strip()]
    
    return role_list

# --- [แก้ไข] ฟังก์ชันคำนวณรายงาน (เพิ่มตัวกรอง role) ---
def process_daily_attendance(target_date_str, target_role=None):
    conn = get_db_conn()
    if not conn: return []
    
    # 1. ดึงรายชื่อพนักงาน (เพิ่มเงื่อนไขกรองตามตำแหน่ง)
    sql_emp = "SELECT employee_id, name, role FROM employees"
    employees = pd.read_sql(sql_emp, conn)
    
    # ถ้ามีการเลือกตำแหน่ง และไม่ใช่ "ทั้งหมด" ให้กรอง DataFrame
    if target_role and target_role != "ทั้งหมด":
        employees = employees[employees['role'] == target_role]

    # 2. ดึง Logs ของวันที่เลือก
    sql_logs = f"SELECT employee_id, check_time FROM attendance_logs WHERE date(check_time) = '{target_date_str}'"
    logs = pd.read_sql(sql_logs, conn)
    conn.close()

    if employees.empty: return [] # ถ้าไม่มีพนักงานในตำแหน่งที่เลือกเลย

    if not logs.empty:
        logs['check_time'] = pd.to_datetime(logs['check_time'])

    report_data = []

    for _, emp in employees.iterrows():
        emp_id = emp['employee_id']
        emp_name = emp['name']
        emp_role = emp['role'] # เก็บตำแหน่งไว้โชว์ด้วย
        
        # กรอง Log เฉพาะคนนี้
        if logs.empty:
            emp_logs = pd.DataFrame()
        else:
            emp_logs = logs[logs['employee_id'] == emp_id]
        
        in_time_str = "-"
        out_time_str = "-"
        status = "ขาดงาน"
        
        if not emp_logs.empty:
            # Logic เข้า: 04:00 - 12:00
            morning_start = pd.Timestamp(f"{target_date_str} 04:00:00")
            morning_end   = pd.Timestamp(f"{target_date_str} 12:00:00")
            morning_logs = emp_logs[(emp_logs['check_time'] >= morning_start) & (emp_logs['check_time'] <= morning_end)]
            
            if not morning_logs.empty:
                in_time_str = morning_logs['check_time'].min().strftime("%H:%M:%S")
                status = "มาทำงาน"

            # Logic ออก: 12:00 - 24:00
            afternoon_start = pd.Timestamp(f"{target_date_str} 12:00:01")
            afternoon_end   = pd.Timestamp(f"{target_date_str} 23:59:59")
            afternoon_logs = emp_logs[(emp_logs['check_time'] >= afternoon_start) & (emp_logs['check_time'] <= afternoon_end)]
            
            if not afternoon_logs.empty:
                out_time_str = afternoon_logs['check_time'].max().strftime("%H:%M:%S")
        
        report_data.append({
            "employee_id": emp_id,
            "name": emp_name,
            "role": emp_role,
            "date": target_date_str,
            "in_time": in_time_str,
            "out_time": out_time_str,
            "status": status
        })
        
    return report_data

# --- [แก้ไข] API Endpoint ให้รับค่า role ---
@app.get("/api/report/daily")
async def get_daily_report(date: str, role: str = "ทั้งหมด"):
    data = process_daily_attendance(date, role)
    return data

# --- PAGE ROUTING ---
@app.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    return templates.TemplateResponse("report.html", {"request": request})

@app.get("/manage_users", response_class=HTMLResponse)
async def manage_users(request: Request):
    return templates.TemplateResponse("user_management.html", {"request": request})

# --- API REPORT ---
@app.get("/api/report/daily")
async def get_daily_report(date: str):
    # date format: YYYY-MM-DD
    data = process_daily_attendance(date)
    return data

@app.get("/api/report/export_monthly")
async def export_monthly_report(month: str, company_name: str = "ชื่อบริษัทของคุณ จำกัด"):
    try:
        year, mth = map(int, month.split('-'))
        if mth == 12: next_month = datetime(year + 1, 1, 1)
        else: next_month = datetime(year, mth + 1, 1)
        
        last_day = (next_month - timedelta(days=1)).day
        all_month_data = []
        
        for d in range(1, last_day + 1):
            current_date = f"{year}-{mth:02d}-{d:02d}"
            daily_data = process_daily_attendance(current_date)
            all_month_data.extend(daily_data)
            
        df = pd.DataFrame(all_month_data)
        df.rename(columns={
            "employee_id": "รหัสพนักงาน", "name": "ชื่อ-สกุล", "role": "ตำแหน่ง",
            "date": "วันที่", "in_time": "เวลาเข้า", "out_time": "เวลาออก", "status": "สถานะ"
        }, inplace=True)
        
        filename = f"Monthly_Report_{month}.xlsx"
        
        # --- เริ่มการเขียน Excel แบบปรับแต่ง (XlsxWriter) ---
        writer = pd.ExcelWriter(filename, engine='xlsxwriter')
        df.to_excel(writer, index=False, startrow=3, sheet_name='Sheet1')
        
        workbook  = writer.book
        worksheet = writer.sheets['Sheet1']

        # 1. สร้าง Format สำหรับส่วนต่างๆ
        header_format = workbook.add_format({
            'bold': True, 'font_size': 18, 'align': 'center', 'valign': 'vcenter'
        })
        subheader_format = workbook.add_format({
            'font_size': 12, 'align': 'center', 'valign': 'vcenter'
        })
        table_header_format = workbook.add_format({
            'bold': True, 'bg_color': '#D7E4BC', 'border': 1, 'align': 'center'
        })
        cell_format = workbook.add_format({'border': 1, 'align': 'center'})

        # 2. เขียนหัวกระดาษ (Merge Cells)
        # บรรทัดที่ 1: ชื่อบริษัท
        worksheet.merge_range('A1:G1', company_name, header_format)
        # บรรทัดที่ 2: ชื่อรายงานและเดือน
        thai_months = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
        report_title = f"รายงานสรุปการลงเวลาทำงาน ประจำเดือน {thai_months[mth]} พ.ศ. {year + 543}"
        worksheet.merge_range('A2:G2', report_title, subheader_format)

        # 3. จัดความกว้างคอลัมน์ให้เหมาะสม
        worksheet.set_column('A:A', 12) # รหัส
        worksheet.set_column('B:B', 25) # ชื่อ
        worksheet.set_column('C:C', 18) # ตำแหน่ง
        worksheet.set_column('D:D', 15) # วันที่
        worksheet.set_column('E:F', 12) # เวลาเข้า-ออก
        worksheet.set_column('G:G', 12) # สถานะ

        # 4. ใส่ Format ให้หัวตาราง (ที่เริ่มจาก row 3)
        for col_num, value in enumerate(df.columns.values):
            worksheet.write(3, col_num, value, table_header_format)

        # 5. ตั้งค่าการพิมพ์ (Print Setup)
        worksheet.set_landscape() # แนวนอน
        worksheet.set_paper(9)    # A4
        worksheet.fit_to_pages(1, 0) # บีบความกว้างให้พอดี 1 หน้า

        writer.close()
        return FileResponse(filename, filename=filename)

    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- API อื่นๆ (User Management) ---
@app.get("/api/employees")
async def get_employees():
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees")
    data = cur.fetchall(); conn.close()
    return data

@app.post("/api/register")
async def register(name:str=Form(...), emp_id:str=Form(...), role:str=Form(...), file:UploadFile=File(...)):
    path = f"images/{emp_id}.jpg"
    with open(path, "wb") as b: shutil.copyfileobj(file.file, b)
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO employees (employee_id, name, role, image_path, embedding) VALUES (?, ?, ?, ?, NULL)", (emp_id, name, role, path))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.delete("/api/employees/delete/{emp_id}")
async def delete_employee(emp_id: str):
    conn = get_db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM employees WHERE employee_id = ?", (emp_id,))
    conn.commit(); conn.close()
    return {"status": "success"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9876)