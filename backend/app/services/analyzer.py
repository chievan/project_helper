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

# 增强型 DeepSeekChat 类，带详细调试日志
class DeepSeekChat(ChatOpenAI):
    def _convert_message_to_dict(self, message: BaseMessage) -> dict:
        dict_msg = super()._convert_message_to_dict(message)
        if isinstance(message, AIMessage):
            # 尝试从各种可能的字段读取思考过程
            reasoning = (
                message.additional_kwargs.get("reasoning_content") or 
                message.additional_kwargs.get("reasoning")
            )
            if reasoning:
                # 核心修复：强制注入 reasoning_content 到发往 API 的 payload 中
                dict_msg["reasoning_content"] = reasoning
                print(f"DEBUG: [Request] Injected reasoning_content (len: {len(reasoning)})")
        return dict_msg

    def _create_chat_result(self, *args: Any, **kwargs: Any) -> ChatResult:
        result = super()._create_chat_result(*args, **kwargs)
        # 获取原始响应对象
        response = args[0] if args else kwargs.get("response")
        
        if response and hasattr(response, "choices") and response.choices:
            raw_msg = response.choices[0].message
            # 诊断日志：打印 raw_msg 的结构
            # print(f"DEBUG: raw_msg type: {type(raw_msg)}")
            
            # 尝试多种方式提取思考过程
            reasoning = None
            if hasattr(raw_msg, "reasoning_content"):
                reasoning = raw_msg.reasoning_content
            elif isinstance(raw_msg, dict):
                reasoning = raw_msg.get("reasoning_content") or raw_msg.get("reasoning")
            
            if reasoning:
                print(f"DEBUG: [Response] Captured reasoning_content (len: {len(reasoning)})")
                # 将其保存到 AIMessage 的额外参数中，以便下一轮迭代使用
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
            
        print(f"Cloning {clone_url} into {self.local_path}...")
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
            
            iteration = 0
            max_iterations = 15
            while iteration < max_iterations:
                iteration += 1
                
                # 每次迭代都打印诊断信息
                print(f"DEBUG: --- Iteration {iteration} ---")
                for i, m in enumerate(messages):
                    if isinstance(m, AIMessage):
                        has_reasoning = "reasoning_content" in m.additional_kwargs
                        print(f"  Message {i} (AI): has_reasoning={has_reasoning}")

                chat_llm = DeepSeekChat(
                    model=settings.DEEPSEEK_ANALYSIS_MODEL,
                    openai_api_key=settings.DEEPSEEK_API_KEY,
                    openai_api_base=settings.DEEPSEEK_BASE_URL,
                    temperature=0
                ).bind_tools(list(self.tools_map.values()))
                
                res = await chat_llm.ainvoke(messages)
                messages.append(res)
                
                if not res.tool_calls:
                    break
                    
                for tool_call in res.tool_calls:
                    tool_name = tool_call["name"]
                    detail = tool_call['args'].get('file_path', tool_name)
                    print(f"🔍 正在深入解析: {detail}")
                    yield json.dumps({"type": "log", "message": f"🔍 正在深入解析: {detail}"})
                    
                    if tool_name in self.tools_map:
                        try:
                            args = tool_call["args"].copy()
                            if "repo_path" in args:
                                args["repo_path"] = self.local_path
                            
                            tool_output = await asyncio.to_thread(self.tools_map[tool_name].invoke, args)
                            messages.append(ToolMessage(content=str(tool_output), tool_call_id=tool_call["id"]))
                        except Exception as e:
                            messages.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call["id"]))
                
                yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0 + iteration * 2.0})

            yield json.dumps({"type": "log", "message": "🔍 调查结束，正在整合调查结果..."})
            yield json.dumps({"type": "status", "status": "generating_report", "progress": 95.0})
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
            if self.repo_id:
                db_repo = session.get(Repository, self.repo_id)
            else:
                statement = select(Repository).where(Repository.url == self.repo_url)
                db_repo = session.exec(statement).first()
                
            if db_repo:
                db_repo.analysis_report = report
                db_repo.status = "completed"
                db_repo.progress = 100.0
                db_repo.updated_at = datetime.utcnow()
                session.add(db_repo)
                session.commit()
