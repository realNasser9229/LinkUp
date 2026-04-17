# LinkUp

LinkUp is a real-time chat platform built with FastAPI and WebSockets.

## Local run

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
