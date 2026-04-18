from future import annotations

import json import os import time from collections import defaultdict, deque from dataclasses import dataclass, asdict from typing import Deque, Dict, List, Optional, Set from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect from fastapi.middleware.cors import CORSMiddleware from fastapi.responses import HTMLResponse

app = FastAPI(title="LinkUp Social") app.add_middleware( CORSMiddleware, allow_origins=[""], allow_credentials=True, allow_methods=[""], allow_headers=["*"], )

============================================================

In-memory social graph/state

============================================================

@dataclass class Profile: user_id: str username: str display_name: str avatar_url: str bio: str = "" banner_url: str = "" followers: int = 0 following: int = 0 posts: int = 0 verified: bool = False created_at: int = 0

@dataclass class Post: post_id: str author_id: str author_name: str text: str media_url: str = "" media_type: str = "text"  # text | image | video | short created_at: int = 0 likes: int = 0 reposts: int = 0 comments: int = 0 pinned: bool = False visibility: str = "public"

@dataclass class Comment: comment_id: str post_id: str author_id: str author_name: str text: str created_at: int = 0 likes: int = 0

@dataclass class LiveRoomState: room_id: str title: str description: str viewers: int = 0 live: bool = False created_at: int = 0

class LinkUpState: def init(self) -> None: self.profiles: Dict[str, Profile] = {} self.username_index: Dict[str, str] = {} self.posts: Deque[Post] = deque(maxlen=2000) self.comments: Dict[str, List[Comment]] = defaultdict(list) self.following: Dict[str, Set[str]] = defaultdict(set) self.followers: Dict[str, Set[str]] = defaultdict(set) self.likes: Dict[str, Set[str]] = defaultdict(set) self.reposts: Dict[str, Set[str]] = defaultdict(set) self.bookmarks: Dict[str, Set[str]] = defaultdict(set) self.notifications: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=100)) self.live_rooms: Dict[str, LiveRoomState] = {} self.room_clients: Dict[str, Dict[str, WebSocket]] = defaultdict(dict) self.room_names: Dict[str, Dict[str, str]] = defaultdict(dict) self.room_presence: Dict[str, Dict[str, dict]] = defaultdict(dict) self.current_room: str = "global"

def now(self) -> int:
    return int(time.time() * 1000)

def safe_username(self, name: str) -> str:
    name = (name or "").strip().lower()
    name = "".join(ch for ch in name if ch.isalnum() or ch in {"_", "."})
    return name[:20]

def make_avatar(self, username: str) -> str:
    seed = (username or "user").strip().lower()[:2] or "u"
    return f"https://api.dicebear.com/8.x/thumbs/svg?seed={seed}"

def ensure_profile(self, user_id: str, username: Optional[str] = None) -> Profile:
    if user_id not in self.profiles:
        clean = self.safe_username(username or f"user_{user_id[:6]}")
        if not clean:
            clean = f"user_{user_id[:6]}"
        self.profiles[user_id] = Profile(
            user_id=user_id,
            username=clean,
            display_name=clean,
            avatar_url=self.make_avatar(clean),
            created_at=self.now(),
        )
        self.username_index[clean] = user_id
    return self.profiles[user_id]

def set_username(self, user_id: str, username: str) -> Profile:
    profile = self.ensure_profile(user_id)
    clean = self.safe_username(username)
    if not clean:
        clean = profile.username
    old = profile.username
    if old in self.username_index:
        self.username_index.pop(old, None)
    profile.username = clean
    if not profile.display_name:
        profile.display_name = clean
    profile.avatar_url = profile.avatar_url or self.make_avatar(clean)
    self.username_index[clean] = user_id
    return profile

def set_display_name(self, user_id: str, display_name: str) -> Profile:
    profile = self.ensure_profile(user_id)
    profile.display_name = (display_name or profile.username)[:32]
    return profile

def set_avatar(self, user_id: str, avatar_url: str) -> Profile:
    profile = self.ensure_profile(user_id)
    avatar_url = (avatar_url or "").strip()
    profile.avatar_url = avatar_url or self.make_avatar(profile.username)
    return profile

def follow(self, a: str, b: str) -> None:
    if a == b:
        return
    self.following[a].add(b)
    self.followers[b].add(a)
    self.ensure_profile(a)
    self.ensure_profile(b)
    self.profiles[a].following = len(self.following[a])
    self.profiles[b].followers = len(self.followers[b])

def unfollow(self, a: str, b: str) -> None:
    self.following[a].discard(b)
    self.followers[b].discard(a)
    if a in self.profiles:
        self.profiles[a].following = len(self.following[a])
    if b in self.profiles:
        self.profiles[b].followers = len(self.followers[b])

def add_post(self, author_id: str, text: str, media_url: str = "", media_type: str = "text", visibility: str = "public") -> Post:
    profile = self.ensure_profile(author_id)
    post = Post(
        post_id="p_" + uuid4().hex[:12],
        author_id=author_id,
        author_name=profile.display_name or profile.username,
        text=(text or "").strip(),
        media_url=(media_url or "").strip(),
        media_type=media_type,
        created_at=self.now(),
        visibility=visibility,
    )
    self.posts.appendleft(post)
    profile.posts += 1
    return post

def add_comment(self, post_id: str, author_id: str, text: str) -> Optional[Comment]:
    post = self.get_post(post_id)
    if not post:
        return None
    profile = self.ensure_profile(author_id)
    comment = Comment(
        comment_id="c_" + uuid4().hex[:12],
        post_id=post_id,
        author_id=author_id,
        author_name=profile.display_name or profile.username,
        text=(text or "").strip(),
        created_at=self.now(),
    )
    self.comments[post_id].append(comment)
    post.comments = len(self.comments[post_id])
    return comment

def get_post(self, post_id: str) -> Optional[Post]:
    for p in self.posts:
        if p.post_id == post_id:
            return p
    return None

def get_feed(self, limit: int = 50) -> List[dict]:
    return [self.serialize_post(p) for p in list(self.posts)[:limit]]

def get_profile_posts(self, user_id: str, limit: int = 50) -> List[dict]:
    items = [p for p in self.posts if p.author_id == user_id]
    return [self.serialize_post(p) for p in items[:limit]]

def like_post(self, user_id: str, post_id: str) -> int:
    liked = self.likes[post_id]
    if user_id in liked:
        liked.remove(user_id)
    else:
        liked.add(user_id)
    post = self.get_post(post_id)
    if post:
        post.likes = len(liked)
        return post.likes
    return len(liked)

def repost(self, user_id: str, post_id: str) -> Optional[Post]:
    original = self.get_post(post_id)
    if not original:
        return None
    self.reposts[post_id].add(user_id)
    original.reposts = len(self.reposts[post_id])
    new_post = self.add_post(
        user_id,
        f"Reposted: {original.text}",
        media_url=original.media_url,
        media_type=original.media_type,
        visibility=original.visibility,
    )
    return new_post

