"""配置加载与全局路径。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "kb_config.json"

_DEFAULTS: dict[str, Any] = {
    "db_path": "data/kb.sqlite3",
    "embedding": {
        "provider": "ollama",
        "model": "nomic-embed-text",
        "base_url": "http://127.0.0.1:11434",
        "enabled": True,
        "batch": 8,
    },
    "answer": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "max_context_chunks": 8,
    },
    "sources": {
        "memory_library": {"enabled": True, "root": "", "include_sessions": True},
        "repos": {"enabled": True, "roots": [], "extensions": [], "max_file_kb": 512},
        "notes": {"enabled": True, "roots": []},
        "inbox": {"enabled": True, "root": "data/inbox"},
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8787,
        "token": "",
        # MCP 的 DNS-rebinding 防护：放行本机与反代域名（带端口用 host:* 通配）
        "allowed_hosts": [
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "agent.example.com",
            "agent.example.com:*",
        ],
    },
}

_cache: dict[str, Any] | None = None


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(reload: bool = False) -> dict[str, Any]:
    """读取配置；缺失项用默认值补齐。"""
    global _cache
    if _cache is not None and not reload:
        return _cache
    raw: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    _cache = _merge(_DEFAULTS, raw)
    return _cache


def db_path(cfg: dict[str, Any] | None = None) -> Path:
    cfg = cfg or load()
    path = Path(cfg["db_path"])
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
