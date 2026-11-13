#!/usr/bin/env python3
"""kb 会话蒸馏器（2026-11-06 · 助手）

把 DSH 会话（session.v4.jsonl.zstd）折叠成 3-6 条自包含事实，经 kb_write 写进知识库。
不写 DSH 插件、不动运行件；任何一步失败只记日志，不写脏数据。

用法:
  python3 kb_distill.py --dry-run --limit 2     # 只打印，不写库
  python3 kb_distill.py --limit 4               # 真写
  python3 kb_distill.py --since-days 7 --limit 10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SESSIONS = Path.home() / ".dsh-admin/sessions"
STATE = Path("/var/lib/kb-distill/state.json")
LOG = Path("/var/log/kb-distill.log")
KB_CONFIG = Path("/opt/kb/kb_config.json")
KB_ENV = Path("/opt/kb/kb.env")
MCP_URL = "http://127.0.0.1:8787/mcp/"

MAX_TRANSCRIPT = 12000
MAX_FACTS = 6
FACT_LEN = 200


def log(msg: str) -> None:
    line = f"[{time.strftime('%F %T')}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def session_files() -> list[Path]:
    out = []
    for p in SESSIONS.rglob("session.v4.jsonl.zstd"):
        try:
            out.append(p)
        except OSError:
            continue
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def decompress(path: Path) -> str:
    proc = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"zstd rc={proc.returncode}: {proc.stderr[:120]!r}")
    return proc.stdout.decode("utf-8", "replace")


def _texts(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            t = c.get("text") or ""
            if t.strip():
                parts.append(t.strip())
    return "\n".join(parts)


def extract(raw: str) -> dict:
    sid = cwd = ""
    started = 0
    turns = users = 0
    lines: list[str] = []
    total = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "session":
            sid = ev.get("id") or sid
            cwd = ev.get("cwd") or cwd
            started = int(ev.get("createdAt") or 0)
            continue
        if t == "user/message":
            txt = _texts((ev.get("data") or {}).get("content"))
            if not txt:
                continue
            users += 1
            chunk = "U: " + txt[:600]
            lines.append(chunk)
            total += len(chunk)
        elif t == "assistant/message":
            msg = (ev.get("data") or {}).get("message") or {}
            txt = _texts(msg.get("content"))
            if not txt:
                continue
            chunk = "A: " + txt[:800]
            lines.append(chunk)
            total += len(chunk)
        elif t == "turn/end":
            turns += 1
        if total > MAX_TRANSCRIPT:
            break
    return {
        "id": sid or "unknown",
        "cwd": cwd,
        "started": started,
        "turns": turns or users,
        "users": users,
        "transcript": "\n".join(lines)[:MAX_TRANSCRIPT],
    }


def _api_key() -> str:
    for line in KB_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("kb.env 里没有 DEEPSEEK_API_KEY")


def _llm_key() -> str:
    return json.loads(KB_CONFIG.read_text(encoding="utf-8"))["server"]["token"]


def distill(transcript: str, meta: dict) -> list[str]:
    key = _api_key()
    system = (
        "你是会话蒸馏器。从一段 AI 助手与用户的开发会话里，抽取 3-6 条**自包含、可复用**的事实："
        "作出的决定、确认的结论、踩过的坑及修法、未完成的待办。"
        "要求：每条一行，以「- 」开头；带日期（用会话日期）；单条不超过 120 字；"
        "不写过程性闲聊、不写命令输出原文、**绝不写任何凭据/密钥/token/密码**；"
        "如果确实没有值得留存的内容，只输出 NONE。"
    )
    user = (
        f"会话日期：{time.strftime('%Y-%m-%d', time.localtime(meta['started'] / 1000))}\n"
        f"工作区：{meta['cwd']}\n用户轮次：{meta['users']}\n\n{transcript}"
    )
    body = {
        "model": "deepseek-flash",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": 900,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    facts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        line = re.sub(r"^([-*•]|\d+[.)])\s*", "", line).strip()
        if len(line) < 8 or line.startswith("[") and "]" not in line:
            continue
        facts.append(line[:FACT_LEN])
        if len(facts) >= MAX_FACTS:
            break
    # 去重
    seen, uniq = set(), []
    for f in facts:
        k = f[:40]
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


class KB:
    def __init__(self) -> None:
        self.tok = _llm_key()
        self.sid = None
        self._rpc = 0
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "kb-distill", "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)

    def _post(self, payload: dict, expect: bool = True):
        req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", "Bearer " + self.tok)
        if self.sid:
            req.add_header("Mcp-Session-Id", self.sid)
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.sid = resp.headers.get("mcp-session-id") or self.sid
            body = resp.read().decode("utf-8", "replace")
        if not expect:
            return None
        for line in body.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return json.loads(body) if body.strip() else None

    def write(self, path: str, content: str, title: str) -> str:
        self._rpc += 1
        res = self._post({"jsonrpc": "2.0", "id": 100 + self._rpc, "method": "tools/call",
                          "params": {"name": "kb_write",
                                     "arguments": {"path": path, "content": content, "title": title}}})
        try:
            return (res["result"]["content"][0]["text"] or "")[:200]
        except Exception:  # noqa: BLE001
            return json.dumps(res, ensure_ascii=False)[:200]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--since-days", type=float, default=14)
    ap.add_argument("--min-idle-min", type=float, default=10)
    args = ap.parse_args()

    state = load_state()
    cutoff = time.time() - args.since_days * 86400
    idle_cut = time.time() - args.min_idle_min * 60
    kb = None if args.dry_run else KB()
    done = 0

    for p in session_files():
        if done >= args.limit:
            break
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff or mtime > idle_cut:
            continue
        try:
            raw = decompress(p)
        except Exception as exc:  # noqa: BLE001
            log(f"SKIP 解压失败 {p.name}: {exc}")
            continue
        meta = extract(raw)
        import hashlib
        h = hashlib.sha256(meta["transcript"].encode()).hexdigest()[:16]
        key = meta["id"]
        old = state.get(key) or {}
        if old.get("hash") == h:
            continue
        if meta["users"] < 2 or len(meta["transcript"]) < 600:
            continue

        log(f"DISTILL {key} users={meta['users']} cwd={meta['cwd']} len={len(meta['transcript'])}")
        try:
            facts = distill(meta["transcript"], meta)
        except Exception as exc:  # noqa: BLE001
            log(f"  LLM 失败，跳过: {exc}")
            continue
        if not facts:
            log("  无值得留存的事实（NONE），跳过")
            state[key] = {"hash": h, "at": time.time(), "written": ""}
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(meta["started"] / 1000))
        rel = f"sessions/{day}/{key}.md"
        content = (
            f"# 会话摘要：{key}\n\n"
            f"> 日期：{day} · 工作区：`{meta['cwd']}` · 用户轮次：{meta['users']}\n"
            f"> 来源：DSH 会话自动蒸馏（kb-distill），原始会话在 `~/.dsh-admin/sessions/`\n\n"
            + "\n".join(f"- {f}" for f in facts) + "\n"
        )
        if args.dry_run:
            log("  [dry-run] " + " | ".join(f[:60] for f in facts[:2]))
            print(content)
        else:
            try:
                out = kb.write(rel, content, f"会话摘要 {day} {key[:12]}")
                log(f"  WRITE {rel} -> {out}")
                state[key] = {"hash": h, "at": time.time(), "written": rel}
            except Exception as exc:  # noqa: BLE001
                log(f"  写库失败: {exc}")
                continue
        done += 1

    if not args.dry_run:
        save_state(state)
    log(f"结束：处理 {done} 个会话")
    return 0


if __name__ == "__main__":
    sys.exit(main())
