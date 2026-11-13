"""系统监控采集（psutil）+ 阈值告警评估。

后台线程每 60s 采样一次写入 SQLite；同时评估告警规则，触发时写 alerts 表，
若 SMTP 已配置则发邮件。站娘可通过 ops-monitor 插件读取同一份数据。
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

from .db import get_db, now, today

_interval = int(time.monotonic())
SERVICES = ["nginx", "dsh-admin", "dsh-panel", "dsh-hub", "cpolar-dsh"]

# 服务告警防误报（2026-10-15 新增）：开机宽限 + 连续确认
# 背景：#11–#15 五条 critical 全部落在开机后 6–8 秒（monitor 首个采样点早于
#       systemd 激活完成；本靴 09:06:01 报警、09:06:04 两单元才 Started）。
SERVICE_BOOT_GRACE_SEC = 120   # 开机后 N 秒内不判定服务故障
SERVICE_CONFIRM_SAMPLES = 2    # 连续 N 轮采样均为 down 才升级为 critical

# 每日额度归零：记录最后一次执行时的日期，同一天内不重复扫描（2026-10-14 新增）
_last_rollover_day: str | None = None


def _rollover_quota_daily() -> None:
    """兜底把各家账号的跨天用量清零。

    展示路径（/api/auth/me、管理端账号列表）已经会各自归零，这里再做一次是为了
    覆盖"某账号当天完全没登录"的情况：否则它的 used_tokens 会一直停在旧值，
    管理端统计与审计口径长期失真。进程启动后第一轮循环即执行。
    """
    global _last_rollover_day
    day = today()
    if _last_rollover_day == day:
        return
    n = get_db().rollover_quota()
    _last_rollover_day = day
    if n:
        print(f"[monitor] 每日额度归零：{n} 个账号 used_tokens 已清零（{day}）", flush=True)


# ── 用量账本（2026-11-13 加）：面板侧只认 DSH 写的 .dshw-usage.json ──────
# 为什么不从 panel.db 算：admin 的 web 会话不经过面板记账，accounts.used_tokens 长期是 0，
# 用它做成本告警等于永远不响。DSH 自己那本账才是真数。30s 缓存，读不到就明说。
USAGE_FILE = Path(os.environ.get("DSH_USAGE_FILE")
                  or (Path.home() / ".dsh-admin" / ".dshw-usage.json"))
_usage_cache: dict = {"ts": 0.0, "data": None}


def usage_snapshot(force: bool = False) -> dict:
    if (not force and _usage_cache["data"] is not None
            and time.time() - _usage_cache["ts"] < 30):
        return _usage_cache["data"]
    try:
        raw = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        data = {"error": f"读不到用量账本：{exc}", "file": str(USAGE_FILE)}
        _usage_cache.update(ts=time.time(), data=data)
        return data
    day = today()
    ev = [e for e in (raw.get("events") or []) if e.get("day") == day]
    ev.sort(key=lambda e: int(e.get("ts") or 0))
    hist = raw.get("history") or {}
    data = {
        "file": str(USAGE_FILE),
        "cny_today": round(float(raw.get("todayUsage") or 0), 2),
        "tokens_today": int(sum(int(e.get("tokens") or 0) for e in ev)),
        "balance": round(float(raw.get("lastBalance") or 0), 2),
        "events": [{"at": int(int(e.get("ts") or 0) // 1000),
                    "tokens": int(e.get("tokens") or 0),
                    "cny": round(float(e.get("cost") or 0), 3)} for e in ev[-12:]],
        "history": [{"day": str(d), "cny": round(float(v or 0), 2)}
                    for d, v in sorted(hist.items())[-7:]],
    }
    _usage_cache.update(ts=time.time(), data=data)
    return data


def _sample() -> dict:
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    net = psutil.net_io_counters()
    load1, _, _ = psutil.getloadavg()
    return {
        "ts": int(now()),
        "cpu": round(cpu, 1),
        "mem": round(mem, 1),
        "disk": round(disk, 1),
        "net_rx": round(net.bytes_recv / 1024, 1),
        "net_tx": round(net.bytes_sent / 1024, 1),
        "load1": round(load1, 2),
        "cost_today": float(usage_snapshot().get("cny_today") or 0.0),
    }


def service_status() -> list[dict]:
    """检查托管服务是否 active（systemd）。"""
    import subprocess
    result = []
    for name in SERVICES:
        try:
            state = subprocess.run(
                ["systemctl", "is-active", name], capture_output=True, text=True,
                timeout=5).stdout.strip()
        except Exception:
            state = "unknown"
        result.append({"service": name, "active": state == "active", "state": state})
    return result


def _eval_rule(rule: dict, value: float) -> bool:
    op = rule["op"]
    if op == ">":
        return value > rule["threshold"]
    if op == "<":
        return value < rule["threshold"]
    if op == ">=":
        return value >= rule["threshold"]
    if op == "<=":
        return value <= rule["threshold"]
    return False


_METRIC_KEY = {"cpu": "cpu", "mem": "mem", "disk": "disk", "load1": "load1",
               "cost_today": "cost_today"}


def check_alerts(sample: dict) -> None:
    db = get_db()
    for rule in db.list_rules():
        if not rule["enabled"]:
            continue
        key = _METRIC_KEY.get(rule["metric"])
        if key is None:
            continue
        if not _eval_rule(rule, sample[key]):
            continue
        # 冷却 10 分钟，避免告警风暴
        if now() - rule["last_fired"] < 600:
            continue
        title = f"[{rule['metric']}] {sample[key]} {rule['op']} {rule['threshold']}"
        # load 系指标按「每核」读数才有意义：同一条规则在 4 核机与 8 核机上含义不同，
        # 口径写进 detail，避免以后看告警时误判（2026-10-17 管理员令）。
        _scope = (f"（{psutil.cpu_count() or '?'} 核口径，≈{float(sample[key] or 0) / (psutil.cpu_count() or 1):.2f}/核）"
                  if rule["metric"].startswith("load") else "")
        db.raise_alert("critical", "monitor", title,
                       f"指标 {rule['metric']}={sample[key]}，阈值 {rule['op']}{rule['threshold']}{_scope}，"
                       f"持续 {rule['duration_sec']}s 规则 {rule['id']}")
        db.mark_rule_fired(rule["id"], now())
        if rule["notify_email"]:
            notify_email("DSH 服务器告警", f"{title}\n\n时间: {time.strftime('%F %T')}\n来源: monitor")


_SVC_DOWN_STREAK = 0


def _uptime_sec() -> float:
    """开机时长（秒）。读 /proc/uptime，不依赖可选的 psutil。"""
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except Exception:
        return 1e9


def _check_services() -> None:
    global _SVC_DOWN_STREAK
    db = get_db()
    # 开机宽限：systemd 激活期不判定（2026-10-15 修开机竞态误报）
    if _uptime_sec() < SERVICE_BOOT_GRACE_SEC:
        return
    down = [s["service"] for s in service_status() if not s["active"]]
    if not down:
        _SVC_DOWN_STREAK = 0
        return
    # 连续确认：连续 SERVICE_CONFIRM_SAMPLES 轮 down 才算故障
    # （对齐 cpolar 看门狗既有的「连续 2 次」设计）
    _SVC_DOWN_STREAK += 1
    if _SVC_DOWN_STREAK < SERVICE_CONFIRM_SAMPLES:
        return
    last = float(db.get_setting("_last_service_alert_ts", "0") or 0)
    if now() - last > 900:
        db.raise_alert("critical", "service", f"服务异常: {', '.join(down)}",
                       "systemd 服务非 active，请检查 journalctl")
        notify_email("DSH 服务器服务异常", f"以下服务未运行: {', '.join(down)}")
        db.set_setting("_last_service_alert_ts", str(now()))


def notify_email(subject: str, body: str) -> bool:
    """发送告警邮件；未配置 SMTP 时静默跳过。"""
    db = get_db()
    smtp_host = db.get_setting("smtp_host")
    if not smtp_host:
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.header import Header
        port = int(db.get_setting("smtp_port", "465"))
        user = db.get_setting("smtp_user")
        password = db.get_setting("smtp_pass")
        sender = db.get_setting("smtp_from", user)
        to = db.get_setting("smtp_to")
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = sender
        msg["To"] = to
        cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with cls(smtp_host, port, timeout=15) as client:
            if port != 465:
                client.starttls()
            if user and password:
                client.login(user, password)
            client.sendmail(sender, [to], msg.as_string())
        return True
    except Exception:
        db.raise_alert("warn", "email", "告警邮件发送失败", traceback.format_exc()[-1500:])
        return False


HOUSEKEEPING_EVERY = 10        # 每 10 轮（≈10 分钟）做一次家务


def housekeeping() -> dict:
    """长期运行的家务：清过期配对码 + 收回"领了活却再没回报"的僵尸子任务。

    可直接调用（测试用），返回本次处理数量；没有需要处理的东西时不做任何写入。
    """
    db = get_db()
    return {
        "pair_codes_purged": db.purge_pair_codes(),
        "subtasks_reaped": db.reap_stale_subtasks(),
    }


def worker_loop() -> None:
    ticks = 0
    while True:
        try:
            # 跨天自动归零（同一天只跑一次；启动后的第一轮也会执行）
            _rollover_quota_daily()
            if psutil is not None:
                sample = _sample()
                get_db().save_sample(sample)
                check_alerts(sample)
            _check_services()
            ticks += 1
            if ticks % HOUSEKEEPING_EVERY == 0:
                housekeeping()
        except Exception:
            traceback.print_exc()
        time.sleep(60)


def _seed_cost_rule() -> None:
    """首次启动补一条默认成本告警：今日花费 > ¥5（平时 0.1–2）。"""
    db = get_db()
    if db.one("SELECT id FROM alert_rules WHERE metric='cost_today'") is not None:
        return
    with db.tx() as c:
        c.execute("INSERT INTO alert_rules(id,metric,op,threshold,duration_sec,"
                  "enabled,notify_email,last_fired) VALUES(?,?,?,?,?,?,?,?)",
                  ("cost-today", "cost_today", ">", 5.0, 0, 1, 0, 0))
    print("[monitor] 已补默认成本告警：cost_today > ¥5", flush=True)


def start_worker() -> None:
    try:
        _seed_cost_rule()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    threading.Thread(target=worker_loop, name="monitor", daemon=True).start()


def current_metrics() -> dict:
    if psutil is None:
        return {"error": "psutil 未安装"}
    sample = _sample()
    # 进程级
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
            if (info["cpu_percent"] or 0) > 1.0 or (info["memory_percent"] or 0) > 1.0:
                procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: (x["cpu_percent"] or 0), reverse=True)
    sample["top_processes"] = procs[:8]
    sample["services"] = service_status()
    sample["usage"] = usage_snapshot()
    # 沙箱磁盘用量
    sandbox_root = Path.home() / "dsh-users"
    if sandbox_root.exists():
        usage = {}
        for child in sandbox_root.iterdir():
            if child.is_dir():
                usage[child.name] = round(sum(
                    f.stat().st_size for f in child.rglob("*") if f.is_file()) / 1048576, 2)
        sample["sandbox_usage_mb"] = usage
    return sample
