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

class DeepSeekChat(ChatOpenAI):
    def _convert_message_to_dict(self, message: BaseMessage) -> dict:
        dict_msg = super()._convert_message_to_dict(message)
        if isinstance(message, AIMessage):
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning: dict_msg["reasoning_content"] = reasoning
        return dict_msg

class RepoAnalyzer:
    def __init__(self, repo_url: str, repo_id: int = None):
        self.repo_url = repo_url
        self.repo_id = repo_id
        self.repo_name = repo_url.split("/")[-1].replace(".git", "")
        self.owner = repo_url.split("/")[-2]
        self.local_path = os.path.abspath(os.path.join(settings.REPO_STORAGE_PATH, self.owner, self.repo_name))
        
        self.llm = DeepSeekChat(
            model=settings.DEEPSEEK_MODEL,
            openai_api_key=settings.DEEPSEEK_API_KEY,
            openai_api_base=settings.DEEPSEEK_BASE_URL,
            temperature=0.3,
            extra_body={"thinking": {"type": "disabled"}}
        )
        self.tools_list = [list_files, read_file_content, search_code_snippet, web_search]
        self.llm_with_tools = self.llm.bind_tools(self.tools_list) # 恢复这个关键属性
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
                SystemMessage(content=f"""你是一个顶级的“技术转大白话”翻译专家。你的任务是分析代码仓库: {self.local_path}。
                
                ### 终极目标：
                生成的报告必须让“完全不懂代码的傻瓜”也能看懂这个项目是干什么的、怎么实现的。
                
                ### 报告板块：项目概述、技术栈、目录结构、核心模块、数据流、设计模式、阅读建议。
                CRITICAL: 必须使用中文。
                """),
                HumanMessage(content="请开始深度调查。")
            ]
            
            iteration = 0
            while iteration < 12:
                iteration += 1
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
                    yield json.dumps({"type": "log", "message": f"🔍 正在执行: {tool_call['name']}"})
                    if tool_call["name"] in self.tools_map:
                        args = tool_call["args"].copy()
                        if "repo_path" in args: args["repo_path"] = self.local_path
                        out = await asyncio.to_thread(self.tools_map[tool_call["name"]].invoke, args)
                        messages.append(ToolMessage(content=str(out), tool_call_id=tool_call["id"]))
                
                yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0 + iteration * 3.0})

            yield json.dumps({"type": "log", "message": "🔍 调查结束，生成最终报告..."})
            # 强化最后的 Prompt，严禁它再想调用工具
            messages.append(HumanMessage(content="""调查环节已彻底结束，严禁调用任何工具或输出任何 < | DSML | > 标签！
            请根据目前的调查结果，直接输出那份「傻子也能懂」的完整中文分析报告。
            报告结构：项目概述、技术栈、目录结构、核心模块、数据流、设计模式、阅读建议。
            使用生动的 Markdown 格式，多用比喻！"""))
            
            final_report = ""
            async for chunk in self.llm.astream(messages):
                if chunk.content:
                    # 最后的兜底逻辑：如果它还是吐出了标签，直接过滤掉（防止前端炸裂）
                    cleaned_content = chunk.content.replace("< | DSML | tool_calls >", "").replace("< | DSML |", "").replace("| >", "")
                    final_report += cleaned_content
                    yield json.dumps({"type": "report_token", "text": cleaned_content})
            
            self.save_final_report(final_report)
            yield json.dumps({"type": "status", "status": "completed", "progress": 100.0})

        except Exception as e:
            print(f"Analysis failed: {str(e)}")
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
