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
# CONFIG
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

ROOM_META = [
    {"id": "general", "name": "general", "desc": "Town square for everything."},
    {"id": "dev", "name": "dev", "desc": "Builds, bugs, and code."},
    {"id": "clips", "name": "clips", "desc": "Short clips and highlights."},
    {"id": "study", "name": "study", "desc": "Focus, notes, learning."},
]

SEARCH_SUGGESTIONS = ["#python", "#games", "#art", "#music", "#ai", "#clips", "#buildinpublic"]

# ----------------------------
# HELPERS
# ----------------------------

def now_ms() -> int:
    return int(time.time() * 1000)


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def clamp_text(value: str, length: int) -> str:
    value = (value or "").strip()
    return value[:length] if value else ""


def escape_for_html(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


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
# FEED STATE
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
# ROOM STATE
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

    async def send_init(self, room: str, ws: WebSocket, client_id: str) -> None:
        await ws.send_json(
            {
                "kind": "init",
                "client_id": client_id,
                "room": room,
                "users": self.users(room),
                "history": self.history_view(room),
                "pins": self.pins_view(room),
                "feed": [serialize_post(p) for p in FEED][-60:],
                "feed_trending": trending_posts(),
                "rooms": ROOM_META,
                "suggestions": SEARCH_SUGGESTIONS,
                "time": now_ms(),
            }
        )

    async def send_presence(self, room: str) -> None:
        await self.broadcast_room(
            room,
            {
                "kind": "presence",
                "room": room,
                "users": self.users(room),
                "time": now_ms(),
            },
        )


ROOMS = RoomState()

# ----------------------------
# PROFILES
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
# WEBSOCKET
# ----------------------------

@ws_router.websocket("/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    client_id = "cli_" + uuid4().hex[:10]
    profile = get_profile(client_id)
    active_room = room_id

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

            if kind == "hello":
                client_id = str(data.get("client_id") or client_id)
                profile = get_profile(client_id)
                profile["name"] = clamp_text(str(data.get("name") or profile["name"]), 24) or "Guest"
                profile["avatar"] = str(data.get("avatar") or profile.get("avatar") or "")
                profile["bio"] = clamp_text(str(data.get("bio") or profile.get("bio") or ""), 80)
                active_room = str(data.get("room") or room_id)

                ROOMS.register(active_room, profile, websocket)
                await ROOMS.send_init(active_room, websocket, client_id)
                await ROOMS.send_presence(active_room)

                await ROOMS.broadcast_all(
                    {
                        "kind": "profile_update",
                        "profile": serialize_profile(profile),
                        "time": now_ms(),
                    }
                )
                continue

            if kind == "profile":
                old_name = profile["name"]
                profile = get_profile(client_id)
                profile["name"] = clamp_text(str(data.get("name") or profile["name"]), 24) or "Guest"
                profile["avatar"] = str(data.get("avatar") or profile.get("avatar") or "")
                profile["bio"] = clamp_text(str(data.get("bio") or profile.get("bio") or ""), 80)

                if active_room:
                    ROOMS.register(active_room, profile, websocket)
                    await ROOMS.send_presence(active_room)

                await ROOMS.broadcast_all(
                    {
                        "kind": "profile_update",
                        "profile": serialize_profile(profile),
                        "old_name": old_name,
                        "time": now_ms(),
                    }
                )
                continue

            if kind == "typing":
                await ROOMS.broadcast_room(
                    active_room,
                    {
                        "kind": "typing",
                        "client_id": client_id,
                        "name": profile["name"],
                        "time": now_ms(),
                    },
                )
                continue

            if kind == "chat":
                text = clamp_text(str(data.get("text") or ""), 5000)
                if not text:
                    continue

                reply_to = data.get("reply_to")
                reply_preview = None
                if reply_to:
                    prev = ROOMS.find_message(active_room, str(reply_to))
                    if prev:
                        reply_preview = {
                            "msg_id": prev["msg_id"],
                            "name": prev.get("name", "Guest"),
                            "text": prev.get("text", "")[:120],
                        }

                message = {
                    "msg_id": uid("msg"),
                    "client_id": client_id,
                    "name": profile["name"],
                    "avatar": profile["avatar"],
                    "text": text,
                    "room": active_room,
                    "created_at": now_ms(),
                    "reply_to": reply_to,
                    "reply_preview": reply_preview,
                    "reactions": {},
                    "pinned": False,
                }
                ROOMS.add_message(active_room, message)
                await ROOMS.broadcast_room(active_room, {"kind": "chat_message", "message": serialize_message(message)})
                continue

            if kind == "pin_chat":
                result = ROOMS.toggle_pin(active_room, str(data.get("msg_id") or ""))
                if result:
                    await ROOMS.broadcast_room(
                        active_room,
                        {
                            "kind": "pin_update",
                            **result,
                            "time": now_ms(),
                        },
                    )
                continue

            if kind == "react_chat":
                result = ROOMS.toggle_chat_reaction(
                    active_room,
                    str(data.get("msg_id") or ""),
                    str(data.get("emoji") or "👍")[:4],
                    client_id,
                )
                if result:
                    await ROOMS.broadcast_room(
                        active_room,
                        {
                            "kind": "reaction_update",
                            **result,
                            "time": now_ms(),
                        },
                    )
                continue

            if kind == "post":
                text = clamp_text(str(data.get("text") or ""), 5000)
                media_url = str(data.get("media_url") or "").strip()
                media_type = str(data.get("media_type") or "text").strip()

                if not text and not media_url:
                    continue

                post = {
                    "id": uid("post"),
                    "client_id": client_id,
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

                await ROOMS.broadcast_all(
                    {
                        "kind": "feed_post",
                        "post": serialize_post(post),
                        "time": now_ms(),
                    }
                )
                continue

            if kind == "feed_like":
                pid = str(data.get("post_id") or "")
                post = FEED_INDEX.get(pid)
                if not post:
                    continue
                likes = post["likes"]
                if client_id in likes:
                    likes.remove(client_id)
                else:
                    likes.add(client_id)

                await ROOMS.broadcast_all(
                    {
                        "kind": "feed_update",
                        "post": serialize_post(post),
                        "time": now_ms(),
                    }
                )
                continue

            if kind == "feed_repost":
                pid = str(data.get("post_id") or "")
                post = FEED_INDEX.get(pid)
                if not post:
                    continue
                reps = post["reposts"]
                if client_id in reps:
                    reps.remove(client_id)
                else:
                    reps.add(client_id)

                await ROOMS.broadcast_all(
                    {
                        "kind": "feed_update",
                        "post": serialize_post(post),
                        "time": now_ms(),
                    }
                )
                continue

            if kind == "feed_comment":
                pid = str(data.get("post_id") or "")
                post = FEED_INDEX.get(pid)
                if not post:
                    continue

                text = clamp_text(str(data.get("text") or ""), 1000)
                if not text:
                    continue

                comment = {
                    "id": uid("c"),
                    "client_id": client_id,
                    "name": profile["name"],
                    "avatar": profile["avatar"],
                    "text": text,
                    "created_at": now_ms(),
                }
                post["comments"].append(comment)

                await ROOMS.broadcast_all(
                    {
                        "kind": "feed_update",
                        "post": serialize_post(post),
                        "time": now_ms(),
                    }
                )
                continue

    except WebSocketDisconnect:
        ROOMS.remove(active_room, client_id)
        await ROOMS.send_presence(active_room)
    except Exception:
        ROOMS.remove(active_room, client_id)
        await ROOMS.send_presence(active_room)


app.include_router(ws_router)

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
  --bg:#313338; --panel:#2b2d31; --panel2:#1e1f22; --text:#f2f3f5; --muted:#b5bac1;
  --accent:#5865f2; --accent2:#3ba55d; --line:rgba(255,255,255,.06);
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
  grid-template-columns:72px 248px minmax(0,1fr) 280px;
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
  width:48px;height:48px;
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
  width:10px;height:10px;border-radius:999px;
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
  min-height:88px;
  max-height:160px;
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

  function profilePayload() {
    return {
      kind: "profile",
      client_id: state.clientId,
      name: state.name,
      avatar: state.avatar,
      bio: state.bio,
      room: roomId(),
    };
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
    el("statMessages")?.textContent = String(state.data.messages.length);
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
      setConn("connected");
      state.socket.send(JSON.stringify({
        kind: "hello",
        client_id: state.clientId,
        room: roomId(),
        name: state.name,
        avatar: state.avatar,
        bio: state.bio,
      }));
    };

    state.socket.onmessage = (e) => {
      if (seq !== state.seq) return;
      const data = JSON.parse(e.data);

      if (data.kind === "init") {
        state.data.users = data.users || [];
        state.data.messages = data.history || [];
        state.data.pins = data.pins || [];
        state.data.feed = data.feed || [];
        state.data.trending = data.feed_trending || [];
        renderAll();
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
        state.data.messages.push(data.message);
        renderMessages();
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
        state.data.feed.unshift(data.post);
        renderFeed();
        renderTrending();
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
        el("channelDescription").textContent = `${data.name || "Someone"} is typing...`;
        clearTimeout(state.typingTimer);
        state.typingTimer = setTimeout(renderTop, 900);
      }
    };

    state.socket.onerror = () => {
      if (seq !== state.seq) return;
      setConn("connection error");
    };

    state.socket.onclose = () => {
      if (seq !== state.seq) return;
      setConn("offline");
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

        if (act === "pin") {
          return send("pin_chat", { msg_id: id });
        }

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

    send("post", { text, media_url, media_type });
    el("postText").value = "";
    el("mediaUrl").value = "";
  };

  el("sendBtn").onclick = () => {
    const text = el("messageInput").value.trim();
    if (!text) return;

    send("chat", { text, reply_to: state.replyTo });
    state.replyTo = null;
    el("messageInput").placeholder = "Message LinkUp...";
    el("messageInput").value = "";
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

  renderAll();
  connect();
})();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(page_html())


@app.get("/health")
def health():
    return {"ok": True, "app": "LinkUp"}


@app.get("/api")
def api():
    return {"message": "LinkUp is live", "websocket": "/ws/{room_id}"}


@app.get("/{path:path}")
def spa(path: str):
    return HTMLResponse(page_html())



