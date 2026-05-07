import os
import shutil
import json
import asyncio
from git import Repo
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, ToolMessage, AIMessage
from langchain_core.outputs import ChatResult
from app.core.config import settings
from app.services.tools import list_files, read_file_content, search_code_snippet, web_search
from app.models.repo import Repository
from sqlmodel import Session, select
from app.core.db import engine
from datetime import datetime
from typing import Optional, Any, List, Dict

# 针对 langchain-openai 0.3.0 深度定制的 DeepSeekChat
class DeepSeekChat(ChatOpenAI):
    # 终极出口封堵：直接修改发往 API 的最终参数字典
    def _convert_messages_to_params(self, messages: List[BaseMessage], **kwargs: Any) -> Dict[str, Any]:
        params = super()._convert_messages_to_params(messages, **kwargs)
        
        # 遍历消息，强制将思考过程注入对应的 API 字典中
        for i, m in enumerate(messages):
            if isinstance(m, AIMessage):
                reasoning = (
                    m.additional_kwargs.get("reasoning_content") or 
                    m.additional_kwargs.get("reasoning")
                )
                if reasoning:
                    # params["messages"] 对应的是发给 OpenAI SDK 的 dict 列表
                    params["messages"][i]["reasoning_content"] = reasoning
                    print(f"DEBUG: [Protocol] >>> FORCED reasoning_content into payload (len: {len(reasoning)})")
        
        return params

    def _create_chat_result(self, *args: Any, **kwargs: Any) -> ChatResult:
        result = super()._create_chat_result(*args, **kwargs)
        response = args[0] if args else kwargs.get("response")
        
        if response and hasattr(response, "choices") and response.choices:
            raw_msg = response.choices[0].message
            # 兼容对象式和字典式响应
            reasoning = getattr(raw_msg, "reasoning_content", None)
            if not reasoning and isinstance(raw_msg, dict):
                reasoning = raw_msg.get("reasoning_content")
            
            if reasoning:
                print(f"DEBUG: [Protocol] <<< CAPTURED reasoning_content (len: {len(reasoning)})")
                result.generations[0].message.additional_kwargs["reasoning_content"] = reasoning
        return result

class RepoAnalyzer:
    def __init__(self, repo_url: str, repo_id: int = None):
        self.repo_url = repo_url
        self.repo_id = repo_id
        self.repo_name = repo_url.split("/")[-1].replace(".git", "")
        self.owner = repo_url.split("/")[-2]
        self.local_path = os.path.abspath(os.path.join(settings.REPO_STORAGE_PATH, self.owner, self.repo_name))
        
        # 显式参数传递
        self.llm = DeepSeekChat(
            model=settings.DEEPSEEK_MODEL,
            openai_api_key=settings.DEEPSEEK_API_KEY,
            openai_api_base=settings.DEEPSEEK_BASE_URL,
            temperature=0,
            extra_body={"thinking": {"type": "enabled"}},
            reasoning_effort="high"
        )
        self.tools_list = [list_files, read_file_content, search_code_snippet, web_search]
        self.tools_map = {tool.name: tool for tool in self.tools_list}
        
    def update_status(self, status: str, progress: float):
        print(f"Updating status: {status}, progress: {progress}%")
        with Session(engine) as session:
            if self.repo_id:
                db_repo = session.get(Repository, self.repo_id)
            else:
                statement = select(Repository).where(Repository.url == self.repo_url)
                db_repo = session.exec(statement).first()
            if db_repo:
                db_repo.status = status
                db_repo.progress = progress
                db_repo.updated_at = datetime.utcnow()
                session.add(db_repo)
                session.commit()

    async def clone_repo(self):
        self.update_status("cloning", 25.0)
        if os.path.exists(self.local_path):
            shutil.rmtree(self.local_path)
        os.makedirs(os.path.dirname(self.local_path), exist_ok=True)
        clone_url = self.repo_url
        if "github.com" in clone_url:
            clone_url = f"https://ghfast.top/{clone_url}"
        print(f"Cloning {clone_url}...")
        await asyncio.to_thread(
            Repo.clone_from, clone_url, self.local_path, depth=1,
            multi_options=['--config http.postBuffer=524288000', '--config http.lowSpeedLimit=0', '--config http.lowSpeedTime=999999'],
            allow_unsafe_options=True
        )
        self.update_status("analyzing", 50.0)
        return self.local_path

    async def analyze_stream(self):
        try:
            yield json.dumps({"type": "status", "status": "cloning", "progress": 25.0})
            await self.clone_repo()
            yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0})
            
            messages = [
                SystemMessage(content=f"你是一个软件架构师。请深度分析本地代码仓库: {self.local_path}"),
                HumanMessage(content="请开始深度调查，必须先调用 list_files 查看结构。")
            ]
            
            iteration = 0
            while iteration < 15:
                iteration += 1
                print(f"DEBUG: === Iteration {iteration} ===")
                
                chat_llm = DeepSeekChat(
                    model=settings.DEEPSEEK_ANALYSIS_MODEL,
                    openai_api_key=settings.DEEPSEEK_API_KEY,
                    openai_api_base=settings.DEEPSEEK_BASE_URL,
                    temperature=0,
                    extra_body={"thinking": {"type": "enabled"}},
                    reasoning_effort="high"
                ).bind_tools(list(self.tools_map.values()))
                
                res = await chat_llm.ainvoke(messages)
                messages.append(res)
                
                if not res.tool_calls:
                    break
                    
                for tool_call in res.tool_calls:
                    tool_name = tool_call["name"]
                    print(f"🔍 正在执行: {tool_name}")
                    yield json.dumps({"type": "log", "message": f"🔍 正在执行: {tool_name}"})
                    
                    if tool_name in self.tools_map:
                        try:
                            args = tool_call["args"].copy()
                            if "repo_path" in args: args["repo_path"] = self.local_path
                            tool_output = await asyncio.to_thread(self.tools_map[tool_name].invoke, args)
                            messages.append(ToolMessage(content=str(tool_output), tool_call_id=tool_call["id"]))
                        except Exception as e:
                            messages.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call["id"]))
                
                yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0 + iteration * 2.0})

            yield json.dumps({"type": "log", "message": "🔍 整合结果..."})
            messages.append(HumanMessage(content="报告输出阶段。"))
            final_report = ""
            async for chunk in self.llm.astream(messages):
                if chunk.content:
                    final_report += chunk.content
                    yield json.dumps({"type": "report_token", "text": chunk.content})
            self.save_final_report(final_report)
            yield json.dumps({"type": "status", "status": "completed", "progress": 100.0})

        except Exception as e:
            print(f"Analysis failed: {str(e)}")
            self.update_status(f"failed: {str(e)}", 0.0)
            yield json.dumps({"type": "error", "message": str(e)})

    def save_final_report(self, report: str):
        with Session(engine) as session:
            statement = select(Repository).where(Repository.url == self.repo_url)
            db_repo = session.exec(statement).first()
            if db_repo:
                db_repo.analysis_report = report
                db_repo.status = "completed"
                db_repo.updated_at = datetime.utcnow()
                session.add(db_repo)
                session.commit()
