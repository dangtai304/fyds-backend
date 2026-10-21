"""Kết nối CSDL. Mặc định SQLite cho pilot; đặt FYDS_DATABASE_URL để dùng PostgreSQL.

Ví dụ PostgreSQL:  postgresql+psycopg://fyds:secret@localhost:5432/fyds
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("FYDS_DATABASE_URL", "sqlite:///./fyds.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
