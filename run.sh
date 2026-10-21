#!/usr/bin/env bash
# Khởi chạy FYDS bằng 1 lệnh:   ./run.sh          (dữ liệu nền)
#                               ./run.sh --demo   (thêm 6 xe mẫu để chạy thử)
set -euo pipefail
cd "$(dirname "$0")"

export TZ="${TZ:-Asia/Ho_Chi_Minh}"
PORT="${PORT:-8000}"

if [ ! -d .venv ]; then
  echo ">> Tạo môi trường Python (.venv)"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo ">> Cập nhật CSDL (Alembic)"
alembic upgrade head
python -m backend.seed "$@"

IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
echo ""
echo "=============================================================="
echo " FYDS đang chạy:  http://localhost:${PORT}"
[ -n "${IP}" ] && echo " Trong mạng LAN:  http://${IP}:${PORT}  (mở trên TV/tablet)"
echo "=============================================================="
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT}"
