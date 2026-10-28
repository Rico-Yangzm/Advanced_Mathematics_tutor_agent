import json
from typing import Any

from app import db
from app.config import DATABASE_PATH
from app.llm import chat
from app.rag import search_documents


PROMPTS = {
    "main": """你是考研数学复习主 Agent。把请求分派给恰当的专业 Agent，并整合其结果。
可选能力：掌握分析、搜题讲解、路线规划、出题判题。必须依据提供的资料和 KT 状态；
不要声称已调用未提供的能力。讲题时遵守循序渐进的启发式规则：一次只推进一个关键步骤，
先确认学生卡点，再给一个小提示或一个问题，不直接倾倒完整解答。""",
    "memory": """你是考研数学复习系统的主 Agent，负责判断哪些信息值得加入跨模块的长期上下文记忆。
只保存用户明确表达且未来复习对话会重复有用的信息：学习目标/考试安排、每周可用时间、
对讲解节奏/格式/范式的偏好、反复出现的学习困难。不要保存单道题内容、答案、一次性请求、
模型推断、敏感个人信息，也不要重复记录 KT 已管理的答题正确率/掌握度。
输出 JSON 对象：{"memories":[{"category":"study_goal|study_schedule|learning_preference|recurring_difficulty",
"content":"简短、准确、可跨模块使用的记忆"}]}。返回当前仍有效的全部记忆；若无适合长期保存的新信息，
原样保留已有记忆。最多 8 条、每条不超过 240 字。不要输出 JSON 以外的内容。""",
    "mastery": """你是考研数学掌握分析 Agent。依据 KT 状态分析薄弱主题、证据强度和近期复习优先级。
区分低掌握度与低置信度；样本少时明确提示“数据不足”，不要把未记录误判为不会。
输出简洁、具体、可执行的复习建议，不编造答题记录。""",
    "search_explain": """你是考研数学搜题讲解 Agent，采用启发式、逐步引导的苏格拉底式教学。
当学生提问或卡住时，先准确回应当前卡点，然后只给一个最小提示，并以一个聚焦问题结束；
每轮最多推进一个推理步骤，答案保持简短清楚。不要在学生未要求完整解答时提前给出完整推导。
如果学生明确要求答案或完整讲解，可以分步骤给出推导，并说明每步依据。区分检索到的资料与推理；
只把检索片段作为参考，不伪造出处。""",
    "roadmap": """你是考研数学路线规划 Agent。按考试时间、目标、可用时长和 KT 状态制定阶段化复习路线。
低掌握度主题优先，置信度低的主题先安排诊断题；路线要可执行、留有复盘和错题回访时间。
缺少用户信息时明确说明假设，不虚构日期或学习时长。""",
    "quiz_grade": """你是考研数学出题判题 Agent。出题需给出题目、主题、难度、考查点和参考答案（答案需隐藏在
“参考答案”区块，避免练习时泄露）；题目应与指定知识点同类型但不照搬资料原题。
判题需比较学生答案与数学上正确的结果，允许等价表达；说明判定依据、关键错因和一个下一步提示。
只返回合法 JSON 对象，生成题字段为 question, topic, difficulty, concept, answer；
判题字段为 correct (boolean), feedback, hint。""",
}


def _context_block(label: str, value: Any) -> str:
    return f"{label}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def analyze_mastery(question: str = "", shared_context: str = "") -> dict[str, Any]:
    knowledge = db.get_knowledge(DATABASE_PATH)
    attempts = db.get_attempts(DATABASE_PATH)
    response = chat(
        PROMPTS["mastery"],
        "\n\n".join(
            [
                _context_block("当前知识状态", knowledge),
                _context_block("最近答题记录", attempts[:30]),
                f"共享上下文记忆与本模块对话摘要：\n{shared_context or '无'}",
                f"学生补充问题：{question or '请总结薄弱点并给出复习建议。'}",
            ]
        ),
    )
    return {"analysis": response, "knowledge": knowledge}


