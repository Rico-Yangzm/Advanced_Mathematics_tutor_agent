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
“参考答案”区块，避免练习时泄露）；题目应针对给定薄弱考点，并结合先修考点设计认知步骤，
但不能照搬检索资料原题。生成 JSON 字段为 question, topic, difficulty, concept,
concepts（1-5 个明确考点名称）, answer。
判题时先分析学生给出的推理/步骤，再判断最终答案；只根据可见证据评价，不从正确答案臆测推理过程。
按目标考点评价学习证据：demonstrated（有充分证据掌握）、partial（部分掌握）、
not_demonstrated（错误或关键步骤明确暴露薄弱）、uncertain（信息不足，不更新）。
给出可解释的简短 rationale 和 0-1 confidence；仅返回合法 JSON：
correct (boolean), feedback, hint, concept_assessments
（对象数组，每项包含 concept, evidence, confidence, rationale）。""",
    "exercise_evaluator": """你是考研数学练习题质量评审员。独立检查候选题与参考答案：
考点是否对齐指定目标和学习者薄弱点，数学推导/答案是否正确且唯一，条件是否完整、表述是否无歧义，
难度是否合适，是否只是改写检索资料中的原题。若有可修正问题，指出具体问题并给出可操作的修订要求。
只有所有关键项达标才 approved=true。只返回合法 JSON：
{"approved":boolean,"issues":["..."],"revision_instructions":"..."}。""",
    "dialogue_knowledge": """你是考研数学学习证据整理器。根据本轮学生自己的消息，判断其中是否明确展示了
