#!/usr/bin/env python3
"""bellhub-chat —— 和助手说话的客户端（由服务器生成，内含地址与一次性凭据）。

直接运行（Windows 双击 / Linux `python3 bellhub-chat-<名字>.py`）：
    进入聊天模式，输入内容回车发送，/quit 退出。

带参数运行：
    python bellhub-chat-<名字>.py health      # 连接自检
    python bellhub-chat-<名字>.py ask "话"    # 发一句，等回复
    python bellhub-chat-<名字>.py stream "话" # 逐字打印回复
    python bellhub-chat-<名字>.py watch       # 持续收新消息
    python bellhub-chat-<名字>.py threads     # 线程列表

只依赖 Python 标准库（3.9+）。线程与游标存在 ~/.bellhub-chat/<名字>.json（0600）。
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "__BASE__"
TOKEN = "__TOKEN__"
NAME = "__NAME__"

STATE_DIR = Path.home() / ".bellhub-chat"
STATE_PATH = STATE_DIR / (NAME.replace("/", "_") + ".json")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"thread": "", "cursor": 0}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        STATE_PATH.chmod(0o600)
    except OSError:  # Windows 上 chmod 语义有限，忽略
        pass


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def api(path: str, body: dict | None = None, method: str | None = None,
        timeout: float = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE.rstrip("/") + path, data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        die(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as e:
        die(f"连不上 {BASE}：{e.reason}")
    return {}


def ensure_thread(st: dict) -> str:
    if st.get("thread"):
        return st["thread"]
    d = api("/api/chat/threads", {"title": f"{NAME} ↔ 助手", "participants": [NAME]})
    st["thread"] = d["thread"]["id"]
    save_state(st)
    print(f"（已建线程 {st['thread']}）")
    return st["thread"]


def health() -> None:
    d = api("/api/chat/health", timeout=15)
    print(f"✅ 连上了：{BASE}")
    print(f"   身份={d['as']}  模式={d['mode']}  模型={d['model']}")
    print(f"   今日用量 {d['tokens_today']}/{d['token_cap']} tok  线程 {d['threads']} 条")


def ask(text: str, quiet: bool = False) -> str:
    st = load_state()
    tid = ensure_thread(st)
    d = api(f"/api/chat/threads/{tid}/messages", {"text": text, "wait": True}, timeout=180)
    reply = d.get("reply")
    if reply:
        if not quiet:
            print(reply["text"])
        st["cursor"] = reply["id"]
        save_state(st)
        return reply["text"]
    if d.get("note"):
        print(f"（{d['note']}）")
    else:
        print("（助手还没回，用 watch 等一会儿）")
    return ""


def stream(text: str) -> None:
    st = load_state()
    tid = ensure_thread(st)
    req = urllib.request.Request(
        BASE.rstrip("/") + f"/api/chat/threads/{tid}/messages/stream",
        data=json.dumps({"text": text}).encode(), method="POST",
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    buf = ""
    with urllib.request.urlopen(req, timeout=180) as r:
        for raw in r:
            buf += raw.decode("utf-8", "replace")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                ev = data = None
                for line in frame.splitlines():
                    if line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: "):
                        data = line[6:]
                if not ev or not data:
                    continue
                obj = json.loads(data)
                if ev == "delta":
                    sys.stdout.write(obj.get("text", ""))
                    sys.stdout.flush()
                elif ev == "done":
                    print()
                    st["cursor"] = obj["cursor"]
                    save_state(st)
                elif ev == "error":
                    print(f"\n[出错] {obj.get('message')}")


def watch() -> None:
    st = load_state()
    tid = ensure_thread(st)
    cursor = st.get("cursor") or 0
    print(f"（watch 线程 {tid}，Ctrl-C 退出）")
    while True:
        d = api(f"/api/chat/threads/{tid}?after={cursor}&wait=20", timeout=40)
        for p in d["posts"]:
            cursor = p["id"]
            who = "助手" if p["role"] == "assistant" else p["from"]
            print(f"\n[{p['at']}] {who}:\n{p['text']}")
        st["cursor"] = cursor
        save_state(st)


def threads() -> None:
    d = api("/api/chat/threads")
    for t in d["threads"]:
        last = t.get("last") or {}
        print(f"{t['id']}  {t['title'][:32]}  {last.get('from', '')}: "
              f"{(last.get('text') or '')[:40]}")


def new(title: str) -> None:
    d = api("/api/chat/threads", {"title": title, "participants": [NAME]})
    st = load_state()
    st["thread"] = d["thread"]["id"]
    st["cursor"] = 0
    save_state(st)
    print(d["thread"]["id"])


def chat_loop() -> None:
    health()
    st = load_state()
    ensure_thread(st)
    print("\n聊天模式：输入内容回车发送，/quit 退出，/new 换线程，/stream 切换逐字模式\n")
    stream_mode = False
    while True:
        try:
            text = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text in ("/quit", "/exit", "exit", "quit"):
            return
        if text == "/new":
            new(f"{NAME} ↔ 助手 {time_tag()}")
            continue
        if text == "/stream":
            stream_mode = not stream_mode
            print(f"（逐字模式 {'开' if stream_mode else '关'}）")
            continue
        try:
            stream(text) if stream_mode else ask(text)
        except Exception as exc:  # noqa: BLE001 —— 单条失败不退出聊天
            print(f"（这条没发出去：{exc}）")


def time_tag() -> str:
    import time
    return time.strftime("%m-%d %H:%M")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        chat_loop()
        return
    cmd, rest = args[0], args[1:]
    if cmd in ("health", "ping"):
        health()
    elif cmd == "ask":
        if not rest:
            die('用法：ask "..."')
        ask(" ".join(rest))
    elif cmd == "stream":
        if not rest:
            die('用法：stream "..."')
        stream(" ".join(rest))
    elif cmd == "watch":
        watch()
    elif cmd == "threads":
        threads()
    elif cmd == "new":
        new(" ".join(rest) or "新线程")
    elif cmd in ("-h", "--help", "help"):
        print(__doc__)
    else:
        die(f"未知命令：{cmd}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出")
