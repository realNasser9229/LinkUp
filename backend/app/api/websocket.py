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
        connections = self.rooms.get(room_id)
        if not connections:
            return
        if websocket in connections:
            connections.remove(websocket)
        if not connections:
            self.rooms.pop(room_id, None)

    async def broadcast(self, room_id: str, message: str) -> None:
        for ws in list(self.rooms.get(room_id, [])):
            await ws.send_text(message)


manager = ConnectionManager()


@router.websocket("/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str):
    await manager.connect(room_id, websocket)
    try:
        while True:
            message = await websocket.receive_text()
            await manager.broadcast(room_id, message)
    except WebSocketDisconnect:
        manager.disconnect(room_id, websocket)
