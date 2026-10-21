"""Kiểm thử luồng nghiệp vụ end-to-end qua HTTP + WebSocket.

Chạy:  pytest -q
"""

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


def checkin(c, h, plate, vehicle="CURTAIN_SIDE", gate="GATE_1"):
    r = c.post("/api/trips/checkin", headers=h, json={
        "license_plate": plate, "gate_in": gate, "vehicle_type": vehicle,
        "cargo_type": "PALLET", "movement_type": "INBOUND", "driver_phone": "0900000000",
    })
    assert r.status_code == 200, r.text
    return r.json()


def recv_state(ws):
    while True:
        msg = ws.receive_json()
        if msg["type"] == "state":
            return msg


def test_complete_on_tablet_pushes_next_truck_to_tv(client):
    gate = login(client, "baove1", "baove123")
    op1 = login(client, "xenang1", "xenang123")
    client.post("/api/forklifts/FL-01/claim", headers=op1)

    a = checkin(client, gate, "51C-111.11", "CURTAIN_SIDE")   # -> BAY-01 (dỡ hông)
    b = checkin(client, gate, "61H-222.22", "BOX_TRUCK")      # -> BAY-02 (bốc đuôi)
    c = checkin(client, gate, "50H-333.33", "CONTAINER")      # -> BAY-03 (ramp)
    d = checkin(client, gate, "51D-444.44", "CURTAIN_SIDE")   # chờ: hết bay

    assert a["trip"]["queue_status"] == "CALLED" and a["trip"]["bay_code"] == "BAY-01"
    assert b["trip"]["bay_code"] == "BAY-02"
    assert c["trip"]["bay_code"] == "BAY-03"
    assert d["trip"]["queue_status"] == "WAITING" and d["queue_position"] == 1
    assert a["trip"]["forklift_code"] == "FL-01"  # xe nâng có tài xế được ưu tiên

    with client.websocket_connect("/ws") as tv:     # màn hình TV
        initial = recv_state(tv)
        assert [t["license_plate"] for t in initial["state"]["queue"]] == ["51D-444.44"]

        r = client.post(f"/api/trips/{a['trip']['id']}/start", headers=op1)
        assert r.json()["queue_status"] == "LOADING"
        recv_state(tv)

        r = client.post(f"/api/trips/{a['trip']['id']}/complete", headers=op1)
        assert r.status_code == 200
        assert [t["license_plate"] for t in r.json()["next_called"]] == ["51D-444.44"]

        msg = recv_state(tv)   # TV nhận ngay sự kiện gọi xe tiếp -> phát chuông + TTS
        called = [e for e in msg["events"] if e["kind"] == "TRIP_CALLED"]
        assert len(called) == 1
        assert called[0]["trip"]["license_plate"] == "51D-444.44"
        assert called[0]["trip"]["bay_code"] == "BAY-01"
        assert any(e["kind"] == "TRIP_COMPLETED" for e in msg["events"])
        assert msg["state"]["queue"] == []


def test_fifo_order_and_fallback_bay(client):
    gate = login(client, "baove1", "baove123")
    disp = login(client, "dieuphoi", "dieuphoi123")
    # 3 xe bạt hông: xe 1 vào BAY-01 (hông), xe 2 dùng bay dự phòng BAY-02 (đuôi), xe 3 chờ
    t1 = checkin(client, gate, "AAA-001")["trip"]
    t2 = checkin(client, gate, "AAA-002", gate="GATE_2")["trip"]
    t3 = checkin(client, gate, "AAA-003")["trip"]
    t4 = checkin(client, gate, "AAA-004", gate="GATE_2")["trip"]
    assert (t1["bay_code"], t2["bay_code"]) == ("BAY-01", "BAY-02")
    assert t3["queue_status"] == "WAITING" and t4["queue_status"] == "WAITING"

    # BAY-02 trống -> xe 3 (đến trước xe 4) được gọi, đúng FIFO
    client.post(f"/api/trips/{t2['id']}/start", headers=disp)
    nxt = client.post(f"/api/trips/{t2['id']}/complete", headers=disp).json()["next_called"]
    assert [t["license_plate"] for t in nxt] == ["AAA-003"]

    stats = client.get("/api/state").json()["stats"]
    assert stats["fifo_violation_rate"] == 0.0


