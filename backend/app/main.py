from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
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

        self.db_path = Path(os.getenv("LINKUP_DB_PATH", "/var/data/linkup.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ws_router = APIRouter()
DB_LOCK = RLock()

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


def row_to_dict(row: sqlite3.Row | None) -> Optional[dict]:
    if row is None:
        return None
    return dict(row)


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
        "media_name": post.get("media_name", ""),
        "created_at": post.get("created_at", now_ms()),
        "likes": post.get("likes", 0),
        "reposts": post.get("reposts", 0),
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
        "pinned": bool(msg.get("pinned", 0)),
        "is_system": bool(msg.get("is_system", 0)),
    }


# ----------------------------
# SQLite
# ----------------------------

def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


@contextmanager
def db(write: bool = False):
    conn = connect_db()
    try:
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with DB_LOCK, db(write=True) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                client_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                avatar TEXT NOT NULL DEFAULT '',
                bio TEXT NOT NULL DEFAULT '',
                accent TEXT NOT NULL DEFAULT '#5865f2',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                msg_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL,
                avatar TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                reply_to TEXT,
                reply_name TEXT,
                reply_text TEXT,
                created_at INTEGER NOT NULL,
                is_system INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS message_reactions (
                msg_id TEXT NOT NULL,
                emoji TEXT NOT NULL,
                client_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (msg_id, emoji, client_id)
            );

            CREATE TABLE IF NOT EXISTS posts (
                post_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL,
                avatar TEXT NOT NULL DEFAULT '',
                bio TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                media_url TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT 'text',
                media_name TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS post_likes (
                post_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (post_id, client_id)
            );

            CREATE TABLE IF NOT EXISTS post_reposts (
                post_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (post_id, client_id)
            );

            CREATE TABLE IF NOT EXISTS post_comments (
                id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL,
                avatar TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_room_time ON messages(room_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(created_at);
            CREATE INDEX IF NOT EXISTS idx_comments_post_time ON post_comments(post_id, created_at);
            """
        )


init_db()


# ----------------------------
# Database helpers
# ----------------------------

def db_get_profile(client_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT client_id, name, avatar, bio, accent, created_at, updated_at FROM profiles WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        return row_to_dict(row)


def db_upsert_profile(client_id: str, name: str, avatar: str, bio: str, accent: str) -> dict:
    existing = db_get_profile(client_id)
    created_at = existing["created_at"] if existing else now_ms()
    name = clamp_text(name or (existing["name"] if existing else "Guest"), 24) or "Guest"
    avatar = (avatar if avatar is not None else (existing["avatar"] if existing else "")).strip()
    bio = clamp_text(bio if bio is not None else (existing["bio"] if existing else ""), 80)
    accent = clamp_text(accent if accent is not None else (existing["accent"] if existing else "#5865f2"), 20) or "#5865f2"
    updated_at = now_ms()

    with DB_LOCK, db(write=True) as conn:
        conn.execute(
            """
            INSERT INTO profiles (client_id, name, avatar, bio, accent, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                name=excluded.name,
                avatar=excluded.avatar,
                bio=excluded.bio,
                accent=excluded.accent,
                updated_at=excluded.updated_at
            """,
            (client_id, name, avatar, bio, accent, created_at, updated_at),
        )

    return {
        "client_id": client_id,
        "name": name,
        "avatar": avatar,
        "bio": bio,
        "accent": accent,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def db_profile_from_client(client_id: str, fallback_name: str = "Guest") -> dict:
    profile = db_get_profile(client_id)
    if profile:
        return profile
    return db_upsert_profile(client_id, fallback_name, "", "", "#5865f2")


def db_message_reactions(msg_id: str) -> dict:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT emoji, COUNT(*) AS c
            FROM message_reactions
            WHERE msg_id = ?
            GROUP BY emoji
            """,
            (msg_id,),
        ).fetchall()
    return {row["emoji"]: row["c"] for row in rows}


def db_load_room_messages(room_id: str, limit: int = 200) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT msg_id, room_id, client_id, name, avatar, text, reply_to, reply_name, reply_text,
                   created_at, is_system, pinned
            FROM messages
            WHERE room_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (room_id, limit),
        ).fetchall()

    messages: List[dict] = []
    for row in rows:
        msg = dict(row)
        msg["reactions"] = db_message_reactions(msg["msg_id"])
        messages.append(msg)
    return messages


def db_save_message(
    room_id: str,
    client_id: str,
    name: str,
    avatar: str,
    text: str,
    msg_id: Optional[str] = None,
    created_at: Optional[int] = None,
    reply_to: Optional[str] = None,
    reply_name: Optional[str] = None,
    reply_text: Optional[str] = None,
    is_system: int = 0,
) -> dict:
    msg_id = msg_id or uid("msg")
    created_at = created_at or now_ms()

    with DB_LOCK, db(write=True) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                msg_id, room_id, client_id, name, avatar, text,
                reply_to, reply_name, reply_text, created_at, is_system, pinned
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                msg_id,
                room_id,
                client_id,
                name,
                avatar,
                text,
                reply_to,
                reply_name,
                reply_text,
                created_at,
                is_system,
            ),
        )

    row = db_get_message(msg_id)
    return row or {
        "msg_id": msg_id,
        "room_id": room_id,
        "client_id": client_id,
        "name": name,
        "avatar": avatar,
        "text": text,
        "reply_to": reply_to,
        "reply_name": reply_name,
        "reply_text": reply_text,
        "created_at": created_at,
        "is_system": is_system,
        "pinned": 0,
        "reactions": {},
    }


def db_get_message(msg_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            """
            SELECT msg_id, room_id, client_id, name, avatar, text, reply_to, reply_name, reply_text,
                   created_at, is_system, pinned
            FROM messages
            WHERE msg_id = ?
            """,
            (msg_id,),
        ).fetchone()
    if not row:
        return None
    msg = dict(row)
    msg["reactions"] = db_message_reactions(msg_id)
    return msg


def db_get_message_preview(msg_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT msg_id, name, text FROM messages WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
    if not row:
        return None
    return {"msg_id": row["msg_id"], "name": row["name"], "text": row["text"]}


def db_toggle_message_pin(msg_id: str) -> Optional[dict]:
    with DB_LOCK, db(write=True) as conn:
        row = conn.execute("SELECT pinned FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
        if not row:
            return None
        new_value = 0 if int(row["pinned"]) else 1
        conn.execute("UPDATE messages SET pinned = ? WHERE msg_id = ?", (new_value, msg_id))
    msg = db_get_message(msg_id)
    if not msg:
        return None
    action = "unpinned" if msg["pinned"] else "pinned"
    return {"action": action, "message": serialize_message(msg)}


def db_toggle_message_reaction(msg_id: str, emoji: str, client_id: str) -> Optional[dict]:
    emoji = (emoji or "👍").strip()[:4] or "👍"
    with DB_LOCK, db(write=True) as conn:
        existing = conn.execute(
            "SELECT 1 FROM message_reactions WHERE msg_id = ? AND emoji = ? AND client_id = ?",
            (msg_id, emoji, client_id),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM message_reactions WHERE msg_id = ? AND emoji = ? AND client_id = ?",
                (msg_id, emoji, client_id),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO message_reactions (msg_id, emoji, client_id, created_at) VALUES (?, ?, ?, ?)",
                (msg_id, emoji, client_id, now_ms()),
            )

    msg = db_get_message(msg_id)
    if not msg:
        return None
    return {"msg_id": msg_id, "reactions": msg["reactions"]}


def db_feed_counts(post_id: str) -> tuple[int, int]:
    with db() as conn:
        likes = conn.execute("SELECT COUNT(*) AS c FROM post_likes WHERE post_id = ?", (post_id,)).fetchone()["c"]
        reposts = conn.execute("SELECT COUNT(*) AS c FROM post_reposts WHERE post_id = ?", (post_id,)).fetchone()["c"]
    return likes, reposts


def db_load_post_comments(post_id: str) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, post_id, client_id, name, avatar, text, created_at
            FROM post_comments
            WHERE post_id = ?
            ORDER BY created_at ASC
            """,
            (post_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def db_get_post(post_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            """
            SELECT post_id, client_id, name, avatar, bio, text, media_url, media_type, media_name, created_at
            FROM posts
            WHERE post_id = ?
            """,
            (post_id,),
        ).fetchone()
    if not row:
        return None
    post = dict(row)
    post["id"] = post.pop("post_id")
    likes, reposts = db_feed_counts(post["id"])
    post["likes"] = likes
    post["reposts"] = reposts
    post["comments"] = db_load_post_comments(post["id"])
    return post


def db_load_feed(limit: int = 80) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT post_id, client_id, name, avatar, bio, text, media_url, media_type, media_name, created_at
            FROM posts
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    posts: List[dict] = []
    for row in rows:
        post = dict(row)
        post["id"] = post.pop("post_id")
        likes, reposts = db_feed_counts(post["id"])
        post["likes"] = likes
        post["reposts"] = reposts
        post["comments"] = db_load_post_comments(post["id"])
        posts.append(post)
    return posts


def db_save_post(
    client_id: str,
    name: str,
    avatar: str,
    bio: str,
    text: str,
    media_url: str = "",
    media_type: str = "text",
    media_name: str = "",
    post_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> dict:
    post_id = post_id or uid("post")
    created_at = created_at or now_ms()

    with DB_LOCK, db(write=True) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO posts (
                post_id, client_id, name, avatar, bio, text,
                media_url, media_type, media_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                client_id,
                name,
                avatar,
                bio,
                text,
                media_url,
                media_type,
                media_name,
                created_at,
            ),
        )
    row = db_get_post(post_id)
    return row or {
        "id": post_id,
        "client_id": client_id,
        "name": name,
        "avatar": avatar,
        "bio": bio,
        "text": text,
        "media_url": media_url,
        "media_type": media_type,
        "media_name": media_name,
        "created_at": created_at,
        "likes": 0,
        "reposts": 0,
        "comments": [],
    }


def db_toggle_post_like(post_id: str, client_id: str) -> Optional[dict]:
    with DB_LOCK, db(write=True) as conn:
        existing = conn.execute(
            "SELECT 1 FROM post_likes WHERE post_id = ? AND client_id = ?",
            (post_id, client_id),
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM post_likes WHERE post_id = ? AND client_id = ?", (post_id, client_id))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO post_likes (post_id, client_id, created_at) VALUES (?, ?, ?)",
                (post_id, client_id, now_ms()),
            )
    return db_get_post(post_id)


def db_toggle_post_repost(post_id: str, client_id: str) -> Optional[dict]:
    with DB_LOCK, db(write=True) as conn:
        existing = conn.execute(
            "SELECT 1 FROM post_reposts WHERE post_id = ? AND client_id = ?",
            (post_id, client_id),
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM post_reposts WHERE post_id = ? AND client_id = ?", (post_id, client_id))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO post_reposts (post_id, client_id, created_at) VALUES (?, ?, ?)",
                (post_id, client_id, now_ms()),
            )
    return db_get_post(post_id)


def db_add_post_comment(
    post_id: str,
    client_id: str,
    name: str,
    avatar: str,
    text: str,
    comment_id: Optional[str] = None,
) -> Optional[dict]:
    comment_id = comment_id or uid("c")
    with DB_LOCK, db(write=True) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO post_comments (id, post_id, client_id, name, avatar, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (comment_id, post_id, client_id, name, avatar, text, now_ms()),
        )
    return db_get_post(post_id)


# ----------------------------
# WebSocket manager
# ----------------------------

class Rooms:
    def __init__(self) -> None:
        self.sockets: Dict[str, Dict[str, WebSocket]] = defaultdict(dict)
        self.members: Dict[str, Dict[str, dict]] = defaultdict(dict)

    def register(self, room: str, profile: dict, ws: WebSocket) -> None:
        self.members[room][profile["client_id"]] = serialize_profile(profile)
        self.sockets[room][profile["client_id"]] = ws

    def unregister(self, room: str, client_id: str) -> None:
        self.sockets[room].pop(client_id, None)
        self.members[room].pop(client_id, None)

    def update_client_profile(self, client_id: str, profile: dict) -> None:
        snap = serialize_profile(profile)
        for room in list(self.members.keys()):
            if client_id in self.members[room]:
                self.members[room][client_id] = snap

    def current_users(self, room: str) -> List[dict]:
        users = list(self.members[room].values())
        users.sort(key=lambda u: (u.get("name", "").lower(), u.get("joined_at", 0)))
        return users

    async def broadcast_room(self, room: str, payload: dict) -> None:
        dead = []
        for client_id, ws in self.sockets[room].items():
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(client_id)
        for client_id in dead:
            self.unregister(room, client_id)

    async def broadcast_all(self, payload: dict) -> None:
        for room in list(self.sockets.keys()):
            await self.broadcast_room(room, payload)


ROOMS = Rooms()


async def push_presence(room: str) -> None:
    await ROOMS.broadcast_room(
        room,
        {
            "kind": "presence",
            "room": room,
            "users": ROOMS.current_users(room),
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


async def push_init(room: str, ws: WebSocket, client_id: str) -> None:
    await ws.send_json(
        {
            "kind": "init",
            "client_id": client_id,
            "room": room,
            "users": ROOMS.current_users(room),
            "history": [serialize_message(m) for m in db_load_room_messages(room, 200)],
            "pins": [serialize_message(m) for m in db_load_room_messages(room, 200) if m.get("pinned")],
            "feed": [serialize_post(p) for p in db_load_feed(80)],
            "feed_trending": [serialize_post(p) for p in db_load_feed(80)[:8]],
            "rooms": ROOMS_META,
            "suggestions": SEARCH_HINTS,
            "time": now_ms(),
        }
    )


async def push_chat_message(room: str, msg: dict) -> None:
    await ROOMS.broadcast_room(room, {"kind": "chat_message", "message": serialize_message(msg)})


async def push_pin_update(room: str, result: dict) -> None:
    await ROOMS.broadcast_room(room, {"kind": "pin_update", **result, "time": now_ms()})


async def push_reaction_update(room: str, result: dict) -> None:
    await ROOMS.broadcast_room(room, {"kind": "reaction_update", **result, "time": now_ms()})


async def push_feed_post() -> None:
    await ROOMS.broadcast_all(
        {
            "kind": "feed_snapshot",
            "feed": [serialize_post(p) for p in db_load_feed(80)],
            "feed_trending": [serialize_post(p) for p in db_load_feed(80)[:8]],
            "time": now_ms(),
        }
    )


async def push_feed_update(post_id: str) -> None:
    post = db_get_post(post_id)
    if not post:
        return
    await ROOMS.broadcast_all({"kind": "feed_update", "post": serialize_post(post), "time": now_ms()})


async def push_pong(ws: WebSocket) -> None:
    await ws.send_json({"kind": "pong", "time": now_ms()})


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
            "is_system": 1,
        },
    )


# ----------------------------
# WebSocket / actions
# ----------------------------

@ws_router.websocket("/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    client_id = "cli_" + uuid4().hex[:10]
    profile = db_profile_from_client(client_id)
    active_room = room_id
    ping_task_alive = True

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
                profile = db_upsert_profile(
                    client_id,
                    str(data.get("name") or profile["name"]),
                    str(data.get("avatar") or profile.get("avatar") or ""),
                    str(data.get("bio") or profile.get("bio") or ""),
                    str(data.get("accent") or profile.get("accent") or "#5865f2"),
                )
                active_room = str(data.get("room") or room_id)

                ROOMS.register(active_room, profile, websocket)
                await push_init(active_room, websocket, client_id)
                await push_presence(active_room)
                continue

            if kind == "profile":
                old_name = profile["name"]
                profile = db_upsert_profile(
                    client_id,
                    str(data.get("name") or profile["name"]),
                    str(data.get("avatar") or profile.get("avatar") or ""),
                    str(data.get("bio") or profile.get("bio") or ""),
                    str(data.get("accent") or profile.get("accent") or "#5865f2"),
                )
                ROOMS.update_client_profile(client_id, profile)
                await push_profile(profile)
                await push_presence(active_room)
                if old_name != profile["name"]:
                    await push_system(active_room, f"{old_name} is now {profile['name']}")
                continue

            if kind == "typing":
                await push_typing(active_room, profile["name"], client_id)
                continue

            if kind == "chat":
                text = clamp_text(str(data.get("text") or ""), 5000)
                if not text:
                    continue

                reply_to = data.get("reply_to")
                reply_preview = None
                if reply_to:
                    prev = db_get_message_preview(str(reply_to))
                    if prev:
                        reply_preview = prev

                msg = db_save_message(
                    room_id=active_room,
                    client_id=client_id,
                    name=profile["name"],
                    avatar=profile["avatar"],
                    text=text,
                    msg_id=str(data.get("msg_id") or uid("msg")),
                    created_at=int(data.get("created_at") or now_ms()),
                    reply_to=str(reply_to) if reply_to else None,
                    reply_name=(reply_preview["name"] if reply_preview else None),
                    reply_text=(reply_preview["text"] if reply_preview else None),
                    is_system=0,
                )
                if reply_preview:
                    msg["reply_preview"] = reply_preview
                await push_chat_message(active_room, msg)
                continue

            if kind == "pin_chat":
                result = db_toggle_message_pin(str(data.get("msg_id") or ""))
                if result:
                    await push_pin_update(active_room, result)
                continue

            if kind == "react_chat":
                result = db_toggle_message_reaction(
                    str(data.get("msg_id") or ""),
                    str(data.get("emoji") or "👍")[:4],
                    client_id,
                )
                if result:
                    await push_reaction_update(active_room, result)
                continue

            if kind == "post":
                text = clamp_text(str(data.get("text") or ""), 5000)
                media_url = str(data.get("media_url") or "").strip()
                media_type = str(data.get("media_type") or "text").strip()
                media_name = str(data.get("media_name") or "").strip()

                if not text and not media_url:
                    continue

                post = db_save_post(
                    client_id=client_id,
                    name=profile["name"],
                    avatar=profile["avatar"],
                    bio=profile["bio"],
                    text=text,
                    media_url=media_url,
                    media_type=media_type,
                    media_name=media_name,
                    post_id=str(data.get("id") or uid("post")),
                    created_at=int(data.get("created_at") or now_ms()),
                )
                await push_feed_post()
                continue

            if kind == "feed_like":
                post = db_toggle_post_like(str(data.get("post_id") or ""), client_id)
                if post:
                    await push_feed_update(post["id"])
                continue

            if kind == "feed_repost":
                post = db_toggle_post_repost(str(data.get("post_id") or ""), client_id)
                if post:
                    await push_feed_update(post["id"])
                continue

            if kind == "feed_comment":
                pid = str(data.get("post_id") or "")
                comment_text = clamp_text(str(data.get("text") or ""), 1000)
                if not pid or not comment_text:
                    continue
                post = db_add_post_comment(
                    pid,
                    client_id,
                    profile["name"],
                    profile["avatar"],
                    comment_text,
                    str(data.get("comment_id") or uid("c")),
                )
                if post:
                    await push_feed_update(post["id"])
                continue

    except WebSocketDisconnect:
        ROOMS.unregister(active_room, client_id)
        await push_presence(active_room)
    except Exception:
        ROOMS.unregister(active_room, client_id)
        await push_presence(active_room)


app.include_router(ws_router, prefix="/ws")


# ----------------------------
# HTTP routes
# ----------------------------

@app.get("/health")
def health():
    return {"ok": True, "app": "LinkUp", "db": str(settings.db_path)}


@app.get("/api")
def api():
    return {"message": "LinkUp is live", "websocket": "/ws/{room_id}"}


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(page_html())


@app.get("/{path:path}", response_class=HTMLResponse)
def spa(path: str):
    return HTMLResponse(page_html())


# ----------------------------
# HTML / CSS / JS
# ----------------------------

def page_html() -> str:
    return r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LinkUp</title>
<style>
:root{
  --bg:#313338;
  --bg-soft:#37393f;
  --panel:#2b2d31;
  --panel2:#1e1f22;
  --text:#f2f3f5;
  --muted:#b5bac1;
  --accent:#5865f2;
  --accent-2:#3ba55d;
  --danger:#ed4245;
  --line:rgba(255,255,255,.06);
  --shadow:0 10px 28px rgba(0,0,0,.25);
}
html[data-theme="light"]{
  --bg:#f4f6fb;
  --bg-soft:#ffffff;
  --panel:#ffffff;
  --panel2:#edf1f7;
  --panel3:#f6f8fb;
  --text:#111827;
  --muted:#5b6475;
  --accent:#5865f2;
  --accent-2:#1b9b58;
  --danger:#dc2626;
  --line:rgba(17,24,39,.10);
  --shadow:0 10px 28px rgba(17,24,39,.10);
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
  grid-template-columns:72px 248px minmax(0,1fr) 320px;
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
  background:#40444b;
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
  position:relative;
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
  background:rgba(255,255,255,.05);
  color:var(--muted);
  font-size:12px;
}
.scroll{
  min-height:0;
  overflow-y:auto;
  flex:1;
}
.section{padding:10px 8px 8px}
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
  color:var(--text);
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
  height:56px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  padding:0 16px;
  border-bottom:1px solid var(--line);
  background:var(--bg-soft);
  gap:12px;
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
  max-width:38vw;
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
  background:var(--accent-2);
  box-shadow:0 0 0 4px rgba(59,165,93,.10);
}
.toolbar{
  display:flex;
  gap:10px;
  align-items:center;
  min-width:0;
}
.tabs{display:flex;gap:8px;flex:0 0 auto}
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
  width:min(260px, 30vw);
  padding:9px 12px;
  border:none;
  border-radius:8px;
  background:var(--panel2);
  color:var(--text);
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
  border-radius:12px;
  background:var(--panel);
  border:1px solid var(--line);
  margin-bottom:12px;
  box-shadow:var(--shadow);
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
  background:var(--panel2);
  color:var(--text);
  border-radius:10px;
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
  border-radius:10px;
  padding:10px 14px;
  cursor:pointer;
  font-weight:700;
}
.secondary{
  border:none;
  background:#40444b;
  color:#fff;
  border-radius:10px;
  padding:10px 14px;
  cursor:pointer;
}
.post,.msgbox{
  padding:14px;
  border-radius:12px;
  background:var(--panel);
  border:1px solid var(--line);
  margin-bottom:10px;
  box-shadow:var(--shadow);
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
  border-radius:12px;
  overflow:hidden;
  background:#111;
}
.media img,.media video{
  display:block;
  width:100%;
  max-height:440px;
  object-fit:cover;
}
.media.compact{
  padding:12px;
  background:rgba(255,255,255,.04);
  border:1px solid var(--line);
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
  border-radius:10px;
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
.settingrow{
  display:flex;
  gap:10px;
  flex-wrap:wrap;
  margin-top:8px;
}
.toggle{
  display:flex;
  align-items:center;
  gap:8px;
  font-size:12px;
  color:var(--muted);
}
.previewframe{
  margin-top:10px;
  border-radius:12px;
  overflow:hidden;
  background:var(--panel2);
  border:1px solid var(--line);
}
.previewframe img,.previewframe video{
  width:100%;
  display:block;
  max-height:220px;
  object-fit:cover;
}
.previewlink{
  display:block;
  padding:12px;
  color:inherit;
  text-decoration:none;
  font-size:13px;
}
.lowdata .media video{
  display:none;
}
.lowdata .media img{
  max-height:240px;
}
.lowdata .post,
.lowdata .msgbox{
  box-shadow:none;
}
.lowdata .pill2,
.lowdata .badge{
  opacity:.95;
}
.mobilebar{
  display:none;
}
.overlay{
  display:none;
}
.sheetclose{
  display:none;
}
@media (max-width:1100px){
  .app{grid-template-columns:72px 240px minmax(0,1fr)}
  .rightbar{display:none}
  .search{width:180px;max-width:35vw}
}
@media (max-width:820px){
  body{
    overflow:hidden;
  }

  .app{
    height:100vh;
    min-height:100vh;
    grid-template-columns:1fr;
    grid-template-rows: 56px 1fr 64px;
  }

  .sidebar,
  .rightbar{
    display:flex;
    position:fixed;
    inset:0 0 auto 0;
    height:100vh;
    width:100vw;
    z-index:40;
    transform:translateY(100%);
    transition:transform .22s ease;
    border:none;
  }

  .sidebar.open,
  .rightbar.open{
    transform:translateY(0);
  }

  .overlay{
    display:block;
    position:fixed;
    inset:0;
    background:rgba(0,0,0,.55);
    opacity:0;
    pointer-events:none;
    transition:opacity .2s ease;
    z-index:35;
  }

  .overlay.open{
    opacity:1;
    pointer-events:auto;
  }

  .sheetclose{
    display:grid;
    place-items:center;
    position:absolute;
    top:12px;
    right:12px;
    width:40px;
    height:40px;
    border:none;
    border-radius:12px;
    background:var(--panel2);
    color:var(--text);
    cursor:pointer;
    z-index:50;
  }

  .main{
    min-height:0;
    overflow:hidden;
  }

  .topbar{
    height:auto;
    min-height:56px;
    padding:10px 12px;
    gap:10px;
    flex-wrap:wrap;
  }

  .titleblock{
    width:100%;
    gap:8px;
    justify-content:space-between;
  }

  .titleblock p{
    max-width:100%;
  }

  .toolbar{
    width:100%;
    justify-content:space-between;
    flex-wrap:wrap;
  }

  .tabs{
    display:flex;
    order:1;
  }

  .search{
    width:100%;
    max-width:none;
    order:3;
  }

  .conn{
    order:2;
  }

  .content{
    padding:10px;
  }

  .box{
    border-radius:16px;
  }

  .textarea{
    min-height:88px;
    max-height:140px;
  }

  #feedList,
  #messages{
    padding-bottom:88px;
  }

  .mobilebar{
    display:flex;
    position:fixed;
    left:0;
    right:0;
    bottom:0;
    height:64px;
    background:var(--panel);
    border-top:1px solid var(--line);
    z-index:50;
    padding:8px;
    gap:8px;
  }

  .mobbtn{
    flex:1;
    border:none;
    border-radius:14px;
    background:var(--panel2);
    color:var(--text);
    font-weight:700;
    cursor:pointer;
  }

  .mobbtn.active{
    background:var(--accent);
    color:#fff;
  }

  .channels-btn,
  .settings-btn{
    width:40px;
    flex:0 0 auto;
  }

  .tabs{
    display:none;
  }

  .sidebar .scroll,
  .rightbar .scroll{
    overflow-y:auto;
    height:calc(100vh - 72px);
  }
}
</style>
</head>
<body>
<div class="overlay" id="overlay"></div>

<div class="app">
  <aside class="rail serverrail" id="servers"></aside>

  <aside class="sidebar" id="sidebar">
    <button class="sheetclose" id="closeSidebarBtn">✕</button>
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

        <div class="mobiletop">
          <button class="iconbtn" id="openChannelsBtn" title="Channels">☰</button>
          <button class="iconbtn" id="openSettingsBtn" title="Settings">⚙</button>
        </div>
      </div>
    </div>

    <div class="content">
      <div id="feedPane" class="pane active">
        <div class="box">
          <div class="label">Create a post</div>
          <textarea id="postText" class="textarea" placeholder="What's happening?"></textarea>

          <div class="settingrow">
            <button class="secondary" id="pickPostMediaBtn">Attach photo/video</button>
            <button class="secondary" id="clearPostMediaBtn">Clear attachment</button>
          </div>

          <div id="postMediaPreview" class="previewframe" style="display:none"></div>

          <div class="row" style="margin-top:10px">
            <input id="mediaUrl" class="input" placeholder="Optional media URL">
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

  <aside class="rightbar" id="rightbar">
    <button class="sheetclose" id="closeRightBtn">✕</button>
    <div class="head">
      <div class="brand">
        <div>
          <h1>Settings</h1>
          <small>edit anytime</small>
        </div>
      </div>
    </div>

    <div class="scroll">
      <div class="section" id="profileSection">
        <div class="label">Your profile</div>
        <div class="box">
          <div class="row" style="align-items:center; gap:12px">
            <div id="avatarPreview" class="avatar" style="width:56px;height:56px;border-radius:18px;font-size:18px">U</div>
            <div style="min-width:0">
              <b id="profilePreviewName">You</b><br>
              <span id="profilePreviewBio" style="color:var(--muted);font-size:12px">Set your profile below</span>
            </div>
          </div>

          <div class="settingrow">
            <button class="secondary" id="pickAvatarBtn">Upload avatar</button>
            <button class="secondary" id="clearAvatarBtn">Clear avatar</button>
          </div>

          <input id="nameInput" class="input" placeholder="Username" style="margin-top:10px">
          <input id="avatarInput" class="input" placeholder="Avatar URL" style="margin-top:8px">
          <textarea id="bioInput" class="textarea" placeholder="Bio" style="min-height:70px;margin-top:8px"></textarea>

          <div class="settingrow">
            <select id="themeInput" class="select">
              <option value="dark">Dark</option>
              <option value="light">Light</option>
            </select>
            <input id="accentInput" class="input" type="color" style="max-width:90px;padding:6px 8px;">
          </div>

          <label class="toggle" style="margin-top:10px;">
            <input id="lowDataInput" type="checkbox">
            Low data mode
          </label>

          <div class="settingrow">
            <button class="primary" id="profileBtn" style="margin-top:0;width:100%">Save profile</button>
          </div>
        </div>
      </div>

      <div class="section" id="peopleSection">
        <div class="label">Online now</div>
        <div id="members" class="list"></div>
      </div>

      <div class="section" id="pinsSection">
        <div class="label">Pinned messages</div>
        <div id="pins" class="pills"></div>
      </div>

      <div class="section" id="trendingSection">
        <div class="label">Trending posts</div>
        <div id="trending" class="list"></div>
      </div>

      <div class="smallnote">
        Username, avatar, bio, accent, theme, low-data mode, and file uploads can all be changed anytime.
      </div>
    </div>
  </aside>
</div>

<div class="mobilebar">
  <button class="mobbtn active" id="mobFeedBtn">Feed</button>
  <button class="mobbtn" id="mobChatBtn">Chat</button>
  <button class="mobbtn channels-btn" id="mobChannelsBtn">☰</button>
  <button class="mobbtn" id="mobPeopleBtn">People</button>
  <button class="mobbtn settings-btn" id="mobSettingsBtn">⚙</button>
</div>

<input id="avatarFileInput" type="file" accept="image/*" hidden>
<input id="postFileInput" type="file" accept="image/*,video/*" hidden>

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
    accent: localStorage.getItem("linkup_accent") || "#5865f2",
    theme: localStorage.getItem("linkup_theme") || "dark",
    manualLowData: localStorage.getItem("linkup_lowdata") === "1",
    socket: null,
    seq: 0,
    typingTimer: null,
    reconnectTimer: null,
    replyTo: null,
    connectedOnce: false,
    network: {
      connection: null,
      effectiveType: "unknown",
      saveData: false,
      downlink: null,
      rtt: null,
      type: "unknown",
    },
    draftPost: {
      dataUrl: "",
      type: "text",
      name: "",
    },
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
  const sidebar = el("sidebar");
  const rightbar = el("rightbar");
  const overlay = el("overlay");

  function applyTheme() {
    document.documentElement.setAttribute("data-theme", state.theme === "light" ? "light" : "dark");
    document.documentElement.style.setProperty("--accent", state.accent || "#5865f2");
    document.body.classList.toggle("lowdata", shouldUseLowData());
  }

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

  function detectNetworkMode() {
    const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection || null;
    state.network.connection = conn;

    if (!conn) {
      state.network.effectiveType = "unknown";
      state.network.saveData = false;
      state.network.downlink = null;
      state.network.rtt = null;
      state.network.type = "unknown";
      return;
    }

    state.network.effectiveType = conn.effectiveType || "unknown";
    state.network.saveData = Boolean(conn.saveData);
    state.network.downlink = typeof conn.downlink === "number" ? conn.downlink : null;
    state.network.rtt = typeof conn.rtt === "number" ? conn.rtt : null;
    state.network.type = conn.type || "unknown";
  }

  function shouldUseLowData() {
    const conn = state.network;
    const slow = new Set(["slow-2g", "2g", "3g"]);
    return state.manualLowData || conn.saveData || slow.has(conn.effectiveType) || conn.type === "cellular";
  }

  function lowDataBadgeText() {
    const pieces = [];
    if (state.network.effectiveType && state.network.effectiveType !== "unknown") pieces.push(state.network.effectiveType);
    if (state.network.saveData) pieces.push("save-data");
    if (state.manualLowData) pieces.push("manual");
    if (!pieces.length) pieces.push("normal");
    return `Data: ${pieces.join(" • ")}`;
  }

  function applyNetworkMode() {
    detectNetworkMode();
    let badge = document.getElementById("networkBadge");

    if (!badge) {
      badge = document.createElement("div");
      badge.id = "networkBadge";
      badge.style.position = "fixed";
      badge.style.right = "12px";
      badge.style.bottom = "76px";
      badge.style.zIndex = "60";
      badge.style.padding = "8px 10px";
      badge.style.borderRadius = "999px";
      badge.style.fontSize = "12px";
      badge.style.background = "rgba(0,0,0,.55)";
      badge.style.color = "#fff";
      badge.style.backdropFilter = "blur(8px)";
      document.body.appendChild(badge);
    }

    badge.textContent = lowDataBadgeText();
    badge.style.display = "block";
    document.body.classList.toggle("lowdata", shouldUseLowData());
  }

  function bindNetworkSignals() {
    applyNetworkMode();

    const conn = state.network.connection;
    if (conn && typeof conn.addEventListener === "function") {
      conn.addEventListener("change", () => {
        const before = shouldUseLowData();
        applyNetworkMode();
        if (before !== shouldUseLowData()) {
          renderFeed();
          renderMessages();
          renderTrending();
        }
      });
    }

    window.addEventListener("online", () => {
      applyNetworkMode();
      if (state.socket && state.socket.readyState !== WebSocket.OPEN) connect();
    });

    window.addEventListener("offline", () => {
      setConn("offline");
      applyNetworkMode();
    });
  }

  function fileToDataURL(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("Failed to read file"));
      reader.readAsDataURL(file);
    });
  }

  function fileKind(file) {
    if (!file) return "text";
    if (file.type.startsWith("image/")) return "image";
    if (file.type.startsWith("video/")) return "video";
    return "text";
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
        closeDrawers();
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
        closeDrawers();
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

  function mediaBlock(url, type, name) {
    if (!url) return "";

    if (shouldUseLowData()) {
      return `
        <div class="media compact">
          <a class="previewlink" href="${esc(url)}" target="_blank" rel="noreferrer noopener">
            ${name ? esc(name) : "Open media"}
          </a>
        </div>
      `;
    }

    if (type === "video" || /\.(mp4|webm|mov|m4v)(\?.*)?$/i.test(url)) {
      return `<div class="media"><video controls preload="metadata" src="${esc(url)}"></video></div>`;
    }

    return `<div class="media"><img loading="lazy" decoding="async" src="${esc(url)}" alt=""></div>`;
  }

  function avatarNode(name, avatar) {
    if (avatar) return `<img class="avatarimg" src="${esc(avatar)}" alt="">`;
    return `<div class="avatar">${esc((name || "?").slice(0,1).toUpperCase())}</div>`;
  }

  function renderFeed() {
    feedList.innerHTML = feedFiltered().map((p) => {
      const comments = (p.comments || []).slice(-3).map((c) => `
        <div style="margin-top:8px;padding:8px 10px;border-radius:8px;background:var(--panel2)">
          <b style="font-size:13px">${esc(c.name)}</b>
          <div style="color:var(--muted);font-size:12px;margin-top:2px">${esc(c.text)}</div>
        </div>
      `).join("");

      const media = mediaBlock(p.media_url, p.media_type, p.media_name);

      return `
        <div class="post" data-post="${esc(p.id)}">
          <div class="posthead">
            <div class="person">
              ${avatarNode(p.name, p.avatar)}
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
              ${avatarNode(m.name, m.avatar)}
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
    if (state.tab === "chat") {
      messagesList.scrollTop = messagesList.scrollHeight;
    }
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
    if (shouldUseLowData()) {
      trendingList.innerHTML = `<div class="smallnote">Low data mode is on. Trending is simplified.</div>`;
      return;
    }

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

  function renderSettingsFields() {
    el("nameInput").value = state.name;
    el("avatarInput").value = state.avatar;
    el("bioInput").value = state.bio;
    el("themeInput").value = state.theme;
    el("accentInput").value = state.accent;
    el("lowDataInput").checked = state.manualLowData;
    el("profilePreviewName").textContent = state.name || "You";
    el("profilePreviewBio").textContent = state.bio || "Set your profile below";

    const preview = el("avatarPreview");
    preview.innerHTML = state.avatar
      ? `<img src="${esc(state.avatar)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:inherit">`
      : `${esc((state.name || "You").slice(0,1).toUpperCase())}`;
  }

  function renderDraftPreview() {
    const preview = el("postMediaPreview");
    if (!state.draftPost.dataUrl) {
      preview.style.display = "none";
      preview.innerHTML = "";
      return;
    }

    preview.style.display = "block";
    if (state.draftPost.type === "video") {
      preview.innerHTML = `<video controls src="${state.draftPost.dataUrl}"></video>`;
    } else {
      preview.innerHTML = `<img src="${state.draftPost.dataUrl}" alt="">`;
    }
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
    el("mobFeedBtn").classList.toggle("active", state.tab === "feed");
    el("mobChatBtn").classList.toggle("active", state.tab === "chat");
  }

  function renderAll() {
    applyTheme();
    renderServers();
    renderChannels();
    renderTop();
    renderTabs();
    renderSettingsFields();
    renderDraftPreview();
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

  function openDrawer(which) {
    if (which === "channels") {
      sidebar.classList.add("open");
      rightbar.classList.remove("open");
      overlay.classList.add("open");
    } else if (which === "settings") {
      rightbar.classList.add("open");
      sidebar.classList.remove("open");
      overlay.classList.add("open");
    } else if (which === "people") {
      rightbar.classList.add("open");
      sidebar.classList.remove("open");
      overlay.classList.add("open");
      setTimeout(() => {
        const sec = document.getElementById("peopleSection");
        if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 40);
    } else {
      closeDrawers();
    }
  }

  function closeDrawers() {
    sidebar.classList.remove("open");
    rightbar.classList.remove("open");
    overlay.classList.remove("open");
  }

  function connect() {
    const seq = ++state.seq;

    if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) {
      try { state.socket.close(1000, "switch"); } catch {}
    }

    clearTimeout(state.reconnectTimer);
    setConn("connecting...");

    state.socket = new WebSocket(wsUrl());

    let pingTimer = null;

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
        accent: state.accent,
      }));

      state.socket.send(JSON.stringify({
        kind: "ping",
        client_id: state.clientId,
        room: roomId(),
        time: Date.now(),
      }));

      pingTimer = setInterval(() => {
        if (state.socket && state.socket.readyState === WebSocket.OPEN) {
          state.socket.send(JSON.stringify({
            kind: "ping",
            client_id: state.clientId,
            room: roomId(),
            time: Date.now(),
          }));
        }
      }, 20000);
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

        if (p.client_id === state.clientId) {
          state.name = p.name || state.name;
          state.avatar = p.avatar || state.avatar;
          state.bio = p.bio || state.bio;
          state.accent = p.accent || state.accent;
          localStorage.setItem("linkup_name", state.name);
          localStorage.setItem("linkup_avatar", state.avatar);
          localStorage.setItem("linkup_bio", state.bio);
          localStorage.setItem("linkup_accent", state.accent);
          renderSettingsFields();
          applyTheme();
        }

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
        state.data.pins = (state.data.messages || []).filter((m) => m.pinned);
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

      if (data.kind === "feed_snapshot") {
        state.data.feed = data.feed || state.data.feed;
        state.data.trending = data.feed_trending || state.data.trending;
        renderFeed();
        renderTrending();
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
          is_system: true,
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
      if (pingTimer) clearInterval(pingTimer);
      setConn(state.connectedOnce ? "disconnected" : "offline");
      state.reconnectTimer = setTimeout(() => connect(), 1200);
    };
  }

  function optimisticPatchPost(postId, patchFn) {
    const idx = state.data.feed.findIndex((p) => p.id === postId);
    if (idx >= 0) {
      state.data.feed[idx] = patchFn(state.data.feed[idx]);
      renderFeed();
      renderTrending();
    }
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

        if (act === "like") {
          optimisticPatchPost(id, (post) => ({ ...post, likes: Math.max(0, (post.likes || 0) + (post._liked ? -1 : 1)), _liked: !post._liked }));
          return send("feed_like", { post_id: id });
        }

        if (act === "repost") {
          optimisticPatchPost(id, (post) => ({ ...post, reposts: Math.max(0, (post.reposts || 0) + (post._reposted ? -1 : 1)), _reposted: !post._reposted }));
          return send("feed_repost", { post_id: id });
        }

        if (act === "comment") {
          const text = prompt("Write a comment");
          if (!text) return;

          const comment = {
            id: `local_c_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`,
            client_id: state.clientId,
            name: state.name,
            avatar: state.avatar,
            text,
            created_at: Date.now(),
          };

          optimisticPatchPost(id, (post) => ({
            ...post,
            comments: [...(post.comments || []), comment],
          }));

          send("feed_comment", { post_id: id, text, comment_id: comment.id });
        }
      };
    });
  }

  function renderProfileFields() {
    el("nameInput").value = state.name;
    el("avatarInput").value = state.avatar;
    el("bioInput").value = state.bio;
    el("themeInput").value = state.theme;
    el("accentInput").value = state.accent;
    el("lowDataInput").checked = state.manualLowData;
    el("profilePreviewName").textContent = state.name || "You";
    el("profilePreviewBio").textContent = state.bio || "Set your profile below";

    const preview = el("avatarPreview");
    preview.innerHTML = state.avatar
      ? `<img src="${esc(state.avatar)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:inherit">`
      : `${esc((state.name || "You").slice(0,1).toUpperCase())}`;
  }

  function renderDraftPreview() {
    const preview = el("postMediaPreview");
    if (!state.draftPost.dataUrl) {
      preview.style.display = "none";
      preview.innerHTML = "";
      return;
    }

    preview.style.display = "block";
    if (state.draftPost.type === "video") {
      preview.innerHTML = `<video controls src="${state.draftPost.dataUrl}"></video>`;
    } else {
      preview.innerHTML = `<img src="${state.draftPost.dataUrl}" alt="">`;
    }
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
    el("mobFeedBtn").classList.toggle("active", state.tab === "feed");
    el("mobChatBtn").classList.toggle("active", state.tab === "chat");
  }

  function renderAll() {
    applyTheme();
    renderServers();
    renderChannels();
    renderTop();
    renderTabs();
    renderSettingsFields();
    renderDraftPreview();
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

  function openDrawer(which) {
    if (which === "channels") {
      sidebar.classList.add("open");
      rightbar.classList.remove("open");
      overlay.classList.add("open");
    } else if (which === "settings") {
      rightbar.classList.add("open");
      sidebar.classList.remove("open");
      overlay.classList.add("open");
    } else if (which === "people") {
      rightbar.classList.add("open");
      sidebar.classList.remove("open");
      overlay.classList.add("open");
      setTimeout(() => {
        const sec = document.getElementById("peopleSection");
        if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 40);
    } else {
      closeDrawers();
    }
  }

  function closeDrawers() {
    sidebar.classList.remove("open");
    rightbar.classList.remove("open");
    overlay.classList.remove("open");
  }

  function bindMobile() {
    el("mobFeedBtn").onclick = () => {
      state.tab = "feed";
      renderTabs();
      closeDrawers();
    };

    el("mobChatBtn").onclick = () => {
      state.tab = "chat";
      renderTabs();
      closeDrawers();
    };

    el("mobChannelsBtn").onclick = () => openDrawer("channels");
    el("mobSettingsBtn").onclick = () => openDrawer("settings");
    el("mobPeopleBtn").onclick = () => openDrawer("people");
    el("openChannelsBtn").onclick = () => openDrawer("channels");
    el("openSettingsBtn").onclick = () => openDrawer("settings");
    el("closeSidebarBtn").onclick = closeDrawers;
    el("closeRightBtn").onclick = closeDrawers;
    overlay.onclick = closeDrawers;

    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawers();
    });
  }

  el("profileBtn").onclick = () => {
    state.name = (el("nameInput").value || "You").trim().slice(0, 24) || "You";
    state.avatar = (el("avatarInput").value || "").trim();
    state.bio = (el("bioInput").value || "").trim().slice(0, 80);
    state.theme = el("themeInput").value === "light" ? "light" : "dark";
    state.accent = el("accentInput").value || "#5865f2";
    state.manualLowData = el("lowDataInput").checked;

    localStorage.setItem("linkup_name", state.name);
    localStorage.setItem("linkup_avatar", state.avatar);
    localStorage.setItem("linkup_bio", state.bio);
    localStorage.setItem("linkup_theme", state.theme);
    localStorage.setItem("linkup_accent", state.accent);
    localStorage.setItem("linkup_lowdata", state.manualLowData ? "1" : "0");

    applyTheme();
    send("profile", { name: state.name, avatar: state.avatar, bio: state.bio, accent: state.accent });
    renderSettingsFields();
    renderFeed();
    renderTrending();
  };

  el("pickAvatarBtn").onclick = () => el("avatarFileInput").click();
  el("clearAvatarBtn").onclick = () => {
    state.avatar = "";
    localStorage.removeItem("linkup_avatar");
    renderSettingsFields();
    send("profile", { name: state.name, avatar: "", bio: state.bio, accent: state.accent });
  };

  el("pickPostMediaBtn").onclick = () => el("postFileInput").click();
  el("clearPostMediaBtn").onclick = () => {
    state.draftPost = { dataUrl: "", type: "text", name: "" };
    renderDraftPreview();
  };

  el("avatarFileInput").onchange = async () => {
    const file = el("avatarFileInput").files && el("avatarFileInput").files[0];
    if (!file) return;

    if (!file.type.startsWith("image/")) {
      alert("Pick an image file for your avatar.");
      el("avatarFileInput").value = "";
      return;
    }

    const dataUrl = await fileToDataURL(file);
    state.avatar = dataUrl;
    localStorage.setItem("linkup_avatar", dataUrl);
    renderSettingsFields();
    send("profile", { name: state.name, avatar: state.avatar, bio: state.bio, accent: state.accent });
    el("avatarFileInput").value = "";
  };

  el("postFileInput").onchange = async () => {
    const file = el("postFileInput").files && el("postFileInput").files[0];
    if (!file) return;

    const kind = fileKind(file);
    if (kind === "text") {
      alert("Pick an image or video file.");
      el("postFileInput").value = "";
      return;
    }

    const dataUrl = await fileToDataURL(file);
    state.draftPost = {
      dataUrl,
      type: kind,
      name: file.name || "",
    };
    renderDraftPreview();
    el("postFileInput").value = "";
  };

  el("postBtn").onclick = () => {
    const text = el("postText").value.trim();
    const media_url = state.draftPost.dataUrl || el("mediaUrl").value.trim();
    const media_type = state.draftPost.type || el("mediaType").value;
    const media_name = state.draftPost.name || "";

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
      media_name,
      created_at: Date.now(),
      likes: 0,
      reposts: 0,
      comments: [],
    };

    optimisticAddPost(post);
    send("post", { id: post.id, text, media_url, media_type, media_name, created_at: post.created_at });

    el("postText").value = "";
    el("mediaUrl").value = "";
    state.draftPost = { dataUrl: "", type: "text", name: "" };
    renderDraftPreview();
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
      is_system: false,
    };

    optimisticAddChat(msg);
    send("chat", { text, reply_to: state.replyTo, msg_id: msg.msg_id, created_at: msg.created_at });

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

  bindMobile();
  bindNetworkSignals();
  renderAll();
  connect();
})();
</script>
</body>
</html>
"""



