from __future__ import annotations

import json
import os
import time
from typing import Dict, List
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
        self.names: Dict[str, Dict[str, str]] = {}
        self.history: Dict[str, List[dict]] = {}

    def ensure(self, room: str) -> None:
        self.sockets.setdefault(room, {})
        self.names.setdefault(room, {})
        self.history.setdefault(room, [])

    def presence(self, room: str) -> List[dict]:
        self.ensure(room)
        users = [
            {"client_id": client_id, "name": name, "status": "online"}
            for client_id, name in self.names[room].items()
        ]
        users.sort(key=lambda x: x["name"].lower())
        return users

    async def send(self, ws: WebSocket, payload: dict) -> None:
        await ws.send_json(payload)

    async def broadcast(self, room: str, payload: dict) -> None:
        self.ensure(room)
        dead: List[str] = []
        for client_id, ws in self.sockets[room].items():
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(client_id)

        for client_id in dead:
            self.sockets[room].pop(client_id, None)
            self.names[room].pop(client_id, None)

    async def send_presence(self, room: str) -> None:
        await self.broadcast(
            room,
            {
                "kind": "presence",
                "room": room,
                "users": self.presence(room),
                "time": int(time.time() * 1000),
            },
        )

    async def send_history(self, room: str, ws: WebSocket) -> None:
        self.ensure(room)
        await self.send(
            ws,
            {
                "kind": "history",
                "room": room,
                "messages": self.history[room][-100:],
                "users": self.presence(room),
                "time": int(time.time() * 1000),
            },
        )

    def register(self, room: str, client_id: str, name: str, ws: WebSocket) -> None:
        self.ensure(room)
        self.sockets[room][client_id] = ws
        self.names[room][client_id] = name

    def rename(self, room: str, client_id: str, name: str) -> None:
        self.ensure(room)
        if client_id in self.names[room]:
            self.names[room][client_id] = name

    def remove(self, room: str, client_id: str) -> None:
        self.ensure(room)
        self.sockets[room].pop(client_id, None)
        self.names[room].pop(client_id, None)

    def push_history(self, room: str, payload: dict) -> None:
        self.ensure(room)
        self.history[room].append(payload)
        if len(self.history[room]) > 250:
            self.history[room] = self.history[room][-250:]


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

            kind = data.get("kind", "message")
            incoming_client_id = str(data.get("client_id") or client_id)
            incoming_name = str(data.get("name") or display_name).strip()[:24] or "Guest"

            if kind == "hello":
                client_id = incoming_client_id
                display_name = incoming_name
                rooms.register(room_id, client_id, display_name, websocket)
                await rooms.send_history(room_id, websocket)
                await rooms.send_presence(room_id)
                continue

            if kind == "rename":
                client_id = incoming_client_id
                display_name = incoming_name
                rooms.register(room_id, client_id, display_name, websocket)
                await rooms.send_presence(room_id)
                continue

            if client_id not in rooms.sockets[room_id]:
                client_id = incoming_client_id
                display_name = incoming_name
                rooms.register(room_id, client_id, display_name, websocket)
                await rooms.send_presence(room_id)

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

            message = {
                "kind": "message",
                "msg_id": str(data.get("msg_id") or ("msg_" + uuid4().hex)),
                "client_id": client_id,
                "name": display_name,
                "text": str(data.get("text") or "").strip(),
                "room": room_id,
                "time": int(data.get("time") or int(time.time() * 1000)),
            }

            if not message["text"]:
                continue

            rooms.push_history(room_id, message)
            await rooms.broadcast(room_id, message)

    except WebSocketDisconnect:
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
      --panel:rgba(13, 19, 40, .88);
      --panel2:rgba(18, 28, 55, .94);
      --line:rgba(255,255,255,.08);
      --text:#eef3ff;
      --muted:#9aa8cf;
      --accent:#7b8cff;
      --accent2:#66efc0;
      --danger:#ff6b7f;
      --shadow:0 30px 90px rgba(0,0,0,.42);
      --radius:22px;
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
      grid-template-columns: 92px 286px minmax(0,1fr) 360px;
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
      padding:18px 20px;
      border-bottom:1px solid var(--line);
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:14px;
    }
    .titleblock h2{
      margin:0;
      font-size:22px;
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
      padding:10px 12px;
      min-width:120px;
    }
    .stat b{
      display:block;
      font-size:18px;
      margin-bottom:2px;
    }
    .stat span{
      color:var(--muted);
      font-size:12px;
    }

    .messages{
      flex:1;
      overflow:auto;
      padding:22px;
      display:flex;
      flex-direction:column;
      gap:12px;
      scroll-behavior:smooth;
    }
    .msg{
      display:grid;
      grid-template-columns:48px minmax(0,1fr);
      gap:12px;
      padding:16px;
      border-radius:18px;
      border:1px solid rgba(255,255,255,.06);
      background:rgba(255,255,255,.025);
    }
    .msg:hover{background:rgba(255,255,255,.032)}
    .avatar{
      width:48px;height:48px;border-radius:16px;
      display:grid;
      place-items:center;
      background:linear-gradient(180deg, #7b8cff, #5361ff);
      color:#fff;
      font-weight:800;
      flex:0 0 auto;
    }
    .head{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      margin-bottom:4px;
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
      line-height:1.6;
      color:#eef2ff;
      font-size:15px;
    }
    .system{
      border-style:dashed;
      background:rgba(123,140,255,.07);
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
      padding:14px;
      border-radius:20px;
      background:#08111f;
      border:1px solid rgba(255,255,255,.06);
    }
    .composer textarea{
      flex:1;
      resize:none;
      min-height:58px;
      max-height:180px;
      background:transparent;
      color:var(--text);
      border:none;
      outline:none;
      line-height:1.6;
      padding:10px 10px;
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
      align-items:center;
      justify-content:space-between;
      gap:8px;
      padding:10px 12px;
      border-radius:14px;
      background:rgba(255,255,255,.03);
      border:1px solid rgba(255,255,255,.05);
    }
    .user strong{font-size:13px}
    .user span{font-size:12px;color:var(--muted)}
    .typing{
      min-height:22px;
      color:#c7d2ff;
      font-size:12px;
      padding:0 22px 8px;
    }
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
            <small>clean, fast, and a little meaner than Discord</small>
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
              <b id="statState">idle</b>
              <span>connection state</span>
            </div>
          </div>
        </div>
        <div class="conn"><span class="dot"></span><span id="connState">connecting...</span></div>
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
        <p>Set your display name. This is your live nickname in the current room.</p>
        <input class="input" id="nameInput" maxlength="24" placeholder="Your name" />
      </div>

      <div class="panel">
        <h3>Who is online</h3>
        <ul class="list" id="members"></ul>
      </div>

      <div class="panel">
        <h3>What’s new</h3>
        <div class="updates">
          <div class="update"><b>Real presence</b><span>Connected users are pulled from the socket, not faked.</span></div>
          <div class="update"><b>Bigger chat view</b><span>Main chat area is wider and easier to read.</span></div>
          <div class="update"><b>Better reconnect</b><span>Stale socket events no longer trap the UI on connecting.</span></div>
          <div class="update"><b>Room history</b><span>Each room keeps message history while you stay on the server.</span></div>
        </div>
      </div>

      <div class="tiny">
        This starter shows online browser sessions in the current room. Real accounts, DMs, roles, and true global presence need auth + database next.
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
    statState: document.getElementById("statState"),
  };

  const savedName = localStorage.getItem("linkup_name") || "You";
  els.nameInput.value = savedName;
  let displayName = savedName;

  let selectedServer = localStorage.getItem("linkup_server") || "home";
  let selectedChannel = localStorage.getItem("linkup_channel") || "general";

  let socket = null;
  let connectionSeq = 0;
  let reconnectTimer = null;
  let handshakeTimer = null;
  let retryCount = 0;

  const localClientId = "cli_" + Math.random().toString(36).slice(2, 10);
  const roomMessages = new Map();
  const roomSeen = new Map();
  let currentPresence = [];
  let typingTimeout = null;

  const systemSeeds = {
    general: [
      "Welcome to LinkUp. This room is live.",
      "The online list is now real for the current room.",
      "You can switch channels, rename yourself, and watch presence update instantly."
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
    return new Date(ts || Date.now()).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit"
    });
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
    }
  }

  function getRoomMessages(room) {
    ensureRoom(room);
    return roomMessages.get(room);
  }

  function seenSet(room) {
    ensureRoom(room);
    return roomSeen.get(room);
  }

  function renderMessageHtml(m) {
    if (m.kind === "system") {
      return `
        <div class="msg system">
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

    return `
      <div class="msg">
        <div class="avatar">${escapeHtml(initial)}</div>
        <div>
          <div class="head">
            <div class="name">
              <span>${escapeHtml(m.name || "Guest")}</span>
              <span class="badge">${isSelf ? "you" : "online"}</span>
            </div>
            <span class="time">${fmtTime(m.time)}</span>
          </div>
          <div class="text">${escapeHtml(m.text || "")}</div>
        </div>
      </div>`;
  }

  function renderMessages(room) {
    const msgs = getRoomMessages(room);
    els.messages.innerHTML = msgs.map(renderMessageHtml).join("");
    els.statMessages.textContent = String(msgs.filter(m => m.kind !== "system").length);
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function addMessage(room, msg, renderNow = true) {
    ensureRoom(room);

    if (!msg.msg_id) {
      msg.msg_id = "msg_" + Math.random().toString(36).slice(2, 14);
    }

    const seen = seenSet(room);
    if (seen.has(msg.msg_id)) return;
    seen.add(msg.msg_id);

    const list = roomMessages.get(room);
    list.push(msg);

    if (room === roomId() && renderNow) {
      els.messages.insertAdjacentHTML("beforeend", renderMessageHtml(msg));
      els.messages.scrollTop = els.messages.scrollHeight;
      els.statMessages.textContent = String(list.filter(m => m.kind !== "system").length);
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
        localStorage.setItem("linkup_channel", selectedChannel);
        renderAll();
        connectSocket(true);
      };
    });
  }

  function renderMembers() {
    const roomUsers = currentPresence || [];
    els.members.innerHTML = roomUsers.length
      ? roomUsers.map(u => `
          <li class="user">
            <div>
              <strong>${escapeHtml(u.name)}</strong><br />
              <span>${u.client_id === localClientId ? "you • live now" : "connected"}</span>
            </div>
            <span class="badge">online</span>
          </li>
        `).join("")
      : `<li class="user"><div><strong>No one else yet</strong><br /><span>Be the first in this room.</span></div><span class="badge">0</span></li>`;

    els.statUsers.textContent = String(roomUsers.length);
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
    renderMembers();
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

    currentPresence = [];
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
        const seen = new Set();
        roomMessages.set(room, []);
        roomSeen.set(room, seen);

        history.forEach(m => {
          if (!m.msg_id) m.msg_id = "hist_" + Math.random().toString(36).slice(2, 12);
          seen.add(m.msg_id);
          roomMessages.get(room).push(m);
        });

        currentPresence = Array.isArray(data.users) ? data.users : [];
        renderMessages(room);
        renderMembers();
        return;
      }

      if (data.kind === "presence") {
        currentPresence = Array.isArray(data.users) ? data.users : [];
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

      if (data.kind === "message") {
        data.time = data.time || Date.now();
        data.client_id = data.client_id || "remote";
        data.msg_id = data.msg_id || `srv_${room}_${data.time}_${Math.random().toString(36).slice(2, 8)}`;
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
      time: Date.now()
    };

    addMessage(roomId(), payload, true);

    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
    }

    els.messageInput.value = "";
    els.messageInput.style.height = "58px";
    els.messageInput.focus();
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
    els.messageInput.style.height = Math.min(180, els.messageInput.scrollHeight) + "px";
    sendTyping();
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