def test_override_requires_reason_and_is_logged(client):
    gate = login(client, "baove1", "baove123")
    disp = login(client, "dieuphoi", "dieuphoi123")
    for i in range(3):
        checkin(client, gate, f"FILL-{i}", ["CURTAIN_SIDE", "BOX_TRUCK", "CONTAINER"][i])
    w1 = checkin(client, gate, "W-001")["trip"]
    w2 = checkin(client, gate, "W-002")["trip"]

    r = client.post(f"/api/trips/{w2['id']}/priority", headers=disp, json={"score": 80, "reason": "ok"})
    assert r.status_code == 422  # lý do quá ngắn

    gate_try = client.post(f"/api/trips/{w2['id']}/priority", headers=gate, json={"score": 80, "reason": "Hàng gấp line 2"})
    assert gate_try.status_code == 403  # bảo vệ không được override

    r = client.post(f"/api/trips/{w2['id']}/priority", headers=disp, json={"score": 80, "reason": "Hàng gấp line 2"})
    assert r.status_code == 200
    queue = client.get("/api/state").json()["queue"]
    assert [t["license_plate"] for t in queue] == ["W-002", "W-001"]

    logs = client.get(f"/api/logs?trip_id={w2['id']}", headers=disp).json()
    assert any(l["action"] == "PRIORITY_OVERRIDE" and "Hàng gấp line 2" in l["note"] for l in logs)

    # Override có lý do không bị tính là vi phạm FIFO
    st = client.get("/api/state").json()
    fill0 = next(t for t in st["active"] if t["license_plate"] == "FILL-0")
    client.post(f"/api/trips/{fill0['id']}/start", headers=disp)
    nxt = client.post(f"/api/trips/{fill0['id']}/complete", headers=disp).json()["next_called"]
    assert nxt[0]["license_plate"] == "W-002"
    stats = client.get("/api/state").json()["stats"]
    assert stats["fifo_violation_rate"] == 0.0 and stats["overrides"] >= 1


def test_checkout_closes_tat_and_blocks_duplicate(client):
    gate = login(client, "baove1", "baove123")
    disp = login(client, "dieuphoi", "dieuphoi123")
    t = checkin(client, gate, "TAT-001")["trip"]

    dup = client.post("/api/trips/checkin", headers=gate, json={
        "license_plate": "tat-001", "gate_in": "GATE_2", "vehicle_type": "CURTAIN_SIDE", "cargo_type": "DRUM"})
    assert dup.status_code == 409  # cùng biển số còn trong sân

    early = client.post(f"/api/trips/{t['id']}/checkout", headers=gate, json={"gate_out": "GATE_2"})
    assert early.status_code == 409  # chưa bốc xong

    client.post(f"/api/trips/{t['id']}/start", headers=disp)
    client.post(f"/api/trips/{t['id']}/complete", headers=disp)
    r = client.post(f"/api/trips/{t['id']}/checkout", headers=gate, json={"gate_out": "GATE_2"})
    assert r.status_code == 200
    assert r.json()["trip"]["queue_status"] == "CHECKED_OUT" and r.json()["tat_min"] >= 0
    assert client.get("/api/state").json()["stats"]["checked_out"] == 1


def test_operator_cannot_touch_other_forklift_and_pause_blocks_dispatch(client):
    gate = login(client, "baove1", "baove123")
    op1 = login(client, "xenang1", "xenang123")
    op2 = login(client, "xenang2", "xenang123")
    client.post("/api/forklifts/FL-01/claim", headers=op1)
    client.post("/api/forklifts/FL-02/claim", headers=op2)

    t = checkin(client, gate, "OP-001")["trip"]
    assert t["forklift_code"] in ("FL-01", "FL-02")
    other = op2 if t["forklift_code"] == "FL-01" else op1
    assert client.post(f"/api/trips/{t['id']}/start", headers=other).status_code == 403

    # Tạm dừng tất cả xe nâng rảnh -> xe mới phải chờ
    disp = login(client, "dieuphoi", "dieuphoi123")
    for f in client.get("/api/state").json()["forklifts"]:
        if f["status"] == "IDLE":
            assert client.post(f"/api/forklifts/{f['code']}/pause", headers=disp, json={"reason": "Hết nhiên liệu"}).status_code == 200
    w = checkin(client, gate, "OP-002", "BOX_TRUCK")
    assert w["trip"]["queue_status"] == "WAITING"
    r = client.post("/api/forklifts/FL-03/resume", headers=disp)
    assert r.status_code == 200
    st = client.get("/api/state").json()
    assert any(a["license_plate"] == "OP-002" for a in st["active"])


def test_health_and_cors_preflight(client):
    assert client.get("/healthz").json()["ok"] is True
    r = client.options("/api/trips/checkin", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    blocked = client.options("/api/state", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in blocked.headers
