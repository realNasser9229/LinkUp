from future import annotations

import json import os import time from collections import defaultdict, deque from dataclasses import dataclass, asdict from typing import Deque, Dict, List, Optional, Set from uuid import uuid4

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect from fastapi.middleware.cors import CORSMiddleware from fastapi.responses import HTMLResponse

app = FastAPI(title="LinkUp")

cors_origins = os.getenv("CORS_ORIGINS", "").strip() if cors_origins == "": allowed_origins = ["*"] else: allowed_origins = [o.strip() for o in cors_origins.split(",") if o.strip()]

app.add_middleware( CORSMiddleware, allow_origins=allowed_origins, allow_credentials=True, allow_methods=[""], allow_headers=[""], )

ws_router = APIRouter()

@dataclass class Profile: user_id: str username: str display_name: str avatar_url: str bio: str = "" followers: int = 0 following: int = 0 posts: int = 0 created_at: int = 0

@dataclass class Post: post_id: str author_id: str author_name: str text: str media_url: str = "" media_type: str = "text" created_at: int = 0 likes: int = 0 comments: int = 0 reposts: int = 0 pinned: bool = False

@dataclass class Comment: comment_id: str post_id: str author_id: str author_name: str text: str created_at: int = 0

class LinkUpState: def init(self) -> None: self.profiles: Dict[str, Profile] = {} self.posts: Deque[Post] = deque(maxlen=2000) self.comments: Dict[str, List[Comment]] = defaultdict(list) self.following: Dict[str, Set[str]] = defaultdict(set) self.followers: Dict[str, Set[str]] = defaultdict(set) self.likes: Dict[str, Set[str]] = defaultdict(set) self.reposts: Dict[str, Set[str]] = defaultdict(set) self.bookmarks: Dict[str, Set[str]] = defaultdict(set) self.room_clients: Dict[str, Dict[str, WebSocket]] = defaultdict(dict) self.room_presence: Dict[str, Dict[str, dict]] = defaultdict(dict) self.room_history: Dict[str, List[dict]] = defaultdict(list) self.room_pins: Dict[str, List[dict]] = defaultdict(list) self.room_last_typing: Dict[str, str] = {}

def now(self) -> int:
    return int(time.time() * 1000)

def safe_username(self, value: str) -> str:
    value = (value or "").strip().lower()
    value = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "."})
    return value[:20]

def avatar_for(self, username: str) -> str:
    seed = (username or "user").strip().lower() or "user"
    return f"https://api.dicebear.com/8.x/thumbs/svg?seed={seed}"

def ensure_profile(self, user_id: str, username: Optional[str] = None) -> Profile:
    if user_id not in self.profiles:
        uname = self.safe_username(username or f"user_{user_id[:6]}") or f"user_{user_id[:6]}"
        self.profiles[user_id] = Profile(
            user_id=user_id,
            username=uname,
            display_name=uname,
            avatar_url=self.avatar_for(uname),
            created_at=self.now(),
        )
    return self.profiles[user_id]

def set_username(self, user_id: str, username: str) -> Profile:
    profile = self.ensure_profile(user_id)
    clean = self.safe_username(username) or profile.username
    profile.username = clean
    if not profile.display_name:
        profile.display_name = clean
    if not profile.avatar_url:
        profile.avatar_url = self.avatar_for(clean)
    return profile

def set_display_name(self, user_id: str, display_name: str) -> Profile:
    profile = self.ensure_profile(user_id)
    profile.display_name = (display_name or profile.username)[:32]
    return profile

def set_avatar(self, user_id: str, avatar_url: str) -> Profile:
    profile = self.ensure_profile(user_id)
    profile.avatar_url = (avatar_url or "").strip() or self.avatar_for(profile.username)
    return profile

def set_bio(self, user_id: str, bio: str) -> Profile:
    profile = self.ensure_profile(user_id)
    profile.bio = (bio or "")[:220]
    return profile

def add_post(self, author_id: str, text: str, media_url: str = "", media_type: str = "text") -> Post:
    profile = self.ensure_profile(author_id)
    post = Post(
        post_id="p_" + uuid4().hex[:12],
        author_id=author_id,
        author_name=profile.display_name or profile.username,
        text=(text or "").strip(),
        media_url=(media_url or "").strip(),
        media_type=media_type,
        created_at=self.now(),
    )
    self.posts.appendleft(post)
    profile.posts += 1
    return post

def get_post(self, post_id: str) -> Optional[Post]:
    for post in self.posts:
        if post.post_id == post_id:
            return post
    return None

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
    if original:
        original.reposts = len(self.reposts[post_id])
    return self.add_post(user_id, f"Reposted: {original.text}", original.media_url, original.media_type)

def bookmark_post(self, user_id: str, post_id: str) -> None:
    if post_id in self.bookmarks[user_id]:
        self.bookmarks[user_id].remove(post_id)
    else:
        self.bookmarks[user_id].add(post_id)

