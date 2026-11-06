import json
import logging
from typing import Any

from app import agents, db
from app.config import DATABASE_PATH
from app.llm import LLMConfigurationError, LLMRequestError, chat


logger = logging.getLogger(__name__)
MODULES = {"tutor", "mastery", "roadmap", "practice"}
MODULE_TITLES = {
    "tutor": "搜题讲解",
    "mastery": "掌握分析",
    "roadmap": "路线规划",
    "practice": "出题判题",
}
MEMORY_CATEGORIES = {
    "study_goal",
    "study_schedule",
    "learning_preference",
    "recurring_difficulty",
}
SUMMARY_THRESHOLD = 16
RECENT_MESSAGE_LIMIT = 12


class ChatConflictError(RuntimeError):
    pass


def _context_for_turn(
    session_id: str, module: str, query: str
) -> tuple[str, list[dict[str, str]]]:
    summary = db.get_chat_summary(DATABASE_PATH, session_id, module)
    recent = db.get_recent_chat_messages(
        DATABASE_PATH, session_id, module, RECENT_MESSAGE_LIMIT
    )
    before_id = recent[0]["id"] if recent else 2**63 - 1
    older = db.search_old_chat_messages(
        DATABASE_PATH, session_id, module, query, before_id
    )
    memories = db.get_global_memories(DATABASE_PATH, session_id)
    relevant_memories = db.search_global_memories(
        DATABASE_PATH, session_id, query, limit=4
    )
    relevant_ids = {item["id"] for item in relevant_memories}
    memory_context = [
        item for item in memories if item["id"] in relevant_ids
    ] or memories[:4]

    sections = []
    if summary["summary"]:
        sections.append(f"本模块压缩摘要：\n{summary['summary']}")
    if memory_context:
        sections.append(
            "主 Agent 维护的跨模块长期记忆：\n"
            + json.dumps(
                [
                    {"category": item["category"], "content": item["content"]}
                    for item in memory_context
                ],
                ensure_ascii=False,
            )
        )
    if recent:
        sections.append(
            "本模块最近对话：\n"
            + json.dumps(
                [
                    {"role": item["role"], "content": item["content"]}
                    for item in recent
                ],
                ensure_ascii=False,
            )
        )
    if older:
        sections.append(
            "从本模块较早对话检索到的相关内容：\n"
            + json.dumps(
                [
                    {"role": item["role"], "content": item["content"]}
                    for item in older
                ],
                ensure_ascii=False,
            )
        )
    history = [
        {"role": item["role"], "content": item["content"]} for item in older
    ]
    if summary["summary"]:
        history.insert(
            0, {"role": "system", "content": f"本模块此前对话摘要：{summary['summary']}"}
        )
    history.extend(
        {"role": item["role"], "content": item["content"]} for item in recent
    )
    return "\n\n".join(sections), history[-18:]


