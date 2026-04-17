from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

app = FastAPI(title="LinkUp")


class Settings:
    def __init__(self) -> None:
        origins = os.getenv("CORS_ORIGINS", "*").strip()
        if origins == "*":
            self.cors_origins = ["*"]
        else:
            self.cors_origins = [o.strip() for o in origins.split(",") if o.strip()]


settings = Settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ws_router = APIRouter()


class RoomState:
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
            users.append(
                {
                    "client_id": client_id,
                    "name": info["name"],
                    "joined_at": info["joined_at"],
                }
            )
        users.sort(key=lambda u: (u["joined_at"], u["name"].lower()))
        return users

    def register(self, room: str, client_id: str, name: str, ws: WebSocket) -> bool:
        self.ensure(room)
        is_new = client_id not in self.members[room]
        if is_new:
            self.members[room][client_id] = {"name": name, "joined_at": int(time.time() * 1000)}
        else:
            self.members[room][client_id]["name"] = name
        self.sockets[room][client_id] = ws
        return is_new

    def remove(self, room: str, client_id: str) -> None:
        self.ensure(room)
        self.sockets[room].pop(client_id, None)
        self.members[room].pop(client_id, None)

    def append_history(self, room: str, payload: dict) -> None:
        self.ensure(room)
        self.history[room].append(payload)
        if len(self.history[room]) > 400:
            self.history[room] = self.history[room][-400:]

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
        if not msg:
            return None

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
        msg = self.get_message_by_id(room, msg_id)
        if not msg:
            return None

        bucket = self.reactions[room][msg_id][emoji]
        if client_id in bucket:
            bucket.remove(client_id)
        else:
            bucket.add(client_id)

        counts = {e: len(users) for e, users in self.reactions[room][msg_id].items()}
        return {"msg_id": msg_id, "reactions": counts}

    def set_message_reactions(self, room: str, msg_id: str, reactions: dict) -> None:
        msg = self.get_message_by_id(room, msg_id)
        if msg:
            msg["reactions"] = reactions

    async def send_history(self, room: str, ws: WebSocket) -> None:
        self.ensure(room)
        await ws.send_json(
            {
                "kind": "history",
                "room": room,
                "messages": self.history[room][-150:],
                "users": self.current_users(room),
                "pins": self.build_pins(room),
                "time": int(time.time() * 1000),
            }
        )

    async def send_presence(self, room: str) -> None:
        await self.broadcast(
            room,
            {
                "kind": "presence",
                "room": room,
                "users": self.current_users(room),
                "time": int(time.time() * 1000),
            },
        )

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

    async def system(self, room: str, text: str) -> None:
        await self.broadcast(
            room,
            {
                "kind": "system",
                "msg_id": "sys_" + uuid4().hex,
                "name": "System",
                "text": text,
                "room": room,
                "time": int(time.time() * 1000),
            },
        )


rooms = RoomState()


@ws_router.websocket("/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str):
    await websocket.accept()
    rooms.ensure(room_id)

    client_id = "guest_" + uuid4().hex[:10]
    display_name = "Guest"

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}

            kind = str(data.get("kind") or "message")
            incoming_client_id = str(data.get("client_id") or client_id)
            incoming_name = str(data.get("name") or display_name).strip()[:24] or "Guest"

            if kind == "hello":
                client_id = incoming_client_id
                display_name = incoming_name

                is_new = rooms.register(room_id, client_id, display_name, websocket)
                if is_new:
                    await rooms.system(room_id, f"{display_name} joined.")
                await rooms.send_history(room_id, websocket)
                await rooms.send_presence(room_id)
                continue

            if kind == "rename":
                client_id = incoming_client_id
                display_name = incoming_name
                rooms.register(room_id, client_id, display_name, websocket)
                await rooms.send_presence(room_id)
                continue

            if client_id not in rooms.members[room_id]:
                client_id = incoming_client_id
                display_name = incoming_name
                rooms.register(room_id, client_id, display_name, websocket)

            if kind == "typing":
                await rooms.broadcast(
                    room_id,
                    {
                        "kind": "typing",
                        "room": room_id,
                        "client_id": client_id,
                        "name": display_name,
                        "time": int(time.time() * 1000),
                    },
                )
                continue

            if kind == "pin":
                msg_id = str(data.get("msg_id") or "")
                result = rooms.toggle_pin(room_id, msg_id)
                if not result:
                    continue
                await rooms.broadcast(
                    room_id,
                    {
                        "kind": "pin_update",
                        "action": result["action"],
                        "message": result["message"],
                        "pins": rooms.build_pins(room_id),
                        "room": room_id,
                        "time": int(time.time() * 1000),
                    },
                )
                continue

            if kind == "reaction":
                msg_id = str(data.get("msg_id") or "")
                emoji = str(data.get("emoji") or "👍").strip()[:4]
                result = rooms.toggle_reaction(room_id, msg_id, emoji, client_id)
                if not result:
                    continue
                rooms.set_message_reactions(room_id, msg_id, result["reactions"])
                await rooms.broadcast(
                    room_id,
                    {
                        "kind": "reaction_update",
                        "msg_id": result["msg_id"],
                        "reactions": result["reactions"],
                        "room": room_id,
                        "time": int(time.time() * 1000),
                    },
                )
                continue

            text = str(data.get("text") or "").strip()
            if not text:
                continue

            reply_to = data.get("reply_to")
            reply_preview = None
            if reply_to:
                reply_msg = rooms.get_message_by_id(room_id, str(reply_to))
                if reply_msg:
                    reply_preview = {
                        "msg_id": reply_msg.get("msg_id"),
                        "name": reply_msg.get("name", "Guest"),
                        "text": reply_msg.get("text", ""),
                    }

            message = {
                "kind": "message",
                "msg_id": str(data.get("msg_id") or ("msg_" + uuid4().hex)),
                "client_id": client_id,
                "name": display_name,
                "text": text,
                "room": room_id,
                "time": int(data.get("time") or int(time.time() * 1000)),
                "reply_to": reply_to,
                "reply_preview": reply_preview,
                "reactions": {},
            }

            rooms.append_history(room_id, message)
            await rooms.broadcast(room_id, message)

    except WebSocketDisconnect:
        rooms.remove(room_id, client_id)
        await rooms.send_presence(room_id)
    except Exception:
        rooms.remove(room_id, client_id)
        await rooms.send_presence(room_id)


