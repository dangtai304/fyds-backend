"""Snapshot trạng thái toàn hệ thống gửi qua WebSocket + thống kê realtime."""
from __future__ import annotations

from datetime import datetime, time
from statistics import mean

from sqlalchemy import select
from sqlalchemy.orm import Session

from .dispatcher import active_trip_for_forklift, short_ticket, waiting_trips
from .models import ACTIVE_STATUSES, Bay, Forklift, QueueStatus, Trip


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def trip_dict(t: Trip) -> dict:
    return {
        "id": t.id,
        "ticket": t.ticket_number,
        "ticket_short": short_ticket(t.ticket_number),
        "license_plate": t.license_plate,
        "carrier_name": t.carrier_name,
        "movement_type": t.movement_type,
        "vehicle_type": t.vehicle_type,
        "cargo_type": t.cargo_type,
        "cargo_note": t.cargo_note,
        "gate_in": t.gate_in,
        "gate_out": t.gate_out,
        "queue_status": t.queue_status,
        "priority_score": t.priority_score,
        "priority_reason": t.priority_reason,
        "override_reason": t.override_reason,
        "is_manual_call": t.is_manual_call,
        "fifo_skipped": t.fifo_skipped,
        "bay_id": t.assigned_bay_id,
        "bay_code": t.bay.bay_code if t.bay else None,
        "bay_name": t.bay.name if t.bay else None,
        "bay_type": t.bay.bay_type if t.bay else None,
        "forklift_id": t.assigned_forklift_id,
        "forklift_code": t.forklift.code if t.forklift else None,
        "checked_in_at": iso(t.checked_in_at),
        "called_at": iso(t.called_at),
        "started_at": iso(t.started_at),
        "finished_at": iso(t.finished_at),
        "checked_out_at": iso(t.checked_out_at),
    }


def _avg_minutes(pairs) -> float | None:
    vals = [(b - a).total_seconds() / 60 for a, b in pairs if a and b]
    return round(mean(vals), 1) if vals else None


def compute_stats(db: Session) -> dict:
    start = datetime.combine(datetime.now().date(), time.min)
    today = list(db.scalars(select(Trip).where(Trip.checked_in_at >= start)))
    checked_out = [t for t in today if t.checked_out_at]
    called = [t for t in today if t.called_at]
    jumps = [t for t in called if t.fifo_skipped > 0]
    unjustified = [t for t in jumps if not (t.priority_reason or t.override_reason)]
    in_yard = [t for t in today if t.queue_status in
               (QueueStatus.WAITING.value, QueueStatus.CALLED.value, QueueStatus.LOADING.value, QueueStatus.COMPLETED.value)]
    return {
        "total_today": len(today),
        "in_yard": len(in_yard),
        "checked_out": len(checked_out),
        "cancelled": sum(1 for t in today if t.queue_status == QueueStatus.CANCELLED.value),
        "avg_tat_min": _avg_minutes((t.checked_in_at, t.checked_out_at) for t in checked_out),
        "avg_wait_min": _avg_minutes((t.checked_in_at, t.called_at) for t in called),
        "avg_load_min": _avg_minutes((t.started_at, t.finished_at) for t in today),
        "called_today": len(called),
        "fifo_jumps": len(jumps),                # vượt hàng có lý do (ưu tiên/override)
        "fifo_violations": len(unjustified),     # vượt hàng KHÔNG lý do
        "fifo_violation_rate": round(100 * len(unjustified) / len(called), 1) if called else 0.0,
        "overrides": sum(1 for t in today if t.priority_score > 0 or t.is_manual_call or t.override_reason),
    }


def build_state(db: Session) -> dict:
    bays = list(db.scalars(select(Bay).order_by(Bay.id)))
    active = list(db.scalars(select(Trip).where(Trip.queue_status.in_(ACTIVE_STATUSES))))
    by_bay = {t.assigned_bay_id: t for t in active}
    forklifts = list(db.scalars(select(Forklift).order_by(Forklift.id)))
    queue = waiting_trips(db)
    completed = list(db.scalars(
        select(Trip).where(Trip.queue_status == QueueStatus.COMPLETED.value).order_by(Trip.finished_at)
    ))
    active_sorted = sorted(active, key=lambda t: (t.queue_status != QueueStatus.CALLED.value, t.called_at or datetime.min))

    return {
        "server_time": iso(datetime.now()),
        "bays": [
            {
                "id": b.id, "bay_code": b.bay_code, "name": b.name, "bay_type": b.bay_type,
                "status": b.status, "note": b.note,
                "trip": trip_dict(by_bay[b.id]) if b.id in by_bay else None,
            }
            for b in bays
        ],
        "forklifts": [
            {
                "id": f.id, "code": f.code, "model": f.model, "capacity_ton": f.capacity_ton,
                "status": f.status, "pause_reason": f.pause_reason,
                "operator_id": f.current_operator_id,
                "operator_name": f.operator.full_name if f.operator else None,
                "trip_id": (lambda t: t.id if t else None)(active_trip_for_forklift(db, f.id)),
            }
            for f in forklifts
        ],
        "queue": [dict(trip_dict(t), position=i + 1) for i, t in enumerate(queue)],
        "active": [trip_dict(t) for t in active_sorted],
        "awaiting_checkout": [trip_dict(t) for t in completed],
        "stats": compute_stats(db),
    }
