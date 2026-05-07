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

# 官方协议完全兼容类
class DeepSeekChat(ChatOpenAI):
    def _convert_message_to_dict(self, message: BaseMessage) -> dict:
        dict_msg = super()._convert_message_to_dict(message)
        if isinstance(message, AIMessage):
            reasoning = (
                message.additional_kwargs.get("reasoning_content") or 
                message.additional_kwargs.get("reasoning")
            )
            if reasoning:
                dict_msg["reasoning_content"] = reasoning
                print(f"DEBUG: [Request] >>> INJECTED reasoning_content (len: {len(reasoning)})")
        return dict_msg

    def _create_chat_result(self, *args: Any, **kwargs: Any) -> ChatResult:
        result = super()._create_chat_result(*args, **kwargs)
        response = args[0] if args else kwargs.get("response")
        if response and hasattr(response, "choices") and response.choices:
            raw_msg = response.choices[0].message
            reasoning = getattr(raw_msg, "reasoning_content", None)
            if not reasoning and isinstance(raw_msg, dict):
                reasoning = raw_msg.get("reasoning_content")
            if reasoning:
                print(f"DEBUG: [Response] <<< CAPTURED reasoning_content (len: {len(reasoning)})")
                result.generations[0].message.additional_kwargs["reasoning_content"] = reasoning
        return result

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
            temperature=0,
            # 显式传入以消除 Warning
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
            
            # 强化提示词：明确告知路径，强制要求使用工具
            messages = [
                SystemMessage(content=f"""你是一个顶级的软件架构师。你的任务是分析当前已经克隆到本地的代码仓库。
                仓库本地绝对路径为: {self.local_path}
                
                ### 核心指令：
                1. **必须使用工具**：你必须先调用 `list_files` 查看目录结构，然后读取核心代码文件。
                2. **降维打击**：用专业且通俗的中文解释架构，禁止大段粘贴代码。
                3. **严禁犯懒**：不要告诉我你没有路径，路径就在上面。立即开始探测！
                """),
                HumanMessage(content=f"请立即开始对路径 {self.local_path} 进行深度调查，并输出中文分析报告。")
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
                    # 如果模型还是不调用工具，强行踢它一脚
                    if iteration == 1:
                        messages.append(HumanMessage(content="你还没有调用任何工具！请先使用 list_files 查看目录结构。"))
                        continue
                    break
                    
                for tool_call in res.tool_calls:
                    tool_name = tool_call["name"]
                    print(f"🔍 正在深入解析: {tool_call['args'].get('file_path', tool_name)}")
                    yield json.dumps({"type": "log", "message": f"🔍 正在深入解析: {tool_call['args'].get('file_path', tool_name)}"})
                    
                    if tool_name in self.tools_map:
                        try:
                            args = tool_call["args"].copy()
                            if "repo_path" in args: args["repo_path"] = self.local_path
                            tool_output = await asyncio.to_thread(self.tools_map[tool_name].invoke, args)
                            messages.append(ToolMessage(content=str(tool_output), tool_call_id=tool_call["id"]))
                        except Exception as e:
                            messages.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call["id"]))
                
                yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0 + iteration * 2.0})

            yield json.dumps({"type": "log", "message": "🔍 调查结束，整合结果..."})
            messages.append(HumanMessage(content="调查结束。现在请立即按照模板输出最终的中文报告。"))
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