app.include_router(ws_router, prefix="/ws", tags=["websocket"])


def page_html() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LinkUp</title>
  <style>
    :root{
      --bg:#313338;
      --panel:#2b2d31;
      --panel-2:#1e1f22;
      --panel-3:#383a40;
      --line:rgba(255,255,255,.06);
      --text:#f2f3f5;
      --muted:#b5bac1;
      --accent:#5865f2;
      --accent-2:#3ba55d;
      --shadow:0 16px 50px rgba(0,0,0,.30);
      --radius:0;
      color-scheme: dark;
    }
    *{box-sizing:border-box}
    html,body{height:100%}
    body{
      margin:0;
      font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background:var(--bg);
      color:var(--text);
      overflow:hidden;
    }
    button,input,textarea{font:inherit}

    .app{
      height:100vh;
      display:grid;
      grid-template-columns: 72px 240px minmax(0,1fr) 268px;
      min-width:0;
    }

    .servers{
      background:var(--panel-2);
      border-right:1px solid rgba(255,255,255,.04);
      padding:12px 0;
      display:flex;
      flex-direction:column;
      gap:10px;
      align-items:center;
      overflow-y:auto;
    }
    .server-btn{
      width:48px;
      height:48px;
      border:none;
      border-radius:16px;
      cursor:pointer;
      background:#383a40;
      color:var(--text);
      font-weight:800;
      transition:.15s ease;
      box-shadow:none;
      flex:0 0 auto;
    }
    .server-btn:hover{background:#4752c4; transform:translateY(-1px)}
    .server-btn.active{
      background:var(--accent);
      border-radius:14px;
    }

    .sidebar{
      background:var(--panel);
      border-right:1px solid rgba(255,255,255,.04);
      display:flex;
      flex-direction:column;
      min-width:0;
      overflow:hidden;
    }
    .side-top{
      padding:16px 14px 12px;
      border-bottom:1px solid rgba(255,255,255,.04);
      flex:0 0 auto;
    }
    .brand{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
    }
    .brand h1{
      margin:0;
      font-size:16px;
      line-height:1.2;
      letter-spacing:.2px;
    }
    .brand small{
      display:block;
      margin-top:3px;
      color:var(--muted);
      font-size:12px;
    }
    .pill{
      margin-top:10px;
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:6px 10px;
      border-radius:999px;
      background:rgba(255,255,255,.04);
      color:var(--muted);
      font-size:12px;
      max-width:100%;
    }
    .section{
      padding:10px 8px 8px;
      overflow-y:auto;
      min-height:0;
      flex:1 1 auto;
    }
    .section-title{
      margin:6px 8px 10px;
      color:#969aa3;
      font-size:11px;
      font-weight:700;
      text-transform:uppercase;
      letter-spacing:.09em;
    }
    .channel{
      width:100%;
      border:none;
      border-radius:8px;
      padding:9px 10px;
      margin-bottom:2px;
      cursor:pointer;
      background:transparent;
      color:var(--text);
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      text-align:left;
      transition:.12s ease;
    }
    .channel:hover{background:rgba(255,255,255,.04)}
    .channel.active{background:rgba(255,255,255,.08)}
    .channel .meta{
      display:flex;
      flex-direction:column;
      min-width:0;
    }
    .channel .meta strong{
      font-size:15px;
      line-height:1.2;
    }
    .channel .meta span{
      color:var(--muted);
      font-size:12px;
      overflow:hidden;
      text-overflow:ellipsis;
      white-space:nowrap;
      max-width:165px;
    }
    .badge{
      font-size:11px;
      padding:3px 7px;
      border-radius:999px;
      background:rgba(255,255,255,.06);
      color:#d5d8de;
      flex:0 0 auto;
    }

    .main{
      min-width:0;
      display:flex;
      flex-direction:column;
      background:var(--bg);
      overflow:hidden;
    }

    .main-top{
      flex:0 0 auto;
      height:48px;
      display:flex;
      align-items:center;
      justify-content:space-between;
      padding:0 16px;
      border-bottom:1px solid rgba(255,255,255,.05);
      background:rgba(49,51,56,.92);
      backdrop-filter: blur(10px);
      z-index:3;
    }
    .titleblock{
      display:flex;
      align-items:center;
      gap:12px;
      min-width:0;
    }
    .titleblock h2{
      margin:0;
      font-size:15px;
      line-height:1;
      white-space:nowrap;
    }
    .titleblock p{
      margin:0;
      color:var(--muted);
      font-size:12px;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
      max-width:42vw;
    }
    .conn{
      display:flex;
      align-items:center;
      gap:8px;
      color:var(--muted);
      font-size:12px;
      flex:0 0 auto;
    }
    .dot{
      width:10px;height:10px;border-radius:999px;background:var(--accent-2);
      box-shadow:0 0 0 4px rgba(59,165,93,.10);
    }

    .meta-row{
      flex:0 0 auto;
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      padding:12px 16px;
      border-bottom:1px solid rgba(255,255,255,.05);
      background:#313338;
    }
    .stats{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      min-width:0;
    }
    .stat{
      padding:7px 10px;
      border-radius:999px;
      background:rgba(255,255,255,.04);
      color:var(--muted);
      font-size:12px;
      white-space:nowrap;
    }
    .search{
      width:min(360px, 42vw);
      padding:9px 12px;
      border-radius:8px;
      border:none;
      outline:none;
      background:#1e1f22;
      color:var(--text);
    }

    .typing{
      flex:0 0 auto;
      min-height:18px;
      padding:8px 16px 0;
      color:#b9bec7;
      font-size:12px;
    }

    .messages{
      flex:1 1 auto;
      min-height:0;
      overflow-y:auto;
      padding:10px 0 0;
    }

    .day-sep{
      display:flex;
      align-items:center;
      gap:12px;
      padding:10px 16px;
      color:#9da3ad;
      font-size:12px;
      text-transform:uppercase;
      letter-spacing:.08em;
    }
    .day-sep::before,.day-sep::after{
      content:"";
      height:1px;
      background:rgba(255,255,255,.08);
      flex:1;
    }

    .msg{
      display:grid;
      grid-template-columns: 42px minmax(0,1fr);
      gap:12px;
      padding:6px 16px 6px 16px;
      position:relative;
    }
    .msg:hover{
      background:rgba(255,255,255,.02);
    }
    .avatar{
      width:40px;
      height:40px;
      border-radius:50%;
      background:var(--accent);
      color:#fff;
      display:grid;
      place-items:center;
      font-weight:800;
      font-size:15px;
      margin-top:3px;
      flex:0 0 auto;
    }
    .content{
      min-width:0;
    }
    .header{
      display:flex;
      align-items:baseline;
      gap:8px;
      min-width:0;
      flex-wrap:wrap;
    }
    .name{
      font-weight:700;
      font-size:15px;
      color:#fff;
    }
    .time{
      color:#8e939d;
      font-size:11px;
    }
    .text{
      margin-top:2px;
      white-space:pre-wrap;
      word-break:break-word;
      line-height:1.5;
      font-size:15px;
      color:#dbdee1;
    }
    .replybox{
      margin-top:6px;
      padding:8px 10px;
      border-left:3px solid rgba(88,101,242,.95);
      background:rgba(88,101,242,.09);
      color:#dfe4ff;
      border-radius:8px;
      font-size:12px;
    }
    .actions{
      display:flex;
      gap:6px;
      flex-wrap:wrap;
      margin-top:8px;
      opacity:.88;
    }
    .chipbtn{
      border:none;
      background:#4f545c;
      color:#fff;
      border-radius:6px;
      padding:5px 8px;
      cursor:pointer;
      font-size:12px;
      line-height:1;
    }
    .chipbtn:hover{background:#5b6068}
    .chipbtn.active{
      background:rgba(88,101,242,.95);
    }
    .system{
      color:#b9bec7;
    }
    .system .avatar{
      background:#4e5058;
    }
    .system .text{
      color:#b9bec7;
      font-style:italic;
    }

    .composer{
      flex:0 0 auto;
      padding:16px;
      background:var(--bg);
    }
    .composer-wrap{
      display:flex;
      align-items:flex-start;
      gap:10px;
      padding:12px 14px;
      border-radius:10px;
      background:#383a40;
      border:1px solid rgba(255,255,255,.04);
    }
    .composer textarea{
      flex:1;
      min-height:44px;
      max-height:140px;
      height:44px;
      resize:none;
      overflow-y:auto;
      border:none;
      outline:none;
      background:transparent;
      color:var(--text);
      line-height:1.5;
      padding:10px 2px;
      font-size:15px;
    }
    .send{
      border:none;
      background:var(--accent);
      color:#fff;
      padding:10px 14px;
      border-radius:8px;
      cursor:pointer;
      font-weight:700;
      flex:0 0 auto;
    }

    .members{
      background:var(--panel);
      border-left:1px solid rgba(255,255,255,.04);
      display:flex;
      flex-direction:column;
      min-width:0;
      overflow:hidden;
    }
    .members-top{
      padding:14px 14px 12px;
      border-bottom:1px solid rgba(255,255,255,.04);
      flex:0 0 auto;
    }
    .members-top h3{
      margin:0;
      font-size:12px;
      text-transform:uppercase;
      letter-spacing:.09em;
      color:#969aa3;
    }
    .memberlist{
      padding:8px;
      overflow-y:auto;
      min-height:0;
      flex:1;
    }
    .member{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      padding:9px 10px;
      border-radius:8px;
      color:var(--text);
    }
    .member:hover{background:rgba(255,255,255,.04)}
    .member .left{
      min-width:0;
    }
    .member .left strong{
      display:block;
      font-size:14px;
      line-height:1.2;
    }
    .member .left span{
      display:block;
      margin-top:2px;
      font-size:12px;
      color:var(--muted);
    }
    .member .dot2{
      width:8px;height:8px;border-radius:999px;background:var(--accent-2);flex:0 0 auto;
    }

    .pinsbar{
      display:flex;
      gap:8px;
      overflow-x:auto;
      padding:0 16px 10px;
      min-height:0;
    }
    .pinpill{
      flex:0 0 auto;
      max-width:280px;
      padding:7px 10px;
      border-radius:999px;
      background:rgba(255,255,255,.05);
      color:#d7dae0;
      font-size:12px;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }

    @media (max-width: 1100px){
      .app{grid-template-columns:72px 240px minmax(0,1fr);}
      .members{display:none}
      .search{width:40vw}
    }
    @media (max-width: 820px){
      body{overflow:auto}
      .app{
        height:auto;
        min-height:100vh;
        grid-template-columns:1fr;
      }
      .servers,.sidebar,.main,.members{
        border-right:none;
        border-left:none;
      }
      .servers{
        flex-direction:row;
        justify-content:flex-start;
        padding:10px;
        overflow-x:auto;
        overflow-y:hidden;
      }
      .sidebar{min-height:220px}
      .members{display:flex;min-height:220px}
      .search{width:100%}
      .meta-row{flex-direction:column; align-items:stretch}
      .titleblock p{max-width:100%}
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="servers" id="servers"></aside>

    <aside class="sidebar">
      <div class="side-top">
        <div class="brand">
          <div>
            <h1>LinkUp</h1>
            <small>clean chat, not cramped</small>
          </div>
          <div class="badge">LIVE</div>
        </div>
        <div class="pill">Room: <span id="roomLabel">#general</span></div>
      </div>

      <div class="section">
        <div class="section-title">Channels</div>
        <div id="channels"></div>
      </div>
    </aside>

    <main class="main">
      <div class="main-top">
        <div class="titleblock">
          <h2 id="channelTitle"># general</h2>
          <p id="channelDescription">A big, readable room for real-time chat.</p>
        </div>
        <div class="conn"><span class="dot"></span><span id="connState">connecting...</span></div>
      </div>

      <div class="meta-row">
        <div class="stats">
          <div class="stat"><span id="statUsers">0</span> online</div>
          <div class="stat"><span id="statMessages">0</span> messages</div>
          <div class="stat"><span id="statPins">0</span> pinned</div>
          <div class="stat"><span id="statState">idle</span></div>
        </div>
        <input class="search" id="searchInput" placeholder="Search this room..." />
      </div>

      <div class="typing" id="typingLine"></div>
      <div class="pinsbar" id="pinsbar"></div>
      <div class="messages" id="messages"></div>

      <div class="composer">
        <div class="composer-wrap">
          <textarea id="messageInput" placeholder="Message LinkUp..."></textarea>
          <button class="send" id="sendBtn">Send</button>
        </div>
      </div>
    </main>

    <aside class="members">
      <div class="members-top">
        <h3>Online</h3>
      </div>
      <div class="memberlist" id="members"></div>
    </aside>
  </div>

<script>
(() => {
  const servers = [
    { id: "home", name: "L", label: "LinkUp Hub" },
    { id: "dev", name: "D", label: "Dev Base" },
    { id: "games", name: "G", label: "Game Squad" },
    { id: "study", name: "S", label: "Study Zone" }
  ];

  const channelsByServer = {
    home: [
      { id: "general", title: "# general", description: "Main feed for everything LinkUp." },
      { id: "announcements", title: "# announcements", description: "Product updates, releases, and changelogs." },
      { id: "showcase", title: "# showcase", description: "Drop cool projects, screenshots, and wins." }
    ],
    dev: [
      { id: "frontend", title: "# frontend", description: "UI, components, and layout experiments." },
      { id: "backend", title: "# backend", description: "APIs, database, auth, and server logic." },
      { id: "bugs", title: "# bugs", description: "Report issues and weird behavior here." }
    ],
    games: [
      { id: "lobby", title: "# lobby", description: "Find players and set up sessions." },
      { id: "clips", title: "# clips", description: "Highlights, funny moments, and screenshots." },
      { id: "meta", title: "# meta", description: "Discuss builds, balance, and strategy." }
    ],
    study: [
      { id: "math", title: "# math", description: "Problem solving and homework help." },
      { id: "coding", title: "# coding", description: "Programming help and pair debugging." },
      { id: "focus", title: "# focus", description: "Quiet study time and accountability." }
    ]
  };

  const emojiChoices = ["👍", "🔥", "😂", "👀", "💯", "❤️"];

  const els = {
    servers: document.getElementById("servers"),
    channels: document.getElementById("channels"),
    messages: document.getElementById("messages"),
    channelTitle: document.getElementById("channelTitle"),
    channelDescription: document.getElementById("channelDescription"),
    connState: document.getElementById("connState"),
    messageInput: document.getElementById("messageInput"),
    sendBtn: document.getElementById("sendBtn"),
    roomLabel: document.getElementById("roomLabel"),
    members: document.getElementById("members"),
    typingLine: document.getElementById("typingLine"),
    statMessages: document.getElementById("statMessages"),
    statUsers: document.getElementById("statUsers"),
    statPins: document.getElementById("statPins"),
    statState: document.getElementById("statState"),
    searchInput: document.getElementById("searchInput"),
    pinsbar: document.getElementById("pinsbar"),
  };

  const savedName = localStorage.getItem("linkup_name") || "You";
  const localClientId = localStorage.getItem("linkup_client_id") || ("cli_" + Math.random().toString(36).slice(2, 10));
  localStorage.setItem("linkup_client_id", localClientId);

  let displayName = savedName;
  let selectedServer = localStorage.getItem("linkup_server") || "home";
  let selectedChannel = localStorage.getItem("linkup_channel") || "general";

  let socket = null;
  let connectionSeq = 0;
  let reconnectTimer = null;
  let handshakeTimer = null;
  let retryCount = 0;
  let typingTimeout = null;
  let replyTo = null;

  const roomMessages = new Map();
  const roomSeen = new Map();
  const roomPresence = new Map();
  const roomPins = new Map();
  const reactionMap = new Map();

  const seeds = {
    general: [
      "Welcome to LinkUp.",
      "This UI is now flatter and easier to read.",
      "Scroll the message area and the composer stays put."
    ],
    announcements: ["Ship updates here."],
    showcase: ["Drop your best work here."],
    frontend: ["UI, motion, and layout talk."],
    backend: ["FastAPI, WebSockets, and database design."],
    bugs: ["Log bugs and squash them."],
    lobby: ["Find your squad."],
    clips: ["Funny moments belong here."],
    meta: ["Discuss builds and strategy."],
    math: ["Study mode online."],
    coding: ["Code and conquer."],
    focus: ["Deep work zone."]
  };

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, s => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#39;"
    }[s]));
  }

  function fmtTime(ts) {
    return new Date(ts || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function currentChannel() {
    const list = channelsByServer[selectedServer] || [];
    return list.find(c => c.id === selectedChannel) || list[0];
  }

  function roomId() {
    return `${selectedServer}:${selectedChannel}`;
  }

  function setStatus(text, connected = false) {
    els.connState.textContent = text;
    els.statState.textContent = text.toLowerCase().slice(0, 8);
    els.connState.style.color = connected ? "#dcffe9" : "";
  }

  function ensureRoom(room) {
    if (!roomMessages.has(room)) {
      const ch = room.split(":")[1] || "general";
      const seedMessages = (seeds[ch] || ["Room loaded."]).map((text, idx) => ({
        msg_id: `seed-${room}-${idx}`,
        kind: "system",
        name: "System",
        text,
        time: Date.now() - ((idx + 1) * 60000),
        room
      }));
      roomMessages.set(room, seedMessages);
      roomSeen.set(room, new Set(seedMessages.map(m => m.msg_id)));
      roomPresence.set(room, []);
      roomPins.set(room, []);
      reactionMap.set(room, new Map());
    }
  }

  function currentUsers() {
    ensureRoom(roomId());
    return roomPresence.get(roomId()) || [];
  }

  function currentPins() {
    ensureRoom(roomId());
    return roomPins.get(roomId()) || [];
  }

  function seenSet(room) {
    ensureRoom(room);
    return roomSeen.get(room);
  }

  function roomList() {
    ensureRoom(roomId());
    return roomMessages.get(roomId());
  }

  function renderServers() {
    els.servers.innerHTML = servers.map(s => `
      <button class="server-btn ${s.id === selectedServer ? "active" : ""}" data-server="${s.id}" title="${s.label}">
        ${s.name}
      </button>
    `).join("");

    els.servers.querySelectorAll("button").forEach(btn => {
      btn.onclick = () => {
        selectedServer = btn.dataset.server;
        selectedChannel = channelsByServer[selectedServer][0].id;
        replyTo = null;
        localStorage.setItem("linkup_server", selectedServer);
        localStorage.setItem("linkup_channel", selectedChannel);
        renderShell();
        connectSocket(true);
      };
    });
  }

  function renderChannels() {
    els.channels.innerHTML = (channelsByServer[selectedServer] || []).map(ch => `
      <button class="channel ${ch.id === selectedChannel ? "active" : ""}" data-channel="${ch.id}">
        <div class="meta">
          <strong>${ch.title}</strong>
          <span>${escapeHtml(ch.description)}</span>
        </div>
        <span class="badge">#</span>
      </button>
    `).join("");

    els.channels.querySelectorAll("button").forEach(btn => {
      btn.onclick = () => {
        selectedChannel = btn.dataset.channel;
        replyTo = null;
        localStorage.setItem("linkup_channel", selectedChannel);
        renderShell();
        connectSocket(true);
      };
    });
  }

  function renderTop() {
    const ch = currentChannel();
    els.roomLabel.textContent = `#${selectedChannel}`;
    els.channelTitle.textContent = ch ? ch.title : "# general";
    els.channelDescription.textContent = ch ? ch.description : "A big, readable room for real-time chat.";
  }

  function renderMembers() {
    const users = currentUsers();
    els.statUsers.textContent = String(users.length);

    els.members.innerHTML = users.length ? users.map(u => `
      <div class="member">
        <div class="left">
          <strong>${escapeHtml(u.name)}</strong>
          <span>online</span>
        </div>
        <div class="dot2"></div>
      </div>
    `).join("") : `
      <div class="member">
        <div class="left">
          <strong>No one else yet</strong>
          <span>Be the first in this room.</span>
        </div>
      </div>
    `;
  }

  function renderPins() {
    const pins = currentPins();
    els.statPins.textContent = String(pins.length);
    els.pinsbar.innerHTML = pins.length ? pins.slice().reverse().map(p => `
      <div class="pinpill" data-pin="${escapeHtml(p.msg_id)}">${escapeHtml(p.name)}: ${escapeHtml((p.text || "").slice(0, 80))}</div>
    `).join("") : `<div class="pinpill">No pins yet</div>`;

    els.pinsbar.querySelectorAll("[data-pin]").forEach(node => {
      node.onclick = () => {
        const id = node.getAttribute("data-pin");
        const target = els.messages.querySelector(`[data-id="${CSS.escape(id)}"]`);
        if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
      };
    });
  }

  function renderMessageHtml(m) {
    const reactions = m.reactions || {};
    const reactionKeys = Object.keys(reactions);
    const isSelf = m.client_id === localClientId;
    const initial = (m.name || "?").trim().slice(0, 1).toUpperCase();

    if (m.kind === "system") {
      return `
        <div class="msg system" data-id="${escapeHtml(m.msg_id)}">
          <div class="avatar">!</div>
          <div class="content">
            <div class="header">
              <div class="name">System</div>
              <div class="time">${fmtTime(m.time)}</div>
            </div>
            <div class="text">${escapeHtml(m.text)}</div>
          </div>
        </div>
      `;
    }

    return `
      <div class="msg" data-id="${escapeHtml(m.msg_id)}">
        <div class="avatar">${escapeHtml(initial)}</div>
        <div class="content">
          <div class="header">
            <div class="name">${escapeHtml(m.name || "Guest")} ${isSelf ? "<span class='badge'>you</span>" : ""}</div>
            <div class="time">${fmtTime(m.time)}</div>
          </div>
          ${m.reply_preview ? `
            <div class="replybox">
              Replying to <b>${escapeHtml(m.reply_preview.name || "Guest")}</b><br/>
              ${escapeHtml((m.reply_preview.text || "").slice(0, 120))}
            </div>
          ` : ""}
          <div class="text">${escapeHtml(m.text || "")}</div>
          <div class="actions">
            <button class="chipbtn" data-action="reply" data-id="${escapeHtml(m.msg_id)}">Reply</button>
            <button class="chipbtn" data-action="pin" data-id="${escapeHtml(m.msg_id)}">Pin</button>
            <button class="chipbtn" data-action="react" data-id="${escapeHtml(m.msg_id)}">React</button>
            ${reactionKeys.map(e => `<button class="chipbtn active">${escapeHtml(e)} ${reactions[e]}</button>`).join("")}
          </div>
        </div>
      </div>
    `;
  }

  function applyFilter(room) {
    const q = (els.searchInput.value || "").trim().toLowerCase();
    return roomList(room).filter(m => {
      if (m.kind === "system") return true;
      if (!q) return true;
      return String(m.text || "").toLowerCase().includes(q) || String(m.name || "").toLowerCase().includes(q);
    });
  }

  function renderMessages() {
    ensureRoom(roomId());
    const msgs = applyFilter(roomId());
    els.messages.innerHTML = msgs.map(renderMessageHtml).join("");
    els.statMessages.textContent = String(roomList().filter(m => m.kind !== "system").length);
    els.messages.scrollTop = els.messages.scrollHeight;
    bindMessageActions();
  }

  function renderShell() {
    renderServers();
    renderChannels();
    renderTop();
    renderMembers();
    renderPins();
    renderMessages();
  }

  function pushMessage(room, msg, renderNow = true) {
    ensureRoom(room);
    if (!msg.msg_id) msg.msg_id = "msg_" + Math.random().toString(36).slice(2, 12);

    const seen = seenSet(room);
    if (seen.has(msg.msg_id)) return;
    seen.add(msg.msg_id);

    msg.reactions = msg.reactions || {};
    roomList().push(msg);

    if (room === roomId() && renderNow) {
      els.messages.insertAdjacentHTML("beforeend", renderMessageHtml(msg));
      els.messages.scrollTop = els.messages.scrollHeight;
      els.statMessages.textContent = String(roomList().filter(m => m.kind !== "system").length);
      bindMessageActions();
    }
  }

  function wsUrl(room) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/${encodeURIComponent(room)}`;
  }

  function cleanupSocketTimers() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (handshakeTimer) {
      clearTimeout(handshakeTimer);
      handshakeTimer = null;
    }
  }

  function scheduleReconnect(seq) {
    if (seq !== connectionSeq) return;
    const delay = Math.min(1000 * Math.pow(2, retryCount), 10000);
    retryCount += 1;

    reconnectTimer = setTimeout(() => {
      if (seq !== connectionSeq) return;
      connectSocket(false);
    }, delay);

    setTimeout(() => {
      if (seq !== connectionSeq) return;
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        setStatus(`reconnecting in ${Math.ceil(delay / 1000)}s`);
      }
    }, 20);
  }

  function connectSocket(force = false) {
    cleanupSocketTimers();
    const seq = ++connectionSeq;
    const room = roomId();
    let opened = false;

    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      try { socket.close(1000, "switch"); } catch {}
    }

    roomPresence.set(room, []);
    els.typingLine.textContent = "";
    setStatus("connecting...");

    try {
      socket = new WebSocket(wsUrl(room));
    } catch {
      setStatus("socket unavailable");
      scheduleReconnect(seq);
      return;
    }

    handshakeTimer = setTimeout(() => {
      if (seq !== connectionSeq) return;
      if (!opened) setStatus("connecting... still trying");
    }, 4000);

    socket.onopen = () => {
      if (seq !== connectionSeq) {
        try { socket.close(1000, "stale"); } catch {}
        return;
      }
      opened = true;
      retryCount = 0;
      cleanupSocketTimers();
      setStatus("connected", true);

      socket.send(JSON.stringify({
        kind: "hello",
        client_id: localClientId,
        name: displayName,
        room,
        server: selectedServer,
        channel: selectedChannel,
        time: Date.now()
      }));
    };

    socket.onmessage = (event) => {
      if (seq !== connectionSeq) return;

      let data = null;
      try { data = JSON.parse(event.data); } catch { data = null; }
      if (!data) return;

      if (data.kind === "history") {
        const history = Array.isArray(data.messages) ? data.messages : [];
        const users = Array.isArray(data.users) ? data.users : [];
        const pins = Array.isArray(data.pins) ? data.pins : [];

        roomMessages.set(room, []);
        roomSeen.set(room, new Set());
        roomPresence.set(room, users);
        roomPins.set(room, pins);

        history.forEach(m => {
          if (!m.msg_id) m.msg_id = "hist_" + Math.random().toString(36).slice(2, 12);
          seenSet(room).add(m.msg_id);
          if (!m.reactions) m.reactions = {};
          roomMessages.get(room).push(m);
        });

        renderMembers();
        renderPins();
        renderMessages();
        return;
      }

      if (data.kind === "presence") {
        roomPresence.set(room, Array.isArray(data.users) ? data.users : []);
        renderMembers();
        return;
      }

      if (data.kind === "typing") {
        if (data.client_id && data.client_id !== localClientId) {
          els.typingLine.textContent = `${data.name || "Someone"} is typing...`;
          clearTimeout(typingTimeout);
          typingTimeout = setTimeout(() => els.typingLine.textContent = "", 1200);
        }
        return;
      }

      if (data.kind === "system") {
        pushMessage(room, data, true);
        return;
      }

      if (data.kind === "pin_update") {
        roomPins.set(room, Array.isArray(data.pins) ? data.pins : []);
        renderPins();
        return;
      }

      if (data.kind === "reaction_update") {
        const list = roomList();
        const target = list.find(m => m.msg_id === data.msg_id);
        if (target) {
          target.reactions = data.reactions || {};
          renderMessages();
        }
        return;
      }

      if (data.kind === "message") {
        data.time = data.time || Date.now();
        data.client_id = data.client_id || "remote";
        data.msg_id = data.msg_id || `srv_${room}_${data.time}_${Math.random().toString(36).slice(2, 8)}`;
        data.reactions = data.reactions || {};
        pushMessage(room, data, true);
      }
    };

    socket.onerror = () => {
      if (seq !== connectionSeq) return;
      if (!opened) setStatus("connection error");
    };

    socket.onclose = () => {
      if (seq !== connectionSeq) return;
      cleanupSocketTimers();

      setStatus(opened ? "disconnected" : "offline");
      scheduleReconnect(seq);
    };
  }

  function sendTyping() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({
      kind: "typing",
      client_id: localClientId,
      name: displayName,
      room: roomId(),
      time: Date.now()
    }));
  }

  function sendMessage() {
    const text = els.messageInput.value.trim();
    if (!text) return;

    const payload = {
      kind: "message",
      msg_id: `cli_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`,
      client_id: localClientId,
      name: displayName,
      text,
      room: roomId(),
      server: selectedServer,
      channel: selectedChannel,
      reply_to: replyTo,
      time: Date.now()
    };

    if (replyTo) {
      const original = roomList().find(m => m.msg_id === replyTo);
      if (original) {
        payload.reply_preview = {
          msg_id: original.msg_id,
          name: original.name,
          text: original.text
        };
      }
    }

    pushMessage(roomId(), payload, true);

    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
    }

    replyTo = null;
    els.messageInput.placeholder = "Message LinkUp...";
    els.messageInput.value = "";
    els.messageInput.focus();
  }

  function bindMessageActions() {
    els.messages.querySelectorAll(".chipbtn[data-action]").forEach(btn => {
      btn.onclick = () => {
        const action = btn.dataset.action;
        const id = btn.dataset.id;
        const room = roomId();

        if (action === "reply") {
          replyTo = id;
          const original = roomList().find(m => m.msg_id === id);
          els.messageInput.placeholder = original ? `Replying to ${original.name}...` : "Replying...";
          els.messageInput.focus();
          return;
        }

        if (action === "pin") {
          if (!socket || socket.readyState !== WebSocket.OPEN) return;
          socket.send(JSON.stringify({
            kind: "pin",
            client_id: localClientId,
            name: displayName,
            room,
            msg_id: id,
            time: Date.now()
          }));
          return;
        }

        if (action === "react") {
          const emoji = prompt(`Reaction: ${emojiChoices.join(" ")}`, "👍");
          if (!emoji) return;
          if (!socket || socket.readyState !== WebSocket.OPEN) return;
          socket.send(JSON.stringify({
            kind: "reaction",
            client_id: localClientId,
            name: displayName,
            room,
            msg_id: id,
            emoji: emoji.trim().slice(0, 4),
            time: Date.now()
          }));
        }
      };
    });
  }

  els.searchInput.addEventListener("input", renderMessages);

  els.messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  els.messageInput.addEventListener("input", () => {
    sendTyping();
  });

  els.sendBtn.onclick = sendMessage;

  window.addEventListener("online", () => {
    setStatus("back online");
    connectSocket(true);
  });

  window.addEventListener("offline", () => {
    setStatus("offline");
  });

  renderShell();
  connectSocket(true);
})();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(content=page_html())


@app.get("/health")
def health():
    return {"ok": True, "app": "LinkUp"}


@app.get("/api")
def api_root():
    return {"message": "LinkUp API is live", "websocket": "/ws/{room_id}"}


@app.get("/{path:path}", response_class=HTMLResponse)
def spa_fallback(path: str):
    return HTMLResponse(content=page_html())
