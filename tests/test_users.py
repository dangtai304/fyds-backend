"""Kiểm thử quản lý người dùng & phân quyền theo vai trò."""
import os
import tempfile

if "FYDS_DATABASE_URL" not in os.environ:
    os.environ["FYDS_DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ.setdefault("FYDS_SECRET", "test-secret")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.database import Base, engine  # noqa: E402
from backend.main import app  # noqa: E402
from backend.seed import seed  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed()
    with TestClient(app) as c:
        yield c


def login(c, user, pw):
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_only_admin_manages_users(client):
    admin = login(client, "admin", "admin123")
    for user, pw in [("dieuphoi", "dieuphoi123"), ("baove1", "baove123"), ("xenang1", "xenang123")]:
        h = login(client, user, pw)
        assert client.get("/api/users", headers=h).status_code == 403
        assert client.post("/api/users", headers=h, json={
            "username": "hack", "full_name": "Hack", "role": "ADMIN", "password": "123456"}).status_code == 403
    users = client.get("/api/users", headers=admin).json()
    assert {u["role"] for u in users} == {"ADMIN", "DISPATCHER", "GATE_SECURITY", "FORKLIFT_OPERATOR"}
    assert "password_hash" not in users[0]


def test_create_user_for_each_role_and_login(client):
    admin = login(client, "admin", "admin123")
    for i, role in enumerate(["FORKLIFT_OPERATOR", "GATE_SECURITY", "DISPATCHER", "ADMIN"]):
        r = client.post("/api/users", headers=admin, json={
            "username": f"  New.User{i} ", "full_name": f"Người dùng {i}", "role": role, "shift": "CA2", "password": "matkhau1"})
        assert r.status_code == 201, r.text
        assert r.json()["username"] == f"new.user{i}"
        me = client.get("/api/me", headers=login(client, f"new.user{i}", "matkhau1")).json()
        assert me["role"] == role

    dup = client.post("/api/users", headers=admin, json={"username": "baove1", "full_name": "Trùng", "role": "GATE_SECURITY", "password": "123456"})
    assert dup.status_code == 409
    bad = client.post("/api/users", headers=admin, json={"username": "có dấu", "full_name": "Sai", "role": "GATE_SECURITY", "password": "123456"})
    assert bad.status_code == 422
    short = client.post("/api/users", headers=admin, json={"username": "abc", "full_name": "Ngắn", "role": "GATE_SECURITY", "password": "123"})
    assert short.status_code == 422


def test_deactivate_revokes_sessions_and_blocks_login(client):
    admin = login(client, "admin", "admin123")
    gate = login(client, "baove1", "baove123")
    assert client.get("/api/me", headers=gate).status_code == 200
    uid = next(u["id"] for u in client.get("/api/users", headers=admin).json() if u["username"] == "baove1")

    r = client.post(f"/api/users/{uid}", headers=admin, json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert client.get("/api/me", headers=gate).status_code == 401           # phiên cũ bị thu hồi ngay
    locked = client.post("/api/auth/login", json={"username": "baove1", "password": "baove123"})
    assert locked.status_code == 403 and "khóa" in locked.json()["detail"]

    client.post(f"/api/users/{uid}", headers=admin, json={"is_active": True})
    assert client.get("/api/me", headers=login(client, "baove1", "baove123")).status_code == 200


def test_role_change_revokes_and_changes_permissions(client):
    admin = login(client, "admin", "admin123")
    op = login(client, "xenang1", "xenang123")
    client.post("/api/forklifts/FL-01/claim", headers=op)
    users = {u["username"]: u for u in client.get("/api/users", headers=admin).json()}

    client.post(f"/api/users/{users['xenang1']['id']}", headers=admin, json={"role": "GATE_SECURITY"})
    assert client.get("/api/me", headers=op).status_code == 401
    fl = next(f for f in client.get("/api/state").json()["forklifts"] if f["code"] == "FL-01")
    assert fl["operator_id"] is None                                         # nhả xe nâng
    new = login(client, "xenang1", "xenang123")
    assert client.post("/api/trips/checkin", headers=new, json={
        "license_plate": "ROLE-01", "gate_in": "GATE_1", "vehicle_type": "BOX_TRUCK", "cargo_type": "PALLET"}).status_code == 200


def test_admin_cannot_lock_self_or_remove_last_admin(client):
    admin = login(client, "admin", "admin123")
    me = client.get("/api/me", headers=admin).json()
    assert client.post(f"/api/users/{me['id']}", headers=admin, json={"is_active": False}).status_code == 409
    assert client.post(f"/api/users/{me['id']}", headers=admin, json={"role": "DISPATCHER"}).status_code == 409

    r = client.post("/api/users", headers=admin, json={"username": "admin2", "full_name": "Admin Hai", "role": "ADMIN", "password": "admin234"})
    admin2 = login(client, "admin2", "admin234")
    assert client.post(f"/api/users/{me['id']}", headers=admin2, json={"is_active": False}).status_code == 200
    # admin2 giờ là admin hoạt động cuối cùng -> không ai hạ được
    assert client.post(f"/api/users/{r.json()['id']}", headers=admin2, json={"role": "GATE_SECURITY"}).status_code == 409


def test_password_reset_and_self_change(client):
    admin = login(client, "admin", "admin123")
    op = login(client, "xenang2", "xenang123")
    uid = next(u["id"] for u in client.get("/api/users", headers=admin).json() if u["username"] == "xenang2")

    assert client.post(f"/api/users/{uid}/password", headers=admin, json={"password": "moi12345"}).status_code == 200
    assert client.get("/api/me", headers=op).status_code == 401
    assert client.post("/api/auth/login", json={"username": "xenang2", "password": "xenang123"}).status_code == 401
    op = login(client, "xenang2", "moi12345")

    wrong = client.post("/api/me/password", headers=op, json={"current_password": "sai", "new_password": "abc12345"})
    assert wrong.status_code == 400
    r = client.post("/api/me/password", headers=op, json={"current_password": "moi12345", "new_password": "abc12345"})
    assert r.status_code == 200
    assert client.get("/api/me", headers=op).status_code == 401              # token cũ hết hiệu lực
    assert client.get("/api/me", headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 200

    logs = client.get("/api/logs", headers=admin).json()
    assert {"USER_PASSWORD_RESET", "USER_PASSWORD_CHANGE"} <= {l["action"] for l in logs}
