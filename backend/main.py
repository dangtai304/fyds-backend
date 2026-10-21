"""FYDS API: REST /api/* + WebSocket /ws. Giao diện nằm ở repo fyds-frontend."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .users_api import router as users_router
from .database import Base, SessionLocal, engine
from .state import build_state
from .ws_manager import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Danh sách origin của frontend, phân tách bằng dấu phẩy. "*" = cho phép tất cả (chỉ dùng khi thử nghiệm).
CORS_ORIGINS = [o.strip() for o in os.getenv(
    "FYDS_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173"
).split(",") if o.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("FYDS_AUTO_CREATE", "0") == "1":
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title="FYDS API - Điều phối xe & xe nâng", version="1.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,  # token đi qua header Authorization, không dùng cookie
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(router)
app.include_router(users_router)


@app.get("/", include_in_schema=False)
def root():
    return {"service": "fyds-backend", "docs": "/docs", "websocket": "/ws"}


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "ws_clients": len(manager.active)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        with SessionLocal() as db:
            await manager.send(ws, {"type": "state", "state": build_state(db), "events": []})
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await manager.send(ws, {"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
