"""Cấu hình chung cho test — chạy trước khi import app.

Mặc định dùng SQLite tạm. Chạy trên Postgres:
    FYDS_TEST_DATABASE_URL=postgresql://user@host:5432/fyds_test pytest -q
"""
import os
import tempfile

os.environ["FYDS_DATABASE_URL"] = os.environ.get("FYDS_TEST_DATABASE_URL") or f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["FYDS_SECRET"] = "test-secret"
os.environ["FYDS_CORS_ORIGINS"] = "http://localhost:5173"
os.environ["FYDS_CORS_ORIGIN_REGEX"] = r"^https://fyds-frontend(-[a-z0-9-]+)?\.vercel\.app$"
os.environ.pop("FYDS_ADMIN_PASSWORD", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    from backend import api
    api._login_fails.clear()
    yield
    api._login_fails.clear()
