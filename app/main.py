from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf.errors import PdfReadError

from app import db
from app import agents
from app import chat
from app.config import DATABASE_PATH, KT_BATCH_SIZE, ROOT_DIR
from app.llm import LLMConfigurationError, LLMRequestError
from app.rag import ingest_pdf, search_documents


app = FastAPI(
    title="考研数学复习 Agent",
    description="五 Agent 协作、PDF RAG 与分批 Knowledge Tracing。",
    version="0.1.0",
)
db.initialize_database(DATABASE_PATH)


class MainRequest(BaseModel):
    action: Literal["explain", "mastery", "roadmap", "practice"]
    message: str = Field(min_length=1)
    topic: str = ""
    history: list[dict[str, str]] = Field(default_factory=list)
    exam_date: str = ""
    hours_per_week: float | None = Field(default=None, gt=0)


class ExplainRequest(BaseModel):
    question: str = Field(min_length=1)
    topic: str = ""
    history: list[dict[str, str]] = Field(default_factory=list)


class MasteryRequest(BaseModel):
    question: str = ""


class RoadmapRequest(BaseModel):
    goal: str = Field(min_length=1)
    exam_date: str = ""
    hours_per_week: float | None = Field(default=None, gt=0)


class QuizRequest(BaseModel):
    mode: Literal["generate", "grade"]
    topic: str = Field(min_length=1)
    question: str = ""
    answer: str = ""
    difficulty: Literal["基础", "中等", "提高"] = "中等"


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=20)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    message: str = Field(min_length=1, max_length=12000)
    topic: str = Field(default="", max_length=160)
    exam_date: str = Field(default="", max_length=40)
    hours_per_week: float | None = Field(default=None, gt=0)
    intent: Literal["message", "answer"] = "message"


@app.get("/api/health")
def health() -> dict[str, str | int]:
    return {"status": "ok", "kt_batch_size": KT_BATCH_SIZE}


@app.post("/api/agents/main")
def main_agent(request: MainRequest):
    try:
        return agents.run_main_agent(
            request.action,
            request.message,
            request.topic,
            request.history,
            request.exam_date,
            request.hours_per_week,
        )
    except LLMConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except LLMRequestError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/agents/mastery")
def mastery_agent(request: MasteryRequest):
    try:
        return agents.analyze_mastery(request.question)
    except LLMConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except LLMRequestError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/agents/search-explain")
def search_explain_agent(request: ExplainRequest):
    try:
        return agents.explain(request.question, request.topic, request.history)
    except LLMConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except LLMRequestError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/agents/roadmap")
def roadmap_agent(request: RoadmapRequest):
    try:
        return agents.plan_route(request.goal, request.exam_date, request.hours_per_week)
    except LLMConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except LLMRequestError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/agents/quiz-grade")
def quiz_grade_agent(request: QuizRequest):
    if request.mode == "grade" and (not request.question.strip() or not request.answer.strip()):
        raise HTTPException(status_code=422, detail="判题模式必须提供 question 和 answer。")
    try:
        result = agents.quiz_or_grade(
            request.topic,
            request.mode,
            request.question,
            request.answer,
            request.difficulty,
        )
        if request.mode == "generate":
            result.pop("answer", None)
        return result
    except LLMConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except LLMRequestError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/chat/{module}")
def get_chat(
    module: str,
    session_id: str = Query(
        min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"
    ),
):
    if module not in chat.MODULES:
        raise HTTPException(status_code=404, detail="未找到该复习模块。")
    summary = db.get_chat_summary(DATABASE_PATH, session_id, module)
    state = db.get_module_state(DATABASE_PATH, session_id, module)
    return {
        "module": module,
        "messages": db.get_chat_messages(DATABASE_PATH, session_id, module),
        "summary": summary["summary"],
        "practice_pending": bool(state.get("active_question")),
    }


@app.post("/api/chat/{module}")
def send_chat(module: str, request: ChatRequest):
    if module not in chat.MODULES:
        raise HTTPException(status_code=404, detail="未找到该复习模块。")
    try:
        return chat.send_message(
            session_id=request.session_id,
            module=module,
            message=request.message.strip(),
            topic=request.topic.strip(),
            exam_date=request.exam_date,
            hours_per_week=request.hours_per_week,
            intent=request.intent,
        )
    except chat.ChatConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except LLMConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except LLMRequestError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.delete("/api/chat/{module}")
def clear_chat(
    module: str,
    session_id: str = Query(
        min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"
    ),
):
    if module not in chat.MODULES:
        raise HTTPException(status_code=404, detail="未找到该复习模块。")
    db.clear_chat_module(DATABASE_PATH, session_id, module)
    return {"cleared": module}


@app.get("/api/memory")
def global_memory(
    session_id: str = Query(
        min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"
    ),
):
    return {"memories": db.get_global_memories(DATABASE_PATH, session_id)}


@app.delete("/api/memory/{memory_id}")
def remove_global_memory(
    memory_id: int,
    session_id: str = Query(
        min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"
    ),
):
    if not db.delete_global_memory(DATABASE_PATH, session_id, memory_id):
        raise HTTPException(status_code=404, detail="未找到该上下文记忆。")
    return {"deleted": memory_id}


@app.post("/api/rag/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename or Path(file.filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=415, detail="仅支持 PDF 文件。")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传的 PDF 文件为空。")
    try:
        return ingest_pdf(DATABASE_PATH, Path(file.filename).name, content)
    except (ValueError, PdfReadError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/rag/documents")
def documents():
    return {"documents": db.list_documents(DATABASE_PATH)}


@app.delete("/api/rag/documents/{source}")
def remove_document(source: str):
    if not db.delete_document(DATABASE_PATH, source):
        raise HTTPException(status_code=404, detail="未找到该资料。")
    return {"deleted": source}


@app.post("/api/rag/search")
def rag_search(request: RagSearchRequest):
    try:
        return {"results": search_documents(DATABASE_PATH, request.query, request.limit)}
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/knowledge")
def knowledge():
    return {"knowledge": db.get_knowledge(DATABASE_PATH), "batch_size": KT_BATCH_SIZE}


@app.get("/api/attempts")
def attempts(topic: str | None = None):
    return {"attempts": db.get_attempts(DATABASE_PATH, topic)}


app.mount("/static", StaticFiles(directory=ROOT_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT_DIR / "static" / "index.html")
