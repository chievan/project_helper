from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from app.core.db import get_session
from app.models.repo import Repository
from app.models.chat import ChatMessage
from app.services.analyzer import RepoAnalyzer
from pydantic import BaseModel
import json
import asyncio
from datetime import datetime
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage, AIMessage

router = APIRouter()

class ChatRequest(BaseModel):
    repo_id: int
    message: str

@router.get("/history/{repo_id}")
async def get_chat_history(repo_id: int, session: Session = Depends(get_session)):
    statement = select(ChatMessage).where(ChatMessage.repo_id == repo_id).order_by(ChatMessage.created_at.asc())
    history = session.exec(statement).all()
    return history

@router.post("/ask")
async def ask_question(data: ChatRequest, session: Session = Depends(get_session)):
    db_repo = session.get(Repository, data.repo_id)
    if not db_repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    
    # 1. Save user message
    user_msg = ChatMessage(repo_id=data.repo_id, role="user", content=data.message)
    session.add(user_msg)
    session.commit()
    
    analyzer = RepoAnalyzer(db_repo.url)
    
    # 2. Get history
    history_statement = select(ChatMessage).where(ChatMessage.repo_id == data.repo_id).order_by(ChatMessage.created_at.desc()).limit(11)
    history_msgs = session.exec(history_statement).all()
    history_msgs.reverse()
    
    async def event_generator():
        messages = [
            SystemMessage(content=f"""You are an expert assistant for the codebase: {db_repo.name}. 
            The repository is located at {analyzer.local_path}.
            
            PROJECT ANALYSIS REPORT:
            {db_repo.analysis_report}
            
            Use the report and history to answer. Answer in Chinese. Be concise.
            CRITICAL: ALWAYS respond in Chinese.
            """)
        ]
        
        for m in history_msgs[:-1]:
            if m.role == "user":
                messages.append(HumanMessage(content=m.content))
            else:
                messages.append(AIMessage(content=m.content))
        messages.append(HumanMessage(content=data.message))
        
        final_answer = ""
        max_iterations = 5
        
        for i in range(max_iterations):
            # Use astream_events for granular tool and token control
            tool_calls = []
            current_ai_msg = None
            
            async for event in analyzer.llm_with_tools.astream_events(messages, version="v2"):
                kind = event["event"]
                
                if kind == "on_chat_model_stream":
                    content = event["data"]["chunk"].content
                    if content:
                        final_answer += content
                        yield f"data: {json.dumps({'text': content})}\n\n"
                
                elif kind == "on_chat_model_end":
                    current_ai_msg = event["data"]["output"]
            
            if current_ai_msg and current_ai_msg.tool_calls:
                messages.append(current_ai_msg)
                for tool_call in current_ai_msg.tool_calls:
                    tool_desc = f"... (using {tool_call['name']})"
                    yield f"data: {json.dumps({'text': tool_desc})}\n\n"
                    if tool_call["name"] in analyzer.tools_map:
                        try:
                            output = analyzer.tools_map[tool_call["name"]].invoke(tool_args := tool_call["args"])
                            messages.append(ToolMessage(content=str(output), tool_call_id=tool_call["id"]))
                        except Exception as e:
                            messages.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call["id"]))
            else:
                break
        else:
            # Final fallback if tools loop exhausted
            async for chunk in analyzer.llm.astream(messages):
                final_answer += chunk.content
                yield f"data: {json.dumps({'text': chunk.content})}\n\n"

        # 3. Save assistant response
        from app.core.db import engine
        with Session(engine) as fresh_session:
            assistant_msg = ChatMessage(repo_id=data.repo_id, role="assistant", content=final_answer)
            fresh_session.add(assistant_msg)
            fresh_session.commit()

    return StreamingResponse(event_generator(), media_type="text/event-stream")
