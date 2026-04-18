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

# ----------------------------
# Config
# ----------------------------

class Settings:
    def __init__(self) -> None:
        origins = os.getenv("CORS_ORIGINS", "*").strip()
        self.cors_origins = ["*"] if origins == "*" else [o.strip() for o in origins.split(",") if o.strip()]


settings = Settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ws_router = APIRouter()

ROOMS_META = [
    {"id": "general", "name": "general", "desc": "Town square for everything."},
    {"id": "dev", "name": "dev", "desc": "Builds, bugs, and code."},
    {"id": "clips", "name": "clips", "desc": "Short clips and highlights."},
    {"id": "study", "name": "study", "desc": "Focus, notes, learning."},
]

SEARCH_HINTS = ["#python", "#games", "#art", "#music", "#ai", "#clips", "#buildinpublic"]


def now_ms() -> int:
    return int(time.time() * 1000)


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def clamp_text(value: str, length: int) -> str:
    value = (value or "").strip()
    return value[:length] if value else ""


def serialize_profile(profile: dict) -> dict:
    return {
        "client_id": profile["client_id"],
        "name": profile.get("name", "Guest"),
        "avatar": profile.get("avatar", ""),
        "bio": profile.get("bio", ""),
        "accent": profile.get("accent", "#5865f2"),
        "joined_at": profile.get("joined_at", now_ms()),
    }


def serialize_comment(comment: dict) -> dict:
    return {
        "id": comment["id"],
        "client_id": comment["client_id"],
        "name": comment.get("name", "Guest"),
        "avatar": comment.get("avatar", ""),
        "text": comment.get("text", ""),
        "created_at": comment.get("created_at", now_ms()),
    }


def serialize_post(post: dict) -> dict:
    return {
        "id": post["id"],
        "client_id": post["client_id"],
        "name": post.get("name", "Guest"),
        "avatar": post.get("avatar", ""),
        "bio": post.get("bio", ""),
        "text": post.get("text", ""),
        "media_url": post.get("media_url", ""),
        "media_type": post.get("media_type", "text"),
        "created_at": post.get("created_at", now_ms()),
        "likes": len(post.get("likes", set())),
        "reposts": len(post.get("reposts", set())),
        "comments": [serialize_comment(c) for c in post.get("comments", [])][-24:],
    }


def serialize_message(msg: dict) -> dict:
    return {
        "msg_id": msg["msg_id"],
        "client_id": msg["client_id"],
        "name": msg.get("name", "Guest"),
        "avatar": msg.get("avatar", ""),
        "text": msg.get("text", ""),
        "room": msg.get("room", "general"),
        "created_at": msg.get("created_at", now_ms()),
        "reply_to": msg.get("reply_to"),
        "reply_preview": msg.get("reply_preview"),
        "reactions": msg.get("reactions", {}),
        "pinned": msg.get("pinned", False),
    }


# ----------------------------
# Global feed state
# ----------------------------

FEED: List[dict] = []
FEED_INDEX: Dict[str, dict] = {}


def seed_feed() -> None:
    if FEED:
        return

    starter = [
        {
            "client_id": "seed_linkup",
            "name": "LinkUp",
            "avatar": "",
            "bio": "official",
            "text": "Welcome to LinkUp — a hybrid timeline + live rooms app.",
            "media_url": "",
            "media_type": "text",
        },
        {
            "client_id": "seed_nova",
            "name": "Nova",
            "avatar": "",
            "bio": "clipper",
            "text": "You can post updates here and still jump into live rooms below.",
            "media_url": "",
            "media_type": "text",
        },
    ]

    for item in starter:
        post = {
            "id": uid("post"),
            **item,
            "created_at": now_ms(),
            "likes": set(),
            "reposts": set(),
            "comments": [],
        }
        FEED.append(post)
        FEED_INDEX[post["id"]] = post


seed_feed()


def trending_posts() -> List[dict]:
    ranked = sorted(
        FEED,
        key=lambda p: (len(p.get("likes", set())) + len(p.get("reposts", set())) + len(p.get("comments", []))),
        reverse=True,
    )
    return [serialize_post(p) for p in ranked[:8]]


# ----------------------------
# Room state
# ----------------------------

