ffrom __future__ import annotations

import json
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# --- Backend Configuration & Models ---

app = FastAPI(title="LinkUp - Nasoro Edition")

class Settings:
    def __init__(self) -> None:
        # Configuration for cross-origin requests
        origins = os.getenv("CORS_ORIGINS", "*").strip()
        self.cors_origins = [o.strip() for o in origins.split(",") if o.strip()] if origins != "*" else ["*"]

settings = Settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class RoomState:
    """Manages the state of chat rooms, including members, history, pins, and reactions."""
    def __init__(self) -> None:
        self.sockets: Dict[str, Dict[str, WebSocket]] = {}
        self.members: Dict[str, Dict[str, dict]] = {}
        self.history: Dict[str, List[dict]] = {}
        self.pins: Dict[str, List[dict]] = {}
        self.reactions: Dict[str, Dict[str, Dict[str, set]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))

    def ensure(self, room: str) -> None:
        self.sockets.setdefault(room, {})
        self.members.setdefault(room, {})
        self.history.setdefault(room, [])
        self.pins.setdefault(room, [])

    def current_users(self, room: str) -> List[dict]:
        self.ensure(room)
        users = []
        for client_id, info in self.members[room].items():
            if client_id not in self.sockets[room]:
                continue
            users.append({
                "client_id": client_id,
                "name": info["name"],
                "joined_at": info["joined_at"],
            })
        users.sort(key=lambda u: (u["joined_at"], u["name"].lower()))
        return users

    def register(self, room: str, client_id: str, name: str, ws: WebSocket) -> bool:
        self.ensure(room)
        is_new = client_id not in self.members[room]
        self.members[room][client_id] = {"name": name, "joined_at": int(time.time() * 1000)}
        self.sockets[room][client_id] = ws
        return is_new

    def remove(self, room: str, client_id: str) -> None:
        self.ensure(room)
        self.sockets[room].pop(client_id, None)
        self.members[room].pop(client_id, None)

    def append_history(self, room: str, payload: dict) -> None:
        self.ensure(room)
        self.history[room].append(payload)
        if len(self.history[room]) > 500: # Increased history buffer
            self.history[room] = self.history[room][-500:]

    def get_message_by_id(self, room: str, msg_id: str) -> Optional[dict]:
        self.ensure(room)
        for msg in self.history[room]:
            if msg.get("msg_id") == msg_id:
                return msg
        return None

    def build_pins(self, room: str) -> List[dict]:
        self.ensure(room)
        return self.pins[room][-50:]

    def toggle_pin(self, room: str, msg_id: str) -> Optional[dict]:
        self.ensure(room)
        msg = self.get_message_by_id(room, msg_id)
        if not msg: return None
        existing = next((p for p in self.pins[room] if p.get("msg_id") == msg_id), None)
        if existing:
            self.pins[room] = [p for p in self.pins[room] if p.get("msg_id") != msg_id]
            return {"action": "unpinned", "message": msg}
        pin_payload = {
            "msg_id": msg["msg_id"],
            "name": msg.get("name", "Guest"),
            "text": msg.get("text", ""),
            "client_id": msg.get("client_id", ""),
            "time": msg.get("time", int(time.time() * 1000)),
        }
        self.pins[room].append(pin_payload)
        return {"action": "pinned", "message": msg}

    def toggle_reaction(self, room: str, msg_id: str, emoji: str, client_id: str) -> Optional[dict]:
        self.ensure(room)
        bucket = self.reactions[room][msg_id][emoji]
        if client_id in bucket: bucket.remove(client_id)
        else: bucket.add(client_id)
        counts = {e: len(users) for e, users in self.reactions[room][msg_id].items()}
        return {"msg_id": msg_id, "reactions": counts}

    async def broadcast(self, room: str, payload: dict) -> None:
        self.ensure(room)
        dead = []
        for client_id, ws in self.sockets[room].items():
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(client_id)
        for client_id in dead:
            self.remove(room, client_id)

rooms = RoomState()

# --- WebSocket Routing ---

ws_router = APIRouter()

