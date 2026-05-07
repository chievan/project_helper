import json
import re
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from app.core.db import get_session
from app.models.repo import Repository
from app.models.chat import ChatMessage
from app.services.analyzer import RepoAnalyzer
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from typing import List, Optional
from datetime import datetime

router = APIRouter()

class ChatRequest(BaseModel):
    repo_id: int
    message: str
    history_limit: int = 20

@router.post("/ask")
async def chat_with_repo(
    data: ChatRequest,
    session: Session = Depends(get_session)
):
    repo = session.get(Repository, data.repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    
    analyzer = RepoAnalyzer(repo.url, repo.id)
    
    # 构造上下文：分析报告 + 历史记录
    messages = [
        SystemMessage(content=f"你是一个对该项目了如指掌的 AI 助手。项目分析背景: {repo.analysis_report}")
    ]
    
    # 从数据库加载真实历史
    db_messages = session.exec(
        select(ChatMessage)
        .where(ChatMessage.repo_id == data.repo_id)
        .order_by(ChatMessage.created_at)
    ).all()
    
    for m in db_messages:
        if m.role == "user":
            messages.append(HumanMessage(content=m.content))
        else:
            messages.append(AIMessage(content=m.content))

    # 保存当前用户提问
    user_msg_db = ChatMessage(repo_id=data.repo_id, role="user", content=data.message)
    session.add(user_msg_db)
    session.commit()
    
    messages.append(HumanMessage(content=data.message))
    
    async def event_generator():
        buffer = ""
        in_tag = False
        full_assistant_content = ""
        
        try:
            async for event in analyzer.llm_with_tools.astream_events(messages, version="v2"):
                kind = event["event"]
                
                if kind == "on_chat_model_stream":
                    content = event["data"]["chunk"].content
                    if not content: continue
                    
                    for char in content:
                        buffer += char
                        if "<" in buffer and not in_tag:
                            in_tag = True
                        
                        if in_tag:
                            if ">" in buffer:
                                if re.search(r"<[\s|]*DSML[\s|]*.*?>", buffer) or re.search(r"<[\s|]*tool_calls[\s|]*.*?>", buffer):
                                    buffer = "" 
                                    in_tag = False
                                elif len(buffer) > 200:
                                    text = buffer
                                    full_assistant_content += text
                                    yield f"data: {json.dumps({'text': text})}\n\n"
                                    buffer = ""
                                    in_tag = False
                            elif len(buffer) > 200:
                                text = buffer
                                full_assistant_content += text
                                yield f"data: {json.dumps({'text': text})}\n\n"
                                buffer = ""
                                in_tag = False
                        else:
                            text = buffer
                            full_assistant_content += text
                            yield f"data: {json.dumps({'text': text})}\n\n"
                            buffer = ""

                elif kind == "on_chat_model_end":
                    if buffer and not in_tag:
                        full_assistant_content += buffer
                        yield f"data: {json.dumps({'text': buffer})}\n\n"
            
            # 保存 AI 回复到数据库
            if full_assistant_content.strip():
                ai_msg_db = ChatMessage(
                    repo_id=data.repo_id, 
                    role="assistant", 
                    content=full_assistant_content.strip()
                )
                session.add(ai_msg_db)
                session.commit()

        except Exception as e:
            yield f"data: {json.dumps({'text': f'Error: {str(e)}'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.get("/history/{repo_id}")
async def get_chat_history(repo_id: int, session: Session = Depends(get_session)):
    messages = session.exec(
        select(ChatMessage)
        .where(ChatMessage.repo_id == repo_id)
        .order_by(ChatMessage.created_at)
    ).all()
    return messages
