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
                    await rooms.system(room_id, f"{display_name} joined the server.")
                await rooms.send_history(room_id, websocket)
                await rooms.send_presence(room_id)
                continue

            if kind == "rename":
                old_name = display_name
                client_id = incoming_client_id
                display_name = incoming_name
                rooms.register(room_id, client_id, display_name, websocket)
                await rooms.system(room_id, f"**{old_name}** is now known as **{display_name}**.")
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
                if result["action"] == "pinned":
                    await rooms.system(room_id, f"**{display_name}** pinned a message.")
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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1, user-scalable=0" />
  <meta name="theme-color" content="#313338" />
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
      --accent-hover:#4752c4;
      --accent-2:#23a559;
      --danger:#da373c;
      --shadow:0 8px 16px rgba(0,0,0,.24);
      --radius:8px;
      color-scheme: dark;
    }
    
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: var(--panel); }
    ::-webkit-scrollbar-thumb { background: #1a1b1e; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #111214; }

    *{box-sizing:border-box}
    html,body{height:100%}
    body{
      margin:0;
      padding-top: env(safe-area-inset-top);
      padding-bottom: env(safe-area-inset-bottom);
      padding-left: env(safe-area-inset-left);
      padding-right: env(safe-area-inset-right);
      font-family: 'gg sans', 'Noto Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif;
      background:var(--bg);
      color:var(--text);
      overflow:hidden;
    }
    button,input,textarea{font:inherit}

    .app{
      height:100%;
      display:grid;
      grid-template-columns: 72px 240px minmax(0,1fr) 268px;
      min-width:0;
    }

    .servers{
      background:var(--panel-2);
      padding:12px 0;
      display:flex;
      flex-direction:column;
      gap:8px;
      align-items:center;
      overflow-y:auto;
    }
    .server-btn{
      width:48px;
      height:48px;
      border:none;
      border-radius:24px;
      cursor:pointer;
      background:var(--bg);
      color:var(--text);
      font-weight:700;
      font-size: 16px;
      transition: all .2s ease;
      box-shadow:none;
      flex:0 0 auto;
      position: relative;
    }
    .server-btn:hover{
      background:var(--accent);
      border-radius:16px;
      color: #fff;
    }
    .server-btn.active{
      background:var(--accent);
      border-radius:16px;
      color: #fff;
    }
    .server-btn::before {
      content: "";
      position: absolute;
      left: -12px;
      top: 50%;
      transform: translateY(-50%) scale(0);
      width: 8px;
      height: 8px;
      background: #fff;
      border-radius: 4px;
      transition: all .2s ease;
    }
    .server-btn.active::before {
      height: 40px;
      transform: translateY(-50%) scale(1);
    }

    .sidebar{
      background:var(--panel);
      display:flex;
      flex-direction:column;
      min-width:0;
      overflow:hidden;
    }
    .side-top{
      padding:16px;
      border-bottom:1px solid #1e1f22;
      flex:0 0 auto;
      box-shadow: 0 1px 2px rgba(0,0,0,.2);
      z-index: 2;
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
      font-weight: 800;
      line-height:1.2;
    }
    .brand small{
      display:block;
      margin-top:2px;
      color:var(--muted);
      font-size:12px;
    }
    .section{
      padding:16px 8px 8px;
      overflow-y:auto;
      min-height:0;
      flex:1 1 auto;
    }
    .section-title{
      margin:0 8px 4px;
      color:var(--muted);
      font-size:12px;
      font-weight:700;
      text-transform:uppercase;
      letter-spacing:.02em;
    }
    .channel{
      width:100%;
      border:none;
      border-radius:4px;
      padding:6px 8px;
      margin-bottom:2px;
      cursor:pointer;
      background:transparent;
      color:#80848e;
      display:flex;
      align-items:center;
      gap:6px;
      text-align:left;
      transition: background .15s ease, color .15s ease;
    }
    .channel:hover{
      background:rgba(78,80,88,.6);
      color: #dbdee1;
    }
    .channel.active{
      background:rgba(78,80,88,.6);
      color: #fff;
    }
    .channel .hash {
      font-size: 18px;
      color: #80848e;
      font-weight: light;
      margin-right: 2px;
    }
    .channel.active .hash { color: #fff; }
    .channel .meta strong{
      font-size:15px;
      font-weight: 500;
    }

    .user-controls {
      background: #232428;
      padding: 8px;
      flex: 0 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .user-controls .user-info {
      display: flex;
      align-items: center;
      gap: 8px;
      border-radius: 4px;
      padding: 4px;
      flex: 1;
      min-width: 0;
      cursor: pointer;
    }
    .user-controls .user-info:hover {
      background: rgba(255,255,255,.05);
    }
    .user-controls .avatar {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: var(--accent);
      color: #fff;
      display: grid;
      place-items: center;
      font-weight: 700;
      font-size: 14px;
      position: relative;
    }
    .user-controls .avatar .status {
      position: absolute;
      bottom: 0;
      right: 0;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--accent-2);
      border: 2px solid #232428;
    }
    .user-controls .user-text {
      display: flex;
      flex-direction: column;
      min-width: 0;
    }
    .user-controls .user-text strong {
      font-size: 14px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .user-controls .user-text span {
      font-size: 11px;
      color: var(--muted);
    }
    .user-controls button {
      background: transparent;
      border: none;
      color: var(--muted);
      cursor: pointer;
      width: 32px;
      height: 32px;
      border-radius: 4px;
      display: grid;
      place-items: center;
      transition: .1s;
    }
    .user-controls button:hover {
      background: rgba(255,255,255,.1);
      color: #fff;
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
      border-bottom:1px solid #1e1f22;
      background:var(--bg);
      box-shadow: 0 1px 2px rgba(0,0,0,.15);
      z-index:3;
    }
    .titleblock{
      display:flex;
      align-items:center;
      gap:12px;
      min-width:0;
    }
    .titleblock .hash {
      color: #80848e;
      font-size: 24px;
      font-weight: 300;
    }
    .titleblock h2{
      margin:0;
      font-size:16px;
      font-weight: 700;
      white-space:nowrap;
    }
    .titleblock .divider {
      width: 1px;
      height: 24px;
      background: #3f4147;
      margin: 0 4px;
    }
    .titleblock p{
      margin:0;
      color:var(--muted);
      font-size:14px;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
      max-width:35vw;
    }
    .top-actions {
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .search-container {
      position: relative;
    }
    .search{
      width: 144px;
      padding: 4px 8px;
      border-radius: 4px;
      border: none;
      outline: none;
      background: #1e1f22;
      color: var(--text);
      font-size: 14px;
      transition: width .2s ease;
    }
    .search:focus {
      width: 240px;
    }

    .typing{
      position: absolute;
      bottom: 85px;
      left: 16px;
      font-weight: 600;
      color: #dbdee1;
      font-size: 14px;
      z-index: 10;
      pointer-events: none;
      text-shadow: 0 0 4px var(--bg);
    }

    .messages{
      flex:1 1 auto;
      min-height:0;
      overflow-y:auto;
      padding:16px 0 24px;
    }

    .msg{
      display:grid;
      grid-template-columns: 48px minmax(0,1fr);
      padding:4px 16px 4px 16px;
      position:relative;
      margin-top: 16px;
    }
    .msg.compact {
      margin-top: 0;
    }
    .msg:hover{
      background:rgba(2,2,2,.04);
    }
    .msg:hover .msg-actions {
      opacity: 1;
    }
    .avatar-wrapper {
      width:40px;
      height:40px;
      border-radius:50%;
      background:var(--accent);
      color:#fff;
      display:grid;
      place-items:center;
      font-weight:700;
      font-size:16px;
      cursor: pointer;
    }
    .avatar-wrapper:hover {
      opacity: 0.9;
    }
    .compact .avatar-wrapper {
      display: none;
    }
    .compact-time {
      display: none;
      font-size: 10px;
      color: #80848e;
      text-align: right;
      padding-right: 4px;
      line-height: 22px;
    }
    .compact:hover .compact-time {
      display: block;
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
      margin-bottom: 2px;
    }
    .compact .header {
      display: none;
    }
    .name{
      font-weight:500;
      font-size:16px;
      color:#fff;
      cursor: pointer;
    }
    .name:hover {
      text-decoration: underline;
    }
    .time{
      color:#80848e;
      font-size:12px;
    }
    .text{
      white-space:pre-wrap;
      word-break:break-word;
      line-height:1.375rem;
      font-size:16px;
      color:#dbdee1;
    }
    .text code {
      font-family: Consolas, monospace;
      background: #1e1f22;
      padding: 2px 4px;
      border-radius: 4px;
      font-size: 14px;
    }
    .text pre {
      background: #1e1f22;
      padding: 8px;
      border-radius: 4px;
      overflow-x: auto;
      border: 1px solid #111214;
    }
    .text b { font-weight: 700; color: #fff; }
    .text i { font-style: italic; }
    
    .replybox{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 4px;
      color: #b5bac1;
      font-size: 14px;
      position: relative;
    }
    .replybox::before {
      content: "";
      display: block;
      width: 24px;
      height: 12px;
      border-left: 2px solid #4f545c;
      border-top: 2px solid #4f545c;
      border-top-left-radius: 6px;
      margin-right: 4px;
      transform: translateY(4px);
    }
    .replybox .rep-avatar {
      width: 16px; height: 16px; border-radius: 50%; background: var(--accent); display: inline-block;
    }

    .msg-actions {
      position: absolute;
      right: 16px;
      top: -12px;
      background: #313338;
      border: 1px solid #1e1f22;
      border-radius: 4px;
      display: flex;
      opacity: 0;
      transition: opacity .1s ease;
      box-shadow: 0 0 0 1px rgba(0,0,0,.05), 0 2px 4px rgba(0,0,0,.1);
      z-index: 5;
    }
    .msg-actions button {
      background: transparent;
      border: none;
      color: var(--muted);
      padding: 4px 8px;
      cursor: pointer;
      display: grid;
      place-items: center;
    }
    .msg-actions button:hover {
      background: rgba(78,80,88,.6);
      color: #dbdee1;
    }
    
    .reactions {
      display:flex;
      gap:4px;
      flex-wrap:wrap;
      margin-top:4px;
    }
    .reaction {
      background: #2b2d31;
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 2px 6px;
      display: flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      font-size: 14px;
      color: var(--muted);
    }
    .reaction:hover {
      border-color: #5865f2;
    }
    .reaction.reacted {
      background: rgba(88,101,242,.15);
      border-color: #5865f2;
    }

    .system{
      color:#80848e;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 16px;
    }
    .system .sys-icon {
      color: var(--accent-2);
      font-size: 18px;
      margin-left: 20px;
    }
    .system .text{
      color:#80848e;
      font-size: 15px;
    }
    .system .text b { color: #dbdee1; }

    .composer{
      flex:0 0 auto;
      padding:0 16px 24px;
      background:var(--bg);
      position: relative;
    }
    .composer-wrap{
      display:flex;
      align-items:flex-start;
      gap:10px;
      padding:10px 16px;
      border-radius:8px;
      background:#383a40;
    }
    .composer textarea{
      flex:1;
      min-height:24px;
      max-height:400px;
      height:24px;
      resize:none;
      overflow-y:auto;
      border:none;
      outline:none;
      background:transparent;
      color:var(--text);
      line-height:1.375rem;
      font-size:16px;
      padding:0;
    }
    .send{
      background: transparent;
      border: none;
      color: var(--muted);
      cursor: pointer;
      display: grid;
      place-items: center;
      padding: 0;
    }
    .send:hover { color: #fff; }

    .members{
      background:var(--panel);
      display:flex;
      flex-direction:column;
      min-width:0;
      overflow:hidden;
    }
    .members-top{
      padding:24px 16px 8px;
      flex:0 0 auto;
    }
    .members-top h3{
      margin:0;
      font-size:12px;
      font-weight: 700;
      text-transform:uppercase;
      letter-spacing:.02em;
      color:var(--muted);
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
      gap:12px;
      padding:6px 8px;
      border-radius:4px;
      color:var(--muted);
      cursor: pointer;
    }
    .member:hover{background:rgba(78,80,88,.6); color: #dbdee1;}
    .member .avatar {
      width: 32px; height: 32px; border-radius: 50%; background: var(--accent); color: #fff; display: grid; place-items: center; font-size: 14px; font-weight: 700; position: relative;
    }
    .member .avatar .status {
      position: absolute; bottom: -2px; right: -2px; width: 12px; height: 12px; background: var(--accent-2); border: 2px solid var(--panel); border-radius: 50%;
    }
    .member .name-wrap{
      min-width:0;
    }
    .member .name-wrap strong{
      display:block;
      font-size:15px;
      font-weight: 500;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .modal-overlay {
      position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,.7);
      display: none; place-items: center; z-index: 100;
    }
    .modal-overlay.active { display: grid; }
    .modal {
      background: #313338;
      border-radius: 8px;
      width: 440px;
      max-width: 90vw;
      box-shadow: var(--shadow);
      display: flex;
      flex-direction: column;
    }
    .modal-header {
      padding: 24px 24px 0;
      text-align: center;
    }
    .modal-header h2 { margin: 0; font-size: 24px; font-weight: 700; color: #f2f3f5; }
    .modal-header p { margin: 8px 0 0; color: #b5bac1; font-size: 15px; }
    .modal-body {
      padding: 24px;
    }
    .modal-body label {
      display: block; margin-bottom: 8px; color: #b5bac1; font-size: 12px; font-weight: 700; text-transform: uppercase;
    }
    .modal-body input {
      width: 100%; padding: 10px; background: #1e1f22; border: none; border-radius: 4px; color: #dbdee1; font-size: 16px; margin-bottom: 20px;
    }
    .modal-footer {
      padding: 16px 24px;
      background: #2b2d31;
      border-radius: 0 0 8px 8px;
      display: flex;
      justify-content: flex-end;
      gap: 12px;
    }
    .btn { padding: 10px 24px; border-radius: 4px; border: none; font-size: 14px; font-weight: 500; cursor: pointer; transition: .15s; }
    .btn-cancel { background: transparent; color: #fff; }
    .btn-cancel:hover { text-decoration: underline; }
    .btn-primary { background: var(--accent); color: #fff; }
    .btn-primary:hover { background: var(--accent-hover); }

    .emoji-picker {
      position: absolute;
      background: #2b2d31;
      border: 1px solid #1e1f22;
      border-radius: 8px;
      padding: 8px;
      display: none;
      gap: 8px;
      box-shadow: var(--shadow);
      z-index: 50;
    }
    .emoji-picker.active { display: flex; }
    .emoji-picker span {
      font-size: 24px; cursor: pointer; padding: 4px; border-radius: 4px; transition: background .1s;
    }
    .emoji-picker span:hover { background: rgba(255,255,255,.1); }

    @media (max-width: 1100px){
      .app{grid-template-columns:72px 240px minmax(0,1fr);}
      .members{display:none}
      .titleblock p{display:none}
    }
    @media (max-width: 820px){
      .app{
        height: 100vh;
        grid-template-columns:1fr;
        grid-template-rows: auto 1fr;
      }
      .servers,.sidebar,.members{ display:none; }
      .search{width: 100px;}
      .search:focus { width: 140px; }
      .msg { padding: 4px 8px; }
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
          </div>
        </div>
      </div>

      <div class="section">
        <div class="section-title">Channels</div>
        <div id="channels"></div>
      </div>

      <div class="user-controls">
         <div class="user-info" id="userProfileBtn">
           <div class="avatar"><span id="myAvatarInitial">U</span><div class="status"></div></div>
           <div class="user-text">
             <strong id="myNameDisplay">You</strong>
             <span>#<span id="myIdDisplay">0000</span></span>
           </div>
         </div>
         <button id="settingsBtn" title="User Settings">⚙️</button>
      </div>
    </aside>

    <main class="main">
      <div class="main-top">
        <div class="titleblock">
          <span class="hash">#</span>
          <h2 id="channelTitle">general</h2>
          <div class="divider"></div>
          <p id="channelDescription">A big, readable room for real-time chat.</p>
        </div>
        <div class="top-actions">
           <div class="search-container">
             <input class="search" id="searchInput" placeholder="Search" />
           </div>
           <div id="connState" style="color:var(--muted); font-size: 12px;">connecting...</div>
        </div>
      </div>

      <div class="messages" id="messages"></div>

      <div class="typing" id="typingLine"></div>

      <div class="composer">
        <div class="composer-wrap">
          <textarea id="messageInput" placeholder="Message #general"></textarea>
          <button class="send" id="sendBtn" title="Send message">
             <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
          </button>
        </div>
      </div>
    </main>

    <aside class="members">
      <div class="members-top">
        <h3>Online — <span id="statUsers">0</span></h3>
      </div>
      <div class="memberlist" id="members"></div>
    </aside>
  </div>

  <div class="modal-overlay" id="settingsModal">
     <div class="modal">
        <div class="modal-header">
           <h2>User Settings</h2>
           <p>Change how you appear to others.</p>
        </div>
        <div class="modal-body">
           <label>Display Name</label>
           <input type="text" id="nameChangeInput" placeholder="Enter new name" />
        </div>
        <div class="modal-footer">
           <button class="btn btn-cancel" id="closeSettings">Cancel</button>
           <button class="btn btn-primary" id="saveSettings">Save Changes</button>
        </div>
     </div>
  </div>

  <div class="emoji-picker" id="emojiPicker">
     <span>👍</span><span>❤️</span><span>😂</span><span>🔥</span><span>👀</span><span>💯</span>
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
      { id: "general", title: "general", description: "Main feed for everything LinkUp." },
      { id: "announcements", title: "announcements", description: "Product updates, releases, and changelogs." },
      { id: "showcase", title: "showcase", description: "Drop cool projects, screenshots, and wins." }
    ],
    dev: [
      { id: "frontend", title: "frontend", description: "UI, components, and layout experiments." },
      { id: "backend", title: "backend", description: "APIs, database, auth, and server logic." },
      { id: "bugs", title: "bugs", description: "Report issues and weird behavior here." }
    ],
    games: [
      { id: "lobby", title: "lobby", description: "Find players and set up sessions." },
      { id: "clips", title: "clips", description: "Highlights, funny moments, and screenshots." },
      { id: "meta", title: "meta", description: "Discuss builds, balance, and strategy." }
    ],
    study: [
      { id: "math", title: "math", description: "Problem solving and homework help." },
      { id: "coding", title: "coding", description: "Programming help and pair debugging." },
      { id: "focus", title: "focus", description: "Quiet study time and accountability." }
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
    members: document.getElementById("members"),
    typingLine: document.getElementById("typingLine"),
    statUsers: document.getElementById("statUsers"),
    searchInput: document.getElementById("searchInput"),
    
    myAvatarInitial: document.getElementById("myAvatarInitial"),
    myNameDisplay: document.getElementById("myNameDisplay"),
    myIdDisplay: document.getElementById("myIdDisplay"),
    settingsBtn: document.getElementById("settingsBtn"),
    userProfileBtn: document.getElementById("userProfileBtn"),
    settingsModal: document.getElementById("settingsModal"),
    nameChangeInput: document.getElementById("nameChangeInput"),
    closeSettings: document.getElementById("closeSettings"),
    saveSettings: document.getElementById("saveSettings"),
    emojiPicker: document.getElementById("emojiPicker"),
  };

  const localClientId = localStorage.getItem("linkup_client_id") || ("cli_" + Math.random().toString(36).slice(2, 10));
  localStorage.setItem("linkup_client_id", localClientId);
  let savedName = localStorage.getItem("linkup_name") || "Guest";

  let displayName = savedName;
  let selectedServer = localStorage.getItem("linkup_server") || "home";
  let selectedChannel = localStorage.getItem("linkup_channel") || "general";

  els.myIdDisplay.textContent = localClientId.slice(-4);

  let socket = null;
  let connectionSeq = 0;
  let reconnectTimer = null;
  let handshakeTimer = null;
  let retryCount = 0;
  let typingTimeout = null;
  let replyTo = null;
  let activeEmojiTarget = null;
  let lastMessageSender = null;
  let lastMessageTime = 0;

  const roomMessages = new Map();
  const roomSeen = new Map();
  const roomPresence = new Map();
  const reactionMap = new Map();

  function updateUserInfo() {
    els.myNameDisplay.textContent = displayName;
    els.myAvatarInitial.textContent = displayName.charAt(0).toUpperCase();
    localStorage.setItem("linkup_name", displayName);
  }
  updateUserInfo();

  function parseMarkdown(text) {
    let esc = String(text).replace(/[&<>"']/g, s => ({"&": "&amp;","<": "&lt;",">": "&gt;","\"": "&quot;","'": "&#39;"}[s]));
    esc = esc.replace(/\*\*(.*?)\*\*/g, '<b>$1</b>');
    esc = esc.replace(/\*(.*?)\*/g, '<i>$1</i>');
    esc = esc.replace(/`(.*?)`/g, '<code>$1</code>');
    esc = esc.replace(/```([\s\S]*?)```/g, '<pre>$1</pre>');
    return esc;
  }

  function fmtTime(ts) {
    return new Date(ts || Date.now()).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function fmtDate(ts) {
    return new Date(ts || Date.now()).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
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
    els.connState.style.color = connected ? "#23a559" : "var(--muted)";
  }

  function ensureRoom(room) {
    if (!roomMessages.has(room)) {
      roomMessages.set(room, []);
      roomSeen.set(room, new Set());
      roomPresence.set(room, []);
      reactionMap.set(room, new Map());
    }
  }

  function currentUsers() {
    ensureRoom(roomId());
    return roomPresence.get(roomId()) || [];
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
        <span class="hash">#</span>
        <div class="meta">
          <strong>${ch.title}</strong>
        </div>
      </button>
    `).join("");

    els.channels.querySelectorAll("button").forEach(btn => {
      btn.onclick = () => {
        selectedChannel = btn.dataset.channel;
        replyTo = null;
        localStorage.setItem("linkup_channel", selectedChannel);
        els.messageInput.placeholder = `Message #${selectedChannel}`;
        renderShell();
        connectSocket(true);
      };
    });
  }

  function renderTop() {
    const ch = currentChannel();
    els.channelTitle.textContent = ch ? ch.title : "general";
    els.channelDescription.textContent = ch ? ch.description : "";
    els.messageInput.placeholder = `Message #${selectedChannel}`;
  }

  function renderMembers() {
    const users = currentUsers();
    els.statUsers.textContent = String(users.length);

    els.members.innerHTML = users.length ? users.map(u => `
      <div class="member">
        <div class="avatar">${(u.name||"?").charAt(0).toUpperCase()}<div class="status"></div></div>
        <div class="name-wrap">
          <strong>${parseMarkdown(u.name)}</strong>
        </div>
      </div>
    `).join("") : `<div class="member" style="color:var(--muted)">No one else yet</div>`;
  }

  function renderMessageHtml(m, isCompact = false) {
    const reactions = m.reactions || {};
    const reactionKeys = Object.keys(reactions);
    const initial = (m.name || "?").trim().slice(0, 1).toUpperCase();

    if (m.kind === "system") {
      return `
        <div class="system" data-id="${parseMarkdown(m.msg_id)}">
          <div class="sys-icon">➜</div>
          <div class="text">${parseMarkdown(m.text)}</div>
        </div>
      `;
    }

    return `
      <div class="msg ${isCompact ? 'compact' : ''}" data-id="${parseMarkdown(m.msg_id)}">
        ${isCompact ? `<div class="compact-time">${fmtTime(m.time)}</div>` : `<div class="avatar-wrapper">${initial}</div>`}
        <div class="content">
          ${!isCompact ? `
          <div class="header">
            <div class="name">${parseMarkdown(m.name || "Guest")}</div>
            <div class="time">${fmtDate(m.time)} ${fmtTime(m.time)}</div>
          </div>
          ` : ''}
          ${m.reply_preview ? `
            <div class="replybox">
              <div class="rep-avatar"></div>
              <span><b>${parseMarkdown(m.reply_preview.name || "Guest")}</b> ${parseMarkdown((m.reply_preview.text || "").slice(0, 60))}</span>
            </div>
          ` : ""}
          <div class="text">${parseMarkdown(m.text || "")}</div>
          <div class="reactions">
            ${reactionKeys.map(e => `
              <div class="reaction ${reactions[e] > 0 ? 'reacted' : ''}" data-action="react" data-id="${parseMarkdown(m.msg_id)}" data-emoji="${e}">
                ${e} <span>${reactions[e]}</span>
              </div>
            `).join("")}
          </div>
          <div class="msg-actions">
            <button data-action="react-prompt" data-id="${parseMarkdown(m.msg_id)}" title="Add Reaction">😊</button>
            <button data-action="reply" data-id="${parseMarkdown(m.msg_id)}" title="Reply">↩️</button>
            <button data-action="pin" data-id="${parseMarkdown(m.msg_id)}" title="Pin">📌</button>
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
    let html = "";
    lastMessageSender = null;
    lastMessageTime = 0;

    msgs.forEach(m => {
      const isCompact = (m.kind === "message" && m.client_id === lastMessageSender && (m.time - lastMessageTime < 300000) && !m.reply_preview);
      html += renderMessageHtml(m, isCompact);
      if (m.kind === "message") {
        lastMessageSender = m.client_id;
        lastMessageTime = m.time;
      } else {
        lastMessageSender = null;
      }
    });

    els.messages.innerHTML = html;
    els.messages.scrollTop = els.messages.scrollHeight;
    bindMessageActions();
  }

  function renderShell() {
    lastMessageSender = null;
    renderServers();
    renderChannels();
    renderTop();
    renderMembers();
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
      const isCompact = (msg.kind === "message" && msg.client_id === lastMessageSender && (msg.time - lastMessageTime < 300000) && !msg.reply_preview);
      els.messages.insertAdjacentHTML("beforeend", renderMessageHtml(msg, isCompact));
      els.messages.scrollTop = els.messages.scrollHeight;
      if (msg.kind === "message") {
        lastMessageSender = msg.client_id;
        lastMessageTime = msg.time;
      } else {
        lastMessageSender = null;
      }
      bindMessageActions();
    }
  }

  function wsUrl(room) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/${encodeURIComponent(room)}`;
  }

  function cleanupSocketTimers() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (handshakeTimer) clearTimeout(handshakeTimer);
    reconnectTimer = null;
    handshakeTimer = null;
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

    try { socket = new WebSocket(wsUrl(room)); } catch {
      setStatus("socket unavailable"); scheduleReconnect(seq); return;
    }

    handshakeTimer = setTimeout(() => {
      if (seq !== connectionSeq) return;
      if (!opened) setStatus("connecting... still trying");
    }, 4000);

    socket.onopen = () => {
      if (seq !== connectionSeq) { try { socket.close(1000, "stale"); } catch {} return; }
      opened = true; retryCount = 0; cleanupSocketTimers();
      setStatus("connected", true);
      socket.send(JSON.stringify({ kind: "hello", client_id: localClientId, name: displayName, room, time: Date.now() }));
    };

    socket.onmessage = (event) => {
      if (seq !== connectionSeq) return;
      let data = null;
      try { data = JSON.parse(event.data); } catch { return; }
      if (!data) return;

      if (data.kind === "history") {
        roomMessages.set(room, []); roomSeen.set(room, new Set());
        roomPresence.set(room, Array.isArray(data.users) ? data.users : []);
        (Array.isArray(data.messages) ? data.messages : []).forEach(m => {
          if (!m.msg_id) m.msg_id = "hist_" + Math.random().toString(36).slice(2, 12);
          seenSet(room).add(m.msg_id);
          if (!m.reactions) m.reactions = {};
          roomMessages.get(room).push(m);
        });
        renderMembers(); renderMessages(); return;
      }

      if (data.kind === "presence") {
        roomPresence.set(room, Array.isArray(data.users) ? data.users : []);
        renderMembers(); return;
      }

      if (data.kind === "typing") {
        if (data.client_id && data.client_id !== localClientId) {
          els.typingLine.textContent = `${data.name || "Someone"} is typing...`;
          clearTimeout(typingTimeout);
          typingTimeout = setTimeout(() => els.typingLine.textContent = "", 1500);
        }
        return;
      }

      if (data.kind === "system") { pushMessage(room, data, true); return; }

      if (data.kind === "reaction_update") {
        const target = roomList().find(m => m.msg_id === data.msg_id);
        if (target) { target.reactions = data.reactions || {}; renderMessages(); }
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

    socket.onerror = () => { if (seq === connectionSeq && !opened) setStatus("connection error"); };
    socket.onclose = () => { if (seq === connectionSeq) { cleanupSocketTimers(); setStatus(opened ? "disconnected" : "offline"); scheduleReconnect(seq); } };
  }

  function sendTyping() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ kind: "typing", client_id: localClientId, name: displayName, room: roomId(), time: Date.now() }));
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
      reply_to: replyTo,
      time: Date.now()
    };

    if (replyTo) {
      const original = roomList().find(m => m.msg_id === replyTo);
      if (original) payload.reply_preview = { msg_id: original.msg_id, name: original.name, text: original.text };
    }

    pushMessage(roomId(), payload, true);
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));

    replyTo = null;
    els.messageInput.placeholder = `Message #${selectedChannel}`;
    els.messageInput.value = "";
    els.messageInput.style.height = '24px';
    els.messageInput.focus();
  }

  function handleReaction(id, emoji) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ kind: "reaction", client_id: localClientId, name: displayName, room: roomId(), msg_id: id, emoji: emoji, time: Date.now() }));
  }

  function bindMessageActions() {
    els.messages.querySelectorAll("[data-action]").forEach(btn => {
      btn.onclick = (e) => {
        const action = btn.dataset.action;
        const id = btn.dataset.id;
        const room = roomId();

        if (action === "reply") {
          replyTo = id;
          const original = roomList().find(m => m.msg_id === id);
          els.messageInput.placeholder = original ? `Replying to ${original.name}` : "Replying...";
          els.messageInput.focus();
          return;
        }

        if (action === "pin") {
          if (!socket || socket.readyState !== WebSocket.OPEN) return;
          socket.send(JSON.stringify({ kind: "pin", client_id: localClientId, name: displayName, room, msg_id: id, time: Date.now() }));
          return;
        }

        if (action === "react") {
          handleReaction(id, btn.dataset.emoji);
          return;
        }

        if (action === "react-prompt") {
          const rect = btn.getBoundingClientRect();
          els.emojiPicker.style.top = `${rect.top - 40}px`;
          els.emojiPicker.style.left = `${rect.left - 150}px`;
          els.emojiPicker.classList.add("active");
          activeEmojiTarget = id;
          e.stopPropagation();
        }
      };
    });
  }

  document.addEventListener("click", (e) => {
    if (!els.emojiPicker.contains(e.target)) {
      els.emojiPicker.classList.remove("active");
      activeEmojiTarget = null;
    }
  });

  els.emojiPicker.querySelectorAll("span").forEach(span => {
    span.onclick = () => {
      if (activeEmojiTarget) handleReaction(activeEmojiTarget, span.textContent);
      els.emojiPicker.classList.remove("active");
      activeEmojiTarget = null;
    };
  });

  els.searchInput.addEventListener("input", renderMessages);

  els.messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  els.messageInput.addEventListener("input", () => {
    els.messageInput.style.height = '24px';
    els.messageInput.style.height = els.messageInput.scrollHeight + 'px';
    sendTyping();
  });

  els.sendBtn.onclick = sendMessage;

  // Settings Modal Logic
  function openSettings() {
    els.nameChangeInput.value = displayName;
    els.settingsModal.classList.add("active");
    els.nameChangeInput.focus();
  }
  function closeSettings() { els.settingsModal.classList.remove("active"); }

  els.settingsBtn.onclick = openSettings;
  els.userProfileBtn.onclick = openSettings;
  els.closeSettings.onclick = closeSettings;
  els.saveSettings.onclick = () => {
    const newName = els.nameChangeInput.value.trim().slice(0, 24);
    if (newName && newName !== displayName) {
      displayName = newName;
      updateUserInfo();
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ kind: "rename", client_id: localClientId, name: displayName, room: roomId(), time: Date.now() }));
      }
    }
    closeSettings();
  };

  window.addEventListener("online", () => { setStatus("back online"); connectSocket(true); });
  window.addEventListener("offline", () => { setStatus("offline"); });

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

