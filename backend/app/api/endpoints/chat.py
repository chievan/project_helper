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
    
    # 构造更高效的上下文提示词
    messages = [
        SystemMessage(content=f"""你是一个对该项目了如指掌的 AI 助手。
        ### 核心原则：
        1. **报告优先**：优先根据下方的“项目分析报告”回答问题。如果报告里已经有答案，禁止调用工具查代码。
        2. **按需查阅**：只有当用户询问报告中未涵盖的极致细节（如具体的函数实现）时，才允许查阅代码。
        3. **言简意赅**：回答要直接、专业、简练，不要长篇大论。
        4. **项目背景报告**：
        {repo.analysis_report}""")
    ]
    
    # 加载历史记录
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

    # 保存用户消息
    user_msg_db = ChatMessage(repo_id=data.repo_id, role="user", content=data.message)
    session.add(user_msg_db)
    session.commit()
    
    messages.append(HumanMessage(content=data.message))
    
    async def event_generator():
        nonlocal messages
        full_assistant_content = ""
        
        # 支持多轮工具调用循环
        for turn in range(5): 
            buffer = ""
            in_tag = False
            current_turn_content = ""
            current_ai_msg = None
            
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
                                    current_turn_content += text
                                    yield f"data: {json.dumps({'text': text})}\n\n"
                                    buffer = ""
                                    in_tag = False
                            elif len(buffer) > 200:
                                text = buffer
                                current_turn_content += text
                                yield f"data: {json.dumps({'text': text})}\n\n"
                                buffer = ""
                                in_tag = False
                        else:
                            text = buffer
                            current_turn_content += text
                            yield f"data: {json.dumps({'text': text})}\n\n"
                            buffer = ""

                elif kind == "on_chat_model_end":
                    if buffer and not in_tag:
                        current_turn_content += buffer
                        yield f"data: {json.dumps({'text': buffer})}\n\n"
                    current_ai_msg = event["data"]["output"]

            if not current_ai_msg: break
            
            # 如果没有工具调用，说明这就是最终回答，跳出循环
            if not current_ai_msg.tool_calls:
                full_assistant_content += current_turn_content
                break
            
            # 如果有工具调用，执行它们并继续下一轮
            messages.append(current_ai_msg)
            for tool_call in current_ai_msg.tool_calls:
                yield f"data: {json.dumps({'text': f'\n🔍 正在检索相关代码: {tool_call['name']}...\n'})}\n\n"
                if tool_call["name"] in analyzer.tools_map:
                    try:
                        args = tool_call["args"].copy()
                        if "repo_path" in args: args["repo_path"] = analyzer.local_path
                        output = await asyncio.to_thread(analyzer.tools_map[tool_call["name"]].invoke, args)
                        messages.append(ToolMessage(content=str(output), tool_call_id=tool_call["id"]))
                    except Exception as e:
                        messages.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call["id"]))
            
            # 工具调用后的自然语言回答会累加到 full_assistant_content
            full_assistant_content += current_turn_content

        # 保存完整回答
        if full_assistant_content.strip():
            ai_msg_db = ChatMessage(repo_id=data.repo_id, role="assistant", content=full_assistant_content.strip())
            session.add(ai_msg_db)
            session.commit()

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.get("/history/{repo_id}")
async def get_chat_history(repo_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(ChatMessage)
        .where(ChatMessage.repo_id == repo_id)
        .order_by(ChatMessage.created_at)
    ).all()
