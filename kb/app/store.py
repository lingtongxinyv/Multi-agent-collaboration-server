"""SQLite 存储层：文档、分块、全文索引、向量。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

import numpy as np

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    title TEXT,
    content_hash TEXT NOT NULL,
    mtime REAL,
    size_bytes INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    path TEXT NOT NULL,
    source TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    vec BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_docs_source ON documents(source);
"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(cfg: dict[str, Any] | None = None):
    conn = sqlite3.connect(config.db_path(cfg), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(cfg: dict[str, Any] | None = None) -> None:
    with connect(cfg) as conn:
        conn.executescript(SCHEMA)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    """对上层屏蔽 SQL 细节。向量矩阵惰性加载并缓存。"""

    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg or config.load()
        init_db(self.cfg)
        self._vec_ids: np.ndarray | None = None
        self._vec_mat: np.ndarray | None = None
        self._vec_version: tuple | None = None

    # ---------- 写入 ----------

    def upsert_document(self, doc: dict[str, Any]) -> tuple[int, str]:
        """返回 (doc_id, 'new' | 'updated' | 'unchanged')。内容未变则跳过重建分块。"""
        digest = content_hash(doc["text"])
        with connect(self.cfg) as conn:
            row = conn.execute(
                "SELECT id, content_hash, status FROM documents WHERE path = ?",
                (doc["path"],),
            ).fetchone()
            if row and row["content_hash"] == digest and row["status"] == "active":
                return row["id"], "unchanged"
            if row:
                action = "updated" if row["status"] != "missing" else "new"
                doc_id = row["id"]
                conn.execute(
                    "UPDATE documents SET source=?, title=?, content_hash=?, mtime=?,"
                    " size_bytes=?, status='active', updated_at=? WHERE id=?",
                    (
                        doc["source"], doc.get("title", ""), digest,
                        doc.get("mtime", 0.0), doc.get("size_bytes", 0), _now(), doc_id,
                    ),
                )
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            else:
                cur = conn.execute(
                    "INSERT INTO documents(source, path, title, content_hash, mtime,"
                    " size_bytes, status, updated_at) VALUES(?,?,?,?,?,?, 'active', ?)",
                    (
                        doc["source"], doc["path"], doc.get("title", ""), digest,
                        doc.get("mtime", 0.0), doc.get("size_bytes", 0), _now(),
                    ),
                )
                doc_id = cur.lastrowid
                action = "new"
        self._invalidate()
        return doc_id, action

    def write_chunks(self, doc_id: int, chunks: Iterable[dict[str, Any]],
                     vectors: list[list[float]] | None = None) -> int:
        count = 0
        with connect(self.cfg) as conn:
            for i, ch in enumerate(chunks):
                vec = None
                if vectors is not None and i < len(vectors) and vectors[i]:
                    vec = np.asarray(vectors[i], dtype=np.float32).tobytes()
                conn.execute(
                    "INSERT INTO chunks(doc_id, seq, path, source, start_line, end_line,"
                    " heading, text, vec) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        doc_id, ch["seq"], ch["path"], ch["source"], ch["start_line"],
                        ch["end_line"], ch.get("heading", ""), ch["text"], vec,
                    ),
                )
                count += 1
        self._invalidate()
        return count

    def mark_missing(self, source: str, known_paths: set[str]) -> list[str]:
        """把源里已不存在的文档标记为 missing（不物理删除，便于追溯）。"""
        removed = []
        with connect(self.cfg) as conn:
            rows = conn.execute(
                "SELECT id, path FROM documents WHERE source=? AND status='active'",
                (source,),
            ).fetchall()
            for row in rows:
                if row["path"] not in known_paths:
                    conn.execute(
                        "UPDATE documents SET status='missing', updated_at=? WHERE id=?",
                        (_now(), row["id"]),
                    )
                    conn.execute("DELETE FROM chunks WHERE doc_id=?", (row["id"],))
                    removed.append(row["path"])
        if removed:
            self._invalidate()
        return removed

    def archive_document(self, path: str) -> bool:
        """把单篇文档软删除：状态置 missing 并清掉分块。返回是否命中。"""
        with connect(self.cfg) as conn:
            row = conn.execute(
                "SELECT id FROM documents WHERE path=? AND status='active'", (path,)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE documents SET status='missing', updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (row["id"],))
        self._invalidate()
        return True

    def _invalidate(self) -> None:
        self._vec_ids = None
        self._vec_mat = None
        self._vec_version = None

    # ---------- 读取 ----------

    def stats(self) -> dict[str, Any]:
        with connect(self.cfg) as conn:
            docs = conn.execute(
                "SELECT source, COUNT(*) AS n FROM documents WHERE status='active' GROUP BY source"
            ).fetchall()
            chunk_n = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            vec_n = conn.execute("SELECT COUNT(*) AS n FROM chunks WHERE vec IS NOT NULL").fetchone()["n"]
        return {
            "documents": {r["source"]: r["n"] for r in docs},
            "documents_total": sum(r["n"] for r in docs),
            "chunks": chunk_n,
            "chunks_with_vector": vec_n,
        }

    def chunks_without_vector(self, limit: int = 100) -> list[tuple[int, str]]:
        with connect(self.cfg) as conn:
            rows = conn.execute(
                "SELECT id, text FROM chunks WHERE vec IS NULL ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [(r["id"], r["text"]) for r in rows]

    def set_vectors(self, pairs: list[tuple[int, list[float]]]) -> None:
        with connect(self.cfg) as conn:
            for cid, vec in pairs:
                conn.execute(
                    "UPDATE chunks SET vec=? WHERE id=?",
                    (np.asarray(vec, dtype=np.float32).tobytes(), cid),
                )
        self._invalidate()

    def chunk_version(self) -> tuple[int, int]:
        with connect(self.cfg) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS mx FROM chunks"
            ).fetchone()
        return (int(row["n"]), int(row["mx"]))

    def load_chunk_texts(self) -> list[tuple[int, str]]:
        """取出全部 chunk 的 (id, text)，供内存 BM25 建索引。"""
        with connect(self.cfg) as conn:
            rows = conn.execute("SELECT id, text FROM chunks ORDER BY id").fetchall()
        return [(r["id"], r["text"]) for r in rows]

    def _load_vectors(self) -> None:
        with connect(self.cfg) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS mx FROM chunks WHERE vec IS NOT NULL"
            ).fetchone()
            version = (row["n"], row["mx"])
            if self._vec_version == version and self._vec_ids is not None:
                return
            rows = conn.execute(
                "SELECT id, vec FROM chunks WHERE vec IS NOT NULL ORDER BY id"
            ).fetchall()
        if not rows:
            self._vec_ids = np.zeros(0, dtype=np.int64)
            self._vec_mat = np.zeros((0, 0), dtype=np.float32)
        else:
            ids = np.array([r["id"] for r in rows], dtype=np.int64)
            mats = [np.frombuffer(r["vec"], dtype=np.float32) for r in rows]
            dim = mats[0].shape[0]
            mat = np.vstack([m if m.shape[0] == dim else np.zeros(dim, dtype=np.float32) for m in mats])
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._vec_ids = ids
            self._vec_mat = mat / norms
        self._vec_version = version

    def vector_search(self, vec: list[float] | None, limit: int = 40) -> list[tuple[int, float]]:
        if not vec:
            return []
        self._load_vectors()
        if self._vec_ids is None or len(self._vec_ids) == 0:
            return []
        q = np.asarray(vec, dtype=np.float32)
        if q.shape[0] != self._vec_mat.shape[1]:
            return []
        n = np.linalg.norm(q)
        if n == 0:
            return []
        scores = self._vec_mat @ (q / n)
        k = min(limit, scores.shape[0])
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        return [(int(self._vec_ids[i]), float(scores[i])) for i in idx]

    def fetch_chunks(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        out: dict[int, dict[str, Any]] = {}
        with connect(self.cfg) as conn:
            for start in range(0, len(ids), 200):
                part = ids[start:start + 200]
                placeholders = ",".join("?" * len(part))
                rows = conn.execute(
                    f"SELECT id, path, source, start_line, end_line, heading, text, doc_id"
                    f" FROM chunks WHERE id IN ({placeholders})",
                    part,
                ).fetchall()
                for r in rows:
                    out[r["id"]] = dict(r)
        return out

    def dump_json(self, path, ids: list[int]) -> None:
        data = self.fetch_chunks(ids)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
