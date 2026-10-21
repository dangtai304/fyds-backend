"""Bộ điều phối FIFO và các chuyển trạng thái của chuyến xe.

Mọi hàm ở đây là đồng bộ, thao tác trên một Session; caller chịu trách nhiệm
commit và phát WebSocket. Caller cũng phải giữ DISPATCH_LOCK để tránh 2 request
cùng gán 1 bay / 1 xe nâng (đủ cho 1 tiến trình; chạy nhiều worker thì cần
SELECT ... FOR UPDATE trên PostgreSQL).
"""
from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Bay,
    BayStatus,
    BayType,
    Forklift,
    ForkliftStatus,
    QueueStatus,
    Trip,
    TripLog,
    VehicleType,
)

# Ma trận tương thích: thứ tự trong list = thứ tự ưu tiên bay.
# Xe bạt hông ưu tiên bay dỡ hông, có thể dùng bay bốc đuôi khi cần.
# Container cần ramp để xe nâng lên sàn container -> chỉ CONTAINER_RAMP.
COMPATIBILITY: dict[str, list[str]] = {
    VehicleType.CURTAIN_SIDE.value: [BayType.SIDE_LOADING.value, BayType.REAR_LOADING.value],
    VehicleType.BOX_TRUCK.value: [BayType.REAR_LOADING.value, BayType.CONTAINER_RAMP.value],
    VehicleType.CONTAINER.value: [BayType.CONTAINER_RAMP.value],
}

# Nếu bật, chỉ giao lệnh cho xe nâng đang có tài xế đăng nhập trên tablet.
REQUIRE_OPERATOR = os.getenv("FYDS_REQUIRE_OPERATOR", "0") == "1"


class DispatchError(Exception):
    """Lỗi nghiệp vụ -> HTTP 409."""


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def compatible_bay_types(vehicle_type: str) -> list[str]:
    return COMPATIBILITY.get(vehicle_type, [])


def is_compatible(vehicle_type: str, bay_type: str) -> bool:
    return bay_type in compatible_bay_types(vehicle_type)


def add_log(db: Session, trip: Trip | None, action: str, frm: str | None, to: str | None,
            user_id: int | None, note: str | None = None) -> None:
    db.add(TripLog(
        trip_id=trip.id if trip else None,
        from_status=frm,
        to_status=to,
        changed_by_user_id=user_id,
        action_name=action,
        note=note,
        created_at=now(),
    ))


def waiting_trips(db: Session) -> list[Trip]:
    """Hàng đợi chuẩn: ORDER BY priority_score DESC, checked_in_at ASC."""
    return list(db.scalars(
        select(Trip)
        .where(Trip.queue_status == QueueStatus.WAITING.value)
        .order_by(Trip.priority_score.desc(), Trip.checked_in_at.asc(), Trip.id.asc())
    ))


def _empty_bays(db: Session) -> list[Bay]:
    return list(db.scalars(select(Bay).where(Bay.status == BayStatus.EMPTY.value).order_by(Bay.id)))


def _idle_forklifts(db: Session) -> list[Forklift]:
    q = select(Forklift).where(Forklift.status == ForkliftStatus.IDLE.value)
    if REQUIRE_OPERATOR:
        q = q.where(Forklift.current_operator_id.is_not(None))
    fls = list(db.scalars(q))
    # Ưu tiên xe nâng có tài xế, sau đó xe rảnh lâu nhất (chia việc đều).
    fls.sort(key=lambda f: (f.current_operator_id is None, f.idle_since or datetime.min, f.id))
    return fls


def pick_bay(trip: Trip, empty_bays: list[Bay]) -> Bay | None:
    for bay_type in compatible_bay_types(trip.vehicle_type):
        for bay in empty_bays:
            if bay.bay_type == bay_type:
                return bay
    return None


