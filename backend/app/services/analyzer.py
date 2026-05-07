import os
import shutil
import json
from git import Repo
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage, AIMessage
from app.core.config import settings
from app.services.tools import list_files, read_file_content, search_code_snippet, web_search
from app.models.repo import Repository
from sqlmodel import Session, select
from app.core.db import engine
from datetime import datetime

import asyncio

class RepoAnalyzer:
    def __init__(self, repo_url: str):
        self.repo_url = repo_url
        self.repo_name = repo_url.split("/")[-1].replace(".git", "")
        self.owner = repo_url.split("/")[-2]
        
        # Resolve path relative to current working directory (server-friendly)
        self.local_path = os.path.abspath(os.path.join(settings.REPO_STORAGE_PATH, self.owner, self.repo_name))
        
        self.llm = ChatOpenAI(
            model="deepseek-chat",
            openai_api_key=settings.DEEPSEEK_API_KEY,
            openai_api_base=settings.DEEPSEEK_BASE_URL,
            temperature=0,
        )
        
        # Define tool mapping for manual execution
        self.tools_list = [list_files, read_file_content, search_code_snippet, web_search]
        self.llm_with_tools = self.llm.bind_tools(self.tools_list)
        self.tools_map = {tool.name: tool for tool in self.tools_list}
        
    def update_status(self, status: str, progress: float):
        print(f"Updating status: {status}, progress: {progress}%")
        with Session(engine) as session:
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
        
        # Use ghfast.top as a high-performance proxy for GitHub clones
        clone_url = self.repo_url
        if "github.com" in clone_url:
            clone_url = f"https://ghfast.top/{clone_url}"
            
        print(f"Cloning {clone_url} into {self.local_path}...")
        # Add optimized git configs to prevent handshake timeouts (OpenSSL mode)
        # Use asyncio.to_thread to run the blocking Repo.clone_from without hanging the event loop
        await asyncio.to_thread(
            Repo.clone_from,
            clone_url, 
            self.local_path, 
            depth=1,
            multi_options=[
                '--config http.postBuffer=524288000',
                '--config http.lowSpeedLimit=0',
                '--config http.lowSpeedTime=999999'
            ],
            allow_unsafe_options=True
        )
        self.update_status("analyzing", 50.0)
        return self.local_path

    async def analyze_stream(self):
        try:
            # 1. 初始状态
            yield json.dumps({"type": "status", "status": "cloning", "progress": 25.0})
            await self.clone_repo()
            
            yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0})
            
            messages = [
                SystemMessage(content=f"""你是一个顶级的软件架构师。你的任务是深度分析代码仓库，并生成一份专业分析报告。
                
                ### 核心原则：
                1. **降维打击**：禁止堆砌原始代码。用通俗的大白话解释。
                2. **结构化输出**：严密按照指定的 Markdown 模板输出。
                3. **深度侦查**：必须通过工具查看目录、配置和核心模块。
                
                CRITICAL: 你的报告必须完全使用中文编写。
                """),
                HumanMessage(content="请开始深度调查，并按照模板生成中文分析报告。")
            ]
            
            # 2. 调查阶段 (带日志输出)
            max_iterations = 15
            for i in range(max_iterations):
                ai_msg = await self.llm_with_tools.ainvoke(messages)
                messages.append(ai_msg)
                
                if not ai_msg.tool_calls:
                    break
                    
                for tool_call in ai_msg.tool_calls:
                    tool_name = tool_call["name"]
                    # 关键：实时告诉前端 AI 在干什么
                    detail = tool_call['args'].get('file_path', tool_name)
                    yield json.dumps({"type": "log", "message": f"🔍 正在深入解析: {detail}"})
                    
                    if tool_name in self.tools_map:
                        try:
                            tool_output = await asyncio.to_thread(self.tools_map[tool_name].invoke, tool_call["args"])
                            messages.append(ToolMessage(content=str(tool_output), tool_call_id=tool_call["id"]))
                        except Exception as e:
                            messages.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call["id"]))
                
                yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0 + (i + 1) * 2.0})

            # 3. 报告生成阶段 (流式 Token)
            yield json.dumps({"type": "status", "status": "completed", "progress": 90.0})
            messages.append(HumanMessage(content="调查结束。现在请立即按照模板输出最终的中文报告。"))
            
            final_report = ""
            async for chunk in self.llm.astream(messages):
                if chunk.content:
                    final_report += chunk.content
                    yield json.dumps({"type": "report_token", "text": chunk.content})

            # 4. 最终持久化
            self.save_final_report(final_report)
            yield json.dumps({"type": "status", "status": "completed", "progress": 100.0})

        except Exception as e:
            error_msg = str(e)
            print(f"Analysis failed: {error_msg}")
            self.update_status(f"failed: {error_msg}", 0.0)
            yield json.dumps({"type": "error", "message": error_msg})

    def save_final_report(self, report: str):
        with Session(engine) as session:
            statement = select(Repository).where(Repository.url == self.repo_url)
            db_repo = session.exec(statement).first()
            if db_repo:
                db_repo.analysis_report = report
                db_repo.status = "completed"
                db_repo.progress = 100.0
                db_repo.updated_at = datetime.utcnow()
                session.add(db_repo)
                session.commit()
