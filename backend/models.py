"""SQLAlchemy models cho FYDS.

Enum được lưu dưới dạng VARCHAR (không dùng native ENUM của DB) để migration
SQLite <-> PostgreSQL đơn giản và thêm giá trị mới không cần ALTER TYPE.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover
        return self.value


class Role(StrEnum):
    GATE_SECURITY = "GATE_SECURITY"
    FORKLIFT_OPERATOR = "FORKLIFT_OPERATOR"
    DISPATCHER = "DISPATCHER"
    ADMIN = "ADMIN"


class BayType(StrEnum):
    SIDE_LOADING = "SIDE_LOADING"
    REAR_LOADING = "REAR_LOADING"
    CONTAINER_RAMP = "CONTAINER_RAMP"


class BayStatus(StrEnum):
    EMPTY = "EMPTY"
    OCCUPIED = "OCCUPIED"
    MAINTENANCE = "MAINTENANCE"


class ForkliftStatus(StrEnum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    PAUSED = "PAUSED"


class MovementType(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class VehicleType(StrEnum):
    CURTAIN_SIDE = "CURTAIN_SIDE"
    BOX_TRUCK = "BOX_TRUCK"
    CONTAINER = "CONTAINER"


class CargoType(StrEnum):
    PALLET = "PALLET"
    DRUM = "DRUM"
    IBC = "IBC"
    BULK = "BULK"


class Gate(StrEnum):
    GATE_1 = "GATE_1"
    GATE_2 = "GATE_2"


class QueueStatus(StrEnum):
    WAITING = "WAITING"
    CALLED = "CALLED"
    LOADING = "LOADING"
    COMPLETED = "COMPLETED"
    CHECKED_OUT = "CHECKED_OUT"
    CANCELLED = "CANCELLED"


ACTIVE_STATUSES = (QueueStatus.CALLED.value, QueueStatus.LOADING.value)
IN_YARD_STATUSES = (
    QueueStatus.WAITING.value,
    QueueStatus.CALLED.value,
    QueueStatus.LOADING.value,
    QueueStatus.COMPLETED.value,
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(30))
    shift: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)


class Bay(Base):
    __tablename__ = "bays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bay_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    bay_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default=BayStatus.EMPTY.value)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Forklift(Base):
    __tablename__ = "forklifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    model: Mapped[str] = mapped_column(String(100))
    capacity_ton: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default=ForkliftStatus.IDLE.value)
    current_operator_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    idle_since: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    pause_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    operator: Mapped[Optional[User]] = relationship(foreign_keys=[current_operator_id])


class Trip(Base):
    __tablename__ = "trips"
    __table_args__ = (
        Index("ix_trips_queue", "queue_status", "priority_score", "checked_in_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Lưu đầy đủ IN-YYMMDD-NNN để unique xuyên ngày; hiển thị dạng ngắn #IN-NNN.
    ticket_number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    license_plate: Mapped[str] = mapped_column(String(20), index=True)
    carrier_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    driver_phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    movement_type: Mapped[str] = mapped_column(String(20))
    vehicle_type: Mapped[str] = mapped_column(String(20))
    cargo_type: Mapped[str] = mapped_column(String(20))
    cargo_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gate_in: Mapped[str] = mapped_column(String(10))
    gate_out: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    assigned_bay_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bays.id"), nullable=True)
    assigned_forklift_id: Mapped[Optional[int]] = mapped_column(ForeignKey("forklifts.id"), nullable=True)

    queue_status: Mapped[str] = mapped_column(String(20), default=QueueStatus.WAITING.value, index=True)
    priority_score: Mapped[int] = mapped_column(Integer, default=0)
    priority_reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    # Đo lường FIFO: số xe đến trước (cùng/hơn mức ưu tiên, tương thích bay) bị vượt khi xe này được gọi.
    fifo_skipped: Mapped[int] = mapped_column(Integer, default=0)
    is_manual_call: Mapped[bool] = mapped_column(default=False)
    override_reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    checked_in_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    called_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    checked_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)

    bay: Mapped[Optional[Bay]] = relationship(foreign_keys=[assigned_bay_id])
    forklift: Mapped[Optional[Forklift]] = relationship(foreign_keys=[assigned_forklift_id])


class TripLog(Base):
    __tablename__ = "trip_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable: một số sự kiện (tạm dừng xe nâng rảnh, bảo trì bay) không gắn với chuyến xe.
    trip_id: Mapped[Optional[int]] = mapped_column(ForeignKey("trips.id"), nullable=True, index=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    changed_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    action_name: Mapped[str] = mapped_column(String(50))
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    trip: Mapped[Optional[Trip]] = relationship()
    user: Mapped[Optional[User]] = relationship()
