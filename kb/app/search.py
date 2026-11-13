"""混合检索：内存 BM25（中文 bigram）+ 向量语义，用 RRF 融合。"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[A-Za-z0-9_]+")

_STOP = set(
    "的 了 是 在 和 与 我 你 他 她 它 们 有 吗 呢 啊 吧 把 被 为 对 就 都 也 很 这 那 个 上 下 中 里 "
    "一个 什么 怎么 如何 哪些 哪个 可以 需要 现在 之前 以前 我们 他们 the a an is are was were of to "
    "and in on for with how what why which that this it be do does".split()
)


def tokenize(text: str) -> list[str]:
    """中文按 bigram 切（单字串保留单字），英文/标识符按词切。"""
    low = text.lower()
    out: list[str] = []
    for m in CJK_RE.finditer(low):
        s = m.group()
        if len(s) == 1:
            out.append(s)
        else:
            out.extend(s[i:i + 2] for i in range(len(s) - 1))
    for w in WORD_RE.findall(low):
        if len(w) >= 2 and w not in _STOP:
            out.append(w)
    return [t for t in out if t not in _STOP]


class BM25:
    def __init__(self, docs: list[tuple[int, str]], k1: float = 1.4, b: float = 0.75):
        self.ids = [d[0] for d in docs]
        self.tf: list[Counter] = []
        self.dl: list[int] = []
        df: Counter = Counter()
        for _, text in docs:
            toks = tokenize(text)
            cnt = Counter(toks)
            self.tf.append(cnt)
            self.dl.append(max(1, len(toks)))
            df.update(cnt.keys())
        self.df = df
        self.n = len(docs)
        self.avgdl = (sum(self.dl) / self.n) if self.n else 0.0
        self.k1 = k1
        self.b = b
        self._idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, limit: int = 40) -> list[tuple[int, float]]:
        q_terms = [t for t in dict.fromkeys(tokenize(query))]
        if not q_terms or not self.n:
            return []
        scored: list[tuple[int, float]] = []
        for i, cnt in enumerate(self.tf):
            score = 0.0
            dl = self.dl[i]
            for term in q_terms:
                f = cnt.get(term, 0)
                if not f:
                    continue
                idf = self._idf.get(term)
                if idf is None:
                    continue
                score += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if score > 0:
                scored.append((self.ids[i], score))
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]


class Searcher:
    """检索入口：组合 BM25 与向量，输出带出处的结果。"""

    def __init__(self, store, embedder):
        self.store = store
        self.embedder = embedder
        self._bm25: BM25 | None = None
        self._version: tuple | None = None

    def _ensure_bm25(self) -> BM25:
        version = self.store.chunk_version()
        if self._bm25 is None or self._version != version:
            self._bm25 = BM25(self.store.load_chunk_texts())
            self._version = version
        return self._bm25

    def search(self, query: str, top_k: int = 6, sources: list[str] | None = None,
               include_sessions: bool = True) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        pool = max(top_k * 6, 40)
        bm25_hits = self._ensure_bm25().search(query, pool)
        vec_hits: list[tuple[int, float]] = []
        if self.embedder.available():
            vec = self.embedder.embed_one(query)
            vec_hits = self.store.vector_search(vec, pool)

        rrf: dict[int, float] = {}
        for rank, (cid, _) in enumerate(bm25_hits):
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank + 1)
        for rank, (cid, _) in enumerate(vec_hits):
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank + 1)

        merged = sorted(rrf.items(), key=lambda x: -x[1])
        if not merged:
            return []
        rows = self.store.fetch_chunks([cid for cid, _ in merged[: top_k * 8]])

        results: list[dict[str, Any]] = []
        for cid, score in merged:
            row = rows.get(cid)
            if not row:
                continue
            if sources and row["source"] not in sources:
                continue
            if not include_sessions and row["source"] == "session":
                continue
            results.append({
                "id": cid,
                "path": row["path"],
                "source": row["source"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "heading": row["heading"],
                "text": row["text"],
                "score": round(score, 5),
            })
            if len(results) >= top_k:
                break
        return results