def feed(self, limit: int = 100) -> List[dict]:
    out: List[dict] = []
    for post in list(self.posts)[:limit]:
        item = asdict(post)
        item["comments_list"] = [asdict(c) for c in self.comments.get(post.post_id, [])[-3:]]
        out.append(item)
    return out

def profile_data(self, user_id: str) -> dict:
    prof = self.ensure_profile(user_id)
    return {
        "profile": asdict(prof),
        "posts": [asdict(p) | {"comments_list": [asdict(c) for c in self.comments.get(p.post_id, [])[-3:]]} for p in self.posts if p.author_id == user_id],
        "followers": list(self.followers[user_id]),
        "following": list(self.following[user_id]),
        "bookmarks": list(self.bookmarks[user_id]),
    }

def search(self, query: str) -> List[dict]:
    q = (query or "").strip().lower()
    if not q:
        return self.feed()
    out: List[dict] = []
    for post in self.posts:
        if q in post.text.lower() or q in post.author_name.lower():
            item = asdict(post)
            item["comments_list"] = [asdict(c) for c in self.comments.get(post.post_id, [])[-3:]]
            out.append(item)
    return out[:100]

async def broadcast(self, room_id: str, payload: dict) -> None:
    dead: List[str] = []
    for client_id, ws in self.room_clients[room_id].items():
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(client_id)
    for cid in dead:
        self.room_clients[room_id].pop(cid, None)
        self.room_presence[room_id].pop(cid, None)

async def push_presence(self, room_id: str) -> None:
    await self.broadcast(room_id, {
        "kind": "presence",
        "room": room_id,
        "users": list(self.room_presence[room_id].values()),
        "time": self.now(),
    })

async def push_history(self, room_id: str, ws: WebSocket) -> None:
    await ws.send_json({
        "kind": "history",
        "room": room_id,
        "feed": self.feed(50),
        "users": list(self.room_presence[room_id].values()),
        "pins": self.room_pins[room_id][-30:],
        "time": self.now(),
    })

state = LinkUpState()

Seed content

for uid, uname in [("u_1", "nas9229alt"), ("u_2", "pixelwave"), ("u_3", "neonbyte")]: p = state.ensure_profile(uid, uname) p.display_name = uname

state.add_post("u_1", "LinkUp is live. Post, clip, reply, like, repost, bookmark.") state.add_post("u_2", "Short-form content fits here too. Think TikTok-style posts.", media_type="short") state.add_post("u_3", "Long-form creators can live here as well. Think YouTube energy.", media_type="video")

@ws_router.websocket("/{room_id}") async def room_socket(websocket: WebSocket, room_id: str): await websocket.accept()

client_id = "u_" + uuid4().hex[:10]
profile = state.ensure_profile(client_id)
state.room_clients[room_id][client_id] = websocket
state.room_presence[room_id][client_id] = {
    "client_id": client_id,
    "username": profile.username,
    "display_name": profile.display_name,
    "avatar_url": profile.avatar_url,
    "bio": profile.bio,
    "online": True,
    "joined_at": state.now(),
}

await websocket.send_json({
    "kind": "init",
    "you": client_id,
    "profile": asdict(profile),
    "feed": state.feed(50),
    "users": list(state.room_presence[room_id].values()),
    "pins": state.room_pins[room_id],
    "time": state.now(),
})
await state.push_presence(room_id)

