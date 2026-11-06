import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import (
    add_chat_message,
    connect,
    get_chat_messages,
    get_knowledge,
    get_knowledge_observations,
    initialize_database,
    record_attempt,
    record_knowledge_perception,
    replace_global_memories,
    search_global_memories,
    search_old_chat_messages,
)
from app.rag import ingest_knowledge_pack, search_documents, split_text


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

    def test_knowledge_perception_updates_each_concept_from_evidence(self):
        result = record_knowledge_perception(
            self.database,
            "极限",
            "判断函数极限是否存在并说明理由",
            False,
            [
                {
                    "concept": "函数极限定义",
                    "evidence": "not_demonstrated",
                    "confidence": 0.8,
                    "rationale": "作答把函数值与极限值混为一谈。",
                },
                {
                    "concept": "连续性",
                    "evidence": "uncertain",
                    "confidence": 0.9,
                    "rationale": "答案没有涉及连续性。",
                },
            ],
        )

        self.assertEqual(result["updated_concepts"], 1)
        knowledge = {item["topic"]: item for item in get_knowledge(self.database)}
        state = knowledge["函数极限定义"]
        self.assertLess(state["mastery"], 0.5)
        self.assertEqual(state["total_attempts"], 1)
        self.assertAlmostEqual(state["confidence"], 0.2)
        self.assertEqual(state["latest_evidence"], "not_demonstrated")
        self.assertIn("混为一谈", state["latest_rationale"])
        self.assertEqual(
            get_knowledge_observations(self.database, ["函数极限定义"])[0]["evidence"],
            "not_demonstrated",
        )

    def test_database_initialization_migrates_existing_knowledge_state(self):
        legacy_database = Path(self.temp_dir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_database)
        try:
            connection.execute(
                "CREATE TABLE knowledge_points ("
                "topic TEXT PRIMARY KEY, mastery REAL NOT NULL DEFAULT 0.5, "
                "confidence REAL NOT NULL DEFAULT 0, "
                "total_attempts INTEGER NOT NULL DEFAULT 0, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO knowledge_points(topic, mastery, total_attempts) "
                "VALUES ('极限', 0.7, 4)"
            )
            connection.commit()
        finally:
            connection.close()

        initialize_database(legacy_database)

        with connect(legacy_database) as connection:
            state = connection.execute(
                "SELECT mastery, total_attempts, evidence_weight "
                "FROM knowledge_points WHERE topic = '极限'"
            ).fetchone()
        self.assertAlmostEqual(state["mastery"], 0.7)
        self.assertEqual(state["total_attempts"], 4)
        self.assertEqual(state["evidence_weight"], 4)

    def test_knowledge_pack_import_is_searchable_and_idempotent(self):
        knowledge_dir = Path(self.temp_dir.name) / "knowledge"
        subject_dir = knowledge_dir / "高等数学"
        subject_dir.mkdir(parents=True)
        (subject_dir / "极限.md").write_text(
            "函数极限定义描述函数在某点附近的变化趋势。", encoding="utf-8"
        )

        first = ingest_knowledge_pack(self.database, knowledge_dir)
        second = ingest_knowledge_pack(self.database, knowledge_dir)

        self.assertEqual(first["documents"], 1)
        self.assertEqual(second["documents"], 0)
        results = search_documents(self.database, "函数极限定义")
        self.assertEqual(results[0]["source"], "11408-math/高等数学/极限.md")

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
