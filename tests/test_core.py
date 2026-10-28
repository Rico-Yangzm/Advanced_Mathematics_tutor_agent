import tempfile
import unittest
from pathlib import Path

from app.db import (
    add_chat_message,
    connect,
    get_chat_messages,
    get_knowledge,
    initialize_database,
    record_attempt,
    replace_global_memories,
    search_global_memories,
    search_old_chat_messages,
)
from app.rag import search_documents, split_text


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "test.sqlite3"
        initialize_database(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_knowledge_updates_once_per_four_attempts(self):
        results = [
            record_attempt(self.database, "极限", "计算极限", correct)
            for correct in (True, False, True, True)
        ]
        self.assertEqual([item["pending_attempts"] for item in results], [1, 2, 3, 0])
        self.assertEqual([item["knowledge_updated"] for item in results], [False, False, False, True])
        state = get_knowledge(self.database)[0]
        self.assertAlmostEqual(state["mastery"], 0.60)
        self.assertEqual(state["total_attempts"], 4)

    def test_splits_text_and_fts_search_returns_source_chunk(self):
        chunks = split_text("定积分用于计算曲线下面积。微积分基本定理连接导数与积分。")
        self.assertTrue(chunks)
        with connect(self.database) as connection:
            cursor = connection.execute(
                "INSERT INTO documents(source, chunk_count) VALUES (?, ?)",
                ("微积分讲义.pdf", 1),
            )
            chunk_id = connection.execute(
                "INSERT INTO rag_chunks(document_id, chunk_index, content) VALUES (?, ?, ?)",
                (cursor.lastrowid, 0, "定积分用于计算曲线下面积。微积分基本定理连接导数与积分。"),
            ).lastrowid
            connection.execute(
                "INSERT INTO rag_chunks_fts(source, chunk_id, content, searchable) "
                "VALUES (?, ?, ?, ?)",
                (
                    "微积分讲义.pdf",
                    str(chunk_id),
                    "定积分用于计算曲线下面积。微积分基本定理连接导数与积分。",
                    "定 积 分 用 于 计 算 曲 线 下 面 积 。 微 积 分 基 本 定 理 连 接 导 数 与 积 分 。",
                ),
            )
        results = search_documents(self.database, "定积分")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "微积分讲义.pdf")
        self.assertIn("定积分", results[0]["content"])

    def test_module_history_is_persistent_and_retrievable(self):
        first_id = add_chat_message(
            self.database, "session-12345", "tutor", "user", "我卡在定积分换元。"
        )
        add_chat_message(
            self.database, "session-12345", "tutor", "assistant", "先观察被积函数。"
        )
        latest_id = add_chat_message(
            self.database, "session-12345", "tutor", "user", "那下一步呢？"
        )
        add_chat_message(
            self.database, "session-12345", "roadmap", "user", "我每周复习十小时。"
        )

        history = get_chat_messages(self.database, "session-12345", "tutor")
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["id"], first_id)
        self.assertEqual(history[-1]["id"], latest_id)
        matches = search_old_chat_messages(
            self.database, "session-12345", "tutor", "定积分", latest_id
        )
        self.assertEqual(len(matches), 1)
        self.assertIn("定积分换元", matches[0]["content"])

    def test_global_memory_is_session_scoped_and_searchable(self):
        replace_global_memories(
            self.database,
            "session-12345",
            "tutor",
            {"learning_preference": "讲题时希望一次只给一个小提示。"},
        )
        replace_global_memories(
            self.database,
            "session-67890",
            "roadmap",
            {"study_schedule": "每周复习十小时。"},
        )
        memories = search_global_memories(
            self.database, "session-12345", "小提示"
        )
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["category"], "learning_preference")
        self.assertEqual(
            search_global_memories(self.database, "session-67890", "小提示"), []
        )


if __name__ == "__main__":
    unittest.main()
