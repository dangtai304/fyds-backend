@echo off
REM Khoi chay FYDS tren Windows:  run.bat   hoac   run.bat --demo
cd /d %~dp0
if not exist .venv (
  echo ^>^> Tao moi truong Python (.venv)
  py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
alembic upgrade head
python -m backend.seed %*
echo.
echo FYDS dang chay: http://localhost:8000
uvicorn backend.main:app --host 0.0.0.0 --port 8000
