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
from typing import Optional, Any

# 保持极简的兼容类，作为最后的安全兜底
class DeepSeekChat(ChatOpenAI):
    def _convert_message_to_dict(self, message: BaseMessage) -> dict:
        dict_msg = super()._convert_message_to_dict(message)
        if isinstance(message, AIMessage):
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning:
                dict_msg["reasoning_content"] = reasoning
        return dict_msg

class RepoAnalyzer:
    def __init__(self, repo_url: str, repo_id: int = None):
        self.repo_url = repo_url
        self.repo_id = repo_id
        self.repo_name = repo_url.split("/")[-1].replace(".git", "")
        self.owner = repo_url.split("/")[-2]
        self.local_path = os.path.abspath(os.path.join(settings.REPO_STORAGE_PATH, self.owner, self.repo_name))
        
        # 核心：通过 extra_body 显式禁用思考模式，彻底解决 400 报错
        self.llm = DeepSeekChat(
            model=settings.DEEPSEEK_MODEL,
            openai_api_key=settings.DEEPSEEK_API_KEY,
            openai_api_base=settings.DEEPSEEK_BASE_URL,
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}}
        )
        self.tools_list = [list_files, read_file_content, search_code_snippet, web_search]
        self.tools_map = {tool.name: tool for tool in self.tools_list}
        
    def update_status(self, status: str, progress: float):
        print(f"Updating status: {status}, progress: {progress}%")
        with Session(engine) as session:
            statement = select(Repository).where(Repository.url == self.repo_url)
            db_repo = session.exec(statement).first()
            if db_repo:
                db_repo.status = status
                db_repo.progress = progress
                session.add(db_repo)
                session.commit()

    async def clone_repo(self):
        self.update_status("cloning", 25.0)
        if os.path.exists(self.local_path): shutil.rmtree(self.local_path)
        os.makedirs(os.path.dirname(self.local_path), exist_ok=True)
        clone_url = f"https://ghfast.top/{self.repo_url}" if "github.com" in self.repo_url else self.repo_url
        await asyncio.to_thread(Repo.clone_from, clone_url, self.local_path, depth=1)
        self.update_status("analyzing", 50.0)

    async def analyze_stream(self):
        try:
            yield json.dumps({"type": "status", "status": "cloning", "progress": 25.0})
            await self.clone_repo()
            yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0})
            
            messages = [
                SystemMessage(content=f"你是一个顶级的软件架构师。请深度分析代码仓库: {self.local_path}。请务必使用工具进行调查。"),
                HumanMessage(content="请开始深度调查。")
            ]
            
            iteration = 0
            while iteration < 15:
                iteration += 1
                
                # 在循环中也显式禁用思考模式
                chat_llm = DeepSeekChat(
                    model=settings.DEEPSEEK_ANALYSIS_MODEL,
                    openai_api_key=settings.DEEPSEEK_API_KEY,
                    openai_api_base=settings.DEEPSEEK_BASE_URL,
                    temperature=0,
                    extra_body={"thinking": {"type": "disabled"}}
                ).bind_tools(list(self.tools_map.values()))
                
                res = await chat_llm.ainvoke(messages)
                messages.append(res)
                
                if not res.tool_calls: break
                for tool_call in res.tool_calls:
                    print(f"🔍 正在执行工具: {tool_call['name']}")
                    yield json.dumps({"type": "log", "message": f"🔍 解析: {tool_call['name']}"})
                    if tool_call["name"] in self.tools_map:
                        args = tool_call["args"].copy()
                        if "repo_path" in args: args["repo_path"] = self.local_path
                        out = await asyncio.to_thread(self.tools_map[tool_call["name"]].invoke, args)
                        messages.append(ToolMessage(content=str(out), tool_call_id=tool_call["id"]))
                
                yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0 + iteration * 2.0})

            yield json.dumps({"type": "log", "message": "🔍 调查结束，整合结果..."})
            messages.append(HumanMessage(content="调查结束。现在请输出最终报告。"))
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
                db_repo.progress = 100.0
                session.add(db_repo)
                session.commit()