class RoomState:
    def __init__(self) -> None:
        self.sockets: Dict[str, Dict[str, WebSocket]] = defaultdict(dict)
        self.members: Dict[str, Dict[str, dict]] = defaultdict(dict)
        self.history: Dict[str, List[dict]] = defaultdict(list)
        self.pins: Dict[str, List[dict]] = defaultdict(list)
        self.reactions: Dict[str, Dict[str, Dict[str, set]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))

    def register(self, room: str, profile: dict, ws: WebSocket) -> None:
        self.members[room][profile["client_id"]] = profile
        self.sockets[room][profile["client_id"]] = ws

    def remove(self, room: str, client_id: str) -> None:
        self.sockets[room].pop(client_id, None)
        self.members[room].pop(client_id, None)

    def users(self, room: str) -> List[dict]:
        users = [
            serialize_profile(profile)
            for cid, profile in self.members[room].items()
            if cid in self.sockets[room]
        ]
        users.sort(key=lambda u: (u.get("name", "").lower(), u.get("joined_at", 0)))
        return users

    def history_view(self, room: str) -> List[dict]:
        return [serialize_message(m) for m in self.history[room]][-180:]

    def pins_view(self, room: str) -> List[dict]:
        return self.pins[room][-30:]

    def add_message(self, room: str, msg: dict) -> None:
        self.history[room].append(msg)
        if len(self.history[room]) > 400:
            self.history[room] = self.history[room][-400:]

    def find_message(self, room: str, msg_id: str) -> Optional[dict]:
        for m in self.history[room]:
            if m["msg_id"] == msg_id:
                return m
        return None

    def toggle_pin(self, room: str, msg_id: str) -> Optional[dict]:
        msg = self.find_message(room, msg_id)
        if not msg:
            return None

        existing = next((p for p in self.pins[room] if p["id"] == msg_id), None)
        if existing:
            self.pins[room] = [p for p in self.pins[room] if p["id"] != msg_id]
            msg["pinned"] = False
            return {"action": "unpinned", "message": serialize_message(msg), "pins": self.pins_view(room)}

        pin = {
            "id": msg["msg_id"],
            "name": msg.get("name", "Guest"),
            "text": msg.get("text", ""),
            "created_at": msg.get("created_at", now_ms()),
        }
        self.pins[room].append(pin)
        msg["pinned"] = True
        return {"action": "pinned", "message": serialize_message(msg), "pins": self.pins_view(room)}

    def toggle_chat_reaction(self, room: str, msg_id: str, emoji: str, client_id: str) -> Optional[dict]:
        msg = self.find_message(room, msg_id)
        if not msg:
            return None

        bucket = self.reactions[room][msg_id][emoji]
        if client_id in bucket:
            bucket.remove(client_id)
        else:
            bucket.add(client_id)

        msg["reactions"] = {e: len(users) for e, users in self.reactions[room][msg_id].items()}
        return {"msg_id": msg_id, "reactions": msg["reactions"]}

    async def broadcast_room(self, room: str, payload: dict) -> None:
        dead = []
        for cid, ws in self.sockets[room].items():
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self.remove(room, cid)

    async def broadcast_all(self, payload: dict) -> None:
        for room in list(self.sockets.keys()):
            await self.broadcast_room(room, payload)


ROOMS = RoomState()

# ----------------------------
# Profiles
# ----------------------------

PROFILES: Dict[str, dict] = {}


def get_profile(client_id: str) -> dict:
    if client_id not in PROFILES:
        PROFILES[client_id] = {
            "client_id": client_id,
            "name": "Guest",
            "avatar": "",
            "bio": "",
            "accent": "#5865f2",
            "joined_at": now_ms(),
        }
    return PROFILES[client_id]


# ----------------------------
# Push helpers
# ----------------------------

async def push_presence(room: str) -> None:
    await ROOMS.broadcast_room(
        room,
        {
            "kind": "presence",
            "room": room,
            "users": ROOMS.users(room),
            "time": now_ms(),
        },
    )


async def push_profile(profile: dict) -> None:
    await ROOMS.broadcast_all(
        {
            "kind": "profile_update",
            "profile": serialize_profile(profile),
            "time": now_ms(),
        }
    )


async def push_feed_post(post: dict) -> None:
    await ROOMS.broadcast_all(
        {
            "kind": "feed_post",
            "post": serialize_post(post),
            "time": now_ms(),
        }
    )


async def push_feed_update(post: dict) -> None:
    await ROOMS.broadcast_all(
        {
            "kind": "feed_update",
            "post": serialize_post(post),
            "time": now_ms(),
        }
    )


async def push_chat_message(room: str, msg: dict) -> None:
    await ROOMS.broadcast_room(
        room,
        {
            "kind": "chat_message",
            "message": serialize_message(msg),
        },
    )


async def push_pin_update(room: str, result: dict) -> None:
    await ROOMS.broadcast_room(
        room,
        {
            "kind": "pin_update",
            **result,
            "time": now_ms(),
        },
    )


async def push_reaction_update(room: str, result: dict) -> None:
    await ROOMS.broadcast_room(
        room,
        {
            "kind": "reaction_update",
            **result,
            "time": now_ms(),
        },
    )


async def push_typing(room: str, name: str, client_id: str) -> None:
    await ROOMS.broadcast_room(
        room,
        {
            "kind": "typing",
            "client_id": client_id,
            "name": name,
            "time": now_ms(),
        },
    )


async def push_system(room: str, text: str) -> None:
    await ROOMS.broadcast_room(
        room,
        {
            "kind": "system",
            "msg_id": uid("sys"),
            "name": "System",
            "text": text,
            "room": room,
            "created_at": now_ms(),
        },
    )


async def push_init(room: str, ws: WebSocket, client_id: str) -> None:
    await ws.send_json(
        {
            "kind": "init",
            "client_id": client_id,
            "room": room,
            "users": ROOMS.users(room),
            "history": ROOMS.history_view(room),
            "pins": ROOMS.pins_view(room),
            "feed": [serialize_post(p) for p in FEED][-80:],
            "feed_trending": trending_posts(),
            "rooms": ROOMS_META,
            "suggestions": SEARCH_HINTS,
            "time": now_ms(),
        }
    )


async def push_pong(ws: WebSocket) -> None:
    await ws.send_json({"kind": "pong", "time": now_ms()})


# ----------------------------
# Action handlers
# ----------------------------

async def set_identity(room: str, client_id: str, name: str, avatar: str, bio: str, ws: WebSocket) -> dict:
    profile = get_profile(client_id)
    profile["name"] = clamp_text(name, 24) or "Guest"
    profile["avatar"] = avatar.strip()
    profile["bio"] = clamp_text(bio, 80)
    ROOMS.register(room, profile, ws)
    return profile


async def handle_message(room: str, profile: dict, data: dict) -> None:
    text = clamp_text(str(data.get("text") or ""), 5000)
    if not text:
        return

    reply_to = data.get("reply_to")
    reply_preview = None
    if reply_to:
        prev = ROOMS.find_message(room, str(reply_to))
        if prev:
            reply_preview = {
                "msg_id": prev["msg_id"],
                "name": prev.get("name", "Guest"),
                "text": prev.get("text", "")[:120],
            }

    message = {
        "msg_id": uid("msg"),
        "client_id": profile["client_id"],
        "name": profile["name"],
        "avatar": profile["avatar"],
        "text": text,
        "room": room,
        "created_at": now_ms(),
        "reply_to": reply_to,
        "reply_preview": reply_preview,
        "reactions": {},
        "pinned": False,
    }
    ROOMS.add_message(room, message)
    await push_chat_message(room, message)


