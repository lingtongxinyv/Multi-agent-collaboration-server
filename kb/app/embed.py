"""向量化：本地 Ollama 优先，失败自动降级（不影响纯关键词检索）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

_LEGACY_HINT = None


class Embedder:
    def __init__(self, cfg: dict[str, Any]):
        conf = cfg.get("embedding", {})
        self.enabled = bool(conf.get("enabled", True))
        self.model = conf.get("model", "nomic-embed-text")
        self.base_url = conf.get("base_url", "http://127.0.0.1:11434").rstrip("/")
        self.batch = max(1, int(conf.get("batch", 8)))
        self._broken = False
        self._warned = False

    def _post(self, path: str, payload: dict, timeout: int = 180) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def available(self) -> bool:
        if not self.enabled or self._broken:
            return False
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=10) as resp:
                tags = json.loads(resp.read().decode("utf-8"))
            names = {m.get("name", "") for m in tags.get("models", [])}
            return any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)
        except Exception:
            return False

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """返回与 texts 对齐的向量；不可用或出错时返回 None。"""
        global _LEGACY_HINT
        if not self.enabled or self._broken or not texts:
            return [] if not texts else None
        out: list[list[float]] = []
        try:
            for start in range(0, len(texts), self.batch):
                part = texts[start:start + self.batch]
                if _LEGACY_HINT:
                    for t in part:
                        data = self._post("/api/embeddings", {"model": self.model, "prompt": t})
                        out.append(data["embedding"])
                else:
                    try:
                        data = self._post("/api/embed", {"model": self.model, "input": part})
                        out.extend(data["embeddings"])
                    except urllib.error.HTTPError as exc:
                        if exc.code == 404:
                            _LEGACY_HINT = True
                            for t in part:
                                data = self._post("/api/embeddings", {"model": self.model, "prompt": t})
                                out.append(data["embedding"])
                        else:
                            raise
        except Exception as exc:
            if not self._warned:
                print(f"[embed] 向量化不可用，已降级为纯关键词检索：{exc}")
                self._warned = True
            self._broken = True
            return None
        return out

    def embed_one(self, text: str) -> list[float] | None:
        vecs = self.embed([text])
        if not vecs:
            return None
        return vecs[0]
