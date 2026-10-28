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

    def test_chat_persists_history_and_shared_memory(self):
        agent_prompts = []

        def fake_agent(system_prompt, user_prompt, temperature=0.3):
            agent_prompts.append(user_prompt)
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
            self.assertIn("一次只给一个提示", agent_prompts[-1])

    def test_chat_rejects_unknown_modules_and_invalid_session_ids(self):
        unknown = self.client.get(
            f"/api/chat/unknown?session_id={self.session_id}"
        )
        self.assertEqual(unknown.status_code, 404)
        invalid_session = self.client.get("/api/chat/tutor?session_id=bad")
        self.assertEqual(invalid_session.status_code, 422)

    def test_long_chat_is_summarized_and_old_turns_are_retained(self):
        def fake_agent(system_prompt, user_prompt, temperature=0.3):
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
        def fake_agent(system_prompt, user_prompt, temperature=0.3):
            if "只返回合法 JSON 对象" in system_prompt:
                if "学生答案：" in user_prompt:
                    return '{"correct":true,"feedback":"答案正确。","hint":"继续下一题。"}'
                return (
                    '{"question":"求极限 sin(x)/x。","topic":"极限",'
                    '"difficulty":"中等","concept":"重要极限","answer":"1"}'
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
            self.assertFalse(
                self.client.get(
                    f"/api/chat/practice?session_id={self.session_id}"
                ).json()["practice_pending"]
            )


if __name__ == "__main__":
    unittest.main()
