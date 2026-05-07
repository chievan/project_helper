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
    
    # 1. Save user message to database
    user_msg = ChatMessage(repo_id=data.repo_id, role="user", content=data.message)
    session.add(user_msg)
    session.commit()
    
    analyzer = RepoAnalyzer(db_repo.url)
    
    # 2. Get recent history for context (last 10 messages)
    history_statement = select(ChatMessage).where(ChatMessage.repo_id == data.repo_id).order_by(ChatMessage.created_at.desc()).limit(11) # include current
    history_msgs = session.exec(history_statement).all()
    history_msgs.reverse() # Back to chronological order
    
    async def event_generator():
        messages = [
            SystemMessage(content=f"""You are an expert assistant for the codebase: {db_repo.name}. 
            The repository is located at {analyzer.local_path}.
            
            PROJECT ANALYSIS REPORT:
            {db_repo.analysis_report}
            
            Use the above report and the conversation history to answer. If detail is missing, use tools.
            
            SPEED GUIDELINES:
            1. If you have enough info, ANSWER IMMEDIATELY.
            2. Be concise.
            
            CRITICAL: You MUST answer the user's questions completely in Chinese (你的所有回答必须完全使用中文).
            """)
        ]
        
        # Add history to LangChain messages
        for m in history_msgs[:-1]: # exclude the one we just added as it will be the HumanMessage
            if m.role == "user":
                messages.append(HumanMessage(content=m.content))
            else:
                messages.append(AIMessage(content=m.content))
        
        messages.append(HumanMessage(content=data.message))
        
        max_iterations = 5
        final_answer = ""
        
        for i in range(max_iterations):
            ai_msg = await analyzer.llm_with_tools.ainvoke(messages)
            messages.append(ai_msg)
            
            if not ai_msg.tool_calls:
                final_answer = ai_msg.content
                break
                
            for tool_call in ai_msg.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                
                if tool_name in analyzer.tools_map:
                    try:
                        tool_output = analyzer.tools_map[tool_name].invoke(tool_args)
                        messages.append(ToolMessage(content=str(tool_output), tool_call_id=tool_call["id"]))
                    except Exception as e:
                        messages.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call["id"]))
                else:
                    messages.append(ToolMessage(content=f"Error: Tool {tool_name} not found.", tool_call_id=tool_call["id"]))
            
            yield f"data: {json.dumps({'text': '... (thinking)'})}\n\n"
            await asyncio.sleep(0.1)
        else:
            final_msg = await analyzer.llm.ainvoke(messages)
            final_answer = final_msg.content

        # 3. Save assistant message to database
        with Session(analyzer.tools_map['list_files']._engine) if hasattr(analyzer.tools_map['list_files'], '_engine') else session as save_session:
            # Note: We need a fresh session or use the one from depends carefully in the generator
            from app.core.db import engine
            with Session(engine) as fresh_session:
                assistant_msg = ChatMessage(repo_id=data.repo_id, role="assistant", content=final_answer)
                fresh_session.add(assistant_msg)
                fresh_session.commit()

        yield f"data: {json.dumps({'text': final_answer})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
