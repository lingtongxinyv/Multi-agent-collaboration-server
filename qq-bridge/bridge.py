#!/usr/bin/env python3
"""DSH QQ 桥 —— 把 QQ（NapCat / OneBot v11）接到站娘对话接口。

数据流：
    QQ 用户 → NapCat → POST /onebot → 本桥 → POST /api/chat/…/messages
                                    ← 回复 ← 本桥 ← GET 线程/同步 reply
    QQ 用户 ← NapCat ← send_private_msg / send_group_msg ← 本桥

设计约束（都是踩过坑之后写的）：
  · 只服务白名单里的 QQ 号；白名单为空 = 全部拒绝（fail closed），绝不变成公开机器人。
  · 收到事件立刻回 200，真正的处理放线程里——OneBot 的 HTTP 上报有超时，同步跑 3~10 秒
    会被它判超时并重投，导致同一条消息回两遍。
  · 按 message_id 去重（重投防护），TTL 7 天。
  · `/枢 内容` 走伙伴：他的回复是异步的（值守会话处理完才写回线程），所以要轮询线程；
    助手走同步 reply，几秒就回。
  · 所有凭据从环境变量读，不进代码、不进日志。

环境变量（见 env.example）：
    QQ_BRIDGE_TOKEN  必填，OneBot 上报时带的 Bearer token
    QQ_ALLOW         必填，逗号分隔的 QQ 号白名单
    ONEBOT_API       默认 http://127.0.0.1:3000
    ONEBOT_TOKEN     NapCat 的 access token（调发送 API 用）
    CHAT_BASE        默认 http://127.0.0.1:8010
    CHAT_TOKEN       必填，对话伙伴 token（面板 /api/chat/peers 生成）
    DRY_RUN          1 = 不真发 QQ，只打日志（联调用）
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import asyncio

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

DATA = Path(os.environ.get("QQ_BRIDGE_DATA", str(Path.home() / "dsh-suite/data/qq-bridge")))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "bridge.db"

BRIDGE_TOKEN = os.environ.get("QQ_BRIDGE_TOKEN", "").strip()
ALLOW = {x.strip() for x in os.environ.get("QQ_ALLOW", "").split(",") if x.strip()}
ONEBOT_API = os.environ.get("ONEBOT_API", "http://127.0.0.1:3000").rstrip("/")
ONEBOT_TOKEN = os.environ.get("ONEBOT_TOKEN", "").strip()
CHAT_BASE = os.environ.get("CHAT_BASE", "http://127.0.0.1:8010").rstrip("/")
CHAT_TOKEN = os.environ.get("CHAT_TOKEN", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
# QQ 号 → {name, token}：不同的人用不同的对话身份，助手才会叫对名字。
# 没配的号退回 CHAT_TOKEN（名字就是那个 peer 的名字）。
try:
    PEERS = json.loads(os.environ.get("QQ_PEERS", "{}") or "{}")
except ValueError:
    PEERS = {}
MAX_IN = 800
MAX_OUT = 3500
# 管道号（QQ_PIPE_ONLY）：只把事件转给订阅方，绝不代回 —— 这些账号的会话不属于桥。
PIPE_ONLY = {x.strip() for x in os.environ.get("QQ_PIPE_ONLY", "").split(",") if x.strip()}

app = FastAPI(title="DSH QQ Bridge", docs_url=None, redoc_url=None)
_lock = threading.Lock()

# ── WS 订阅（给 伙伴-A 这类外部伙伴实时收消息，不用轮询）──────────
SUBSCRIBERS: set = set()
LOOP = None


@app.on_event("startup")
async def _on_startup() -> None:
    global LOOP
    LOOP = asyncio.get_running_loop()


def _broadcast(payload: dict) -> None:
    """线程安全地往所有 WS 订阅者推一条事件。"""
    if not SUBSCRIBERS or LOOP is None:
        return

    async def _send() -> None:
        dead = []
        for ws in list(SUBSCRIBERS):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            SUBSCRIBERS.discard(ws)

    try:
        asyncio.run_coroutine_threadsafe(_send(), LOOP)
    except RuntimeError:
        pass


def log(msg: str) -> None:
    print(f"[qq-bridge {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS links (
            key TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            cursor INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS seen (
            message_id TEXT PRIMARY KEY,
            ts REAL NOT NULL
        );
        """)