可用于知识追踪的数学解题思路、推导步骤或概念解释。之前对话和助教回复只用于理解题目背景，
绝不能把助教提供的解法、提示或结论当作学生已掌握的证据；提问、复述题目、只说“懂了/不会”、
请求提示、学习计划和一般寒暄都不是掌握证据。
仅评估本轮消息中能直接观察到的步骤；按具体知识点分别标注 demonstrated（正确且有充分依据）、
partial（部分正确或过程不完整）、not_demonstrated（明确出现错误概念/步骤）。
证据不明确、只给答案而无法判断思路时不要猜测，直接返回空 observations。
每项 rationale 用不超过 80 字简述对应的用户原话/步骤；confidence 必须反映证据确定性，低于 0.6 的不应输出。
最多输出 5 个直接相关的考点，不要推断深层先修知识。只返回 JSON：
{"observations":[{"concept":"具体考点","evidence":"demonstrated|partial|not_demonstrated",
"confidence":0.0,"rationale":"用户本轮的可见证据"}]}。""",
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
    concepts: list[str] | None = None,
) -> dict[str, Any]:
    if mode == "generate":
        requested_concepts = concepts or []
        knowledge = db.get_knowledge(DATABASE_PATH)
        normalized_topic = topic.strip().casefold()
        target_knowledge = [
            item
            for item in knowledge
            if normalized_topic
            and (
                normalized_topic in item["topic"].casefold()
                or item["topic"].casefold() in normalized_topic
            )
        ]
        relevant_knowledge = target_knowledge or knowledge[:5]
        targets = requested_concepts or [
            item["topic"] for item in target_knowledge[:5]
        ] or [topic]
        sources = search_documents(
            DATABASE_PATH, " ".join([topic, *targets]), limit=5
        )
        examples = db.get_knowledge_observations(
            DATABASE_PATH, targets, limit=6
        )
        generation_context = "\n\n".join(
            [
                f"学习目标：{topic}",
                f"指定目标考点：{json.dumps(targets, ensure_ascii=False)}",
                f"难度：{difficulty}",
                _context_block("相关 KT 状态（低掌握或低置信度优先）", relevant_knowledge),
                _context_block("相近考点的历史知识感知证据", examples),
                _context_block("RAG 检索资料，仅用于核验考点与先修关系，不可照抄原题", sources),
                f"共享的学习偏好与上下文：\n{shared_context or '无'}",
                "请生成全新题目，明确覆盖目标考点并标注 concepts；题目可检验关键推理步骤。",
            ]
        )
        candidate = None
        feedback = ""
        for revision in range(2):
            prompt = generation_context
            if feedback:
                prompt += f"\n\n质量评审的修订要求：\n{feedback}"
            candidate = _parse_agent_json(
                chat(PROMPTS["quiz_grade"], prompt, temperature=0.2),
                "出题 Agent",
            )
            _validate_generated_exercise(
                candidate,
                targets,
                require_target=bool(requested_concepts or target_knowledge),
            )
            review = _parse_agent_json(
                chat(
                    PROMPTS["exercise_evaluator"],
                    _context_block(
                        "待评审题目与答案",
                        {
                            "exercise": candidate,
                            "targets": targets,
                            "sources": sources,
                            "learner_state": relevant_knowledge,
                        },
                    ),
                    temperature=0.1,
                ),
                "质量评审 Agent",
            )
            if not isinstance(review.get("approved"), bool):
                raise ValueError("质量评审 Agent 返回缺少布尔值 approved。")
            if review["approved"]:
                return candidate
            issues = review.get("issues", [])
            if not isinstance(issues, list) or any(
                not isinstance(issue, str) for issue in issues
            ):
                raise ValueError("质量评审 Agent 返回的 issues 必须是字符串数组。")
            if revision == 1:
                raise ValueError(
                    "练习题经过一次修订后仍未通过质量评审："
                    + "；".join(str(issue) for issue in issues)
                )
            revision_instructions = review.get("revision_instructions", "")
            if not isinstance(revision_instructions, str):
                raise ValueError("质量评审 Agent 返回的修订要求必须是字符串。")
            feedback = revision_instructions.strip()
            if not feedback:
                feedback = "请根据评审指出的问题修订题目，并复核答案。"
        raise ValueError("练习题生成与质量评审未能完成。")

    requested_concepts = list(dict.fromkeys(concepts or [topic]))
    sources = search_documents(
        DATABASE_PATH,
        " ".join([topic, *requested_concepts, question]),
        limit=5,
    )
    knowledge = db.get_knowledge(DATABASE_PATH)
    relevant_knowledge = [
        item
        for item in knowledge
        if item["topic"] in requested_concepts
    ]
    examples = db.get_knowledge_observations(
        DATABASE_PATH, requested_concepts, limit=6
    )
    prompt = "\n\n".join(
        [
            f"题目主题：{topic}",
            f"目标考点（评估时必须使用这些名称）：{json.dumps(requested_concepts, ensure_ascii=False)}",
            f"题目：{question}\n学生答案及可见解题过程：{answer}",
            _context_block("目标考点当前掌握状态", relevant_knowledge),
            _context_block("相同考点的历史感知样例（用于一致性判断）", examples),
            _context_block("相关知识资料，供核验概念及依赖关系", sources),
            f"共享的学习偏好与上下文：\n{shared_context or '无'}",
            "判定正确性；逐个目标考点评价可见解题证据。若只有最终答案而没有步骤，"
            "不得声称观察到了推理路径；证据不足时用 uncertain，不要更新该考点。",
        ]
    )
    result = _parse_agent_json(
        chat(PROMPTS["quiz_grade"], prompt, temperature=0.2),
        "判题与知识感知 Agent",
    )
    required = ("correct", "feedback", "hint", "concept_assessments")
    if any(key not in result for key in required):
        raise ValueError(f"判题 Agent 返回缺少必需字段：{', '.join(required)}")
    if not isinstance(result["correct"], bool):
        raise ValueError("判题结果中的 correct 必须为布尔值。")
    assessments = _validate_knowledge_assessments(
        result["concept_assessments"], requested_concepts
    )
    result["concept_assessments"] = assessments
    result["attempt"] = db.record_knowledge_perception(
        DATABASE_PATH, topic, question, result["correct"], assessments
    )
    return result


def _parse_agent_json(raw: str, agent_name: str) -> dict[str, Any]:
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{agent_name}未返回合法 JSON：{error}") from error
    if not isinstance(result, dict):
        raise ValueError(f"{agent_name}返回的 JSON 顶层必须是对象。")
    return result


def _validate_generated_exercise(
    result: dict[str, Any], targets: list[str], require_target: bool
) -> None:
    required = ("question", "answer", "concepts")
    if any(key not in result for key in required):
        raise ValueError(f"出题 Agent 返回缺少必需字段：{', '.join(required)}")
    concepts = result["concepts"]
    if (
        not isinstance(concepts, list)
        or not concepts
        or len(concepts) > 5
        or any(not isinstance(item, str) or not item.strip() for item in concepts)
    ):
        raise ValueError("出题 Agent 的 concepts 必须是 1-5 个非空考点名称。")
    if not isinstance(result["question"], str) or not result["question"].strip():
        raise ValueError("出题 Agent 返回的题目不能为空。")
    if not isinstance(result["answer"], str) or not result["answer"].strip():
        raise ValueError("出题 Agent 返回的参考答案不能为空。")
    target_names = {item.casefold() for item in targets}
    if require_target and target_names and not any(
        concept.casefold() in target_names for concept in concepts
    ):
        raise ValueError("出题 Agent 返回的考点未覆盖指定目标。")


def _validate_knowledge_assessments(
    value: Any, requested_concepts: list[str]
) -> list[dict[str, Any]]:
    allowed_evidence = {
        "demonstrated",
        "partial",
        "not_demonstrated",
        "uncertain",
    }
    if not isinstance(value, list) or not value or len(value) > 8:
        raise ValueError("判题 Agent 必须返回 1-8 项知识感知结果。")
    expected = {item.casefold() for item in requested_concepts}
    assessments = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("知识感知结果中的考点评估必须是对象。")
        concept = item.get("concept")
        evidence = item.get("evidence")
        confidence = item.get("confidence")
        rationale = item.get("rationale")
        if (
            not isinstance(concept, str)
            or concept.casefold() not in expected
            or concept.casefold() in seen
            or evidence not in allowed_evidence
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
            or not isinstance(rationale, str)
            or len(rationale) > 400
        ):
            raise ValueError("判题 Agent 返回了无效或不匹配的知识感知结果。")
        seen.add(concept.casefold())
        assessments.append(
            {
                "concept": concept.strip(),
                "evidence": evidence,
                "confidence": float(confidence),
                "rationale": rationale.strip(),
            }
        )
    return assessments


def perceive_dialogue_knowledge(
    *,
    module: str,
    topic: str,
    user_message: str,
    history: list[dict[str, str]],
) -> dict[str, Any]:
    recent_context = [
        {
            "role": item["role"],
            "content": item["content"][-300:],
        }
        for item in history[-2:]
    ]
    current_states = db.get_knowledge(DATABASE_PATH)
    normalized_topic = topic.strip().casefold()
    relevant_states = [
        item
        for item in current_states
        if normalized_topic
        and (
            normalized_topic in item["topic"].casefold()
            or item["topic"].casefold() in normalized_topic
        )
    ][:8]
    if not relevant_states:
        relevant_states = current_states[:8]
    relevant_states = [
        {
            "topic": item["topic"],
            "mastery": item["mastery"],
            "confidence": item["confidence"],
        }
        for item in relevant_states
    ]
    evidence_text = user_message
    if len(evidence_text) > 3000:
        evidence_text = evidence_text[:1500] + "\n…\n" + evidence_text[-1500:]
    prompt = "\n\n".join(
        [
            f"当前模块：{module}",
            f"用户填写的知识主题：{topic or '未提供'}",
            _context_block("当前相关 KT 状态，仅供考点对齐", relevant_states),
            _context_block("此前对话，仅供理解题目背景，不是本轮学习证据", recent_context),
            "本轮唯一可用于判断掌握情况的学生消息：\n"
            + evidence_text,
        ]
    )
    result = _parse_agent_json(
        chat(PROMPTS["dialogue_knowledge"], prompt, temperature=0.1),
        "对话知识感知 Agent",
    )
    observations = result.get("observations")
    if not isinstance(observations, list) or len(observations) > 5:
        raise ValueError("对话知识感知 Agent 返回的 observations 必须是最多 5 项的数组。")
    allowed_evidence = {"demonstrated", "partial", "not_demonstrated"}
    assessments = []
    seen = set()
    for item in observations:
        if not isinstance(item, dict):
            raise ValueError("对话知识感知结果中的考点评估必须是对象。")
        concept = item.get("concept")
        evidence = item.get("evidence")
        confidence = item.get("confidence")
        rationale = item.get("rationale")
        if (
            not isinstance(concept, str)
            or not concept.strip()
            or len(concept.strip()) > 120
            or concept.casefold() in seen
            or evidence not in allowed_evidence
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0.6 <= confidence <= 1
            or not isinstance(rationale, str)
            or not rationale.strip()
            or len(rationale) > 160
        ):
            raise ValueError("对话知识感知 Agent 返回了无效的学习证据。")
        seen.add(concept.casefold())
        assessments.append(
            {
                "concept": concept.strip(),
                "evidence": evidence,
                "confidence": float(confidence),
                "rationale": rationale.strip(),
            }
        )
    if not assessments:
        return {"updated_concepts": [], "knowledge": []}

    record_topic = topic.strip() or assessments[0]["concept"]
    recorded = db.record_knowledge_perception(
        DATABASE_PATH,
        record_topic,
        evidence_text,
        all(item["evidence"] == "demonstrated" for item in assessments),
        assessments,
    )
    return {
        "updated_concepts": [item["concept"] for item in assessments],
        "knowledge": recorded["knowledge"],
    }


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
