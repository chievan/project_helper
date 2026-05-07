from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from app.core.db import get_session
from app.models.repo import Repository
from app.services.analyzer import RepoAnalyzer
from pydantic import BaseModel
from datetime import datetime
import asyncio
import json

router = APIRouter()

class RepoSubmit(BaseModel):
    url: str

@router.post("/submit")
async def submit_repo(data: RepoSubmit, background_tasks: BackgroundTasks, session: Session = Depends(get_session)):
    # Check cache
    statement = select(Repository).where(Repository.url == data.url)
    db_repo = session.exec(statement).first()
    
    if db_repo and db_repo.status == "completed":
        return {"message": "Project already analyzed", "repo_id": db_repo.id, "cached": True}
    
    if not db_repo:
        # Initial entry
        parts = data.url.rstrip("/").split("/")
        name = parts[-1].replace(".git", "")
        owner = parts[-2]
        db_repo = Repository(url=data.url, name=name, owner=owner, status="cloning", progress=10.0)
        session.add(db_repo)
        session.commit()
        session.refresh(db_repo)
    else:
        db_repo.status = "cloning"
        db_repo.progress = 10.0
        db_repo.updated_at = datetime.utcnow()
        session.add(db_repo)
        session.commit()

    # Start analysis in background
    background_tasks.add_task(run_analysis, data.url)
    
    return {"message": "Analysis started", "repo_id": db_repo.id, "cached": False}

async def run_analysis(url: str):
    try:
        analyzer = RepoAnalyzer(url)
        await analyzer.analyze()
    except Exception as e:
        print(f"Background analysis failed: {e}")
        # Status update is handled inside analyzer.analyze() usually, 
        # but if it fails before that, we should ensure it's marked as failed.

@router.get("/events/{repo_id}")
async def stream_repo_analysis(repo_id: int, session: Session = Depends(get_session)):
    db_repo = session.get(Repository, repo_id)
    if not db_repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    
    if db_repo.status == "completed":
        # If already completed, just send one final event
        async def completed_gen():
            yield f"data: {json.dumps({'type': 'status', 'status': 'completed', 'progress': 100.0})}\n\n"
        return StreamingResponse(completed_gen(), media_type="text/event-stream")

    analyzer = RepoAnalyzer(db_repo.url)
    
    async def event_generator():
        async for event in analyzer.analyze_stream():
            yield f"data: {event}\n\n"
            await asyncio.sleep(0.01) # Small sleep to ensure smooth flow

    return StreamingResponse(event_generator(), media_type="text/event-stream")

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