async def handle_post(profile: dict, data: dict) -> None:
    text = clamp_text(str(data.get("text") or ""), 5000)
    media_url = str(data.get("media_url") or "").strip()
    media_type = str(data.get("media_type") or "text").strip()

    if not text and not media_url:
        return

    post = {
        "id": uid("post"),
        "client_id": profile["client_id"],
        "name": profile["name"],
        "avatar": profile["avatar"],
        "bio": profile["bio"],
        "text": text,
        "media_url": media_url,
        "media_type": media_type,
        "created_at": now_ms(),
        "likes": set(),
        "reposts": set(),
        "comments": [],
    }
    FEED.insert(0, post)
    FEED_INDEX[post["id"]] = post
    await push_feed_post(post)


async def handle_feed_like(profile: dict, data: dict) -> None:
    pid = str(data.get("post_id") or "")
    post = FEED_INDEX.get(pid)
    if not post:
        return

    likes = post["likes"]
    if profile["client_id"] in likes:
        likes.remove(profile["client_id"])
    else:
        likes.add(profile["client_id"])

    await push_feed_update(post)


async def handle_feed_repost(profile: dict, data: dict) -> None:
    pid = str(data.get("post_id") or "")
    post = FEED_INDEX.get(pid)
    if not post:
        return

    reps = post["reposts"]
    if profile["client_id"] in reps:
        reps.remove(profile["client_id"])
    else:
        reps.add(profile["client_id"])

    await push_feed_update(post)


async def handle_feed_comment(profile: dict, data: dict) -> None:
    pid = str(data.get("post_id") or "")
    post = FEED_INDEX.get(pid)
    if not post:
        return

    text = clamp_text(str(data.get("text") or ""), 1000)
    if not text:
        return

    comment = {
        "id": uid("c"),
        "client_id": profile["client_id"],
        "name": profile["name"],
        "avatar": profile["avatar"],
        "text": text,
        "created_at": now_ms(),
    }
    post["comments"].append(comment)
    await push_feed_update(post)


# ----------------------------
# WebSocket
# ----------------------------

@ws_router.websocket("/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    client_id = "cli_" + uuid4().hex[:10]
    profile = get_profile(client_id)
    active_room = room_id
    connected = False

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}

            kind = str(data.get("kind") or "hello")

            if kind == "ping":
                await push_pong(websocket)
                continue

            if kind == "hello":
                client_id = str(data.get("client_id") or client_id)
                profile = await set_identity(
                    active_room,
                    client_id,
                    str(data.get("name") or profile["name"]),
                    str(data.get("avatar") or profile.get("avatar") or ""),
                    str(data.get("bio") or profile.get("bio") or ""),
                    websocket,
                )
                active_room = str(data.get("room") or room_id)

                if not connected:
                    connected = True

                await push_init(active_room, websocket, client_id)
                await push_presence(active_room)
                await push_profile(profile)
                continue

            if kind == "profile":
                old_name = profile["name"]
                profile = await set_identity(
                    active_room,
                    client_id,
                    str(data.get("name") or profile["name"]),
                    str(data.get("avatar") or profile.get("avatar") or ""),
                    str(data.get("bio") or profile.get("bio") or ""),
                    websocket,
                )
                await push_profile(profile)
                await push_presence(active_room)
                if old_name != profile["name"]:
                    await push_system(active_room, f"{old_name} is now {profile['name']}")
                continue

            if kind == "typing":
                await push_typing(active_room, profile["name"], client_id)
                continue

            if kind == "chat":
                await handle_message(active_room, profile, data)
                continue

            if kind == "pin_chat":
                result = ROOMS.toggle_pin(active_room, str(data.get("msg_id") or ""))
                if result:
                    await push_pin_update(active_room, result)
                continue

            if kind == "react_chat":
                result = ROOMS.toggle_chat_reaction(
                    active_room,
                    str(data.get("msg_id") or ""),
                    str(data.get("emoji") or "👍")[:4],
                    client_id,
                )
                if result:
                    await push_reaction_update(active_room, result)
                continue

            if kind == "post":
                await handle_post(profile, data)
                continue

            if kind == "feed_like":
                await handle_feed_like(profile, data)
                continue

            if kind == "feed_repost":
                await handle_feed_repost(profile, data)
                continue

            if kind == "feed_comment":
                await handle_feed_comment(profile, data)
                continue

    except WebSocketDisconnect:
        ROOMS.remove(active_room, client_id)
        await push_presence(active_room)
    except Exception:
        ROOMS.remove(active_room, client_id)
        await push_presence(active_room)


# ----------------------------
# Routes
# ----------------------------

@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(page_html())


@app.get("/health")
def health():
    return {"ok": True, "app": "LinkUp"}


@app.get("/api")
def api():
    return {"message": "LinkUp is live", "websocket": "/ws/{room_id}"}


@app.get("/{path:path}", response_class=HTMLResponse)
def spa(path: str):
    return HTMLResponse(page_html())


# ----------------------------
# UI
# ----------------------------

