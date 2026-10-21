FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=Asia/Ho_Chi_Minh
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && python -m backend.seed ${SEED_ARGS:-} && uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
