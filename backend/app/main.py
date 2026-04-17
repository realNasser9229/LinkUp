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

        counts = {}
        for e, users in self.reactions[room][msg_id].items():
            counts[e] = len(users)

        return {
            "msg_id": msg_id,
            "reactions": counts,
        }

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
                "pinned": False,
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
      --bg:#050812;
      --panel:rgba(12, 18, 37, .90);
      --panel2:rgba(17, 26, 51, .96);
      --line:rgba(255,255,255,.08);
      --text:#eef3ff;
      --muted:#9aa8cf;
      --accent:#7b8cff;
      --accent2:#66efc0;
      --shadow:0 30px 90px rgba(0,0,0,.42);
      --radius:24px;
      color-scheme: dark;
    }
    *{box-sizing:border-box}
    html,body{height:100%}
    body{
      margin:0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      color:var(--text);
      background:
        radial-gradient(circle at 0% 0%, rgba(123,140,255,.18), transparent 22%),
        radial-gradient(circle at 100% 0%, rgba(102,239,192,.10), transparent 20%),
        linear-gradient(180deg, #050812 0%, #070d18 100%);
    }
    button,input,textarea{font:inherit}
    .app{
      min-height:100vh;
      padding:16px;
      display:grid;
      grid-template-columns: 92px 286px minmax(0,1fr) 380px;
      gap:16px;
    }
    .glass{
      background:var(--panel);
      border:1px solid var(--line);
      box-shadow:var(--shadow);
      backdrop-filter: blur(18px);
    }

    .servers{
      border-radius:var(--radius);
      padding:12px 8px;
      display:flex;
      flex-direction:column;
      gap:10px;
      align-items:center;
      overflow:auto;
    }
    .server-btn{
      width:58px;height:58px;
      border:none;
      border-radius:18px;
      cursor:pointer;
      background:linear-gradient(180deg, #1a2448, #0f1630);
      color:var(--text);
      border:1px solid rgba(255,255,255,.05);
      font-weight:800;
      transition:.15s ease;
      flex:0 0 auto;
    }
    .server-btn:hover{transform:translateY(-1px); border-color:rgba(123,140,255,.35)}
    .server-btn.active{
      background:linear-gradient(180deg, #7b8cff, #5361ff);
      box-shadow:0 14px 32px rgba(123,140,255,.22);
    }

    .sidebar{
      border-radius:var(--radius);
      overflow:hidden;
      display:flex;
      flex-direction:column;
      min-width:0;
    }
    .side-top{
      padding:18px;
      border-bottom:1px solid var(--line);
      background:linear-gradient(180deg, rgba(255,255,255,.01), rgba(255,255,255,.02));
    }
    .brand{
      display:flex;
      align-items:flex-start;
      justify-content:space-between;
      gap:12px;
    }
    .brand h1{
      margin:0;
      font-size:24px;
      line-height:1;
    }
    .brand small{
      display:block;
      margin-top:5px;
      color:var(--muted);
      font-size:12px;
    }
    .pill{
      display:inline-flex;
      align-items:center;
      gap:8px;
      margin-top:12px;
      padding:8px 10px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,.08);
      background:rgba(255,255,255,.03);
      color:#d8e0ff;
      font-size:12px;
      width:fit-content;
    }
    .section{
      padding:14px;
    }
    .section-title{
      margin:0 0 10px;
      color:#b7c4ea;
      font-size:12px;
      text-transform:uppercase;
      letter-spacing:.14em;
    }
    .channel{
      width:100%;
      border:none;
      border-radius:14px;
      padding:12px;
      margin-bottom:6px;
      cursor:pointer;
      background:transparent;
      color:var(--text);
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      text-align:left;
      transition:.14s ease;
    }
    .channel:hover{background:rgba(255,255,255,.04)}
    .channel.active{
      background:rgba(123,140,255,.15);
      outline:1px solid rgba(123,140,255,.27);
    }
    .channel .meta{
      display:flex;
      flex-direction:column;
      min-width:0;
    }
    .channel .meta strong{font-size:14px}
    .channel .meta span{
      color:var(--muted);
      font-size:12px;
      overflow:hidden;
      text-overflow:ellipsis;
      white-space:nowrap;
      max-width:190px;
    }
    .badge{
      font-size:11px;
      padding:3px 8px;
      border-radius:999px;
      background:rgba(123,140,255,.14);
      color:#dbe2ff;
      border:1px solid rgba(123,140,255,.18);
      flex:0 0 auto;
    }
    .footer-note{
      margin-top:auto;
      padding:14px 16px;
      border-top:1px solid var(--line);
      color:var(--muted);
      font-size:12px;
      line-height:1.6;
    }
    .kbd{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      min-width:24px;
      padding:2px 6px;
      border-radius:6px;
      border:1px solid rgba(255,255,255,.08);
      background:rgba(255,255,255,.04);
      color:#dbe3ff;
      font-size:11px;
    }

    .main{
      border-radius:var(--radius);
      overflow:hidden;
      display:flex;
      flex-direction:column;
      min-width:0;
      background:var(--panel2);
    }
    .main-top{
      position:sticky;
      top:0;
      z-index:10;
      padding:18px 20px;
      border-bottom:1px solid var(--line);
      display:flex;
      align-items:flex-start;
      justify-content:space-between;
      gap:14px;
      background:rgba(17, 26, 51, .98);
      backdrop-filter: blur(14px);
    }
    .titleblock h2{
      margin:0;
      font-size:24px;
    }
    .titleblock p{
      margin:5px 0 0;
      color:var(--muted);
      font-size:13px;
    }
    .conn{
      display:flex;
      align-items:center;
      gap:10px;
      color:var(--muted);
      font-size:13px;
      white-space:nowrap;
    }
    .dot{
      width:10px;height:10px;border-radius:999px;background:var(--accent2);
      box-shadow:0 0 0 4px rgba(102,239,192,.12);
    }
    .stats{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      margin-top:14px;
    }
    .stat{
      border:1px solid rgba(255,255,255,.08);
      background:rgba(255,255,255,.03);
      border-radius:16px;
      padding:12px 14px;
      min-width:140px;
    }
    .stat b{
      display:block;
      font-size:19px;
      margin-bottom:2px;
    }
    .stat span{
      color:var(--muted);
      font-size:12px;
    }

    .topbar-tools{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      justify-content:flex-end;
      align-items:center;
    }
    .search{
      width:min(360px, 52vw);
      padding:12px 14px;
      border-radius:14px;
      border:1px solid rgba(255,255,255,.08);
      background:#08111f;
      color:var(--text);
      outline:none;
    }
    .toolbtn{
      border:none;
      background:rgba(255,255,255,.06);
      color:var(--text);
      border:1px solid rgba(255,255,255,.06);
      padding:12px 14px;
      border-radius:14px;
      cursor:pointer;
    }
    .toolbtn:hover{background:rgba(255,255,255,.09)}

    .typing{
      min-height:22px;
      color:#c7d2ff;
      font-size:12px;
      padding:0 22px 8px;
    }

    .messages{
      flex:1;
      overflow:auto;
      padding:24px;
      display:flex;
      flex-direction:column;
      gap:14px;
      scroll-behavior:smooth;
    }
    .msg{
      display:grid;
      grid-template-columns:52px minmax(0,1fr);
      gap:14px;
      padding:18px;
      border-radius:20px;
      border:1px solid rgba(255,255,255,.06);
      background:rgba(255,255,255,.025);
    }
    .msg:hover{background:rgba(255,255,255,.032)}
    .avatar{
      width:52px;height:52px;border-radius:18px;
      display:grid;
      place-items:center;
      background:linear-gradient(180deg, #7b8cff, #5361ff);
      color:#fff;
      font-weight:800;
      flex:0 0 auto;
      box-shadow:0 10px 20px rgba(123,140,255,.16);
    }
    .head{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      margin-bottom:8px;
    }
    .name{
      display:flex;
      align-items:center;
      gap:8px;
      font-weight:700;
      min-width:0;
    }
    .name span:first-child{
      overflow:hidden;
      text-overflow:ellipsis;
      white-space:nowrap;
    }
    .time{color:var(--muted);font-size:12px;flex:0 0 auto}
    .text{
      white-space:pre-wrap;
      word-break:break-word;
      line-height:1.7;
      color:#eef2ff;
      font-size:15px;
    }
    .system{
      border-style:dashed;
      background:rgba(123,140,255,.07);
    }
    .replybox{
      margin-bottom:10px;
      padding:10px 12px;
      border-radius:14px;
      border-left:3px solid rgba(123,140,255,.75);
      background:rgba(123,140,255,.08);
      color:#d9e2ff;
      font-size:12px;
    }
    .msg-actions{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
      margin-top:12px;
    }
    .chipbtn{
      border:none;
      border-radius:999px;
      padding:7px 10px;
      cursor:pointer;
      font-size:12px;
      background:rgba(255,255,255,.05);
      color:#e9eeff;
      border:1px solid rgba(255,255,255,.06);
    }
    .chipbtn:hover{background:rgba(255,255,255,.09)}
    .chipbtn.active{
      background:rgba(123,140,255,.16);
      border-color:rgba(123,140,255,.22);
    }

    .composer{
      padding:16px 18px 18px;
      border-top:1px solid var(--line);
      background:linear-gradient(180deg, rgba(255,255,255,.01), rgba(255,255,255,.03));
    }
    .composer-wrap{
      display:flex;
      gap:12px;
      align-items:flex-end;
      padding:16px;
      border-radius:22px;
      background:#08111f;
      border:1px solid rgba(255,255,255,.06);
    }
    .composer textarea{
      flex:1;
      resize:none;
      min-height:72px;
      max-height:220px;
      background:transparent;
      color:var(--text);
      border:none;
      outline:none;
      line-height:1.65;
      padding:12px 10px;
      font-size:15px;
    }
    .send{
      border:none;
      background:linear-gradient(180deg, #7b8cff, #5361ff);
      color:#fff;
      padding:14px 18px;
      border-radius:14px;
      cursor:pointer;
      font-weight:700;
      box-shadow:0 14px 30px rgba(123,140,255,.20);
      min-width:98px;
    }
    .send:hover{filter:brightness(1.04)}

    .tools{
      border-radius:var(--radius);
      overflow:hidden;
      display:flex;
      flex-direction:column;
      min-width:0;
    }
    .panel{
      padding:16px;
      border-bottom:1px solid var(--line);
    }
    .panel h3{
      margin:0 0 10px;
      font-size:14px;
    }
    .panel p, .panel li{
      color:var(--muted);
      font-size:13px;
      line-height:1.6;
    }
    .input{
      width:100%;
      margin-top:10px;
      padding:12px 14px;
      border-radius:14px;
      border:1px solid rgba(255,255,255,.08);
      background:#08111f;
      color:var(--text);
      outline:none;
    }
    .list{
      display:flex;
      flex-direction:column;
      gap:10px;
      margin:0;
      padding:0;
      list-style:none;
    }
    .user{
      display:flex;
      align-items:flex-start;
      justify-content:space-between;
      gap:10px;
      padding:12px;
      border-radius:16px;
      background:rgba(255,255,255,.03);
      border:1px solid rgba(255,255,255,.05);
    }
    .user-main{
      min-width:0;
      flex:1;
    }
    .user strong{
      font-size:13px;
      display:flex;
      align-items:center;
      gap:6px;
      margin-bottom:2px;
    }
    .user span{
      font-size:12px;
      color:var(--muted);
    }
    .mini{
      border:none;
      border-radius:10px;
      padding:7px 9px;
      cursor:pointer;
      font-size:11px;
      background:rgba(255,255,255,.06);
      color:#e9eeff;
      border:1px solid rgba(255,255,255,.06);
    }
    .mini:hover{background:rgba(255,255,255,.09)}
    .tiny{
      color:var(--muted);
      font-size:12px;
      line-height:1.6;
      padding:14px 16px;
    }
    .updates{
      display:flex;
      flex-direction:column;
      gap:10px;
    }
    .update{
      padding:12px;
      border-radius:14px;
      border:1px solid rgba(255,255,255,.06);
      background:rgba(255,255,255,.025);
    }
    .update b{
      display:block;
      margin-bottom:4px;
      font-size:13px;
    }

    @media (max-width: 1280px){
      .app{grid-template-columns: 92px 270px minmax(0,1fr);}
      .tools{display:none}
    }
    @media (max-width: 900px){
      .app{
        grid-template-columns:1fr;
        padding:12px;
      }
      .servers{
        flex-direction:row;
        justify-content:flex-start;
        overflow:auto;
      }
      .main{min-height:74vh}
      .tools{display:flex}
      .search{width:100%}
      .topbar-tools{justify-content:flex-start}
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="servers glass" id="servers"></aside>

    <aside class="sidebar glass">
      <div class="side-top">
        <div class="brand">
          <div>
            <h1>LinkUp</h1>
            <small>clean, fast, and less cramped</small>
          </div>
          <div class="badge">LIVE</div>
        </div>
        <div class="pill">Room: <span id="roomLabel">#general</span></div>
      </div>

      <div class="section">
        <h3 class="section-title">Channels</h3>
        <div id="channels"></div>
      </div>

      <div class="footer-note">
        Press <span class="kbd">Enter</span> to send, <span class="kbd">Shift</span> + <span class="kbd">Enter</span> for a new line.
      </div>
    </aside>

    <main class="main glass">
      <div class="main-top">
        <div class="titleblock">
          <h2 id="channelTitle"># general</h2>
          <p id="channelDescription">A large room for real-time chat, with live presence and reconnects.</p>
          <div class="stats">
            <div class="stat">
              <b id="statMessages">0</b>
              <span>messages in room</span>
            </div>
            <div class="stat">
              <b id="statUsers">0</b>
              <span>online now</span>
            </div>
            <div class="stat">
              <b id="statPins">0</b>
              <span>pinned messages</span>
            </div>
            <div class="stat">
              <b id="statState">idle</b>
              <span>connection state</span>
            </div>
          </div>
        </div>
        <div class="topbar-tools">
          <input class="search" id="searchInput" placeholder="Search messages in this room..." />
          <button class="toolbtn" id="jumpPinsBtn">Pins</button>
          <div class="conn"><span class="dot"></span><span id="connState">connecting...</span></div>
        </div>
      </div>

      <div class="typing" id="typingLine"></div>
      <div class="messages" id="messages"></div>

      <div class="composer">
        <div class="composer-wrap">
          <textarea id="messageInput" placeholder="Write a message to LinkUp..."></textarea>
          <button class="send" id="sendBtn">Send</button>
        </div>
      </div>
    </main>

    <aside class="tools glass">
      <div class="panel">
        <h3>Your profile</h3>
        <p>Set your display name. Your browser keeps it saved.</p>
        <input class="input" id="nameInput" maxlength="24" placeholder="Your name" />
      </div>

      <div class="panel">
        <h3>Online now</h3>
        <ul class="list" id="members"></ul>
      </div>

      <div class="panel">
        <h3>Pinned messages</h3>
        <ul class="list" id="pins"></ul>
      </div>

      <div class="panel">
        <h3>What’s new</h3>
        <div class="updates">
          <div class="update"><b>No admin gimmicks</b><span>Nothing is auto-promoted, no first-user power abuse.</span></div>
          <div class="update"><b>Better UX</b><span>More space, bigger message flow, less visual clutter.</span></div>
          <div class="update"><b>Search + pins</b><span>Find messages fast and keep important ones up top.</span></div>
          <div class="update"><b>Replies + reactions</b><span>Talk like a real modern chat app.</span></div>
        </div>
      </div>

      <div class="tiny">
        This is now built like a product shell, not a tiny demo. Add auth + database next and it becomes persistent.
      </div>
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
    nameInput: document.getElementById("nameInput"),
    members: document.getElementById("members"),
    typingLine: document.getElementById("typingLine"),
    statMessages: document.getElementById("statMessages"),
    statUsers: document.getElementById("statUsers"),
    statPins: document.getElementById("statPins"),
    statState: document.getElementById("statState"),
    searchInput: document.getElementById("searchInput"),
    pins: document.getElementById("pins"),
    jumpPinsBtn: document.getElementById("jumpPinsBtn"),
  };

  const savedName = localStorage.getItem("linkup_name") || "You";
  const localClientId = localStorage.getItem("linkup_client_id") || ("cli_" + Math.random().toString(36).slice(2, 10));
  localStorage.setItem("linkup_client_id", localClientId);
  els.nameInput.value = savedName;

  let displayName = savedName;
  let selectedServer = localStorage.getItem("linkup_server") || "home";
  let selectedChannel = localStorage.getItem("linkup_channel") || "general";

  let socket = null;
  let connectionSeq = 0;
  let reconnectTimer = null;
  let handshakeTimer = null;
  let retryCount = 0;
  let typingTimeout = null;
  let activeReplyTo = null;

  const roomMessages = new Map();
  const roomSeen = new Map();
  const roomPresence = new Map();
  const roomPins = new Map();
  const pinnedIds = new Set();

  const systemSeeds = {
    general: [
      "Welcome to LinkUp. This room is live.",
      "Search, pin, reply, and react are enabled.",
      "No fake admin junk. Just chat features."
    ],
    announcements: ["Ship updates here.", "Use this room for changelogs."],
    showcase: ["Drop your best work here.", "Show your builds and screenshots."],
    frontend: ["UI, motion, and layout talk."],
    backend: ["FastAPI, WebSockets, and database design."],
    bugs: ["Log a bug, then squash it."],
    lobby: ["Find your squad."],
    clips: ["Funny moments belong here."],
    meta: ["Discuss game balance and tactics."],
    math: ["Study mode online."],
    coding: ["Code and conquer."],
    focus: ["Deep work zone."]
  };

  const emojiChoices = ["👍", "🔥", "😂", "👀", "💯", "❤️"];

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, s => ({
      "&":"&amp;",
      "<":"&lt;",
      ">":"&gt;",
      "\"":"&quot;",
      "'":"&#39;"
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
    els.statState.textContent = text.toLowerCase().slice(0, 10);
    els.connState.style.color = connected ? "#d7ffe9" : "";
  }

  function ensureRoom(room) {
    if (!roomMessages.has(room)) {
      const ch = room.split(":")[1] || "general";
      const seed = (systemSeeds[ch] || ["Room loaded."]).map((text, idx) => ({
        msg_id: `seed-${room}-${idx}`,
        kind: "system",
        name: "System",
        text,
        time: Date.now() - ((idx + 1) * 60000),
        room
      }));
      roomMessages.set(room, seed);
      roomSeen.set(room, new Set(seed.map(m => m.msg_id)));
      roomPresence.set(room, []);
      roomPins.set(room, []);
    }
  }

  function seenSet(room) {
    ensureRoom(room);
    return roomSeen.get(room);
  }

  function getRoomMessages(room) {
    ensureRoom(room);
    return roomMessages.get(room);
  }

  function getPresence(room) {
    ensureRoom(room);
    return roomPresence.get(room) || [];
  }

  function getPins(room) {
    ensureRoom(room);
    return roomPins.get(room) || [];
  }

  function myState(room) {
    const users = getPresence(room);
    return users.find(u => u.client_id === localClientId) || null;
  }

  function canPin() {
    return true;
  }

  function renderReactionBadges(reactions = {}) {
    const keys = Object.keys(reactions || {});
    if (!keys.length) return "";
    return keys.map(e => `<button class="chipbtn" data-emoji="${escapeHtml(e)}">${escapeHtml(e)} ${reactions[e]}</button>`).join("");
  }

  function renderMessageHtml(m) {
    if (m.kind === "system") {
      return `
        <div class="msg system" data-id="${escapeHtml(m.msg_id)}">
          <div class="avatar">!</div>
          <div>
            <div class="head">
              <div class="name"><span>System</span><span class="badge">notice</span></div>
              <span class="time">${fmtTime(m.time)}</span>
            </div>
            <div class="text">${escapeHtml(m.text)}</div>
          </div>
        </div>`;
    }

    const initial = (m.name || "?").trim().slice(0, 1).toUpperCase();
    const isSelf = m.client_id === localClientId;
    const reactions = m.reactions || {};
    const reply = m.reply_preview;

    return `
      <div class="msg" data-id="${escapeHtml(m.msg_id)}">
        <div class="avatar">${escapeHtml(initial)}</div>
        <div>
          <div class="head">
            <div class="name">
              <span>${escapeHtml(m.name || "Guest")}</span>
              <span class="badge">${isSelf ? "you" : "online"}</span>
            </div>
            <span class="time">${fmtTime(m.time)}</span>
          </div>
          ${reply ? `
            <div class="replybox">
              Replying to <b>${escapeHtml(reply.name || "Guest")}</b><br/>
              ${escapeHtml((reply.text || "").slice(0, 120))}
            </div>
          ` : ""}
          <div class="text">${escapeHtml(m.text || "")}</div>
          <div class="msg-actions">
            <button class="chipbtn" data-act="reply" data-id="${escapeHtml(m.msg_id)}">Reply</button>
            <button class="chipbtn" data-act="pin" data-id="${escapeHtml(m.msg_id)}">${pinnedIds.has(m.msg_id) ? "Unpin" : "Pin"}</button>
            <button class="chipbtn" data-act="react" data-id="${escapeHtml(m.msg_id)}">React</button>
            ${Object.keys(reactions).length ? renderReactionBadges(reactions) : ""}
          </div>
        </div>
      </div>`;
  }

  function renderMessages(room) {
    const query = (els.searchInput.value || "").trim().toLowerCase();
    const msgs = getRoomMessages(room).filter(m => {
      if (m.kind === "system") return true;
      if (!query) return true;
      return String(m.text || "").toLowerCase().includes(query) || String(m.name || "").toLowerCase().includes(query);
    });

    els.messages.innerHTML = msgs.map(renderMessageHtml).join("");
    els.statMessages.textContent = String(getRoomMessages(room).filter(m => m.kind !== "system").length);
    els.messages.scrollTop = els.messages.scrollHeight;

    bindMessageActions();
  }

  function updatePins(room) {
    const pins = getPins(room);
    els.pins.innerHTML = pins.length
      ? pins.slice().reverse().map(p => `
          <li class="user">
            <div class="user-main">
              <strong>${escapeHtml(p.name || "Guest")}</strong>
              <span>${escapeHtml((p.text || "").slice(0, 70))}</span>
            </div>
            <button class="mini" data-jump="${escapeHtml(p.msg_id)}">Jump</button>
          </li>
        `).join("")
      : `<li class="user"><div class="user-main"><strong>No pins yet</strong><span>Pin important messages so they stay visible.</span></div></li>`;

    els.statPins.textContent = String(pins.length);

    els.pins.querySelectorAll("button[data-jump]").forEach(btn => {
      btn.onclick = () => {
        const id = btn.dataset.jump;
        const node = els.messages.querySelector(`[data-id="${CSS.escape(id)}"]`);
        if (node) {
          node.scrollIntoView({ behavior: "smooth", block: "center" });
          node.style.outline = "1px solid rgba(123,140,255,.42)";
          setTimeout(() => { node.style.outline = ""; }, 1200);
        }
      };
    });
  }

  function addMessage(room, msg, renderNow = true) {
    ensureRoom(room);

    if (!msg.msg_id) {
      msg.msg_id = "msg_" + Math.random().toString(36).slice(2, 14);
    }

    const seen = seenSet(room);
    if (seen.has(msg.msg_id)) return;
    seen.add(msg.msg_id);

    msg.reactions = msg.reactions || {};
    const list = roomMessages.get(room);
    list.push(msg);

    if (room === roomId() && renderNow) {
      els.messages.insertAdjacentHTML("beforeend", renderMessageHtml(msg));
      els.messages.scrollTop = els.messages.scrollHeight;
      els.statMessages.textContent = String(list.filter(m => m.kind !== "system").length);
      bindMessageActions();
    }
  }

  function renderServers() {
    els.servers.innerHTML = servers.map(s => `
      <button class="server-btn ${s.id === selectedServer ? "active" : ""}" data-server="${s.id}" title="${s.label}">
        ${s.name}
      </button>
    `).join("");

    els.servers.querySelectorAll("button").forEach(btn => {
      btn.onclick = () => {
        const nextServer = btn.dataset.server;
        if (nextServer === selectedServer) return;
        selectedServer = nextServer;
        selectedChannel = channelsByServer[selectedServer][0].id;
        activeReplyTo = null;
        localStorage.setItem("linkup_server", selectedServer);
        localStorage.setItem("linkup_channel", selectedChannel);
        renderAll();
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
        const nextChannel = btn.dataset.channel;
        if (nextChannel === selectedChannel) return;
        selectedChannel = nextChannel;
        activeReplyTo = null;
        localStorage.setItem("linkup_channel", selectedChannel);
        renderAll();
        connectSocket(true);
      };
    });
  }

  function updateProfileBox() {
    const me = myState(roomId());
    const label = me ? `online • ${me.name}` : "offline in room";
    document.getElementById("roomLabel").textContent = `#${selectedChannel}`;
    els.statUsers.textContent = String(getPresence(roomId()).length);
    document.getElementById("channelTitle").textContent = currentChannel()?.title || "# general";
    document.getElementById("channelDescription").textContent = currentChannel()?.description || "A large room for real-time chat.";
  }

  function renderMembers() {
    const roomUsers = getPresence(roomId());
    const me = myState(roomId());

    document.getElementById("meName").textContent = displayName;
    document.getElementById("meRoleLine").textContent = me ? "online now" : "connecting...";
    document.getElementById("meRoleBadge").textContent = me ? "live" : "guest";

    els.members.innerHTML = roomUsers.length
      ? roomUsers.map(u => {
          const isSelf = u.client_id === localClientId;
          return `
            <li class="user">
              <div class="user-main">
                <strong>${escapeHtml(u.name)} ${isSelf ? "• you" : ""}</strong>
                <span>connected</span>
              </div>
              <span class="badge">online</span>
            </li>
          `;
        }).join("")
      : `<li class="user"><div class="user-main"><strong>No one else yet</strong><span>Be the first in this room.</span></div><span class="badge">0</span></li>`;
  }

  function setHeader() {
    const ch = currentChannel();
    els.channelTitle.textContent = ch ? ch.title : "# general";
    els.channelDescription.textContent = ch ? ch.description : "A large room for real-time chat, with live presence and reconnects.";
    els.roomLabel.textContent = `#${selectedChannel}`;
    document.title = `LinkUp — ${ch ? ch.title : "# general"}`;
  }

  function renderAll() {
    renderServers();
    renderChannels();
    setHeader();
    updateProfileBox();
    renderMembers();
    updatePins(roomId());
    renderMessages(roomId());
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
    }, 25);
  }

  function connectSocket(force = false) {
    cleanupSocketTimers();
    const seq = ++connectionSeq;
    const room = roomId();
    let opened = false;

    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      try { socket.close(1000, "switching-room"); } catch {}
    }

    roomPresence.set(room, []);
    els.typingLine.textContent = "";
    setStatus("connecting...");

    try {
      socket = new WebSocket(wsUrl(room));
    } catch (err) {
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
      try {
        data = JSON.parse(event.data);
      } catch {
        data = null;
      }
      if (!data) return;

      if (data.kind === "history") {
        const history = Array.isArray(data.messages) ? data.messages : [];
        const users = Array.isArray(data.users) ? data.users : [];
        const pins = Array.isArray(data.pins) ? data.pins : [];

        const seen = new Set();
        roomMessages.set(room, []);
        roomSeen.set(room, seen);
        roomPresence.set(room, users);
        roomPins.set(room, pins);
        pinnedIds.clear();
        pins.forEach(p => pinnedIds.add(p.msg_id));

        history.forEach(m => {
          if (!m.msg_id) m.msg_id = "hist_" + Math.random().toString(36).slice(2, 12);
          seen.add(m.msg_id);
          if (!m.reactions) m.reactions = {};
          roomMessages.get(room).push(m);
        });

        renderMembers();
        updatePins(room);
        renderMessages(room);
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
          typingTimeout = setTimeout(() => {
            els.typingLine.textContent = "";
          }, 1200);
        }
        return;
      }

      if (data.kind === "system") {
        addMessage(room, data, true);
        return;
      }

      if (data.kind === "pin_update") {
        const msgId = data.message?.msg_id;
        if (data.action === "pinned" && msgId) pinnedIds.add(msgId);
        if (data.action === "unpinned" && msgId) pinnedIds.delete(msgId);

        const pins = Array.isArray(data.pins) ? data.pins : [];
        roomPins.set(room, pins);
        updatePins(room);
        renderMessages(room);
        return;
      }

      if (data.kind === "reaction_update") {
        const list = roomMessages.get(room) || [];
        const target = list.find(m => m.msg_id === data.msg_id);
        if (target) {
          target.reactions = data.reactions || {};
          renderMessages(room);
        }
        return;
      }

      if (data.kind === "message") {
        data.time = data.time || Date.now();
        data.client_id = data.client_id || "remote";
        data.msg_id = data.msg_id || `srv_${room}_${data.time}_${Math.random().toString(36).slice(2, 8)}`;
        if (!data.reactions) data.reactions = {};
        addMessage(room, data, true);
      }
    };

    socket.onerror = () => {
      if (seq !== connectionSeq) return;
      if (!opened) setStatus("connection error");
    };

    socket.onclose = () => {
      if (seq !== connectionSeq) return;
      cleanupSocketTimers();

      if (opened) {
        setStatus("disconnected");
      } else {
        setStatus("offline");
      }

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
      reply_to: activeReplyTo,
      time: Date.now()
    };

    const replyNode = activeReplyTo ? roomMessages.get(roomId())?.find(m => m.msg_id === activeReplyTo) : null;
    if (replyNode) {
      payload.reply_preview = {
        msg_id: replyNode.msg_id,
        name: replyNode.name,
        text: replyNode.text
      };
    }

    addMessage(roomId(), payload, true);

    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
    }

    activeReplyTo = null;
    els.messageInput.value = "";
    els.messageInput.style.height = "72px";
    els.messageInput.focus();
  }

  function bindMessageActions() {
    document.querySelectorAll(".msg-actions .chipbtn").forEach(btn => {
      btn.onclick = () => {
        const room = roomId();
        const act = btn.dataset.act;
        const msgId = btn.dataset.id;

        if (act === "reply") {
          activeReplyTo = msgId;
          const node = roomMessages.get(room)?.find(m => m.msg_id === msgId);
          if (node) {
            els.messageInput.placeholder = `Replying to ${node.name}...`;
            els.messageInput.focus();
          }
          return;
        }

        if (act === "pin") {
          if (!canPin()) return;
          if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({
              kind: "pin",
              client_id: localClientId,
              name: displayName,
              room,
              msg_id: msgId,
              time: Date.now()
            }));
          }
          return;
        }

        if (act === "react") {
          const emoji = prompt(`Pick a reaction: ${emojiChoices.join(" ")}`, "👍");
          if (!emoji) return;
          if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({
              kind: "reaction",
              client_id: localClientId,
              name: displayName,
              room,
              msg_id: msgId,
              emoji: emoji.trim().slice(0, 4),
              time: Date.now()
            }));
          }
          return;
        }
      };
    });
  }

  els.nameInput.addEventListener("change", () => {
    displayName = (els.nameInput.value || "You").trim().slice(0, 24) || "You";
    localStorage.setItem("linkup_name", displayName);

    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({
        kind: "rename",
        client_id: localClientId,
        name: displayName,
        room: roomId(),
        time: Date.now()
      }));
    }

    updateProfileBox();
  });

  els.sendBtn.onclick = sendMessage;

  els.messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  els.messageInput.addEventListener("input", () => {
    els.messageInput.style.height = "auto";
    els.messageInput.style.height = Math.min(220, els.messageInput.scrollHeight) + "px";
    sendTyping();
  });

  els.searchInput.addEventListener("input", () => {
    renderMessages(roomId());
  });

  els.jumpPinsBtn.addEventListener("click", () => {
    const firstPin = els.pins.querySelector("button[data-jump]");
    if (firstPin) firstPin.click();
  });

  window.addEventListener("online", () => {
    setStatus("back online");
    connectSocket(true);
  });

  window.addEventListener("offline", () => {
    setStatus("offline");
  });

  renderAll();
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
