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
    
    # 1. 构造上下文
    messages = [
        SystemMessage(content=f"""你是一个对该项目了如指掌的 AI 助手。
        优先根据下方的“项目分析报告”回答问题。如果报告中没有，再考虑查阅代码。
        项目分析背景: {repo.analysis_report}""")
    ]
    
    # 2. 加载历史
    db_messages = session.exec(
        select(ChatMessage)
        .where(ChatMessage.repo_id == data.repo_id)
        .order_by(ChatMessage.created_at)
    ).all()
    for m in db_messages:
        role_msg = HumanMessage(content=m.content) if m.role == "user" else AIMessage(content=m.content)
        messages.append(role_msg)

    # 3. 保存并加入当前提问
    user_msg_db = ChatMessage(repo_id=data.repo_id, role="user", content=data.message)
    session.add(user_msg_db)
    session.commit()
    messages.append(HumanMessage(content=data.message))
    
    async def event_generator():
        nonlocal messages
        
        # 阶段 A：调查阶段（不直播字符流，只显示进度）
        for turn in range(3): # 最多调查 3 轮，提速
            res = await analyzer.llm_with_tools.ainvoke(messages)
            if not res.tool_calls:
                # 如果没有工具调用，直接进入阶段 B
                messages.append(res)
                break
            
            messages.append(res)
            for tool_call in res.tool_calls:
                yield f"data: {json.dumps({'text': f'🔍 正在检索相关代码: {tool_call['name']}...'})}\n\n"
                if tool_call["name"] in analyzer.tools_map:
                    try:
                        args = tool_call["args"].copy()
                        if "repo_path" in args: args["repo_path"] = analyzer.local_path
                        out = await asyncio.to_thread(analyzer.tools_map[tool_call["name"]].invoke, args)
                        messages.append(ToolMessage(content=str(out), tool_call_id=tool_call["id"]))
                    except Exception as e:
                        messages.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call["id"]))
        
        # 阶段 B：生成最终回答（开启直播流，严禁标签）
        messages.append(HumanMessage(content="调查结束。现在请直接输出最终的简练中文回答。严禁输出任何 DSML 标签或工具调用！"))
        
        full_answer = ""
        async for chunk in analyzer.llm.astream(messages):
            if chunk.content:
                # 最后的安全过滤
                cleaned = chunk.content.replace("< | DSML |", "").replace("| >", "").replace("tool_calls", "")
                full_answer += cleaned
                yield f"data: {json.dumps({'text': cleaned})}\n\n"
        
        # 保存 AI 回答
        if full_answer.strip():
            ai_msg_db = ChatMessage(repo_id=data.repo_id, role="assistant", content=full_answer.strip())
            session.add(ai_msg_db)
            session.commit()

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.get("/history/{repo_id}")
async def get_chat_history(repo_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(ChatMessage).where(ChatMessage.repo_id == repo_id).order_by(ChatMessage.created_at)
    ).all()
