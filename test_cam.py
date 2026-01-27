import cv2

cap = cv2.VideoCapture(1) # ลองเปลี่ยนเลข 0 เป็น 1 ถ้ามีหลายกล้อง

if not cap.isOpened():
    print("❌ เปิดกล้องไม่ได้! ตรวจสอบว่าไม่มีโปรแกรมอื่นใช้งานอยู่")
else:
    print("✅ เปิดกล้องสำเร็จ! กด 'q' เพื่อออก")
    while True:
        ret, frame = cap.read()
        if not ret:
            print("อ่านเฟรมภาพไม่ได้")
            break
        cv2.imshow('Test Camera', frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

cap.release()
cv2.destroyAllWindows()