@ws_router.websocket("/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str):
    await websocket.accept()
    client_id = "guest_" + uuid4().hex[:10]
    display_name = "Guest"

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw) if raw else {}
            kind = data.get("kind", "message")
            
            # Update user identity if provided
            if "name" in data: display_name = str(data["name"])[:24].strip() or "Guest"
            if "client_id" in data: client_id = str(data["client_id"])

            if kind == "hello":
                rooms.register(room_id, client_id, display_name, websocket)
                await rooms.broadcast(room_id, {"kind": "presence", "users": rooms.current_users(room_id)})
                await websocket.send_json({
                    "kind": "history", 
                    "messages": rooms.history.get(room_id, [])[-150:],
                    "pins": rooms.build_pins(room_id)
                })
                continue

            if kind == "rename":
                rooms.register(room_id, client_id, display_name, websocket)
                await rooms.broadcast(room_id, {"kind": "presence", "users": rooms.current_users(room_id)})
                continue

            if kind == "message":
                text = str(data.get("text", "")).strip()
                if not text: continue
                msg_payload = {
                    "kind": "message",
                    "msg_id": "msg_" + uuid4().hex,
                    "client_id": client_id,
                    "name": display_name,
                    "text": text,
                    "time": int(time.time() * 1000),
                    "reactions": {}
                }
                rooms.append_history(room_id, msg_payload)
                await rooms.broadcast(room_id, msg_payload)

    except WebSocketDisconnect:
        rooms.remove(room_id, client_id)
        await rooms.broadcast(room_id, {"kind": "presence", "users": rooms.current_users(room_id)})
    except Exception:
        rooms.remove(room_id, client_id)

app.include_router(ws_router, prefix="/ws")

# --- Frontend HTML/CSS/JS ---

