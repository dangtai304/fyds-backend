"""Quản lý người dùng (chỉ ADMIN) + tự đổi mật khẩu.

Vai trò:  FORKLIFT_OPERATOR (Lái xe nâng) · GATE_SECURITY (Bảo vệ)
          DISPATCHER (Control Tower / điều phối) · ADMIN (Quản trị)
"""
from __future__ import annotations

import re
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import current_user, hash_password, make_token, require_roles, verify_password
from .database import get_db
from .dispatcher import add_log, now
from .models import Forklift, Role, User
from .state import iso

router = APIRouter(prefix="/api")
ADMIN_ONLY = require_roles()  # require_roles luôn cho ADMIN; không thêm vai trò nào khác

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,50}$")
Password = Annotated[str, StringConstraints(min_length=6, max_length=128)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)]


class UserCreateIn(BaseModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=3, max_length=50)]
    full_name: Name
    role: Role
    shift: Optional[str] = Field(None, max_length=20)
    password: Password


class UserUpdateIn(BaseModel):
    full_name: Optional[Name] = None
    role: Optional[Role] = None
    shift: Optional[str] = Field(None, max_length=20)
    is_active: Optional[bool] = None


class PasswordResetIn(BaseModel):
    password: Password


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: Password


def user_out(u: User) -> dict:
    return {
        "id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role, "shift": u.shift,
        "is_active": u.is_active, "created_at": iso(u.created_at), "last_login_at": iso(u.last_login_at),
    }


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == Role.ADMIN.value, User.is_active.is_(True))) or 0


def _revoke(db: Session, u: User) -> None:
    """Thu hồi mọi phiên: tăng token_version, nhả xe nâng đang giữ."""
    u.token_version = (u.token_version or 0) + 1
    for fl in db.scalars(select(Forklift).where(Forklift.current_operator_id == u.id)):
        fl.current_operator_id = None


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(ADMIN_ONLY)):
    users = db.scalars(select(User).order_by(User.role, User.username))
    return [user_out(u) for u in users]


@router.post("/users", status_code=201)
def create_user(body: UserCreateIn, db: Session = Depends(get_db), admin: User = Depends(ADMIN_ONLY)):
    if not USERNAME_RE.match(body.username):
        raise HTTPException(422, "Tên đăng nhập 3–50 ký tự, chỉ gồm a-z, 0-9, dấu chấm, gạch dưới, gạch ngang")
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, f"Tên đăng nhập '{body.username}' đã tồn tại")
    u = User(username=body.username, full_name=body.full_name, role=body.role.value, shift=body.shift or None,
             password_hash=hash_password(body.password), is_active=True, token_version=0, created_at=now())
    db.add(u)
    db.flush()
    add_log(db, None, "USER_CREATE", None, u.role, admin.id, f"Tạo tài khoản {u.username} ({u.full_name})")
    db.commit()
    return user_out(u)


@router.post("/users/{user_id}")
def update_user(user_id: int, body: UserUpdateIn, db: Session = Depends(get_db), admin: User = Depends(ADMIN_ONLY)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Không tìm thấy người dùng")
    changes: list[str] = []
    revoke = False

    if body.role is not None and body.role.value != u.role:
        if u.id == admin.id:
            raise HTTPException(409, "Không thể tự đổi vai trò của chính mình")
        if u.role == Role.ADMIN.value and u.is_active and _active_admins(db) <= 1:
            raise HTTPException(409, "Phải còn ít nhất một Quản trị đang hoạt động")
        changes.append(f"vai trò {u.role} → {body.role.value}")
        u.role = body.role.value
        revoke = True

    if body.is_active is not None and body.is_active != u.is_active:
        if u.id == admin.id:
            raise HTTPException(409, "Không thể tự khóa tài khoản của chính mình")
        if not body.is_active and u.role == Role.ADMIN.value and _active_admins(db) <= 1:
            raise HTTPException(409, "Phải còn ít nhất một Quản trị đang hoạt động")
        changes.append("mở khóa" if body.is_active else "khóa tài khoản")
        u.is_active = body.is_active
        revoke = revoke or not body.is_active

    if body.full_name is not None and body.full_name != u.full_name:
        changes.append(f"tên '{u.full_name}' → '{body.full_name}'")
        u.full_name = body.full_name
    if "shift" in body.model_fields_set and (body.shift or None) != u.shift:
        changes.append(f"ca {u.shift or '-'} → {body.shift or '-'}")
        u.shift = body.shift or None

    if revoke:
        _revoke(db, u)
    if changes:
        add_log(db, None, "USER_UPDATE", None, None, admin.id, f"{u.username}: " + "; ".join(changes))
    db.commit()
    return user_out(u)


@router.post("/users/{user_id}/password")
def reset_password(user_id: int, body: PasswordResetIn, db: Session = Depends(get_db), admin: User = Depends(ADMIN_ONLY)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Không tìm thấy người dùng")
    u.password_hash = hash_password(body.password)
    _revoke(db, u)
    add_log(db, None, "USER_PASSWORD_RESET", None, None, admin.id, f"Đặt lại mật khẩu cho {u.username}")
    db.commit()
    return {"ok": True}


@router.post("/me/password")
def change_own_password(body: PasswordChangeIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mật khẩu hiện tại không đúng")
    user.password_hash = hash_password(body.new_password)
    user.token_version = (user.token_version or 0) + 1  # đăng xuất các thiết bị khác
    add_log(db, None, "USER_PASSWORD_CHANGE", None, None, user.id, f"{user.username} tự đổi mật khẩu")
    db.commit()
    return {"token": make_token(user.id, user.token_version)}
