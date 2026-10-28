import re
import sqlite3
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from app.db import connect, searchable_text


def split_text(text: str, chunk_size: int = 1200, overlap: int = 180) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        if end < len(normalized):
            boundary = normalized.rfind(" ", start + chunk_size // 2, end)
            if boundary > start:
                end = boundary
        chunks.append(normalized[start:end].strip())
        if end == len(normalized):
            break
        start = max(start + 1, end - overlap)
    return chunks


def ingest_pdf(
    database_path: Path | str, source: str, content: bytes
) -> dict[str, int | str]:
    reader = PdfReader(BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    chunks = split_text(text)
    if not chunks:
        raise ValueError("PDF 中未提取到可检索文本；扫描版 PDF 请先进行 OCR。")

    with connect(database_path) as connection:
        existing = connection.execute(
            "SELECT id FROM documents WHERE source = ?", (source,)
        ).fetchone()
        if existing:
            connection.execute("DELETE FROM rag_chunks_fts WHERE source = ?", (source,))
            connection.execute("DELETE FROM documents WHERE id = ?", (existing["id"],))
        cursor = connection.execute(
            "INSERT INTO documents(source, chunk_count) VALUES (?, ?)",
            (source, len(chunks)),
        )
        document_id = cursor.lastrowid
        for index, chunk in enumerate(chunks):
            chunk_cursor = connection.execute(
                "INSERT INTO rag_chunks(document_id, chunk_index, content) "
                "VALUES (?, ?, ?)",
                (document_id, index, chunk),
            )
            connection.execute(
                "INSERT INTO rag_chunks_fts(source, chunk_id, content, searchable) "
                "VALUES (?, ?, ?, ?)",
                (source, str(chunk_cursor.lastrowid), chunk, searchable_text(chunk)),
            )
    return {"source": source, "chunks": len(chunks), "pages": len(reader.pages)}


def search_documents(
    database_path: Path | str, query: str, limit: int = 5
) -> list[dict[str, str]]:
    terms = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", query)
    if not terms:
        return []
    phrases = []
    for term in terms:
        phrase = " ".join(term) if re.fullmatch(r"[\u4e00-\u9fff]+", term) else term
        phrases.append(f'"{phrase.replace(chr(34), chr(34) * 2)}"')
    expression = " OR ".join(phrases)
    try:
        with connect(database_path) as connection:
            rows = connection.execute(
                """
                SELECT source, chunk_id, content,
                       bm25(rag_chunks_fts) AS rank
                FROM rag_chunks_fts
                WHERE searchable MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (expression, limit),
            ).fetchall()
    except sqlite3.OperationalError as error:
        if "fts5" in str(error).lower() or "syntax" in str(error).lower():
            raise RuntimeError(f"RAG 检索失败：{error}") from error
        raise
    return [
        {"source": row["source"], "chunk_id": row["chunk_id"], "content": row["content"]}
        for row in rows
    ]
