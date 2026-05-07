import json
import re
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from app.core.db import get_session, engine
from app.models.repo import Repository
from app.models.chat import ChatMessage
from app.services.analyzer import RepoAnalyzer
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from typing import List, Optional, Dict, Any
from datetime import datetime

router = APIRouter()

class ChatRequest(BaseModel):
    repo_id: int
    message: str

class ChatManager:
    def __init__(self):
        self.active_tasks: Dict[int, asyncio.Task] = {}
        self.queues: Dict[int, List[asyncio.Queue]] = {}
        self.current_responses: Dict[int, str] = {}
        self.logs_cache: Dict[int, List[Any]] = {} 

    async def start_chat_task(self, repo_id: int, message: str, repo_url: str, report: str):
        if repo_id in self.active_tasks:
            self.active_tasks[repo_id].cancel()
        
        self.current_responses[repo_id] = ""
        self.logs_cache[repo_id] = []
        task = asyncio.create_task(self._run_chat_logic(repo_id, message, repo_url, report))
        self.active_tasks[repo_id] = task

    async def _run_chat_logic(self, repo_id: int, message: str, repo_url: str, report: str):
        analyzer = RepoAnalyzer(repo_url, repo_id)
        
        with Session(engine) as session:
            messages = [SystemMessage(content=f"项目背景: {report}")]
            db_history = session.exec(select(ChatMessage).where(ChatMessage.repo_id == repo_id).order_by(ChatMessage.created_at)).all()
            for m in db_history:
                messages.append(HumanMessage(content=m.content) if m.role == "user" else AIMessage(content=m.content))
            
            session.add(ChatMessage(repo_id=repo_id, role="user", content=message))
            session.commit()
            messages.append(HumanMessage(content=message))

        try:
            full_answer = ""
            # 阶段 A：调查
            for turn in range(3):
                res = await analyzer.llm_with_tools.ainvoke(messages)
                if not res.tool_calls:
                    messages.append(res)
                    break
                messages.append(res)
                for tool_call in res.tool_calls:
                    log_entry = {"text": f"🔍 正在检索相关代码: {tool_call['name']}...\n"}
                    self.logs_cache[repo_id].append(log_entry) # 存入缓存
                    await self._broadcast(repo_id, log_entry)
                    
                    if tool_call["name"] in analyzer.tools_map:
                        try:
                            args = tool_call["args"].copy()
                            if "repo_path" in args: args["repo_path"] = analyzer.local_path
                            out = await asyncio.to_thread(analyzer.tools_map[tool_call["name"]].invoke, args)
                            messages.append(ToolMessage(content=str(out), tool_call_id=tool_call["id"]))
                        except Exception as e:
                            messages.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call["id"]))

            # 阶段 B：直播回答
            messages.append(HumanMessage(content="调查结束。现在请直接输出最终的简练中文回答。严禁输出任何标签！"))
            async for chunk in analyzer.llm.astream(messages):
                if chunk.content:
                    cleaned = chunk.content.replace("< | DSML |", "").replace("| >", "").replace("tool_calls", "")
                    full_answer += cleaned
                    self.current_responses[repo_id] = full_answer
                    await self._broadcast(repo_id, {"text": cleaned})

            if full_answer.strip():
                with Session(engine) as session:
                    session.add(ChatMessage(repo_id=repo_id, role="assistant", content=full_answer.strip()))
                    session.commit()
            
            await self._broadcast(repo_id, {"done": True})

        except asyncio.CancelledError:
            if self.current_responses.get(repo_id):
                with Session(engine) as session:
                    session.add(ChatMessage(repo_id=repo_id, role="assistant", content=self.current_responses[repo_id]))
                    session.commit()
        except Exception as e:
            await self._broadcast(repo_id, {"text": f"Error: {str(e)}", "done": True})
        finally:
            if repo_id in self.active_tasks: del self.active_tasks[repo_id]

    async def _broadcast(self, repo_id: int, data: Any):
        if repo_id in self.queues:
            for q in self.queues[repo_id]: await q.put(data)

    async def subscribe(self, repo_id: int):
        q = asyncio.Queue()
        # 补课阶段：先发送调查日志
        if repo_id in self.logs_cache:
            for log in self.logs_cache[repo_id]:
                await q.put(log)
        
        # 补课阶段：再发送已生成的回答
        if repo_id in self.current_responses and self.current_responses[repo_id]:
            await q.put({"text": self.current_responses[repo_id], "is_resume": True})
        
        if repo_id not in self.queues: self.queues[repo_id] = []
        self.queues[repo_id].append(q)
        return q

    def unsubscribe(self, repo_id: int, q: asyncio.Queue):
        if repo_id in self.queues: self.queues[repo_id].remove(q)

chat_manager = ChatManager()

@router.post("/ask")
async def chat_with_repo(data: ChatRequest, session: Session = Depends(get_session)):
    repo = session.get(Repository, data.repo_id)
    if not repo: raise HTTPException(status_code=404, detail="Repository not found")
    await chat_manager.start_chat_task(repo.id, data.message, repo.url, repo.analysis_report)
    return {"message": "Chat task started"}

@router.get("/events/{repo_id}")
async def stream_chat_events(repo_id: int):
    q = await chat_manager.subscribe(repo_id)
    async def event_generator():
        try:
            while True:
                data = await q.get()
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("done"): break
        finally:
            chat_manager.unsubscribe(repo_id, q)
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.get("/history/{repo_id}")
async def get_chat_history(repo_id: int, session: Session = Depends(get_session)):
    return session.exec(select(ChatMessage).where(ChatMessage.repo_id == repo_id).order_by(ChatMessage.created_at)).all()
