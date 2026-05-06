# Project Helper AI - 智能仓库分析助手

Project Helper AI 是一款基于 DeepSeek 大模型驱动的开源仓库分析工具。它能帮助开发者快速理解任何复杂的 GitHub 仓库，自动生成架构报告，并支持针对源代码的交互式问答。

![UI Preview](https://img.shields.io/badge/UI-Modern_Dark-blueviolet)
![License](https://img.shields.io/badge/License-MIT-green)

## 🌟 核心功能
- **深度架构分析**：一键生成通俗易懂的项目综述、技术栈映射及模块逻辑分析。
- **源码级 Q&A**：针对代码实现细节提问，AI 实时检索并解答。
- **专业级 UI 设计**：采用 1:5:3 黄金比例布局，深色模式，极致阅读体验。
- **历史记录管理**：自动保存分析任务，支持历史回顾与一键删除。
- **服务器部署就绪**：支持单进程托管前端构建产物，极简部署流程。

## 🛠 技术栈
- **Frontend**: Vue 3 + Vite + Lucide Icons + Marked.js
- **Backend**: FastAPI + SQLModel + SQLite
- **AI Engine**: DeepSeek V3/V4 (via LangChain)

---

## 🚀 服务器部署教程 (Deployment Guide)

为了在生产环境获得最佳性能，请按照以下步骤操作：

### 1. 克隆项目
```bash
git clone <your-repo-url>
cd project_helper
```

### 2. 前端打包 (Frontend Build)
在服务器上编译 Vue 静态资源：
```bash
cd frontend
npm install
npm run build
```
编译完成后，会生成 `dist` 目录。后端会自动托管此目录。

### 3. 后端环境配置 (Backend Setup)
```bash
cd ../backend

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置 API Key
# 在 backend 目录下创建 .env 文件
echo "DEEPSEEK_API_KEY=你的_DEEPSEEK_API_KEY" > .env
```

### 4. 生产环境运行 (Using PM2)
推荐使用 PM2 守护进程确保服务稳定：
```bash
# 安装 PM2 (如果未安装)
npm install -g pm2

# 启动服务 (默认端口 8008)
pm2 start "uvicorn app.main:app --host 0.0.0.0 --port 8008" --name project-helper
```

### 5. 访问
浏览器访问：`http://你的服务器IP:8008`

---

## 📄 开源协议
本项目采用 [MIT License](LICENSE) 协议。