def page_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LinkUp</title>
<style>
:root{
  --bg:#313338;
  --panel:#2b2d31;
  --panel2:#1e1f22;
  --text:#f2f3f5;
  --muted:#b5bac1;
  --accent:#5865f2;
  --accent2:#3ba55d;
  --line:rgba(255,255,255,.06);
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;
  font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  background:var(--bg);
  color:var(--text);
  overflow:hidden;
}
button,input,textarea,select{font:inherit}
.app{
  height:100vh;
  display:grid;
  grid-template-columns:72px 248px minmax(0,1fr) 300px;
  min-width:0;
}
.rail{
  background:var(--panel2);
  border-right:1px solid var(--line);
  display:flex;
  flex-direction:column;
  overflow:hidden;
}
.serverrail{
  padding:12px 0;
  align-items:center;
  gap:10px;
}
.serverbtn{
  width:48px;
  height:48px;
  border:none;
  border-radius:16px;
  background:#383a40;
  color:#fff;
  font-weight:800;
  cursor:pointer;
}
.serverbtn.active{background:var(--accent)}
.sidebar{
  background:var(--panel);
  border-right:1px solid var(--line);
  display:flex;
  flex-direction:column;
  min-width:0;
  overflow:hidden;
}
.rightbar{
  background:var(--panel);
  border-left:1px solid var(--line);
  display:flex;
  flex-direction:column;
  min-width:0;
  overflow:hidden;
}
.head{
  padding:16px;
  border-bottom:1px solid var(--line);
  flex:0 0 auto;
}
.brand{
  display:flex;
  justify-content:space-between;
  gap:10px;
  align-items:flex-start;
}
.brand h1{margin:0;font-size:16px}
.brand small{display:block;color:var(--muted);font-size:12px;margin-top:3px}
.pill{
  display:inline-flex;
  align-items:center;
  margin-top:10px;
  padding:6px 10px;
  border-radius:999px;
  background:rgba(255,255,255,.04);
  color:var(--muted);
  font-size:12px;
}
.scroll{
  min-height:0;
  overflow-y:auto;
  flex:1;
}
.section{
  padding:10px 8px 8px;
}
.sectiontitle{
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
  padding:10px;
  margin-bottom:2px;
  background:transparent;
  color:#fff;
  display:flex;
  justify-content:space-between;
  gap:10px;
  text-align:left;
  cursor:pointer;
}
.channel.active,.channel:hover{background:rgba(255,255,255,.04)}
.channel .meta{
  min-width:0;
  display:flex;
  flex-direction:column;
}
.channel strong{font-size:15px}
.channel span{
  font-size:12px;
  color:var(--muted);
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
  max-width:160px;
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
  overflow:hidden;
}
.topbar{
  height:50px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  padding:0 16px;
  border-bottom:1px solid var(--line);
  background:rgba(49,51,56,.95);
}
.titleblock{
  display:flex;
  align-items:center;
  gap:12px;
  min-width:0;
}
.titleblock h2{margin:0;font-size:15px}
.titleblock p{
  margin:0;
  color:var(--muted);
  font-size:12px;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
  max-width:40vw;
}
.conn{
  display:flex;
  gap:8px;
  align-items:center;
  color:var(--muted);
  font-size:12px;
  flex:0 0 auto;
}
.dot{
  width:10px;
  height:10px;
  border-radius:999px;
  background:var(--accent2);
  box-shadow:0 0 0 4px rgba(59,165,93,.1);
}
.toolbar{
  display:flex;
  gap:10px;
  align-items:center;
}
.tabs{
  display:flex;
  gap:8px;
}
.tab{
  border:none;
  background:#40444b;
  color:#fff;
  padding:8px 12px;
  border-radius:999px;
  cursor:pointer;
}
.tab.active{background:var(--accent)}
.search{
  width:260px;
  max-width:30vw;
  padding:9px 12px;
  border:none;
  border-radius:8px;
  background:#1e1f22;
  color:#fff;
  outline:none;
}
.content{
  min-height:0;
  overflow-y:auto;
  flex:1;
  padding:14px;
}
.pane{display:none}
.pane.active{display:block}
.box{
  padding:14px;
  border-radius:10px;
  background:#2b2d31;
  border:1px solid var(--line);
  margin-bottom:12px;
}
.label{
  color:var(--muted);
  font-size:12px;
  margin-bottom:8px;
}
.row{display:flex;gap:10px;flex-wrap:wrap}
.input,.textarea,select{
  border:none;
  outline:none;
  background:#1e1f22;
  color:#fff;
  border-radius:8px;
  padding:10px 12px;
}
.textarea{
  width:100%;
  min-height:96px;
  max-height:170px;
  overflow-y:auto;
  resize:none;
}
.input{flex:1;min-width:180px}
.select{padding:10px 12px}
.primary{
  border:none;
  background:var(--accent);
  color:#fff;
  border-radius:8px;
  padding:10px 14px;
  cursor:pointer;
  font-weight:700;
}
.secondary{
  border:none;
  background:#40444b;
  color:#fff;
  border-radius:8px;
  padding:10px 14px;
  cursor:pointer;
}
.post,.msgbox{
  padding:14px;
  border-radius:10px;
  background:#2b2d31;
  border:1px solid var(--line);
  margin-bottom:10px;
}
.posthead,.msghead{
  display:flex;
  justify-content:space-between;
  gap:10px;
  align-items:center;
}
.person{
  display:flex;
  gap:10px;
  align-items:center;
  min-width:0;
}
.avatar{
  width:40px;
  height:40px;
  border-radius:50%;
  background:var(--accent);
  display:grid;
  place-items:center;
  font-weight:800;
  flex:0 0 auto;
  overflow:hidden;
}
.avatarimg{
  width:40px;
  height:40px;
  border-radius:50%;
  object-fit:cover;
  flex:0 0 auto;
  background:#40444b;
}
.person b{display:block;font-size:14px}
.person span,.stamp{font-size:12px;color:var(--muted)}
.posttext,.msgtext{
  margin-top:10px;
  line-height:1.55;
  white-space:pre-wrap;
  word-break:break-word;
}
.media{
  margin-top:12px;
  border-radius:10px;
  overflow:hidden;
  background:#111;
}
.media img,.media video{
  display:block;
  width:100%;
  max-height:440px;
  object-fit:cover;
}
.actions{
  display:flex;
  gap:8px;
  flex-wrap:wrap;
  margin-top:10px;
}
.chip{
  border:none;
  background:#40444b;
  color:#fff;
  padding:7px 10px;
  border-radius:999px;
  cursor:pointer;
  font-size:12px;
}
.chip.active{background:rgba(88,101,242,.9)}
.replybox{
  margin-top:8px;
  padding:8px 10px;
  border-left:3px solid var(--accent);
  background:rgba(88,101,242,.08);
  border-radius:8px;
  font-size:12px;
  color:#dfe4ff;
}
.list{
  padding:0 8px 8px;
  overflow-y:auto;
}
.user{
  display:flex;
  justify-content:space-between;
  gap:8px;
  align-items:flex-start;
  padding:10px;
  border-radius:8px;
}
.user:hover{background:rgba(255,255,255,.04)}
.user .left{min-width:0}
.user .left strong{
  display:block;
  font-size:14px;
}
.user .left span{
  display:block;
  color:var(--muted);
  font-size:12px;
  margin-top:2px;
}
.pills{
  display:flex;
  flex-wrap:wrap;
  gap:8px;
  padding:0 8px 8px;
}
.pill2{
  padding:7px 10px;
  border-radius:999px;
  background:rgba(255,255,255,.05);
  font-size:12px;
  color:#d7dae0;
}
.smallnote{
  padding:12px 16px;
  color:var(--muted);
  font-size:12px;
  line-height:1.6;
}
@media (max-width:1100px){
  .app{grid-template-columns:72px 240px minmax(0,1fr)}
  .rightbar{display:none}
  .search{width:180px;max-width:35vw}
}
@media (max-width:820px){
  body{overflow:auto}
  .app{
    height:auto;
    min-height:100vh;
    grid-template-columns:1fr;
  }
  .sidebar,.rightbar{display:none}
  .main{min-height:100vh}
  .search{display:none}
}
</style>
</head>
<body>
<div class="app">
  <aside class="rail serverrail" id="servers"></aside>

  <aside class="sidebar">
    <div class="head">
      <div class="brand">
        <div>
          <h1>LinkUp</h1>
          <small>Twitter x Discord hybrid</small>
        </div>
        <span class="badge">LIVE</span>
      </div>
      <div class="pill">Room: <span id="roomLabel">#general</span></div>
    </div>
    <div class="scroll">
      <div class="section">
        <div class="sectiontitle">Channels</div>
        <div id="channels"></div>
      </div>
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div class="titleblock">
        <h2 id="channelTitle"># general</h2>
        <p id="channelDescription">A flatter, cleaner room for chat and posts.</p>
      </div>
      <div class="toolbar">
        <div class="tabs">
          <button class="tab active" data-tab="feed">Feed</button>
          <button class="tab" data-tab="chat">Chat</button>
        </div>
        <input id="searchInput" class="search" placeholder="Search feed or chat...">
        <div class="conn"><span class="dot"></span><span id="connState">connecting...</span></div>
      </div>
    </div>

    <div class="content">
      <div id="feedPane" class="pane active">
        <div class="box">
          <div class="label">Create a post</div>
          <textarea id="postText" class="textarea" placeholder="What's happening?"></textarea>
          <div class="row" style="margin-top:10px">
            <input id="mediaUrl" class="input" placeholder="Media URL (image / video / clip)">
            <select id="mediaType" class="select">
              <option value="text">Text</option>
              <option value="image">Image</option>
              <option value="video">Video</option>
            </select>
            <button class="primary" id="postBtn">Post</button>
          </div>
        </div>
        <div id="feedList"></div>
      </div>

      <div id="chatPane" class="pane">
        <div class="box">
          <div class="label">Message the room</div>
          <textarea id="messageInput" class="textarea" placeholder="Message LinkUp..."></textarea>
          <div class="row" style="margin-top:10px">
            <button class="primary" id="sendBtn">Send</button>
            <button class="secondary" id="clearReplyBtn">Clear reply</button>
          </div>
        </div>
        <div id="messages"></div>
      </div>
    </div>
  </main>

  <aside class="rightbar">
    <div class="head">
      <div class="brand">
        <div>
          <h1>Your profile</h1>
          <small>change it anytime</small>
        </div>
      </div>
    </div>
    <div class="scroll">
      <div class="section">
        <div class="label">Profile</div>
        <div class="box">
          <input id="nameInput" class="input" placeholder="Username">
          <input id="avatarInput" class="input" placeholder="Avatar URL" style="margin-top:8px">
          <textarea id="bioInput" class="textarea" placeholder="Bio" style="min-height:70px;margin-top:8px"></textarea>
          <button class="primary" id="profileBtn" style="margin-top:8px">Save profile</button>
        </div>
      </div>

      <div class="section">
        <div class="label">Online now</div>
        <div id="members" class="list"></div>
      </div>

      <div class="section">
        <div class="label">Pinned messages</div>
        <div id="pins" class="pills"></div>
      </div>

      <div class="section">
        <div class="label">Trending posts</div>
        <div id="trending" class="list"></div>
      </div>

      <div class="smallnote">
        Users can change username and avatar anytime. No hard lock, no nonsense.
      </div>
    </div>
  </aside>
