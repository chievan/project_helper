import os
import shutil
import json
import asyncio
from datetime import datetime
from typing import Optional, Any
from git import Repo
from sqlmodel import Session, select

from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, ToolMessage, AIMessage
from langchain_core.outputs import ChatResult

from app.core.config import settings
from app.services.tools import list_files, read_file_content, search_code_snippet, web_search
from app.models.repo import Repository
from app.core.db import engine

# 自定义 ChatOpenAI 类以兼容 DeepSeek 的 reasoning_content 字段
class DeepSeekChat(ChatOpenAI):
    def _convert_message_to_dict(self, message: BaseMessage) -> dict:
        dict_msg = super()._convert_message_to_dict(message)
        if isinstance(message, AIMessage):
            # 优先从 additional_kwargs 中读取
            reasoning = message.additional_kwargs.get("reasoning_content") or message.additional_kwargs.get("reasoning")
            if reasoning:
                dict_msg["reasoning_content"] = reasoning
        return dict_msg

    def _create_chat_result(self, *args: Any, **kwargs: Any) -> ChatResult:
        result = super()._create_chat_result(*args, **kwargs)
        # 第一个位置参数通常是 response
        response = args[0] if args else kwargs.get("response")
        
        # 核心修复：从原始响应中提取推理内容并持久化到 AIMessage 对象中
        if response and hasattr(response, "choices") and response.choices:
            raw_msg = response.choices[0].message
            # 兼容官方 SDK 对象或字典格式
            reasoning = None
            if hasattr(raw_msg, "reasoning_content"):
                reasoning = raw_msg.reasoning_content
            elif isinstance(raw_msg, dict):
                reasoning = raw_msg.get("reasoning_content")
            
            if reasoning:
                print(f"DEBUG: Captured reasoning_content, length: {len(reasoning)}")
                result.generations[0].message.additional_kwargs["reasoning_content"] = reasoning
        return result

class RepoAnalyzer:
    def __init__(self, repo_url: str, repo_id: int = None):
        self.repo_url = repo_url
        self.repo_id = repo_id
        self.repo_name = repo_url.split("/")[-1].replace(".git", "")
        self.owner = repo_url.split("/")[-2]
        
        # Resolve path relative to current working directory (server-friendly)
        self.local_path = os.path.abspath(os.path.join(settings.REPO_STORAGE_PATH, self.owner, self.repo_name))
        
        self.llm = DeepSeekChat(
            model=settings.DEEPSEEK_MODEL,
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
            repo = session.get(Repository, self.repo_id)
            if repo:
                repo.status = status
                repo.progress = progress
                session.add(repo)
                session.commit()

    async def analyze_stream(self):
        try:
            # 1. 克隆/更新仓库
            if os.path.exists(self.local_path):
                shutil.rmtree(self.local_path)
            
            os.makedirs(os.path.dirname(self.local_path), exist_ok=True)
            yield json.dumps({"type": "status", "status": "cloning", "progress": 25.0})
            
            # 使用加速地址
            clone_url = self.repo_url.replace("https://github.com/", "https://ghfast.top/https://github.com/")
            log_msg = f"Cloning {clone_url} into {self.local_path}..."
            print(log_msg)
            yield json.dumps({"type": "log", "message": log_msg})
            
            await asyncio.to_thread(Repo.clone_from, clone_url, self.local_path)
            
            yield json.dumps({"type": "status", "status": "analyzing", "progress": 50.0})
            
            messages = [
                SystemMessage(content=f"你是一个资深的架构分析师。你正在分析项目: {self.repo_name}。你可以使用提供的工具来探索代码库。"),
                HumanMessage(content="请开始深度调查，并按照模板生成中文分析报告。")
            ]
            
            # 2. 深度分析循环 (使用标准模型进行工具调用，保证稳定性)
            iteration = 0
            max_iterations = 15
            while iteration < max_iterations:
                iteration += 1
                
                # 工具调用阶段使用指定的分析模型
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
                    log_msg = f"🔍 正在深入解析: {detail}"
                    print(log_msg)
                    yield json.dumps({"type": "log", "message": log_msg})
                    
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

            # 3. 报告生成阶段 (使用 Pro 模型进行深度整合)
            yield json.dumps({"type": "log", "message": "🔍 调查结束，正在使用 Pro 模型整合调查结果..."})
            yield json.dumps({"type": "status", "status": "generating_report", "progress": 95.0})
            messages.append(HumanMessage(content="调查结束。现在请立即按照模板输出最终的中文报告。"))
            
            final_report = ""
            # 此处 self.llm 已经是 settings.DEEPSEEK_MODEL (即 v4-pro)
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
            repo = session.get(Repository, self.repo_id)
            if repo:
                repo.analysis_report = report
                repo.status = "completed"
                repo.progress = 100.0
                repo.updated_at = datetime.utcnow()
                session.add(repo)
                session.commit()
