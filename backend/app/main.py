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
        self.reactions: Dict[str, Dict[str, Dict[str, set]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(set))
        )

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
            incoming_name = str(data.get("name") or display_name).strip()[:32] or "Guest"

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
                old_name = rooms.members[room_id].get(client_id, {}).get("name", display_name)
                client_id = incoming_client_id
                display_name = incoming_name
                rooms.register(room_id, client_id, display_name, websocket)
                if old_name != display_name:
                    await rooms.system(room_id, f"{old_name} is now known as {display_name}.")
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
        if display_name != "Guest":
            await rooms.system(room_id, f"{display_name} left.")
        await rooms.send_presence(room_id)
    except Exception:
        rooms.remove(room_id, client_id)
        await rooms.send_presence(room_id)


app.include_router(ws_router, prefix="/ws", tags=["websocket"])


def page_html() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
  <title>LinkUp</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&display=swap" rel="stylesheet"/>
  <style>
  :root{
    --bg:#313338;
    --panel:#2b2d31;
    --panel-2:#1e1f22;
    --panel-3:#383a40;
    --panel-4:#404249;
    --line:rgba(255,255,255,.06);
    --text:#f2f3f5;
    --text-2:#dbdee1;
    --muted:#b5bac1;
    --muted-2:#80848e;
    --accent:#5865f2;
    --accent-h:#4752c4;
    --green:#23a55a;
    --red:#f23f42;
    --yellow:#f0b232;
    --shadow:0 8px 32px rgba(0,0,0,.5);
    --shadow-sm:0 2px 10px rgba(0,0,0,.35);
    color-scheme:dark;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%;height:100dvh}
  body{
    font-family:'DM Sans',system-ui,-apple-system,sans-serif;
    background:var(--bg);color:var(--text);
    overflow:hidden;font-size:15px;line-height:1.5;
  }
  button,input,textarea{font:inherit;cursor:pointer;border:none;outline:none}
  ::-webkit-scrollbar{width:6px;height:6px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:3px}
  ::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.18)}

  /* ── APP SHELL ── */
  .app{
    height:100dvh;
    display:grid;
    grid-template-columns:72px 240px minmax(0,1fr) 240px;
    overflow:hidden;
  }

  /* ── SERVER LIST ── */
  .servers{
    background:var(--panel-2);
    padding:12px 0;
    display:flex;flex-direction:column;
    gap:8px;align-items:center;
    overflow-y:auto;overflow-x:hidden;
    z-index:10;
  }
  .srv-sep{
    width:32px;height:2px;
    background:rgba(255,255,255,.08);
    border-radius:1px;flex:0 0 auto;
  }
  .srv-btn{
    width:48px;height:48px;
    border-radius:50%;
    background:var(--panel-3);
    color:var(--text);
    font-weight:700;font-size:17px;
    display:grid;place-items:center;
    position:relative;flex:0 0 auto;
    overflow:visible;
    transition:border-radius .15s ease,background .15s ease,transform .1s ease;
  }
  .srv-btn:hover{background:var(--accent);border-radius:14px;transform:translateY(-1px)}
  .srv-btn.active{background:var(--accent);border-radius:14px}
  .srv-btn .notch{
    position:absolute;left:-4px;
    width:8px;border-radius:0 4px 4px 0;
    background:#fff;
    transition:height .15s ease,top .15s ease;
    pointer-events:none;
  }
  .srv-btn:not(.active):not(:hover) .notch{height:8px;top:calc(50% - 4px)}
  .srv-btn.active .notch,.srv-btn:hover .notch{height:20px;top:calc(50% - 10px)}
  .srv-tooltip{
    position:fixed;left:82px;
    background:#111214;color:var(--text);
    font-size:13px;font-weight:600;
    padding:7px 12px;border-radius:6px;
    white-space:nowrap;z-index:1000;
    box-shadow:var(--shadow);
    pointer-events:none;display:none;
  }
  .srv-emoji{font-size:22px;line-height:1}

  /* ── CHANNEL SIDEBAR ── */
  .sidebar{
    background:var(--panel);
    display:flex;flex-direction:column;
    overflow:hidden;
    transition:transform .25s cubic-bezier(.4,0,.2,1);
  }
  .sidebar-head{
    height:48px;
    padding:0 16px;
    display:flex;align-items:center;justify-content:space-between;
    border-bottom:1px solid rgba(255,255,255,.05);
    flex:0 0 auto;
    cursor:pointer;
    transition:background .12s;
    user-select:none;
  }
  .sidebar-head:hover{background:rgba(255,255,255,.03)}
  .sidebar-head h1{font-size:15px;font-weight:700;letter-spacing:.01em}
  .sidebar-head .live-badge{
    font-size:10px;font-weight:700;letter-spacing:.06em;
    padding:2px 6px;border-radius:4px;
    background:var(--red);color:#fff;
  }
  .sidebar-body{flex:1 1 auto;overflow-y:auto;min-height:0;padding:8px 0}
  .ch-section-label{
    display:flex;align-items:center;gap:4px;
    padding:14px 8px 4px 16px;
    color:var(--muted-2);font-size:11px;
    font-weight:700;text-transform:uppercase;letter-spacing:.08em;
    cursor:pointer;
    transition:color .1s;
    user-select:none;
  }
  .ch-section-label:hover{color:var(--text)}
  .ch-section-label .tri{font-size:9px;transition:transform .15s}
  .ch-btn{
    display:flex;align-items:center;gap:8px;
    padding:7px 8px 7px 14px;
    margin:1px 8px;
    width:calc(100% - 16px);
    border-radius:6px;
    background:transparent;
    color:var(--muted);
    font-size:14.5px;font-weight:500;text-align:left;
    transition:background .1s,color .1s;
  }
  .ch-btn:hover{background:rgba(255,255,255,.05);color:var(--text-2)}
  .ch-btn.active{background:rgba(255,255,255,.09);color:#fff;font-weight:600}
  .ch-icon{color:var(--muted-2);font-size:17px;width:20px;text-align:center;flex:0 0 auto}
  .ch-btn.active .ch-icon{color:#fff}
  .ch-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

  /* ── USER PANEL ── */
  .user-panel{
    flex:0 0 auto;height:52px;
    background:rgba(0,0,0,.25);
    border-top:1px solid rgba(255,255,255,.05);
    display:flex;align-items:center;gap:8px;padding:0 8px;
  }
  .u-av{
    width:32px;height:32px;border-radius:50%;
    display:grid;place-items:center;
    font-weight:700;font-size:13px;color:#fff;
    flex:0 0 auto;position:relative;cursor:pointer;
    transition:filter .1s;
  }
  .u-av:hover{filter:brightness(1.2)}
  .u-av .online-dot{
    position:absolute;bottom:-1px;right:-1px;
    width:10px;height:10px;border-radius:50%;
    background:var(--green);border:2px solid var(--panel);
  }
  .u-info{flex:1;min-width:0}
  .u-name-row{display:flex;align-items:center;gap:4px}
  .u-name{
    font-size:13px;font-weight:600;color:var(--text);
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    max-width:110px;cursor:pointer;
    transition:color .1s;
  }
  .u-name:hover{color:#fff;text-decoration:underline}
  .u-edit-icon{color:var(--muted-2);font-size:11px;cursor:pointer;flex:0 0 auto;transition:color .1s}
  .u-edit-icon:hover{color:var(--text)}
  .u-status{font-size:11px;color:var(--muted-2)}
  .u-name-input{
    font-size:13px;font-weight:600;
    background:var(--panel-2);color:var(--text);
    border-radius:4px;padding:2px 6px;
    width:116px;border:1px solid var(--accent);
  }
  .u-actions{display:flex;gap:2px;flex:0 0 auto}
  .u-action{
    width:28px;height:28px;border-radius:4px;
    background:transparent;color:var(--muted-2);
    display:grid;place-items:center;font-size:16px;
    transition:background .1s,color .1s;
  }
  .u-action:hover{background:rgba(255,255,255,.08);color:var(--text)}

  /* ── MAIN CHAT ── */
  .main{
    display:flex;flex-direction:column;
    background:var(--bg);overflow:hidden;min-width:0;
    position:relative;
  }

  /* ── TOP BAR ── */
  .topbar{
    flex:0 0 auto;height:48px;
    display:flex;align-items:center;justify-content:space-between;
    padding:0 16px;
    border-bottom:1px solid rgba(255,255,255,.06);
    background:var(--bg);z-index:5;gap:12px;
  }
  .topbar-l{display:flex;align-items:center;gap:10px;min-width:0;overflow:hidden}
  .topbar-ch-icon{color:var(--muted-2);font-size:20px;flex:0 0 auto}
  .topbar-name{font-size:15px;font-weight:700;white-space:nowrap}
  .topbar-sep{width:1px;height:16px;background:rgba(255,255,255,.12);flex:0 0 auto}
  .topbar-desc{
    font-size:13px;color:var(--muted);
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    max-width:38vw;
  }
  .topbar-r{display:flex;align-items:center;gap:6px;flex:0 0 auto}
  .icon-btn{
    width:28px;height:28px;border-radius:4px;
    background:transparent;color:var(--muted-2);
    display:grid;place-items:center;font-size:17px;
    transition:background .1s,color .1s;
  }
  .icon-btn:hover{background:rgba(255,255,255,.06);color:var(--text)}
  .conn-chip{
    display:flex;align-items:center;gap:6px;
    font-size:12px;color:var(--muted);
    padding:4px 8px;border-radius:4px;
    background:rgba(255,255,255,.03);
    white-space:nowrap;
  }
  .c-dot{
    width:8px;height:8px;border-radius:50%;
    background:var(--green);flex:0 0 auto;
    transition:background .4s;
    box-shadow:0 0 0 3px rgba(35,165,90,.15);
  }
  .c-dot.off{background:var(--muted-2);box-shadow:none}
  .c-dot.err{background:var(--red);box-shadow:0 0 0 3px rgba(242,63,66,.15)}
  .hamburger{
    display:none;width:36px;height:36px;border-radius:6px;
    background:transparent;color:var(--text);
    align-items:center;justify-content:center;font-size:20px;
  }
  .hamburger:hover{background:rgba(255,255,255,.06)}

  /* search */
  .srch{position:relative}
  .srch input{
    background:var(--panel-2);color:var(--text);
    padding:5px 28px 5px 28px;border-radius:6px;
    font-size:13px;width:140px;
    transition:width .2s,box-shadow .2s;
  }
  .srch input:focus{width:200px;box-shadow:0 0 0 2px rgba(88,101,242,.35)}
  .srch input::placeholder{color:var(--muted-2)}
  .srch .si{
    position:absolute;left:8px;top:50%;transform:translateY(-50%);
    color:var(--muted-2);font-size:13px;pointer-events:none;
  }
  .srch .sc{
    position:absolute;right:6px;top:50%;transform:translateY(-50%);
    background:transparent;color:var(--muted-2);font-size:12px;
    display:none;padding:0;
  }
  .srch input:not(:placeholder-shown)~.sc{display:block}

  /* ── STATS BAR ── */
  .stats-bar{
    flex:0 0 auto;
    display:flex;align-items:center;gap:12px;
    padding:5px 16px;
    border-bottom:1px solid rgba(255,255,255,.04);
    background:rgba(0,0,0,.08);
    font-size:12px;color:var(--muted-2);
    overflow:hidden;
  }
  .stats-bar .sv{color:var(--muted);font-weight:600}
  .stats-sep{color:rgba(255,255,255,.15)}

  /* ── PINS ── */
  .pins-strip{
    flex:0 0 auto;
    display:none;
    align-items:center;gap:8px;
    padding:5px 16px;
    border-bottom:1px solid rgba(255,255,255,.04);
    background:rgba(0,0,0,.12);
    overflow-x:auto;
  }
  .pins-strip.show{display:flex}
  .pin-ico{color:var(--muted-2);font-size:13px;flex:0 0 auto}
  .pin-chip{
    flex:0 0 auto;max-width:260px;
    padding:3px 10px;border-radius:999px;
    background:rgba(255,255,255,.05);
    color:var(--text-2);font-size:12px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    cursor:pointer;transition:background .1s;
  }
  .pin-chip:hover{background:rgba(255,255,255,.11)}
  .pin-chip .pn{color:var(--accent);font-weight:600;margin-right:3px}

  /* ── MESSAGES ── */
  .messages{
    flex:1 1 auto;min-height:0;
    overflow-y:auto;padding:12px 0 4px;
  }
  .day-div{
    display:flex;align-items:center;gap:12px;
    padding:16px 16px 8px;
    color:var(--muted-2);font-size:11px;font-weight:600;
    letter-spacing:.04em;
  }
  .day-div::before,.day-div::after{
    content:'';flex:1;height:1px;
    background:rgba(255,255,255,.07);
  }

  /* message group */
  .mg{
    padding:1px 16px 1px 72px;
    position:relative;
  }
  .mg.head{padding-top:14px;padding-left:16px}
  .mg.compact{padding-left:72px}
  .mg:hover{background:rgba(255,255,255,.017)}
  .mg:hover .msg-acts{opacity:1;pointer-events:all}
  .mg:hover .ct-time{color:var(--muted-2)}

  /* avatar + header row */
  .mg-head-row{
    display:grid;
    grid-template-columns:40px minmax(0,1fr);
    gap:14px;align-items:flex-start;
    margin-left:-56px;
  }
  .av{
    width:40px;height:40px;border-radius:50%;
    display:grid;place-items:center;
    font-weight:700;font-size:14px;color:#fff;
    flex:0 0 auto;cursor:pointer;
    transition:filter .1s;margin-top:2px;
  }
  .av:hover{filter:brightness(1.15)}
  .ct-time{
    position:absolute;left:22px;top:6px;
    font-size:10px;color:transparent;
    transition:color .1s;user-select:none;
    pointer-events:none;white-space:nowrap;
  }
  .mg-header{display:flex;align-items:baseline;gap:8px;margin-bottom:2px}
  .mg-name{
    font-size:15px;font-weight:600;cursor:pointer;
  }
  .mg-name:hover{text-decoration:underline}
  .mg-ts{font-size:11px;color:var(--muted-2);font-weight:400}
  .you-tag{
    font-size:10px;padding:1px 5px;
    border-radius:999px;
    background:rgba(88,101,242,.28);color:#c0c8ff;
    font-weight:600;vertical-align:middle;
  }
  .mg-text{
    color:var(--text-2);line-height:1.55;
    font-size:15px;word-break:break-word;white-space:pre-wrap;
    margin-left:0;
  }

  /* reply */
  .rp-bar{
    display:flex;align-items:stretch;gap:8px;
    margin-bottom:6px;
  }
  .rp-line{width:2px;border-radius:1px;background:rgba(88,101,242,.7);flex:0 0 auto}
  .rp-body{
    background:rgba(255,255,255,.03);
    border-radius:0 6px 6px 0;
    padding:5px 10px;min-width:0;flex:1;
    cursor:pointer;
  }
  .rp-body:hover{background:rgba(255,255,255,.06)}
  .rp-author{font-size:12px;font-weight:600;color:var(--accent);margin-bottom:1px}
  .rp-text{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

  /* reactions */
  .rxns{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}
  .rxn{
    display:flex;align-items:center;gap:4px;
    padding:2px 8px;border-radius:999px;
    background:rgba(255,255,255,.06);
    border:1px solid rgba(255,255,255,.08);
    font-size:14px;cursor:pointer;
    transition:background .1s,border-color .1s;
  }
  .rxn:hover{background:rgba(88,101,242,.2);border-color:var(--accent)}
  .rxn.mine{background:rgba(88,101,242,.25);border-color:rgba(88,101,242,.6)}
  .rxn-ct{font-size:12px;font-weight:600;color:var(--text-2)}

  /* hover action bar */
  .msg-acts{
    position:absolute;top:-16px;right:16px;
    display:flex;gap:2px;
    background:var(--panel-3);
    border:1px solid rgba(255,255,255,.1);
    border-radius:8px;padding:3px;
    opacity:0;pointer-events:none;
    transition:opacity .1s;z-index:20;
    box-shadow:var(--shadow-sm);
  }
  .ma-btn{
    width:28px;height:28px;border-radius:4px;
    background:transparent;color:var(--muted);
    display:grid;place-items:center;font-size:15px;
    transition:background .1s,color .1s;
    position:relative;
  }
  .ma-btn:hover{background:rgba(255,255,255,.08);color:var(--text)}
  .ma-btn[data-action="pin"].pinned{color:#f0b232}
  .ma-tip{
    position:absolute;bottom:calc(100% + 5px);left:50%;
    transform:translateX(-50%);
    background:#111214;color:#fff;
    font-size:11px;font-weight:600;
    padding:4px 8px;border-radius:5px;
    white-space:nowrap;pointer-events:none;
    opacity:0;transition:opacity .1s;
  }
  .ma-btn:hover .ma-tip{opacity:1}

  /* system message */
  .sys-msg{
    display:flex;align-items:center;gap:12px;
    padding:2px 16px 2px 72px;
    color:var(--muted-2);font-size:13px;font-style:italic;
  }
  .sys-ico{
    position:absolute;left:28px;
    width:24px;height:24px;border-radius:50%;
    background:var(--panel-3);
    display:grid;place-items:center;
    font-size:12px;color:var(--muted-2);
    font-style:normal;
  }

  /* ── TYPING ── */
  .typing-bar{
    flex:0 0 auto;height:22px;
    padding:0 16px;
    display:flex;align-items:center;gap:6px;
    font-size:12px;color:var(--muted);
  }
  .t-dots{display:flex;gap:3px;align-items:center}
  .t-dot{
    width:4px;height:4px;border-radius:50%;
    background:var(--muted);
    animation:tdot 1.2s infinite;
  }
  .t-dot:nth-child(2){animation-delay:.2s}
  .t-dot:nth-child(3){animation-delay:.4s}
  @keyframes tdot{
    0%,80%,100%{transform:translateY(0);opacity:.6}
    40%{transform:translateY(-5px);opacity:1}
  }

  /* ── SCROLL BTN ── */
  .scroll-btn{
    position:absolute;bottom:90px;right:20px;
    width:36px;height:36px;border-radius:50%;
    background:var(--panel-3);
    border:1px solid rgba(255,255,255,.12);
    color:var(--text);font-size:16px;
    display:none;place-items:center;
    box-shadow:var(--shadow-sm);z-index:10;
    transition:transform .1s;
  }
  .scroll-btn.show{display:grid}
  .scroll-btn:hover{transform:translateY(-2px)}

  /* ── COMPOSER ── */
  .composer{
    flex:0 0 auto;
    padding:0 16px 16px;
    background:var(--bg);position:relative;
  }
  .reply-ind{
    display:none;align-items:center;gap:8px;
    padding:6px 12px 6px 14px;
    background:rgba(255,255,255,.04);
    border:1px solid rgba(255,255,255,.06);
    border-bottom:none;
    border-radius:8px 8px 0 0;
    font-size:12px;color:var(--muted);
  }
  .reply-ind.on{display:flex}
  .reply-ind .rn{color:var(--accent);font-weight:600;margin:0 3px}
  .reply-ind .rx{
    margin-left:auto;background:transparent;
    color:var(--muted-2);font-size:17px;
    width:20px;height:20px;
    display:grid;place-items:center;
    padding:0;transition:color .1s;
  }
  .reply-ind .rx:hover{color:var(--text)}
  .comp-wrap{
    display:flex;align-items:flex-end;gap:0;
    background:var(--panel-3);
    border:1px solid rgba(255,255,255,.05);
    border-radius:10px;overflow:visible;position:relative;
  }
  .reply-ind.on~.comp-wrap{border-radius:0 0 10px 10px}
  .comp-left{display:flex;gap:2px;padding:10px 6px;flex:0 0 auto}
  .comp-btn{
    width:28px;height:28px;border-radius:4px;
    background:transparent;color:var(--muted-2);
    display:grid;place-items:center;font-size:19px;
    transition:color .1s;
  }
  .comp-btn:hover{color:var(--text)}
  .msg-input{
    flex:1;min-height:44px;max-height:200px;
    resize:none;background:transparent;
    color:var(--text);line-height:1.5;
    padding:13px 4px;font-size:15px;
    overflow-y:auto;
  }
  .msg-input::placeholder{color:var(--muted-2)}
  .comp-right{display:flex;align-items:flex-end;gap:2px;padding:10px 10px;flex:0 0 auto}
  .send-btn{
    width:32px;height:32px;border-radius:6px;
    background:var(--accent);color:#fff;
    display:grid;place-items:center;font-size:17px;
    transition:background .1s,transform .1s;
  }
  .send-btn:hover{background:var(--accent-h);transform:scale(1.05)}
  .send-btn:active{transform:scale(.94)}

  /* ── EMOJI PICKER ── */
  .emoji-picker{
    position:absolute;
    bottom:calc(100% + 8px);right:0;
    background:var(--panel-2);
    border:1px solid rgba(255,255,255,.12);
    border-radius:12px;padding:10px;
    z-index:200;box-shadow:var(--shadow);
    display:none;flex-direction:column;gap:8px;
    min-width:270px;
  }
  .emoji-picker.open{display:flex}
  .ep-search{
    background:var(--panel-3);color:var(--text);
    padding:6px 10px;border-radius:6px;font-size:13px;
    border:1px solid rgba(255,255,255,.06);
    transition:border-color .15s;
  }
  .ep-search:focus{border-color:var(--accent)}
  .ep-search::placeholder{color:var(--muted-2)}
  .ep-cats{
    display:flex;gap:2px;
    border-bottom:1px solid rgba(255,255,255,.06);
    padding-bottom:6px;
  }
  .ep-cat{
    font-size:18px;padding:4px 6px;border-radius:5px;
    background:transparent;opacity:.55;
    transition:opacity .1s,background .1s;
  }
  .ep-cat:hover,.ep-cat.on{opacity:1;background:rgba(255,255,255,.07)}
  .ep-label{font-size:11px;color:var(--muted-2);font-weight:600;padding:2px 0}
  .ep-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:2px}
  .ep-e{
    font-size:20px;width:32px;height:32px;
    border-radius:5px;background:transparent;
    display:grid;place-items:center;
    transition:background .1s,transform .1s;
  }
  .ep-e:hover{background:rgba(255,255,255,.1);transform:scale(1.25)}
  .ep-e:active{transform:scale(1)}
  .ep-target{display:none}

  /* ── MEMBERS PANEL ── */
  .members-panel{
    background:var(--panel);
    border-left:1px solid rgba(255,255,255,.04);
    display:flex;flex-direction:column;overflow:hidden;
  }
  .mp-head{
    height:48px;padding:0 16px;
    display:flex;align-items:center;
    border-bottom:1px solid rgba(255,255,255,.04);
    flex:0 0 auto;
  }
  .mp-head h3{
    font-size:11px;text-transform:uppercase;
    letter-spacing:.08em;color:var(--muted-2);font-weight:700;
  }
  .mp-scroll{flex:1;overflow-y:auto;padding:8px 0}
  .mp-cat{
    font-size:11px;text-transform:uppercase;
    letter-spacing:.08em;color:var(--muted-2);font-weight:700;
    padding:8px 16px 4px;
  }
  .mp-member{
    display:flex;align-items:center;gap:10px;
    padding:5px 8px;margin:0 4px;
    border-radius:6px;cursor:pointer;
    transition:background .1s;
  }
  .mp-member:hover{background:rgba(255,255,255,.05)}
  .mp-member:hover .mp-name{color:var(--text)}
  .mp-av{
    width:32px;height:32px;border-radius:50%;
    display:grid;place-items:center;
    font-size:12px;font-weight:700;color:#fff;
    flex:0 0 auto;position:relative;
  }
  .mp-av .mp-dot{
    position:absolute;bottom:-1px;right:-1px;
    width:9px;height:9px;border-radius:50%;
    background:var(--green);border:2px solid var(--panel);
  }
  .mp-name{font-size:14px;font-weight:500;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .mp-you{font-size:10px;color:var(--muted-2);margin-left:2px}

  /* ── TOASTS ── */
  .toasts{
    position:fixed;bottom:90px;left:50%;
    transform:translateX(-50%);
    z-index:9999;display:flex;flex-direction:column;gap:8px;
    pointer-events:none;align-items:center;
  }
  .toast{
    background:var(--panel-2);
    border:1px solid rgba(255,255,255,.14);
    color:var(--text);
    padding:9px 16px;border-radius:8px;
    font-size:13px;font-weight:500;
    box-shadow:var(--shadow);white-space:nowrap;
    animation:tin .25s ease,tout .3s ease 2.2s forwards;
  }
  .toast.ok{border-color:rgba(35,165,90,.5);color:#8affc6}
  .toast.bad{border-color:rgba(242,63,66,.5);color:#ffb5b5}
  @keyframes tin{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  @keyframes tout{to{opacity:0;pointer-events:none}}

  /* ── MOBILE OVERLAY ── */
  .overlay{
    display:none;position:fixed;inset:0;
    background:rgba(0,0,0,.72);z-index:50;
  }
  .overlay.on{display:block}

  /* ── RESPONSIVE ── */
  @media(max-width:1080px){
    .app{grid-template-columns:72px 220px minmax(0,1fr)}
    .members-panel{display:none}
    .srch input{width:120px}
    .srch input:focus{width:170px}
  }
  @media(max-width:740px){
    .app{grid-template-columns:1fr;height:100dvh}
    .servers{display:none}
    .sidebar{
      position:fixed;left:0;top:0;bottom:0;
      width:280px;z-index:60;
      transform:translateX(-100%);
    }
    .sidebar.open{transform:translateX(0)}
    .hamburger{display:flex}
    .topbar-desc{display:none}
    .topbar-sep{display:none}
    .topbar-ch-icon{display:none}
    .srch input{width:110px}
    .srch input:focus{width:140px}
    .mg,.mg.head{padding-left:56px}
    .mg .ct-time{left:12px}
    .mg .mg-head-row{margin-left:-40px}
    .msg-acts{right:10px}
    .stats-bar{font-size:11px;padding:4px 12px}
  }
  @media(max-width:480px){
    .app{height:100dvh}
    .topbar{padding:0 10px;gap:8px}
    .stats-bar{display:none}
    .composer{padding:0 10px 12px}
    .mg,.mg.head{padding-left:52px;padding-right:10px}
    .mg .mg-head-row{margin-left:-38px}
    .messages{padding:8px 0 0}
  }
  </style>
</head>
<body>
<div class="app">

  <!-- SERVER LIST -->
  <aside class="servers" id="servers"></aside>

  <!-- CHANNEL SIDEBAR -->
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-head" id="sidebarHead">
      <h1 id="serverLabel">LinkUp</h1>
      <span class="live-badge">LIVE</span>
    </div>
    <div class="sidebar-body" id="channels"></div>
    <!-- USER PANEL -->
    <div class="user-panel" id="userPanel">
      <div class="u-av" id="uAv" title="Click to change status"><span id="uAvText">?</span><span class="online-dot"></span></div>
      <div class="u-info" id="uInfo">
        <div class="u-name-row">
          <span class="u-name" id="uName" title="Click to change username">You</span>
          <span class="u-edit-icon" id="uEditIcon" title="Edit username">✏</span>
        </div>
        <div class="u-status" id="uStatusTxt">Online</div>
      </div>
      <div class="u-actions">
        <button class="u-action" title="Mute" id="muteBtn">🔇</button>
        <button class="u-action" title="Settings" id="settingsBtn">⚙</button>
      </div>
    </div>
  </aside>

  <!-- MAIN CHAT -->
  <main class="main" id="main">
    <div class="topbar">
      <div class="topbar-l">
        <button class="hamburger" id="hamburger">☰</button>
        <span class="topbar-ch-icon">#</span>
        <span class="topbar-name" id="topName">general</span>
        <div class="topbar-sep"></div>
        <span class="topbar-desc" id="topDesc">Main chat room.</span>
      </div>
      <div class="topbar-r">
        <div class="srch">
          <span class="si">🔍</span>
          <input type="text" id="searchInput" placeholder="Search…" autocomplete="off"/>
          <button class="sc" id="searchClear" onclick="document.getElementById('searchInput').value='';document.getElementById('searchInput').dispatchEvent(new Event('input'))">✕</button>
        </div>
        <button class="icon-btn" title="Members" id="membersToggle">👥</button>
        <div class="conn-chip"><div class="c-dot off" id="connDot"></div><span id="connText">connecting…</span></div>
      </div>
    </div>

    <div class="stats-bar" id="statsBar">
      <span class="stat-item"><span class="sv" id="statUsers">0</span> online</span>
      <span class="stats-sep">·</span>
      <span class="stat-item"><span class="sv" id="statMsgs">0</span> messages</span>
      <span class="stats-sep">·</span>
      <span class="stat-item"><span class="sv" id="statPins">0</span> pinned</span>
      <span class="stats-sep">·</span>
      <span class="stat-item" id="statRoom">#general</span>
    </div>

    <div class="pins-strip" id="pinsStrip">
      <span class="pin-ico">📌</span>
    </div>

    <div class="messages" id="messages"></div>
    <div class="typing-bar" id="typingBar"></div>

    <button class="scroll-btn" id="scrollBtn" title="Scroll to bottom">↓</button>

    <div class="composer">
      <div class="reply-ind" id="replyInd">
        Replying to <span class="rn" id="replyName"></span>
        <button class="rx" id="replyCancel">✕</button>
      </div>
      <div class="comp-wrap">
        <div class="comp-left">
          <button class="comp-btn" title="Attach file" onclick="showToast('File uploads coming soon!', '')">📎</button>
          <button class="comp-btn" title="GIF" onclick="showToast('GIFs coming soon!', '')">🎞</button>
        </div>
        <textarea class="msg-input" id="msgInput" placeholder="Message #general…" rows="1"></textarea>
        <div class="comp-right">
          <div class="emoji-picker" id="emojiPicker">
            <input class="ep-search" type="text" placeholder="Search emoji…" id="epSearch" autocomplete="off"/>
            <div class="ep-cats" id="epCats"></div>
            <div class="ep-label" id="epLabel">Frequently Used</div>
            <div class="ep-grid" id="epGrid"></div>
            <div class="ep-target" id="epTarget"></div>
          </div>
          <button class="comp-btn" id="emojiToggle" title="Emoji">😊</button>
          <button class="send-btn" id="sendBtn" title="Send (Enter)">➤</button>
        </div>
      </div>
    </div>
  </main>

  <!-- MEMBERS PANEL -->
  <aside class="members-panel" id="membersPanel">
    <div class="mp-head"><h3>Members</h3></div>
    <div class="mp-scroll" id="memberList"></div>
  </aside>
</div>

<div class="toasts" id="toasts"></div>
<div class="overlay" id="overlay"></div>
<div class="server-tooltip" id="srvTooltip"></div>

<script>
(() => {
"use strict";

/* ════════════════════════════════
   CONFIG
════════════════════════════════ */
const SERVERS = [
  { id:"home",  emoji:"🏠", label:"LinkUp Hub" },
  { id:"dev",   emoji:"💻", label:"Dev Base" },
  { id:"games", emoji:"🎮", label:"Game Squad" },
  { id:"study", emoji:"📚", label:"Study Zone" },
  { id:"music", emoji:"🎵", label:"Music Room" },
  { id:"art",   emoji:"🎨", label:"Art Studio" },
];

const CHANNELS = {
  home:[
    {id:"general",       icon:"#", title:"general",       desc:"Main feed for everything LinkUp."},
    {id:"announcements", icon:"📢", title:"announcements", desc:"Product updates and changelogs."},
    {id:"showcase",      icon:"✨", title:"showcase",      desc:"Drop cool projects and wins."},
    {id:"random",        icon:"🎲", title:"random",        desc:"Off-topic chatter."},
  ],
  dev:[
    {id:"frontend",  icon:"#", title:"frontend",  desc:"UI, components, and layout."},
    {id:"backend",   icon:"#", title:"backend",   desc:"APIs, database, auth, servers."},
    {id:"bugs",      icon:"🐛", title:"bugs",      desc:"Report issues here."},
    {id:"releases",  icon:"🚀", title:"releases",  desc:"Release notes and changelogs."},
  ],
  games:[
    {id:"lobby",    icon:"#", title:"lobby",    desc:"Find players and set up sessions."},
    {id:"clips",    icon:"🎬", title:"clips",    desc:"Highlights and funny moments."},
    {id:"meta",     icon:"⚔", title:"meta",     desc:"Builds, balance, and strategy."},
    {id:"lfg",      icon:"🔎", title:"lfg",      desc:"Looking for group."},
  ],
  study:[
    {id:"math",   icon:"➗", title:"math",   desc:"Problem solving and homework."},
    {id:"coding", icon:"⌨", title:"coding", desc:"Programming help."},
    {id:"focus",  icon:"🔕", title:"focus",  desc:"Quiet study time."},
    {id:"resources",icon:"📖",title:"resources",desc:"Useful links and guides."},
  ],
  music:[
    {id:"now-playing", icon:"▶", title:"now-playing", desc:"What are you listening to?"},
    {id:"recs",        icon:"💿", title:"recs",        desc:"Music recommendations."},
    {id:"production",  icon:"🎹", title:"production",  desc:"Making beats and tracks."},
  ],
  art:[
    {id:"gallery",    icon:"🖼", title:"gallery",    desc:"Share your artwork."},
    {id:"critiques",  icon:"💬", title:"critiques",  desc:"Get feedback on your work."},
    {id:"inspiration",icon:"💡", title:"inspiration",desc:"Inspiration and references."},
  ],
};

const SEEDS = {
  general:["Welcome to LinkUp — a real-time chat built on FastAPI WebSockets.","Switch servers and channels on the left. Your name is editable anytime.","React, reply, and pin messages using the action bar that appears on hover."],
  announcements:["📢 LinkUp v2 — redesigned UI, username changing, emoji picker, mobile support."],
  showcase:["Drop your best work here!"],
  random:["Anything goes here 😄"],
  frontend:["UI and component discussion lives here."],
  backend:["FastAPI, WebSockets, and database talk."],
  bugs:["Found a bug? Log it here."],
  releases:["v2.0 — Discord-like UI overhaul 🎉"],
  lobby:["Find your squad here."],
  clips:["Post your highlight clips!"],
  meta:["Discuss builds and game strategy."],
  lfg:["Looking for group? Post here."],
  math:["Math help is available!"],
  coding:["Share your code questions."],
  focus:["🔕 Quiet study zone."],
  resources:["Useful study resources."],
  "now-playing":["Currently spinning… 🎵"],
  recs:["Recommend your favourite tracks!"],
  production:["Share your latest beat."],
  gallery:["Post your artwork here!"],
  critiques:["Post work for feedback."],
  inspiration:["Drop inspiring references."],
};

/* avatar colours */
const AV_COLORS = [
  "#5865f2","#3ba55d","#ed4245","#faa81a",
  "#9b59b6","#eb459e","#2ecc71","#e67e22",
  "#1abc9c","#3498db","#e74c3c","#f39c12",
];
function avColor(str){
  let h=0;for(const c of String(str)){h=(h*31+c.charCodeAt(0))>>>0}
  return AV_COLORS[h%AV_COLORS.length];
}

/* emoji categories */
const EMOJI_DATA = {
  "😊 Smileys":[
    "😀","😃","😄","😁","😆","😅","🤣","😂","🙂","🙃","😉","😊",
    "😇","🥰","😍","🤩","😘","😗","😚","😙","🥲","😋","😛","😜",
    "🤪","😝","🤑","🤗","🤭","🫢","🤫","🤔","🤐","🤨","😐","😑",
    "😶","🫥","😏","😒","🙄","😬","🤥","😔","😪","🤤","😴","😷",
  ],
  "👍 People":[
    "👋","🤚","🖐","✋","🖖","🫱","🫲","🤝","👏","🙌","🫶","🤲",
    "🤜","🤛","👊","✊","🤞","✌","🤟","🤘","👌","🤌","🤏","👈",
    "👉","👆","🖕","👇","☝","🫵","👍","👎","✊","👊","💪","🦾",
    "🦵","🦶","👂","🦻","👃","🫀","🫁","🧠","🦷","🦴","👀","👁",
  ],
  "🎉 Activities":[
    "⚽","🏀","🏈","⚾","🥎","🎾","🏐","🏉","🥏","🎱","🪀","🏓",
    "🏸","🏒","🥊","🎯","🎳","🎮","🕹","🎲","♟","🎭","🎨","🖼",
    "🎰","🚂","🚗","✈","🚀","🛸","🎡","🎢","🎠","🎪","🤹","🎭",
    "🎬","🎤","🎧","🎼","🎹","🥁","🪘","🎸","🎷","🪗","🎺","🎻",
  ],
  "🌍 Nature":[
    "🌸","💐","🌷","🌹","🥀","🌺","🌻","🌼","🌿","🍀","🍁","🍂",
    "🍃","🍄","🌾","🌵","🎄","🌴","🌳","🌲","🦋","🐛","🐝","🐞",
    "🦗","🦟","🦠","🐢","🦎","🐍","🦕","🦖","🦦","🦥","🐿","🦔",
    "🐾","🦁","🐯","🐻","🦊","🦝","🐱","🐶","🐺","🐗","🦌","🦬",
  ],
  "🍔 Food":[
    "🍏","🍎","🍐","🍊","🍋","🍌","🍉","🍇","🍓","🫐","🍈","🍒",
    "🍑","🥭","🍍","🥥","🥝","🍅","🍆","🥑","🫑","🥦","🧄","🧅",
    "🥔","🌽","🌶","🫚","🥕","🫛","🥐","🥯","🍞","🥖","🥨","🧀",
    "🍔","🍟","🌭","🍕","🫔","🌮","🌯","🥙","🧆","🥚","🍳","🥘",
  ],
  "✨ Symbols":[
    "❤","🧡","💛","💚","💙","💜","🖤","🤍","🤎","💔","❤‍🔥","💕",
    "💞","💓","💗","💖","💘","💝","✨","⭐","🌟","💫","⚡","🔥",
    "💥","🌈","☀","🌤","⛅","🌈","❄","🌊","💧","🌙","🌛","🌝",
    "💯","🆒","🆙","🆕","🆓","🔴","🟠","🟡","🟢","🔵","🟣","⚫",
  ],
};

/* ════════════════════════════════
   STATE
════════════════════════════════ */
const $ = id => document.getElementById(id);

const savedName    = localStorage.getItem("lu_name") || "You";
const localId      = localStorage.getItem("lu_cid")  || ("cli_"+Math.random().toString(36).slice(2,10));
localStorage.setItem("lu_cid", localId);

let displayName    = savedName;
let selServer      = localStorage.getItem("lu_srv") || "home";
let selChannel     = localStorage.getItem("lu_ch")  || "general";
let socket         = null;
let connSeq        = 0;
let retryCount     = 0;
let reconnTimer    = null;
let hsTimer        = null;
let typingTmr      = null;
let replyTo        = null;
let muted          = false;
let epTargetId     = null; /* emoji picker target msg id */

const roomMsgs  = new Map();
const roomSeen  = new Map();
const roomPres  = new Map();
const roomPins  = new Map();

/* ════════════════════════════════
   HELPERS
════════════════════════════════ */
function esc(s){
  return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function fmtTs(ts){
  return new Date(ts||Date.now()).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
}
function fmtDay(ts){
  const d=new Date(ts||Date.now()),n=new Date();
  const same=(a,b)=>a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()&&a.getDate()===b.getDate();
  if(same(d,n)) return "Today";
  const yes=new Date(n); yes.setDate(yes.getDate()-1);
  if(same(d,yes)) return "Yesterday";
  return d.toLocaleDateString([],{month:"long",day:"numeric",year:"numeric"});
}
function roomId(){return `${selServer}:${selChannel}`}
function ch(){
  const list=CHANNELS[selServer]||[];
  return list.find(c=>c.id===selChannel)||list[0];
}

function ensureRoom(r){
  if(roomMsgs.has(r)) return;
  const key=r.split(":")[1]||"general";
  const seeds=(SEEDS[key]||["Room loaded."]).map((t,i)=>({
    msg_id:`seed-${r}-${i}`,kind:"system",name:"System",text:t,
    time:Date.now()-(i+1)*60000,room:r
  }));
  roomMsgs.set(r,seeds);
  roomSeen.set(r,new Set(seeds.map(m=>m.msg_id)));
  roomPres.set(r,[]);
  roomPins.set(r,[]);
}
function msgList(r){ensureRoom(r||roomId());return roomMsgs.get(r||roomId())}
function seenSet(r){ensureRoom(r);return roomSeen.get(r)}

function showToast(msg, type=""){
  const t=document.createElement("div");
  t.className="toast"+(type?" "+type:"");
  t.textContent=msg;
  $("toasts").appendChild(t);
  setTimeout(()=>t.remove(),2600);
}

/* ════════════════════════════════
   STATUS / CONNECTION
════════════════════════════════ */
function setStatus(text, ok=false, err=false){
  $("connText").textContent=text;
  const d=$("connDot");
  d.className="c-dot"+(ok?"":(err?" err":" off"));
}

/* ════════════════════════════════
   USERNAME EDITING
════════════════════════════════ */
function renderUserPanel(){
  const color=avColor(displayName);
  const av=$("uAv");
  av.style.background=color;
  $("uAvText").textContent=(displayName||"?").trim()[0].toUpperCase();
  $("uName").textContent=displayName;
}

function startEditName(){
  const wrap=$("uInfo");
  const old=displayName;
  wrap.innerHTML=`<input class="u-name-input" id="uNameInp" maxlength="32" value="${esc(old)}" autocomplete="off"/>`;
  const inp=$("uNameInp");
  inp.focus(); inp.select();
  function commit(){
    const v=inp.value.trim().slice(0,32)||old;
    if(v!==old){
      displayName=v;
      localStorage.setItem("lu_name",v);
      if(socket&&socket.readyState===WebSocket.OPEN){
        socket.send(JSON.stringify({kind:"rename",client_id:localId,name:v,room:roomId(),time:Date.now()}));
      }
      showToast(`Username changed to ${v}`, "ok");
    }
    wrap.innerHTML=`<div class="u-name-row"><span class="u-name" id="uName" title="Click to change username">${esc(v)}</span><span class="u-edit-icon" id="uEditIcon" title="Edit username">✏</span></div><div class="u-status" id="uStatusTxt">Online</div>`;
    $("uName").onclick=startEditName;
    $("uEditIcon").onclick=startEditName;
    renderUserPanel();
  }
  inp.onblur=commit;
  inp.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();commit()}if(e.key==="Escape"){inp.value=old;commit()}};
}

/* ════════════════════════════════
   RENDER SERVERS
════════════════════════════════ */
function renderServers(){
  const tooltip=$("srvTooltip");
  $("servers").innerHTML=SERVERS.map(s=>`
    <button class="srv-btn${s.id===selServer?" active":""}" data-srv="${s.id}" title="${esc(s.label)}">
      <span class="notch"></span>
      <span class="srv-emoji">${s.emoji}</span>
    </button>
  `).join(`<div class="srv-sep"></div>`);
  $("servers").querySelectorAll(".srv-btn").forEach(btn=>{
    btn.onmouseenter=()=>{
      tooltip.textContent=btn.title;
      const r=btn.getBoundingClientRect();
      tooltip.style.top=(r.top+r.height/2-12)+"px";
      tooltip.style.display="block";
    };
    btn.onmouseleave=()=>{tooltip.style.display="none"};
    btn.onclick=()=>{
      selServer=btn.dataset.srv;
      selChannel=(CHANNELS[selServer]||[{id:"general"}])[0].id;
      replyTo=null;
      localStorage.setItem("lu_srv",selServer);
      localStorage.setItem("lu_ch",selChannel);
      renderShell(); connectSocket(true);
      tooltip.style.display="none";
    };
  });
}

/* ════════════════════════════════
   RENDER CHANNELS
════════════════════════════════ */
function renderChannels(){
  const list=CHANNELS[selServer]||[];
  $("serverLabel").textContent=SERVERS.find(s=>s.id===selServer)?.label||"LinkUp";
  $("channels").innerHTML=`<div class="ch-section-label"><span class="tri">▸</span> TEXT CHANNELS</div>`+
    list.map(c=>`
      <button class="ch-btn${c.id===selChannel?" active":""}" data-ch="${c.id}">
        <span class="ch-icon">${c.icon||"#"}</span>
        <span class="ch-name">${esc(c.title)}</span>
      </button>
    `).join("");
  $("channels").querySelectorAll(".ch-btn").forEach(btn=>{
    btn.onclick=()=>{
      selChannel=btn.dataset.ch;
      replyTo=null;
      localStorage.setItem("lu_ch",selChannel);
      renderShell(); connectSocket(true);
      closeSidebar();
    };
  });
}

/* ════════════════════════════════
   RENDER MEMBERS
════════════════════════════════ */
function renderMembers(){
  const users=roomPres.get(roomId())||[];
  $("statUsers").textContent=String(users.length);
  const list=$("memberList");
  if(!users.length){
    list.innerHTML=`<div style="padding:12px 16px;font-size:13px;color:var(--muted-2)">No one else in here yet.</div>`;
    return;
  }
  list.innerHTML=`<div class="mp-cat">Online — ${users.length}</div>`+
    users.map(u=>{
      const isMe=u.client_id===localId;
      const col=avColor(u.name);
      return `<div class="mp-member">
        <div class="mp-av" style="background:${col}">
          ${esc((u.name||"?")[0].toUpperCase())}
          <span class="mp-dot"></span>
        </div>
        <span class="mp-name">${esc(u.name||"Guest")}${isMe?`<span class="mp-you">(you)</span>`:""}</span>
      </div>`;
    }).join("");
}

/* ════════════════════════════════
   RENDER PINS
════════════════════════════════ */
function renderPins(){
  const pins=roomPins.get(roomId())||[];
  $("statPins").textContent=String(pins.length);
  const strip=$("pinsStrip");
  strip.className="pins-strip"+(pins.length?" show":"");
  const existing=strip.querySelectorAll(".pin-chip");
  existing.forEach(e=>e.remove());
  pins.slice().reverse().forEach(p=>{
    const chip=document.createElement("div");
    chip.className="pin-chip";
    chip.innerHTML=`<span class="pn">${esc(p.name)}</span>${esc((p.text||"").slice(0,80))}`;
    chip.dataset.pin=p.msg_id;
    chip.onclick=()=>{
      const t=$("messages").querySelector(`[data-mg="${CSS.escape(p.msg_id)}"]`);
      if(t) t.scrollIntoView({behavior:"smooth",block:"center"});
    };
    strip.appendChild(chip);
  });
}

/* ════════════════════════════════
   RENDER TOPBAR
════════════════════════════════ */
function renderTop(){
  const c=ch();
  const name=c?c.title:"general";
  const desc=c?c.desc:"Real-time chat.";
  $("topName").textContent=name;
  $("topDesc").textContent=desc;
  $("msgInput").placeholder=`Message #${name}…`;
  $("statRoom").textContent=`#${name}`;
}

/* ════════════════════════════════
   RENDER MESSAGES
════════════════════════════════ */
function buildMsgHtml(msgs){
  let html=""; let prevDate=null; let prevSender=null; let prevTime=0;
  for(let i=0;i<msgs.length;i++){
    const m=msgs[i];
    if(m.kind==="system"){
      html+=`<div class="sys-msg" style="position:relative" data-mg="${esc(m.msg_id)}">
        <span class="sys-ico">ℹ</span>
        <span>${esc(m.text||"")}</span>
        <span style="margin-left:auto;font-size:10px;color:var(--muted-2)">${fmtTs(m.time)}</span>
      </div>`;
      prevSender=null; continue;
    }
    const dayLabel=fmtDay(m.time);
    if(dayLabel!==prevDate){
      html+=`<div class="day-div">${esc(dayLabel)}</div>`;
      prevDate=dayLabel; prevSender=null;
    }
    const isSelf=m.client_id===localId;
    const col=avColor(m.name||"?");
    const init=(m.name||"?")[0].toUpperCase();
    const grouped=prevSender===m.client_id&&(m.time-prevTime)<300000;
    const reactions=m.reactions||{};
    const rxnKeys=Object.keys(reactions).filter(k=>reactions[k]>0);
    const rp=m.reply_preview;
    const isPinned=!!(roomPins.get(roomId())||[]).find(p=>p.msg_id===m.msg_id);

    html+=`<div class="mg${grouped?" compact":" head"}" data-mg="${esc(m.msg_id)}">`;

    if(grouped){
      html+=`<span class="ct-time">${fmtTs(m.time)}</span>`;
    } else {
      html+=`<div class="mg-head-row">
        <div class="av" style="background:${col}" title="${esc(m.name||"Guest")}">${esc(init)}</div>
        <div>
          <div class="mg-header">
            <span class="mg-name" style="color:${col}">${esc(m.name||"Guest")}${isSelf?`&nbsp;<span class="you-tag">you</span>`:""}</span>
            <span class="mg-ts">${fmtTs(m.time)}</span>
          </div>`;
    }

    html+=`<div class="mg-body">`;
    if(rp){
      html+=`<div class="rp-bar"><div class="rp-line"></div>
        <div class="rp-body" data-scroll="${esc(rp.msg_id||"")}">
          <div class="rp-author">${esc(rp.name||"Guest")}</div>
          <div class="rp-text">${esc((rp.text||"").slice(0,120))}</div>
        </div></div>`;
    }
    html+=`<div class="mg-text">${esc(m.text||"")}</div>`;
    if(rxnKeys.length){
      html+=`<div class="rxns">`;
      rxnKeys.forEach(e=>{
        html+=`<button class="rxn" data-action="reaction" data-id="${esc(m.msg_id)}" data-emoji="${esc(e)}">${esc(e)}<span class="rxn-ct">${reactions[e]}</span></button>`;
      });
      html+=`</div>`;
    }
    html+=`</div>`; /* mg-body */

    if(!grouped) html+=`</div></div>`; /* close inner + mg-head-row */

    /* action bar */
    html+=`<div class="msg-acts">
      <button class="ma-btn" data-action="react" data-id="${esc(m.msg_id)}" title="Add reaction">
        <span class="ma-tip">React</span>😊
      </button>
      <button class="ma-btn" data-action="reply" data-id="${esc(m.msg_id)}" title="Reply">
        <span class="ma-tip">Reply</span>↩
      </button>
      <button class="ma-btn${isPinned?" pinned":""}" data-action="pin" data-id="${esc(m.msg_id)}" title="${isPinned?"Unpin":"Pin"}">
        <span class="ma-tip">${isPinned?"Unpin":"Pin"}</span>📌
      </button>
      <button class="ma-btn" data-action="copy" data-id="${esc(m.msg_id)}" title="Copy text">
        <span class="ma-tip">Copy</span>📋
      </button>
    </div>`;

    html+=`</div>`; /* close .mg */
    prevSender=m.client_id; prevTime=m.time;
  }
  return html;
}

function applyFilter(){
  const q=(($("searchInput").value)||"").trim().toLowerCase();
  return msgList().filter(m=>{
    if(!q) return true;
    if(m.kind==="system") return String(m.text||"").toLowerCase().includes(q);
    return String(m.text||"").toLowerCase().includes(q)||String(m.name||"").toLowerCase().includes(q);
  });
}

function renderMessages(){
  ensureRoom(roomId());
  const msgs=applyFilter();
  $("messages").innerHTML=buildMsgHtml(msgs);
  $("statMsgs").textContent=String(msgList().filter(m=>m.kind!=="system").length);
  scrollToBottom();
  bindMsgActions();
  bindReplyScrolls();
}

function scrollToBottom(force=false){
  const el=$("messages");
  const atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<80;
  if(atBottom||force) el.scrollTop=el.scrollHeight;
}

function pushMsg(room, msg, render=true){
  ensureRoom(room);
  if(!msg.msg_id) msg.msg_id="m_"+Math.random().toString(36).slice(2,12);
  if(seenSet(room).has(msg.msg_id)) return;
  seenSet(room).add(msg.msg_id);
  msg.reactions=msg.reactions||{};
  msgList(room).push(msg);
  if(room===roomId()&&render){
    const el=$("messages");
    const atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<100;
    const frag=buildMsgHtml([msg]);
    el.insertAdjacentHTML("beforeend",frag);
    $("statMsgs").textContent=String(msgList().filter(m=>m.kind!=="system").length);
    if(atBottom) scrollToBottom(true);
    else $("scrollBtn").classList.add("show");
    bindMsgActions();
    bindReplyScrolls();
  }
}

/* ════════════════════════════════
   BIND INTERACTIONS
════════════════════════════════ */
function bindMsgActions(){
  $("messages").querySelectorAll(".ma-btn[data-action]").forEach(btn=>{
    btn.onclick=e=>{
      e.stopPropagation();
      const action=btn.dataset.action, id=btn.dataset.id;
      if(action==="reply") startReply(id);
      else if(action==="pin") sendPin(id);
      else if(action==="react") openEmojiPicker(id);
      else if(action==="copy") copyMsg(id);
    };
  });
  $("messages").querySelectorAll(".rxn[data-action='reaction']").forEach(btn=>{
    btn.onclick=e=>{
      e.stopPropagation();
      sendReaction(btn.dataset.id, btn.dataset.emoji);
    };
  });
}

function bindReplyScrolls(){
  $("messages").querySelectorAll(".rp-body[data-scroll]").forEach(el=>{
    el.onclick=()=>{
      const t=$("messages").querySelector(`[data-mg="${CSS.escape(el.dataset.scroll)}"]`);
      if(t) t.scrollIntoView({behavior:"smooth",block:"center"});
    };
  });
}

function startReply(id){
  const msg=msgList().find(m=>m.msg_id===id);
  replyTo=id;
  $("replyName").textContent=msg?msg.name:"someone";
  $("replyInd").classList.add("on");
  $("msgInput").focus();
}

function cancelReply(){
  replyTo=null;
  $("replyInd").classList.remove("on");
  $("msgInput").focus();
}

function copyMsg(id){
  const msg=msgList().find(m=>m.msg_id===id);
  if(!msg) return;
  navigator.clipboard?.writeText(msg.text||"").then(()=>showToast("Copied!","ok")).catch(()=>showToast("Copy failed","bad"));
}

function sendPin(id){
  if(!socket||socket.readyState!==WebSocket.OPEN) return;
  socket.send(JSON.stringify({kind:"pin",client_id:localId,name:displayName,room:roomId(),msg_id:id,time:Date.now()}));
}

function sendReaction(id, emoji){
  if(!socket||socket.readyState!==WebSocket.OPEN) return;
  socket.send(JSON.stringify({kind:"reaction",client_id:localId,name:displayName,room:roomId(),msg_id:id,emoji,time:Date.now()}));
}

/* ════════════════════════════════
   EMOJI PICKER
════════════════════════════════ */
function buildEmojiPicker(){
  const cats=Object.keys(EMOJI_DATA);
  $("epCats").innerHTML=cats.map((cat,i)=>`
    <button class="ep-cat${i===0?" on":""}" data-cat="${esc(cat)}" title="${esc(cat)}">${cat.split(" ")[0]}</button>
  `).join("");
  $("epCats").querySelectorAll(".ep-cat").forEach(btn=>{
    btn.onclick=()=>{
      $("epCats").querySelectorAll(".ep-cat").forEach(b=>b.classList.remove("on"));
      btn.classList.add("on");
      $("epLabel").textContent=btn.dataset.cat;
      showEmojiCat(btn.dataset.cat);
    };
  });
  showEmojiCat(cats[0]);
}

function showEmojiCat(cat){
  const emojis=EMOJI_DATA[cat]||[];
  $("epGrid").innerHTML=emojis.map(e=>`<button class="ep-e" data-e="${esc(e)}">${e}</button>`).join("");
  $("epGrid").querySelectorAll(".ep-e").forEach(btn=>{
    btn.onclick=()=>commitEmoji(btn.dataset.e);
  });
}

function filterEmojis(q){
  if(!q){
    const active=$("epCats").querySelector(".ep-cat.on");
    if(active) showEmojiCat(active.dataset.cat);
    return;
  }
  const all=Object.values(EMOJI_DATA).flat();
  $("epGrid").innerHTML=all.filter(e=>e.includes(q)).map(e=>`<button class="ep-e" data-e="${esc(e)}">${e}</button>`).join("");
  $("epGrid").querySelectorAll(".ep-e").forEach(btn=>{
    btn.onclick=()=>commitEmoji(btn.dataset.e);
  });
}

function commitEmoji(emoji){
  if(epTargetId){
    sendReaction(epTargetId, emoji);
    closeEmojiPicker();
  } else {
    const inp=$("msgInput");
    const s=inp.selectionStart||0, e=inp.selectionEnd||0;
    inp.value=inp.value.slice(0,s)+emoji+inp.value.slice(e);
    inp.setSelectionRange(s+emoji.length,s+emoji.length);
    inp.focus();
    closeEmojiPicker();
  }
}

function openEmojiPicker(msgId=null){
  epTargetId=msgId;
  const ep=$("emojiPicker");
  ep.classList.add("open");
  $("epSearch").value=""; $("epSearch").focus();
  setTimeout(()=>document.addEventListener("click",outsideEP,{once:true,capture:true}),10);
}

function closeEmojiPicker(){
  $("emojiPicker").classList.remove("open");
  epTargetId=null;
}

function outsideEP(e){
  if(!$("emojiPicker").contains(e.target)&&e.target!==$("emojiToggle")) closeEmojiPicker();
}

/* ════════════════════════════════
   SEND MESSAGE
════════════════════════════════ */
function sendMessage(){
  const text=$("msgInput").value.trim();
  if(!text) return;

  const payload={
    kind:"message",
    msg_id:`cli_${Date.now()}_${Math.random().toString(36).slice(2,9)}`,
    client_id:localId,
    name:displayName,
    text,
    room:roomId(),
    server:selServer,
    channel:selChannel,
    reply_to:replyTo,
    time:Date.now(),
  };

  if(replyTo){
    const orig=msgList().find(m=>m.msg_id===replyTo);
    if(orig) payload.reply_preview={msg_id:orig.msg_id,name:orig.name,text:orig.text};
  }

  pushMsg(roomId(), payload, true);
  if(socket&&socket.readyState===WebSocket.OPEN) socket.send(JSON.stringify(payload));

  cancelReply();
  $("msgInput").value="";
  $("msgInput").style.height="";
  $("msgInput").focus();
}

function sendTyping(){
  if(!socket||socket.readyState!==WebSocket.OPEN) return;
  socket.send(JSON.stringify({kind:"typing",client_id:localId,name:displayName,room:roomId(),time:Date.now()}));
}

/* ════════════════════════════════
   WEBSOCKET
════════════════════════════════ */
function wsUrl(room){
  const p=location.protocol==="https:"?"wss":"ws";
  return `${p}://${location.host}/ws/${encodeURIComponent(room)}`;
}

function cleanTimers(){
  clearTimeout(reconnTimer); reconnTimer=null;
  clearTimeout(hsTimer); hsTimer=null;
}

function scheduleReconnect(seq){
  if(seq!==connSeq) return;
  const delay=Math.min(1000*Math.pow(2,retryCount),12000);
  retryCount++;
  reconnTimer=setTimeout(()=>{if(seq===connSeq) connectSocket(false)},delay);
  setTimeout(()=>{
    if(seq===connSeq&&(!socket||socket.readyState!==WebSocket.OPEN))
      setStatus(`retry in ${Math.ceil(delay/1000)}s`);
  },20);
}

function connectSocket(force=false){
  cleanTimers();
  const seq=++connSeq;
  const room=roomId();
  let opened=false;

  if(socket&&(socket.readyState===WebSocket.OPEN||socket.readyState===WebSocket.CONNECTING)){
    try{socket.close(1000,"switch")}catch{}
  }

  roomPres.set(room,[]);
  $("typingBar").innerHTML="";
  setStatus("connecting…");

  try{ socket=new WebSocket(wsUrl(room)) }
  catch{setStatus("unavailable",false,true);scheduleReconnect(seq);return}

  hsTimer=setTimeout(()=>{if(seq===connSeq&&!opened) setStatus("slow connection…")},4000);

  socket.onopen=()=>{
    if(seq!==connSeq){try{socket.close(1000,"stale")}catch{}return}
    opened=true; retryCount=0; cleanTimers();
    setStatus("connected",true);
    socket.send(JSON.stringify({kind:"hello",client_id:localId,name:displayName,room,server:selServer,channel:selChannel,time:Date.now()}));
  };

  socket.onmessage=e=>{
    if(seq!==connSeq) return;
    let data;
    try{data=JSON.parse(e.data)}catch{return}
    if(!data) return;

    if(data.kind==="history"){
      const hist=Array.isArray(data.messages)?data.messages:[];
      const users=Array.isArray(data.users)?data.users:[];
      const pins=Array.isArray(data.pins)?data.pins:[];
      roomMsgs.set(room,[]);
      roomSeen.set(room,new Set());
      roomPres.set(room,users);
      roomPins.set(room,pins);
      hist.forEach(m=>{
        if(!m.msg_id) m.msg_id="h_"+Math.random().toString(36).slice(2,12);
        seenSet(room).add(m.msg_id);
        m.reactions=m.reactions||{};
        msgList(room).push(m);
      });
      renderMembers(); renderPins(); renderMessages();
      return;
    }
    if(data.kind==="presence"){
      roomPres.set(room,Array.isArray(data.users)?data.users:[]);
      renderMembers(); return;
    }
    if(data.kind==="typing"){
      if(data.client_id&&data.client_id!==localId){
        const name=esc(data.name||"Someone");
        $("typingBar").innerHTML=`<div class="t-dots"><div class="t-dot"></div><div class="t-dot"></div><div class="t-dot"></div></div><span>${name} is typing…</span>`;
        clearTimeout(typingTmr);
        typingTmr=setTimeout(()=>$("typingBar").innerHTML="",1400);
      }
      return;
    }
    if(data.kind==="system"){
      pushMsg(room,data,true); return;
    }
    if(data.kind==="pin_update"){
      roomPins.set(room,Array.isArray(data.pins)?data.pins:[]);
      renderPins();
      if(room===roomId()) renderMessages();
      const act=data.action==="pinned"?"📌 Message pinned":"📌 Message unpinned";
      showToast(act, data.action==="pinned"?"ok":"");
      return;
    }
    if(data.kind==="reaction_update"){
      const list=msgList(room);
      const t=list.find(m=>m.msg_id===data.msg_id);
      if(t){t.reactions=data.reactions||{};if(room===roomId()) renderMessages();}
      return;
    }
    if(data.kind==="message"){
      data.time=data.time||Date.now();
      data.client_id=data.client_id||"remote";
      data.msg_id=data.msg_id||`srv_${room}_${data.time}_${Math.random().toString(36).slice(2,8)}`;
      data.reactions=data.reactions||{};
      pushMsg(room,data,true);
    }
  };

  socket.onerror=()=>{if(seq===connSeq&&!opened) setStatus("error",false,true)};
  socket.onclose=()=>{
    if(seq!==connSeq) return;
    cleanTimers();
    setStatus(opened?"disconnected":"offline",false,!opened);
    scheduleReconnect(seq);
  };
}

/* ════════════════════════════════
   SHELL
════════════════════════════════ */
function renderShell(){
  renderServers();
  renderChannels();
  renderTop();
  renderMembers();
  renderPins();
  renderMessages();
}

/* ════════════════════════════════
   MOBILE SIDEBAR
════════════════════════════════ */
function openSidebar(){
  $("sidebar").classList.add("open");
  $("overlay").classList.add("on");
}
function closeSidebar(){
  $("sidebar").classList.remove("open");
  $("overlay").classList.remove("on");
}

/* ════════════════════════════════
   EVENTS
════════════════════════════════ */
$("hamburger").onclick=openSidebar;
$("overlay").onclick=closeSidebar;

$("replyCancel").onclick=cancelReply;

$("scrollBtn").onclick=()=>{scrollToBottom(true);$("scrollBtn").classList.remove("show")};

$("messages").onscroll=()=>{
  const el=$("messages");
  const atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<120;
  $("scrollBtn").classList.toggle("show",!atBottom);
};

$("msgInput").addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendMessage()}
  if(e.key==="Escape"){cancelReply();closeEmojiPicker()}
});

let typThrottle=0;
$("msgInput").addEventListener("input",()=>{
  const now=Date.now();
  if(now-typThrottle>2000){typThrottle=now;sendTyping()}
  /* auto-resize */
  const inp=$("msgInput");
  inp.style.height="auto";
  inp.style.height=Math.min(inp.scrollHeight,200)+"px";
});

$("sendBtn").onclick=sendMessage;

$("emojiToggle").onclick=e=>{
  e.stopPropagation();
  if($("emojiPicker").classList.contains("open")) closeEmojiPicker();
  else openEmojiPicker(null);
};

$("epSearch").addEventListener("input",()=>filterEmojis($("epSearch").value));

$("searchInput").addEventListener("input",renderMessages);

$("muteBtn").onclick=()=>{
  muted=!muted;
  $("muteBtn").textContent=muted?"🔕":"🔇";
  showToast(muted?"Muted":"Unmuted", muted?"bad":"ok");
};

$("settingsBtn").onclick=()=>showToast("Settings coming soon!", "");

$("membersToggle").onclick=()=>{
  const mp=$("membersPanel");
  mp.style.display=mp.style.display==="none"?"flex":"none";
};

/* username click */
$("uName").onclick=startEditName;
$("uEditIcon").onclick=startEditName;

/* online/offline */
window.addEventListener("online",()=>{setStatus("back online",true);connectSocket(true)});
window.addEventListener("offline",()=>setStatus("offline",false,false));

/* keyboard shortcuts */
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==="k"){e.preventDefault();$("searchInput").focus();$("searchInput").select()}
  if(e.key==="Escape"&&document.activeElement===$("searchInput")){
    $("searchInput").value="";
    renderMessages();
    $("msgInput").focus();
  }
});

/* ════════════════════════════════
   INIT
════════════════════════════════ */
buildEmojiPicker();
renderUserPanel();
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
    return {"ok": True, "app": "LinkUp", "version": "2.0"}


@app.get("/api")
def api_root():
    return {"message": "LinkUp API v2", "websocket": "/ws/{room_id}"}


@app.get("/{path:path}", response_class=HTMLResponse)
def spa_fallback(path: str):
    return HTMLResponse(content=page_html())



