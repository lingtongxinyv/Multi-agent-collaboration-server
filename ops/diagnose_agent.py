#!/usr/bin/env python3
"""铃枢 BellHub 任务链路诊断器 —— 分清「额度用尽」与「其他故障」。

当用户任务下发后「agent 无回应」或「回复异常信息」时，本工具按互相独立的
证据链逐层排查，最后给出唯一结论：

  1) 面板配额（我们自己的额度）：日 token / 并发 / 磁盘
  2) 模型侧额度（DeepSeek 账户）：余额接口 + Key 有效性
  3) 执行体状态：设备在线与否、指令 pending/sent 超时、最近回传结果
  4) 错误文本分类：把 DSH / API / 设备端的原始报错映射到具体病因
  5) 服务与代理链路：五服务状态 + nginx 上游超时

用法：
  python3 ops/diagnose_agent.py                    # 全量诊断
  python3 ops/diagnose_agent.py --user mjb         # 聚焦某个用户
  python3 ops/diagnose_agent.py --text "报错原文"   # 只做错误文本分类
  python3 ops/diagnose_agent.py --json             # 机读输出

只读工具：不写数据库、不改任何配置；DeepSeek 余额接口是只读 GET。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SUITE = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("DSH_SUITE_DB", SUITE / "data" / "panel.db"))
DEVICE_ONLINE_SEC = 90          # 与面板判定一致：90 秒内有心跳算在线
CMD_STUCK_SEC = 180             # 指令 sent 超过该秒数仍未回传 → 卡住
BALANCE_URL = "https://api.deepseek.com/user/balance"

# ── 病因分类表：正则 → (病因码, 人话结论, 建议动作) ────────────────────
RULES: list[tuple[str, str, str, str]] = [
    # —— 额度类（我们自己的面板配额）——
    (r"今日 token 配额已用完", "PANEL_QUOTA_TOKENS", "用户的面板日 token 配额已用尽", "管理端「账号」页调高 quota_tokens_daily，或等次日重置"),
    (r"本次请求超出剩余配额", "PANEL_QUOTA_TOKENS", "本次请求将超出剩余配额", "调高配额或缩短任务"),
    (r"并发会话已达上限", "PANEL_QUOTA_CONCURRENT", "并发会话数触顶", "等运行中会话收尾，或调高 quota_concurrent"),
    (r"磁盘配额已用完", "PANEL_QUOTA_DISK", "沙箱磁盘配额用尽", "清理「我的文件」或调高 quota_disk_mb"),
    # —— 额度类（DeepSeek 账户侧）——
    (r"Insufficient Balance|insufficient_quota|余额不足|insufficient balance", "MODEL_BALANCE", "DeepSeek 账户余额不足", "给 API Key 充值（platform.deepseek.com）后重试"),
    (r"\b402\b", "MODEL_BALANCE", "模型侧返回 402（通常为余额不足）", "查 DeepSeek 账户余额/账单"),
    (r"429|rate.?limit|Too Many Requests", "MODEL_RATELIMIT", "模型侧限流（QPS/并发超限，非余额问题）", "降低并发或稍后重试"),
    # —— 凭据 ——
    (r"401|AUTH|Invalid API key|api key is invalid|Unauthorized", "MODEL_AUTH", "API Key 无效/被吊销", "管理端「站娘对话」重设 key"),
    (r"MISSING_CREDENTIAL|尚未配置站娘 API Key", "NO_KEY", "尚未配置站娘 API Key", "管理端「站娘对话」页设置 key"),
    # —— 执行体（设备端 handler）——
    (r"expected str, bytes or os\.PathLike", "DEVICE_HANDLER_PARAM", "设备端 handler 参数类型错误（代码 bug，与额度无关）", "把设备上的 device_agent.py 更新为最新版后重启设备端 agent"),
    (r"handler 不存在", "DEVICE_HANDLER_MISSING", "config.json 指定的 handler 文件不存在", "检查设备端 config.json 的 handler 字段与文件是否同目录"),
    (r"handler 异常|handler 无输出", "DEVICE_HANDLER", "设备端 handler 执行异常", "看设备端终端输出定位 handler 内部报错"),
    (r"config\.json 的 handler 字段必须是字符串", "DEVICE_CONFIG", "设备端 config.json 的 handler 值类型非法", "把 handler 改成 \"device_handler.py\" 或 \"shell_handler.py\""),
    # —— 权限 / 环境 ——
    (r"EACCES|permission denied", "PERMISSION", "文件权限不足（常见：属主被 root 污染）", "chown -R agent:agent 对应路径后重试"),
    (r"failed to observe session", "DSH_SESSION_STORE", "DSH 会话库不可读（常见：root 属主污染 ~/.dsh-headless）", "chown -R agent:agent ~/.dsh-headless"),
    (r"datatype mismatch", "DB_SCHEMA", "SQLite 类型不匹配（代码 bug）", "查 panel journal 定位调用点"),
    # —— 网络 / 链路 ——
    (r"network error|ETIMEDOUT|ECONNRESET|ENOTFOUND|getaddrinfo|fetch failed|Network is unreachable", "NETWORK", "网络中断/域名解析失败", "检查本机网络与 cpolar 隧道状态"),
    (r"upstream timed out", "PROXY_TIMEOUT", "反向代理按空闲超时切断了长连接", "检查 nginx location / 的 proxy_read_timeout / proxy_buffering"),
    (r"站娘执行超时", "TIMEOUT", "站娘执行超过 900 秒上限", "把任务拆小，或延长 assistant.TIMEOUT_SEC"),
    # —— 设备离线 ——
    (r"设备不存在|device not found", "DEVICE_NOT_FOUND", "设备不存在或不属于该用户", "核对设备名与归属"),
]

VERDICT_ADVICE = {
    "PANEL_QUOTA_TOKENS": "这是**我们面板的配额**用尽，去管理端「账号」页调整额度即可；与 DeepSeek 账户余额无关。",
    "MODEL_BALANCE": "这是**模型账户余额**问题，需要给 API Key 充值；面板配额再高也无效。",
    "MODEL_AUTH": "Key 本身失效，属于凭据问题，不是额度问题。",
    "NO_KEY": "根本没配 Key，属于配置缺失。",
    "DEVICE_HANDLER_PARAM": "设备端 Agent 代码 bug，与任何额度无关；更新设备上的 device_agent.py 即可。",
    "PERMISSION": "属主/权限问题，通常在运维改动后出现。",
    "NETWORK": "网络链路问题，额度与代码都正常。",
}


# ────────────────────────────── 工具 ──────────────────────────────
def db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"找不到数据库：{DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def ts(v) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(v)).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "-"


def head(title: str) -> None:
    print(f"\n【{title}】")


def classify(text: str) -> list[tuple[str, str, str]]:
    """把一段错误文本映射成若干 (病因码, 结论, 建议)。"""
    if not text:
        return []
    hits = []
    for pattern, code, why, fix in RULES:
        if re.search(pattern, text, re.IGNORECASE):
            hits.append((code, why, fix))
    # 命中更具体的同族病因时，丢弃泛化条目（如 DEVICE_HANDLER_PARAM 覆盖 DEVICE_HANDLER）
    if {c for c, _, _ in hits if c != "DEVICE_HANDLER"}:
        hits = [h for h in hits if h[0] != "DEVICE_HANDLER"]
    return hits


# ─────────────────── 1. 面板配额（我们自己的额度） ───────────────────
def check_panel_quota(conn: sqlite3.Connection, user: str | None) -> list[dict]:
    sql = ("SELECT a.username, a.role, a.quota_tokens_daily, a.used_tokens, a.used_date,"
           " a.quota_concurrent, a.quota_disk_mb, a.used_disk_mb, a.disabled,"
           " (SELECT COUNT(*) FROM sessions s WHERE s.user_id=a.id AND s.status='running') AS running"
           " FROM accounts a")
    params: tuple = ()
    if user:
        sql += " WHERE a.username=?"
        params = (user,)
    rows = [dict(r) for r in conn.execute(sql, params)]
    head("1. 面板配额（铃枢自己的额度）")
    any_hit = False
    for r in rows:
        q = r["quota_tokens_daily"] or 0
        pct = (r["used_tokens"] / q * 100) if q else 0
        flags = []
        if r["disabled"]:
            flags.append("账号被禁用")
        if q and r["used_tokens"] >= q:
            flags.append("★日配额已用尽"); any_hit = True
        if r["running"] >= (r["quota_concurrent"] or 5):
            flags.append("★并发触顶"); any_hit = True
        if r["used_disk_mb"] >= (r["quota_disk_mb"] or 0):
            flags.append("★磁盘配额用尽"); any_hit = True
        print(f"   {r['username']:<6} {r['role']:<5} 今日 {r['used_tokens']:>10,}/{q:<12,} ({pct:6.2f}%)"
              f"  并发 {r['running']}/{r['quota_concurrent']}  磁盘 {r['used_disk_mb']:.1f}/{r['quota_disk_mb']}MB"
              f"  用量日期={r['used_date'] or '-'}" + ("   " + "；".join(flags) if flags else ""))
    print(f"   → 面板配额结论：{'❌ 有账号触顶（见 ★）' if any_hit else '✅ 无人触顶，配额不是病因'}")
    return rows


# ──────────────────── 2. 模型侧额度（DeepSeek） ────────────────────
def check_model(conn: sqlite3.Connection, net: bool = True) -> dict:
    head("2. 模型侧额度（DeepSeek 账户，与面板配额完全独立）")
    row = conn.execute("SELECT value FROM settings WHERE key='assistant_api_key'").fetchone()
    key = (row["value"] if row else "") or ""
    if not key:
        print("   ❌ 未配置 assistant_api_key（管理端「站娘对话」页设置）")
        return {"key_ok": False, "available": None}
    print(f"   Key：已配置（{key[:6]}…{key[-4:]}，长度 {len(key)}）" if len(key) > 12 else "   Key：已配置")
    if not re.match(r"^sk-", key):
        print("   ⚠️ Key 格式异常（应以 sk- 开头）")
    if not net:
        return {"key_ok": True, "available": None}
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "20", BALANCE_URL, "-H", f"Authorization: Bearer {key}",
             "-w", "\n__HTTP__%{http_code}"],
            capture_output=True, text=True, timeout=30).stdout
        body, _, code = out.rpartition("__HTTP__")
        code = code.strip()
        data = json.loads(body) if body.strip().startswith("{") else {}
        if code == "200":
            infos = data.get("balance_infos") or [{}]
            bal = ", ".join(f"{i.get('total_balance')} {i.get('currency')}" for i in infos)
            avail = data.get("is_available")
            print(f"   余额接口：HTTP 200  is_available={avail}  余额={bal}")
            print(f"   → 模型额度结论：{'✅ 账户可用、余额充足，额度不是病因' if avail else '❌ 账户不可用（余额不足或被停用）'}")
            return {"key_ok": True, "available": bool(avail), "balance": bal}
        if code == "401":
            print(f"   ❌ 余额接口 HTTP 401：Key 无效/被吊销")
            return {"key_ok": False, "available": False}
        print(f"   ⚠️ 余额接口 HTTP {code}：{body.strip()[:200] or '（无响应体）'}")
        return {"key_ok": True, "available": None}
    except Exception as exc:
        print(f"   ⚠️ 无法访问余额接口（{type(exc).__name__}: {exc}）—— 本机网络问题，不代表余额有问题")
        return {"key_ok": True, "available": None}


# ───────────────────── 3. 执行体：设备 / Worker ─────────────────────
def check_devices(conn: sqlite3.Connection, user: str | None) -> list[dict]:
    head("3. 执行体状态（设备 Agent / Worker）")
    now = time.time()
    sql = ("SELECT d.id, d.name, d.status, d.last_seen, a.username FROM devices d"
           " JOIN accounts a ON a.id=d.user_id")
    params: tuple = ()
    if user:
        sql += " WHERE a.username=?"
        params = (user,)
    devs = [dict(r) for r in conn.execute(sql, params)]
    if not devs:
        print("   （没有注册任何设备）")
    problems = []
    for d in devs:
        age = now - (d["last_seen"] or 0)
        online = age < DEVICE_ONLINE_SEC
        print(f"   设备 {d['name']:<14} 用户={d['username']:<6} {'🟢在线' if online else '🔴离线'}"
              f"  最后心跳 {age:.0f}s 前")
        if not online:
            problems.append((d["name"], "DEVICE_OFFLINE", f"设备 {d['name']} 已离线 {age/60:.1f} 分钟，指令会一直 pending"))
        rows = [dict(r) for r in conn.execute(
            "SELECT id, text, status, result, created_at, finished_at FROM device_commands"
            " WHERE device_id=? ORDER BY id DESC LIMIT 5", (d["id"],))]
        for idx, c in enumerate(rows):
            age_c = now - (c["created_at"] or now)
            note = ""
            if c["status"] == "pending" and age_c > CMD_STUCK_SEC:
                note = f"  ← ★pending {age_c/60:.1f} 分钟未取走（设备没在轮询/离线）"
                problems.append((d["name"], "DEVICE_NOT_POLLING", "指令长时间 pending，设备端 agent 未在拉取"))
            elif c["status"] == "sent" and age_c > CMD_STUCK_SEC:
                note = f"  ← ★sent {age_c/60:.1f} 分钟未回传（handler 卡住或设备掉线）"
                problems.append((d["name"], "DEVICE_STUCK", "指令已取走但长时间不回传"))
            cls = classify(c["result"] or "")
            cls_txt = f"  → 分类：{cls[0][0]}（{cls[0][1]}）" if cls else ""
            print(f"     #{c['id']:<4} {c['status']:<7} {ts(c['created_at'])} {(c['text'] or '')[:40]!r}{note}")
            if c["result"]:
                print(f"           回传：{(c['result'] or '')[:110]!r}{cls_txt}")
            if cls and c["status"] == "failed":
                if idx == 0:
                    # 只有「最新一条」失败才算当前故障
                    problems.append((d["name"], cls[0][0], f"最新指令 #{c['id']} 回传：{(c['result'] or '')[:80]}"))
                else:
                    newer = rows[0]
                    print(f"           ↑ 历史失败；该设备最新指令 #{newer['id']} 状态={newer['status']}"
                          f"（{'已恢复，不计入本次故障' if newer['status'] == 'done' else '仍未恢复'}）")
    ws = [dict(r) for r in conn.execute("SELECT * FROM workers")]
    if ws:
        for w in ws:
            age = now - (w["last_seen"] or 0)
            print(f"   Worker {w['name']:<16} status={w['status']:<8} approved={w['approved']}"
                  f" user_available={w['user_available']} 最后心跳 {age:.0f}s 前")
    else:
        print("   （没有注册任何 Worker）")
    return problems


# ───────────────── 4. 最近失败会话 + 错误文本分类 ─────────────────
def check_sessions(conn: sqlite3.Connection, user: str | None, sid: str | None) -> list[dict]:
    head("4. 最近会话失败与错误分类")
    sql = ("SELECT s.id, s.title, s.status, s.tokens_used, s.updated_at, s.dsh_session_id, a.username"
           " FROM sessions s JOIN accounts a ON a.id=s.user_id")
    where, params = [], []
    if user:
        where.append("a.username=?"); params.append(user)
    if sid:
        where.append("s.id=?"); params.append(sid)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.updated_at DESC LIMIT 12"
    rows = [dict(r) for r in conn.execute(sql, tuple(params))]
    problems = []
    for r in rows:
        flag = "❌" if r["status"] == "failed" else ("⏳" if r["status"] == "running" else "✅")
        print(f"   {flag} {r['username']:<6} {r['status']:<7} {ts(r['updated_at'])} tokens={r['tokens_used']:>9,}"
              f"  {r['title'][:34]!r}")
        if r["status"] == "failed":
            problems.append((r["id"], "SESSION_FAILED", r["title"][:60]))
        # 该会话最后一条消息里的中断标记（main.py 会把已生成内容+标记落库）
        m = conn.execute("SELECT text FROM chat_messages WHERE session_id=? AND role='assistant'"
                         " ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        if m and "本轮被中断" in (m["text"] or ""):
            tail = m["text"].split("本轮被中断")[-1][:60]
            print(f"        最后一条为中断落库的残文（…{tail}）")
    log_rows = [dict(r) for r in conn.execute(
        "SELECT ts, action, detail FROM audit_log WHERE action LIKE '%error%' OR action LIKE 'session.failed%'"
        " ORDER BY id DESC LIMIT 8")]
    if log_rows:
        print("   审计中的失败记录：")
        for r in log_rows:
            print(f"     {ts(r['ts'])} {r['action']}: {(r['detail'] or '')[:120]}")
            for code, why, fix in classify(r["detail"] or ""):
                problems.append(("audit", code, f"{why}｜建议：{fix}"))
    return problems


# ─────────────────── 5. 服务与代理链路 ───────────────────
def check_services() -> list[dict]:
    head("5. 服务与代理链路")
    problems = []
    for svc in ["dsh-panel", "dsh-hub", "dsh-admin", "nginx", "cpolar-dsh"]:
        state = subprocess.run(["systemctl", "is-active", f"{svc}.service"],
                               capture_output=True, text=True).stdout.strip()
        print(f"   {svc:<11} {state}")
        if state != "active":
            problems.append((svc, "SERVICE_DOWN", f"{svc} 非 active"))
    try:
        err = Path("/var/log/nginx/error.log")
        counts = {"upstream timed out": 0, "prematurely closed": 0, "connect() failed": 0}
        if err.exists():
            for line in err.read_text(errors="replace").splitlines()[-200:]:
                for k in counts:
                    if k in line:
                        counts[k] += 1
        print(f"   nginx error.log（当前文件）: upstream timed out={counts['upstream timed out']}"
              f"  prematurely closed={counts['prematurely closed']}"
              f"  connect() failed={counts['connect() failed']}")
        print("     （服务重启后的 connect() failed / prematurely closed 属正常瞬断；"
              "只有 upstream timed out 才是代理按空闲超时切断长连接）")
        if counts["upstream timed out"]:
            problems.append(("nginx", "PROXY_TIMEOUT",
                             f"error.log 出现 {counts['upstream timed out']} 条 upstream timed out"))
    except Exception as exc:
        print(f"   （读 nginx 日志失败：{exc}）")
    return problems


# ─────────────────────────── 主流程 ───────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="铃枢 BellHub 任务链路诊断")
    ap.add_argument("--user", help="只看某个用户")
    ap.add_argument("--session", help="只看某个会话 id")
    ap.add_argument("--text", help="只对一段错误文本做分类")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--no-net", action="store_true", help="跳过 DeepSeek 余额查询")
    args = ap.parse_args()

    if args.text:
        hits = classify(args.text)
        if args.json:
            print(json.dumps([{"cause": c, "why": w, "fix": f} for c, w, f in hits],
                             ensure_ascii=False, indent=2))
        elif hits:
            for c, w, f in hits:
                print(f"病因码：{c}\n结论：{w}\n建议：{f}\n")
        else:
            print("未能识别该文本 → 病因码 UNKNOWN\n"
                  "建议：查 journalctl -u dsh-panel 与站娘子进程 stderr，必要时把原文交给助手。")
        return

    started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("═" * 66)
    print(f"铃枢 BellHub 任务链路诊断　{started}"
          + (f"　聚焦用户={args.user}" if args.user else "")
          + (f"　聚焦会话={args.session}" if args.session else ""))
    print("═" * 66)
    conn = db()
    problems: list[dict] = []
    check_panel_quota(conn, args.user)
    model = check_model(conn, net=not args.no_net)
    for p in check_devices(conn, args.user):
        problems.append(p)
    for p in check_sessions(conn, args.user, args.session):
        problems.append(p)
    for p in check_services():
        problems.append(p)

    print("\n" + "═" * 66)
    print("最终判断")
    print("═" * 66)
    if not problems:
        print("  ✅ 未发现异常：配额、模型额度、执行体、服务链路全部正常。")
        print("     若用户仍反馈「无回应」，请把前端展示的原始报错或会话 id 给助手再查。")
        return
    seen = {}
    for who, code, detail in problems:
        seen.setdefault(code, []).append(f"{who}: {detail}")
    for code, details in seen.items():
        print(f"\n  ▸ {code}")
        for d in details[:4]:
            print(f"      - {d}")
        if code in VERDICT_ADVICE:
            print(f"      判断：{VERDICT_ADVICE[code]}")
        for rule in RULES:
            if rule[1] == code:
                print(f"      建议：{rule[3]}")
                break
    if model.get("available") and any(c == "PANEL_QUOTA_TOKENS" for c in seen):
        print("\n  ⚠️ 注意：模型账户余额正常，问题在**面板配额**，别去充值。")
    if model.get("available") is False:
        print("\n  ⚠️ 注意：DeepSeek 账户不可用（余额不足/停用）——这是**所有用户**都会失败的原因，"
              "与单个用户的面板配额无关。")
    if args.json:
        print("\n" + json.dumps({"problems": problems, "model": model}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