def bookmark_post(self, user_id: str, post_id: str) -> None:
    if post_id in self.bookmarks[user_id]:
        self.bookmarks[user_id].remove(post_id)
    else:
        self.bookmarks[user_id].add(post_id)

def pins_for_profile(self, user_id: str) -> List[str]:
    return list(self.bookmarks[user_id])

def serialize_profile(self, profile: Profile) -> dict:
    return asdict(profile)

def serialize_post(self, post: Post) -> dict:
    return asdict(post)

def serialize_comment(self, c: Comment) -> dict:
    return asdict(c)

def search_posts(self, q: str) -> List[dict]:
    q = (q or "").strip().lower()
    if not q:
        return self.get_feed()
    out = []
    for p in self.posts:
        if q in p.text.lower() or q in p.author_name.lower():
            out.append(self.serialize_post(p))
    return out[:50]

def ensure_live_room(self, room_id: str) -> LiveRoomState:
    if room_id not in self.live_rooms:
        self.live_rooms[room_id] = LiveRoomState(
            room_id=room_id,
            title=room_id.replace("_", " ").title(),
            description="Live social room",
            created_at=self.now(),
        )
    return self.live_rooms[room_id]

state = LinkUpState()

Seed content

seed_users = [ ("u_1", "nas9229alt"), ("u_2", "pixelwave"), ("u_3", "neonbyte"), ] for uid, uname in seed_users: p = state.ensure_profile(uid, uname) p.display_name = uname

state.add_post("u_1", "LinkUp just got a social-feed mode. Post, react, pin, and browse profiles.") state.add_post("u_2", "Short-form clips are coming next. Think TikTok-style vertical posts.", media_type="short") state.add_post("u_3", "YouTube-ish long posts, playlists, and creator pages can fit into this too.", media_type="video")

============================================================

WebSocket social rooms

============================================================

async def broadcast_room(room_id: str, payload: dict) -> None: sockets = state.room_clients[room_id] dead = [] for client_id, ws in sockets.items(): try: await ws.send_json(payload) except Exception: dead.append(client_id) for cid in dead: sockets.pop(cid, None) state.room_names[room_id].pop(cid, None) state.room_presence[room_id].pop(cid, None)

@app.websocket("/ws/{room_id}") async def ws_room(websocket: WebSocket, room_id: str): await websocket.accept() room = state.ensure_live_room(room_id)

client_id = "u_" + uuid4().hex[:10]
profile = state.ensure_profile(client_id)
state.room_clients[room_id][client_id] = websocket
state.room_names[room_id][client_id] = profile.display_name
state.room_presence[room_id][client_id] = {
    "client_id": client_id,
    "username": profile.username,
    "display_name": profile.display_name,
    "avatar_url": profile.avatar_url,
    "profile": state.serialize_profile(profile),
    "online": True,
    "joined_at": state.now(),
}
room.viewers = len(state.room_clients[room_id])

await websocket.send_json({
    "kind": "init",
    "you": client_id,
    "room": room_id,
    "profile": state.serialize_profile(profile),
    "feed": state.get_feed(50),
    "live_room": asdict(room),
    "users": list(state.room_presence[room_id].values()),
    "notifications": list(state.notifications[client_id]),
    "time": state.now(),
})

await broadcast_room(room_id, {
    "kind": "presence",
    "room": room_id,
    "users": list(state.room_presence[room_id].values()),
    "time": state.now(),
})

try:
    while True:
        raw = await websocket.receive_text()
        try:
            data = json.loads(raw)
        except Exception:
            data = {}

        kind = data.get("kind")

        if kind == "set_username":
            new_username = data.get("username", "")
            old = profile.username
            profile = state.set_username(client_id, new_username)
            state.set_display_name(client_id, data.get("display_name", profile.display_name))
            state.room_names[room_id][client_id] = profile.display_name
            state.room_presence[room_id][client_id]["username"] = profile.username
            state.room_presence[room_id][client_id]["display_name"] = profile.display_name
            state.room_presence[room_id][client_id]["profile"] = state.serialize_profile(profile)
            await broadcast_room(room_id, {
                "kind": "profile_update",
                "client_id": client_id,
                "profile": state.serialize_profile(profile),
                "old_username": old,
                "time": state.now(),
            })
            continue

        if kind == "set_display_name":
            profile = state.set_display_name(client_id, data.get("display_name", profile.display_name))
            state.room_names[room_id][client_id] = profile.display_name
            state.room_presence[room_id][client_id]["display_name"] = profile.display_name
            await broadcast_room(room_id, {
                "kind": "profile_update",
                "client_id": client_id,
                "profile": state.serialize_profile(profile),
                "time": state.now(),
            })
            continue

        if kind == "set_avatar":
            profile = state.set_avatar(client_id, data.get("avatar_url", profile.avatar_url))
            state.room_presence[room_id][client_id]["avatar_url"] = profile.avatar_url
            state.room_presence[room_id][client_id]["profile"] = state.serialize_profile(profile)
            await broadcast_room(room_id, {
                "kind": "profile_update",
                "client_id": client_id,
                "profile": state.serialize_profile(profile),
                "time": state.now(),
            })
            continue

        if kind == "follow":
            target = data.get("target_user_id", "")
            if target:
                state.follow(client_id, target)
                await websocket.send_json({"kind": "follow_result", "ok": True, "time": state.now()})
            continue

        if kind == "unfollow":
            target = data.get("target_user_id", "")
            if target:
                state.unfollow(client_id, target)
                await websocket.send_json({"kind": "follow_result", "ok": True, "time": state.now()})
            continue

        if kind == "room_post":
            text = (data.get("text") or "").strip()
            media_url = (data.get("media_url") or "").strip()
            media_type = (data.get("media_type") or "text").strip()
            visibility = (data.get("visibility") or "public").strip()
            if not text and not media_url:
                continue
            post = state.add_post(client_id, text, media_url=media_url, media_type=media_type, visibility=visibility)
            await broadcast_room(room_id, {
                "kind": "feed_post",
                "post": state.serialize_post(post),
                "time": state.now(),
            })
            continue

        if kind == "comment":
            post_id = data.get("post_id", "")
            text = (data.get("text") or "").strip()
            if not post_id or not text:
                continue
            c = state.add_comment(post_id, client_id, text)
            if c:
                post = state.get_post(post_id)
                await broadcast_room(room_id, {
                    "kind": "comment_added",
                    "post_id": post_id,
                    "comment": state.serialize_comment(c),
                    "post": state.serialize_post(post) if post else None,
                    "time": state.now(),
                })
            continue

        if kind == "like":
            post_id = data.get("post_id", "")
            if not post_id:
                continue
            count = state.like_post(client_id, post_id)
            post = state.get_post(post_id)
            await broadcast_room(room_id, {
                "kind": "like_update",
                "post_id": post_id,
                "likes": count,
                "post": state.serialize_post(post) if post else None,
                "time": state.now(),
            })
            continue

        if kind == "repost":
            post_id = data.get("post_id", "")
            if not post_id:
                continue
            new_post = state.repost(client_id, post_id)
            if new_post:
                await broadcast_room(room_id, {
                    "kind": "feed_post",
                    "post": state.serialize_post(new_post),
                    "repost_of": post_id,
                    "time": state.now(),
                })
            continue

        if kind == "bookmark":
            post_id = data.get("post_id", "")
            if post_id:
                state.bookmark_post(client_id, post_id)
                await websocket.send_json({"kind": "bookmark_result", "ok": True, "time": state.now()})
            continue

        if kind == "search_posts":
            q = data.get("query", "")
            results = state.search_posts(q)
            await websocket.send_json({"kind": "search_results", "query": q, "results": results, "time": state.now()})
            continue

        if kind == "get_profile":
            uid = data.get("user_id") or client_id
            prof = state.ensure_profile(uid)
            posts = state.get_profile_posts(uid)
            await websocket.send_json({
                "kind": "profile_data",
                "profile": state.serialize_profile(prof),
                "posts": posts,
                "following": list(state.following[uid]),
                "followers": list(state.followers[uid]),
                "bookmarks": list(state.bookmarks[uid]),
                "time": state.now(),
            })
            continue

        if kind == "get_feed":
            await websocket.send_json({"kind": "feed_data", "posts": state.get_feed(50), "time": state.now()})
            continue

        if kind == "typing":
            await broadcast_room(room_id, {
                "kind": "typing",
                "client_id": client_id,
                "display_name": profile.display_name,
                "time": state.now(),
            })
            continue

        if kind == "ping":
            await websocket.send_json({"kind": "pong", "time": state.now()})
            continue