def seen_before(message_id: str) -> bool:
    with db() as c:
        row = c.execute("SELECT 1 FROM seen WHERE message_id=?", (message_id,)).fetchone()
        if row:
            return True
        c.execute("INSERT INTO seen(message_id, ts) VALUES (?,?)", (message_id, time.time()))
        c.execute("DELETE FROM seen WHERE ts < ?", (time.time() - 7 * 86400,))
        return False


# ── 小工具 ──────────────────────────────────────────────────────────
def http_json(url: str, body: dict | None = None, headers: dict | None = None,
              timeout: float = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode() or "{}"
        return json.loads(raw)


def chat(path: str, body: dict | None = None, timeout: float = 120,
         token: str | None = None) -> dict:
    return http_json(CHAT_BASE + path, body,
                     {"Authorization": f"Bearer {token or CHAT_TOKEN}"}, timeout)


def peer_of(uid: str) -> dict:
    return PEERS.get(str(uid)) or {}


CQ = re.compile(r"\[CQ:[^\]]+\]")


def extract_text(event: dict) -> str:
    """优先用 message 段拼文本（干净），退回 raw_message 去 CQ 码。"""
    segs = event.get("message")
    if isinstance(segs, list):
        parts = [s.get("data", {}).get("text", "") for s in segs if s.get("type") == "text"]
        text = "".join(parts)
        if text.strip():
            return text.strip()
    return CQ.sub("", str(event.get("raw_message") or "")).strip()


def mentioned_me(event: dict) -> bool:
    me = str(event.get("self_id") or "")
    segs = event.get("message")
    if isinstance(segs, list):
        return any(s.get("type") == "at" and str(s.get("data", {}).get("qq")) == me
                   for s in segs)
    return f"[CQ:at,qq={me}]" in str(event.get("raw_message") or "")


def onebot(action: str, payload: dict) -> dict:
    if DRY_RUN:
        log(f"[DRY] {action} {json.dumps(payload, ensure_ascii=False)[:300]}")
        return {"status": "dry-run"}
    headers = {"Authorization": f"Bearer {ONEBOT_TOKEN}"} if ONEBOT_TOKEN else {}
    try:
        return http_json(f"{ONEBOT_API}/{action}", payload, headers, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log(f"OneBot {action} 失败: {exc}")
        return {"status": "failed", "error": str(exc)}


def send_text(event: dict, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    if len(text) > MAX_OUT:
        text = text[:MAX_OUT] + "\n…（太长了，剩下的在面板 /chat 里）"
    if event.get("message_type") == "group":
        onebot("send_group_msg", {"group_id": event.get("group_id"),
                                  "message": [{"type": "text", "data": {"text": text}}]})
    else:
        onebot("send_private_msg", {"user_id": event.get("user_id"),
                                    "message": [{"type": "text", "data": {"text": text}}]})
    _broadcast({"type": "qq_out",
                "qq": event.get("group_id") if event.get("message_type") == "group"
                else event.get("user_id"),
                "text": text, "ts": time.time()})


# ── 线程映射 ────────────────────────────────────────────────────────
def link_key(event: dict) -> str:
    if event.get("message_type") == "group":
        return f"group:{event.get('group_id')}"
    return f"user:{event.get('user_id')}"


def get_thread(key: str) -> str:
    with db() as c:
        row = c.execute("SELECT thread_id FROM links WHERE key=?", (key,)).fetchone()
        return row["thread_id"] if row else ""


def set_thread(key: str, tid: str) -> None:
    with db() as c:
        c.execute("INSERT INTO links(key, thread_id, cursor, updated_at) VALUES (?,?,0,?)"
                  " ON CONFLICT(key) DO UPDATE SET thread_id=excluded.thread_id, cursor=0,"
                  " updated_at=excluded.updated_at", (key, tid, time.time()))


def get_cursor(key: str) -> int:
    with db() as c:
        row = c.execute("SELECT cursor FROM links WHERE key=?", (key,)).fetchone()
        return int(row["cursor"]) if row else 0


def set_cursor(key: str, cur: int) -> None:
    with db() as c:
        c.execute("UPDATE links SET cursor=?, updated_at=? WHERE key=?", (cur, time.time(), key))


def ensure_thread(key: str, title: str, token: str | None = None) -> str:
    tid = get_thread(key)
    if tid:
        return tid
    d = chat("/api/chat/threads", {"title": title, "participants": []}, token=token)
    tid = d["thread"]["id"]
    set_thread(key, tid)
    log(f"新建线程 {tid}（{key}）")
    return tid


# ── 处理一条消息 ────────────────────────────────────────────────────
HELP = ("我是助手～这座服务器的站娘。\n"
        "直接说话就行；要派活给值守会话，用 /task 目标（仅管理员可用）。\n"
        "/枢 内容  → 转给伙伴（异步，可能要等一会儿）\n"
        "/看       → 看线程里最新一条回复\n"
        "/new      → 换一个新线程\n"
        "/help     → 这张卡片")


def poll_for_reply(key: str, tid: str, after: int, token: str | None = None,
                   timeout: float = 120) -> str:
    """等线程里出现新的 assistant 消息（伙伴的自动回写用它）。"""
    deadline = time.time() + timeout
    cur = after
    while time.time() < deadline:
        try:
            d = chat(f"/api/chat/threads/{tid}?after={cur}&wait=15", timeout=30,
                     token=token)
        except Exception:  # noqa: BLE001
            time.sleep(2)
            continue
        for p in d.get("posts", []):
            cur = p["id"]
            if p.get("role") == "assistant":
                set_cursor(key, cur)
                return p.get("text", "")
        set_cursor(key, cur)
    return ""


def handle(event: dict) -> None:
    key = link_key(event)
    user_id = str(event.get("user_id") or "")
    text = extract_text(event)
    if not text:
        return
    if len(text) > MAX_IN:
        text = text[:MAX_IN]
    tok = peer_of(user_id).get("token") or CHAT_TOKEN
    log(f"收到 {key}（{len(text)} 字）：{text[:60]}")
    _broadcast({"type": "qq_in", "qq": user_id,
                "name": peer_of(user_id).get("name") or user_id,
                "text": text, "ts": time.time()})

    # 管道号：事件照常广播，但不建线程、不代回（只做服务器通道）
    if user_id in PIPE_ONLY:
        log(f"管道号 {user_id}：已广播事件，不代回（会话归账号管理员）")
        return

    # 群聊必须 @ 我，私聊不要求
    if event.get("message_type") == "group" and not mentioned_me(event):
        return

    try:
        if text in ("/help", "help", "帮助"):
            send_text(event, HELP)
            return
        if text in ("/new", "新线程"):
            tid = ensure_thread(key + ":" + str(int(time.time())), "QQ 新线程", token=tok)
            set_thread(key, tid)
            send_text(event, f"换好新线程了（{tid}）。")
            return
        if text in ("/看", "/look"):
            tid = get_thread(key)
            if not tid:
                send_text(event, "还没开始聊呢。")
                return
            d = chat(f"/api/chat/threads/{tid}?after=0&limit=20", token=tok)
            posts = [p for p in d.get("posts", []) if p.get("role") == "assistant"]
            send_text(event, posts[-1]["text"] if posts else "线程里还没有回复。")
            return

        to = ""
        if text.startswith("/枢"):
            to, text = "伙伴", text[2:].strip()
            if not text:
                send_text(event, "用法：/枢 内容")
                return

        tid = ensure_thread(key, "QQ 对话" if event.get("message_type") == "group"
                            else f"QQ {user_id}（{peer_of(user_id).get('name') or user_id}）", token=tok)

        if to == "伙伴":
            d = chat(f"/api/chat/threads/{tid}/messages",
                     {"text": text, "to": "伙伴", "wait": False}, timeout=30, token=tok)
            cur = d.get("cursor", 0)
            set_cursor(key, cur)
            send_text(event, "转给伙伴了，他处理完会回在这个对话里；稍后发 /看 也行。")
            reply = poll_for_reply(key, tid, cur, token=tok, timeout=120)
            if reply:
                send_text(event, reply)
            return

        d = chat(f"/api/chat/threads/{tid}/messages",
                 {"text": text, "wait": True, "timeout": 100}, timeout=130, token=tok)
        reply = (d.get("reply") or {}).get("text")
        if reply:
            set_cursor(key, (d.get("reply") or {}).get("id", 0))
            send_text(event, reply)
        elif d.get("note"):
            send_text(event, d["note"])
        else:
            send_text(event, "助手还在想，稍后发 /看 取回复。")
    except Exception as exc:  # noqa: BLE001
        log(f"处理失败: {exc}")
        send_text(event, f"这条没处理成：{type(exc).__name__}。稍后再试，或去面板 /chat 看。")


def worker(event: dict) -> None:
    with _lock:          # OneBot 事件是串行的；串行处理避免同一线程并发写
        handle(event)


# ── HTTP ────────────────────────────────────────────────────────────
@app.post("/onebot")
async def onebot_event(request: Request):
    auth = request.headers.get("authorization", "")
    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
    if not supplied:
        supplied = request.query_params.get("access_token", "").strip()
    if not BRIDGE_TOKEN or supplied != BRIDGE_TOKEN:
        client = request.client.host if request.client else ""
        # 兜底：NapCat 的 httpClient 实测不发 Authorization（token 字段没生效），
        # 而它只能来自本 compose 的 docker 网段。只对该网段放行；其它来源一律 401。
        if client.startswith("172.19."):
            if supplied:
                log(f"接受 docker 网段上报（带了不匹配的 token，已忽略）{client}")
        else:
            log(f"鉴权失败 client={client} auth={auth[:20]!r} "
                f"q={request.query_params.get('access_token','')[:20]!r}")
            return JSONResponse({"status": "denied"}, status_code=401)
    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        return {"status": "bad-json"}
    if event.get("post_type") != "message":
        return {"status": "ignored"}
    mid = str(event.get("message_id") or "")
    if mid and seen_before(mid):
        return {"status": "duplicate"}
    uid = str(event.get("user_id") or "")
    if uid not in ALLOW:
        log(f"忽略非白名单 QQ {uid}")
        return {"status": "not-allowed"}
    # 立刻 ack，处理放后台：OneBot 的上报超时很短，同步处理会被重投
    threading.Thread(target=worker, args=(event,), daemon=True).start()
    return {"status": "ok"}


@app.post("/publish")
async def publish_event(request: Request):
    """本机内部发布口：把另一条通道（Reverse WS）收到的事件送进同一个 WS 广播。

    边界：只允许**本机回环** + `QQ_BRIDGE_TOKEN`；只接受 qq_in / qq_out。
    这里只做转发，不建线程、不调聊天接口、不发 QQ —— 端点「只收不回」。
    """
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1"):
        return JSONResponse({"status": "denied"}, status_code=403)
    auth = request.headers.get("authorization", "")
    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not BRIDGE_TOKEN or supplied != BRIDGE_TOKEN:
        return JSONResponse({"status": "denied"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"status": "bad-json"}
    if payload.get("type") not in ("qq_in", "qq_out"):
        return {"status": "ignored"}
    if not payload.get("qq") or not isinstance(payload.get("text"), str):
        return {"status": "ignored"}
    payload.setdefault("ts", time.time())
    payload["via"] = "reverse-ws"
    _broadcast(payload)
    return {"status": "ok"}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """外部订阅端点：连上后实时收 qq_in / qq_out 事件。

    鉴权：`?token=<对话伙伴 token>` 或 `Authorization: Bearer <token>`。
    令牌用 /api/chat/health 验一次（不落日志）。
    """
    token = ws.query_params.get("token", "").strip()
    auth = ws.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    ok = False
    if token:
        try:
            d = await asyncio.to_thread(chat, "/api/chat/health", None, 8, token)
            ok = bool(d.get("ok"))
        except Exception:  # noqa: BLE001
            ok = False
    if not ok:
        await ws.close(code=4401)
        return
    await ws.accept()
    SUBSCRIBERS.add(ws)
    log(f"WS 订阅者接入（当前 {len(SUBSCRIBERS)}）")
    try:
        await ws.send_json({"type": "hello", "subscribers": len(SUBSCRIBERS)})
        while True:
            await ws.receive_text()      # 客户端消息暂不处理，只保活
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        SUBSCRIBERS.discard(ws)
        log(f"WS 订阅者离开（剩余 {len(SUBSCRIBERS)}）")


@app.get("/health")
def health():
    with db() as c:
        links = c.execute("SELECT COUNT(*) n FROM links").fetchone()["n"]
        seen = c.execute("SELECT COUNT(*) n FROM seen").fetchone()["n"]
    return {"ok": True, "allow": len(ALLOW), "links": links, "seen": seen,
            "peers": {k: v.get("name") for k, v in PEERS.items()},
            "subscribers": len(SUBSCRIBERS),
            "dry_run": DRY_RUN, "onebot": ONEBOT_API, "chat": CHAT_BASE}


def main() -> None:
    import uvicorn
    init_db()
    if not BRIDGE_TOKEN:
        log("⚠️  QQ_BRIDGE_TOKEN 为空：所有上报都会被拒（401）")
    if not ALLOW:
        log("⚠️  QQ_ALLOW 为空：所有消息都会被忽略（fail closed）")
    port = int(os.environ.get("QQ_BRIDGE_PORT", "8770"))
    host = os.environ.get("QQ_BRIDGE_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
