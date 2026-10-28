# 考研数学复习 Agent

FastAPI + SQLite 的本地复习工作台：PDF 资料进入全文检索 RAG；练习判题后按知识点每 4 题更新一次 KT；网页使用侧边栏模块和持久化对话。

## 启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

配置 `.env` 中的 OpenAI 兼容服务地址、API Key 和模型名称。应用直接读取系统环境变量，因此启动前可在 PowerShell 设置：

```powershell
$env:LLM_BASE_URL = "https://api.openai.com/v1"
$env:LLM_API_KEY = "..."
$env:LLM_MODEL = "gpt-4o-mini"
python -m uvicorn app.main:app --reload
```

浏览器打开 `http://127.0.0.1:8000`，接口说明见 `/docs`。模型服务未配置时，调用 Agent 接口会明确返回 HTTP 503；PDF 入库、资料检索、KT 查询不需要模型。

## 五个 Agent 与工作流

| Agent | API | 职责 |
|---|---|---|
| 主 Agent | `POST /api/agents/main` | `action` 分派到专业 Agent，再整合交付 |
| 掌握分析 Agent | `POST /api/agents/mastery` | 结合 KT 状态与近期答题记录分析薄弱点 |
| 搜题讲解 Agent | `POST /api/agents/search-explain` | 先查本地 RAG，再按“一个提示 / 一个问题”逐步讲题 |
| 路线规划 Agent | `POST /api/agents/roadmap` | 结合目标、时间与 KT 状态规划 |
| 出题判题 Agent | `POST /api/agents/quiz-grade` | `mode=generate` 出题；`mode=grade` 判题并登记答题记录 |

主 Agent `action` 可选 `explain`、`mastery`、`roadmap`、`practice`。RAG 维护接口为 `POST /api/rag/upload`、`GET /api/rag/documents`、`DELETE /api/rag/documents/{source}`、`POST /api/rag/search`。KT 和答题记录分别见 `GET /api/knowledge`、`GET /api/attempts`。

## 持久化对话与共享上下文

- 网页侧边栏中的讲题、掌握分析、路线规划、出题判题是彼此独立的会话模块；会话由浏览器生成的 session ID 隔离，记录保存在 SQLite，刷新或重新打开页面后仍可继续。
- `GET/POST/DELETE /api/chat/{module}` 读取、发送或清空模块对话。模块值为 `tutor`、`mastery`、`roadmap`、`practice`。
- 每个模块的未压缩对话达到 16 条后，主 Agent 生成滚动摘要；后续请求组合模块摘要、最近消息，并通过 FTS5 检索较早的相关对话。
- 每轮回复后主 Agent 筛选可跨模块复用的学习目标、时间安排、讲解偏好和反复困难，存入独立的全局上下文记忆；单题内容和答题表现不作为长期记忆。侧栏可查看并删除记忆。
- `GET /api/memory?session_id=...` 查看共享记忆，`DELETE /api/memory/{memory_id}?session_id=...` 删除单条。清空模块对话不会清除全局记忆或 KT。
- 前端将 Agent 回复按安全转义后的 Markdown 渲染，支持标题、列表、引用、链接、行内代码和代码块。

## RAG 与 KT 数据策略

- PDF 按文本提取并切成有重叠的文本块，保存来源、页数和块；SQLite FTS5 进行本地全文检索。扫描件若没有文本层，需要 OCR 后再导入。
- KT 按“知识点”分别累计答题：每 4 题计算本批正确率，以 `60% × 原掌握度 + 40% × 本批正确率` 平滑更新；置信度随已纳入题数增长。未满 4 题只累计待更新数，不提前改掌握度。
- 默认数据库为 `data/study_agent.db`，可用 `DATABASE_PATH` 更改。

## 测试

```powershell
python -m unittest discover -s tests -v
```
