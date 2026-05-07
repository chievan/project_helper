import json
import re
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from app.core.db import get_session
from app.models.repo import Repository
from app.services.analyzer import RepoAnalyzer
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from typing import List, Optional

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
    
    # 构造历史背景
    messages = [
        SystemMessage(content=f"你是一个对该项目了如指掌的 AI 助手。项目背景: {repo.analysis_report}")
    ]
    
    # 获取最近的历史记录 (模拟，实际可从数据库读取)
    # 这里我们只带入当前的分析报告作为背景
    messages.append(HumanMessage(content=data.message))
    
    async def event_generator():
        # 状态机：用于流式拦截 DSML 标签
        buffer = ""
        in_tag = False
        
        try:
            async for event in analyzer.llm_with_tools.astream_events(messages, version="v2"):
                kind = event["event"]
                
                if kind == "on_chat_model_stream":
                    content = event["data"]["chunk"].content
                    if not content: continue
                    
                    # 遍历每一个字符，进行状态过滤
                    for char in content:
                        buffer += char
                        if "<" in buffer and not in_tag:
                            in_tag = True
                        
                        if in_tag:
                            # 如果缓冲区包含完整的标签，检查并处理
                            if ">" in buffer:
                                # 匹配 DSML 标签或类似的工具调用标签
                                if re.search(r"<[\s|]*DSML[\s|]*.*?>", buffer) or re.search(r"<[\s|]*tool_calls[\s|]*.*?>", buffer):
                                    buffer = "" # 抹除标签
                                    in_tag = False
                                elif len(buffer) > 200: # 容错：如果太长还没闭合，可能不是标签
                                    yield f"data: {json.dumps({'text': buffer})}\n\n"
                                    buffer = ""
                                    in_tag = False
                                else:
                                    # 还没到闭合点，继续观察
                                    pass
                            elif len(buffer) > 200: # 长度溢出保护
                                yield f"data: {json.dumps({'text': buffer})}\n\n"
                                buffer = ""
                                in_tag = False
                        else:
                            # 正常文本，直接吐出
                            yield f"data: {json.dumps({'text': buffer})}\n\n"
                            buffer = ""

                elif kind == "on_chat_model_end":
                    # 最后的兜底清空
                    if buffer and not in_tag:
                        yield f"data: {json.dumps({'text': buffer})}\n\n"
                    
                    current_ai_msg = event["data"]["output"]
                    if current_ai_msg and current_ai_msg.tool_calls:
                        # 处理工具调用逻辑 (保持原有逻辑)
                        for tool_call in current_ai_msg.tool_calls:
                            tool_desc = f"... (正在通过 {tool_call['name']} 深入调查)"
                            yield f"data: {json.dumps({'text': tool_desc})}\n\n"
                            if tool_call["name"] in analyzer.tools_map:
                                try:
                                    output = await asyncio.to_thread(analyzer.tools_map[tool_call["name"]].invoke, tool_call["args"])
                                    messages.append(current_ai_msg)
                                    messages.append(ToolMessage(content=str(output), tool_call_id=tool_call["id"]))
                                    # 注意：这里递归调用可能比较复杂，简单处理建议前端发起新一轮请求或在此继续迭代
                                except: pass

        except Exception as e:
            yield f"data: {json.dumps({'text': f'Error: {str(e)}'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.get("/history/{repo_id}")
async def get_chat_history(repo_id: int, session: Session = Depends(get_session)):
    # 暂时返回空，后续可以对接数据库存储的聊天历史
    return []
