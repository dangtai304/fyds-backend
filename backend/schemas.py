from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints

from .models import CargoType, Gate, MovementType, VehicleType

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=300)]


class LoginIn(BaseModel):
    username: str
    password: str


class CheckinIn(BaseModel):
    license_plate: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=20)]
    gate_in: Gate
    vehicle_type: VehicleType
    cargo_type: CargoType
    movement_type: MovementType = MovementType.INBOUND
    carrier_name: Optional[str] = Field(None, max_length=120)
    driver_phone: Optional[str] = Field(None, max_length=20)
    cargo_note: Optional[str] = Field(None, max_length=500)


class CheckoutIn(BaseModel):
    gate_out: Gate


class ReasonIn(BaseModel):
    reason: Reason


class PriorityIn(ReasonIn):
    score: int = Field(50, ge=1, le=100)


class ReassignIn(ReasonIn):
    bay_id: int


class BayStatusIn(BaseModel):
    status: Literal["EMPTY", "MAINTENANCE"]
    reason: Optional[str] = Field(None, max_length=300)
