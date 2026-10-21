"""Kết nối CSDL.

Thứ tự ưu tiên:
  1. FYDS_DATABASE_URL  (đặt tay)
  2. DATABASE_URL       (Render / Heroku / Railway tự cấp khi gắn Postgres)
  3. sqlite:///./fyds.db (pilot chạy local)

URL dạng postgres:// hoặc postgresql:// được đổi sang driver psycopg 3 (postgresql+psycopg://).
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def normalize_url(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DATABASE_URL = normalize_url(
    os.getenv("FYDS_DATABASE_URL") or os.getenv("DATABASE_URL") or "sqlite:///./fyds.db"
)
IS_SQLITE = DATABASE_URL.startswith("sqlite")

_connect_args = {"check_same_thread": False} if IS_SQLITE else {}
_pool = {} if IS_SQLITE else {"pool_size": 5, "max_overflow": 5, "pool_recycle": 300}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True, **_pool)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
