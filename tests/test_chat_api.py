import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


_temp_dir = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(_temp_dir.name) / "chat-api.sqlite3")

from fastapi.testclient import TestClient

from app.main import app
from app import db
from app.config import DATABASE_PATH


class ChatApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.session_id = f"test-session-{uuid.uuid4().hex[:12]}"

    def tearDown(self):
        self.client.close()

    def test_bundled_math_knowledge_is_loaded_into_rag(self):
        documents = self.client.get("/api/rag/documents").json()["documents"]
        sources = {document["source"] for document in documents}

        self.assertEqual(len(sources), 32)
        self.assertIn(
            "11408-math/高等数学/第01讲_函数极限与连续.md", sources
        )
        search = self.client.post(
            "/api/rag/search", json={"query": "函数极限", "limit": 5}
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertTrue(search.json()["results"])

    def test_generated_exercise_is_revised_after_quality_feedback(self):
        candidate = (
            '{"question":"求极限 x/sin(x)。","topic":"极限",'
            '"difficulty":"中等","concept":"极限","concepts":["极限"],'
            '"answer":"1"}'
        )
        revised = (
            '{"question":"求极限 (1-cos(x))/x^2。","topic":"极限",'
            '"difficulty":"中等","concept":"极限","concepts":["极限"],'
            '"answer":"1/2"}'
        )
        responses = [
            candidate,
            '{"approved":false,"issues":["考点提示不足"],'
            '"revision_instructions":"在题干明确考查等价无穷小。"}',
            revised,
            '{"approved":true,"issues":[],"revision_instructions":""}',
        ]
        with patch("app.agents.chat", side_effect=responses) as model:
            response = self.client.post(
                "/api/agents/quiz-grade",
                json={
                    "mode": "generate",
                    "topic": "极限",
                    "concepts": ["极限"],
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("1-cos(x)", response.json()["question"])
        self.assertNotIn("answer", response.json())
        self.assertEqual(model.call_count, 4)
        self.assertIn("在题干明确考查等价无穷小", model.call_args_list[2].args[1])

    def test_chat_persists_history_and_shared_memory(self):
        agent_prompts = []

        def fake_agent(system_prompt, user_prompt, temperature=0.3):
            agent_prompts.append((system_prompt, user_prompt))
            if system_prompt.startswith("你是考研数学学习证据整理器"):
                return '{"observations":[]}'
            return "先看清楚题目要求什么。"

        def fake_main(system_prompt, user_prompt, temperature=0.3):
            return json.dumps(
                {
                    "memories": [
                        {
                            "category": "learning_preference",
                            "content": "讲题时希望一次只给一个提示。",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        with (
            patch("app.agents.chat", side_effect=fake_agent),
            patch("app.chat.chat", side_effect=fake_main),
        ):
            response = self.client.post(
                "/api/chat/tutor",
                json={
                    "session_id": self.session_id,
                    "message": "我卡在极限题第一步。",
                    "topic": "极限",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["memory_updated"])

            history = self.client.get(
                f"/api/chat/tutor?session_id={self.session_id}"
            )
            self.assertEqual(history.status_code, 200)
            self.assertEqual(len(history.json()["messages"]), 2)

            memory = self.client.get(
                f"/api/memory?session_id={self.session_id}"
            )
            self.assertEqual(
                memory.json()["memories"][0]["content"],
                "讲题时希望一次只给一个提示。",
            )

            analysis = self.client.post(
                "/api/chat/mastery",
                json={
                    "session_id": self.session_id,
                    "message": "请分析薄弱点。",
                },
            )
            self.assertEqual(analysis.status_code, 200, analysis.text)
            self.assertTrue(
                any(
                    "一次只给一个提示" in prompt
                    for system_prompt, prompt in agent_prompts
                    if system_prompt.startswith("你是考研数学掌握分析 Agent")
                )
            )

    def test_tutor_dialogue_updates_kt_from_explicit_user_reasoning(self):
        prompts = []

        def fake_agent(system_prompt, user_prompt, temperature=0.3):
            prompts.append((system_prompt, user_prompt))
            if system_prompt.startswith("你是考研数学学习证据整理器"):
                self.assertIn("f'(x)>0", user_prompt)
                return json.dumps(
                    {
                        "observations": [
                            {
                                "concept": "单调性判定",
                                "evidence": "demonstrated",
                                "confidence": 0.9,
                                "rationale": "用户说明导数为正，所以函数递增。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            return "你的推理方向正确。"

        with (
            patch("app.agents.chat", side_effect=fake_agent),
            patch("app.chat.chat", return_value='{"memories":[]}'),
        ):
            response = self.client.post(
                "/api/chat/tutor",
                json={
                    "session_id": self.session_id,
                    "message": "我算得 f'(x)>0，所以函数在区间上递增。",
                    "topic": "单调性",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["knowledge_update"]["updated_concepts"],
            ["单调性判定"],
        )
        self.assertIn("KT 证据记录", response.json()["message"])
        knowledge = self.client.get("/api/knowledge").json()["knowledge"]
        state = next(item for item in knowledge if item["topic"] == "单调性判定")
        self.assertEqual(state["latest_evidence"], "demonstrated")
        self.assertIn("f'(x)>0", prompts[-1][1])

    def test_dialogue_without_explicit_reasoning_does_not_update_kt(self):
        def fake_agent(system_prompt, user_prompt, temperature=0.3):
            if system_prompt.startswith("你是考研数学学习证据整理器"):
                return '{"observations":[]}'
            return "请先尝试写出你的解题步骤。"

        with (
            patch("app.agents.chat", side_effect=fake_agent),
            patch("app.chat.chat", return_value='{"memories":[]}'),
        ):
            response = self.client.post(
                "/api/chat/mastery",
                json={
                    "session_id": self.session_id,
                    "message": "请分析一下我的薄弱点。",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["knowledge_update"]["updated_concepts"], []
        )
        self.assertEqual(self.client.get("/api/knowledge").json()["knowledge"], [])

    def test_chat_rejects_unknown_modules_and_invalid_session_ids(self):
        unknown = self.client.get(
            f"/api/chat/unknown?session_id={self.session_id}"
        )
        self.assertEqual(unknown.status_code, 404)
        invalid_session = self.client.get("/api/chat/tutor?session_id=bad")
        self.assertEqual(invalid_session.status_code, 422)

    def test_long_chat_is_summarized_and_old_turns_are_retained(self):
        def fake_agent(system_prompt, user_prompt, temperature=0.3):
            if system_prompt.startswith("你是考研数学学习证据整理器"):
                return '{"observations":[]}'
            return "每轮回复。"

        def fake_main(system_prompt, user_prompt, temperature=0.3):
            if system_prompt.startswith("你负责压缩"):
                return "用户在复习极限，偏好简短提示。"
            return '{"memories":[]}'

        with (
            patch("app.agents.chat", side_effect=fake_agent),
            patch("app.chat.chat", side_effect=fake_main),
        ):
            for index in range(8):
                response = self.client.post(
                    "/api/chat/tutor",
                    json={
                        "session_id": self.session_id,
                        "message": f"极限复习的第 {index + 1} 个问题。",
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)

        history = self.client.get(
            f"/api/chat/tutor?session_id={self.session_id}"
        )
        self.assertTrue(history.json()["summary"])
        self.assertEqual(len(history.json()["messages"]), 16)
        older = db.search_old_chat_messages(
            DATABASE_PATH, self.session_id, "tutor", "第 1 个问题", 15
        )
        self.assertTrue(older)

    def test_practice_chat_keeps_question_for_hints_and_grading(self):
        knowledge_calls = []

        def fake_agent(system_prompt, user_prompt, temperature=0.3):
            if system_prompt.startswith("你是考研数学学习证据整理器"):
                knowledge_calls.append(user_prompt)
                return '{"observations":[]}'
            if system_prompt.startswith("你是考研数学练习题质量评审员"):
                return '{"approved":true,"issues":[],"revision_instructions":""}'
            if system_prompt.startswith("你是考研数学出题判题 Agent"):
                if "学生答案及可见解题过程：" in user_prompt:
                    return (
                        '{"correct":true,"feedback":"答案正确。","hint":"继续下一题。",'
                        '"concept_assessments":[{"concept":"重要极限",'
                        '"evidence":"demonstrated","confidence":0.8,'
                        '"rationale":"答案符合目标极限结论。"}]}'
                    )
                return (
                    '{"question":"求极限 sin(x)/x。","topic":"极限",'
                    '"difficulty":"中等","concept":"重要极限",'
                    '"concepts":["重要极限"],"answer":"1"}'
                )
            return "先回忆重要极限的结论，不要急着代入。"

        with (
            patch("app.agents.chat", side_effect=fake_agent),
            patch("app.chat.chat", return_value='{"memories":[]}'),
        ):
            generated = self.client.post(
                "/api/chat/practice",
                json={
                    "session_id": self.session_id,
                    "message": "出一道极限题",
                    "topic": "极限",
                },
            )
            self.assertEqual(generated.status_code, 200, generated.text)
            self.assertTrue(generated.json()["metadata"]["practice_pending"])
            self.assertNotIn("答案是 1", generated.json()["message"])

            hint = self.client.post(
                "/api/chat/practice",
                json={
                    "session_id": self.session_id,
                    "message": "给我一个提示",
                },
            )
            self.assertEqual(hint.status_code, 200, hint.text)
            self.assertTrue(hint.json()["metadata"]["practice_pending"])

            graded = self.client.post(
                "/api/chat/practice",
                json={
                    "session_id": self.session_id,
                    "message": "1",
                    "intent": "answer",
                },
            )
            self.assertEqual(graded.status_code, 200, graded.text)
            self.assertTrue(graded.json()["metadata"]["grading"]["correct"])
            self.assertEqual(
                graded.json()["metadata"]["grading"]["attempt"]["updated_concepts"],
                1,
            )
            self.assertEqual(
                len(knowledge_calls),
                2,
                "生成请求与提示请求各做一次整理；判题结果不应重复整理或更新 KT。",
            )
            self.assertFalse(
                self.client.get(
                    f"/api/chat/practice?session_id={self.session_id}"
                ).json()["practice_pending"]
            )


if __name__ == "__main__":
    unittest.main()
