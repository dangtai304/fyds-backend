"""Khởi tạo dữ liệu mẫu (idempotent): 3 bay, 3 xe nâng, tài khoản theo vai trò.

    python -m backend.seed           # dữ liệu nền
    python -m backend.seed --demo    # thêm 6 xe tải đang chờ để chạy thử
    python -m backend.seed --create  # tạo bảng bằng create_all (khi không dùng Alembic)
"""
from __future__ import annotations

import sys
from datetime import timedelta

from sqlalchemy import select

from .auth import hash_password
from .database import Base, SessionLocal, engine
from .dispatcher import check_in, now, try_dispatch
from .models import Bay, Forklift, Role, User

USERS = [
    ("admin", "admin123", "Quản trị hệ thống", Role.ADMIN, None),
    ("dieuphoi", "dieuphoi123", "Trưởng ca điều phối", Role.DISPATCHER, "CA1"),
    ("baove1", "baove123", "Bảo vệ Cổng 1", Role.GATE_SECURITY, "CA1"),
    ("baove2", "baove123", "Bảo vệ Cổng 2", Role.GATE_SECURITY, "CA1"),
    ("xenang1", "xenang123", "Nguyễn Văn An", Role.FORKLIFT_OPERATOR, "CA1"),
    ("xenang2", "xenang123", "Trần Văn Bình", Role.FORKLIFT_OPERATOR, "CA1"),
    ("xenang3", "xenang123", "Lê Văn Cường", Role.FORKLIFT_OPERATOR, "CA1"),
]

BAYS = [
    ("BAY-01", "Bay 01 - Dỡ bạt hông", "SIDE_LOADING"),
    ("BAY-02", "Bay 02 - Bốc đuôi", "REAR_LOADING"),
    ("BAY-03", "Bay 03 - Ramp container", "CONTAINER_RAMP"),
]

FORKLIFTS = [
    ("FL-01", "Toyota 8FD25", 2.5),
    ("FL-02", "Toyota 8FD30", 3.0),
    ("FL-03", "Komatsu FD35", 3.5),
]

DEMO_TRUCKS = [
    ("51C-123.45", "GATE_1", "CURTAIN_SIDE", "PALLET", "INBOUND", "Vận tải Phương Nam", "0901234567", "20 pallet nguyên liệu"),
    ("61H-456.78", "GATE_2", "BOX_TRUCK", "DRUM", "OUTBOUND", "Logistics Bình Dương", "0912345678", "40 phuy 200L"),
    ("50H-789.01", "GATE_1", "CONTAINER", "PALLET", "OUTBOUND", "Gemadept", "0987654321", "Cont 40' xuất khẩu"),
    ("51D-222.33", "GATE_2", "CURTAIN_SIDE", "IBC", "INBOUND", "Vận tải Hòa Bình", "0933445566", "12 bồn IBC"),
    ("72C-333.44", "GATE_1", "BOX_TRUCK", "PALLET", "INBOUND", "Sài Gòn Express", "0944556677", None),
    ("60C-555.66", "GATE_2", "CURTAIN_SIDE", "BULK", "OUTBOUND", "Đồng Nai Trans", "0955667788", "Bao jumbo"),
]


def seed(demo: bool = False) -> None:
    with SessionLocal() as db:
        for username, pw, name, role, shift in USERS:
            if not db.scalar(select(User).where(User.username == username)):
                db.add(User(username=username, password_hash=hash_password(pw), full_name=name,
                            role=role.value, shift=shift))
        for code, name, bay_type in BAYS:
            if not db.scalar(select(Bay).where(Bay.bay_code == code)):
                db.add(Bay(bay_code=code, name=name, bay_type=bay_type, status="EMPTY"))
        for code, model, cap in FORKLIFTS:
            if not db.scalar(select(Forklift).where(Forklift.code == code)):
                db.add(Forklift(code=code, model=model, capacity_ton=cap, status="IDLE", idle_since=now()))
        db.commit()

        if demo:
            base = now() - timedelta(minutes=len(DEMO_TRUCKS) * 4)
            for i, (plate, gate, vt, ct, mt, carrier, phone, note) in enumerate(DEMO_TRUCKS):
                try:
                    check_in(db, {
                        "license_plate": plate, "gate_in": gate, "vehicle_type": vt, "cargo_type": ct,
                        "movement_type": mt, "carrier_name": carrier, "driver_phone": phone,
                        "cargo_note": note, "checked_in_at": base + timedelta(minutes=i * 4),
                    }, None)
                except Exception as e:  # xe đã có trong sân
                    print(f"  bỏ qua {plate}: {e}")
            try_dispatch(db)
            db.commit()
    print("Seed xong." + (" Đã thêm xe demo." if demo else ""))


if __name__ == "__main__":
    if "--create" in sys.argv:
        Base.metadata.create_all(engine)
    seed(demo="--demo" in sys.argv)
