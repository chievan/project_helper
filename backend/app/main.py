import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from app.api.endpoints import repo, chat, github
from app.core.config import settings

from app.core.db import init_db

app = FastAPI(
    title="Project Helper API",
    description="Agentic GitHub Repository Analyzer & Q&A Assistant",
    version="1.0.0"
)

@app.on_event("startup")
def on_startup():
    init_db()

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.staticfiles import StaticFiles

# Include Routers
app.include_router(repo.router, prefix="/api/repo", tags=["Repository"])
app.include_router(chat.router, prefix="/api/chat", tags=["Chat"])
app.include_router(github.router, prefix="/api/github", tags=["GitHub"])

# Static Files Serving for Production
# This assumes the frontend is built into ../frontend/dist
static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../frontend/dist")
if os.path.exists(static_path):
    app.mount("/", StaticFiles(directory=static_path, html=True), name="static")
else:
    @app.get("/")
    async def root():
        return {"message": "Welcome to Project Helper API (Static files not found)"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
