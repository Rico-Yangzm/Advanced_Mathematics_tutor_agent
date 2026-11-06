# 考研数学复习 Agent

FastAPI + SQLite 的本地复习工作台：PDF 与内置考研数学考点进入全文检索 RAG；出题结合考点资料和知识状态并经过质量评审；判题根据解题证据更新知识感知 KT；网页使用侧边栏模块和持久化对话。

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

## Agent 与工作流

| Agent | API | 职责 |
|---|---|---|
| 主 Agent | `POST /api/agents/main` | `action` 分派到专业 Agent，再整合交付 |
| 掌握分析 Agent | `POST /api/agents/mastery` | 结合 KT 状态与近期答题记录分析薄弱点 |
| 搜题讲解 Agent | `POST /api/agents/search-explain` | 先查本地 RAG，再按“一个提示 / 一个问题”逐步讲题 |
| 路线规划 Agent | `POST /api/agents/roadmap` | 结合目标、时间与 KT 状态规划 |
| 出题判题 Agent | `POST /api/agents/quiz-grade` | `mode=generate` 按目标考点、RAG 资料与薄弱状态出题并评审；`mode=grade` 按考点感知解题证据并登记状态 |
| 质量评审 Agent | 出题流程内部调用 | 独立检查题目质量；未通过时将评审意见反馈给出题 Agent 并最多重写一次 |

主 Agent `action` 可选 `explain`、`mastery`、`roadmap`、`practice`。RAG 维护接口为 `POST /api/rag/upload`、`GET /api/rag/documents`、`DELETE /api/rag/documents/{source}`、`POST /api/rag/search`。KT 和答题记录分别见 `GET /api/knowledge`、`GET /api/attempts`。

## 持久化对话与共享上下文

- 网页侧边栏中的讲题、掌握分析、路线规划、出题判题是彼此独立的会话模块；会话由浏览器生成的 session ID 隔离，记录保存在 SQLite，刷新或重新打开页面后仍可继续。
- `GET/POST/DELETE /api/chat/{module}` 读取、发送或清空模块对话。模块值为 `tutor`、`mastery`、`roadmap`、`practice`。
- 每个模块的未压缩对话达到 16 条后，主 Agent 生成滚动摘要；后续请求组合模块摘要、最近消息，并通过 FTS5 检索较早的相关对话。
- 每轮回复后主 Agent 筛选可跨模块复用的学习目标、时间安排、讲解偏好和反复困难，存入独立的全局上下文记忆；单题内容和答题表现不作为长期记忆。侧栏可查看并删除记忆。
- `GET /api/memory?session_id=...` 查看共享记忆，`DELETE /api/memory/{memory_id}?session_id=...` 删除单条。清空模块对话不会清除全局记忆或 KT。
- 前端将 Agent 回复按安全转义后的 Markdown 渲染，支持标题、列表、引用、链接、行内代码和代码块。
- 回复中的 `$...$`、`\(...\)`、`$$...$$`、`\[...\]` 数学公式会转换为浏览器原生 MathML；支持分式、上下标、根式、常用函数/符号及 `aligned`、`cases` 等环境，不依赖外部 CDN。
- 聊天输入框的“∑ 公式”快捷面板可插入分式、根式、上下标、极限、积分、求和、微分、常用关系符号和希腊字母；选中已有文本后插入分式等模板可直接包裹内容，光标会定位到下一处待填写位置。

## RAG 与 KT 数据策略

- PDF 按文本提取并切成有重叠的文本块，保存来源、页数和块；SQLite FTS5 进行本地全文检索。扫描件若没有文本层，需要 OCR 后再导入。
- 首次启动会把 `knowledge/11408-math/` 中的高数、线代、概率论知识章分块加入 SQLite FTS5；再次启动时跳过已导入来源。资料包保留上游 MIT 许可与来源说明，详见 [knowledge/11408-math/ATTRIBUTION.md](knowledge/11408-math/ATTRIBUTION.md)。
- 出题会检索目标考点和相关资料，参考掌握度/证据置信度决定侧重点；质量评审针对考点覆盖、正确性、歧义、难度及与原题的相似风险检查，最多按反馈修订一次。
- KT 按知识点保存逐题证据。判题分析答案及可见解题步骤，输出 `demonstrated`、`partial`、`not_demonstrated` 或 `uncertain` 及置信度；只有有证据的考点才更新。掌握度按 `旧值 + 0.35 × 证据置信度 × (证据分数 - 旧值)` 平滑调整，证据置信度为 `min(1, 有效观察数 / 4) × 平均证据置信度`。历史每 4 题批次记录仍兼容。
- 搜题讲解、掌握分析和出题判题模块会在每轮普通对话后用一次精简模型调用提取本轮用户明确展示的解题证据；助手提供的内容仅供理解题目，绝不作为用户掌握证据。无具体步骤或置信度低于 0.6 时不更新。练习判题轮次复用判题已得到的 KT 评估，不重复调用。此调用通常额外消耗约 0.6k–1.5k tokens/轮；极长输入按首尾各 1500 字截取，单轮消耗也会相应增加。
- 出题与知识感知流程借鉴用户提供的论文《Knowledge Starts with Practice: Knowledge-Aware Exercise Generative Recommendation with Adaptive Multi-Agent Cooperation》中的知识状态感知、个性化生成和评审反馈思路；本项目实现为本地 RAG、单次质量复核/至多一次修订及可解释的考点证据更新，并非论文模型的完整复现。论文 PDF 被 Git 忽略，不会作为项目资料提交。
- 默认数据库为 `data/study_agent.db`，可用 `DATABASE_PATH` 更改。

## 测试

```powershell
python -m unittest discover -s tests -v
```
