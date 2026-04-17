from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.api.websocket import router as ws_router
from app.core.config import settings

app = FastAPI(title="LinkUp")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ws_router, prefix="/ws", tags=["websocket"])


def page_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LinkUp</title>
  <style>
    :root{
      --bg:#0b1020;
      --bg2:#111833;
      --panel:#0f1730;
      --panel2:#121c3a;
      --line:#23315e;
      --text:#ecf2ff;
      --muted:#97a6cc;
      --accent:#7c8cff;
      --accent2:#6ef3c5;
      --danger:#ff6b7a;
      --shadow:0 24px 80px rgba(0,0,0,.42);
      --radius:22px;
      color-scheme: dark;
    }
    *{box-sizing:border-box}
    html,body{height:100%}
    body{
      margin:0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(124,140,255,.20), transparent 35%),
        radial-gradient(circle at top right, rgba(110,243,197,.12), transparent 30%),
        linear-gradient(180deg, #080d1a 0%, #0a0f1f 100%);
      color:var(--text);
    }
    a{color:inherit}
    button,input{font:inherit}
    .app{
      display:grid;
      grid-template-columns: 84px 280px minmax(0,1fr) 320px;
      gap:16px;
      padding:16px;
      height:100vh;
    }
    .glass{
      background: rgba(15, 23, 48, .82);
      backdrop-filter: blur(18px);
      border:1px solid rgba(255,255,255,.06);
      box-shadow: var(--shadow);
    }
    .servers{
      border-radius: var(--radius);
      padding:12px 8px;
      display:flex;
      flex-direction:column;
      align-items:center;
      gap:10px;
      overflow:auto;
    }
    .sbtn{
      width:56px;height:56px;border:none;border-radius:18px;
      background:linear-gradient(180deg, #1a2447, #101936);
      color:var(--text);
      cursor:pointer;
      display:grid;place-items:center;
      font-weight:800;
      letter-spacing:.2px;
      transition:.18s ease;
      border:1px solid rgba(255,255,255,.05);
    }
    .sbtn:hover{transform:translateY(-1px); border-color: rgba(124,140,255,.35)}
    .sbtn.active{background:linear-gradient(180deg, #7c8cff, #5d6aff); color:#fff; box-shadow:0 10px 25px rgba(124,140,255,.30)}
    .sidebar{
      border-radius:var(--radius);
      display:flex;
      flex-direction:column;
      min-width:0;
      overflow:hidden;
    }
    .side-top{
      padding:18px 18px 14px;
      border-bottom:1px solid rgba(255,255,255,.06);
    }
    .brand{
      display:flex;align-items:center;justify-content:space-between;gap:10px;
    }
    .brand h1{margin:0;font-size:22px;letter-spacing:.2px}
    .brand small{color:var(--muted)}
    .chip{
      display:inline-flex;align-items:center;gap:8px;
      border:1px solid rgba(255,255,255,.08);
      color:var(--muted);
      padding:8px 10px;border-radius:999px;
      margin-top:12px;
      font-size:12px;
      width:fit-content;
    }
    .section{
      padding:14px 14px 10px;
    }
    .section h3{
      margin:0 0 10px;
      font-size:12px;
      text-transform:uppercase;
      letter-spacing:.14em;
      color:#b6c2e8;
    }
    .channel{
      width:100%;
      border:none;
      background:transparent;
      color:var(--text);
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      padding:12px 12px;
      border-radius:14px;
      cursor:pointer;
      transition:.16s ease;
      text-align:left;
    }
    .channel:hover{background:rgba(255,255,255,.04)}
    .channel.active{background:rgba(124,140,255,.16); outline:1px solid rgba(124,140,255,.28)}
    .channel .meta{display:flex;flex-direction:column;min-width:0}
    .channel .meta strong{font-size:14px}
    .channel .meta span{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px}
    .main{
      border-radius:var(--radius);
      display:flex;
      flex-direction:column;
      min-width:0;
      overflow:hidden;
    }
    .main-top{
      padding:18px 20px;
      border-bottom:1px solid rgba(255,255,255,.06);
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:12px;
    }
    .titleblock h2{margin:0;font-size:20px}
    .titleblock p{margin:5px 0 0;color:var(--muted);font-size:13px}
    .status{
      display:flex;align-items:center;gap:8px;
      color:var(--muted);
      font-size:13px;
    }
    .dot{
      width:10px;height:10px;border-radius:999px;background:var(--accent2);
      box-shadow:0 0 0 4px rgba(110,243,197,.12);
    }
    .messages{
      flex:1;
      overflow:auto;
      padding:18px;
      display:flex;
      flex-direction:column;
      gap:14px;
      scroll-behavior:smooth;
    }
    .msg{
      display:grid;
      grid-template-columns:44px minmax(0,1fr);
      gap:12px;
      padding:14px;
      border:1px solid rgba(255,255,255,.06);
      border-radius:18px;
      background:rgba(255,255,255,.02);
    }
    .avatar{
      width:44px;height:44px;border-radius:16px;
      background:linear-gradient(180deg, #7c8cff, #4a57ff);
      display:grid;place-items:center;
      font-weight:800;
      color:#fff;
    }
    .msg .head{
      display:flex;align-items:center;justify-content:space-between;gap:10px;
      margin-bottom:5px;
    }
    .msg .name{
      display:flex;align-items:center;gap:8px;
      font-weight:700;
    }
    .badge{
      font-size:11px;padding:3px 8px;border-radius:999px;
      background:rgba(124,140,255,.14);
      color:#cfd7ff;
      border:1px solid rgba(124,140,255,.18);
    }
    .time{color:var(--muted);font-size:12px}
    .text{line-height:1.6;color:#ebf0ff;white-space:pre-wrap;word-break:break-word}
    .composer{
      padding:16px;
      border-top:1px solid rgba(255,255,255,.06);
      background:linear-gradient(180deg, rgba(255,255,255,.01), rgba(255,255,255,.03));
    }
    .composer-wrap{
      display:flex;
      align-items:flex-end;
      gap:12px;
      padding:12px;
      border-radius:18px;
      background:#0b1328;
      border:1px solid rgba(255,255,255,.06);
    }
    textarea{
      flex:1;
      resize:none;
      min-height:48px;
      max-height:160px;
      background:transparent;
      color:var(--text);
      border:none;
      outline:none;
      line-height:1.55;
      padding:12px 10px;
    }
    .send{
      border:none;
      background:linear-gradient(180deg, #7c8cff, #5564ff);
      color:#fff;
      padding:13px 18px;
      border-radius:14px;
      cursor:pointer;
      font-weight:700;
      box-shadow:0 14px 30px rgba(124,140,255,.22);
    }
    .send:hover{filter:brightness(1.04)}
    .tools{
      border-radius:var(--radius);
      display:flex;
      flex-direction:column;
      min-width:0;
      overflow:hidden;
    }
    .tools .panel{
      padding:16px;
      border-bottom:1px solid rgba(255,255,255,.06);
    }
    .tools h3{margin:0 0 10px;font-size:14px}
    .tools p, .tools li{color:var(--muted);font-size:13px;line-height:1.6}
    .input{
      width:100%;
      margin-top:10px;
      padding:12px 14px;
      border-radius:14px;
      border:1px solid rgba(255,255,255,.08);
      background:#0b1328;
      color:var(--text);
      outline:none;
    }
    .mini-list{
      display:flex;flex-direction:column;gap:10px;
      margin:0;padding:0;list-style:none;
    }
    .user{
      display:flex;align-items:center;justify-content:space-between;gap:8px;
      padding:10px 12px;border-radius:14px;
      background:rgba(255,255,255,.03);
      border:1px solid rgba(255,255,255,.05);
    }
    .user strong{font-size:13px}
    .user span{font-size:12px;color:var(--muted)}
    .footer-note{
      padding:14px 16px;
      color:var(--muted);
      font-size:12px;
      line-height:1.6;
    }
    .kbd{
      display:inline-flex;align-items:center;justify-content:center;
      min-width:24px;padding:2px 6px;border-radius:6px;
      border:1px solid rgba(255,255,255,.08);
      background:rgba(255,255,255,.04);
      color:#dbe3ff;
      font-size:11px;
    }

    @media (max-width: 1200px){
      .app{grid-template-columns: 84px 260px minmax(0,1fr); }
      .tools{display:none}
    }
    @media (max-width: 900px){
      .app{grid-template-columns: 1fr; height:auto; min-height:100vh}
      .servers{flex-direction:row; justify-content:flex-start; overflow:auto}
      .sidebar{min-height:420px}
      .main{min-height:70vh}
      .tools{display:block}
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
            <small>better-than-Discord starter</small>
          </div>
          <div class="badge">LIVE</div>
        </div>
        <div class="chip">Room: <span id="roomLabel">#general</span></div>
      </div>

      <div class="section">
        <h3>Channels</h3>
        <div id="channels"></div>
      </div>

      <div class="footer-note">
        Tip: press <span class="kbd">Enter</span> to send, <span class="kbd">Shift</span> + <span class="kbd">Enter</span> for a new line.
      </div>
    </aside>

    <main class="main glass">
      <div class="main-top">
        <div class="titleblock">
          <h2 id="channelTitle"># general</h2>
          <p id="channelDescription">A fast, clean, real-time room for your crew.</p>
        </div>
        <div class="status"><span class="dot"></span><span id="connState">connecting...</span></div>
      </div>

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
        <p>Set your display name. It saves in your browser.</p>
        <input class="input" id="nameInput" maxlength="24" placeholder="Your name" />
      </div>
      <div class="panel">
        <h3>Members online</h3>
        <ul class="mini-list" id="members"></ul>
      </div>
      <div class="panel">
        <h3>LinkUp goals</h3>
        <ul>
          <li>Servers, channels, DMs, roles</li>
          <li>Live chat with WebSockets</li>
          <li>Fast UI, low clutter, more polish</li>
        </ul>
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

  const members = [
    { name: "Nova", role: "admin" },
    { name: "Pixel", role: "mod" },
    { name: "Echo", role: "member" },
    { name: "You", role: "online" }
  ];

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
  };

  const savedName = localStorage.getItem("linkup_name") || "You";
  els.nameInput.value = savedName;
  let displayName = savedName;

  let selectedServer = localStorage.getItem("linkup_server") || "home";
  let selectedChannel = localStorage.getItem("linkup_channel") || "general";
  let socket = null;
  let reconnectTimer = null;
  let localId = Math.random().toString(36).slice(2, 9);

  const systemMessages = {
    general: [
      "Welcome to LinkUp. This room is live.",
      "You can rename the app, add DMs, roles, and moderation next."
    ],
    announcements: ["Ship updates here.", "Use this room for changelogs."],
    showcase: ["Drop your best work here.", "Show your builds and screenshots."],
    frontend: ["React, CSS, motion, and layout talk."],
    backend: ["FastAPI, WebSockets, database design."],
    bugs: ["Log a bug, then squash it."],
    lobby: ["Find your squad."],
    clips: ["Funny moments belong here."],
    meta: ["Discuss game balance and tactics."],
    math: ["Study mode online."],
    coding: ["Code and conquer."],
    focus: ["Deep work zone."]
  };

  function currentChannel() {
    return channelsByServer[selectedServer].find(c => c.id === selectedChannel) || channelsByServer[selectedServer][0];
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, s => ({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;" }[s]));
  }

  function fmtTime(ts) {
    return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function renderServers() {
    els.servers.innerHTML = servers.map(s => `
      <button class="sbtn ${s.id === selectedServer ? "active" : ""}" data-server="${s.id}" title="${s.label}">
        ${s.name}
      </button>
    `).join("");

    els.servers.querySelectorAll("button").forEach(btn => {
      btn.onclick = () => {
        selectedServer = btn.dataset.server;
        selectedChannel = channelsByServer[selectedServer][0].id;
        localStorage.setItem("linkup_server", selectedServer);
        localStorage.setItem("linkup_channel", selectedChannel);
        renderAll();
        connectSocket();
      };
    });
  }

  function renderChannels() {
    els.channels.innerHTML = channelsByServer[selectedServer].map(ch => `
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
        localStorage.setItem("linkup_channel", selectedChannel);
        renderAll();
        connectSocket();
      };
    });
  }

  function renderMembers() {
    els.members.innerHTML = members.map(m => `
      <li class="user">
        <div>
          <strong>${escapeHtml(m.name)}</strong><br/>
          <span>${escapeHtml(m.role)}</span>
        </div>
        <span class="badge">${m.role}</span>
      </li>
    `).join("");
  }

  function setHeader() {
    const ch = currentChannel();
    els.channelTitle.textContent = ch.title;
    els.channelDescription.textContent = ch.description;
    els.roomLabel.textContent = `#${ch.id}`;
    document.title = `LinkUp — ${ch.title}`;
  }

  function addMessage({ name, text, time, kind = "message" }) {
    const initial = (name || "?").trim().slice(0,1).toUpperCase();
    const html = kind === "system"
      ? `<div class="msg">
           <div class="avatar">!</div>
           <div>
             <div class="head"><div class="name"><span>System</span><span class="badge">notice</span></div><span class="time">${fmtTime(time)}</span></div>
             <div class="text">${escapeHtml(text)}</div>
           </div>
         </div>`
      : `<div class="msg">
           <div class="avatar">${escapeHtml(initial)}</div>
           <div>
             <div class="head">
               <div class="name"><span>${escapeHtml(name)}</span><span class="badge">${kind === "self" ? "you" : "member"}</span></div>
               <span class="time">${fmtTime(time)}</span>
             </div>
             <div class="text">${escapeHtml(text)}</div>
           </div>
         </div>`;
    els.messages.insertAdjacentHTML("beforeend", html);
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function seedMessages() {
    els.messages.innerHTML = "";
    const ch = selectedChannel;
    (systemMessages[ch] || ["Room loaded."]).forEach((msg, i) => {
      addMessage({ name: "System", text: msg, time: Date.now() - (1000 * 60 * (3 - i)), kind: "system" });
    });
  }

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/${selectedChannel}`;
  }

  function setConnState(text, ok=false) {
    els.connState.textContent = text;
    els.connState.style.color = ok ? "#d1ffe9" : "";
  }

  function connectSocket() {
    if (socket) {
      try { socket.close(); } catch {}
    }
    setConnState("connecting...");
    seedMessages();

    try {
      socket = new WebSocket(wsUrl());
    } catch (e) {
      setConnState("offline");
      return;
    }

    socket.onopen = () => setConnState("connected", true);
    socket.onclose = () => {
      setConnState("reconnecting...");
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connectSocket, 1200);
    };
    socket.onerror = () => setConnState("connection error");
    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.kind === "system") {
          addMessage({ ...data, kind: "system" });
        } else {
          addMessage({
            name: data.name || "Guest",
            text: data.text || "",
            time: data.time || Date.now(),
            kind: (data.client_id === localId) ? "self" : "member"
          });
        }
      } catch {
        addMessage({ name: "Guest", text: String(event.data), time: Date.now(), kind: "member" });
      }
    };
  }

  function sendMessage() {
    const text = els.messageInput.value.trim();
    if (!text || !socket || socket.readyState !== WebSocket.OPEN) return;
    const payload = {
      kind: "message",
      client_id: localId,
      name: displayName,
      text,
      channel: selectedChannel,
      room: selectedServer,
      time: Date.now()
    };
    socket.send(JSON.stringify(payload));
    els.messageInput.value = "";
    els.messageInput.focus();
  }

  function renderAll() {
    renderServers();
    renderChannels();
    renderMembers();
    setHeader();
  }

  els.nameInput.addEventListener("change", () => {
    displayName = (els.nameInput.value || "You").trim().slice(0, 24) || "You";
    localStorage.setItem("linkup_name", displayName);
  });

  els.sendBtn.onclick = sendMessage;
  els.messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  renderAll();
  connectSocket();
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
