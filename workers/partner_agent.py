#!/usr/bin/env python3
"""伙伴值守分身 —— 服务器侧常驻 Agent（Hub Agent Adapter 客户端）。

作用：让「伙伴」从「只能被留言、靠人唤醒」升级为「可被推送、近实时双向」。
本进程常驻服务器，轮询 mcp-hub 的 Agent 通道：

    /agent/poll    → 拉取待认领子任务 + 未读消息
    /agent/claim   → 认领子任务
    /agent/complete → 回传子任务结果（失败走 /agent/fail）
    /agent/message → 给其它 Agent（如助手）回消息

有活才起一个 `dsh headless` 进程（伙伴人格，DSH_HOME=~/.dsh-partner）跑任务，
空闲时只轮询、不调 LLM，避免空烧额度。

配置 partner.config.json（与本文件同目录，首次运行自动生成）：
{
  "hub": "http://127.0.0.1:8765",
  "name": "伙伴",
  "token": "",                 // 留空则首次运行自动注册（需管理员审批后可用）
  "agent_type": "resident",
  "capabilities": ["ops", "inspect", "shell", "long-task"],
  "tools": ["bash", "read", "write"],
  "poll_interval": 15,
  "dsh_home": str(Path.home() / ".dsh-partner"),
  "workdir": str(Path.home() / "deepseek-harness"),
  "timeout_sec": 900,
  "api_key": "",               // 留空则从 panel.db settings.assistant_api_key 读取
  "session_max_turns": 6,      // 会话滚动：同一对端满 N 轮自动开新会话（0=不滚动）
  "session_max_tokens": 80000, // 会话滚动：上一轮 totalTokens 达到阈值即滚动
  "rate_max_hops": 6,          // 速率熔断：窗口内同一对端超过 N 条消息则静默
  "rate_window_sec": 600,      // 速率熔断滑动窗口（秒）
  "loop_window_sec": 3600,     // 回合预算窗口（秒；跨会话/跨重启持久）
  "loop_max_replies": 3,       // 回合预算：窗口内同一对端最多回 N 条，超限静默
  "loop_max_tokens": 300000,   // 成本预算：窗口内同一对端累计 tokens 上限，超限静默
  "turn_max_tokens": 200000    // 单轮预算：该对端上一轮 tokens ≥ 此值 → 静默一轮（护栏 f）
}

纯标准库实现（urllib/json/sqlite3/subprocess），无第三方依赖。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

VERSION = "2026-10-16e"
HERE = Path(__file__).resolve().parent
# 配置路径可被 DSH_XIAOSHU_CONFIG 覆盖：root 版与 agent 版共用同一份脚本，
# 各自指向自己的配置文件（root 版隔离 DSH_HOME=/root/.dsh-partner）。
CONFIG_PATH = Path(os.environ.get("DSH_XIAOSHU_CONFIG") or (HERE / "partner.config.json"))
DEFAULT_DSH_DIR = Path.home() / "deepseek-harness"
DEFAULT_PANEL_DB = Path.home() / "dsh-suite/data/panel.db"
# 单例锁：固定路径，供 agent 版与 root 版共用一个 pid 文件，
# 防止两个值守实例同时轮询导致同一消息被重复处理。
LOCK_PATH = Path("/tmp/dsh-partner-agent.pid")

_stop = False
# 速率熔断的内存态：{partner: [收信时间戳]}，进程重启即清零（熔断本就是临时保护）。
_BOUNCE: dict[str, list[float]] = {}


def _dsh_dir(cfg: dict) -> Path:
    return Path(cfg.get("dsh_dir") or DEFAULT_DSH_DIR)


def _panel_db(cfg: dict) -> Path:
    return Path(cfg.get("panel_db") or DEFAULT_PANEL_DB)


def _node_bin(cfg: dict) -> str:
    """定位 node：优先配置，其次常见安装位置，最后 PATH。root 版需显式配置。"""
    if cfg.get("node_bin"):
        return cfg["node_bin"]
    for cand in (Path.home() / ".local/nodejs/bin/node",
                 Path.home() / ".local/nodejs/bin/node"):
        if cand.exists():
            return str(cand)
    return shutil.which("node") or "node"


def log(msg: str) -> None:
    print(f"[partner {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps({
            "hub": "http://127.0.0.1:8765",
            "name": "伙伴",
            "token": "",
            "agent_type": "resident",
            "capabilities": ["ops", "inspect", "shell", "long-task"],
            "tools": ["bash", "read", "write"],
            "poll_interval": 15,
            "dsh_home": str(Path.home() / ".dsh-partner"),
            "dsh_dir": str(DEFAULT_DSH_DIR),
            "workdir": str(DEFAULT_DSH_DIR),
            "panel_db": str(DEFAULT_PANEL_DB),
            "node_bin": "",
            "timeout_sec": 900,
            "api_key": "",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已生成默认配置 {CONFIG_PATH}，请填写 token（留空会自动注册）后重新运行")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def api(cfg: dict, path: str, body: dict | None = None) -> dict:
    """带 X-Agent-Auth 调 hub Agent 通道。"""
    url = cfg["hub"].rstrip("/") + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    # agent 名可能含非 ASCII（如「伙伴」）：HTTP 头按 iso-8859-1 传输，
    # 故对名称做 percent-encoding，hub 侧 unquote 还原。
    req.add_header("X-Agent-Auth", f"{quote(cfg['name'], safe='')}:{cfg['token']}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            return {"error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def register(cfg: dict) -> None:
    """首次运行：注册 Agent 适配器并持久化 token。"""
    url = cfg["hub"].rstrip("/") + "/agent/register"
    body = {"name": cfg["name"], "agent_type": cfg.get("agent_type", "resident"),
            "capabilities": cfg.get("capabilities", []), "tools": cfg.get("tools", [])}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    cfg["token"] = out["token"]
    _save_config(cfg)
    log(f"注册完成 token={out['token'][:6]}… approved={out.get('approved')}（未批准需管理员在面板审批）")


def _resolve_tsx_loader(cfg: dict) -> str:
    out = subprocess.run(
        [_node_bin(cfg), "-p", "require.resolve('tsx/esm', { paths: ['%s'] })" % _dsh_dir(cfg)],
        capture_output=True, text=True, timeout=30)
    loader = (out.stdout or "").strip()
    if not loader:
        raise RuntimeError(f"解析 tsx loader 失败: {out.stderr.strip()}")
    return loader


def _read_api_key(cfg: dict) -> str:
    if cfg.get("api_key"):
        return cfg["api_key"]
    try:
        db = sqlite3.connect(str(_panel_db(cfg)))
        row = db.execute("SELECT value FROM settings WHERE key='assistant_api_key'").fetchone()
        db.close()
        return (row[0] if row else "") or ""
    except Exception as exc:  # noqa: BLE001
        log(f"读取 panel.db 的 assistant_api_key 失败: {exc}")
        return ""


def run_headless(cfg: dict, prompt: str, session_id: str | None = None) -> dict:
    """跑一次伙伴 headless，返回 {reply, session_id, tokens}。"""
    node = _node_bin(cfg)
    dsh_dir = _dsh_dir(cfg)
    loader = _resolve_tsx_loader(cfg)
    workdir = Path(cfg.get("workdir") or dsh_dir)
    args = [node, "--import", f"file://{loader}",
            str(dsh_dir / "apps/cli/src/bin.ts"), "headless", "--json", "-"]
    if session_id:
        args = [*args[:-2], "--session-id", session_id, *args[-2:]]
    env = os.environ.copy()
    env["PATH"] = f"{Path(node).parent}:" + env.get("PATH", "")
    env["DSH_HOME"] = cfg.get("dsh_home") or str(Path.home() / ".dsh-partner")
    env["DEEPSEEK_API_KEY"] = _read_api_key(cfg)
    env["TSX_TSCONFIG_PATH"] = str(dsh_dir / "tsconfig.base.json")
    try:
        proc = subprocess.run(args, cwd=str(workdir), env=env, input=prompt,
                              capture_output=True, text=True,
                              timeout=int(cfg.get("timeout_sec", 900)))
    except subprocess.TimeoutExpired:
        return {"reply": f"（伙伴执行超时 >{cfg.get('timeout_sec', 900)}s）", "session_id": session_id,
                "tokens": 0, "error": True}
    except OSError as exc:
        return {"reply": f"（无法启动 dsh CLI: {exc}）", "session_id": session_id, "tokens": 0,
                "error": True}
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-500:]
        return {"reply": f"（伙伴执行失败 rc={proc.returncode}: {detail}）", "session_id": session_id,
                "tokens": 0, "error": True}
    return _parse_events(proc.stdout, session_id)


def _parse_events(stdout: str, session_id: str | None) -> dict:
    sid = session_id
    final_text = ""
    last_text = ""
    tokens = 0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "session" and ev.get("sessionId"):
            sid = ev["sessionId"]
        elif t == "final" and ev.get("text"):
            final_text = ev["text"]
        elif t == "text" and ev.get("text"):
            last_text = ev["text"]
        elif t == "status" and isinstance(ev.get("usage"), dict):
            tokens = max(tokens, int(ev["usage"].get("totalTokens", 0) or 0))
    return {"reply": (final_text or last_text or "(伙伴无输出)"), "session_id": sid,
            "tokens": tokens, "error": False}


# ── 会话连续性：同一对话方复用 session，实现多轮 ────────────────────

def _partners_path(cfg: dict) -> Path:
    return Path(cfg.get("dsh_home") or ".") / "partners.json"


def load_partners(cfg: dict) -> dict:
    p = _partners_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_partner(cfg: dict, partner: str, session_id: str,
                 turns: int = 0, last_tokens: int = 0) -> None:
    data = load_partners(cfg)
    data[partner] = {"session_id": session_id, "turns": turns, "last_tokens": last_tokens}
    p = _partners_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 会话成本护栏 v2026-10-14c：滚动 / 拦截 / 熔断 ───────────────────
# 背景：同一对端 session 无限续接 → 历史无界累积，每轮全量重发（input 滚雪球，
# 2026-10-14 实测 39 分钟烧 326k tokens，其中大半是重发的历史与 [no-reply] 空转）。
# 三道护栏：
#   a) 会话滚动：同一对端满 session_max_turns 轮或上一轮 totalTokens ≥ session_max_tokens
#      → 自动开新会话，新会话首条注入滚动告知（共享记忆以黑板为准），会话保持轻量。
#   b) 双端拦截：终止/空类消息不唤起会话；终止/空类回执不发出——ack 环的根治。
#   c) 速率熔断：滑动窗口内同一对端消息超过 rate_max_hops → 静默 + 黑板一次性告警。

def _session_state(state) -> tuple[str | None, int, int]:
    """兼容旧格式 partners.json（纯 session id 字符串）。返回 (sid, turns, last_tokens)。"""
    if isinstance(state, dict):
        return (state.get("session_id"),
                int(state.get("turns", 0) or 0),
                int(state.get("last_tokens", 0) or 0))
    return state, 0, 0


def _blackboard_alert(cfg: dict, partner: str, window: int, max_hops: int) -> None:
    """熔断首次触发时向黑板写一条追加式告警（直写 panel.db，只读其余）。"""
    try:
        db = sqlite3.connect(str(_panel_db(cfg)), timeout=10)
        key = f"admin/{cfg['name']}-rate-guard-{time.strftime('%Y%m%d')}"
        text = (f"\n[{time.strftime('%H:%M:%S')}] ★速率熔断触发：{window // 60} 分钟内收到来自"
                f"「{partner}」的消息超过 {max_hops} 条。窗口滑出前该对端来消息将被静默跳过"
                f"（不唤起会话、不回信）。若非循环事故，请双方放缓协作节奏、改走黑板。"
                f"—— {cfg['name']}@值守分身 自动护栏")
        db.execute(
            "INSERT INTO blackboard(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=value||excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (key, text, time.time(), f"{cfg['name']}@值守分身"))
        db.commit()
        db.close()
        log(f"速率熔断触发（{partner}），已写黑板告警 {key}")
    except Exception as exc:  # noqa: BLE001
        log(f"熔断告警写黑板失败: {exc}")


def _rate_limited(cfg: dict, partner: str) -> bool:
    """滑动窗口熔断：窗口内计数超限返回 True；首次超限写黑板告警一次。"""
    window = int(cfg.get("rate_window_sec", 600))
    max_hops = int(cfg.get("rate_max_hops", 6))
    now = time.time()
    hits = [t for t in _BOUNCE.get(partner, []) if now - t < window]
    hits.append(now)
    _BOUNCE[partner] = hits
    if len(hits) <= max_hops:
        return False
    if len(hits) == max_hops + 1:
        _blackboard_alert(cfg, partner, window, max_hops)
    return True


# ── 会话成本护栏 v2026-10-16d：回合/成本预算（治「慢而蓄意」的跨会话乒乓）──
# 背景（2026-10-16 事故）：两值守 07:42–09:16 以「复核回合」互相回信 18 轮，累计
#   2,376,740 tokens，当日余额消耗 7.01（前一日全天仅 1.92）。护栏 c（频率型
#   6 条/600s）对**慢循环**（5–8 分钟/跳）永不触发；护栏 a 的会话滚动又会把「6 轮」
#   计数随新会话清零 ⇒ 没有任何闸拦得住。
# 与 a/c 的关键区别：**跨会话、跨进程重启持久化**（partners.json 的 guard:<对端> 键），
#   滚动与重启都不会清零，因此慢循环也拦得住。
#   d) 回合预算 loop_max_replies：窗口内同一对端已回 ≥N 条 → 静默（不唤起会话、不回信）
#   e) 成本预算 loop_max_tokens ：窗口内同一对端累计 tokens ≥N → 同上
# 静默期照常消费消息（hub poll 已置 delivered=1），只是**不调用模型**；首次触发写黑板告警。
# 豁免：以「【事故】」开头的消息（看门狗自救求助信的专用首行）不拦且不计数，
#   以免故障期把自救通道一并静默。
_LOOP_EXEMPT_PREFIX = "【事故】"


def _guard_key(partner: str) -> str:
    return f"guard:agent:{partner}"


def _guard_load(cfg: dict, partner: str) -> dict:
    st = load_partners(cfg).get(_guard_key(partner))
    if not isinstance(st, dict):
        return {"replies": [], "tokens": [], "alerted_at": 0.0}
    return {"replies": [float(t) for t in (st.get("replies") or [])],
            "tokens": [[float(x[0]), int(x[1])] for x in (st.get("tokens") or [])
                       if isinstance(x, (list, tuple)) and len(x) == 2],
            "alerted_at": float(st.get("alerted_at") or 0.0)}


def _guard_save(cfg: dict, partner: str, st: dict) -> None:
    data = load_partners(cfg)
    data[_guard_key(partner)] = st
    p = _partners_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _loop_alert(cfg: dict, partner: str, reason: str) -> None:
    """回合/成本预算首次触发时写黑板告警（追加式，直写 panel.db）。"""
    try:
        db = sqlite3.connect(str(_panel_db(cfg)), timeout=10)
        key = f"admin/{cfg['name']}-loop-budget-{time.strftime('%Y%m%d')}"
        text = (f"\n[{time.strftime('%H:%M:%S')}] ★回合/成本预算触发：{reason}。"
                f"窗口滑出前「{partner}」的来消息将被静默（不唤起会话、不回信）。"
                f"需继续协作请改走黑板 admin/* 条目，或等窗口滑出。"
                f"—— {cfg['name']}@值守分身 自动护栏")
        db.execute(
            "INSERT INTO blackboard(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=value||excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (key, text, time.time(), f"{cfg['name']}@值守分身"))
        db.commit()
        db.close()
        log(f"回合/成本预算触发（{partner}）：{reason}；已写黑板告警 {key}")
    except Exception as exc:  # noqa: BLE001
        log(f"回合预算告警写黑板失败: {exc}")


def _loop_budget_exceeded(cfg: dict, partner: str) -> str:
    """回合/成本预算闸。放行返回 ''，拦截返回原因（并保证每窗口只告警一次）。"""
    window = int(cfg.get("loop_window_sec", 3600))
    max_replies = int(cfg.get("loop_max_replies", 3))
    max_tokens = int(cfg.get("loop_max_tokens", 300000))
    if window <= 0 or (max_replies <= 0 and max_tokens <= 0):
        return ""
    now = time.time()
    st = _guard_load(cfg, partner)
    replies = [t for t in st["replies"] if now - t < window]
    toks = [x for x in st["tokens"] if now - x[0] < window]
    used = sum(x[1] for x in toks)
    reason = ""
    if max_replies > 0 and len(replies) >= max_replies:
        reason = (f"{window // 60} 分钟内已向该对端回复 {len(replies)} 条"
                  f"（上限 {max_replies} 条）")
    elif max_tokens > 0 and used >= max_tokens:
        reason = f"{window // 60} 分钟内该对端累计 {used} tokens（上限 {max_tokens}）"
    if reason and now - st["alerted_at"] > window:
        _loop_alert(cfg, partner, reason)
        st["alerted_at"] = now
    st["replies"], st["tokens"] = replies, toks
    _guard_save(cfg, partner, st)
    return reason


def _guard_record(cfg: dict, partner: str, tokens: int) -> None:
    """记一次成功回复（跨会话持久，供回合/成本预算使用）。"""
    now = time.time()
    window = int(cfg.get("loop_window_sec", 3600))
    st = _guard_load(cfg, partner)
    st["replies"] = [t for t in st["replies"] if now - t < window] + [now]
    st["tokens"] = ([x for x in st["tokens"] if now - x[0] < window]
                    + [[now, int(tokens or 0)]])
    _guard_save(cfg, partner, st)





_CHAT_TAG = re.compile(r"^\[chat:([0-9a-f]{16})#(\d+)\]\s*")


def _chat_thread_of(text: str) -> tuple[str, str]:
    """从镜像消息里取出 (thread_id, 去掉前缀的正文)；非镜像消息返回 ("", 原文)。"""
    m = _CHAT_TAG.match(text or "")
    if not m:
        return "", text
    return m.group(1), (text[m.end():] or "").strip()


def _chat_append(cfg: dict, thread_id: str, name: str, text: str) -> None:
    """把回复写回对话线程（与面板 /api/chat 同一张表）。

    失败只记日志：对话回写是「锦上添花」，不能让它拖垮值守主循环。
    去重：线程最后一条若与本条逐字相同（agent 自己已用 chat_send 发过），跳过。
    """
    try:
        db = sqlite3.connect(str(_panel_db(cfg)), timeout=10)
        try:
            last = db.execute(
                "SELECT text FROM chat_posts WHERE thread_id=? ORDER BY id DESC LIMIT 1",
                (thread_id,)).fetchone()
            if last and (last[0] or "").strip() == text.strip():
                return
            db.execute(
                "INSERT INTO chat_posts (thread_id, ts, from_id, from_name, role, text, meta)"
                " VALUES (?,?,?,?,?,?,?)",
                (thread_id, time.time(), f"agent:{name}", name, "assistant",
                 text[:4000],
                 json.dumps({"mode": "worker", "via": "agent_messages"},
                            ensure_ascii=False)))
            db.execute("UPDATE chat_threads SET last_at=? WHERE id=?",
                       (time.time(), thread_id))
            db.commit()
            log(f"已把回复写回对话线程 {thread_id}")
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        log(f"写回对话线程 {thread_id} 失败: {exc}")


# ── 任务处理 ─────────────────────────────────────────────────────

def handle_message(cfg: dict, msg: dict) -> None:
    frm = msg.get("from") or "?"
    text = (msg.get("text") or "").strip()
    chat_tid, text = _chat_thread_of(text)
    # 护栏 b·接收端：终止/空消息不唤起会话、不回信（队列中的 [no-reply] 引信在此拆除）
    if not text or text.startswith("[no-reply]"):
        log(f"收到 {frm} 的终止/空消息，护栏静默跳过（不唤起会话）")
        return
    # 护栏 c：速率熔断
    if _rate_limited(cfg, frm):
        log(f"收到 {frm} 的消息，处于速率熔断窗口，静默跳过")
        return
    # 护栏 d/e：回合与成本预算（跨会话持久；治「慢而蓄意」的慢循环）
    # 豁免看门狗自救求助信（首行「【事故】」），以免故障期把自救通道静默掉。
    if text.startswith(_LOOP_EXEMPT_PREFIX):
        log(f"收到 {frm} 的事故类消息（自救通道），豁免回合/成本预算")
    else:
        why = _loop_budget_exceeded(cfg, frm)
        if why:
            log(f"收到 {frm} 的消息，回合/成本预算已耗尽（{why}）"
                f"→ 静默：不唤起会话、不回信（消息已消费）")
            return
    log(f"收到 {frm} 的消息（{len(text)} 字）：{text[:80]}")
    partners = load_partners(cfg)
    key = f"agent:{frm}"
    sid, turns, last_tokens = _session_state(partners.get(key))
    # 护栏 f：单轮预算——该对端上一轮回复 tokens 超限 → 静默一轮（事后闸）。
    # 治「单轮爆量」：实测单轮峰值 461k tokens / 14.5 分钟，timeout_sec 只管墙钟。
    # 爆量轮已花的钱追不回，但能拦住「连爆两轮」。告警同窗口去重；豁免事故信。
    turn_max = int(cfg.get("turn_max_tokens", 200000) or 0)
    if turn_max > 0 and last_tokens >= turn_max \
            and not text.startswith(_LOOP_EXEMPT_PREFIX):
        reason = f"上一轮回复耗 {last_tokens} tokens（单轮上限 {turn_max}）"
        st = _guard_load(cfg, frm)
        if time.time() - st.get("alerted_at", 0.0) > 3600:
            _loop_alert(cfg, frm, reason)
            st["alerted_at"] = time.time()
            _guard_save(cfg, frm, st)
        log(f"收到 {frm} 的消息，单轮预算已超（{reason}）"
            f"→ 静默：不唤起会话、不回信（消息已消费）")
        return
    # 护栏 a：会话滚动判定（基于上一轮记录的状态）
    rollover = (int(cfg.get("session_max_turns", 6)) > 0
                and turns >= int(cfg.get("session_max_turns", 6))) \
        or last_tokens >= int(cfg.get("session_max_tokens", 80000))
    prompt = (f"来自「{frm}」的消息（经 mcp-hub 推送）：\n\n{text}\n\n"
              f"请处理并按需回复；若需回信，回复内容会由值守进程转交 {frm}。")
    if rollover:
        sid = None
        prompt = (f"【会话已滚动】与 {frm} 的上一会话因长度上限已归档，本条为新会话首条消息，"
                  f"不携带此前上下文；共享记忆以黑板为准（先读摘要键 admin/open-items-brief，"
                  f"需细节再读 admin/open-items 全文，或运行 "
                  f"python3 ~/dsh-suite/ops/context-restore.py）。请直接处理以下消息：\n\n"
                  + prompt)
    out = run_headless(cfg, prompt, sid)
    if out.get("session_id"):
        save_partner(cfg, key, out["session_id"],
                     turns=1 if rollover else turns + 1,
                     last_tokens=int(out.get("tokens", 0) or 0))
    # 护栏 b·发送端：空/终止类回执不发出（error 时改为合成故障文本送达）
    # ★2026-10-15 修：error 原先整条豁免守卫 —— 实测 error=True 且 reply='' 会发出空串、
    #   reply='[no-reply]' 会把终止标记本身写进队列。改为合成故障文本：既保证故障送达，
    #   又不破坏「永不向队列写入空/终止消息」不变量。
    reply = out["reply"]
    err = out.get("error")
    stripped = reply.strip()
    if err:
        if not stripped or stripped.startswith("[no-reply]"):
            reply = f"[error] {cfg['name']} 执行失败：{err}"
    elif (not stripped
          or stripped.startswith("[no-reply]")
          or stripped == f"（{cfg['name']}无输出）"):
        log(f"回复为空/终止标记，护栏不发送（避免 ack 环），已记录本轮 turns/last_tokens")
        return
    res = api(cfg, "/agent/message", {"to_agent": frm, "text": reply,
                                      "task_id": msg.get("task_id") or ""})
    if res.get("ok"):
        _guard_record(cfg, frm, int(out.get("tokens", 0) or 0))
        log(f"已回复 {frm}（{len(reply)} 字, {'滚动后新会话' if rollover else f'第 {turns + 1} 轮'}, "
            f"tokens={out['tokens']}）")
        if chat_tid:
            _chat_append(cfg, chat_tid, cfg["name"], reply)
    else:
        log(f"回复 {frm} 失败: {res.get('error')}")



def handle_subtask(cfg: dict, sub: dict) -> None:
    sid = sub.get("id")
    title = sub.get("title") or sub.get("prompt") or ""
    log(f"发现子任务 {sid}：{title[:60]}")
    claim = api(cfg, "/agent/claim", {"subtask_id": sid})
    if "subtask" not in claim:
        log(f"认领失败: {claim.get('error')}")
        return
    sub = claim["subtask"]
    partners = load_partners(cfg)
    sess = partners.get(f"subtask:{sid}")
    prompt = (f"【mcp-hub 子任务 {sid}】\n标题：{sub.get('title')}\n"
              f"能力要求：{sub.get('capability') or '不限'}\n\n{sub.get('prompt') or ''}\n\n"
              f"请直接执行并把结论/产物整理成回传结果。")
    out = run_headless(cfg, prompt, sess)
    if out.get("session_id"):
        save_partner(cfg, f"subtask:{sid}", out["session_id"])
    if out.get("error"):
        api(cfg, "/agent/fail", {"subtask_id": sid, "error": out["reply"][:500]})
        log(f"子任务 {sid} 标记失败")
        return
    res = api(cfg, "/agent/complete", {"subtask_id": sid, "result": out["reply"],
                                       "tokens_used": out["tokens"]})
    if res.get("ok"):
        log(f"子任务 {sid} 已完成（tokens={out['tokens']}）")
    else:
        log(f"子任务 {sid} 回传失败: {res.get('error')}")


def _handle_signal(signum, _frame):  # noqa: ANN001
    global _stop
    _stop = True
    log(f"收到信号 {signum}，准备退出")


def acquire_lock(path: Path = LOCK_PATH) -> bool:
    """单例锁：已有存活实例则返回 False。"""
    if path.exists():
        try:
            pid = int(path.read_text().strip())
            os.kill(pid, 0)  # 探测存活：不发送信号，仅检查权限/存在
            return False
        except ProcessLookupError:
            pass  # 陈旧 pid，继续抢占
        except (ValueError, PermissionError):
            return False  # pid 不可读或属他人进程（无权探测）→ 视为在跑
    path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock(path: Path = LOCK_PATH) -> None:
    try:
        if path.exists() and path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if not acquire_lock():
        log(f"已有伙伴值守实例在运行（锁 {LOCK_PATH}），本进程退出")
        sys.exit(0)
    cfg = _load_config()
    if not cfg.get("token"):
        try:
            register(cfg)
        except Exception as exc:  # noqa: BLE001
            log(f"注册失败: {exc}")
            sys.exit(1)
    interval = max(5, int(cfg.get("poll_interval", 15)))
    log(f"伙伴值守分身 v{VERSION} 已启动｜hub={cfg['hub']}｜轮询 {interval}s｜"
        f"DSH_HOME={cfg.get('dsh_home')}｜护栏:滚动≤{cfg.get('session_max_turns', 6)}轮/"
        f"{cfg.get('session_max_tokens', 80000)}tok+双端拦截+熔断{cfg.get('rate_max_hops', 6)}跳/"
        f"{int(cfg.get('rate_window_sec', 600)) // 60}分"
        f"+回合≤{cfg.get('loop_max_replies', 3)}条/"
        f"{int(cfg.get('loop_window_sec', 3600)) // 60}分"
        f"+成本≤{cfg.get('loop_max_tokens', 300000)}tok")

    while not _stop:
        try:
            out = api(cfg, "/agent/poll")
            if out.get("error"):
                log(f"轮询失败: {out['error']}")
                time.sleep(interval)
                continue
            for msg in out.get("messages", []):
                handle_message(cfg, msg)
            for sub in out.get("subtasks", []):
                handle_subtask(cfg, sub)
        except Exception as exc:  # noqa: BLE001
            log(f"处理异常: {exc}")
        if _stop:
            break
        time.sleep(interval)
    release_lock()
    log("已退出")


if __name__ == "__main__":
    main()
