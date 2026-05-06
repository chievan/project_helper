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

    async def analyze(self):
        try:
            await self.clone_repo()
            
            messages = [
                SystemMessage(content=f"""你是一个顶级的软件架构师和技术布道者。
                你的任务是深度分析位于 {self.local_path} 的代码仓库，并生成一份“傻子都能看懂”的专业分析报告。
                
                ### 核心原则：
                1. **降维打击**：禁止堆砌原始代码。请用通俗的比喻和简洁的语言解释复杂的架构，做到即使技术新人也能秒懂。
                2. **结构化输出**：严密按照指定的 Markdown 模板输出。
                3. **深度侦查**：必须先查看目录（list_files），再读取配置（README/package.json/requirements.txt等），最后剖析核心逻辑模块。
                
                ### 最终报告模板（必须严格遵守）：
                # 🚀 项目全维度分析报告：{self.repo_name}
                
                ## 1. 项目“大白话”概述
                （用一两句话说清楚这个项目是干嘛的，它解决了什么痛点，价值在哪里）
                
                ## 2. 技术“兵器谱” (Tech Stack)
                （清晰列出编程语言、核心框架、数据库、以及该项目依赖的最关键的 3-5 个库）
                
                ## 3. 房子是怎么盖的 (Architecture)
                （用比喻或简单逻辑解释目录结构的组织方式，以及采用了什么设计模式（如 MVC, 微服务, 插件化等））
                
                ## 4. 核心“零部件”解析 (Core Modules)
                （挑选最关键的几个文件或文件夹，说明它们在系统里分别扮演什么“器官”角色）
                
                ## 5. 数据是怎么跑的 (Data Flow)
                （描述一个核心业务流程（如用户请求或任务处理）是如何在代码间流转的）
                
                ## 6. 专家级阅读建议
                （给新人的“藏宝图”：如果我要快速上手，我该按什么顺序去读代码？）
                
                ## 7. 亮点与改进建议
                （总结项目的闪光点，以及从架构师角度看可以优化的地方）
                
                IMPORTANT: 你必须通过调用工具来获取真实信息。严禁胡编乱造。
                CRITICAL: 你的最终分析报告必须完全使用中文编写 (The entire report must be in Chinese).
                """),
                HumanMessage(content="请开始深度调查，并按照模板生成那份通俗易懂的中文分析报告。")
            ]
            
            self.update_status("analyzing", 75.0)
            
            # Manual tool execution loop
            max_iterations = 15
            for i in range(max_iterations):
                ai_msg = await self.llm_with_tools.ainvoke(messages)
                messages.append(ai_msg)
                
                if not ai_msg.tool_calls:
                    break
                    
                # Execute tools
                for tool_call in ai_msg.tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call["args"]
                    
                    print(f"Executing tool: {tool_name} with args: {tool_args}")
                    
                    if tool_name in self.tools_map:
                        tool_output = self.tools_map[tool_name].invoke(tool_args)
                        messages.append(ToolMessage(content=str(tool_output), tool_call_id=tool_call["id"]))
                    else:
                        messages.append(ToolMessage(content=f"Error: Tool {tool_name} not found.", tool_call_id=tool_call["id"]))
                
                self.update_status("analyzing", 75.0 + (i + 1) * 1.5)

            # Synthesize final report
            if isinstance(messages[-1], ToolMessage) or (isinstance(messages[-1], AIMessage) and messages[-1].tool_calls):
                messages.append(HumanMessage(content="你已经完成了调查。现在请立即根据你收集到的所有信息，按照要求的 Markdown 模板撰写那份通俗易懂的中文报告。禁止再使用工具。"))
                final_msg = await self.llm.ainvoke(messages)
                report = final_msg.content
            else:
                report = messages[-1].content
            
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
            
            return report
            
        except Exception as e:
            print(f"Analysis failed: {str(e)}")
            self.update_status(f"failed: {str(e)}", 0.0)
            raise e