def count_fifo_skipped(db: Session, trip: Trip, bay: Bay) -> int:
    """Số xe đang chờ, đến TRƯỚC, ưu tiên >= xe này, và cũng vào được bay này -> bị vượt."""
    n = 0
    for other in waiting_trips(db):
        if other.id == trip.id:
            continue
        earlier = (other.checked_in_at, other.id) < (trip.checked_in_at, trip.id)
        if earlier and other.priority_score >= trip.priority_score and is_compatible(other.vehicle_type, bay.bay_type):
            n += 1
    return n


def _assign(db: Session, trip: Trip, bay: Bay, forklift: Forklift, user_id: int | None,
            manual: bool = False, reason: str | None = None) -> None:
    trip.fifo_skipped = count_fifo_skipped(db, trip, bay)
    trip.is_manual_call = manual
    if manual:
        trip.override_reason = reason
    prev = trip.queue_status
    trip.queue_status = QueueStatus.CALLED.value
    trip.bay = bay
    trip.forklift = forklift
    trip.called_at = now()
    bay.status = BayStatus.OCCUPIED.value
    forklift.status = ForkliftStatus.BUSY.value
    forklift.idle_since = None
    note = f"Gọi vào {bay.bay_code}, xe nâng {forklift.code}"
    if manual:
        note += f" (gọi thủ công: {reason})"
    add_log(db, trip, "MANUAL_CALL" if manual else "AUTO_CALL", prev, trip.queue_status, user_id, note)


def try_dispatch(db: Session, user_id: int | None = None) -> list[Trip]:
    """Gọi xe khi có bay EMPTY + xe nâng IDLE. Lặp đến khi hết tài nguyên hoặc hết xe phù hợp."""
    called: list[Trip] = []
    while True:
        forklifts = _idle_forklifts(db)
        bays = _empty_bays(db)
        if not forklifts or not bays:
            break
        match = None
        for trip in waiting_trips(db):  # đã theo đúng thứ tự ưu tiên/FIFO
            bay = pick_bay(trip, bays)
            if bay:
                match = (trip, bay)
                break
        if not match:
            break
        trip, bay = match
        _assign(db, trip, bay, forklifts[0], user_id)
        db.flush()
        called.append(trip)
    return called


# ---------------------------------------------------------------- transitions

def next_ticket_number(db: Session, movement_type: str) -> str:
    prefix = "IN" if movement_type == "INBOUND" else "OUT"
    day = now().strftime("%y%m%d")
    stem = f"{prefix}-{day}-"
    count = db.scalar(select(func.count()).select_from(Trip).where(Trip.ticket_number.like(f"{stem}%"))) or 0
    return f"{stem}{count + 1:03d}"


def check_in(db: Session, data: dict, user_id: int | None) -> Trip:
    plate = normalize_plate(data["license_plate"])
    existing = db.scalar(select(Trip).where(
        Trip.license_plate == plate,
        Trip.queue_status.in_([QueueStatus.WAITING.value, QueueStatus.CALLED.value,
                               QueueStatus.LOADING.value, QueueStatus.COMPLETED.value]),
    ))
    if existing:
        raise DispatchError(f"Xe {plate} đang có phiếu {short_ticket(existing.ticket_number)} trong sân, chưa check-out")
    if not compatible_bay_types(data["vehicle_type"]):
        raise DispatchError("Loại xe chưa được cấu hình bay tương thích")
    trip = Trip(
        ticket_number=next_ticket_number(db, data["movement_type"]),
        license_plate=plate,
        carrier_name=(data.get("carrier_name") or None),
        driver_phone=(data.get("driver_phone") or None),
        movement_type=data["movement_type"],
        vehicle_type=data["vehicle_type"],
        cargo_type=data["cargo_type"],
        cargo_note=(data.get("cargo_note") or None),
        gate_in=data["gate_in"],
        queue_status=QueueStatus.WAITING.value,
        priority_score=0,
        checked_in_at=data.get("checked_in_at") or now(),
        created_by_user_id=user_id,
    )
    db.add(trip)
    db.flush()
    add_log(db, trip, "CHECK_IN", None, trip.queue_status, user_id, f"Vào {trip.gate_in}")
    return trip


def _get_trip(db: Session, trip_id: int) -> Trip:
    trip = db.get(Trip, trip_id)
    if not trip:
        raise DispatchError("Không tìm thấy chuyến xe")
    return trip