def _format_practice_result(
    result: dict[str, Any], session_id: str, message: str, intent: str, module: str
) -> tuple[str, dict[str, Any]]:
    state = db.get_module_state(DATABASE_PATH, session_id, module)
    active_question = state.get("active_question")
    if intent == "answer":
        if not active_question:
            raise ChatConflictError("当前没有等待作答的练习题，请先让 Agent 出一道题。")
        graded = agents.quiz_or_grade(
            active_question.get("topic", "考研数学"),
            "grade",
            active_question["question"],
            message,
            shared_context=result["context"],
            concepts=active_question.get("concepts", []),
        )
        state.pop("active_question", None)
        db.set_module_state(DATABASE_PATH, session_id, module, state)
        evidence_labels = {
            "demonstrated": "有掌握证据",
            "partial": "部分掌握",
            "not_demonstrated": "仍需加强",
            "uncertain": "证据不足，暂不更新",
        }
        evidence_lines = [
            f"- {item['concept']}：{evidence_labels[item['evidence']]} "
            f"（置信度 {item['confidence']:.0%}）— {item['rationale']}"
            for item in graded["concept_assessments"]
        ]
        evidence_summary = (
            "\n".join(evidence_lines)
            if evidence_lines
            else "本次没有足够证据更新目标考点。"
        )
        response = (
            f"**{'回答正确' if graded['correct'] else '还需要再想一步'}**\n\n"
            f"{graded['feedback']}\n\n**下一步提示：** {graded['hint']}\n\n"
            f"**知识感知更新：**\n{evidence_summary}"
        )
        return response, {"grading": graded}

    if active_question:
        guided = agents.explain(
            (
                f"当前练习题：{active_question['question']}\n"
                f"参考答案仅供后台评分，不要直接透露。\n学生卡住的地方：{message}"
            ),
            topic=active_question.get("topic", ""),
            history=result["history"],
            shared_context=result["context"],
        )
        return guided["answer"], {"sources": guided["sources"], "practice_pending": True}

    topic = result.get("topic") or message
    generated = agents.quiz_or_grade(
        topic,
        "generate",
        difficulty=result.get("difficulty", "中等"),
        shared_context=result["context"],
    )
    state["active_question"] = generated
    db.set_module_state(DATABASE_PATH, session_id, module, state)
    response = (
        f"**{generated['question']}**\n\n"
        f"考查点：{generated.get('concept', topic)} · "
        f"难度：{generated.get('difficulty', '中等')}\n\n"
        f"目标考点：{'、'.join(generated.get('concepts', []))}\n\n"
        "想要提示可以直接问我；准备好后，勾选输入框旁的“提交作答”再发送答案。"
    )
    return response, {"practice_pending": True}


def _run_specialist(
    module: str,
    message: str,
    context: str,
    history: list[dict[str, str]],
    topic: str,
    exam_date: str,
    hours_per_week: float | None,
    intent: str,
    session_id: str,
) -> tuple[str, dict[str, Any]]:
    if module == "tutor":
        result = agents.explain(
            message, topic=topic, history=history, shared_context=context
        )
        return result["answer"], {"sources": result["sources"]}
    if module == "mastery":
        result = agents.analyze_mastery(message, shared_context=context)
        return result["analysis"], {"knowledge": result["knowledge"]}
    if module == "roadmap":
        result = agents.plan_route(
            message,
            exam_date=exam_date,
            hours_per_week=hours_per_week,
            shared_context=context,
        )
        return result["roadmap"], {"knowledge": result["knowledge"]}
    if module == "practice":
        result = {
            "context": context,
            "history": history,
            "topic": topic,
            "difficulty": "中等",
        }
        return _format_practice_result(
            result, session_id, message, intent, module
        )
    raise ValueError(f"不支持的复习模块：{module}")


def _curate_global_memory(
    session_id: str,
    module: str,
    user_message: str,
    assistant_message: str,
) -> bool:
    current = db.get_global_memories(DATABASE_PATH, session_id)
    prompt = {
        "module": MODULE_TITLES[module],
        "已有长期记忆": [
            {"category": item["category"], "content": item["content"]}
            for item in current
        ],
        "本轮对话": [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ],
    }
    response = chat(
        agents.PROMPTS["memory"],
        json.dumps(prompt, ensure_ascii=False),
        temperature=0.1,
    )
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError:
        logger.warning("Main agent returned invalid JSON while curating memory.")
        return False
    if not isinstance(parsed, dict) or not isinstance(parsed.get("memories"), list):
        logger.warning("Main agent returned an invalid memory payload.")
        return False
    memories = parsed["memories"]
    if len(memories) > 8:
        logger.warning("Main agent returned more than eight global memories.")
        return False
    sanitized: dict[str, str] = {}
    for item in memories:
        if not isinstance(item, dict):
            logger.warning("Main agent returned a malformed memory entry.")
            return False
        category, content = item.get("category"), item.get("content")
        if (
            category not in MEMORY_CATEGORIES
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 240
        ):
            logger.warning("Main agent returned an invalid global memory entry.")
            return False
        sanitized[category] = content.strip()
    db.replace_global_memories(
        DATABASE_PATH, session_id, module, sanitized
    )
    return True


