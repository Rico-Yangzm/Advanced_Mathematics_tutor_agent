import sqlite3
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def connect(database_path: Path | str) -> Iterator[sqlite3.Connection]:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(database_path: Path | str) -> None:
    with connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL UNIQUE,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5(
                source UNINDEXED, chunk_id UNINDEXED, content UNINDEXED, searchable
            );
            CREATE TABLE IF NOT EXISTS knowledge_points (
                topic TEXT PRIMARY KEY,
                mastery REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.0,
                total_attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                question TEXT NOT NULL,
                correct INTEGER NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_topic_pending
                ON attempts(topic, applied, id);
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                module TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_session_module
                ON chat_messages(session_id, module, id);
            CREATE TABLE IF NOT EXISTS chat_summaries (
                session_id TEXT NOT NULL,
                module TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                summarized_through INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(session_id, module)
            );
            CREATE TABLE IF NOT EXISTS chat_state (
                session_id TEXT NOT NULL,
                module TEXT NOT NULL,
                state_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(session_id, module)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5(
                session_id UNINDEXED, module UNINDEXED, message_id UNINDEXED,
                searchable
            );
            CREATE TABLE IF NOT EXISTS global_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                source_module TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(session_id, category)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS global_memories_fts USING fts5(
                session_id UNINDEXED, memory_id UNINDEXED, searchable
            );
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(rag_chunks_fts)").fetchall()
        }
        if "searchable" not in columns:
            connection.execute("DROP TABLE rag_chunks_fts")
            connection.execute(
                "CREATE VIRTUAL TABLE rag_chunks_fts USING fts5("
                "source UNINDEXED, chunk_id UNINDEXED, content UNINDEXED, searchable)"
            )
            chunks = connection.execute(
                """
                SELECT d.source, c.id, c.content
                FROM rag_chunks c JOIN documents d ON d.id = c.document_id
                """
            ).fetchall()
            connection.executemany(
                "INSERT INTO rag_chunks_fts(source, chunk_id, content, searchable) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        row["source"],
                        str(row["id"]),
                        row["content"],
                        searchable_text(row["content"]),
                    )
                    for row in chunks
                ],
            )


def searchable_text(text: str) -> str:
    return re.sub(r"([\u4e00-\u9fff])", r" \1 ", text)


def list_documents(database_path: Path | str) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, chunk_count, created_at FROM documents ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def delete_document(database_path: Path | str, source: str) -> bool:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT id FROM documents WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            return False
        connection.execute("DELETE FROM rag_chunks_fts WHERE source = ?", (source,))
        connection.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
    return True


def record_attempt(
    database_path: Path | str, topic: str, question: str, correct: bool
) -> dict[str, Any]:
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO attempts(topic, question, correct) VALUES (?, ?, ?)",
            (topic, question, int(correct)),
        )
        connection.execute(
            "INSERT OR IGNORE INTO knowledge_points(topic) VALUES (?)", (topic,)
        )
        pending = connection.execute(
            "SELECT id, correct FROM attempts "
            "WHERE topic = ? AND applied = 0 ORDER BY id",
            (topic,),
        ).fetchall()
        updated = False
        while len(pending) >= 4:
            batch = pending[:4]
            row = connection.execute(
                "SELECT mastery, total_attempts FROM knowledge_points WHERE topic = ?",
                (topic,),
            ).fetchone()
            accuracy = sum(item["correct"] for item in batch) / 4
            mastery = 0.6 * row["mastery"] + 0.4 * accuracy
            total_attempts = row["total_attempts"] + 4
            confidence = min(1.0, total_attempts / 12)
            connection.execute(
                "UPDATE knowledge_points SET mastery = ?, confidence = ?, "
                "total_attempts = ?, updated_at = CURRENT_TIMESTAMP WHERE topic = ?",
                (mastery, confidence, total_attempts, topic),
            )
            connection.executemany(
                "UPDATE attempts SET applied = 1 WHERE id = ?",
                [(item["id"],) for item in batch],
            )
            pending = pending[4:]
            updated = True
        state = connection.execute(
            "SELECT topic, mastery, confidence, total_attempts, updated_at "
            "FROM knowledge_points WHERE topic = ?",
            (topic,),
        ).fetchone()
    return {
        "knowledge": dict(state),
        "pending_attempts": len(pending),
        "knowledge_updated": updated,
    }


