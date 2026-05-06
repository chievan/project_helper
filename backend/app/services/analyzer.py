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
        
        clone_url = self.repo_url
            
        print(f"Cloning {clone_url} into {self.local_path}...")
        # Add optimized git configs to prevent handshake timeouts (OpenSSL mode)
        Repo.clone_from(
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
                SystemMessage(content=f"""You are an expert software architect. Analyze the repository at {self.local_path}.
                
                Your goal is to provide a DEEP and COMPREHENSIVE technical analysis. 
                You MUST use the provided tools to explore the codebase thoroughly before answering.
                
                Steps:
                1. List files to see the overall structure.
                2. Read README.md and core configuration files (e.g., package.json, requirements.txt, pyproject.toml).
                3. Examine the main entry point and core business logic modules.
                4. Identify the tech stack and data flow.
                
                Your final response MUST be a detailed Markdown report with the following sections:
                # Project Analysis: {self.repo_name}
                
                ## 1. Executive Summary
                High-level overview of the project's purpose and value.
                
                ## 2. Technical Stack
                Detailed list of languages, frameworks, databases, and key libraries.
                
                ## 3. Architecture & Directory Structure
                Explanation of the project's organization and design patterns used.
                
                ## 4. Core Modules & Implementation Details
                Deep dive into the most important files and their responsibilities.
                
                ## 5. Data Flow & Integration
                How data moves through the system.
                
                ## 6. Developer Insights & Recommendations
                Suggestions for improvement or how to get started with the codebase.
                
                IMPORTANT: Every time you use a tool that requires 'repo_path', YOU MUST provide: '{self.local_path}'.
                CRITICAL: The final report MUST be written completely in Chinese (你的最终分析报告必须完全使用中文编写).
                """),
                HumanMessage(content="Perform a thorough investigation and generate the final comprehensive report in Chinese.")
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
                
                # Update progress incrementally within the 75% stage
                self.update_status("analyzing", 75.0 + (i + 1) * 1.5)

            # If we hit the max iterations and the last message is a ToolMessage, 
            # we need one final LLM call to synthesize the report.
            if isinstance(messages[-1], ToolMessage) or (isinstance(messages[-1], AIMessage) and messages[-1].tool_calls):
                messages.append(HumanMessage(content="You have reached the maximum number of tool calls. Please synthesize all the information gathered so far into the final Markdown report immediately without using any more tools. CRITICAL: The report MUST be written completely in Chinese (你的最终分析报告必须完全使用中文编写)."))
                final_msg = await self.llm.ainvoke(messages) # Use standard llm without tools bound to force a text response
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
