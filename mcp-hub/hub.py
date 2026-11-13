"""MCP Hub（:8765）—— 多 Agent 协作中枢。

两个面：
1. MCP streamable-http（/mcp）：站娘（DSH dsh-mcp-client，serverName=hub）连接，
   获得任务队列、用户沙箱读写、blackboard、worker 名册工具。
2. Worker REST（/worker/*）：跨设备 worker 客户端轮询领任务，token 或注册-审批认证。

与面板共享 panel.db（WAL 多进程）；用户沙箱路径校验复用 app.sandbox。
任务流转：pending → running（claim 原子抢占）→ done / failed。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

# 让 hub 进程能 import 面板的 app 包（共享 db/sandbox）
BACKEND_DIR = Path(__file__).resolve().parents[1] / "panel" / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from starlette.routing import Mount, Route

from app import sandbox
from app.db import DATA_DIR, DB, get_db

# 对话接口（2026-10-23）：复用面板侧 app/chat.py 的表与函数，别写第二份。
# 导入失败只降级为「工具不可用」，不能让 hub 起不来。
try:
    from app import chat as chatmod
except Exception as _chat_import_exc:  # noqa: BLE001
    chatmod = None
    print(f"[hub] chat 模块不可用: {_chat_import_exc}", file=sys.stderr)

mcp = FastMCP("hub")

# 站娘/驻场分身可领取的 assignee 名；assignee 为空 = 任何人可领。
# 部署方可用 SUITE_AGENT_NAME 换成自己的人设名（默认「助手」）。
_AGENT_NAME = os.environ.get("SUITE_AGENT_NAME", "助手").strip() or "助手"
PERSONA_NAMES = tuple(dict.fromkeys((_AGENT_NAME, "助手", "resident", "")))

# 设备交回产物的落盘目录（助手 2026-10-14）：给"在设备上构建、把结果带回来"用，
# 免得为了一个 exe 去借第三方文件站。
ARTIFACT_DIR = DATA_DIR / "device-artifacts"
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


def _task_view(t: dict) -> dict:
    return {k: t[k] for k in ("id", "ts", "user_name", "kind", "title", "prompt",
                              "assignee", "status", "result")}


# ── 站娘工具（MCP） ─────────────────────────────────────────────

@mcp.tool
def task_poll(limit: int = 20) -> str:
    """查看任务队列中可认领的待办任务（assignee 为空或点名站娘的）。

    每次会话开始与空闲时都应调用；认领用 task_claim。
    """
    if (err := root_only()):
        return err
    db = get_db()
    rows = db.query(
        "SELECT * FROM tasks WHERE status='pending' AND (assignee='' OR assignee IN (?,?))"
        " ORDER BY ts LIMIT ?", (_AGENT_NAME, "助手", min(limit, 50)))
    return json.dumps([_task_view(t) for t in rows], ensure_ascii=False, indent=1)


@mcp.tool
def task_claim(task_id: str) -> str:
    """认领一个待办任务（原子抢占，pending→running）。认领后必须处理并用 task_complete/task_fail 收尾。"""
    if (err := root_only()):
        return err
    db = get_db()
    task = db.claim_task(task_id, _AGENT_NAME)
    if task is None:
        return "ERROR: 任务不存在或已被他人认领"
    return json.dumps(_task_view(task), ensure_ascii=False, indent=1)


@mcp.tool
def task_complete(task_id: str, result: str, tokens_used: int = 0) -> str:
    """完成任务并写回结果。tokens_used 填本次为该任务消耗的 token 估算值（用于用户配额计量）。"""
    if (err := root_only()):
        return err
    task = get_db().complete_task(task_id, _AGENT_NAME, result, max(0, tokens_used))
    return "OK: 结果已回写" if task else "ERROR: 任务不在 running 状态"


@mcp.tool
def task_fail(task_id: str, error: str) -> str:
    """任务无法完成时标记失败并说明原因。"""
    if (err := root_only()):
        return err
    db = get_db()
    task = db.get_task(task_id)
    if task is None or task["status"] != "running":
        return "ERROR: 任务不在 running 状态"
    db.set_task_status(task_id, "failed", f"失败原因: {error}")
    return "OK"


def _task_sandbox(task_id: str) -> tuple[DB, dict, Path]:
    db = get_db()
    task = db.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    ident = current_identity()
    if ident.get("kind") == "agent" and task["user_name"] != ident.get("username"):
        raise PermissionError(f"该凭据不能访问账号 {task['user_name']} 的任务")
    return db, task, sandbox.sandbox_root_for(task["user_name"])


@mcp.tool
def sandbox_list(task_id: str, path: str = "") -> str:
    """列出任务所属用户的沙箱目录内容（path 为沙箱内相对路径）。"""
    if (err := chat_only()):
        return err
    try:
        _, task, root = _task_sandbox(task_id)
        items = sandbox.list_dir(task["user_name"], path)
        return json.dumps(items, ensure_ascii=False, indent=1)
    except (ValueError, FileNotFoundError, PermissionError) as exc:
        return f"ERROR: {exc}"


@mcp.tool
def sandbox_read(task_id: str, path: str) -> str:
    """读取任务所属用户沙箱内的文件（相对路径，禁止 ..，上限 2MB）。"""
    if (err := chat_only()):
        return err
    try:
        _, task, root = _task_sandbox(task_id)
        return sandbox.read_file(task["user_name"], path)
    except (ValueError, FileNotFoundError, PermissionError) as exc:
        return f"ERROR: {exc}"


@mcp.tool
def sandbox_write(task_id: str, path: str, content: str) -> str:
    """写入任务所属用户沙箱内的文件（相对路径，受磁盘配额约束）。用户任务的产出必须写在这里。"""
    if (err := chat_only()):
        return err
    try:
        _, task, root = _task_sandbox(task_id)
        info = sandbox.write_file(task["user_name"], path, content)
        return f"OK: 已写入 {info['relpath']}（{info['size']} 字节）"
    except (ValueError, PermissionError) as exc:
        return f"ERROR: {exc}"


@mcp.tool
def worker_list() -> str:
    """查看已注册的协作 worker（名称、能力、是否在线、是否用户可用）。用户任务可指派给 user_available=true 的 worker。"""
    if (err := chat_only()):
        return err
    rows = get_db().list_workers()
    view = [{"name": w["name"], "capabilities": json.loads(w["capabilities"]),
             "user_available": bool(w["user_available"]), "approved": bool(w["approved"]),
             "status": w["status"], "last_seen": w["last_seen"]} for w in rows]
    return json.dumps(view, ensure_ascii=False, indent=1)


@mcp.tool
def blackboard_write(key: str, value: str, author: str = "") -> str:
    """向共享黑板写入笔记（跨会话/跨 agent 共享），如巡检结论、任务上下文。

    author 为 updated_by 署名：留空默认「助手」（站娘本机通道的历史习惯）；
    其他管理员（如伙伴）调用时**必须显式传自己的名字**，否则署名会失真——
    本机通道在身份层都是 root，不打开这个口子就无法区分是谁写的。
    """
    if (err := root_only()):
        return err
    get_db().bb_write(key, value, author.strip()[:32] or _AGENT_NAME)
    return f"OK: blackboard[{key}] 已更新"


@mcp.tool
def blackboard_read(key: str) -> str:
    """读取共享黑板笔记；key 传 __list__ 列出全部条目。"""
    if (err := chat_only()):
        return err
    db = get_db()
    if key == "__list__":
        return json.dumps(db.bb_list(), ensure_ascii=False, indent=1)
    value = db.bb_read(key)
    return value if value is not None else f"（无条目: {key}）"


@mcp.tool
def blackboard_tail(key: str, lines: int = 40) -> str:
    """只读黑板条目的**末尾若干行**（大键别整读）。lines 默认 40，上限 400。

    动机（2026-11-13）：`admin/open-items` 已 70KB，整读一次 ≈ 2 万 token 且每轮重发。
    要「看最新几条」就只用这个，别调 blackboard_read。
    """
    if (err := chat_only()):
        return err
    v = get_db().bb_read(key)
    if v is None:
        return f"（无条目: {key}）"
    ls = v.splitlines()
    n = max(1, min(int(lines or 40), 400))
    head = ""
    if len(ls) > n:
        head = f"…（共 {len(ls)} 行 / {len(v)} 字符，只显示末 {n} 行）\n"
    return head + "\n".join(ls[-n:])


@mcp.tool
def blackboard_search(query: str, limit: int = 20) -> str:
    """在**全部**黑板条目里按关键词搜，返回「键 / 行号 / 那一行」，**不返回全文**。

    用于从大键里定点取几行（替代整读）。仅管理员可用：它会扫全站条目，比单键读的面宽。
    """
    if (err := root_only()):
        return err
    q = (query or "").strip()
    if len(q) < 2:
        return "关键词至少 2 个字符"
    n = max(1, min(int(limit or 20), 100))
    rows = get_db().query(
        "SELECT key, value FROM blackboard WHERE value LIKE ? ORDER BY updated_at DESC LIMIT 200",
        (f"%{q}%",))
    out, hit = [], 0
    for r in rows:
        for i, line in enumerate((r["value"] or "").splitlines(), 1):
            if q.lower() in line.lower():
                out.append(f"{r['key']}:{i}: {line.strip()[:200]}")
                hit += 1
                if hit >= n:
                    break
        if hit >= n:
            break
    if not out:
        return f"（全板未命中: {q}）"
    return f"命中 {len(out)} 行（上限 {n}）—— 只给定位，读细节再用 blackboard_tail\n" + "\n".join(out)


@mcp.tool
def blackboard_sections(key: str) -> str:
    """列出黑板条目的**标题结构**（行号 + 标题前 100 字），不出正文。

    用法：先 sections 看结构 → 再 `blackboard_tail(key, 行数)` 或 `blackboard_search` 取具体段。
    这是「大键别整读」的第三块拼图（2026-11-13）。
    """
    if (err := chat_only()):
        return err
    v = get_db().bb_read(key)
    if v is None:
        return f"（无条目: {key}）"
    lines = v.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        t = line.strip()
        if not t:
            continue
        if t.startswith(("■", "【", "#", "▶", "##")) or re.match(r"^\d+[.、]", t):
            out.append(f"{i:>5}: {t[:100]}")
        if len(out) >= 200:
            break
    if not out:
        return f"（{key}：{len(lines)} 行，未识别到标题行；用 blackboard_tail 直接看末尾）"
    return (f"{key}：共 {len(lines)} 行 / {len(v)} 字符，识别到 {len(out)} 个标题\n"
            + "\n".join(out))


# ── 站娘设备工具（用户设备沟通） ─────────────────────────────────

def _user_device(db: DB, user_name: str, device_name: str) -> dict:
    """按用户名 + 设备名取设备，校验归属（站娘只能操作当前对话用户的设备）。"""
    user = db.one("SELECT id FROM accounts WHERE username=?", (user_name,))
    if user is None:
        raise ValueError(f"用户不存在: {user_name}")
    d = db.one("SELECT * FROM devices WHERE user_id=? AND name=?", (user["id"], device_name))
    if d is None:
        raise ValueError(f"用户 {user_name} 没有名为 {device_name} 的设备")
    return d


@mcp.tool
def device_list(user_name: str) -> str:
    """查看某用户名下的自有设备（名称、描述、在线状态、最近心跳）。

    user_name 填当前对话用户的用户名（站娘只能看到该用户的设备）。
    """
    if (err := chat_only()):
        return err
    user_name, err = scoped_user(user_name)
    if err:
        return f"ERROR: {err}"
    db = get_db()
    user = db.one("SELECT id FROM accounts WHERE username=?", (user_name,))
    if user is None:
        return f"ERROR: 用户不存在: {user_name}"
    rows = db.list_devices(user["id"])
    view = [{"name": d["name"], "desc": d["desc"],
             "online": (time.time() - d["last_seen"]) < 90,
             "last_seen": d["last_seen"]} for d in rows]
    return json.dumps(view, ensure_ascii=False, indent=1)


@mcp.tool
def device_send(user_name: str, device_name: str, text: str) -> str:
    """向用户的某台设备下发一条指令（文本）。设备在线时会在数秒到数十秒内取走执行。

    user_name 填当前对话用户的用户名；text 描述要设备执行的事（设备端 handler 自行解释）。
    下发成功返回指令 id，用 device_results 查询执行结果。
    """
    if (err := chat_only()):
        return err
    user_name, err = scoped_user(user_name)
    if err:
        return f"ERROR: {err}"
    db = get_db()
    try:
        d = _user_device(db, user_name, device_name)
    except ValueError as exc:
        return f"ERROR: {exc}"
    cmd = db.queue_device_command(d["id"], text, source="assistant")
    return f"OK: 指令已下发（id={cmd['id']}），设备 {device_name} 将在下次心跳时取走。可用 device_results 查询结果"


@mcp.tool
def device_results(user_name: str, device_name: str, limit: int = 5) -> str:
    """查询用户某台设备最近的指令与执行结果（新→旧）。下发指令后建议等待一次心跳周期（约 15-30 秒）再查询。"""
    if (err := chat_only()):
        return err
    user_name, err = scoped_user(user_name)
    if err:
        return f"ERROR: {err}"
    db = get_db()
    try:
        d = _user_device(db, user_name, device_name)
    except ValueError as exc:
        return f"ERROR: {exc}"
    rows = db.list_device_commands(d["id"], max(1, min(limit, 20)))
    view = [{"id": c["id"], "status": c["status"], "text": c["text"][:200],
             "result": c["result"][:2000], "source": c["source"],
             "created_at": c["created_at"]} for c in rows]
    return json.dumps(view, ensure_ascii=False, indent=1)


# ── 链接自检与队伍编排（助手 2026-10-14）──────────────────────────

def _subtask_view(s: dict) -> dict:
    return {k: s.get(k) for k in ("id", "step", "title", "prompt", "assignee",
                                  "capability", "status", "result", "depends_on",
                                  "finished_at")}


@mcp.tool
def link_ping() -> str:
    """链接自检：返回本次调用者是谁、权限范围、能做什么、服务端时间与随机挑战码。

    IDE/客户端首次接入时先调它 —— 把「连上没有」从远程猜谜变成一行结论。
    """
    ident = current_identity()
    return json.dumps({
        "ok": True,
        "identity": ident.get("kind"),
        "name": ident.get("name"),
        "username": ident.get("username") or None,
        "scope": "全站（管理员）" if ident.get("kind") == "root"
                 else f"仅账号 {ident.get('username') or '未绑定'}",
        "capabilities": ident.get("capabilities") or [],
        "server_time": round(time.time(), 3),
        "nonce": secrets.token_hex(8),
    }, ensure_ascii=False, indent=1)


@mcp.tool
def agent_list() -> str:
    """队员名册：谁在线、属于谁、有哪些能力。

    站娘（管理员通道）看全站；用户凭据只看自己账号的队员。
    """
    if (err := chat_only()):
        return err
    ident = current_identity()
    db = get_db()
    rows = (db.list_user_agents(ident["user_id"]) if ident.get("kind") == "agent"
            else db.list_agents())
    ts = time.time()
    return json.dumps([{
        "name": a.get("name"), "agent_type": a.get("agent_type", ""),
        "platform": a.get("platform", ""), "host": a.get("host", ""),
        "capabilities": json.loads(a.get("capabilities") or "[]"),
        "online": (ts - (a.get("last_seen") or 0)) < 90,
        "last_seen": a.get("last_seen"),
    } for a in rows], ensure_ascii=False, indent=1)


# ── 对话接口（MCP 工具版 · 2026-10-23）─────────────────────────────
# 昨天上线的 /api/chat 只有 HTTP：Trae / DSH 会话想聊一句得先 curl，不像 agent
# 该有的样子。这里把同一套 chat_threads/chat_posts 暴露成工具，并补一条镜像：
# 发给别的 agent 时同步进 agent_messages，对方下一次 /agent/poll 就能拿到
# （正文带 [chat:<tid>#<post>] 前缀，它知道该回哪个线程）。
#
# 归属约定：MCP 根身份没有名字（IDENTITY_ROOT.name=admin），root 调用者用
# as_name 自报面；agent 身份强制用自己名字、不能改名 —— 多写入面共用同一
# 名字的坑（open-items §11/§12）不再重犯。

def _chat_ident(as_name: str = "") -> dict:
    ident = current_identity()
    if ident.get("kind") == "agent":
        name = (ident.get("name") or "agent")[:32]
        return {"id": f"agent:{ident.get('agent_id') or name}", "name": name, "kind": "peer"}
    if ident.get("kind") == "chat":
        # 对话伙伴：强制用登记名，不能 as_name 自报面，也不能当 admin 看全部线程
        name = (ident.get("name") or "peer")[:32]
        return {"id": f"peer:{name}", "name": name, "kind": "peer"}
    name = (as_name or "").strip()[:32] or "站娘"
    return {"id": f"mcp:{name}", "name": name, "kind": "admin"}


def _chat_ready() -> str:
    return "" if chatmod is not None else \
        "ERROR: 对话模块不可用（app.chat 导入失败，见 hub 启动日志）"


@mcp.tool
def chat_threads(limit: int = 20) -> str:
    """对话线程列表（新的在前）：id / 标题 / 参与者 / 最后一条摘要。

    用 chat_create 开线程、chat_send 说话、chat_read 取回复。
    这是「聊天」，不是派活：派活仍走 task_create/subtask_create。
    """
    if (err := _chat_ready()):
        return err
    ident = _chat_ident()
    db = get_db()
    rows = db.query("SELECT * FROM chat_threads ORDER BY last_at DESC LIMIT ?",
                    (min(max(int(limit), 1), 100),))
    out = []
    for t in rows:
        if not chatmod._can_see(t, ident):
            continue
        last = db.one("SELECT text, from_name FROM chat_posts WHERE thread_id=?"
                      " ORDER BY id DESC LIMIT 1", (t["id"],))
        v = chatmod._thread_view(t)
        v["last"] = ({"from": last["from_name"], "text": last["text"][:120]}
                     if last else None)
        out.append(v)
    return json.dumps(out, ensure_ascii=False, indent=1)


@mcp.tool
def chat_create(title: str, participants: str = "", as_name: str = "") -> str:
    """新开一个对话线程，返回 {thread_id, participants}。

    participants 用逗号分隔（如 "伙伴-A,助手"）；会自动补上自己与助手。
    as_name：root 调用者自报面（助手 / 伙伴 / 管理员），agent 身份忽略此项。
    """
    if (err := _chat_ready()):
        return err
    ident = _chat_ident(as_name)
    parts = [p.strip() for p in re.split(r"[,，\s]+", participants or "") if p.strip()]
    if ident["name"] not in parts:
        parts.insert(0, ident["name"])
    if not any(p in chatmod.DUTY_NAMES for p in parts):
        parts.append(_AGENT_NAME)
    if len(parts) == 1:          # 创建者本身就是助手：默认对端是管理员
        parts.append("管理员")
    tid = secrets.token_hex(8)
    ts = time.time()
    db = get_db()
    with db.tx() as c:
        c.execute("INSERT INTO chat_threads"
                  " (id,title,kind,participants,created_by,created_at,last_at,meta)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (tid, (title or f"{ident['name']} 的对话")[:120], "peer",
                   json.dumps(parts, ensure_ascii=False), ident["id"], ts, ts, "{}"))
    return json.dumps({"ok": True, "thread_id": tid, "participants": parts},
                      ensure_ascii=False)


@mcp.tool
def chat_send(thread_id: str, text: str, to: str = "", as_name: str = "") -> str:
    """在线程里说一句话，立刻返回游标；收件人是助手时后台生成回复。

    - 收件人默认在线程参与者里找助手；`to` 可点名别人（如 "伙伴"）。
    - 发给助手：立即返回 {"reply_pending": true}，随后用 chat_read(thread_id, after=cursor) 取回复。
    - 发给其它已登记 agent：正文会镜像进 agent_messages，对方下次 /agent/poll 收到
      （带 [chat:<tid>#<post>] 前缀，它可用 chat_send 回到同一线程）。
    """
    if (err := _chat_ready()):
        return err
    ident = _chat_ident(as_name)
    db = get_db()
    t = db.one("SELECT * FROM chat_threads WHERE id=?", (thread_id,))
    if t is None:
        return "ERROR: 线程不存在（先 chat_threads 看 id，或 chat_create 新建）"
    if not chatmod._can_see(t, ident):
        return "ERROR: 无权访问该线程"
    text = (text or "").strip()
    if not text:
        return "ERROR: 消息为空"
    post = chatmod._append(thread_id, ident, "user",
                           text[:chatmod.MAX_INPUT_CHARS], {"to": to})
    target = chatmod._target_of(to, t, ident)
    if target in chatmod.DUTY_NAMES:
        threading.Thread(target=chatmod._duty_reply, args=(t, ident, text),
                         daemon=True).start()
        return json.dumps({"ok": True, "post": chatmod._post_view(post),
                           "cursor": post["id"], "reply_pending": True,
                           "target": target,
                           "next": f"chat_read('{thread_id}', after={post['id']})"},
                          ensure_ascii=False)
    note = ""
    if target and db.one("SELECT name FROM agents WHERE name=?", (target,)):
        db.send_agent_message("", ident["name"], target,
                              f"[chat:{thread_id}#{post['id']}] {text}")
        note = f"已镜像进「{target}」的信箱"
    return json.dumps({"ok": True, "post": chatmod._post_view(post),
                       "cursor": post["id"], "reply_pending": False,
                       "target": target, "note": note}, ensure_ascii=False)


@mcp.tool
def chat_read(thread_id: str, after: int = 0, limit: int = 50) -> str:
    """读线程里 id > after 的消息（含 reply_pending 时来取助手的回复）。

    返回 {"posts": [...], "cursor": 最新 id}；下一轮把 cursor 传给 after。
    """
    if (err := _chat_ready()):
        return err
    ident = _chat_ident()
    db = get_db()
    t = db.one("SELECT * FROM chat_threads WHERE id=?", (thread_id,))
    if t is None:
        return "ERROR: 线程不存在"
    if not chatmod._can_see(t, ident):
        return "ERROR: 无权访问该线程"
    rows = db.query("SELECT * FROM chat_posts WHERE thread_id=? AND id>?"
                    " ORDER BY id LIMIT ?",
                    (thread_id, max(int(after), 0), min(max(int(limit), 1), 200)))
    return json.dumps({"posts": [chatmod._post_view(p) for p in rows],
                       "cursor": rows[-1]["id"] if rows else int(after)},
                      ensure_ascii=False)


@mcp.tool
def task_create(user_name: str, title: str, prompt: str = "", kind: str = "user-worker") -> str:
    """站娘为用户开一个父任务，之后用 subtask_create 拆给队员（站娘专属）。"""
    if (err := root_only()):
        return err
    db = get_db()
    user = db.one("SELECT * FROM accounts WHERE username=?", (user_name,))
    if user is None:
        return f"ERROR: 用户不存在: {user_name}"
    task = db.create_task(user, title, prompt or title, kind)
    return json.dumps(_task_view(task), ensure_ascii=False, indent=1)


@mcp.tool
def subtask_create(parent_task_id: str, step: int, title: str, prompt: str,
                   capability: str = "", assignee: str = "") -> str:
    """队长把父任务拆成一个子任务，放进队列等队员认领（站娘专属）。

    assignee 留空 = 任何具备该 capability 的队员都能领；capability 用队员
    注册时上报的能力标签（如 dsh_headless / code / general）。
    """
    if (err := root_only()):
        return err
    sub = get_db().create_subtask(parent_task_id, step, title, prompt, capability, assignee)
    return json.dumps(_subtask_view(sub), ensure_ascii=False, indent=1)


@mcp.tool
def subtask_status(parent_task_id: str) -> str:
    """查看某个父任务下全部子任务的状态与结果（站娘专属）。"""
    if (err := root_only()):
        return err
    subs = get_db().list_subtasks(parent_task_id)
    return json.dumps([_subtask_view(s) for s in subs], ensure_ascii=False, indent=1)


@mcp.tool
def subtask_reassign(subtask_id: str, assignee: str = "", capability: str = "") -> str:
    """把失败/卡住的子任务退回队列，可换队员或换能力标签后重试（站娘专属）。

    典型用法：subtask_status 看到 failed → 读原因（如本机拒绝/超时）→
    subtask_reassign 换个有该能力的队员，或补上 capability 让别的队员来领。
    """
    if (err := root_only()):
        return err
    sub = get_db().reset_subtask(subtask_id, assignee or None, capability or None)
    if sub is None:
        return f"ERROR: 子任务不存在: {subtask_id}"
    return json.dumps(_subtask_view(sub), ensure_ascii=False, indent=1)


@mcp.tool
def dispatch_to_agent(user_name: str, prompt: str, title: str = "",
                      capability: str = "") -> str:
    """一站派活：给某个用户开父任务并直接派给它**在线的**队员（站娘专属）。

    这是"会话内派活"的主入口：用户说"在我电脑上做 X"时，站娘一次调用即可 ——
    自动按 capability 挑在线队员，没有可用队员时明确回报（而不是留一条没人领的任务）。
    capability 留空 = 该用户任意在线队员。
    """
    if (err := root_only()):
        return err
    db = get_db()
    user = db.one("SELECT * FROM accounts WHERE username=?", (user_name.strip(),))
    if user is None:
        return f"ERROR: 用户不存在: {user_name}"
    agent = db.pick_user_agent(user["id"], capability)
    if agent is None:
        online = [a["name"] for a in db.list_user_agents(user["id"])
                  if (time.time() - (a["last_seen"] or 0)) < 90]
        return (f"ERROR: {user_name} 当前没有在线的队员。"
                f"已登记：{online or '（无）'}；让用户先跑 `bellhub-connector run`，"
                f"或改 capability（当前要求：{capability or '任意'}）")
    task = db.create_task(user, (title.strip() or prompt.strip()[:40]), prompt, "link-dispatch")
    sub = db.create_subtask(task["id"], 1, (title.strip() or prompt.strip()[:40]),
                            prompt, capability, agent["name"])
    return json.dumps({
        "task_id": task["id"], "subtask_id": sub["id"], "assignee": agent["name"],
        "capability": capability, "status": sub["status"],
        "next": f"用 task_report(task_id='{task['id']}') 取结果（队员执行需要几秒到几分钟）",
    }, ensure_ascii=False, indent=1)


@mcp.tool
def task_report(task_id: str) -> str:
    """收活：汇总某个父任务下全部子任务的状态与结果（站娘专属）。

    用户问"我让你办的事怎么样了"时调它；全部完成时 summary 就是可直接回报给用户的结论。
    """
    if (err := root_only()):
        return err
    db = get_db()
    task = db.get_task(task_id)
    if task is None:
        return f"ERROR: 任务不存在: {task_id}"
    subs = db.list_subtasks(task_id)
    failed = [s["id"] for s in subs if s["status"] == "failed"]
    return json.dumps({
        "task_id": task_id, "title": task["title"], "status": task["status"],
        "user": task["user_name"],
        "all_done": db.all_subtasks_done(task_id),
        "failed": failed,
        "subtasks": [_subtask_view(s) for s in subs],
        "summary": db.aggregate_subtask_results(task_id),
        "retry_hint": (f"有 {len(failed)} 个子任务失败，可 dispatch_retry(task_id='{task_id}') "
                       f"退回队列换队员重试") if failed else "",
    }, ensure_ascii=False, indent=1)


@mcp.tool
def dispatch_retry(task_id: str) -> str:
    """自动改派：把该任务下失败的子任务退回队列，并优先换到**另一个**在线队员（站娘专属）。

    仍没有别的队员时退回为无人认领（assignee 清空），任何具备该能力的队员都能领。
    """
    if (err := root_only()):
        return err
    db = get_db()
    task = db.get_task(task_id)
    if task is None:
        return f"ERROR: 任务不存在: {task_id}"
    user = db.one("SELECT * FROM accounts WHERE username=?", (task["user_name"],))
    moved = []
    for s in db.list_subtasks(task_id):
        if s["status"] != "failed":
            continue
        cand = db.pick_user_agent(user["id"], s["capability"] or "") if user else None
        # 有"另一个"在线队员就换过去；否则清空 assignee，退回给任意具备该能力的队员
        alt = cand["name"] if (cand is not None and cand["name"] != s["assignee"]) else ""
        db.reset_subtask(s["id"], alt, None)
        moved.append({"subtask_id": s["id"], "from": s["assignee"], "to": alt or "（待领取）"})
    return json.dumps({"task_id": task_id, "reassigned": moved,
                       "note": "已退回队列；队员下次心跳即会领取"},
                      ensure_ascii=False, indent=1)


# ── Worker REST ────────────────────────────────────────────────

def _auth_worker(request: Request) -> dict:
    auth_header = request.headers.get("x-worker-auth", "")
    name, _, token = auth_header.partition(":")
    if not name or not token:
        raise PermissionError("缺少 X-Worker-Auth 认证头（格式 name:token）")
    worker = get_db().verify_worker(name, token)
    if worker is None:
        raise PermissionError("认证失败：worker 名或 token 无效")
    return worker


async def _json_body(request: Request) -> dict:
    try:
        return json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return {}


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def worker_register(request: Request):
    """注册 worker：{name, capabilities} → pending，等管理员审批后才能领任务。响应含 token。"""
    body = await _json_body(request)
    name = str(body.get("name", "")).strip()[:32]
    if len(name) < 2:
        return _err(400, "name 至少 2 个字符")
    db = get_db()
    worker, token = db.register_worker(name, body.get("capabilities", []),
                                       request.client.host if request.client else "")
    return JSONResponse({"worker_id": worker["id"], "token": token,
                         "approved": bool(worker["approved"]),
                         "note": "未审批的 worker 不可领任务，请联系管理员在面板审批"})


async def worker_poll(request: Request):
    try:
        worker = _auth_worker(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    if not worker["approved"]:
        return JSONResponse({"error": "worker 未通过管理员审批", "approved": False}, status_code=403)
    db = get_db()
    # 领取范围：指名给这个 worker 的任务；若 user_available=true 还可领无主用户任务
    clauses = "status='pending' AND (assignee=?)"
    params: list = [worker["name"]]
    if worker["user_available"]:
        clauses += " OR (status='pending' AND assignee='' AND kind='user-worker')"
        # 管理面板把派给 worker 的用户任务 assignee 置为 worker 名；此处兼容无主
    rows = db.query(f"SELECT * FROM tasks WHERE {clauses} ORDER BY ts LIMIT 10", params)
    return JSONResponse({"tasks": [_task_view(t) for t in rows]})


async def worker_claim(request: Request):
    try:
        worker = _auth_worker(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    if not worker["approved"]:
        return _err(403, "worker 未通过管理员审批")
    body = await _json_body(request)
    task = get_db().claim_task(str(body.get("task_id", "")), worker["name"])
    if task is None:
        return _err(409, "任务不存在或已被认领")
    return JSONResponse({"task": _task_view(task)})


async def worker_complete(request: Request):
    try:
        worker = _auth_worker(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    body = await _json_body(request)
    task = get_db().complete_task(str(body.get("task_id", "")), worker["name"],
                                  str(body.get("result", "")),
                                  int(body.get("tokens_used", 0) or 0))
    if task is None:
        return _err(409, "任务不在 running 状态")
    return JSONResponse({"ok": True})


async def worker_fail(request: Request):
    try:
        worker = _auth_worker(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    body = await _json_body(request)
    db = get_db()
    task = db.get_task(str(body.get("task_id", "")))
    if task is None or task["status"] != "running":
        return _err(409, "任务不在 running 状态")
    db.set_task_status(task["id"], "failed", f"worker {worker['name']}: {body.get('error', '')}")
    return JSONResponse({"ok": True})


async def worker_heartbeat(request: Request):
    try:
        worker = _auth_worker(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    return JSONResponse({"ok": True, "name": worker["name"]})


# ── Agent Adapter REST（Hub-Adapter 枢纽模式） ───────────────────

def _auth_agent(request: Request) -> dict:
    auth_header = request.headers.get("x-agent-auth", "")
    name, _, token = auth_header.partition(":")
    if not name or not token:
        raise PermissionError("缺少 X-Agent-Auth 认证头（格式 name:token）")
    # HTTP 头按 iso-8859-1 传输，agent 名可能含非 ASCII（如「伙伴」）：
    # 客户端对名称做 percent-encoding，此处 unquote 还原；纯 ASCII 名不受影响。
    name = unquote(name)
    agent = get_db().verify_agent(name, token)
    if agent is None:
        raise PermissionError("认证失败：agent 名或 token 无效")
    return agent


async def agent_register(request: Request):
    """注册 Agent 适配器：{name, agent_type, capabilities, tools} → 返回 token。"""
    body = await _json_body(request)
    name = str(body.get("name", "")).strip()[:32]
    if len(name) < 2:
        return _err(400, "name 至少 2 个字符")
    db = get_db()
    agent, token = db.register_agent(
        name, body.get("agent_type", "adapter"),
        body.get("capabilities", []), body.get("tools", []),
        request.client.host if request.client else "")
    return JSONResponse({"agent_id": agent["id"], "token": token,
                         "approved": bool(agent["approved"]),
                         "note": "未审批的 agent 不可领任务，请联系管理员在面板审批"})


async def agent_heartbeat(request: Request):
    try:
        agent = _auth_agent(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    # 返回待认领的子任务数 + 未读消息数
    db = get_db()
    if not agent["approved"]:
        return JSONResponse({"ok": True, "approved": False, "note": "等待管理员审批"})
    pending = db.pending_subtasks(agent["name"])
    msgs = db.poll_agent_messages(agent["name"])
    return JSONResponse({"ok": True, "pending_subtasks": len(pending),
                         "messages": len(msgs)})


async def agent_poll(request: Request):
    """Agent 拉取可认领的子任务 + 未读消息。"""
    try:
        agent = _auth_agent(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    if not agent["approved"]:
        return _err(403, "agent 未通过管理员审批")
    db = get_db()
    pending = db.pending_subtasks(agent["name"])
    msgs = db.poll_agent_messages(agent["name"])
    return JSONResponse({"subtasks": [{"id": s["id"], "step": s["step"],
                                       "title": s["title"], "prompt": s["prompt"],
                                       "capability": s["capability"],
                                       "parent_task_id": s["parent_task_id"]}
                                      for s in pending],
                         "messages": [{"id": m["id"], "from": m["from_agent"],
                                       "text": m["text"], "task_id": m["task_id"]}
                                      for m in msgs]})


async def agent_claim(request: Request):
    """Agent 认领子任务。"""
    try:
        agent = _auth_agent(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    if not agent["approved"]:
        return _err(403, "agent 未通过管理员审批")
    body = await _json_body(request)
    sid = str(body.get("subtask_id", ""))
    sub = get_db().claim_subtask(sid, agent["name"])
    if sub is None:
        return _err(409, "子任务不存在或已被认领")
    return JSONResponse({"subtask": {"id": sub["id"], "step": sub["step"],
                                     "title": sub["title"], "prompt": sub["prompt"],
                                     "capability": sub["capability"],
                                     "parent_task_id": sub["parent_task_id"],
                                     "depends_on": sub["depends_on"]}})


async def agent_complete(request: Request):
    """Agent 上报子任务完成。"""
    try:
        agent = _auth_agent(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    body = await _json_body(request)
    db = get_db()
    sub = db.complete_subtask(str(body.get("subtask_id", "")),
                              str(body.get("result", "")),
                              int(body.get("tokens_used", 0) or 0))
    if sub is None:
        return _err(409, "子任务不在 running 状态")
    return JSONResponse({"ok": True})


async def agent_fail(request: Request):
    try:
        agent = _auth_agent(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    body = await _json_body(request)
    db = get_db()
    db.fail_subtask(str(body.get("subtask_id", "")),
                    f"agent {agent['name']}: {body.get('error', '')}")
    return JSONResponse({"ok": True})


async def agent_message(request: Request):
    """Agent 给另一个 Agent 发消息（经 Hub 中转）。"""
    try:
        agent = _auth_agent(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    body = await _json_body(request)
    to = str(body.get("to_agent", "")).strip()
    if not to:
        return _err(400, "to_agent 不能为空")
    mid = get_db().send_agent_message(
        str(body.get("task_id", "")), agent["name"], to,
        str(body.get("text", "")))
    return JSONResponse({"ok": True, "message_id": mid})


# ── 用户设备 REST（我的设备：心跳即拉指令，执行后回传） ────────────

def _auth_device(request: Request) -> dict:
    auth_header = request.headers.get("x-device-auth", "")
    name, _, token = auth_header.partition(":")
    if not name or not token:
        raise PermissionError("缺少 X-Device-Auth 认证头（格式 name:token）")
    device = get_db().verify_device(name, token)
    if device is None:
        raise PermissionError("认证失败：设备名或 token 无效")
    return device


async def device_heartbeat(request: Request):
    """设备心跳：刷新在线状态并取走待执行指令（pending→sent）。"""
    try:
        device = _auth_device(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    db = get_db()
    db.device_online(device["id"], request.client.host if request.client else "")
    commands = db.take_device_commands(device["id"])
    return JSONResponse({
        "ok": True,
        # id 一律以字符串下发：device_commands.id 是自增整数，但设备端 agent 会把
        # 它塞进 subprocess 参数表，传 int 会让旧版 agent 抛
        # TypeError: expected str, bytes or os.PathLike object, not int，
        # 导致该设备所有指令失败（2026-10-13 实证）。线上格式统一用不透明字符串，
        # 回传时 finish_device_command 会自行 int() 转换，新旧客户端都能用。
        "commands": [{"id": str(c["id"]), "text": c["text"]} for c in commands],
    })


async def device_artifact(request: Request):
    """设备把构建产物/日志交回服务器（X-Device-Auth 鉴权，原始字节体）。

    设备令牌本就等价于"这台机器上的执行权"，让它把产物交回来是对称能力：
    在设备上打完包直接 POST 本端点即可，不必借道 paste.rs / catbox 之类。
    文件名会被净化（去路径成分、限字符集与长度），超过上限直接 413。
    """
    try:
        device = _auth_device(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    raw_name = (request.headers.get("x-artifact-name") or "artifact.bin").strip()
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(raw_name).name)[:80] or "artifact.bin"
    body = await request.body()
    if not body:
        return _err(400, "空文件")
    if len(body) > MAX_ARTIFACT_BYTES:
        return _err(413, f"文件过大（上限 {MAX_ARTIFACT_BYTES // 1048576}MB）")
    digest = hashlib.sha256(body).hexdigest()
    want = (request.headers.get("x-artifact-sha256") or "").lower().strip()
    if want and want != digest:
        return _err(400, f"sha256 不符：声明 {want[:16]}… 实际 {digest[:16]}…")
    dest_dir = ARTIFACT_DIR / re.sub(r"[^A-Za-z0-9._-]", "_", device["name"])[:40]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    dest.write_bytes(body)
    return JSONResponse({"ok": True, "device": device["name"], "path": str(dest),
                         "size": len(body), "sha256": digest})


async def device_report(request: Request):
    """设备上报指令执行结果（done / failed）。"""
    try:
        device = _auth_device(request)
    except PermissionError as exc:
        return _err(401, str(exc))
    body = await _json_body(request)
    ok = str(body.get("status", "done")) == "done"
    cmd = get_db().finish_device_command(
        str(body.get("cmd_id", "")),
        "done" if ok else "failed",
        str(body.get("result", "") or body.get("error", "")))
    if cmd is None or cmd["device_id"] != device["id"]:
        return _err(409, "指令不存在或不在等待结果状态")
    return JSONResponse({"ok": True})


# ── MCP 层鉴权（助手 2026-10-13 加固）──────────────────────────────
# 背景：/mcp/ 原本不做任何鉴权，而 /mcp-hub/ 经 nginx→cpolar 暴露在公网，
# 任何人只要知道 URL 就能 initialize 并调用 device_send 等工具（某台设备的
# handler 是 shell_handler.py，等于公网任意命令执行入口）。
#
# 策略：只对「经 nginx 代理进来的公网请求」强制 Bearer 密钥。
#   公网  → nginx 注入 X-BellHub-Proxy: 1 → 必须带正确密钥，否则 401
#   本机  → 无该头 → 直接放行（Trae stdio 桥 / 站娘自己的 MCP 客户端零改动）
# 之所以不用「来源 IP 是否回环」判定：nginx 反代到 127.0.0.1:8765，公网与
# 本机在 hub 眼里都是回环地址，无法区分，必须由 nginx 显式打标。
TOKEN_FILE = Path(os.environ.get("MCP_TOKEN_FILE", str(Path(__file__).resolve().parents[1] / "data" / "mcp_token")))


def load_mcp_token() -> str:
    """读取/首次生成 MCP 访问密钥。文件权限 0600，内容 strip 后即密钥。"""
    if TOKEN_FILE.is_file():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    tok = secrets.token_urlsafe(32)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok + "\n", encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    print(f"[hub] 已生成 MCP 访问密钥: {TOKEN_FILE}", file=sys.stderr)
    return tok


MCP_TOKEN = load_mcp_token()

# ── 调用者身份（助手 2026-10-14）────────────────────────────────
# MCP 面原来只有「全站唯一密钥」一种身份，等于把所有人的设备执行权捆在一起，
# 普通用户根本没法各自接入。现在多一种身份：用户在面板配对后拿到的 agent 凭据
# 也能连 MCP，但**只能碰自己账号的资源**（设备/沙箱/名册）。
IDENTITY_ROOT = {"kind": "root", "user_id": "", "username": "", "name": "admin",
                 "agent_id": "", "capabilities": []}


def identity_for_token(supplied: str) -> dict | None:
    """全局密钥 → 管理员；agent 凭据 → 队员；对话伙伴凭据 → 只读聊天（kind=chat）。"""
    if supplied and hmac.compare_digest(supplied, MCP_TOKEN):
        return dict(IDENTITY_ROOT)
    # 对话伙伴（2026-10-23）：让 伙伴-A 这类伙伴能用自己的 token 连 MCP，
    # 但只允许 chat_* 工具 —— 不给它们 root 级密钥。
    try:
        peer = get_db().one(
            "SELECT * FROM chat_peers WHERE token_hash=? AND enabled=1",
            (hashlib.sha256(supplied.encode()).hexdigest(),))
    except Exception:  # noqa: BLE001 —— 老库没有 chat_peers 时不影响既有身份
        peer = None
    if peer:
        return {"kind": "chat", "user_id": "", "name": peer["name"], "agent_id": "",
                "username": "", "capabilities": ["chat"]}
    agent = get_db().verify_agent_token(supplied)
    if agent is None:
        return None
    account = get_db().get_account(agent["user_id"]) if agent["user_id"] else None
    return {"kind": "agent", "user_id": agent["user_id"], "name": agent["name"],
            "agent_id": agent["id"],
            "username": (account or {}).get("username", ""),
            "capabilities": json.loads(agent["capabilities"] or "[]")}


def current_identity() -> dict:
    """工具内读取本次调用身份；无 HTTP 上下文（stdio/内部调用）按管理员处理。"""
    try:
        from fastmcp.server.dependencies import get_http_request
        req = get_http_request()
    except Exception:                                  # noqa: BLE001
        return dict(IDENTITY_ROOT)
    if req is None:
        return dict(IDENTITY_ROOT)
    return (req.scope.get("state") or {}).get("bellhub_identity") or dict(IDENTITY_ROOT)


def scoped_user(user_name: str) -> tuple[str, str]:
    """把外部传入的 user_name 收敛到凭据真正的管理员，返回 (user_name, error)。"""
    ident = current_identity()
    if ident.get("kind") != "agent":
        return (user_name or "").strip(), ""
    mine = ident.get("username") or ""
    if not mine:
        return "", "该凭据未绑定任何账号，请重新配对"
    if user_name and user_name.strip() != mine:
        return "", f"该凭据只属于账号 {mine}，不能操作 {user_name.strip()} 的资源"
    return mine, ""


def root_only() -> str:
    """站娘专属工具（任务队列/黑板写/派活）不允许用户凭据调用。"""
    return "" if current_identity().get("kind") == "root" else "ERROR: 该操作仅站娘（本机管理员通道）可用"


def chat_only() -> str:
    """对话伙伴凭据（kind=chat）只能使用 chat_* 工具。

    它的权限面 = 聊天线程；黑板、名册、沙箱、设备一律不给。
    """
    if current_identity().get("kind") != "chat":
        return ""
    return "ERROR: 这是对话伙伴凭据，只能使用 chat_* 工具（聊天/读线程/建线程）"


class MCPAuthMiddleware:
    """公网 /mcp 请求必须带有效凭据（管理员密钥或某用户的 agent 凭据）。

    本机请求（无 X-BellHub-Proxy 标记）保持免鉴权：站娘自己的 MCP 客户端与
    stdio 桥都走这条路，动它会把现有链路一起打断。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path", "").startswith("/mcp"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in (scope.get("headers") or [])}
            public = bool((headers.get("x-bellhub-proxy") or "").strip())
            supplied = ""
            auth = headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            elif headers.get("x-mcp-token"):
                supplied = headers["x-mcp-token"].strip()
            ident = identity_for_token(supplied) if supplied else None
            if public and ident is None:
                body = json.dumps({
                    "error": "unauthorized",
                    "detail": "MCP 端点需要鉴权：请携带 Authorization: Bearer <token>。"
                              "个人凭据在面板「我的智能体」一键生成；管理员密钥见 "
                              "~/dsh-suite/data/mcp_token（仅管理员可读）。",
                }, ensure_ascii=False).encode()
                await Response(body, status_code=401,
                               headers={"WWW-Authenticate": "Bearer",
                                        "Content-Type": "application/json; charset=utf-8"})(scope, receive, send)
                return
            scope.setdefault("state", {})["bellhub_identity"] = ident or dict(IDENTITY_ROOT)
        await self.app(scope, receive, send)


# ── 组合 ASGI（原 §）────────────────────────────────────────────


# 2026-10-22 助手：修「BellHub MCP 又断了」的根因
#   fastmcp / MCP SDK 默认 session_idle_timeout = 1800 秒（30 分钟）：客户端
#   （如 Trae 直连 streamable-http）空闲超过 30 分钟后会话被回收，之后再请求
#   一律回 404 Session not found，而客户端往往要连撞好几次才肯重新 initialize
#   ⇒ 用户看到的就是「又断了」。实测：11:40 最后一次成功 → 12:34 起连续 404，
#   直到 13:41 客户端自行重开会话才恢复（nginx access.log 佐证）。
#   这里放宽到 30 天（会话对象极小，max_sessions 默认 10000）。
#   env BELLHUB_MCP_IDLE_TIMEOUT 可覆盖（秒）。
MCP_SESSION_IDLE_TIMEOUT = float(os.environ.get("BELLHUB_MCP_IDLE_TIMEOUT", str(30 * 24 * 3600)))
mcp_app = mcp.http_app(path="/", session_idle_timeout=MCP_SESSION_IDLE_TIMEOUT)

routes = [
    Mount("/mcp", app=mcp_app),
    Route("/worker/register", worker_register, methods=["POST"]),
    Route("/worker/heartbeat", worker_heartbeat, methods=["POST"]),
    Route("/worker/poll", worker_poll, methods=["POST"]),
    Route("/worker/claim", worker_claim, methods=["POST"]),
    Route("/worker/complete", worker_complete, methods=["POST"]),
    Route("/worker/fail", worker_fail, methods=["POST"]),
    Route("/agent/register", agent_register, methods=["POST"]),
    Route("/agent/heartbeat", agent_heartbeat, methods=["POST"]),
    Route("/agent/poll", agent_poll, methods=["POST"]),
    Route("/agent/claim", agent_claim, methods=["POST"]),
    Route("/agent/complete", agent_complete, methods=["POST"]),
    Route("/agent/fail", agent_fail, methods=["POST"]),
    Route("/agent/message", agent_message, methods=["POST"]),
    Route("/device/heartbeat", device_heartbeat, methods=["POST"]),
    Route("/device/artifact", device_artifact, methods=["POST"]),
    Route("/device/report", device_report, methods=["POST"]),
]

app = MCPAuthMiddleware(Starlette(routes=routes, lifespan=mcp_app.router.lifespan_context))
# 详见文件上方 MCPAuthMiddleware 注释：公网（带 X-BellHub-Proxy 头）必须带 Bearer 密钥


def main() -> None:
    import uvicorn
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
