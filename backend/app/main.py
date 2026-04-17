from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, users, servers, channels, messages, dms, roles, moderation
from app.api.websocket import router as ws_router

app = FastAPI(title="LinkUp API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # lock this down in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(servers.router, prefix="/servers", tags=["servers"])
app.include_router(channels.router, prefix="/channels", tags=["channels"])
app.include_router(messages.router, prefix="/messages", tags=["messages"])
app.include_router(dms.router, prefix="/dms", tags=["dms"])
app.include_router(roles.router, prefix="/roles", tags=["roles"])
app.include_router(moderation.router, prefix="/moderation", tags=["moderation"])
app.include_router(ws_router, prefix="/ws", tags=["websocket"])

@app.get("/health")
def health():
    return {"ok": True, "app": "LinkUp"}