</div>

<script>
(() => {
  const rooms = [
    { id: "general", name: "general", desc: "Town square for everything." },
    { id: "dev", name: "dev", desc: "Builds, bugs, and code." },
    { id: "clips", name: "clips", desc: "Short clips and highlights." },
    { id: "study", name: "study", desc: "Focus, notes, learning." },
  ];

  const el = (id) => document.getElementById(id);

  const state = {
    tab: "feed",
    server: localStorage.getItem("linkup_server") || "main",
    channel: localStorage.getItem("linkup_channel") || "general",
    clientId: localStorage.getItem("linkup_client_id") || ("cli_" + Math.random().toString(36).slice(2, 10)),
    name: localStorage.getItem("linkup_name") || "You",
    avatar: localStorage.getItem("linkup_avatar") || "",
    bio: localStorage.getItem("linkup_bio") || "",
    socket: null,
    seq: 0,
    reconnectTimer: null,
    typingTimer: null,
    replyTo: null,
    connectedOnce: false,
    data: {
      feed: [],
      messages: [],
      users: [],
      pins: [],
      trending: [],
    },
  };

  localStorage.setItem("linkup_client_id", state.clientId);

  const serverRail = el("servers");
  const channelRail = el("channels");
  const feedList = el("feedList");
  const messagesList = el("messages");
  const membersList = el("members");
  const pinsList = el("pins");
  const trendingList = el("trending");

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (m) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[m]));
  }

  function ts(t) {
    return new Date(t || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function roomId() {
    return `${state.server}:${state.channel}`;
  }

  function currentRoomName() {
    return `#${state.channel}`;
  }

  function wsUrl() {
    return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/${encodeURIComponent(roomId())}`;
  }

  function setConn(text) {
    el("connState").textContent = text;
  }

  function send(kind, payload) {
    if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return;
    state.socket.send(JSON.stringify({ kind, client_id: state.clientId, room: roomId(), ...payload }));
  }

  function flattenSearch(item) {
    return JSON.stringify(item).toLowerCase();
  }

  function renderServers() {
    serverRail.innerHTML = `
      <div style="height:8px"></div>
      ${["main", "work", "game", "study"].map((s) => `
        <button class="serverbtn ${state.server === s ? "active" : ""}" data-server="${s}">${s.slice(0,1).toUpperCase()}</button>
      `).join("")}
    `;
    serverRail.querySelectorAll(".serverbtn").forEach((btn) => {
      btn.onclick = () => {
        state.server = btn.dataset.server;
        localStorage.setItem("linkup_server", state.server);
        state.channel = "general";
        localStorage.setItem("linkup_channel", state.channel);
        renderAll();
        connect();
      };
    });
  }

  function renderChannels() {
    channelRail.innerHTML = `
      <div class="sectiontitle">Channels</div>
      ${rooms.map((r) => `
        <button class="channel ${state.channel === r.id ? "active" : ""}" data-channel="${r.id}">
          <div class="meta"><strong># ${esc(r.name)}</strong><span>${esc(r.desc)}</span></div>
          <span class="badge">#</span>
        </button>
      `).join("")}
    `;
    channelRail.querySelectorAll(".channel").forEach((btn) => {
      btn.onclick = () => {
        state.channel = btn.dataset.channel;
        localStorage.setItem("linkup_channel", state.channel);
        renderAll();
        connect();
      };
    });
  }

  function feedFiltered() {
    const q = el("searchInput").value.trim().toLowerCase();
    return !q ? state.data.feed : state.data.feed.filter((p) => flattenSearch(p).includes(q));
  }

  function chatFiltered() {
    const q = el("searchInput").value.trim().toLowerCase();
    return !q ? state.data.messages : state.data.messages.filter((m) => flattenSearch(m).includes(q));
  }

  function renderFeed() {
    feedList.innerHTML = feedFiltered().map((p) => {
      const comments = (p.comments || []).slice(-3).map((c) => `
        <div style="margin-top:8px;padding:8px 10px;border-radius:8px;background:#1e1f22">
          <b style="font-size:13px">${esc(c.name)}</b>
          <div style="color:#b5bac1;font-size:12px;margin-top:2px">${esc(c.text)}</div>
        </div>
      `).join("");

      const media =
        p.media_url
          ? (
              p.media_type === "video" || /\.(mp4|webm|mov|m4v)(\?.*)?$/i.test(p.media_url)
                ? `<div class="media"><video controls src="${esc(p.media_url)}"></video></div>`
                : `<div class="media"><img src="${esc(p.media_url)}" alt=""></div>`
            )
          : "";

      return `
        <div class="post" data-post="${esc(p.id)}">
          <div class="posthead">
            <div class="person">
              ${p.avatar ? `<img class="avatarimg" src="${esc(p.avatar)}" alt="">` : `<div class="avatar">${esc((p.name || "?").slice(0,1).toUpperCase())}</div>`}
              <div>
                <b>${esc(p.name || "Guest")}</b>
                <span>${ts(p.created_at)} · ${esc(p.bio || "")}</span>
              </div>
            </div>
          </div>
          <div class="posttext">${esc(p.text || "")}</div>
          ${media}
          <div class="actions">
            <button class="chip" data-pact="like" data-id="${esc(p.id)}">Like ${p.likes || 0}</button>
            <button class="chip" data-pact="repost" data-id="${esc(p.id)}">Repost ${p.reposts || 0}</button>
            <button class="chip" data-pact="comment" data-id="${esc(p.id)}">Comment ${p.comments?.length || 0}</button>
          </div>
          ${comments}
        </div>
      `;
    }).join("");
    bindFeedActions();
  }

  function renderMessages() {
    messagesList.innerHTML = chatFiltered().map((m) => {
      const reacts = Object.entries(m.reactions || {}).map(([e, n]) => `<span class="badge">${esc(e)} ${n}</span>`).join(" ");
      return `
        <div class="msgbox" data-id="${esc(m.msg_id)}">
          <div class="msghead">
            <div class="person">
              ${m.avatar ? `<img class="avatarimg" src="${esc(m.avatar)}" alt="">` : `<div class="avatar">${esc((m.name || "?").slice(0,1).toUpperCase())}</div>`}
              <div>
                <b>${esc(m.name || "Guest")}</b>
                <span>${ts(m.created_at)}</span>
              </div>
            </div>
            <span class="stamp">${m.pinned ? "pinned" : ""}</span>
          </div>
          ${m.reply_preview ? `<div class="replybox">Replying to <b>${esc(m.reply_preview.name)}</b><br>${esc(m.reply_preview.text)}</div>` : ""}
          <div class="msgtext">${esc(m.text || "")}</div>
          <div class="actions">
            <button class="chip" data-act="reply" data-id="${esc(m.msg_id)}">Reply</button>
            <button class="chip" data-act="pin" data-id="${esc(m.msg_id)}">${m.pinned ? "Unpin" : "Pin"}</button>
            <button class="chip" data-act="react" data-id="${esc(m.msg_id)}">React</button>
            ${reacts}
          </div>
        </div>
      `;
    }).join("");
    bindMessageActions();
  }

  function renderMembers() {
    membersList.innerHTML = (state.data.users || []).map((u) => `
      <div class="user">
        <div class="left">
          <strong>${esc(u.name)}</strong>
          <span>${esc(u.bio || "online")}</span>
        </div>
        <span class="badge">live</span>
      </div>
    `).join("") || `<div class="smallnote">No one here yet.</div>`;
  }

  function renderPins() {
    pinsList.innerHTML = (state.data.pins || []).slice().reverse().map((p) => `
      <div class="pill2" data-pin="${esc(p.id)}">${esc(p.name)}: ${esc((p.text || "").slice(0, 60))}</div>
    `).join("") || `<div class="pill2">No pins yet</div>`;

    pinsList.querySelectorAll("[data-pin]").forEach((node) => {
      node.onclick = () => {
        const target = messagesList.querySelector(`[data-id="${CSS.escape(node.dataset.pin)}"]`);
        if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
      };
    });
  }

  function renderTrending() {
    trendingList.innerHTML = (state.data.trending || []).map((p) => `
      <div class="user">
        <div class="left">
          <strong>${esc(p.name)}</strong>
          <span>${esc((p.text || "").slice(0, 55))}</span>
        </div>
        <span class="badge">${(p.likes || 0) + (p.reposts || 0)}</span>
      </div>
    `).join("") || `<div class="smallnote">Nothing trending yet.</div>`;
  }

  function renderProfileFields() {
    el("nameInput").value = state.name;
    el("avatarInput").value = state.avatar;
    el("bioInput").value = state.bio;
  }

  function renderTop() {
    el("roomLabel").textContent = currentRoomName();
    el("channelTitle").textContent = `# ${state.channel}`;
    el("channelDescription").textContent =
      state.tab === "feed"
        ? "A timeline for posts, clips, and updates."
        : "A roomy live chat with smooth scrolling.";
  }

  function renderTabs() {
    document.querySelectorAll(".tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === state.tab);
    });
    el("feedPane").classList.toggle("active", state.tab === "feed");
    el("chatPane").classList.toggle("active", state.tab === "chat");
  }

  function renderAll() {
    renderServers();
    renderChannels();
    renderTop();
    renderTabs();
    renderProfileFields();
    renderFeed();
    renderMessages();
    renderMembers();
    renderPins();
    renderTrending();
  }

  function optimisticAddChat(msg) {
    state.data.messages.push(msg);
    renderMessages();
  }

  function optimisticAddPost(post) {
    state.data.feed.unshift(post);
    renderFeed();
    renderTrending();
  }

  function connect() {
    const seq = ++state.seq;

    if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) {
      try { state.socket.close(1000, "switch"); } catch {}
    }

    clearTimeout(state.reconnectTimer);
    setConn("connecting...");

    state.socket = new WebSocket(wsUrl());

    state.socket.onopen = () => {
      if (seq !== state.seq) return;

      state.connectedOnce = true;
      setConn("connected");

      state.socket.send(JSON.stringify({
        kind: "hello",
        client_id: state.clientId,
        room: roomId(),
        name: state.name,
        avatar: state.avatar,
        bio: state.bio,
      }));

      state.socket.send(JSON.stringify({
        kind: "ping",
        client_id: state.clientId,
        room: roomId(),
        time: Date.now(),
      }));
    };

    state.socket.onmessage = (e) => {
      if (seq !== state.seq) return;

      const data = JSON.parse(e.data);

      if (data.kind === "pong") {
        if (state.connectedOnce) setConn("connected");
        return;
      }

      if (data.kind === "init") {
        state.data.users = data.users || [];
        state.data.messages = data.history || [];
        state.data.pins = data.pins || [];
        state.data.feed = data.feed || [];
        state.data.trending = data.feed_trending || [];
        renderAll();
        if (state.connectedOnce) setConn("connected");
        return;
      }

      if (data.kind === "presence") {
        state.data.users = data.users || [];
        renderMembers();
        return;
      }

      if (data.kind === "profile_update") {
        const p = data.profile || {};
        const idx = (state.data.users || []).findIndex((u) => u.client_id === p.client_id);
        if (idx >= 0) state.data.users[idx] = p;
        else state.data.users = [...(state.data.users || []), p];
        renderMembers();
        renderTrending();
        return;
      }

      if (data.kind === "chat_message") {
        if (!state.data.messages.some((m) => m.msg_id === data.message.msg_id)) {
          state.data.messages.push(data.message);
          renderMessages();
        }
        return;
      }

      if (data.kind === "pin_update") {
        state.data.pins = data.pins || [];
        const m = state.data.messages.find((x) => x.msg_id === data.message?.msg_id);
        if (m) m.pinned = data.action === "pinned";
        renderPins();
        renderMessages();
        return;
      }

      if (data.kind === "reaction_update") {
        const m = state.data.messages.find((x) => x.msg_id === data.msg_id);
        if (m) m.reactions = data.reactions || {};
        renderMessages();
        return;
      }

      if (data.kind === "feed_post") {
        if (!state.data.feed.some((p) => p.id === data.post.id)) {
          state.data.feed.unshift(data.post);
          renderFeed();
          renderTrending();
        }
        return;
      }

      if (data.kind === "feed_update") {
        const idx = state.data.feed.findIndex((p) => p.id === data.post.id);
        if (idx >= 0) state.data.feed[idx] = data.post;
        else state.data.feed.unshift(data.post);
        renderFeed();
        renderTrending();
        return;
      }

      if (data.kind === "typing") {
        el("channelDescription").textContent = currentRoomName() + " • " + (data.name || "Someone") + " is typing...";
        clearTimeout(state.typingTimer);
        state.typingTimer = setTimeout(renderTop, 900);
        return;
      }

      if (data.kind === "system") {
        state.data.messages.push({
          msg_id: data.msg_id,
          client_id: "system",
          name: "System",
          avatar: "",
          text: data.text,
          room: roomId(),
          created_at: data.created_at || Date.now(),
          reply_to: null,
          reply_preview: null,
          reactions: {},
          pinned: false,
        });
        renderMessages();
        return;
      }
    };

    state.socket.onerror = () => {
      if (seq !== state.seq) return;
      setConn("connection error");
    };

    state.socket.onclose = () => {
      if (seq !== state.seq) return;
      setConn(state.connectedOnce ? "disconnected" : "offline");
      state.reconnectTimer = setTimeout(() => connect(), 1200);
    };
  }

  function bindMessageActions() {
    messagesList.querySelectorAll("[data-act]").forEach((btn) => {
      btn.onclick = () => {
        const id = btn.dataset.id;
        const act = btn.dataset.act;

        if (act === "reply") {
          state.replyTo = id;
          const original = state.data.messages.find((m) => m.msg_id === id);
          el("messageInput").placeholder = original ? `Replying to ${original.name}...` : "Replying...";
          el("messageInput").focus();
          return;
        }

        if (act === "pin") return send("pin_chat", { msg_id: id });

        if (act === "react") {
          const emoji = prompt("Reaction", "👍");
          if (emoji) send("react_chat", { msg_id: id, emoji });
        }
      };
    });
  }

  function bindFeedActions() {
    feedList.querySelectorAll("[data-pact]").forEach((btn) => {
      btn.onclick = () => {
        const id = btn.dataset.id;
        const act = btn.dataset.pact;

        if (act === "like") return send("feed_like", { post_id: id });
        if (act === "repost") return send("feed_repost", { post_id: id });
        if (act === "comment") {
          const text = prompt("Write a comment");
          if (text) send("feed_comment", { post_id: id, text });
        }
      };
    });
  }

  document.querySelectorAll(".tab").forEach((btn) => {
    btn.onclick = () => {
      state.tab = btn.dataset.tab;
      renderTabs();
    };
  });

  el("profileBtn").onclick = () => {
    state.name = (el("nameInput").value || "You").trim().slice(0, 24) || "You";
    state.avatar = (el("avatarInput").value || "").trim();
    state.bio = (el("bioInput").value || "").trim().slice(0, 80);

    localStorage.setItem("linkup_name", state.name);
    localStorage.setItem("linkup_avatar", state.avatar);
    localStorage.setItem("linkup_bio", state.bio);

    send("profile", { name: state.name, avatar: state.avatar, bio: state.bio });
    renderProfileFields();
  };

  el("postBtn").onclick = () => {
    const text = el("postText").value.trim();
    const media_url = el("mediaUrl").value.trim();
    const media_type = el("mediaType").value;

    if (!text && !media_url) return;

    const post = {
      id: `local_post_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`,
      client_id: state.clientId,
      name: state.name,
      avatar: state.avatar,
      bio: state.bio,
      text,
      media_url,
      media_type,
      created_at: Date.now(),
      likes: 0,
      reposts: 0,
      comments: [],
    };

    optimisticAddPost(post);
    send("post", { text, media_url, media_type });

    el("postText").value = "";
    el("mediaUrl").value = "";
  };

  el("sendBtn").onclick = () => {
    const text = el("messageInput").value.trim();
    if (!text) return;

    const original = state.replyTo ? state.data.messages.find((m) => m.msg_id === state.replyTo) : null;
    const msg = {
      msg_id: `local_msg_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`,
      client_id: state.clientId,
      name: state.name,
      avatar: state.avatar,
      text,
      room: roomId(),
      created_at: Date.now(),
      reply_to: state.replyTo || null,
      reply_preview: original ? { msg_id: original.msg_id, name: original.name, text: original.text } : null,
      reactions: {},
      pinned: false,
    };

    optimisticAddChat(msg);
    send("chat", { text, reply_to: state.replyTo });

    state.replyTo = null;
    el("messageInput").placeholder = "Message LinkUp...";
    el("messageInput").value = "";
    el("messageInput").style.height = "96px";
  };

  el("clearReplyBtn").onclick = () => {
    state.replyTo = null;
    el("messageInput").placeholder = "Message LinkUp...";
  };

  el("messageInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      el("sendBtn").click();
    }
  });

  el("postText").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.ctrlKey) {
      e.preventDefault();
      el("postBtn").click();
    }
  });

  el("searchInput").addEventListener("input", () => {
    if (state.tab === "feed") renderFeed();
    else renderMessages();
  });

  el("messageInput").addEventListener("input", () => {
    send("typing", {});
  });

  window.addEventListener("beforeunload", () => {
    try {
      if (state.socket && state.socket.readyState === WebSocket.OPEN) {
        state.socket.close(1000, "bye");
      }
    } catch {}
  });

  renderAll();
  connect();
})();
</script>
</body>
</html>
"""



