# fyds-backend

API + WebSocket cho **FYDS – Hệ thống điều phối xe tải & xe nâng** (nhà máy 2 cổng, bốc dỡ bằng xe nâng tại bay mặt sân).
Gọi xe **đúng thứ tự đến cổng (FIFO)**, mọi lần vượt hàng **bắt buộc có lý do** và được ghi nhật ký, đo **TAT** từ check-in đến check-out.

Giao diện (TV phòng chờ, tablet xe nâng, cổng bảo vệ, dashboard điều phối) nằm ở repo **[fyds-frontend](https://github.com/dangtai304/fyds-frontend)**.

**Stack:** Python 3.11+ · FastAPI · WebSocket · SQLAlchemy 2 · Alembic · SQLite (pilot) / PostgreSQL

## Chạy

```bash
./run.sh --demo        # Linux/macOS: tạo .venv, cài thư viện, migrate, seed, chạy :8000
run.bat --demo         # Windows
docker compose up -d --build
```

- API docs (Swagger): `http://localhost:8000/docs`
- WebSocket: `ws://localhost:8000/ws`
- Bỏ `--demo` khi chạy thật (không tạo 6 xe mẫu).

Tài khoản mẫu: `dieuphoi/dieuphoi123` (Control Tower) · `baove1/baove123` (bảo vệ) · `xenang1..3/xenang123` (lái xe nâng) · `admin/admin123`. **Đổi mật khẩu trước khi chạy thật.**

## Vai trò & phân quyền

| Vai trò (`role`) | Tên hiển thị | Được làm |
|---|---|---|
| `FORKLIFT_OPERATOR` | Lái xe nâng | Nhận xe nâng, bắt đầu / hoàn thành bốc dỡ **chỉ trên lệnh của xe nâng mình**, tạm dừng / báo sự cố |
| `GATE_SECURITY` | Bảo vệ | Check-in, check-out |
| `DISPATCHER` | Control Tower | Mọi thao tác điều phối: override ưu tiên, gọi thủ công, đổi bay, hủy, bảo trì bay, tạm dừng xe nâng, xem nhật ký |
| `ADMIN` | Admin | Toàn quyền + quản lý người dùng |

Khóa tài khoản, đổi vai trò, đặt lại mật khẩu hay tự đổi mật khẩu đều tăng `users.token_version` → **mọi phiên cũ của người đó mất hiệu lực ngay** (frontend tự về trang đăng nhập trong ≤ 60 giây). Không thể tự khóa / tự đổi vai trò của mình, và luôn phải còn ít nhất 1 Admin đang hoạt động. Mọi thay đổi tài khoản ghi vào nhật ký (`USER_CREATE`, `USER_UPDATE`, `USER_PASSWORD_RESET`, `USER_PASSWORD_CHANGE`).

## Cấu hình (biến môi trường)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `FYDS_DATABASE_URL` | `sqlite:///./fyds.db` | PostgreSQL: `postgresql+psycopg://user:pass@host/fyds` (thêm `psycopg[binary]`) |
| `FYDS_CORS_ORIGINS` | `http://localhost:5173,…:4173` | Origin của frontend được phép gọi API trực tiếp. Không cần khi frontend proxy qua nginx (cùng origin) |
| `FYDS_SECRET` | tự sinh vào `.fyds_secret` | Khóa ký token đăng nhập |
| `FYDS_TOKEN_TTL_HOURS` | `14` | Hạn phiên đăng nhập |
| `FYDS_REQUIRE_OPERATOR` | `0` | `1` = chỉ giao lệnh cho xe nâng có tài xế đăng nhập tablet |
| `TZ` | `Asia/Ho_Chi_Minh` | Múi giờ (thống kê "hôm nay") |

## API

| Method | Path | Vai trò | Mô tả |
|---|---|---|---|
| POST | `/api/auth/login` | — | `{username, password}` → `{token, user}` (Bearer token) |
| GET | `/api/me` | đã đăng nhập | Thông tin người dùng |
| GET | `/api/state` | — | Snapshot: bays, forklifts, queue, active, awaiting_checkout, stats |
| GET | `/api/logs?limit=&trip_id=` | điều phối | Nhật ký kiểm toán |
| POST | `/api/trips/checkin` | bảo vệ | Tạo phiếu `#IN-xxx` / `#OUT-xxx`, tự gọi xe nếu có bay trống |
| POST | `/api/trips/{id}/checkout` | bảo vệ | `{gate_out}` → đóng TAT |
| POST | `/api/forklifts/{code}/claim` | lái xe nâng | Nhận xe nâng đầu ca |
| POST | `/api/trips/{id}/start` | lái xe nâng | CALLED → LOADING |
| POST | `/api/trips/{id}/complete` | lái xe nâng | LOADING → COMPLETED, giải phóng bay, gọi xe tiếp |
| POST | `/api/forklifts/{code}/pause` · `/resume` | lái xe nâng | Tạm dừng / báo sự cố (`{reason}`) |
| POST | `/api/trips/{id}/priority` | điều phối | `{score, reason}` — Ưu tiên–Override |
| POST | `/api/trips/{id}/reassign` | điều phối | `{bay_id, reason}` — gọi thủ công / đổi bay |
| POST | `/api/trips/{id}/cancel` | điều phối | `{reason}` |
| POST | `/api/bays/{id}/status` | điều phối | `{status: EMPTY\|MAINTENANCE, reason}` |
| GET | `/api/users` | admin | Danh sách người dùng (không trả mật khẩu) |
| POST | `/api/users` | admin | `{username, full_name, role, shift, password}` tạo tài khoản |
| POST | `/api/users/{id}` | admin | `{full_name?, role?, shift?, is_active?}` sửa / khóa / mở khóa |
| POST | `/api/users/{id}/password` | admin | `{password}` đặt lại mật khẩu |
| POST | `/api/me/password` | đã đăng nhập | `{current_password, new_password}` → token mới |

`ADMIN` được làm mọi thao tác. Lý do bắt buộc ≥ 5 ký tự (thiếu → 422).

**WebSocket `/ws`:** khi kết nối nhận ngay `{type:"state", state, events:[]}`; sau mỗi thao tác ghi, mọi client nhận snapshot mới kèm sự kiện: `TRIP_CALLED` (TV phát chuông + đọc loa), `TRIP_CHECKED_IN`, `TRIP_STARTED`, `TRIP_COMPLETED`, `TRIP_CHECKED_OUT`, `TRIP_CANCELLED`, `PRIORITY_OVERRIDE`, `FORKLIFT_PAUSED`, `FORKLIFT_RESUMED`, `BAY_STATUS`. Client gửi `"ping"` để giữ kết nối.

## Thuật toán gọi xe

`backend/dispatcher.py → try_dispatch` chạy mỗi khi bay trống hoặc xe nâng rảnh: duyệt hàng đợi theo `priority_score DESC, checked_in_at ASC`, lấy xe đầu tiên có bay tương thích đang trống, gán xe nâng rảnh lâu nhất (ưu tiên xe có tài xế), lặp đến khi hết tài nguyên.

| Loại xe | Bay ưu tiên | Bay dự phòng |
|---|---|---|
| Xe bạt hông | SIDE_LOADING | REAR_LOADING |
| Xe thùng kín | REAR_LOADING | CONTAINER_RAMP |
| Container | CONTAINER_RAMP | — |

Sửa trong `COMPATIBILITY`. **Vi phạm FIFO** = lượt gọi vượt xe đến trước (cùng/hơn mức ưu tiên, vào được bay đó) mà không có lý do; lượt vượt có lý do được đếm riêng là override.

## Cấu trúc

```
backend/    main.py (app, CORS, /ws) · api.py (REST điều phối) · users_api.py (quản lý người dùng)
            dispatcher.py (FIFO, chuyển trạng thái)
            models.py · state.py (snapshot + KPI) · ws_manager.py · auth.py · seed.py
migrations/ Alembic
tests/      pytest end-to-end (HTTP + WebSocket)
```

## Kiểm thử

```bash
pip install -r requirements.txt && pytest -q
```

## Giới hạn

- Khóa điều phối nằm trong 1 tiến trình → chạy **1 worker uvicorn**. Nhiều worker cần `SELECT … FOR UPDATE` (PostgreSQL) + pub/sub (Redis) cho WebSocket.
- Chưa có API quản trị danh mục bay/xe nâng, báo cáo lịch sử, xuất Excel.