def start_loading(db: Session, trip_id: int, user_id: int | None) -> Trip:
    trip = _get_trip(db, trip_id)
    if trip.queue_status != QueueStatus.CALLED.value:
        raise DispatchError("Chỉ bắt đầu bốc dỡ được khi xe đang ở trạng thái ĐANG GỌI")
    if trip.forklift and trip.forklift.status == ForkliftStatus.PAUSED.value:
        raise DispatchError("Xe nâng đang tạm dừng — hãy bấm TIẾP TỤC trước")
    trip.queue_status = QueueStatus.LOADING.value
    trip.started_at = now()
    add_log(db, trip, "START_LOADING", QueueStatus.CALLED.value, trip.queue_status, user_id)
    return trip


def _release(db: Session, trip: Trip) -> None:
    if trip.bay and trip.bay.status == BayStatus.OCCUPIED.value:
        trip.bay.status = BayStatus.EMPTY.value
    if trip.forklift:
        fl = trip.forklift
        if fl.status == ForkliftStatus.BUSY.value:
            fl.status = ForkliftStatus.IDLE.value
        fl.idle_since = now()


def complete_loading(db: Session, trip_id: int, user_id: int | None) -> tuple[Trip, list[Trip]]:
    trip = _get_trip(db, trip_id)
    if trip.queue_status != QueueStatus.LOADING.value:
        raise DispatchError("Chỉ hoàn thành được khi xe đang BỐC DỠ")
    trip.queue_status = QueueStatus.COMPLETED.value
    trip.finished_at = now()
    _release(db, trip)
    add_log(db, trip, "COMPLETE_LOADING", QueueStatus.LOADING.value, trip.queue_status, user_id,
            f"Giải phóng {trip.bay.bay_code if trip.bay else ''}")
    db.flush()
    return trip, try_dispatch(db, user_id)


def check_out(db: Session, trip_id: int, gate_out: str, user_id: int | None) -> Trip:
    trip = _get_trip(db, trip_id)
    if trip.queue_status != QueueStatus.COMPLETED.value:
        raise DispatchError("Xe chưa bốc dỡ xong, chưa thể check-out")
    trip.queue_status = QueueStatus.CHECKED_OUT.value
    trip.gate_out = gate_out
    trip.checked_out_at = now()
    tat = (trip.checked_out_at - trip.checked_in_at).total_seconds() / 60
    add_log(db, trip, "CHECK_OUT", QueueStatus.COMPLETED.value, trip.queue_status, user_id,
            f"Ra {gate_out}, TAT {tat:.0f} phút")
    return trip


def cancel_trip(db: Session, trip_id: int, reason: str, user_id: int | None) -> tuple[Trip, list[Trip]]:
    trip = _get_trip(db, trip_id)
    if trip.queue_status not in (QueueStatus.WAITING.value, QueueStatus.CALLED.value):
        raise DispatchError("Chỉ hủy được xe đang chờ hoặc đang gọi")
    prev = trip.queue_status
    if prev == QueueStatus.CALLED.value:
        _release(db, trip)
    trip.queue_status = QueueStatus.CANCELLED.value
    add_log(db, trip, "CANCEL", prev, trip.queue_status, user_id, reason)
    db.flush()
    return trip, try_dispatch(db, user_id)


def set_priority(db: Session, trip_id: int, score: int, reason: str, user_id: int | None) -> tuple[Trip, list[Trip]]:
    trip = _get_trip(db, trip_id)
    if trip.queue_status != QueueStatus.WAITING.value:
        raise DispatchError("Chỉ đặt ưu tiên cho xe đang chờ")
    old = trip.priority_score
    trip.priority_score = score
    trip.priority_reason = reason
    add_log(db, trip, "PRIORITY_OVERRIDE", trip.queue_status, trip.queue_status, user_id,
            f"Ưu tiên {old} → {score}: {reason}")
    db.flush()
    return trip, try_dispatch(db, user_id)