def explain(
    question: str,
    topic: str = "",
    history: list[dict[str, str]] | None = None,
    shared_context: str = "",
) -> dict[str, Any]:
    retrieval_query = " ".join(part for part in (topic, question) if part)
    sources = search_documents(DATABASE_PATH, retrieval_query)
    response = chat(
        PROMPTS["search_explain"],
        "\n\n".join(
            [
                f"知识主题：{topic or '未指定'}",
                f"跨模块学习记忆与本模块历史摘要：\n{shared_context or '无'}",
                _context_block("检索资料（可能为空）", sources),
                _context_block("此前对话", history or []),
                f"学生当前问题：{question}",
            ]
        ),
    )
    return {"answer": response, "sources": sources}


def plan_route(
    goal: str,
    exam_date: str = "",
    hours_per_week: float | None = None,
    shared_context: str = "",
) -> dict[str, Any]:
    knowledge = db.get_knowledge(DATABASE_PATH)
    response = chat(
        PROMPTS["roadmap"],
        "\n\n".join(
            [
                f"目标：{goal}",
                f"考试日期：{exam_date or '未提供'}",
                f"每周可用时长：{hours_per_week if hours_per_week is not None else '未提供'}",
                _context_block("KT 状态", knowledge),
                f"用户长期上下文与本模块历史：\n{shared_context or '无'}",
            ]
        ),
    )
    return {"roadmap": response, "knowledge": knowledge}


def quiz_or_grade(
    topic: str,
    mode: str,
    question: str = "",
    answer: str = "",
    difficulty: str = "中等",
    shared_context: str = "",
) -> dict[str, Any]:
    if mode == "generate":
        prompt = (
            f"请生成一道{difficulty}难度的考研数学练习题，主题是“{topic}”。"
            "题目要检测理解而不是只考机械计算。\n"
            f"共享的学习偏好与上下文：{shared_context or '无'}"
        )
    else:
        prompt = (
            f"主题：{topic}\n题目：{question}\n学生答案：{answer}\n"
            f"共享的学习偏好与上下文：{shared_context or '无'}\n"
            "请判定答案，返回指定 JSON 字段。"
        )
    raw = chat(PROMPTS["quiz_grade"], prompt, temperature=0.2)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"出题判题 Agent 未返回合法 JSON：{error}") from error
    required = ("question", "answer") if mode == "generate" else ("correct", "feedback", "hint")
    if not isinstance(result, dict) or any(key not in result for key in required):
        raise ValueError(f"出题判题 Agent 返回缺少必需字段：{', '.join(required)}")
    if mode == "grade":
        if not isinstance(result["correct"], bool):
            raise ValueError("判题结果中的 correct 必须为布尔值。")
        result["attempt"] = db.record_attempt(
            DATABASE_PATH, topic, question, result["correct"]
        )
    return result


def run_main_agent(
    action: str,
    message: str,
    topic: str = "",
    history: list[dict[str, str]] | None = None,
    exam_date: str = "",
    hours_per_week: float | None = None,
) -> dict[str, Any]:
    if action == "explain":
        specialist = explain(message, topic, history)
        task = f"请把讲解结果交付给学生，继续保持逐步引导。\n{specialist['answer']}"
    elif action == "mastery":
        specialist = analyze_mastery(message)
        task = f"请用友好、清楚的方式概括掌握分析。\n{specialist['analysis']}"
    elif action == "roadmap":
        specialist = plan_route(message, exam_date, hours_per_week)
        task = f"请清晰呈现路线规划。\n{specialist['roadmap']}"
    elif action == "practice":
        specialist = quiz_or_grade(topic or message, "generate")
        student_view = {key: value for key, value in specialist.items() if key != "answer"}
        task = f"请引导学生开始练习，并隐藏参考答案。\n{json.dumps(student_view, ensure_ascii=False)}"
        specialist = student_view
    else:
        raise ValueError(f"不支持的主 Agent action：{action}")
    answer = chat(PROMPTS["main"], task, temperature=0.2)
    return {"action": action, "answer": answer, "specialist_result": specialist}
