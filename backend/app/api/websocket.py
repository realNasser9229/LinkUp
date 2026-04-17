from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

class ConnectionManager:
    def __init__(self):
        self.rooms = {}

    async def connect(self, room_id: str, ws: WebSocket):
        await ws.accept()
        self.rooms.setdefault(room_id, []).append(ws)

    def disconnect(self, room_id: str, ws: WebSocket):
        if room_id in self.rooms and ws in self.rooms[room_id]:
            self.rooms[room_id].remove(ws)

    async def broadcast(self, room_id: str, payload: str):
        for ws in self.rooms.get(room_id, []):
            await ws.send_text(payload)

manager = ConnectionManager()

@router.websocket("/{room_id}")
async def room_socket(ws: WebSocket, room_id: str):
    await manager.connect(room_id, ws)
    try:
        while True:
            msg = await ws.receive_text()
            await manager.broadcast(room_id, msg)
    except WebSocketDisconnect:
        manager.disconnect(room_id, ws)
