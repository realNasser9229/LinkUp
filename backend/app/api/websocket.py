from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, List[WebSocket]] = {}

    async def connect(self, room_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.rooms.setdefault(room_id, []).append(websocket)

    def disconnect(self, room_id: str, websocket: WebSocket) -> None:
        sockets = self.rooms.get(room_id)
        if not sockets:
            return
        if websocket in sockets:
            sockets.remove(websocket)
        if not sockets:
            self.rooms.pop(room_id, None)

    async def broadcast_json(self, room_id: str, payload: dict) -> None:
        for ws in list(self.rooms.get(room_id, [])):
            try:
                await ws.send_json(payload)
            except Exception:
                pass


manager = ConnectionManager()


@router.websocket("/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str):
    await manager.connect(room_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            await manager.broadcast_json(room_id, data)
    except WebSocketDisconnect:
        manager.disconnect(room_id, websocket)
