<script setup lang="ts">
import { ref, onMounted, computed, nextTick } from 'vue'
import axios from 'axios'
import { 
  Clock, Code, Cpu, Layers, Terminal, AlertCircle, Trash2, ExternalLink
} from 'lucide-vue-next'
import { marked } from 'marked'

const repoUrl = ref('')
const isAnalyzing = ref(false)
const currentStep = ref('idle')
const progress = ref(0)
const repoId = ref<number | null>(null)
const report = ref('')
const chatMessage = ref('')
const chatHistory = ref<{role: string, content: string}[]>([])
const historyList = ref<{id: number, url: string, name: string, status: string}[]>([])
const chatScroll = ref<HTMLElement | null>(null)
const isChatting = ref(false)

// --- 布局拖拽逻辑 ---
const sidebarWidth = ref(Number(localStorage.getItem('sidebarWidth')) || 230)
const chatWidth = ref(Number(localStorage.getItem('chatWidth')) || 400)
const isResizingSidebar = ref(false)
const isResizingChat = ref(false)

const startResizingSidebar = () => { isResizingSidebar.value = true; document.body.style.cursor = 'col-resize' }
const startResizingChat = () => { isResizingChat.value = true; document.body.style.cursor = 'col-resize' }

const handleMouseMove = (e: MouseEvent) => {
  if (isResizingSidebar.value) {
    sidebarWidth.value = Math.max(180, Math.min(400, e.clientX))
  } else if (isResizingChat.value) {
    const newChatWidth = window.innerWidth - e.clientX
    chatWidth.value = Math.max(250, Math.min(window.innerWidth * 0.6, newChatWidth))
  }
}

const stopResizing = () => {
  if (isResizingSidebar.value || isResizingChat.value) {
    localStorage.setItem('sidebarWidth', sidebarWidth.value.toString())
    localStorage.setItem('chatWidth', chatWidth.value.toString())
  }
  isResizingSidebar.value = false
  isResizingChat.value = false
  document.body.style.cursor = 'default'
}

const analysisSteps = computed(() => [
  { id: 'cloning', title: '克隆仓库', desc: '正在从 GitHub 获取源代码...' },
  { id: 'analyzing', title: '分析架构', desc: '扫描目录结构与技术栈...' },
  { id: 'modules', title: '模块映射', desc: '识别核心逻辑与数据流...' },
  { id: 'completed', title: '生成报告', desc: '正在整理架构分析见解...' }
])

const activeStepIndex = computed(() => {
  if (currentStep.value === 'cloning') return 0
  if (currentStep.value === 'analyzing') return 1
  if (currentStep.value.startsWith('analyzing')) return 2
  if (currentStep.value === 'completed') return 3
  return -1
})

const fetchHistory = async () => {
  try {
    const res = await axios.get('/api/repo/all/list')
    historyList.value = res.data
  } catch (err) {
    console.error(err)
  }
}

const deleteRepo = async (id: number, event: Event) => {
  event.stopPropagation()
  if (!confirm('确定要删除这条历史记录吗？')) return
  
  try {
    await axios.delete(`/api/repo/${id}`)
    await fetchHistory()
    if (repoId.value === id) {
      repoId.value = null
      report.value = ''
      chatHistory.value = []
      currentStep.value = 'idle'
    }
  } catch (err) {
    console.error(err)
  }
}

const selectHistory = async (id: number) => {
  if (isAnalyzing.value) return
  repoId.value = id
  report.value = ''
  chatHistory.value = []
  isAnalyzing.value = true
  
  try {
    const res = await axios.get(`/api/repo/${id}`)
    report.value = res.data.analysis_report
    currentStep.value = res.data.status
    progress.value = res.data.progress
    isAnalyzing.value = false
  } catch (err) {
    console.error(err)
    isAnalyzing.value = false
  }
}

const submitRepo = async () => {
  if (!repoUrl.value) return
  const url = repoUrl.value.trim()
  isAnalyzing.value = true
  currentStep.value = 'cloning'
  progress.value = 10
  report.value = ''
  chatHistory.value = []
  
  try {
    const res = await axios.post('/api/repo/submit', { url })
    repoId.value = res.data.repo_id
    repoUrl.value = ''
    
    if (res.data.cached) {
      await fetchStatus()
    } else {
      pollStatus()
    }
    await fetchHistory()
  } catch (err) {
    console.error(err)
    isAnalyzing.value = false
    currentStep.value = 'failed'
  }
}

const pollStatus = () => {
  const timer = setInterval(async () => {
    if (!repoId.value) return
    try {
      const res = await axios.get(`/api/repo/${repoId.value}`)
      progress.value = res.data.progress
      currentStep.value = res.data.status
      
      if (res.data.status === 'completed') {
        clearInterval(timer)
        report.value = res.data.analysis_report
        isAnalyzing.value = false
        fetchHistory()
      } else if (res.data.status.startsWith('failed')) {
        clearInterval(timer)
        isAnalyzing.value = false
      }
    } catch (err) {
      clearInterval(timer)
      isAnalyzing.value = false
    }
  }, 2000)
}