def get_knowledge(database_path: Path | str) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT k.topic, k.mastery, k.confidence, k.total_attempts, k.updated_at,
                   (SELECT COUNT(*) FROM attempts a
                    WHERE a.topic = k.topic AND a.applied = 0) AS pending_attempts
            FROM knowledge_points k ORDER BY k.mastery ASC, k.topic ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_attempts(database_path: Path | str, topic: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT topic, question, correct, applied, created_at FROM attempts"
    parameters: tuple[Any, ...] = ()
    if topic:
        query += " WHERE topic = ?"
        parameters = (topic,)
    query += " ORDER BY id DESC LIMIT 100"
    with connect(database_path) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


def _fts_expression(query: str) -> str:
    terms = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", query)
    phrases = []
    for term in terms:
        phrase = " ".join(term) if re.fullmatch(r"[\u4e00-\u9fff]+", term) else term
        phrases.append(f'"{phrase.replace(chr(34), chr(34) * 2)}"')
    return " OR ".join(phrases)


def add_chat_message(
    database_path: Path | str,
    session_id: str,
    module: str,
    role: str,
    content: str,
) -> int:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO chat_messages(session_id, module, role, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, module, role, content),
        )
        message_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO chat_messages_fts(session_id, module, message_id, searchable) "
            "VALUES (?, ?, ?, ?)",
            (session_id, module, str(message_id), searchable_text(content)),
        )
    return message_id


def get_chat_messages(
    database_path: Path | str, session_id: str, module: str, limit: int = 200
) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, role, content, created_at FROM chat_messages "
            "WHERE session_id = ? AND module = ? ORDER BY id DESC LIMIT ?",
            (session_id, module, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_recent_chat_messages(
    database_path: Path | str, session_id: str, module: str, limit: int = 12
) -> list[dict[str, Any]]:
    return get_chat_messages(database_path, session_id, module, limit)


def get_chat_summary(
    database_path: Path | str, session_id: str, module: str
) -> dict[str, Any]:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT summary, summarized_through FROM chat_summaries "
            "WHERE session_id = ? AND module = ?",
            (session_id, module),
        ).fetchone()
    return dict(row) if row else {"summary": "", "summarized_through": 0}


def get_unsummarized_chat_messages(
    database_path: Path | str, session_id: str, module: str, after_id: int
) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, role, content FROM chat_messages "
            "WHERE session_id = ? AND module = ? AND id > ? ORDER BY id LIMIT 32",
            (session_id, module, after_id),
        ).fetchall()
    return [dict(row) for row in rows]


