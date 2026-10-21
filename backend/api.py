"""REST API. Mọi thao tác ghi đều: giữ lock -> nghiệp vụ -> commit -> broadcast snapshot qua WS."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import dispatcher as d
from .auth import current_user, make_token, require_roles, verify_password
from .database import get_db
from .models import Bay, Forklift, Role, Trip, TripLog, User
from .schemas import (
    BayStatusIn,
    CheckinIn,
    CheckoutIn,
    LoginIn,
    PriorityIn,
    ReasonIn,
    ReassignIn,
)
from .state import build_state, iso, trip_dict
from .ws_manager import manager

router = APIRouter(prefix="/api")
DISPATCH_LOCK = asyncio.Lock()

GATE = require_roles(Role.GATE_SECURITY, Role.DISPATCHER)
OPERATOR = require_roles(Role.FORKLIFT_OPERATOR, Role.DISPATCHER)
DISPATCH = require_roles(Role.DISPATCHER)


def user_dict(u: User) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role, "shift": u.shift}


def called_events(trips: list[Trip], reason: str = "AUTO") -> list[dict]:
    return [{"kind": "TRIP_CALLED", "reason": reason, "trip": trip_dict(t)} for t in trips]


async def publish(db: Session, events: list[dict]) -> dict:
    """Commit rồi đẩy snapshot + sự kiện tới mọi client (TV, tablet, bảo vệ, điều phối)."""
    db.commit()
    state = build_state(db)
    await manager.broadcast({"type": "state", "state": state, "events": events})
    return state


def run(fn, *args):
    try:
        return fn(*args)
    except d.DispatchError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e


def ensure_operator_owns(user: User, forklift: Forklift | None) -> None:
    if user.role == Role.FORKLIFT_OPERATOR.value and (not forklift or forklift.current_operator_id != user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Lệnh này thuộc xe nâng khác")


# ------------------------------------------------------------------ auth

@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sai tên đăng nhập hoặc mật khẩu")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tài khoản đã bị khóa — liên hệ Quản trị")
    user.last_login_at = d.now()
    db.commit()
    return {"token": make_token(user.id, user.token_version or 0), "user": user_dict(user)}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_dict(user)


# ------------------------------------------------------------------ read

@router.get("/state")
def get_state(db: Session = Depends(get_db)):
    return build_state(db)


@router.get("/logs")
def get_logs(limit: int = 40, trip_id: int | None = None, db: Session = Depends(get_db),
             _: User = Depends(DISPATCH)):
    q = select(TripLog).order_by(TripLog.id.desc()).limit(min(limit, 200))
    if trip_id:
        q = q.where(TripLog.trip_id == trip_id)
    return [
        {
            "id": l.id, "trip_id": l.trip_id,
            "ticket_short": d.short_ticket(l.trip.ticket_number) if l.trip else None,
            "license_plate": l.trip.license_plate if l.trip else None,
            "action": l.action_name, "from_status": l.from_status, "to_status": l.to_status,
            "note": l.note, "user": l.user.full_name if l.user else "Hệ thống",
            "created_at": iso(l.created_at),
        }
        for l in db.scalars(q)
    ]


# ------------------------------------------------------------------ gate

@router.post("/trips/checkin")
async def checkin(body: CheckinIn, db: Session = Depends(get_db), user: User = Depends(GATE)):
    async with DISPATCH_LOCK:
        trip = run(d.check_in, db, body.model_dump(mode="json"), user.id)
        called = d.try_dispatch(db, user.id)
        await publish(db, [{"kind": "TRIP_CHECKED_IN", "trip": trip_dict(trip)}] + called_events(called))
        position = next((i + 1 for i, t in enumerate(d.waiting_trips(db)) if t.id == trip.id), None)
        return {"trip": trip_dict(trip), "queue_position": position}


@router.post("/trips/{trip_id}/checkout")
async def checkout(trip_id: int, body: CheckoutIn, db: Session = Depends(get_db), user: User = Depends(GATE)):
    async with DISPATCH_LOCK:
        trip = run(d.check_out, db, trip_id, body.gate_out.value, user.id)
        tat = round((trip.checked_out_at - trip.checked_in_at).total_seconds() / 60, 1)
        await publish(db, [{"kind": "TRIP_CHECKED_OUT", "trip": trip_dict(trip), "tat_min": tat}])
        return {"trip": trip_dict(trip), "tat_min": tat}


# ------------------------------------------------------------------ forklift

@router.post("/forklifts/{code}/claim")
async def claim_forklift(code: str, db: Session = Depends(get_db), user: User = Depends(OPERATOR)):
    async with DISPATCH_LOCK:
        fl = db.scalar(select(Forklift).where(Forklift.code == code.upper()))
        if not fl:
            raise HTTPException(404, "Không tìm thấy xe nâng")
        if user.role == Role.FORKLIFT_OPERATOR.value and fl.current_operator_id != user.id:
            for other in db.scalars(select(Forklift).where(Forklift.current_operator_id == user.id)):
                other.current_operator_id = None
            fl.current_operator_id = user.id
            d.add_log(db, None, "FORKLIFT_CLAIM", None, None, user.id, f"{user.full_name} nhận {fl.code}")
            db.flush()
            called = d.try_dispatch(db, user.id)
            await publish(db, called_events(called))
        return {"forklift": fl.code}


@router.post("/trips/{trip_id}/start")
async def start(trip_id: int, db: Session = Depends(get_db), user: User = Depends(OPERATOR)):
    async with DISPATCH_LOCK:
        trip = db.get(Trip, trip_id)
        ensure_operator_owns(user, trip.forklift if trip else None)
        trip = run(d.start_loading, db, trip_id, user.id)
        await publish(db, [{"kind": "TRIP_STARTED", "trip": trip_dict(trip)}])
        return trip_dict(trip)


@router.post("/trips/{trip_id}/complete")
async def complete(trip_id: int, db: Session = Depends(get_db), user: User = Depends(OPERATOR)):
    async with DISPATCH_LOCK:
        trip = db.get(Trip, trip_id)
        ensure_operator_owns(user, trip.forklift if trip else None)
        trip, called = run(d.complete_loading, db, trip_id, user.id)
        await publish(db, [{"kind": "TRIP_COMPLETED", "trip": trip_dict(trip)}] + called_events(called))
        return {"trip": trip_dict(trip), "next_called": [trip_dict(t) for t in called]}


@router.post("/forklifts/{code}/pause")
async def pause(code: str, body: ReasonIn, db: Session = Depends(get_db), user: User = Depends(OPERATOR)):
    async with DISPATCH_LOCK:
        fl = db.scalar(select(Forklift).where(Forklift.code == code.upper()))
        if not fl:
            raise HTTPException(404, "Không tìm thấy xe nâng")
        ensure_operator_owns(user, fl)
        run(d.pause_forklift, db, fl, body.reason, user.id)
        await publish(db, [{"kind": "FORKLIFT_PAUSED", "forklift": fl.code, "reason": body.reason}])
        return {"ok": True}


@router.post("/forklifts/{code}/resume")
async def resume(code: str, db: Session = Depends(get_db), user: User = Depends(OPERATOR)):
    async with DISPATCH_LOCK:
        fl = db.scalar(select(Forklift).where(Forklift.code == code.upper()))
        if not fl:
            raise HTTPException(404, "Không tìm thấy xe nâng")
        ensure_operator_owns(user, fl)
        _, called = run(d.resume_forklift, db, fl, user.id)
        await publish(db, [{"kind": "FORKLIFT_RESUMED", "forklift": fl.code}] + called_events(called))
        return {"ok": True}


# ------------------------------------------------------------------ dispatcher overrides

@router.post("/trips/{trip_id}/priority")
async def priority(trip_id: int, body: PriorityIn, db: Session = Depends(get_db), user: User = Depends(DISPATCH)):
    async with DISPATCH_LOCK:
        trip, called = run(d.set_priority, db, trip_id, body.score, body.reason, user.id)
        await publish(db, [{"kind": "PRIORITY_OVERRIDE", "trip": trip_dict(trip)}] + called_events(called))
        return trip_dict(trip)


@router.post("/trips/{trip_id}/reassign")
async def reassign(trip_id: int, body: ReassignIn, db: Session = Depends(get_db), user: User = Depends(DISPATCH)):
    async with DISPATCH_LOCK:
        trip, called = run(d.reassign, db, trip_id, body.bay_id, body.reason, user.id)
        events = called_events(called[:1], "MANUAL") + called_events(called[1:])
        await publish(db, events)
        return trip_dict(trip)


@router.post("/trips/{trip_id}/cancel")
async def cancel(trip_id: int, body: ReasonIn, db: Session = Depends(get_db), user: User = Depends(DISPATCH)):
    async with DISPATCH_LOCK:
        trip, called = run(d.cancel_trip, db, trip_id, body.reason, user.id)
        await publish(db, [{"kind": "TRIP_CANCELLED", "trip": trip_dict(trip)}] + called_events(called))
        return trip_dict(trip)


@router.post("/bays/{bay_id}/status")
async def bay_status(bay_id: int, body: BayStatusIn, db: Session = Depends(get_db), user: User = Depends(DISPATCH)):
    async with DISPATCH_LOCK:
        bay = db.get(Bay, bay_id)
        if not bay:
            raise HTTPException(404, "Không tìm thấy bay")
        if body.status == "MAINTENANCE" and len((body.reason or "").strip()) < 5:
            raise HTTPException(422, "Cần nhập lý do bảo trì (tối thiểu 5 ký tự)")
        called = run(d.set_bay_status, db, bay, body.status, body.reason, user.id)
        await publish(db, [{"kind": "BAY_STATUS", "bay": bay.bay_code, "status": bay.status}] + called_events(called))
        return {"ok": True}