try:
    while True:
        raw = await websocket.receive_text()
        try:
            data = json.loads(raw)
        except Exception:
            data = {}

        kind = data.get("kind", "")

        if kind == "set_username":
            profile = state.set_username(client_id, data.get("username", profile.username))
            profile = state.set_display_name(client_id, data.get("display_name", profile.display_name))
            state.room_presence[room_id][client_id].update({
                "username": profile.username,
                "display_name": profile.display_name,
                "profile": asdict(profile),
            })
            await state.broadcast(room_id, {
                "kind": "profile_update",
                "client_id": client_id,
                "profile": asdict(profile),
                "time": state.now(),
            })
            continue

        if kind == "set_display_name":
            profile = state.set_display_name(client_id, data.get("display_name", profile.display_name))
            state.room_presence[room_id][client_id]["display_name"] = profile.display_name
            await state.broadcast(room_id, {
                "kind": "profile_update",
                "client_id": client_id,
                "profile": asdict(profile),
                "time": state.now(),
            })
            continue

        if kind == "set_avatar":
            profile = state.set_avatar(client_id, data.get("avatar_url", profile.avatar_url))
            state.room_presence[room_id][client_id]["avatar_url"] = profile.avatar_url
            await state.broadcast(room_id, {
                "kind": "profile_update",
                "client_id": client_id,
                "profile": asdict(profile),
                "time": state.now(),
            })
            continue

        if kind == "set_bio":
            profile = state.set_bio(client_id, data.get("bio", profile.bio))
            state.room_presence[room_id][client_id]["bio"] = profile.bio
            await state.broadcast(room_id, {
                "kind": "profile_update",
                "client_id": client_id,
                "profile": asdict(profile),
                "time": state.now(),
            })
            continue

        if kind == "typing":
            await state.broadcast(room_id, {
                "kind": "typing",
                "client_id": client_id,
                "display_name": profile.display_name,
                "time": state.now(),
            })
            continue

        if kind == "get_feed":
            await websocket.send_json({"kind": "feed_data", "feed": state.feed(50), "time": state.now()})
            continue

        if kind == "search_posts":
            q = data.get("query", "")
            await websocket.send_json({"kind": "search_results", "query": q, "feed": state.search(q), "time": state.now()})
            continue

        if kind == "get_profile":
            uid = data.get("user_id") or client_id
            await websocket.send_json({"kind": "profile_data", **state.profile_data(uid), "time": state.now()})
            continue

        if kind == "follow":
            target = data.get("target_user_id", "")
            if target and target != client_id:
                state.following[client_id].add(target)
                state.followers[target].add(client_id)
                await websocket.send_json({"kind": "follow_result", "ok": True, "time": state.now()})
            continue

        if kind == "unfollow":
            target = data.get("target_user_id", "")
            if target:
                state.following[client_id].discard(target)
                state.followers[target].discard(client_id)
                await websocket.send_json({"kind": "follow_result", "ok": True, "time": state.now()})
            continue

        if kind == "room_post":
            text = (data.get("text") or "").strip()
            media_url = (data.get("media_url") or "").strip()
            media_type = (data.get("media_type") or "text").strip()
            if not text and not media_url:
                continue
            post = state.add_post(client_id, text, media_url=media_url, media_type=media_type)
            await state.broadcast(room_id, {"kind": "feed_post", "post": asdict(post), "time": state.now()})
            continue

        if kind == "comment":
            post_id = data.get("post_id", "")
            text = (data.get("text") or "").strip()
            comment = state.add_comment(post_id, client_id, text)
            if comment:
                post = state.get_post(post_id)
                await state.broadcast(room_id, {
                    "kind": "comment_added",
                    "post_id": post_id,
                    "comment": asdict(comment),
                    "post": asdict(post) if post else None,
                    "time": state.now(),
                })
            continue

        if kind == "like":
            post_id = data.get("post_id", "")
            count = state.like_post(client_id, post_id)
            post = state.get_post(post_id)
            await state.broadcast(room_id, {
                "kind": "like_update",
                "post_id": post_id,
                "likes": count,
                "post": asdict(post) if post else None,
                "time": state.now(),
            })
            continue

        if kind == "repost":
            post_id = data.get("post_id", "")
            new_post = state.repost(client_id, post_id)
            if new_post:
                await state.broadcast(room_id, {"kind": "feed_post", "post": asdict(new_post), "repost_of": post_id, "time": state.now()})
            continue

        if kind == "bookmark":
            post_id = data.get("post_id", "")
            if post_id:
                state.bookmark_post(client_id, post_id)
                await websocket.send_json({"kind": "bookmark_result", "ok": True, "time": state.now()})
            continue

        if kind == "pin":
            post_id = data.get("post_id", "")
            post = state.get_post(post_id)
            if post:
                pinned = next((p for p in state.room_pins[room_id] if p["post_id"] == post_id), None)
                if pinned:
                    state.room_pins[room_id] = [p for p in state.room_pins[room_id] if p["post_id"] != post_id]
                else:
                    state.room_pins[room_id].append({
                        "post_id": post.post_id,
                        "author_name": post.author_name,
                        "text": post.text,
                        "time": post.created_at,
                    })
                await state.broadcast(room_id, {"kind": "pins_update", "pins": state.room_pins[room_id], "time": state.now()})
            continue

        if kind == "ping":
            await websocket.send_json({"kind": "pong", "time": state.now()})
            continue

except WebSocketDisconnect:
    state.room_clients[room_id].pop(client_id, None)
    state.room_presence[room_id].pop(client_id, None)
    await state.push_presence(room_id)

@app.get("/health") def health(): return {"ok": True, "app": "LinkUp"}

@app.get("/api/feed") def api_feed(limit: int = 50): return {"feed": state.feed(limit)}

@app.get("/api/profile/{user_id}") def api_profile(user_id: str): return state.profile_data(user_id)

@app.get("/", response_class=HTMLResponse) def home(): return HTMLResponse(_html())

@app.get("/{path:path}", response_class=HTMLResponse) def spa(path: str): return HTMLResponse(_html())