except WebSocketDisconnect:
    state.room_clients[room_id].pop(client_id, None)
    state.room_names[room_id].pop(client_id, None)
    state.room_presence[room_id].pop(client_id, None)
    room.viewers = len(state.room_clients[room_id])
    await broadcast_room(room_id, {
        "kind": "presence",
        "room": room_id,
        "users": list(state.room_presence[room_id].values()),
        "time": state.now(),
    })

============================================================

REST helpers

============================================================

@app.get("/health") def health(): return {"ok": True, "app": "LinkUp Social"}

@app.get("/api/feed") def api_feed(limit: int = 50): return {"posts": state.get_feed(limit)}

@app.get("/api/profile/{user_id}") def api_profile(user_id: str): prof = state.ensure_profile(user_id) return { "profile": state.serialize_profile(prof), "posts": state.get_profile_posts(user_id), "followers": list(state.followers[user_id]), "following": list(state.following[user_id]), "bookmarks": list(state.bookmarks[user_id]), }

@app.post("/api/mock-avatars/{user_id}") def api_mock_avatar(user_id: str): prof = state.ensure_profile(user_id) prof.avatar_url = state.make_avatar(prof.username) return {"profile": state.serialize_profile(prof)}

============================================================

Frontend: social app shell

============================================================

@app.get("/", response_class=HTMLResponse) def home(): return HTMLResponse(_html())

@app.get("/{path:path}", response_class=HTMLResponse) def spa(path: str): return HTMLResponse(_html())