def _compact_conversation(session_id: str, module: str) -> bool:
    summary = db.get_chat_summary(DATABASE_PATH, session_id, module)
    messages = db.get_unsummarized_chat_messages(
        DATABASE_PATH,
        session_id,
        module,
        summary["summarized_through"],
    )
    if len(messages) < SUMMARY_THRESHOLD:
        return False
    prompt = {
        "previous_summary": summary["summary"],
        "new_messages": [
            {"role": item["role"], "content": item["content"]} for item in messages
        ],
    }
    compacted = chat(
        "你负责压缩考研数学复习模块的历史对话。保留用户长期目标和约束、"
        "学习偏好、尚未解决的问题、题目当前进度及重要结论；区分用户事实与推测。"
        "舍弃寒暄和重复内容。用简短中文给出可供后续对话使用的摘要，不要回答题目，"
        "摘要最多 1200 个汉字，优先保留仍然有效的信息。",
        json.dumps(prompt, ensure_ascii=False),
        temperature=0.1,
    )
    db.save_chat_summary(
        DATABASE_PATH, session_id, module, compacted, messages[-1]["id"]
    )
    return True


def send_message(
    *,
    session_id: str,
    module: str,
    message: str,
    topic: str = "",
    exam_date: str = "",
    hours_per_week: float | None = None,
    intent: str = "message",
) -> dict[str, Any]:
    if module not in MODULES:
        raise ValueError(f"不支持的复习模块：{module}")
    context, history = _context_for_turn(session_id, module, message)
    response, metadata = _run_specialist(
        module,
        message,
        context,
        history,
        topic,
        exam_date,
        hours_per_week,
        intent,
        session_id,
    )
    knowledge_update = None
    knowledge_update_error = None
    if module in {"tutor", "mastery", "practice"} and "grading" not in metadata:
        try:
            knowledge_update = agents.perceive_dialogue_knowledge(
                module=module,
                topic=topic,
                user_message=message,
                history=db.get_recent_chat_messages(
                    DATABASE_PATH, session_id, module, RECENT_MESSAGE_LIMIT
                ),
            )
            updated_concepts = knowledge_update["updated_concepts"]
            if updated_concepts:
                response += (
                    "\n\n**KT 证据记录：**本轮解题过程更新了 "
                    + "、".join(updated_concepts)
                    + "。"
                )
        except (LLMConfigurationError, LLMRequestError, ValueError) as error:
            knowledge_update_error = str(error)
            logger.warning("Dialogue knowledge state was not updated: %s", error)
    if metadata.get("sources"):
        response += "\n\n参考资料：" + "、".join(
            source for source in (item.get("source", "") for item in metadata["sources"])
            if source
        )
    db.add_chat_message(DATABASE_PATH, session_id, module, "user", message)
    db.add_chat_message(DATABASE_PATH, session_id, module, "assistant", response)

    memory_updated = False
    memory_error = None
    try:
        memory_updated = _curate_global_memory(
            session_id, module, message, response
        )
    except (LLMConfigurationError, LLMRequestError) as error:
        memory_error = str(error)
        logger.warning("Global context memory was not updated: %s", error)

    summary_updated = False
    summary_error = None
    try:
        summary_updated = _compact_conversation(session_id, module)
    except (LLMConfigurationError, LLMRequestError) as error:
        summary_error = str(error)
        logger.warning("Conversation context was not compacted: %s", error)

    return {
        "module": module,
        "message": response,
        "metadata": metadata,
        "knowledge_update": knowledge_update,
        "knowledge_update_error": knowledge_update_error,
        "memory_updated": memory_updated,
        "memory_error": memory_error,
        "summary_updated": summary_updated,
        "summary_error": summary_error,
    }
