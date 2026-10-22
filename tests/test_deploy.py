"""Kiểm thử các thiết lập khi triển khai công khai (Render + Vercel)."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.database import Base, engine, normalize_url
from backend.main import app
from backend.seed import seed


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed()
    with TestClient(app) as c:
        yield c


def test_render_database_url_is_normalized():
    assert normalize_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///./x.db") == "sqlite:///./x.db"


@pytest.mark.parametrize("origin,allowed", [
    ("https://fyds-frontend.vercel.app", True),
    ("https://fyds-frontend-git-main-dangtai304.vercel.app", True),   # bản preview
    ("https://fyds-frontend-abc123xyz-team.vercel.app", True),
    ("http://localhost:5173", True),
    ("https://evil.vercel.app", False),
    ("https://fyds-frontend.vercel.app.evil.com", False),
])
def test_cors_for_vercel_origins(client, origin, allowed):
    r = client.options("/api/auth/login", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert (r.headers.get("access-control-allow-origin") == origin) is allowed


def test_vercel_production_allowed_without_any_env(monkeypatch):
    """Render service tạo tay, không có biến CORS: bản Vercel production vẫn phải gọi được."""
    import importlib
    import backend.main as m
    monkeypatch.delenv("FYDS_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FYDS_CORS_ORIGIN_REGEX", raising=False)
    m2 = importlib.reload(m)
    try:
        assert m2.origin_allowed("https://fyds-frontend.vercel.app")
        assert m2.origin_allowed("https://fyds-frontend-git-main-x.vercel.app")
        assert not m2.origin_allowed("https://evil.vercel.app")
        with TestClient(m2.app) as c:
            r = c.options("/api/auth/login", headers={"Origin": "https://fyds-frontend.vercel.app",
                          "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
            assert r.headers.get("access-control-allow-origin") == "https://fyds-frontend.vercel.app"
    finally:
        importlib.reload(m)


def test_websocket_rejects_foreign_origin(client):
    with client.websocket_connect("/ws", headers={"Origin": "https://fyds-frontend.vercel.app"}) as ws:
        assert ws.receive_json()["type"] == "state"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_public_state_hides_driver_phone(client):
    h = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"username": "baove1", "password": "baove123"}).json()["token"]}
    client.post("/api/trips/checkin", headers=h, json={
        "license_plate": "PRIV-001", "gate_in": "GATE_1", "vehicle_type": "CURTAIN_SIDE",
        "cargo_type": "PALLET", "driver_phone": "0909999999"})
    assert "0909999999" not in client.get("/api/state").text


def test_login_bruteforce_is_throttled(client):
    for _ in range(8):
        assert client.post("/api/auth/login", json={"username": "dieuphoi", "password": "sai"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "dieuphoi", "password": "dieuphoi123"})
    assert r.status_code == 429 and "thử lại sau" in r.json()["detail"]
    # tài khoản khác không bị ảnh hưởng
    assert client.post("/api/auth/login", json={"username": "baove2", "password": "baove123"}).status_code == 200


def test_login_throttle_cannot_be_bypassed_by_spoofing_ip(client):
    for i in range(20):
        r = client.post("/api/auth/login", headers={"X-Forwarded-For": f"10.0.0.{i}"},
                        json={"username": "xenang3", "password": "sai"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", headers={"X-Forwarded-For": "10.9.9.9"},
                    json={"username": "xenang3", "password": "xenang123"})
    assert r.status_code == 429


def test_production_seed_creates_only_admin(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("FYDS_ADMIN_PASSWORD", "MatKhauManh@2026")
    seed()
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "dieuphoi", "password": "dieuphoi123"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "admin", "password": "MatKhauManh@2026"}).status_code == 200
        assert len(c.get("/api/state").json()["bays"]) == 3