const fetchStatus = async () => {
  if (!repoId.value) return
  const res = await axios.get(`/api/repo/${repoId.value}`)
  report.value = res.data.analysis_report
  progress.value = 100
  currentStep.value = 'completed'
  isAnalyzing.value = false
}

const sendMessage = async () => {
  if (!chatMessage.value || !repoId.value) return
  
  const userMsg = chatMessage.value
  chatHistory.value.push({ role: 'user', content: userMsg })
  chatMessage.value = ''
  
  const aiMsgIdx = chatHistory.value.push({ role: 'assistant', content: '...' }) - 1
  scrollToBottom()
  isChatting.value = true

  try {
    const response = await fetch('/api/chat/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_id: repoId.value, message: userMsg })
    })
    
    const reader = response.body?.getReader()
    if (!reader) {
      isChatting.value = false
      return
    }
    
    chatHistory.value[aiMsgIdx].content = ''
    
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      
      const chunk = new TextDecoder().decode(value)
      const lines = chunk.split('\n')
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            if (data.text !== '... (analyzing code)') {
                chatHistory.value[aiMsgIdx].content += data.text
                scrollToBottom()
            }
          } catch(e) {}
        }
      }
    }
  } catch (err) {
    console.error(err)
  } finally {
    isChatting.value = false
  }
}

const scrollToBottom = () => {
  nextTick(() => {
    if (chatScroll.value) {
      chatScroll.value.scrollTop = chatScroll.value.scrollHeight
    }
  })
}

onMounted(() => {
  fetchHistory()
  window.addEventListener('mousemove', handleMouseMove)
  window.addEventListener('mouseup', stopResizing)
})
</script>

