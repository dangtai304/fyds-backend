"""Xác thực gọn nhẹ: PBKDF2 cho mật khẩu + token ký HMAC (không cần thư viện ngoài)."""
import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .database import get_db
from .models import Role, User

TOKEN_TTL_SECONDS = int(os.getenv("FYDS_TOKEN_TTL_HOURS", "14")) * 3600  # ~1 ca + dư


def _load_secret() -> bytes:
    env = os.getenv("FYDS_SECRET")
    if env:
        return env.encode()
    path = Path(os.getenv("FYDS_SECRET_FILE", ".fyds_secret"))
    if path.exists():
        return path.read_text().strip().encode()
    value = secrets.token_hex(32)
    try:
        path.write_text(value)
    except OSError:
        pass
    return value.encode()


SECRET = _load_secret()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(dk.hex(), dk_hex)


def make_token(user_id: int) -> str:
    payload = f"{user_id}.{int(time.time())}"
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def read_token(token: str) -> int | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        uid, issued, sig = raw.split(".")
    except Exception:
        return None
    expected = hmac.new(SECRET, f"{uid}.{issued}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if time.time() - int(issued) > TOKEN_TTL_SECONDS:
        return None
    return int(uid)


_bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Vui lòng đăng nhập")
    uid = read_token(creds.credentials)
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn")
    return user


def require_roles(*roles: Role):
    allowed = {r.value for r in roles} | {Role.ADMIN.value}

    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Tài khoản không có quyền thực hiện thao tác này")
        return user

    return dep