def _html() -> str: return r"""<!doctype html>

<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>LinkUp</title>
  <style>
    :root{
      --bg:#0f1115;
      --panel:#15181e;
      --panel2:#1b1f27;
      --panel3:#212633;
      --line:rgba(255,255,255,.06);
      --text:#f4f7fb;
      --muted:#a7afbe;
      --accent:#6d7cff;
      --accent2:#4fd18c;
      --accent3:#ffb84d;
      --danger:#ff6b7f;
      --radius:18px;
      color-scheme: dark;
    }
    *{box-sizing:border-box}
    html,body{height:100%}
    body{
      margin:0;
      background:var(--bg);
      color:var(--text);
      font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      overflow:hidden;
    }
    button,input,textarea{font:inherit}
    .app{
      height:100vh;
      display:grid;
      grid-template-columns: 76px 260px minmax(0,1fr) 330px;
      min-width:0;
    }
    .rail{background:var(--panel2); border-right:1px solid var(--line); overflow:auto;}
    .left-rail{display:flex; flex-direction:column; align-items:center; gap:10px; padding:12px 0;}
    .srv{width:48px;height:48px;border:none;border-radius:16px;background:#262b35;color:var(--text);font-weight:800;cursor:pointer;}
    .srv.active{background:var(--accent);}
    .srv:hover{filter:brightness(1.05)}
    .sidebar{background:var(--panel); display:flex; flex-direction:column; min-width:0; overflow:hidden; border-right:1px solid var(--line);}
    .side-head{padding:16px;border-bottom:1px solid var(--line)}
    .brand{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
    .brand h1{margin:0;font-size:18px}
    .brand small{display:block;color:var(--muted);font-size:12px;margin-top:4px}
    .pill{display:inline-flex;align-items:center;gap:8px;margin-top:10px;padding:7px 10px;border-radius:999px;background:rgba(255,255,255,.05);color:var(--muted);font-size:12px}
    .channels{padding:10px 10px 12px; overflow:auto; min-height:0;}
    .section-title{margin:8px 8px 10px; font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:#808897}
    .channel{display:flex;justify-content:space-between;align-items:center;width:100%;border:none;background:transparent;color:var(--text);padding:10px 10px;border-radius:10px;cursor:pointer;text-align:left}
    .channel:hover{background:rgba(255,255,255,.04)}
    .channel.active{background:rgba(255,255,255,.08)}
    .channel .meta{min-width:0;display:flex;flex-direction:column}
    .channel .meta strong{font-size:14px}
    .channel .meta span{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px}
    .main{display:flex;flex-direction:column;min-width:0;overflow:hidden;background:linear-gradient(180deg, #12151c 0%, #0f1115 100%)}
    .topbar{height:54px; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:0 16px; border-bottom:1px solid var(--line); background:rgba(21,24,30,.94); backdrop-filter:blur(10px)}
    .title{min-width:0}
    .title h2{margin:0;font-size:16px}
    .title p{margin:2px 0 0;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:45vw}
    .status{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px;flex:0 0 auto}
    .dot{width:10px;height:10px;border-radius:999px;background:var(--accent2);box-shadow:0 0 0 4px rgba(79,209,140,.12)}
    .meta-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.02)}
    .stats{display:flex;flex-wrap:wrap;gap:8px;min-width:0}
    .stat{padding:7px 10px;border-radius:999px;background:rgba(255,255,255,.05);color:var(--muted);font-size:12px;white-space:nowrap}
    .toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
    .search{width:min(420px, 42vw);padding:10px 12px;border:none;outline:none;border-radius:10px;background:#1f242f;color:var(--text)}
    .btn{border:none;border-radius:10px;background:#262b35;color:var(--text);padding:10px 12px;cursor:pointer}
    .btn:hover{background:#2e3441}
    .feed{flex:1; min-height:0; overflow-y:auto; padding:18px 0 120px}
    .section{margin:0 auto; width:min(920px, calc(100vw - 32px));}
    .composer-wrap{position:sticky; bottom:0; z-index:4; background:linear-gradient(180deg, rgba(15,17,21,0), rgba(15,17,21,.85) 30%, rgba(15,17,21,1)); padding:14px 0 18px}
    .composer{width:min(920px, calc(100vw - 32px)); margin:0 auto; border:1px solid var(--line); border-radius:18px; background:var(--panel); padding:14px}
    .composer-top{display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:10px}
    .namepill{padding:8px 10px;border-radius:999px;background:rgba(255,255,255,.05);color:var(--muted);font-size:12px}
    .composer textarea{width:100%; min-height:76px; max-height:180px; resize:none; overflow-y:auto; border:none; outline:none; background:transparent; color:var(--text); line-height:1.6; font-size:15px}
    .composer-actions{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-top:10px}
    .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
    .accent{background:var(--accent)}
    .accent2{background:var(--accent2)}
    .ghost{background:#262b35}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:14px}
    .post{background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:14px; margin-bottom:12px}
    .post-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
    .author{display:flex;gap:10px;align-items:center;min-width:0}
    .avatar{width:44px;height:44px;border-radius:50%;background:#272c37;object-fit:cover;flex:0 0 auto}
    .author strong{display:block;font-size:14px}
    .author span{display:block;font-size:12px;color:var(--muted)}
    .media{margin-top:12px;border-radius:16px;overflow:hidden;background:#000}
    .media img,.media video{width:100%;display:block;max-height:520px;object-fit:cover}
    .post-text{margin-top:10px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
    .post-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
    .tiny{border:none;padding:8px 10px;border-radius:999px;background:#262b35;color:var(--text);cursor:pointer;font-size:12px}
    .tiny:hover{background:#303646}
    .counts{color:var(--muted);font-size:12px}
    .comments{margin-top:12px;display:flex;flex-direction:column;gap:8px}
    .comment{padding:10px 12px;border-radius:14px;background:rgba(255,255,255,.03);border:1px solid var(--line)}
    .comment .who{font-weight:700;font-size:13px;margin-bottom:4px}
    .right{background:var(--panel);border-left:1px solid var(--line);display:flex;flex-direction:column;min-width:0;overflow:hidden}
    .right-head{padding:16px;border-bottom:1px solid var(--line)}
    .right-body{overflow-y:auto;min-height:0;padding:10px}
    .profile-box{display:flex;align-items:center;gap:12px}
    .profile-pic{width:56px;height:56px;border-radius:50%;background:#272c37;object-fit:cover}
    .profile-box h3{margin:0;font-size:16px}
    .profile-box p{margin:4px 0 0;color:var(--muted);font-size:12px;line-height:1.5}
    .field{display:flex;flex-direction:column;gap:6px;margin-top:10px}
    .field label{font-size:12px;color:#c2c8d4}
    .field input,.field textarea{width:100%;padding:10px 12px;border-radius:12px;border:none;outline:none;background:#1f242f;color:var(--text)}
    .mini-list{display:flex;flex-direction:column;gap:8px}
    .mini-item{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;border-radius:12px;background:rgba(255,255,255,.04)}
    .mini-item strong{font-size:13px}
    .mini-item span{font-size:12px;color:var(--muted)}
    .typing{height:18px;padding:0 16px 8px;color:#c9ced8;font-size:12px}
    .system{color:#b8beca;font-style:italic;padding:8px 16px}
    .reply-preview{margin-top:8px;padding:10px 12px;border-left:3px solid var(--accent);background:rgba(109,124,255,.10);border-radius:12px;color:#e4e8ff;font-size:12px}
    .muted{color:var(--muted)}
    .scrollbar::-webkit-scrollbar{width:10px;height:10px}
    .scrollbar::-webkit-scrollbar-thumb{background:#2a2f3a;border-radius:999px}
    .scrollbar::-webkit-scrollbar-track{background:transparent}
    @media (max-width: 1200px){
      .app{grid-template-columns:76px 240px minmax(0,1fr)}
      .right{display:none}
      .search{width:min(320px, 40vw)}
    }
    @media (max-width: 820px){
      body{overflow:auto}
      .app{height:auto;min-height:100vh;grid-template-columns:1fr}
      .rail,.sidebar,.main,.right{border:none}
      .rail{flex-direction:row;overflow-x:auto;overflow-y:hidden;padding:10px;display:flex}
      .left-rail{flex-direction:row;padding:0}
      .sidebar{min-height:230px}
      .right{display:block;min-height:240px}
      .topbar,.meta-row{flex-direction:column;align-items:stretch}
      .toolbar,.search{width:100%}
      .section,.composer{width:calc(100vw - 20px)}
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="rail left-rail scrollbar" id="servers"></aside><aside class="sidebar">
  <div class="side-head">
    <div class="brand">
      <div>
        <h1>LinkUp</h1>
        <small>social feed + short video + creator space</small>
      </div>
      <div class="stat">LIVE</div>
    </div>
    <div class="pill">Room <span id="roomLabel">#global</span></div>
  </div>
  <div class="channels scrollbar">
    <div class="section-title">Spaces</div>
    <div id="channels"></div>
  </div>
</aside>

<main class="main scrollbar">
  <div class="topbar">
    <div class="title">
      <h2 id="channelTitle">Global Feed</h2>
      <p id="channelDescription">Post updates, clips, short videos, and creator content.</p>
    </div>
    <div class="status"><span class="dot"></span><span id="connState">connecting...</span></div>
  </div>

  <div class="meta-row">
    <div class="stats">
      <div class="stat" id="statPosts">0 posts</div>
      <div class="stat" id="statUsers">0 online</div>
      <div class="stat" id="statFeed">0 items</div>
    </div>
    <div class="toolbar">
      <input class="search" id="searchInput" placeholder="Search posts, people, clips..." />
      <button class="btn" id="refreshFeed">Refresh</button>
    </div>
  </div>

  <div class="typing" id="typingLine"></div>
  <div class="feed"><div class="section" id="feed"></div></div>

  <div class="composer-wrap">
    <div class="composer">
      <div class="composer-top">
        <span class="namepill" id="youName">You</span>
        <span class="namepill" id="modeLabel">Post mode</span>
        <span class="namepill" id="replyLabel" style="display:none"></span>
      </div>
      <textarea id="composerText" placeholder="What’s happening on LinkUp?"></textarea>
      <div class="composer-actions">
        <div class="row">
          <button class="tiny" id="modePost">Post</button>
          <button class="tiny" id="modeClip">Short</button>
          <button class="tiny" id="modeVideo">Video</button>
          <button class="tiny" id="modeLive">Live</button>
        </div>
        <div class="row">
          <button class="tiny" id="postBtn">Publish</button>
        </div>
      </div>
    </div>
  </div>
</main>

<aside class="right scrollbar">
  <div class="right-head">
    <div class="profile-box">
      <img class="profile-pic" id="profilePic" alt="avatar" />
      <div>
        <h3 id="profileName">Your profile</h3>
        <p id="profileBio">Edit your name, avatar, and bio anytime.</p>
      </div>
    </div>
  </div>
  <div class="right-body">
    <div class="card">
      <div class="field">
        <label>Username</label>
        <input id="usernameInput" placeholder="username" />
      </div>
      <div class="field">
        <label>Display name</label>
        <input id="displayNameInput" placeholder="display name" />
      </div>
      <div class="field">
        <label>Avatar URL</label>
        <input id="avatarInput" placeholder="https://..." />
      </div>
      <div class="field">
        <label>Bio</label>
        <textarea id="bioInput" placeholder="Write something cool..."></textarea>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="btn accent" id="saveProfile">Save profile</button>
        <button class="btn ghost" id="randomAvatar">Random avatar</button>
      </div>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="section-title">Online now</div>
      <div class="mini-list" id="onlineList"></div>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="section-title">Bookmarks</div>
      <div class="mini-list" id="bookmarksList"></div>
    </div>
  </div>
</aside>

  </div><script>
(() => {
  const servers = [
    { id: 'global', name: 'G', label: 'Global' },
    { id: 'shorts', name: 'S', label: 'Shorts' },
    { id: 'video', name: 'V', label: 'Video' },
    { id: 'live', name: 'L', label: 'Live' },
  ];

  const channelsByServer = {
    global: [
      { id: 'global', title: 'Global Feed', description: 'Posts, creators, clips, and discussions.' },
      { id: 'trending', title: 'Trending', description: 'Fast-moving popular posts.' },
      { id: 'explore', title: 'Explore', description: 'Search and discover content.' },
    ],
    shorts: [
      { id: 'shorts', title: 'Shorts', description: 'Vertical clips and quick takes.' },
      { id: 'remix', title: 'Remix', description: 'Reposts, stitches, and duets.' },
      { id: 'fun', title: 'Fun', description: 'Funny clips and memes.' },
    ],
    video: [
      { id: 'video', title: 'Videos', description: 'Long-form creator content.' },
      { id: 'guides', title: 'Guides', description: 'Tutorials and how-tos.' },
      { id: 'playlists', title: 'Playlists', description: 'Series and collections.' },
    ],
    live: [
      { id: 'live', title: 'Live', description: 'Live rooms and stream chat.' },
      { id: 'events', title: 'Events', description: 'Scheduled live sessions.' },
      { id: 'backstage', title: 'Backstage', description: 'Creator and host prep.' },
    ],
  };

  const defaultAvatar = (seed) => `https://api.dicebear.com/8.x/thumbs/svg?seed=${encodeURIComponent(seed || 'user')}`;

  const state = {
    socket: null,
    seq: 0,
    reconnectTimer: null,
    handshakeTimer: null,
    retryCount: 0,
    typingTimer: null,
    localUserId: localStorage.getItem('linkup_user_id') || ('u_' + Math.random().toString(36).slice(2, 10)),
    username: localStorage.getItem('linkup_username') || '',
    displayName: localStorage.getItem('linkup_display_name') || '',
    avatarUrl: localStorage.getItem('linkup_avatar') || '',
    bio: localStorage.getItem('linkup_bio') || '',
    server: localStorage.getItem('linkup_server') || 'global',
    channel: localStorage.getItem('linkup_channel') || 'global',
    mode: 'text',
    replyTo: null,
    online: [],
    bookmarks: [],
    feedCache: [],
    feedMap: new Map(),
    profile: null,
    currentRoom: roomKey(),
    search: '',
    typing: '',
  };

  localStorage.setItem('linkup_user_id', state.localUserId);

  const els = {
    servers: document.getElementById('servers'),
    channels: document.getElementById('channels'),
    feed: document.getElementById('feed'),
    connState: document.getElementById('connState'),
    roomLabel: document.getElementById('roomLabel'),
    channelTitle: document.getElementById('channelTitle'),
    channelDescription: document.getElementById('channelDescription'),
    searchInput: document.getElementById('searchInput'),
    refreshFeed: document.getElementById('refreshFeed'),
    composerText: document.getElementById('composerText'),
    postBtn: document.getElementById('postBtn'),
    modePost: document.getElementById('modePost'),
    modeClip: document.getElementById('modeClip'),
    modeVideo: document.getElementById('modeVideo'),
    modeLive: document.getElementById('modeLive'),
    modeLabel: document.getElementById('modeLabel'),
    replyLabel: document.getElementById('replyLabel'),
    typingLine: document.getElementById('typingLine'),
    onlineList: document.getElementById('onlineList'),
    bookmarksList: document.getElementById('bookmarksList'),
    usernameInput: document.getElementById('usernameInput'),
    displayNameInput: document.getElementById('displayNameInput'),
    avatarInput: document.getElementById('avatarInput'),
    bioInput: document.getElementById('bioInput'),
    saveProfile: document.getElementById('saveProfile'),
    randomAvatar: document.getElementById('randomAvatar'),
    profilePic: document.getElementById('profilePic'),
    profileName: document.getElementById('profileName'),
    profileBio: document.getElementById('profileBio'),
    youName: document.getElementById('youName'),
    statPosts: document.getElementById('statPosts'),
    statUsers: document.getElementById('statUsers'),
    statFeed: document.getElementById('statFeed'),
  };

  function roomKey() {
    return `${state.server}:${state.channel}`;
  }

  function setStatus(text, ok = false) {
    els.connState.textContent = text;
    els.connState.style.color = ok ? '#d7ffe7' : '';
  }

  function cleanText(value) {
    return String(value || '').trim();
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[ch]));
  }

  function timeText(ts) {
    return new Date(ts || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function setProfileFields(profile) {
    els.usernameInput.value = profile?.username || state.username || '';
    els.displayNameInput.value = profile?.display_name || state.displayName || '';
    els.avatarInput.value = profile?.avatar_url || state.avatarUrl || '';
    els.bioInput.value = profile?.bio || state.bio || '';
    els.profilePic.src = profile?.avatar_url || state.avatarUrl || defaultAvatar(profile?.username || state.username || 'user');
    els.profileName.textContent = profile?.display_name || profile?.username || 'Your profile';
    els.profileBio.textContent = profile?.bio || 'Edit your name, avatar, and bio anytime.';
    els.youName.textContent = profile?.display_name || profile?.username || 'You';
  }

  function saveLocalProfile(profile) {
    if (!profile) return;
    state.profile = profile;
    state.username = profile.username || '';
    state.displayName = profile.display_name || '';
    state.avatarUrl = profile.avatar_url || '';
    state.bio = profile.bio || '';
    localStorage.setItem('linkup_username', state.username);
    localStorage.setItem('linkup_display_name', state.displayName);
    localStorage.setItem('linkup_avatar', state.avatarUrl);
    localStorage.setItem('linkup_bio', state.bio);
    setProfileFields(profile);
  }

  function renderServers() {
    els.servers.innerHTML = servers.map(s => `
      <button class="srv ${s.id === state.server ? 'active' : ''}" data-server="${s.id}" title="${escapeHtml(s.label)}">${escapeHtml(s.name)}</button>
    `).join('');
    els.servers.querySelectorAll('button').forEach(btn => {
      btn.onclick = () => {
        const next = btn.dataset.server;
        if (next === state.server) return;
        state.server = next;
        state.channel = channelsByServer[next][0].id;
        localStorage.setItem('linkup_server', state.server);
        localStorage.setItem('linkup_channel', state.channel);
        state.currentRoom = roomKey();
        connect(true);
        renderShell();
      };
    });
  }

  function renderChannels() {
    els.channels.innerHTML = (channelsByServer[state.server] || []).map(ch => `
      <button class="channel ${ch.id === state.channel ? 'active' : ''}" data-channel="${ch.id}">
        <div class="meta"><strong>${escapeHtml(ch.title)}</strong><span>${escapeHtml(ch.description)}</span></div>
        <span class="badge">#</span>
      </button>
    `).join('');
    els.channels.querySelectorAll('button').forEach(btn => {
      btn.onclick = () => {
        const next = btn.dataset.channel;
        if (next === state.channel) return;
        state.channel = next;
        state.currentRoom = roomKey();
        localStorage.setItem('linkup_channel', state.channel);
        connect(true);
        renderShell();
      };
    });
  }

  function renderTop() {
    const ch = (channelsByServer[state.server] || []).find(x => x.id === state.channel) || channelsByServer[state.server][0];
    els.roomLabel.textContent = `#${state.channel}`;
    els.channelTitle.textContent = ch?.title || 'Global Feed';
    els.channelDescription.textContent = ch?.description || 'Post updates, clips, short videos, and creator content.';
  }

  function feedToHtml(post) {
    const mediaHtml = post.media_url ? (
      post.media_type === 'video' || post.media_type === 'short'
        ? `<div class="media"><video controls playsinline src="${escapeHtml(post.media_url)}"></video></div>`
        : `<div class="media"><img src="${escapeHtml(post.media_url)}" alt="media" /></div>`
    ) : '';

    const comments = (post._comments || []).slice(-3).map(c => `
      <div class="comment">
        <div class="who">${escapeHtml(c.author_name)}</div>
        <div>${escapeHtml(c.text)}</div>
      </div>
    `).join('');

    return `
      <article class="post" data-post="${escapeHtml(post.post_id)}">
        <div class="post-head">
          <div class="author">
            <img class="avatar" src="${escapeHtml(post._avatar || defaultAvatar(post.author_name))}" alt="avatar" />
            <div>
              <strong>${escapeHtml(post.author_name)} ${post.media_type === 'short' ? '<span class="badge">short</span>' : ''}</strong>
              <span>${escapeHtml(post.author_tag || '@' + post.author_name.toLowerCase())} • ${timeText(post.created_at)}</span>
            </div>
          </div>
          <button class="tiny" data-action="bookmark" data-post="${escapeHtml(post.post_id)}">${post._bookmarked ? 'Bookmarked' : 'Bookmark'}</button>
        </div>
        ${post.text ? `<div class="post-text">${escapeHtml(post.text)}</div>` : ''}
        ${mediaHtml}
        <div class="post-actions">
          <button class="tiny" data-action="like" data-post="${escapeHtml(post.post_id)}">Like · ${post.likes || 0}</button>
          <button class="tiny" data-action="comment" data-post="${escapeHtml(post.post_id)}">Comment · ${post.comments || 0}</button>
          <button class="tiny" data-action="repost" data-post="${escapeHtml(post.post_id)}">Repost · ${post.reposts || 0}</button>
          <button class="tiny" data-action="reply" data-post="${escapeHtml(post.post_id)}">Reply</button>
          <button class="tiny" data-action="pin" data-post="${escapeHtml(post.post_id)}">Pin</button>
        </div>
        ${comments ? `<div class="comments">${comments}</div>` : ''}
      </article>
    `;
  }

  function decorateFeed(items) {
    const bookmarked = new Set(state.bookmarks);
    return items.map(p => ({
      ...p,
      _avatar: (state.feedMap.get(p.author_id)?.avatar_url) || defaultAvatar(p.author_name),
      _bookmarked: bookmarked.has(p.post_id),
      _comments: p._comments || []
    }));
  }

  function renderFeed() {
    const q = cleanText(state.search).toLowerCase();
    let items = state.feedCache.slice();
    if (q) {
      items = items.filter(p => String(p.text || '').toLowerCase().includes(q) || String(p.author_name || '').toLowerCase().includes(q));
    }
    els.feed.innerHTML = decorateFeed(items).map(feedToHtml).join('') || `<div class="card muted">No posts yet.</div>`;
    els.statPosts.textContent = `${state.feedCache.length} posts`;
    els.statFeed.textContent = `${items.length} items`;
    bindFeedActions();
  }

  function renderOnline(users) {
    state.online = users || [];
    els.statUsers.textContent = `${state.online.length} online`;
    els.onlineList.innerHTML = state.online.length ? state.online.map(u => `
      <div class="mini-item">
        <div>
          <strong>${escapeHtml(u.display_name || u.username || 'user')}</strong><br/>
          <span>@${escapeHtml(u.username || 'user')}</span>
        </div>
        <button class="tiny" data-user="${escapeHtml(u.client_id)}">Open</button>
      </div>
    `).join('') : `<div class="mini-item"><div><strong>No one yet</strong><br/><span>Be the first online.</span></div></div>`;
  }

  function renderBookmarks() {
    const ids = state.bookmarks || [];
    const list = ids.map(id => state.feedCache.find(p => p.post_id === id)).filter(Boolean);
    els.bookmarksList.innerHTML = list.length ? list.map(p => `
      <div class="mini-item">
        <div><strong>${escapeHtml(p.author_name)}</strong><br/><span>${escapeHtml((p.text || '').slice(0, 65))}</span></div>
        <button class="tiny" data-jump="${escapeHtml(p.post_id)}">Jump</button>
      </div>
    `).join('') : `<div class="mini-item"><div><strong>No bookmarks</strong><br/><span>Save posts here.</span></div></div>`;
    els.bookmarksList.querySelectorAll('[data-jump]').forEach(btn => {
      btn.onclick = () => {
        const target = els.feed.querySelector(`[data-post="${CSS.escape(btn.dataset.jump)}"]`);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      };
    });
  }

  function syncCounts() {
    els.statPosts.textContent = `${state.feedCache.length} posts`;
    els.statUsers.textContent = `${state.online.length} online`;
  }

  function renderShell() {
    renderServers();
    renderChannels();
    renderTop();
    setProfileFields(state.profile || {
      username: state.username,
      display_name: state.displayName,
      avatar_url: state.avatarUrl,
      bio: state.bio
    });
    renderOnline(state.online);
    renderBookmarks();
    renderFeed();
    syncCounts();
  }

  function normalizePost(p) {
    if (!p) return p;
    p._comments = p._comments || [];
    return p;
  }

  function upsertFeed(posts, prepend = false) {
    const map = new Map(state.feedCache.map(p => [p.post_id, normalizePost(p)]));
    for (const raw of posts || []) {
      const p = normalizePost(raw);
      map.set(p.post_id, p);
    }
    const arr = Array.from(map.values()).sort((a, b) => Number(b.created_at) - Number(a.created_at));
    state.feedCache = prepend ? arr : arr;
    state.feedMap = new Map(state.feedCache.map(p => [p.author_id, {
      avatar_url: p._avatar || defaultAvatar(p.author_name),
      name: p.author_name,
    }]));
  }

  function wsUrl(room) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${location.host}/ws/${encodeURIComponent(room)}`;
  }

  function cleanupTimers() {
    if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
    if (state.handshakeTimer) clearTimeout(state.handshakeTimer);
    state.reconnectTimer = null;
    state.handshakeTimer = null;
  }

  function scheduleReconnect(seq) {
    if (seq !== state.seq) return;
    const delay = Math.min(1000 * Math.pow(2, state.retryCount), 10000);
    state.retryCount += 1;
    state.reconnectTimer = setTimeout(() => {
      if (seq !== state.seq) return;
      connect(false);
    }, delay);
    setStatus(`reconnecting in ${Math.ceil(delay / 1000)}s`);
  }

  function connect(force = false) {
    cleanupTimers();
    const seq = ++state.seq;
    const room = roomKey();
    state.currentRoom = room;
    let opened = false;

    if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) {
      try { state.socket.close(1000, 'switch'); } catch {}
    }

    setStatus('connecting...');

    try {
      state.socket = new WebSocket(wsUrl(room));
    } catch (e) {
      setStatus('socket unavailable');
      scheduleReconnect(seq);
      return;
    }

    state.handshakeTimer = setTimeout(() => {
      if (seq !== state.seq) return;
      if (!opened) setStatus('connecting... still trying');
    }, 3500);

    state.socket.onopen = () => {
      if (seq !== state.seq) return;
      opened = true;
      state.retryCount = 0;
      cleanupTimers();
      setStatus('connected', true);
      state.socket.send(JSON.stringify({
        kind: 'hello',
        user_id: state.localUserId,
        username: state.username,
        display_name: state.displayName,
        avatar_url: state.avatarUrl,
        bio: state.bio,
        room,
        time: Date.now()
      }));
    };

    state.socket.onclose = () => {
      if (seq !== state.seq) return;
      cleanupTimers();
      setStatus(opened ? 'disconnected' : 'offline');
      scheduleReconnect(seq);
    };

    state.socket.onerror = () => {
      if (seq !== state.seq) return;
      if (!opened) setStatus('connection error');
    };

    state.socket.onmessage = (event) => {
      if (seq !== state.seq) return;
      let data = null;
      try { data = JSON.parse(event.data); } catch { return; }

      if (data.kind === 'init') {
        if (data.profile) saveLocalProfile(data.profile);
        upsertFeed(data.feed || [], false);
        state.online = data.users || [];
        state.bookmarks = data.notifications ? [] : state.bookmarks;
        setStatus('connected', true);
        renderShell();
        return;
      }

      if (data.kind === 'presence') {
        state.online = data.users || [];
        renderOnline(state.online);
        return;
      }

      if (data.kind === 'profile_update') {
        if (data.client_id === state.localUserId && data.profile) {
          saveLocalProfile(data.profile);
        }
        state.online = state.online.map(u => u.client_id === data.client_id ? {
          ...u,
          username: data.profile?.username || u.username,
          display_name: data.profile?.display_name || u.display_name,
          avatar_url: data.profile?.avatar_url || u.avatar_url,
          profile: data.profile || u.profile,
        } : u);
        renderOnline(state.online);
        renderShell();
        return;
      }

      if (data.kind === 'feed_post') {
        const post = normalizePost(data.post);
        if (post) {
          const idx = state.feedCache.findIndex(p => p.post_id === post.post_id);
          if (idx >= 0) state.feedCache[idx] = post; else state.feedCache.unshift(post);
          state.feedCache.sort((a, b) => Number(b.created_at) - Number(a.created_at));
          renderFeed();
        }
        return;
      }

      if (data.kind === 'comment_added') {
        const post = state.feedCache.find(p => p.post_id === data.post_id);
        if (post) {
          post._comments = post._comments || [];
          post._comments.push(data.comment);
          post.comments = (post.comments || 0) + 1;
          renderFeed();
        }
        return;
      }

      if (data.kind === 'like_update') {
        const post = state.feedCache.find(p => p.post_id === data.post_id);
        if (post) {
          post.likes = data.likes || 0;
          renderFeed();
        }
        return;
      }

      if (data.kind === 'typing') {
        if (data.client_id !== state.localUserId) {
          els.typingLine.textContent = `${data.display_name || 'Someone'} is typing...`;
          clearTimeout(state.typingTimer);
          state.typingTimer = setTimeout(() => els.typingLine.textContent = '', 1200);
        }
        return;
      }

      if (data.kind === 'search_results') {
        if (data.results) {
          upsertFeed(data.results, false);
          renderFeed();
        }
        return;
      }

      if (data.kind === 'feed_data') {
        upsertFeed(data.posts || [], false);
        renderFeed();
        return;
      }
    };
  }

  function postFeed() {
    const text = cleanText(els.composerText.value);
    const mediaUrl = '';
    if (!text && !mediaUrl) return;
    if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return;

    const mediaType = state.mode === 'short' ? 'short' : state.mode === 'video' ? 'video' : state.mode === 'live' ? 'live' : 'text';

    state.socket.send(JSON.stringify({
      kind: 'room_post',
      user_id: state.localUserId,
      text,
      media_url: mediaUrl,
      media_type: mediaType,
      visibility: 'public',
      time: Date.now()
    }));

    els.composerText.value = '';
    state.replyTo = null;
    updateModeLabel();
  }

  function updateModeLabel() {
    els.modeLabel.textContent = state.mode === 'text' ? 'Post mode' : state.mode === 'short' ? 'Short mode' : state.mode === 'video' ? 'Video mode' : 'Live mode';
    els.replyLabel.style.display = state.replyTo ? 'inline-flex' : 'none';
    els.replyLabel.textContent = state.replyTo ? `Replying to ${state.replyTo}` : '';
  }

  function openProfileFor(userId) {
    if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return;
    state.socket.send(JSON.stringify({ kind: 'get_profile', user_id: userId, time: Date.now() }));
  }

  function bindFeedActions() {
    els.feed.querySelectorAll('[data-action]').forEach(btn => {
      btn.onclick = () => {
        const action = btn.dataset.action;
        const postId = btn.dataset.post;
        if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return;

        if (action === 'like') {
          state.socket.send(JSON.stringify({ kind: 'like', post_id: postId, user_id: state.localUserId, time: Date.now() }));
          return;
        }
        if (action === 'comment') {
          const text = prompt('Write a comment:');
          if (!text) return;
          state.socket.send(JSON.stringify({ kind: 'comment', post_id: postId, text, user_id: state.localUserId, time: Date.now() }));
          return;
        }
        if (action === 'repost') {
          state.socket.send(JSON.stringify({ kind: 'repost', post_id: postId, user_id: state.localUserId, time: Date.now() }));
          return;
        }
        if (action === 'bookmark') {
          state.socket.send(JSON.stringify({ kind: 'bookmark', post_id: postId, user_id: state.localUserId, time: Date.now() }));
          if (!state.bookmarks.includes(postId)) state.bookmarks.push(postId); else state.bookmarks = state.bookmarks.filter(id => id !== postId);
          renderBookmarks();
          renderFeed();
          return;
        }
        if (action === 'reply') {
          state.replyTo = postId;
          const post = state.feedCache.find(p => p.post_id === postId);
          els.replyLabel.style.display = 'inline-flex';
          els.replyLabel.textContent = post ? `Replying to ${post.author_name}` : 'Replying';
          els.composerText.focus();
          return;
        }
        if (action === 'pin') {
          if (!state.bookmarks.includes(postId)) state.bookmarks.push(postId);
          else state.bookmarks = state.bookmarks.filter(id => id !== postId);
          renderBookmarks();
          renderFeed();
          return;
        }
      };
    });

    els.onlineList.querySelectorAll('[data-user]').forEach(btn => {
      btn.onclick = () => openProfileFor(btn.dataset.user);
    });
  }

  function bindProfileForm() {
    els.saveProfile.onclick = () => {
      const username = cleanText(els.usernameInput.value);
      const displayName = cleanText(els.displayNameInput.value);
      const avatarUrl = cleanText(els.avatarInput.value);
      const bio = cleanText(els.bioInput.value);

      if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return;
      if (username) state.socket.send(JSON.stringify({ kind: 'set_username', username, display_name: displayName || username, avatar_url: avatarUrl || state.avatarUrl, bio, time: Date.now() }));
      else state.socket.send(JSON.stringify({ kind: 'set_display_name', display_name: displayName || state.displayName || 'User', time: Date.now() }));
      state.socket.send(JSON.stringify({ kind: 'set_avatar', avatar_url: avatarUrl || defaultAvatar(username || displayName || state.username || 'user'), time: Date.now() }));

      state.username = username || state.username;
      state.displayName = displayName || state.displayName;
      state.avatarUrl = avatarUrl || state.avatarUrl;
      state.bio = bio;
      localStorage.setItem('linkup_username', state.username);
      localStorage.setItem('linkup_display_name', state.displayName);
      localStorage.setItem('linkup_avatar', state.avatarUrl);
      localStorage.setItem('linkup_bio', state.bio);
      setProfileFields({ username: state.username, display_name: state.displayName, avatar_url: state.avatarUrl, bio: state.bio });
    };

    els.randomAvatar.onclick = () => {
      const seed = cleanText(els.usernameInput.value || els.displayNameInput.value || state.username || 'user') || 'user';
      els.avatarInput.value = defaultAvatar(seed + '_' + Math.random().toString(36).slice(2, 6));
    };
  }

  function bindComposer() {
    els.postBtn.onclick = postFeed;
    els.composerText.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        postFeed();
      }
    });
    els.composerText.addEventListener('input', () => {
      if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return;
      state.socket.send(JSON.stringify({ kind: 'typing', user_id: state.localUserId, display_name: state.displayName || state.username || 'User', time: Date.now() }));
    });

    els.modePost.onclick = () => { state.mode = 'text'; updateModeLabel(); };
    els.modeClip.onclick = () => { state.mode = 'short'; updateModeLabel(); };
    els.modeVideo.onclick = () => { state.mode = 'video'; updateModeLabel(); };
    els.modeLive.onclick = () => { state.mode = 'live'; updateModeLabel(); };
  }

  function bindSearch() {
    els.searchInput.addEventListener('input', () => {
      state.search = els.searchInput.value;
      if (state.socket && state.socket.readyState === WebSocket.OPEN) {
        state.socket.send(JSON.stringify({ kind: 'search_posts', query: state.search, time: Date.now() }));
      }
      renderFeed();
    });
    els.refreshFeed.onclick = () => {
      if (state.socket && state.socket.readyState === WebSocket.OPEN) {
        state.socket.send(JSON.stringify({ kind: 'get_feed', time: Date.now() }));
      }
    };
  }

  function initLocalProfile() {
    const username = state.username || ('user_' + state.localUserId.slice(-4));
    const displayName = state.displayName || username;
    const avatarUrl = state.avatarUrl || defaultAvatar(username);
    const bio = state.bio || '';
    saveLocalProfile({
      user_id: state.localUserId,
      username,
      display_name: displayName,
      avatar_url: avatarUrl,
      bio,
      followers: 0,
      following: 0,
      posts: 0,
      verified: false,
      created_at: Date.now()
    });
  }

  function bootstrap() {
    state.bookmarks = Array.from(state.bookmarks || []);
    initLocalProfile();
    bindComposer();
    bindSearch();
    bindProfileForm();
    updateModeLabel();
    renderShell();
    connect(true);
  }

  bootstrap();
})();
</script></body>
</html>"""