def page_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Nasoro LinkUp</title>
    <style>
        :root {
            /* Material 3 & Glassmorphism Tokens */
            --bg: #0f1115;
            --surface: rgba(28, 27, 31, 0.7);
            --glass: rgba(255, 255, 255, 0.08);
            --glass-border: rgba(255, 255, 255, 0.12);
            --primary: #d0bcff;
            --on-primary: #381e72;
            --secondary: #ccc2dc;
            --text: #e6e1e5;
            --text-muted: #938f99;
            --blur: 24px;
            --radius-m3: 28px;
        }

        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        
        body {
            margin: 0;
            background: var(--bg);
            color: var(--text);
            font-family: 'Roboto', 'Inter', system-ui, sans-serif;
            height: 100vh;
            overflow: hidden;
            display: flex;
        }

        /* Responsive Layout */
        .app-shell {
            display: flex;
            width: 100%;
            height: 100%;
            background: radial-gradient(circle at top right, #2d2346, #0f1115);
        }

        /* Sidebar / Channel Drawer */
        .sidebar {
            width: 280px;
            height: 100%;
            background: var(--glass);
            backdrop-filter: blur(var(--blur));
            -webkit-backdrop-filter: blur(var(--blur));
            border-right: 1px solid var(--glass-border);
            display: flex;
            flex-direction: column;
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 1000;
        }

        .side-header {
            padding: 24px 16px;
            border-bottom: 1px solid var(--glass-border);
        }

        .side-header h1 { margin: 0; font-size: 20px; color: var(--primary); }

        .user-config {
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .m3-input {
            background: rgba(0, 0, 0, 0.3);
            border: 1px solid var(--glass-border);
            border-radius: 12px;
            padding: 12px;
            color: white;
            outline: none;
            width: 100%;
        }

        .channel-list { flex: 1; overflow-y: auto; padding: 8px; }

        .channel-item {
            padding: 12px 16px;
            border-radius: 16px;
            margin-bottom: 4px;
            cursor: pointer;
            transition: 0.2s;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .channel-item:hover { background: var(--glass); }
        .channel-item.active { background: var(--primary); color: var(--on-primary); }

        /* Main Content Area */
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            min-width: 0;
            position: relative;
        }

        .top-bar {
            height: 64px;
            padding: 0 16px;
            display: flex;
            align-items: center;
            gap: 16px;
            background: rgba(15, 17, 21, 0.5);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid var(--glass-border);
        }

        #menuToggle {
            background: none; border: none; color: white; font-size: 24px; cursor: pointer; display: none;
        }

        .message-area { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }

        .msg-bubble {
            background: var(--glass);
            padding: 12px 16px;
            border-radius: 18px;
            max-width: 85%;
            border: 1px solid var(--glass-border);
            align-self: flex-start;
        }

        .msg-bubble.own { align-self: flex-end; background: var(--on-primary); border-color: var(--primary); }

        .msg-meta { font-size: 11px; color: var(--text-muted); margin-bottom: 4px; display: flex; justify-content: space-between; }

        .composer {
            padding: 16px;
            background: var(--bg);
            display: flex;
            gap: 12px;
            align-items: flex-end;
            padding-bottom: calc(16px + env(safe-area-inset-bottom));
        }

        .composer-box {
            flex: 1;
            background: var(--glass);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            padding: 12px 16px;
            color: white;
            max-height: 120px;
            overflow-y: auto;
            outline: none;
            font-size: 16px;
        }

        .btn-round {
            width: 48px; height: 48px; border-radius: 24px; border: none; background: var(--primary);
            color: var(--on-primary); font-weight: bold; cursor: pointer; display: flex; align-items: center; justify-content: center;
        }

        /* Mobile Adjustments */
        @media (max-width: 800px) {
            #menuToggle { display: block; }
            .sidebar {
                position: absolute;
                transform: translateX(-100%);
            }
            .sidebar.open { transform: translateX(0); }
            .overlay {
                display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 999;
            }
            .sidebar.open + .overlay { display: block; }
        }
    </style>
</head>
<body>
    <div class="app-shell">
        <aside class="sidebar" id="sidebar">
            <div class="side-header">
                <h1>Nasoro</h1>
            </div>
            <div class="user-config">
                <input type="text" id="usernameInput" class="m3-input" placeholder="Change display name...">
                <button class="btn-round" style="width: 100%; border-radius: 12px;" onclick="updateName()">Update Profile</button>
            </div>
            <div class="channel-list" id="channels">
                <div class="channel-item active" onclick="switchRoom('general')"># general</div>
                <div class="channel-item" onclick="switchRoom('dev')"># dev-base</div>
                <div class="channel-item" onclick="switchRoom('showcase')"># showcase</div>
            </div>
        </aside>
        <div class="overlay" onclick="toggleMenu()"></div>

        <main class="main-content">
            <header class="top-bar">
                <button id="menuToggle" onclick="toggleMenu()">☰</button>
                <h2 id="roomTitle"># general</h2>
            </header>

            <div class="message-area" id="msgFeed"></div>

            <div class="composer">
                <div class="composer-box" id="composer" contenteditable="true" placeholder="Message Nasoro..."></div>
                <button class="btn-round" onclick="sendMsg()">➤</button>
            </div>
        </main>
    </div>

    <script>
        let currentRoom = 'general';
        let socket = null;
        let clientId = localStorage.getItem('nas_cid') || 'cli_' + Math.random().toString(36).substr(2, 9);
        let userName = localStorage.getItem('nas_name') || 'User';
        localStorage.setItem('nas_cid', clientId);

        document.getElementById('usernameInput').value = userName;

        function connect() {
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            socket = new WebSocket(`${proto}://${location.host}/ws/${currentRoom}`);

            socket.onopen = () => {
                socket.send(JSON.stringify({ kind: 'hello', client_id: clientId, name: userName }));
            };

            socket.onmessage = (e) => {
                const data = JSON.parse(e.data);
                if (data.kind === 'message') appendMsg(data);
                if (data.kind === 'history') {
                    document.getElementById('msgFeed').innerHTML = '';
                    data.messages.forEach(appendMsg);
                }
            };

            socket.onclose = () => setTimeout(connect, 2000);
        }

        function appendMsg(m) {
            const feed = document.getElementById('msgFeed');
            const isOwn = m.client_id === clientId;
            const div = document.createElement('div');
            div.className = `msg-bubble ${isOwn ? 'own' : ''}`;
            div.innerHTML = `
                <div class="msg-meta">
                    <strong>${m.name}</strong>
                    <span>${new Date(m.time).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}</span>
                </div>
                <div class="text">${m.text}</div>
            `;
            feed.appendChild(div);
            feed.scrollTop = feed.scrollHeight;
        }

        function sendMsg() {
            const box = document.getElementById('composer');
            const text = box.innerText.trim();
            if (!text || !socket) return;
            socket.send(JSON.stringify({ kind: 'message', text: text, client_id: clientId, name: userName }));
            box.innerText = '';
        }

        function updateName() {
            userName = document.getElementById('usernameInput').value.trim() || 'User';
            localStorage.setItem('nas_name', userName);
            if (socket) socket.send(JSON.stringify({ kind: 'rename', name: userName, client_id: clientId }));
        }

        function switchRoom(id) {
            if (id === currentRoom) return;
            currentRoom = id;
            document.getElementById('roomTitle').innerText = '# ' + id;
            document.querySelectorAll('.channel-item').forEach(el => el.classList.remove('active'));
            event.currentTarget.classList.add('active');
            if (socket) socket.close();
            if (window.innerWidth < 800) toggleMenu();
        }

        function toggleMenu() {
            document.getElementById('sidebar').classList.toggle('open');
        }

        // Enter key to send
        document.getElementById('composer').addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMsg();
            }
        });

        connect();
    </script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(content=page_html())

@app.get("/health")
def health():
    return {"status": "online", "project": "Nasoro LinkUp"}



