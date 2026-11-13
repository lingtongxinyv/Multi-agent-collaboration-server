"""入库调度：采集 → 增量判重 → 分块 → 向量化 → 落库。"""
from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import config
from .chunk import split
from .collectors import collect_all
from .embed import Embedder
from .store import Store

MARKDOWN_EXTS = {".md", ".markdown"}
NOTES_ROOT = config.ROOT / "data" / "kb-notes"


def _note_path(rel: str) -> Path:
    """把相对路径限制在 data/kb-notes 下的 .md 文件，防止穿越。"""
    if not rel.strip():
        raise ValueError("path 不能为空")
    candidate = (NOTES_ROOT / rel).resolve()
    root = NOTES_ROOT.resolve()
    if root not in candidate.parents:
        raise ValueError("path 必须是 data/kb-notes 下的相对路径")
    if candidate.suffix.lower() not in MARKDOWN_EXTS:
        candidate = candidate.with_suffix(".md")
    return candidate


def index_document(store: Store, embedder: Embedder, doc: dict[str, Any],
                   vector_ok: bool) -> dict[str, Any]:
    """单篇落库：判重 → 分块 → 向量化 → 写 chunks。"""
    out: dict[str, Any] = {"action": "skip", "chunks": 0, "vectors": 0, "vector_ok": vector_ok}
    text = doc["text"]
    if not text or not text.strip():
        return out
    doc_id, action = store.upsert_document(doc)
    out["action"] = action
    if action == "unchanged":
        return out
    pieces = split(text, markdown=Path(doc["path"]).suffix.lower() in MARKDOWN_EXTS)
    if not pieces:
        return out
    chunks = [
        {
            "seq": p["seq"],
            "path": doc["path"],
            "source": doc["source"],
            "start_line": p["start_line"],
            "end_line": p["end_line"],
            "heading": p["heading"],
            "text": p["text"],
        }
        for p in pieces
    ]
    vectors = None
    if vector_ok:
        vectors = embedder.embed([c["text"] for c in chunks])
        if vectors:
            out["vectors"] = len(vectors)
        else:
            vector_ok = False
            out["vector_ok"] = False
    out["chunks"] = store.write_chunks(doc_id, chunks, vectors)
    return out


def write_note(cfg: dict[str, Any], path: str, content: str, title: str = "") -> dict[str, Any]:
    """写入/更新一条知识（落在 data/kb-notes，源名 kb），并立即入库。"""
    target = _note_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    doc = {
        "source": "kb",
        "path": str(target).replace("\\", "/"),
        "title": title or path,
        "text": content,
        "mtime": target.stat().st_mtime,
        "size_bytes": target.stat().st_size,
    }
    embedder = Embedder(cfg)
    res = index_document(Store(cfg), embedder, doc, embedder.available())
    res["path"] = doc["path"]
    return res


def delete_note(cfg: dict[str, Any], path: str) -> dict[str, Any]:
    """软删除一条知识：标记 missing 并清掉分块，文件保留可追溯。"""
    target = _note_path(path)
    store = Store(cfg)
    existed = store.archive_document(str(target).replace("\\", "/"))
    return {"path": str(target).replace("\\", "/"), "archived": existed}


def backfill_vectors(cfg: dict[str, Any] | None = None, embedder: Embedder | None = None,
                     verbose: bool = True) -> int:
    """给缺向量的分块补建向量（模型后装或中途掉线后补）。"""
    cfg = cfg or config.load(reload=True)
    embedder = embedder or Embedder(cfg)
    if not embedder.available():
        return 0
    store = Store(cfg)
    total = 0
    while True:
        batch = store.chunks_without_vector(64)
        if not batch:
            break
        vecs = embedder.embed([t for _, t in batch])
        if not vecs:
            break
        store.set_vectors(list(zip([cid for cid, _ in batch], vecs)))
        total += len(vecs)
        if verbose and total % 640 == 0:
            print(f"[embed] 已补向量 {total}")
    if verbose and total:
        print(f"[embed] 补建向量完成：{total} 块")
    return total


def run_ingest(cfg: dict[str, Any] | None = None, only: list[str] | None = None,
               verbose: bool = True) -> dict[str, Any]:
    cfg = cfg or config.load(reload=True)
    store = Store(cfg)
    embedder = Embedder(cfg)
    vector_ok = embedder.available()
    if verbose:
        state = f"开启（{embedder.model}）" if vector_ok else "不可用，降级为纯关键词检索"
        print(f"[ingest] 向量化：{state}")

    stats: dict[str, Any] = {
        "seen": 0, "new": 0, "updated": 0, "unchanged": 0,
        "chunks": 0, "vectors": 0, "errors": 0, "removed": 0,
    }
    known: dict[str, set[str]] = defaultdict(set)
    t0 = time.time()

    for doc in collect_all(cfg, only):
        stats["seen"] += 1
        known[doc["source"]].add(doc["path"])
        try:
            res = index_document(store, embedder, doc, vector_ok)
            if res["action"] == "unchanged":
                stats["unchanged"] += 1
                continue
            if res["action"] in ("new", "updated"):
                stats[res["action"]] += 1
            stats["chunks"] += res["chunks"]
            stats["vectors"] += res["vectors"]
            if not res["vector_ok"]:
                vector_ok = False
            if stats["seen"] % 50 == 0 and verbose:
                print(f"[ingest] 已扫描 {stats['seen']} 篇，新增分块 {stats['chunks']}")
        except Exception as exc:  # 单个文件失败不影响整体
            stats["errors"] += 1
            if verbose:
                print(f"[ingest] 跳过 {doc['path']}: {exc}")

    for source, paths in known.items():
        stats["removed"] += len(store.mark_missing(source, paths))

    if embedder.available():
        stats["backfilled"] = backfill_vectors(cfg, embedder, verbose)

    stats["elapsed_s"] = round(time.time() - t0, 2)
    if verbose:
        print(
            f"[ingest] 完成：扫描 {stats['seen']}，新增 {stats['new']}，更新 "
            f"{stats['updated']}，未变 {stats['unchanged']}，分块 {stats['chunks']}，"
            f"向量 {stats['vectors']}，清理 {stats['removed']}，错误 {stats['errors']}，"
            f"耗时 {stats['elapsed_s']}s"
        )
    return stats
