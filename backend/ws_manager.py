"""Quản lý kết nối WebSocket và phát trạng thái tới mọi màn hình."""
from __future__ import annotations

import json
import logging

from fastapi import WebSocket

log = logging.getLogger("fyds.ws")


class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)
        log.info("WS connected (%d clients)", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)
        log.info("WS disconnected (%d clients)", len(self.active))

    async def send(self, ws: WebSocket, message: dict) -> None:
        await ws.send_text(json.dumps(message, ensure_ascii=False))

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message, ensure_ascii=False)
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
