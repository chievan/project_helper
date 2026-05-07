from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from app.core.db import get_session
from app.models.repo import Repository
from app.services.analyzer import RepoAnalyzer
from pydantic import BaseModel
from datetime import datetime
import asyncio
import json
from typing import Dict, List, Any

router = APIRouter()

class RepoSubmit(BaseModel):
    url: str

# 全局任务管理器：解耦“分析进程”与“浏览器连接”
class AnalysisManager:
    def __init__(self):
        self.active_tasks: Dict[int, asyncio.Task] = {}
        self.subscribers: Dict[int, List[asyncio.Queue]] = {}
        self.logs_cache: Dict[int, List[Dict[str, Any]]] = {}

    async def get_or_create_task(self, repo_id: int, url: str):
        # 如果任务已经在跑了，直接返回
        if repo_id in self.active_tasks and not self.active_tasks[repo_id].done():
            return
        
        # 否则启动一个全新的、持久的后台任务
        self.logs_cache[repo_id] = []
        task = asyncio.create_task(self._run_analysis(repo_id, url))
        self.active_tasks[repo_id] = task

    async def _run_analysis(self, repo_id: int, url: str):
        analyzer = RepoAnalyzer(url, repo_id)
        try:
            async for event_str in analyzer.analyze_stream():
                event = json.loads(event_str)
                # 存入历史缓存
                self.logs_cache[repo_id].append(event)
                # 广播给当前所有正在看直播的人
                if repo_id in self.subscribers:
                    for q in self.subscribers[repo_id]:
                        await q.put(event)
        except Exception as e:
            print(f"Manager analysis failed: {e}")
        finally:
            # 即使直播结束，任务记录也保留一段时间
            pass

    async def subscribe(self, repo_id: int):
        q = asyncio.Queue()
        # 关键：新进场的人先补课（发送历史缓存）
        if repo_id in self.logs_cache:
            for event in self.logs_cache[repo_id]:
                await q.put(event)
        
        if repo_id not in self.subscribers:
            self.subscribers[repo_id] = []
        self.subscribers[repo_id].append(q)
        return q

    def unsubscribe(self, repo_id: int, q: asyncio.Queue):
        if repo_id in self.subscribers:
            self.subscribers[repo_id].remove(q)

manager = AnalysisManager()

@router.post("/submit")
async def submit_repo(data: RepoSubmit, session: Session = Depends(get_session)):
    statement = select(Repository).where(Repository.url == data.url)
    db_repo = session.exec(statement).first()
    
    if not db_repo:
        parts = data.url.rstrip("/").split("/")
        name = parts[-1].replace(".git", "")
        owner = parts[-2]
        db_repo = Repository(url=data.url, name=name, owner=owner, status="pending", progress=0.0)
        session.add(db_repo)
        session.commit()
        session.refresh(db_repo)
    
    # 异步预热分析任务
    await manager.get_or_create_task(db_repo.id, data.url)
    return {"message": "Analysis started", "repo_id": db_repo.id}

@router.get("/events/{repo_id}")
async def stream_repo_analysis(repo_id: int, session: Session = Depends(get_session)):
    db_repo = session.get(Repository, repo_id)
    if not db_repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    
    # 确保后台任务正在运行
    await manager.get_or_create_task(repo_id, db_repo.url)
    
    # 接入订阅流
    q = await manager.subscribe(repo_id)
    
    async def event_generator():
        try:
            while True:
                try:
                    # 30秒心跳检查
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("status") == "completed":
                        break
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            manager.unsubscribe(repo_id, q)

    return StreamingResponse(
        event_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@router.get("/{repo_id}")
async def get_repo_status(repo_id: int, session: Session = Depends(get_session)):
    db_repo = session.get(Repository, repo_id)
    if not db_repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    return db_repo

@router.get("/all/list")
async def list_repos(session: Session = Depends(get_session)):
    statement = select(Repository).order_by(Repository.updated_at.desc()).limit(10)
    results = session.exec(statement).all()
    return results

@router.delete("/{repo_id}")
async def delete_repo(repo_id: int, session: Session = Depends(get_session)):
    db_repo = session.get(Repository, repo_id)
    if not db_repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    
    session.delete(db_repo)
    session.commit()
    return {"message": "Repository deleted successfully"}