def reassign(db: Session, trip_id: int, bay_id: int, reason: str, user_id: int | None) -> tuple[Trip, list[Trip]]:
    """CALLED: đổi sang bay khác. WAITING: gọi thủ công vào bay chỉ định."""
    trip = _get_trip(db, trip_id)
    bay = db.get(Bay, bay_id)
    if not bay:
        raise DispatchError("Không tìm thấy bay")
    if bay.status != BayStatus.EMPTY.value:
        raise DispatchError(f"{bay.bay_code} không trống")

    if trip.queue_status == QueueStatus.CALLED.value:
        old_bay = trip.bay
        if old_bay:
            old_bay.status = BayStatus.EMPTY.value
        trip.bay = bay
        bay.status = BayStatus.OCCUPIED.value
        trip.called_at = now()
        trip.override_reason = reason
        add_log(db, trip, "CHANGE_BAY", trip.queue_status, trip.queue_status, user_id,
                f"{old_bay.bay_code if old_bay else '-'} → {bay.bay_code}: {reason}")
        db.flush()
        return trip, [trip] + try_dispatch(db, user_id)

    if trip.queue_status == QueueStatus.WAITING.value:
        forklifts = _idle_forklifts(db)
        if not forklifts:
            raise DispatchError("Không có xe nâng rảnh để gọi xe")
        _assign(db, trip, bay, forklifts[0], user_id, manual=True, reason=reason)
        db.flush()
        return trip, [trip] + try_dispatch(db, user_id)

    raise DispatchError("Chỉ đổi bay khi xe đang chờ hoặc đang gọi (chưa bốc dỡ)")


def pause_forklift(db: Session, forklift: Forklift, reason: str, user_id: int | None) -> Forklift:
    if forklift.status == ForkliftStatus.PAUSED.value:
        raise DispatchError("Xe nâng đã ở trạng thái tạm dừng")
    active = active_trip_for_forklift(db, forklift.id)
    old = forklift.status
    forklift.status = ForkliftStatus.PAUSED.value
    forklift.pause_reason = reason
    add_log(db, active, "FORKLIFT_PAUSE", old, ForkliftStatus.PAUSED.value, user_id, f"{forklift.code}: {reason}")
    return forklift


def resume_forklift(db: Session, forklift: Forklift, user_id: int | None) -> tuple[Forklift, list[Trip]]:
    if forklift.status != ForkliftStatus.PAUSED.value:
        raise DispatchError("Xe nâng không ở trạng thái tạm dừng")
    active = active_trip_for_forklift(db, forklift.id)
    forklift.status = ForkliftStatus.BUSY.value if active else ForkliftStatus.IDLE.value
    if not active:
        forklift.idle_since = now()
    forklift.pause_reason = None
    add_log(db, active, "FORKLIFT_RESUME", ForkliftStatus.PAUSED.value, forklift.status, user_id, forklift.code)
    db.flush()
    return forklift, try_dispatch(db, user_id)


def set_bay_status(db: Session, bay: Bay, new_status: str, reason: str | None, user_id: int | None) -> list[Trip]:
    if bay.status == BayStatus.OCCUPIED.value:
        raise DispatchError("Bay đang có xe, không đổi trạng thái được")
    old = bay.status
    bay.status = new_status
    bay.note = reason if new_status == BayStatus.MAINTENANCE.value else None
    add_log(db, None, "BAY_STATUS", old, new_status, user_id, f"{bay.bay_code}: {reason or ''}".strip())
    db.flush()
    return try_dispatch(db, user_id)


def active_trip_for_forklift(db: Session, forklift_id: int) -> Trip | None:
    return db.scalar(select(Trip).where(
        Trip.assigned_forklift_id == forklift_id,
        Trip.queue_status.in_([QueueStatus.CALLED.value, QueueStatus.LOADING.value]),
    ))


# ---------------------------------------------------------------- helpers

def normalize_plate(plate: str) -> str:
    return "".join(plate.upper().split())


def short_ticket(ticket: str) -> str:
    parts = ticket.split("-")
    return f"{parts[0]}-{parts[-1]}" if len(parts) >= 3 else ticket
