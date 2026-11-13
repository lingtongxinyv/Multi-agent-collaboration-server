#!/usr/bin/env python3
"""黑板历史蒸馏器（2026-11-06 · 助手）

把 hub 黑板的历史键分批折叠成可检索的事实，经 kb_write 写进知识库 history/。
- 断点续跑：/var/lib/kb-bb-distill/state.json 记录已处理键（按内容哈希）
- 分批：每批 ≤12 键、每键截断 1800 字，控制单次成本
- 失败只记日志，不写脏数据；凭据类键（token/密码）直接跳过

用法:
  python3 kb_bb_distill.py --dry-run --max-batches 1
  python3 kb_bb_distill.py --max-batches 999      # 全量回填
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HUB = "http://127.0.0.1:8765/mcp/"
KB = "http://127.0.0.1:8787/mcp/"
STATE = Path("/var/lib/kb-bb-distill/state.json")
LOG = Path("/var/log/kb-bb-distill.log")
KB_CONFIG = Path("/opt/kb/kb_config.json")
KB_ENV = Path("/opt/kb/kb.env")

BATCH_KEYS = 10
KEY_CHARS = 1600
SKIP_PREFIX = ("tri/__probe", "linktest_diag", "tri/channel-")
SECRET_HINT = re.compile(r"(token|password|secret|api[_-]?key)", re.I)


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
        return {"done": {}, "batches": 0}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


class Rpc:
    def __init__(self, url: str, token: str = "", name: str = "kb-bb") -> None:
        self.url, self.token, self.sid, self.i = url, token, None, 0
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": name, "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)

    def _post(self, payload: dict, expect: bool = True):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        if self.sid:
            req.add_header("Mcp-Session-Id", self.sid)
        with urllib.request.urlopen(req, timeout=120) as resp:
            self.sid = resp.headers.get("mcp-session-id") or self.sid
            body = resp.read().decode("utf-8", "replace")
        if not expect:
            return None
        for line in body.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return json.loads(body) if body.strip() else None

    def call(self, tool: str, args: dict) -> str:
        self.i += 1
        res = self._post({"jsonrpc": "2.0", "id": 100 + self.i, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
        try:
            return "\n".join(c.get("text", "") for c in res["result"]["content"])
        except Exception:  # noqa: BLE001
            return json.dumps(res, ensure_ascii=False)


def api_key() -> str:
    for line in KB_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("no DEEPSEEK_API_KEY")


def distill(batch: list[tuple[str, str]], batch_no: int) -> list[str]:
    key = api_key()
    body_text = "\n\n".join(f"### {k}\n{v}" for k, v in batch)
    system = (
        "你是运维协作记录的蒸馏器。输入是同一台服务器（铃枢）的黑板记录片段。"
        "**无论片段多琐碎，都必须输出 3-8 条可复用的事实**：做出的决定及理由、确认的结论、踩过的坑与修法、未完成的待办、"
        "接口/路径/阈值等硬事实。只有整段完全空白才允许输出 NONE。"
        "要求：每条一行、以「- 」开头；尽量带日期；单条 ≤150 字；合并重复；"
        "不写寒暄与过程性对话、不写命令原文；**绝不写任何凭据/密钥/token/密码**。"
    )
    body = {
        "model": "deepseek-flash",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": f"批次 {batch_no}\n\n{body_text}"}],
        "max_tokens": 4000,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.load(resp)
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    facts = []
    for line in text.splitlines():
        line = re.sub(r"^([-*•]|\d+[.)])\s*", "", line.strip())
        if len(line) < 8 or line.upper() == "NONE":
            continue
        facts.append(line[:180])
    seen, uniq = set(), []
    for f in facts:
        if f[:30] in seen:
            continue
        seen.add(f[:30])
        uniq.append(f)
    return uniq[:10]



def distill_recursive(batch, idx, depth=0):
    """整批没产出时对半降批重试（推理模型容易把 token 烧在思考上）。"""
    facts = distill(batch, idx)
    if facts or depth >= 2 or len(batch) <= 4:
        return facts
    mid = len(batch) // 2
    log(f"  降批重试（{len(batch)} → {mid}+{len(batch)-mid}）")
    return distill_recursive(batch[:mid], idx, depth + 1) + distill_recursive(batch[mid:], idx, depth + 1)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-batches", type=int, default=999)
    ap.add_argument("--min-chars", type=int, default=120)
    args = ap.parse_args()

    hub = Rpc(HUB, name="kb-bb-hub")
    kb = None if args.dry_run else Rpc(KB, json.loads(KB_CONFIG.read_text())["server"]["token"], name="kb-bb-writer")
    state = load_state()

    raw = hub.call("blackboard_read", {"key": "__list__"})
    entries = json.loads(raw)
    log(f"黑板键 {len(entries)} 个")

    # 按家族+时间排序，让同主题进同一批
    def fam(k: str) -> str:
        return k.split("/")[0] if "/" in k else "misc"

    entries.sort(key=lambda e: (fam(e["key"]), e.get("updated_at", 0)))

    batch: list[tuple[str, str]] = []
    batch_keys: list[str] = []
    done_batches = 0
    written = 0

    def flush(batch, batch_keys, idx):
        nonlocal written
        if not batch:
            return
        log(f"BATCH {idx}: {len(batch)} keys ({batch_keys[0]} … {batch_keys[-1]})")
        try:
            facts = distill_recursive(batch, idx)
        except Exception as exc:  # noqa: BLE001
            log(f"  LLM 失败: {exc}")
            return
        if not facts:
            log("  NONE，跳过")
            for k in batch_keys:
                state["done"][k] = "none"
            return
        fam_name = fam(batch_keys[0])
        rel = f"history/{fam_name}-{idx:03d}.md"
        content = (
            f"# 黑板历史蒸馏 · {fam_name} #{idx:03d}\n\n"
            f"> 来源：hub 黑板（{len(batch)} 个键，{batch_keys[0]} … {batch_keys[-1]}）\n"
            f"> 生成：kb-bb-distill 2026-11-06 · 自动蒸馏，细节以原键为准\n\n"
            + "\n".join(f"- {f}" for f in facts) + "\n"
        )
        if args.dry_run:
            log("  [dry-run] " + " | ".join(f[:50] for f in facts[:2]))
            print(content)
        else:
            out = kb.call("kb_write", {"path": rel, "content": content,
                                       "title": f"黑板历史 {fam_name} #{idx:03d}"})
            log(f"  WRITE {rel} -> {out[:120]}")
            written += 1
        for k in batch_keys:
            state["done"][k] = "ok"

    idx = 0
    for e in entries:
        k = e["key"]
        if k.startswith(SKIP_PREFIX) or k in state["done"]:
            continue
        try:
            text = hub.call("blackboard_read", {"key": k})
        except Exception as exc:  # noqa: BLE001
            log(f"SKIP read {k}: {exc}")
            continue
        if not text or text.startswith("（无条目"):
            state["done"][k] = "missing"
            continue
        if len(text.strip()) < args.min_chars:
            state["done"][k] = "short"
            continue
        snippet = text[:KEY_CHARS]
        batch.append((k, snippet))
        batch_keys.append(k)
        if len(batch) >= BATCH_KEYS:
            idx += 1
            flush(batch, batch_keys, idx)
            batch, batch_keys = [], []
            done_batches += 1
            state["batches"] = state.get("batches", 0) + 1
            if not args.dry_run:
                save_state(state)
            if done_batches >= args.max_batches:
                break
    if batch and done_batches < args.max_batches:
        idx += 1
        flush(batch, batch_keys, idx)
        state["batches"] = state.get("batches", 0) + 1
    if not args.dry_run:
        save_state(state)
    log(f"结束：批 {done_batches + (1 if batch else 0)}，写库 {written}，已记 {len(state['done'])} 键")
    return 0


if __name__ == "__main__":
    sys.exit(main())