def _html() -> str: return """<!doctype html>

<html lang='en'>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1' />
  <title>LinkUp</title>
  <style>
    :root{--bg:#313338;--side:#2b2d31;--side2:#1e1f22;--text:#f2f3f5;--muted:#b5bac1;--line:rgba(255,255,255,.06);--accent:#5865f2;--green:#3ba55d;}
    *{box-sizing:border-box}
    html,body{height:100%} body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif;overflow:hidden}
    button,input,textarea{font:inherit}
    .app{height:100vh;display:grid;grid-template-columns:72px 240px minmax(0,1fr) 280px}
    .servers{background:var(--side2);border-right:1px solid var(--line);overflow-y:auto;padding:10px 0;display:flex;flex-direction:column;align-items:center;gap:10px}
    .srv{width:48px;height:48px;border:none;border-radius:16px;background:#383a40;color:var(--text);font-weight:800;cursor:pointer}.srv.active{background:var(--accent)}
    .sidebar{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-width:0;overflow:hidden}
    .head{padding:14px;border-bottom:1px solid var(--line)} .head h1{margin:0;font-size:17px} .head small{display:block;color:var(--muted);font-size:12px;margin-top:4px}
    .pill{display:inline-flex;margin-top:10px;padding:7px 10px;border-radius:999px;background:rgba(255,255,255,.05);color:var(--muted);font-size:12px}
    .channels{padding:8px;overflow-y:auto;min-height:0;flex:1}.sec{margin:8px 8px 10px;color:#969aa3;font-size:11px;letter-spacing:.12em;text-transform:uppercase}
    .channel{display:flex;justify-content:space-between;align-items:center;width:100%;border:none;background:transparent;color:var(--text);padding:10px;border-radius:8px;cursor:pointer;text-align:left}.channel:hover{background:rgba(255,255,255,.04)}.channel.active{background:rgba(255,255,255,.08)}
    .channel .meta{min-width:0;display:flex;flex-direction:column}.channel .meta strong{font-size:14px}.channel .meta span{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px}
    .main{display:flex;flex-direction:column;overflow:hidden;min-width:0;background:#313338}
    .top{height:54px;display:flex;justify-content:space-between;align-items:center;gap:10px;padding:0 16px;border-bottom:1px solid var(--line);background:rgba(49,51,56,.94)}
    .top h2{margin:0;font-size:16px}.top p{margin:2px 0 0;color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:45vw}
    .status{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}.dot{width:10px;height:10px;border-radius:999px;background:var(--green)}
    .meta{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.02)}
    .stats{display:flex;gap:8px;flex-wrap:wrap}.stat{padding:7px 10px;border-radius:999px;background:rgba(255,255,255,.05);color:var(--muted);font-size:12px;white-space:nowrap}
    .search{width:min(420px,42vw);padding:10px 12px;border:none;outline:none;border-radius:10px;background:#1f242f;color:var(--text)}
    .feed{flex:1;min-height:0;overflow-y:auto;padding:18px 0 120px}
    .inner{width:min(920px,calc(100vw - 32px));margin:0 auto}
    .post{background:#2b2d31;border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:12px}
    .post-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.author{display:flex;gap:10px;align-items:center;min-width:0}.avatar{width:44px;height:44px;border-radius:50%;background:#272c37;object-fit:cover;flex:0 0 auto}
    .author strong{display:block;font-size:14px}.author span{display:block;font-size:12px;color:var(--muted)}
    .post-text{margin-top:10px;line-height:1.6;white-space:pre-wrap;word-break:break-word}.media{margin-top:12px;border-radius:14px;overflow:hidden;background:#000}.media img,.media video{width:100%;display:block;max-height:520px;object-fit:cover}
    .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.tiny{border:none;padding:8px 10px;border-radius:999px;background:#262b35;color:var(--text);cursor:pointer;font-size:12px}.tiny:hover{background:#303646}
    .comments{margin-top:12px;display:flex;flex-direction:column;gap:8px}.comment{padding:10px 12px;border-radius:12px;background:rgba(255,255,255,.03);border:1px solid var(--line)}.comment .who{font-weight:700;font-size:13px;margin-bottom:4px}
    .typing{height:18px;padding:0 16px 8px;color:#c9ced8;font-size:12px}
    .composer-wrap{position:sticky;bottom:0;background:linear-gradient(180deg,rgba(49,51,56,0),rgba(49,51,56,.9) 20%,rgba(49,51,56,1));padding:14px 0 18px}.composer{width:min(920px,calc(100vw - 32px));margin:0 auto;background:#383a40;border:1px solid var(--line);border-radius:16px;padding:14px}
    .composer-top{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}.namepill{padding:7px 10px;border-radius:999px;background:rgba(255,255,255,.05);color:var(--muted);font-size:12px}
    .composer textarea{width:100%;min-height:80px;max-height:180px;resize:none;overflow-y:auto;border:none;outline:none;background:transparent;color:var(--text);line-height:1.6;font-size:15px}
    .composer-actions{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.btn{border:none;border-radius:10px;background:#262b35;color:var(--text);padding:10px 12px;cursor:pointer}.btn.primary{background:var(--accent)}
    .right{background:var(--side);border-left:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden;min-width:0}.right-head{padding:14px;border-bottom:1px solid var(--line)}.profile{display:flex;gap:12px;align-items:center}.profile-pic{width:56px;height:56px;border-radius:50%;object-fit:cover;background:#272c37}.profile h3{margin:0;font-size:16px}.profile p{margin:4px 0 0;color:var(--muted);font-size:12px;line-height:1.5}
    .right-body{overflow-y:auto;min-height:0;padding:10px}.box{background:#23262d;border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px}.field{display:flex;flex-direction:column;gap:6px;margin-top:10px}.field label{font-size:12px;color:#c2c8d4}.field input,.field textarea{width:100%;padding:10px 12px;border-radius:10px;border:none;outline:none;background:#1f242f;color:var(--text)}.list{display:flex;flex-direction:column;gap:8px}.item{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;border-radius:12px;background:rgba(255,255,255,.04)}
    @media (max-width: 1200px){.app{grid-template-columns:72px 240px minmax(0,1fr)}.right{display:none}.search{width:40vw}}
    @media (max-width: 820px){body{overflow:auto}.app{height:auto;min-height:100vh;grid-template-columns:1fr}.servers{flex-direction:row;overflow-x:auto;overflow-y:hidden;padding:10px}.sidebar{min-height:220px}.right{display:block;min-height:240px}.top,.meta{flex-direction:column;align-items:stretch}.search{width:100%}.inner,.composer{width:calc(100vw - 20px)}}
  </style>
</head>
<body>
  <div class='app'>
    <aside class='servers' id='servers'></aside>
    <aside class='sidebar'>
      <div class='head'>
        <h1>LinkUp</h1>
        <small>social feed + short clips + creators</small>
        <div class='pill'>Room <span id='roomLabel'>#global</span></div>
      </div>
      <div class='channels'><div class='sec'>Spaces</div><div id='channels'></div></div>
    </aside><main class='main'>
  <div class='top'><div><h2 id='channelTitle'>Global Feed</h2><p id='channelDescription'>Post updates, clips, and creator content.</p></div><div class='status'><span class='dot'></span><span id='connState'>connecting...</span></div></div>
  <div class='meta'><div class='stats'><div class='stat' id='statPosts'>0 posts</div><div class='stat' id='statUsers'>0 online</div><div class='stat' id='statFeed'>0 items</div></div><input class='search' id='searchInput' placeholder='Search posts, people, clips...' /></div>
  <div class='typing' id='typingLine'></div>
  <div class='feed'><div class='inner' id='feed'></div></div>
  <div class='composer-wrap'><div class='composer'>
    <div class='composer-top'><span class='namepill' id='youName'>You</span><span class='namepill' id='modeLabel'>Post mode</span><span class='namepill' id='replyLabel' style='display:none'></span></div>
    <textarea id='composerText' placeholder='What’s happening on LinkUp?'></textarea>
    <div class='composer-actions'><div class='row'><button class='btn' id='modePost'>Post</button><button class='btn' id='modeClip'>Short</button><button class='btn' id='modeVideo'>Video</button><button class='btn' id='modeLive'>Live</button></div><div class='row'><button class='btn primary' id='postBtn'>Publish</button></div></div>
  </div></div>
</main>

<aside class='right'>
  <div class='right-head'><div class='profile'><img class='profile-pic' id='profilePic' alt='avatar'/><div><h3 id='profileName'>Your profile</h3><p id='profileBio'>Edit your name, avatar, and bio anytime.</p></div></div></div>
  <div class='right-body'>
    <div class='box'>
      <div class='field'><label>Username</label><input id='usernameInput' placeholder='username'/></div>
      <div class='field'><label>Display name</label><input id='displayNameInput' placeholder='display name'/></div>
      <div class='field'><label>Avatar URL</label><input id='avatarInput' placeholder='https://...'/></div>
      <div class='field'><label>Bio</label><textarea id='bioInput' placeholder='Write something cool...'></textarea></div>
      <div class='row' style='margin-top:10px'><button class='btn primary' id='saveProfile'>Save profile</button><button class='btn' id='randomAvatar'>Random avatar</button></div>
    </div>
    <div class='box'><div class='sec'>Online now</div><div class='list' id='onlineList'></div></div>
    <div class='box'><div class='sec'>Bookmarks</div><div class='list' id='bookmarksList'></div></div>
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
    profile: null,
    search: '',
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

  function roomKey() { return `${state.server}:${state.channel}`; }
  function clean(v) { return String(v || '').trim(); }
  function escapeHtml(str) { return String(str).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[ch])); }
  function timeText(ts) { return new Date(ts || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
  function setStatus(text, ok = false) { els.connState.textContent = text; els.connState.style.color = ok ? '#d7ffe7' : ''; }

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
    els.servers.innerHTML = servers.map(s => `<button class='srv ${s.id === state.server ? 'active' : ''}' data-server='${s.id}' title='${escapeHtml(s.label)}'>${escapeHtml(s.name)}</button>`).join('');
    els.servers.querySelectorAll('button').forEach(btn => btn.onclick = () => {
      const next = btn.dataset.server;
      if (next === state.server) return;
      state.server = next;
      state.channel = channelsByServer[next][0].id;
      localStorage.setItem('linkup_server', state.server);
      localStorage.setItem('linkup_channel', state.channel);
      connect(true);
      renderShell();
    });
  }

  function renderChannels() {
    els.channels.innerHTML = (channelsByServer[state.server] || []).map(ch => `<button class='channel ${ch.id === state.channel ? 'active' : ''}' data-channel='${ch.id}'><div class='meta'><strong>${escapeHtml(ch.title)}</strong><span>${escapeHtml(ch.description)}</span></div><span class='badge'>#</span></button>`).join('');
    els.channels.querySelectorAll('button').forEach(btn => btn.onclick = () => {
      const next = btn.dataset.channel;
      if (next === state.channel) return;
      state.channel = next;
      localStorage.setItem('linkup_channel', state.channel);
      connect(true);
      renderShell();
    });
  }

  function renderTop() {
    const ch = (channelsByServer[state.server] || []).find(x => x.id === state.channel) || channelsByServer[state.server][0];
    els.roomLabel.textContent = `#${state.channel}`;
    els.channelTitle.textContent = ch?.title || 'Global Feed';
    els.channelDescription.textContent = ch?.description || 'Post updates, clips, and creator content.';
  }

  function normalizePost(post) {
    if (!post) return post;
    post.comments_list = post.comments_list || [];
    return post;
  }

  function upsertFeed(feed) {
    const map = new Map(state.feedCache.map(p => [p.post_id, p]));
    for (const raw of (feed || [])) {
      map.set(raw.post_id, normalizePost(raw));
    }
    state.feedCache = Array.from(map.values()).sort((a, b) => Number(b.created_at) - Number(a.created_at));
    els.statPosts.textContent = `${state.feedCache.length} posts`;
  }

  function renderOnline(users) {
    state.online = users || [];
    els.statUsers.textContent = `${state.online.length} online`;
    els.onlineList.innerHTML = state.online.length ? state.online.map(u => `<div class='item'><div><strong>${escapeHtml(u.display_name || u.username || 'user')}</strong><br><span>@${escapeHtml(u.username || 'user')}</span></div><button class='tiny' data-user='${escapeHtml(u.client_id)}'>Open</button></div>`).join('') : `<div class='item'><div><strong>No one yet</strong><br><span>Be the first online.</span></div></div>`;
    els.onlineList.querySelectorAll('[data-user]').forEach(btn => btn.onclick = () => {
      if (state.socket && state.socket.readyState === WebSocket.OPEN) {
        state.socket.send(JSON.stringify({ kind: 'get_profile', user_id: btn.dataset.user, time: Date.now() }));
      }
    });
  }

  function renderBookmarks() {
    const items = (state.bookmarks || []).map(id => state.feedCache.find(p => p.post_id === id)).filter(Boolean);
    els.bookmarksList.innerHTML = items.length ? items.map(p => `<div class='item'><div><strong>${escapeHtml(p.author_name)}</strong><br><span>${escapeHtml((p.text || '').slice(0, 65))}</span></div><button class='tiny' data-jump='${escapeHtml(p.post_id)}'>Jump</button></div>`).join('') : `<div class='item'><div><strong>No bookmarks</strong><br><span>Save posts here.</span></div></div>`;
    els.bookmarksList.querySelectorAll('[data-jump]').forEach(btn => btn.onclick = () => {
      const target = els.feed.querySelector(`[data-post='${CSS.escape(btn.dataset.jump)}']`);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }

  function postHtml(post) {
    const comments = (post.comments_list || []).slice(-3).map(c => `<div class='comment'><div class='who'>${escapeHtml(c.author_name)}</div><div>${escapeHtml(c.text)}</div></div>`).join('');
    const media = post.media_url ? (post.media_type === 'video' || post.media_type === 'short' ? `<div class='media'><video controls playsinline src='${escapeHtml(post.media_url)}'></video></div>` : `<div class='media'><img src='${escapeHtml(post.media_url)}' alt='media'/></div>`) : '';
    return `<article class='post' data-post='${escapeHtml(post.post_id)}'><div class='post-head'><div class='author'><img class='avatar' src='${escapeHtml(post._avatar || defaultAvatar(post.author_name))}' alt='avatar'/><div><strong>${escapeHtml(post.author_name)}</strong><span>${timeText(post.created_at)}</span></div></div><button class='tiny' data-action='bookmark' data-post='${escapeHtml(post.post_id)}'>Bookmark</button></div><div class='post-text'>${escapeHtml(post.text || '')}</div>${media}<div class='actions'><button class='tiny' data-action='like' data-post='${escapeHtml(post.post_id)}'>Like · ${post.likes || 0}</button><button class='tiny' data-action='comment' data-post='${escapeHtml(post.post_id)}'>Comment · ${post.comments || 0}</button><button class='tiny' data-action='repost' data-post='${escapeHtml(post.post_id)}'>Repost · ${post.reposts || 0}</button><button class='tiny' data-action='reply' data-post='${escapeHtml(post.post_id)}'>Reply</button><button class='tiny' data-action='pin' data-post='${escapeHtml(post.post_id)}'>Pin</button></div>${comments ? `<div class='comments'>${comments}</div>` : ''}</article>`;
  }

  function renderFeed() {
    const q = clean(state.search).toLowerCase();
    const items = q ? state.feedCache.filter(p => String(p.text || '').toLowerCase().includes(q) || String(p.author_name || '').toLowerCase().includes(q)) : state.feedCache;
    els.feed.innerHTML = items.length ? items.map(postHtml).join('') : `<div class='post'><div class='muted'>No posts yet.</div></div>`;
    els.statFeed.textContent = `${items.length} items`;
    bindFeedActions();
  }

  function renderShell() {
    renderServers();
    renderChannels();
    renderTop();
    setProfileFields(state.profile || { username: state.username, display_name: state.displayName, avatar_url: state.avatarUrl, bio: state.bio });
    renderOnline(state.online);
    renderBookmarks();
    renderFeed();
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
    state.reconnectTimer = setTimeout(() => { if (seq === state.seq) connect(false); }, delay);
    setStatus(`reconnecting in ${Math.ceil(delay / 1000)}s`);
  }

  function connect(force = false) {
    cleanupTimers();
    const seq = ++state.seq;
    const room = roomKey();
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

    state.handshakeTimer = setTimeout(() => { if (seq === state.seq && !opened) setStatus('connecting... still trying'); }, 3500);

    state.socket.onopen = () => {
      if (seq !== state.seq) return;
      opened = true;
      state.retryCount = 0;
      cleanupTimers();
      setStatus('connected', true);
      state.socket.send(JSON.stringify({ kind: 'hello', user_id: state.localUserId, username: state.username, display_name: state.displayName, avatar_url: state.avatarUrl, bio: state.bio, room, time: Date.now() }));
    };

    state.socket.onclose = () => {
      if (seq !== state.seq) return;
      cleanupTimers();
      setStatus(opened ? 'disconnected' : 'offline');
      scheduleReconnect(seq);
    };

    state.socket.onerror = () => { if (seq === state.seq && !opened) setStatus('connection error'); };

    state.socket.onmessage = (event) => {
      if (seq !== state.seq) return;
      let data = null;
      try { data = JSON.parse(event.data); } catch { return; }

      if (data.kind === 'init') {
        if (data.profile) saveLocalProfile(data.profile);
        upsertFeed(data.feed || []);
        renderOnline(data.users || []);
        state.bookmarks = state.bookmarks || [];
        if (data.pins) state.bookmarks = state.bookmarks; // UI only for now
        renderShell();
        setStatus('connected', true);
        return;
      }

      if (data.kind === 'presence') { renderOnline(data.users || []); return; }
      if (data.kind === 'profile_update') { if (data.client_id === state.localUserId && data.profile) saveLocalProfile(data.profile); renderOnline(state.online.map(u => u.client_id === data.client_id ? { ...u, ...data.profile, profile: data.profile } : u)); renderShell(); return; }
      if (data.kind === 'feed_data') { upsertFeed(data.feed || []); renderFeed(); return; }
      if (data.kind === 'search_results') { upsertFeed(data.feed || []); renderFeed(); return; }
      if (data.kind === 'feed_post') { upsertFeed([data.post]); renderFeed(); return; }
      if (data.kind === 'comment_added') { const p = state.feedCache.find(x => x.post_id === data.post_id); if (p) { p.comments_list = p.comments_list || []; p.comments_list.push(data.comment); p.comments = (p.comments || 0) + 1; } renderFeed(); return; }
      if (data.kind === 'like_update') { const p = state.feedCache.find(x => x.post_id === data.post_id); if (p) p.likes = data.likes || 0; renderFeed(); return; }
      if (data.kind === 'pins_update') { renderShell(); return; }
      if (data.kind === 'typing') { if (data.client_id !== state.localUserId) { els.typingLine.textContent = `${data.display_name || 'Someone'} is typing...`; clearTimeout(state.typingTimer); state.typingTimer = setTimeout(() => els.typingLine.textContent = '', 1200); } return; }
    };
  }

  function bindFeedActions() {
    els.feed.querySelectorAll('[data-action]').forEach(btn => {
      btn.onclick = () => {
        if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return;
        const action = btn.dataset.action;
        const postId = btn.dataset.post;
        if (action === 'like') state.socket.send(JSON.stringify({ kind: 'like', post_id: postId, user_id: state.localUserId, time: Date.now() }));
        if (action === 'comment') {
          const text = prompt('Write a comment:');
          if (text) state.socket.send(JSON.stringify({ kind: 'comment', post_id: postId, text, user_id: state.localUserId, time: Date.now() }));
        }
        if (action === 'repost') state.socket.send(JSON.stringify({ kind: 'repost', post_id: postId, user_id: state.localUserId, time: Date.now() }));
        if (action === 'bookmark') {
          state.socket.send(JSON.stringify({ kind: 'bookmark', post_id: postId, user_id: state.localUserId, time: Date.now() }));
          if (!state.bookmarks.includes(postId)) state.bookmarks.push(postId); else state.bookmarks = state.bookmarks.filter(x => x !== postId);
          renderBookmarks();
          renderFeed();
        }
        if (action === 'reply') {
          state.replyTo = postId;
          const post = state.feedCache.find(p => p.post_id === postId);
          els.replyLabel.style.display = 'inline-flex';
          els.replyLabel.textContent = post ? `Replying to ${post.author_name}` : 'Replying';
          els.composerText.focus();
        }
        if (action === 'pin') state.socket.send(JSON.stringify({ kind: 'pin', post_id: postId, user_id: state.localUserId, time: Date.now() }));
      };
    });
  }

  function bindProfileForm() {
    els.saveProfile.onclick = () => {
      if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return;
      const username = clean(els.usernameInput.value);
      const displayName = clean(els.displayNameInput.value);
      const avatarUrl = clean(els.avatarInput.value);
      const bio = clean(els.bioInput.value);
      if (username) state.socket.send(JSON.stringify({ kind: 'set_username', username, display_name: displayName || username, time: Date.now() }));
      if (displayName) state.socket.send(JSON.stringify({ kind: 'set_display_name', display_name: displayName, time: Date.now() }));
      if (avatarUrl) state.socket.send(JSON.stringify({ kind: 'set_avatar', avatar_url: avatarUrl, time: Date.now() }));
      state.socket.send(JSON.stringify({ kind: 'set_bio', bio, time: Date.now() }));
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
    els.randomAvatar.onclick = () => { els.avatarInput.value = defaultAvatar((els.usernameInput.value || els.displayNameInput.value || state.username || 'user') + '_' + Math.random().toString(36).slice(2, 6)); };
  }

  function bindComposer() {
    els.postBtn.onclick = () => {
      const text = clean(els.composerText.value);
      if (!text || !state.socket || state.socket.readyState !== WebSocket.OPEN) return;
      const mediaType = state.mode === 'short' ? 'short' : state.mode === 'video' ? 'video' : state.mode === 'live' ? 'live' : 'text';
      state.socket.send(JSON.stringify({ kind: 'room_post', user_id: state.localUserId, text, media_type: mediaType, visibility: 'public', time: Date.now() }));
      els.composerText.value = '';
      state.replyTo = null;
      els.replyLabel.style.display = 'none';
    };
    els.composerText.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); els.postBtn.click(); }
    });
    els.composerText.addEventListener('input', () => {
      if (state.socket && state.socket.readyState === WebSocket.OPEN) state.socket.send(JSON.stringify({ kind: 'typing', display_name: state.displayName || state.username || 'User', time: Date.now() }));
    });
    els.modePost.onclick = () => { state.mode = 'text'; els.modeLabel.textContent = 'Post mode'; };
    els.modeClip.onclick = () => { state.mode = 'short'; els.modeLabel.textContent = 'Short mode'; };
    els.modeVideo.onclick = () => { state.mode = 'video'; els.modeLabel.textContent = 'Video mode'; };
    els.modeLive.onclick = () => { state.mode = 'live'; els.modeLabel.textContent = 'Live mode'; };
  }

  function bindSearch() {
    els.searchInput.addEventListener('input', () => {
      state.search = els.searchInput.value;
      renderFeed();
      if (state.socket && state.socket.readyState === WebSocket.OPEN) state.socket.send(JSON.stringify({ kind: 'search_posts', query: state.search, time: Date.now() }));
    });
  }

  function initLocalProfile() {
    const username = state.username || ('user_' + state.localUserId.slice(-4));
    const displayName = state.displayName || username;
    const avatarUrl = state.avatarUrl || defaultAvatar(username);
    saveLocalProfile({ user_id: state.localUserId, username, display_name: displayName, avatar_url: avatarUrl, bio: state.bio || '', followers: 0, following: 0, posts: 0, created_at: Date.now() });
  }

  function bootstrap() {
    initLocalProfile();
    bindComposer();
    bindSearch();
    bindProfileForm();
    renderShell();
    setStatus('connecting...');
    connect(true);
  }

  bootstrap();
})();
</script></body>
</html>"""



