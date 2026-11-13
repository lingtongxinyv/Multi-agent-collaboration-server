#!/usr/bin/env python3
"""站娘对话接口的命令行客户端（本机用）。

用法：
    ops/chat.py health
    ops/chat.py roster
    ops/chat.py ls
    ops/chat.py new "和 伙伴-A 的线程" [--with 伙伴-A]
    ops/chat.py send <thread_id> "在吗？服务器怎么样" [--wait]
    ops/chat.py read <thread_id> [--after N] [--wait 15]
    ops/chat.py watch <thread_id>          # 从末尾开始持续打印
    ops/chat.py peer add <名字> [--admin]   # 仅管理员，token 只打印一次

凭据来源（按顺序）：
    $CHAT_TOKEN  →  ~/.dsh-chat-token  →  <repo>/data/mcp_token（本机管理员）
地址：$CHAT_BASE，默认 http://127.0.0.1:8010
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:  # 系统 python3 没装 httpx —— 自动切到仓库 venv 再跑
    _venv = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
    if _venv.exists():
        os.execv(str(_venv), [str(_venv), __file__, *sys.argv[1:]])
    raise

REPO = Path(__file__).resolve().parents[1]
BASE = os.environ.get("CHAT_BASE", "http://127.0.0.1:8010").rstrip("/")


def token() -> str:
    tok = os.environ.get("CHAT_TOKEN", "").strip()
    if tok:
        return tok
    for p in (Path.home() / ".dsh-chat-token", REPO / "data" / "mcp_token"):
        try:
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except OSError:
            continue
    sys.exit("找不到对话凭据：设置 CHAT_TOKEN，或写入 ~/.dsh-chat-token")


def api(method: str, path: str, body: dict | None = None, timeout: float = 120):
    headers = {"Authorization": f"Bearer {token()}"}
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, BASE + path, headers=headers, json=body)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:500]}
    if r.status_code >= 400:
        sys.exit(f"HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)}")
    return data


def show(post: dict) -> None:
    mark = "助手" if post["role"] == "assistant" else post["from"]
    print(f"\n[{post['at']}] {mark}:\n{post['text']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="站娘对话接口 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("roster")
    sub.add_parser("ls")
    p_new = sub.add_parser("new")
    p_new.add_argument("title")
    p_new.add_argument("--with", dest="with_", action="append", default=[])
    p_send = sub.add_parser("send")
    p_send.add_argument("tid")
    p_send.add_argument("text", nargs="+")
    p_send.add_argument("--no-wait", action="store_true")
    p_read = sub.add_parser("read")
    p_read.add_argument("tid")
    p_read.add_argument("--after", type=int, default=0)
    p_read.add_argument("--wait", type=float, default=0)
    p_watch = sub.add_parser("watch")
    p_watch.add_argument("tid")
    p_peer = sub.add_parser("peer")
    p_peer.add_argument("action", choices=["add", "ls"])
    p_peer.add_argument("name", nargs="?")
    p_peer.add_argument("--admin", action="store_true")
    args = ap.parse_args()

    if args.cmd == "health":
        print(json.dumps(api("GET", "/api/chat/health"), ensure_ascii=False, indent=2))
    elif args.cmd == "roster":
        print(json.dumps(api("GET", "/api/chat/roster"), ensure_ascii=False, indent=2))
    elif args.cmd == "ls":
        for t in api("GET", "/api/chat/threads")["threads"]:
            last = t.get("last") or {}
            print(f"{t['id']}  {t['title'][:32]:32s}  {last.get('at', '')}  "
                  f"{last.get('from', '')}: {(last.get('text') or '')[:40]}")
    elif args.cmd == "new":
        parts = ["管理员"] + list(args.with_) + ["助手"]
        out = api("POST", "/api/chat/threads",
                  {"title": args.title, "participants": list(dict.fromkeys(parts))})
        print(out["thread"]["id"])
    elif args.cmd == "send":
        out = api("POST", f"/api/chat/threads/{args.tid}/messages",
                  {"text": " ".join(args.text), "wait": not args.no_wait},
                  timeout=150)
        show(out["post"])
        if out.get("reply"):
            show(out["reply"])
        elif out.get("reply_pending"):
            print("\n（助手还在想，用 read / watch 取回复）")
        elif out.get("note"):
            print(f"\n（{out['note']}）")
    elif args.cmd == "read":
        out = api("GET", f"/api/chat/threads/{args.tid}?after={args.after}&wait={args.wait}",
                  timeout=args.wait + 20)
        for p in out["posts"]:
            show(p)
        print(f"\ncursor={out['cursor']}")
    elif args.cmd == "watch":
        cursor = api("GET", f"/api/chat/threads/{args.tid}?after=0")["cursor"]
        print(f"（watch {args.tid}，Ctrl-C 退出）")
        while True:
            out = api("GET", f"/api/chat/threads/{args.tid}?after={cursor}&wait=20",
                      timeout=40)
            for p in out["posts"]:
                show(p)
            cursor = out["cursor"]
    elif args.cmd == "peer":
        if args.action == "ls":
            print(json.dumps(api("GET", "/api/chat/peers"), ensure_ascii=False, indent=2))
        else:
            if not args.name:
                sys.exit("peer add 需要名字")
            out = api("POST", "/api/chat/peers",
                      {"name": args.name, "kind": "admin" if args.admin else "peer"})
            print(json.dumps(out, ensure_ascii=False, indent=2))
            print("\n⚠ token 只显示这一次，请立刻保存。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出")
