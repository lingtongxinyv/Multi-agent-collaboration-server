"""四类数据源采集：Trae/DSH 记忆库、仓库文档代码、手写笔记、收件箱。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

SKIP_DIRS = {
    ".git", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build", ".idea",
    ".vscode", "site-packages", "vendor_cp311", "vendor_cp312", "vendor_cp313",
}
TEXT_EXTS = {".md", ".markdown", ".txt", ".rst", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".csv"}


def _read(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


def _doc(source: str, path: Path, text: str, title: str) -> dict[str, Any]:
    st = path.stat()
    return {
        "source": source,
        "path": str(path),
        "title": title,
        "text": text,
        "mtime": st.st_mtime,
        "size_bytes": st.st_size,
    }


def _walk(root: Path, extensions: set[str], max_bytes: int) -> Iterator[Path]:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if extensions and path.suffix.lower() not in extensions:
                continue
            try:
                if path.stat().st_size > max_bytes:
                    continue
            except OSError:
                continue
            yield path


# ---------- 1. Trae / DSH 记忆库 ----------

def _flatten(value: Any, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(p for p in (_flatten(v, depth + 1) for v in value) if p)
    if isinstance(value, dict):
        for key in ("content", "text", "message", "body", "summary", "output"):
            if key in value:
                got = _flatten(value[key], depth + 1)
                if got:
                    return got
        return " ".join(
            p for p in (_flatten(v, depth + 1) for k, v in value.items()
                        if k not in ("session_id", "id", "timestamp", "time", "role", "type"))
            if p
        )
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _session_lines(text: str) -> str:
    """把 jsonl 每行渲染成一行可读文本，保持行号与原文一致。"""
    out: list[str] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            out.append("")
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            out.append(raw)
            continue
        if isinstance(obj, dict):
            role = obj.get("role") or obj.get("type") or ""
            body = _flatten(obj)
            out.append(f"[{role}] {body}" if role else body)
        else:
            out.append(_flatten(obj))
    return "\n".join(out)


def collect_memory_library(conf: dict[str, Any], want_docs: bool = True,
                           want_sessions: bool = True) -> Iterator[dict[str, Any]]:
    root = Path(conf.get("root", ""))
    if not root or not root.exists():
        return
    if want_docs:
        for name in ("project_memory.md", "user_profile.md"):
            path = root / name
            if path.is_file():
                yield _doc("memory", path, _read(path), name)
    projects = root / "projects"
    if not projects.exists():
        return
    for proj in sorted(projects.iterdir()):
        if not proj.is_dir():
            continue
        pm = proj / "project_memory.md"
        if want_docs and pm.is_file():
            yield _doc("memory", pm, _read(pm), f"{proj.name} 项目记忆")
        for day in sorted(proj.iterdir()):
            if not day.is_dir():
                continue
            topics = day / "topics.md"
            if want_docs and topics.is_file():
                yield _doc("memory", topics, _read(topics), f"{proj.name} {day.name} 话题")
            if want_sessions:
                for sess in sorted(day.glob("session_memory_*.jsonl")):
                    yield _doc("session", sess, _session_lines(_read(sess)),
                               f"{proj.name} {day.name} 会话 {sess.stem}")


# ---------- 2 / 3. 仓库与笔记 ----------

def collect_paths(source: str, roots: list[str], extensions: list[str], max_kb: int) -> Iterator[dict[str, Any]]:
    exts = {e.lower() for e in extensions}
    max_bytes = max_kb * 1024
    for raw in roots:
        root = Path(raw)
        if not root.exists():
            continue
        for path in _walk(root, exts, max_bytes):
            text = _read(path)
            if text.strip():
                yield _doc(source, path, text, path.name)


# ---------- 4. 收件箱 ----------

def collect_inbox(conf: dict[str, Any]) -> Iterator[dict[str, Any]]:
    root = Path(conf.get("root", "data/inbox"))
    if not root.is_absolute():
        root = Path(__file__).resolve().parent.parent / root
    for path in _walk(root, TEXT_EXTS | {".html", ".htm"}, 2 * 1024 * 1024):
        text = _read(path)
        if not text.strip():
            continue
        if path.suffix.lower() in (".html", ".htm"):
            text = _strip_html(text)
        yield _doc("inbox", path, text, path.stem)


def _strip_html(text: str) -> str:
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


# ---------- 调度 ----------

def collect_all(cfg: dict[str, Any], only: list[str] | None = None) -> Iterator[dict[str, Any]]:
    conf = cfg.get("sources", {})
    picked = set(only or ["memory", "session", "repo", "notes", "inbox"])

    ml_conf = conf.get("memory_library", {})
    if ml_conf.get("enabled") and picked & {"memory", "session"}:
        yield from collect_memory_library(
            ml_conf,
            want_docs="memory" in picked,
            want_sessions="session" in picked and bool(ml_conf.get("include_sessions", True)),
        )

    repo_conf = conf.get("repos", {})
    if repo_conf.get("enabled") and "repo" in picked and repo_conf.get("roots"):
        yield from collect_paths(
            "repo", repo_conf["roots"],
            repo_conf.get("extensions", []), int(repo_conf.get("max_file_kb", 512)),
        )

    notes_conf = conf.get("notes", {})
    if notes_conf.get("enabled") and "notes" in picked and notes_conf.get("roots"):
        yield from collect_paths(
            "notes", notes_conf["roots"], list(TEXT_EXTS), 1024,
        )

    if conf.get("inbox", {}).get("enabled") and "inbox" in picked:
        yield from collect_inbox(conf["inbox"])
