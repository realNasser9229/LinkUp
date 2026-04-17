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


def landing_page() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>LinkUp</title>
        <style>
          :root {
            color-scheme: dark;
            --bg: #0b1020;
            --card: #111833;
            --text: #e7ecff;
            --muted: #94a3c7;
            --accent: #6d7cff;
            --line: #22305f;
          }
          body {
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            background: radial-gradient(circle at top, #18214a 0%, var(--bg) 50%);
            color: var(--text);
            font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
          }
          .card {
            width: min(720px, calc(100vw - 32px));
            background: rgba(17, 24, 51, 0.92);
            border: 1px solid var(--line);
            border-radius: 24px;
            padding: 32px;
            box-shadow: 0 24px 80px rgba(0,0,0,.35);
          }
          h1 { margin: 0 0 10px; font-size: 44px; }
          p { color: var(--muted); line-height: 1.6; font-size: 16px; }
          .pill {
            display: inline-block;
            margin-top: 14px;
            padding: 8px 12px;
            border: 1px solid var(--line);
            border-radius: 999px;
            color: var(--accent);
            background: rgba(109,124,255,.08);
            font-size: 13px;
          }
          code {
            display: block;
            margin-top: 18px;
            padding: 14px 16px;
            border-radius: 14px;
            background: #0b1227;
            border: 1px solid var(--line);
            color: #dfe6ff;
            overflow-x: auto;
          }
        </style>
      </head>
      <body>
        <main class="card">
          <h1>LinkUp</h1>
          <p>Your real-time chat platform is live. The backend is responding, the root route exists, and Render should no longer show Not Found here.</p>
          <span class="pill">FastAPI • WebSockets • Render</span>
          <code>/health → live check
/ws/{room_id} → real-time chat socket</code>
        </main>
      </body>
    </html>
    """


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(content=landing_page())


@app.get("/health")
def health():
    return {"ok": True, "app": "LinkUp"}


@app.get("/{path:path}", response_class=HTMLResponse)
def catch_all(path: str):
    # Keeps the site from showing Not Found when someone opens a random browser path.
    # Remove later once the real frontend router is in place.
    return HTMLResponse(content=landing_page())


app.include_router(ws_router, prefix="/ws", tags=["websocket"])
