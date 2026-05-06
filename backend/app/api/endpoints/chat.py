from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from app.core.db import get_session
from app.models.repo import Repository
from app.services.analyzer import RepoAnalyzer
from pydantic import BaseModel
import json
import asyncio

router = APIRouter()

class ChatRequest(BaseModel):
    repo_id: int
    message: str

@router.post("/ask")
async def ask_question(data: ChatRequest, session: Session = Depends(get_session)):
    db_repo = session.get(Repository, data.repo_id)
    if not db_repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    
    analyzer = RepoAnalyzer(db_repo.url)
    
    async def event_generator():
        from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
        
        messages = [
            SystemMessage(content=f"""You are an expert assistant for the codebase: {db_repo.name}. 
            The repository is located at {analyzer.local_path}.
            
            PROJECT ANALYSIS REPORT:
            {db_repo.analysis_report}
            
            Use the above report to answer the user's question. If the report doesn't contain enough detail, use tools to explore the code.
            
            SPEED GUIDELINES:
            1. If you already have enough information from the report or previous tools, ANSWER IMMEDIATELY.
            2. Do NOT use tools if not strictly necessary. 
            3. Be concise and professional.
            
            IMPORTANT: Every time you use a tool that requires 'repo_path', YOU MUST provide: '{analyzer.local_path}'.
            CRITICAL: You MUST answer the user's questions completely in Chinese (你的所有回答必须完全使用中文).
            """),
            HumanMessage(content=data.message)
        ]
        
        # Robust manual tool loop for chat - optimized for speed
        max_iterations = 5
        final_answer = "I'm sorry, I couldn't find a definitive answer after exploring the codebase."
        
        for i in range(max_iterations):
            ai_msg = await analyzer.llm_with_tools.ainvoke(messages)
            messages.append(ai_msg)
            
            if not ai_msg.tool_calls:
                final_answer = ai_msg.content
                break
                
            # Execute tools
            for tool_call in ai_msg.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                
                if tool_name in analyzer.tools_map:
                    try:
                        tool_output = analyzer.tools_map[tool_name].invoke(tool_args)
                        messages.append(ToolMessage(content=str(tool_output), tool_call_id=tool_call["id"]))
                    except Exception as e:
                        messages.append(ToolMessage(content=f"Error executing tool: {str(e)}", tool_call_id=tool_call["id"]))
                else:
                    messages.append(ToolMessage(content=f"Error: Tool {tool_name} not found.", tool_call_id=tool_call["id"]))
            
            # Send a heartbeat or intermediate update if needed
            yield f"data: {json.dumps({'text': '... (analyzing code)'})}\n\n"
            await asyncio.sleep(0.1)

        else:
            # This 'else' block runs if the for loop finishes without 'break'
            messages.append(HumanMessage(content="You have reached the maximum number of tool calls. Please provide your best answer based on the information gathered so far without using any more tools."))
            final_msg = await analyzer.llm.ainvoke(messages)
            final_answer = final_msg.content

        yield f"data: {json.dumps({'text': final_answer})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