def save_chat_summary(
    database_path: Path | str,
    session_id: str,
    module: str,
    summary: str,
    summarized_through: int,
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO chat_summaries(session_id, module, summary, summarized_through)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id, module) DO UPDATE SET
                summary = excluded.summary,
                summarized_through = excluded.summarized_through,
                updated_at = CURRENT_TIMESTAMP
            """,
            (session_id, module, summary, summarized_through),
        )


def search_old_chat_messages(
    database_path: Path | str,
    session_id: str,
    module: str,
    query: str,
    before_id: int,
    limit: int = 4,
) -> list[dict[str, Any]]:
    expression = _fts_expression(query)
    if not expression:
        return []
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT m.id, m.role, m.content, m.created_at
            FROM chat_messages_fts f
            JOIN chat_messages m ON m.id = CAST(f.message_id AS INTEGER)
            WHERE chat_messages_fts MATCH ?
              AND f.session_id = ? AND f.module = ?
              AND m.id < ?
            ORDER BY bm25(chat_messages_fts)
            LIMIT ?
            """,
            (f"searchable : ({expression})", session_id, module, before_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_global_memories(
    database_path: Path | str, session_id: str
) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, category, content, source_module, updated_at "
            "FROM global_memories WHERE session_id = ? "
            "ORDER BY updated_at DESC, id DESC",
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def search_global_memories(
    database_path: Path | str, session_id: str, query: str, limit: int = 5
) -> list[dict[str, Any]]:
    expression = _fts_expression(query)
    if not expression:
        return []
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT g.id, g.category, g.content, g.source_module, g.updated_at
            FROM global_memories_fts f
            JOIN global_memories g ON g.id = CAST(f.memory_id AS INTEGER)
            WHERE global_memories_fts MATCH ?
              AND f.session_id = ?
            ORDER BY bm25(global_memories_fts)
            LIMIT ?
            """,
            (f"searchable : ({expression})", session_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_global_memory(
    database_path: Path | str,
    session_id: str,
    category: str,
    content: str,
    source_module: str,
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO global_memories(session_id, category, content, source_module)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id, category) DO UPDATE SET
                content = excluded.content,
                source_module = excluded.source_module,
                updated_at = CURRENT_TIMESTAMP
            """,
            (session_id, category, content, source_module),
        )
        memory = connection.execute(
            "SELECT id FROM global_memories WHERE session_id = ? AND category = ?",
            (session_id, category),
        ).fetchone()
        connection.execute(
            "DELETE FROM global_memories_fts WHERE memory_id = ?",
            (str(memory["id"]),),
        )
        connection.execute(
            "INSERT INTO global_memories_fts(session_id, memory_id, searchable) "
            "VALUES (?, ?, ?)",
            (session_id, str(memory["id"]), searchable_text(content)),
        )


def replace_global_memories(
    database_path: Path | str,
    session_id: str,
    source_module: str,
    memories: dict[str, str],
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "DELETE FROM global_memories_fts WHERE session_id = ?", (session_id,)
        )
        connection.execute(
            "DELETE FROM global_memories WHERE session_id = ?", (session_id,)
        )
        for category, content in memories.items():
            cursor = connection.execute(
                """
                INSERT INTO global_memories(session_id, category, content, source_module)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, category, content, source_module),
            )
            connection.execute(
                "INSERT INTO global_memories_fts(session_id, memory_id, searchable) "
                "VALUES (?, ?, ?)",
                (session_id, str(cursor.lastrowid), searchable_text(content)),
            )


def delete_global_memory(
    database_path: Path | str, session_id: str, memory_id: int
) -> bool:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT id FROM global_memories WHERE session_id = ? AND id = ?",
            (session_id, memory_id),
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            "DELETE FROM global_memories_fts WHERE memory_id = ?", (str(memory_id),)
        )
        connection.execute(
            "DELETE FROM global_memories WHERE id = ?", (memory_id,)
        )
    return True


def get_module_state(
    database_path: Path | str, session_id: str, module: str
) -> dict[str, Any]:
    import json

    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT state_json FROM chat_state WHERE session_id = ? AND module = ?",
            (session_id, module),
        ).fetchone()
    return json.loads(row["state_json"]) if row else {}


def set_module_state(
    database_path: Path | str,
    session_id: str,
    module: str,
    state: dict[str, Any],
) -> None:
    import json

    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO chat_state(session_id, module, state_json) VALUES (?, ?, ?)
            ON CONFLICT(session_id, module) DO UPDATE SET state_json = excluded.state_json
            """,
            (session_id, module, json.dumps(state, ensure_ascii=False)),
        )


def clear_chat_module(
    database_path: Path | str, session_id: str, module: str
) -> None:
    with connect(database_path) as connection:
        message_ids = connection.execute(
            "SELECT id FROM chat_messages WHERE session_id = ? AND module = ?",
            (session_id, module),
        ).fetchall()
        connection.executemany(
            "DELETE FROM chat_messages_fts WHERE message_id = ?",
            [(str(row["id"]),) for row in message_ids],
        )
        connection.execute(
            "DELETE FROM chat_messages WHERE session_id = ? AND module = ?",
            (session_id, module),
        )
        connection.execute(
            "DELETE FROM chat_summaries WHERE session_id = ? AND module = ?",
            (session_id, module),
        )
        connection.execute(
            "DELETE FROM chat_state WHERE session_id = ? AND module = ?",
            (session_id, module),
        )