<template>
  <div class="dashboard-layout" :style="{ gridTemplateColumns: `${sidebarWidth}px 4px 1fr 4px ${chatWidth}px` }">
    <!-- 左侧边栏 (1) -->
    <aside class="left-sidebar">
      <div class="logo-section">
        <Cpu class="logo-icon" :size="24" />
        <div class="logo-container">
          <span class="logo-text">仓库分析助手</span>
          <a href="https://github.com/chievan/project_helper" target="_blank" class="title-link">
            <ExternalLink :size="12" />
          </a>
        </div>
      </div>


      <div class="history-section">
        <h3 class="section-title">历史记录</h3>
        <div class="history-list-container scrollable">
          <div 
            v-for="item in historyList" 
            :key="item.id" 
            class="history-item"
            :class="{ active: repoId === item.id }"
            @click="selectHistory(item.id)"
          >
            <div class="repo-name">{{ item.name }}</div>
            <div class="repo-status">
              <div class="status-info">
                <span class="status-dot" :class="item.status === 'completed' ? 'completed' : 'processing'"></span>
                {{ item.status === 'completed' ? '已完成' : '分析中' }}
              </div>
              <button 
                class="delete-action-btn"
                @click="deleteRepo(item.id, $event)"
              >
                删除
              </button>
            </div>
          </div>
          <div v-if="historyList.length === 0" class="empty-history text-center text-gray-500 py-4 text-xs">
            暂无历史分析记录
          </div>
        </div>
      </div>

    </aside>

    <!-- 拖拽条 1 -->
    <div class="resizer" @mousedown="startResizingSidebar" :class="{ active: isResizingSidebar }"></div>

    <!-- 中间主内容区 (5) -->
    <main class="main-content">
      <header class="panel-header">
        <div class="repo-info">
          <GithubIcon v-if="repoId" :size="20" class="mr-2 inline text-gray-400" />
          <span class="font-bold text-lg">{{ repoId ? '项目分析报告' : '准备就绪' }}</span>
        </div>
        <div v-if="isAnalyzing" class="progress-pill bg-gray-200 px-3 py-1 rounded-full text-xs flex items-center gap-2">
          <Loader2 class="animate-spin" :size="12" />
          {{ Math.round(progress) }}% 已完成
        </div>
      </header>

      <div class="report-content scrollable">
        <div v-if="report" class="markdown-body fade-in" v-html="marked(report)"></div>
        <div v-else-if="currentStep === 'idle'" class="empty-state flex flex-col items-center justify-center h-full text-center">
          <Layers :size="64" class="text-gray-300 mb-6" />
          <h2 class="text-2xl font-bold mb-2">欢迎使用 AI 仓库分析助手</h2>
          <p class="text-gray-500 max-w-md">在左侧输入 GitHub 仓库地址，AI 将为您深度剖析项目架构、技术栈及核心逻辑。</p>
        </div>
        <div v-else-if="isAnalyzing && !report" class="loading-state flex flex-col items-center justify-center h-full">
           <Terminal :size="48" class="text-green-600 mb-4 animate-pulse" />
           <p class="text-lg font-medium">AI 正在努力扫描代码库...</p>
           <p class="text-sm text-gray-500">大型项目可能需要 1-2 分钟，请稍候。</p>
        </div>
      </div>
    </main>

    <!-- 拖拽条 2 -->
    <div class="resizer" @mousedown="startResizingChat" :class="{ active: isResizingChat }"></div>

    <!-- 右侧分析详情 (4) -->
    <aside class="right-sidebar">
      <div class="progress-section">
        <!-- 新增：水平布局的输入框和按钮 -->
        <div class="horizontal-input-group mb-6">
          <input 
            v-model="repoUrl" 
            type="text" 
            placeholder="粘贴 GitHub 仓库地址..." 
            class="custom-input flex-1"
            @keyup.enter="submitRepo"
          />
          <button class="analyze-btn" @click="submitRepo" :disabled="isAnalyzing">
            <Loader2 v-if="isAnalyzing" class="animate-spin" :size="16" />
            <Search v-else :size="16" />
            <span>{{ isAnalyzing ? '分析中' : '开始' }}</span>
          </button>
        </div>
        
        <div class="timeline">
          <div 
            v-for="(step, index) in analysisSteps" 
            :key="step.id" 
            class="timeline-item"
            :class="{ active: index <= activeStepIndex }"
          >
            <div class="timeline-icon" :class="{ active: index <= activeStepIndex }"></div>
            <div class="timeline-content">
              <div class="timeline-title" :class="{ 'text-green-500': index <= activeStepIndex }">{{ step.title }}</div>
              <div class="timeline-desc">{{ step.desc }}</div>
            </div>
          </div>
        </div>
      </div>

      <div class="chat-section">
        <h3 class="section-title mb-4 flex items-center gap-2">
          <MessageSquare :size="14" /> 源码 Q&A
        </h3>
        <div class="chat-messages scrollable" ref="chatScroll">
          <div v-if="chatHistory.length === 0" class="empty-chat text-center py-8">
            <MessageSquare :size="32" class="text-gray-300 mx-auto mb-2" />
            <p class="text-xs text-gray-500">问问我：“这个项目是如何处理权限验证的？”</p>
          </div>
          <div 
            v-for="(msg, i) in chatHistory" 
            :key="i" 
            :class="['message', msg.role]"
          >
            <div v-html="marked(msg.content)"></div>
            <span v-if="msg.role === 'assistant' && isChatting && i === chatHistory.length - 1" class="typing-cursor"></span>
          </div>
        </div>
        <div class="chat-input-container">
          <input 
            v-model="chatMessage" 
            class="custom-input" 
            placeholder="输入您的问题..."
            @keyup.enter="sendMessage"
            :disabled="!repoId"
          />
          <button class="analyze-btn px-4" @click="sendMessage" :disabled="!repoId">
            <Send :size="18" />
          </button>
        </div>
      </div>
    </aside>
  </div>
</template>

<style>
/* Global Utility classes for Light Mode */
.mr-2 { margin-right: 0.5rem; }
.inline { display: inline; }
.text-gray-300 { color: #d1d5db; }
.text-gray-400 { color: #9ca3af; }
.text-gray-500 { color: #6b7280; }
.text-green-500 { color: #10b981; }
.text-green-600 { color: #059669; }
.font-bold { font-weight: 700; }
.text-lg { font-size: 1.125rem; }
.text-2xl { font-size: 1.5rem; }
.mb-2 { margin-bottom: 0.5rem; }
.mb-4 { margin-bottom: 1rem; }
.mb-6 { margin-bottom: 1.5rem; }
.text-center { text-align: center; }
.flex { display: flex; }
.flex-col { flex-direction: column; }
.items-center { align-items: center; }
.justify-center { justify-content: center; }
.h-full { height: 100%; }
.max-w-md { max-width: 28rem; }
.animate-pulse { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .5; } }
.animate-spin { animation: spin 1s linear infinite; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
.px-3 { padding-left: 0.75rem; padding-right: 0.75rem; }
.py-1 { padding-top: 0.25rem; padding-bottom: 0.25rem; }
.py-4 { padding-top: 1rem; padding-bottom: 1rem; }
.py-8 { padding-top: 2rem; padding-bottom: 2rem; }
.px-4 { padding-left: 1rem; padding-right: 1rem; }
.rounded-full { border-radius: 9999px; }
.text-xs { font-size: 0.75rem; }
.gap-2 { gap: 0.5rem; }
.bg-gray-200 { background-color: #e5e7eb; }
</style>
