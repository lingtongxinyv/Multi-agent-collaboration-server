#!/usr/bin/env python3
"""每日 22:00 定时关机（2026-10-17 管理员令）· root crontab 调用。

■ 生效件 = /usr/local/sbin/dsh-nightly-shutdown.py（root:root 0755）
  仓库源件 ops/nightly-shutdown.py 只是源；改源件后必须重新 install 到该路径。
■ 取消通道（任一命中即今晚不关机）：
  1. 黑板键 admin/shutdown-hold-YYYYMMDD（任意 agent/管理员可写）
  2. 标记文件 /tmp/dsh-shutdown-hold（touch 即生效，重启消失）
■ 安全规则（2026-10-17 20:25 事故后定稿）：
  **读不到取消状态 ⇒ 不关机**（fail-safe），并写 /var/log/dsh-shutdown.log。
  事故原因：生效件按 __file__ 推断库路径，装到 /usr/local/sbin 后指向了不存在的
  /usr/local/data/panel.db，读失败被当成「无阻止」→ 20:25:40 误关机。
  现在库路径写死为仓库绝对路径，且读失败一律不关机。
■ 2026-11-13 18:46 管理员令停用：root crontab 中调用本脚本的那行已注释（备份 /var/backups/root-crontab-20261113-184627.bak），
  22:00 不再自动关机。恢复=去掉 crontab 注释行行首的 #。
■ 用法：dsh-nightly-shutdown.py [--dry-run]
  --dry-run 只判定与记日志，绝不调用 poweroff（自检专用）。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SUITE = Path.home() / "dsh-suite"
DB = SUITE / "data" / "panel.db"
HOLD_FILE = Path("/tmp/dsh-shutdown-hold")
LOG_FILE = Path("/var/log/dsh-shutdown.log")
BB_HOLD_PREFIX = "admin/shutdown-hold-"
BB_LOG_KEY = "ops/shutdown-log"
OWNER = (1000, 1000)  # 站点属主


def log(msg: str) -> None:
    line = f"[{time.strftime('%F %T')}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _conn() -> sqlite3.Connection:
    # 以读写打开：只读模式在 WAL 库上依赖 -shm 可写，历史上正是它让 reads 静默失败。
    return sqlite3.connect(str(DB), timeout=10)


def _give_back_ownership() -> None:
    """root 写 agent 的库会把 -wal/-shm 建成/改成 root 属主，写完立刻还回去。"""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chown(f"{DB}{suffix}", *OWNER)
        except OSError:
            pass


def bb_append(text: str) -> bool:
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO blackboard (key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value = value || excluded.value, updated_at = excluded.updated_at",
            (BB_LOG_KEY, text, time.time(), "伙伴@nightly-shutdown"))
        conn.commit()
        conn.close()
        _give_back_ownership()
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] 黑板写入失败: {exc}")
        return False


def read_holds(today: str) -> tuple[list[str], bool]:
    """返回 (阻止原因列表, 读取是否成功)。读取失败一律按不关机处理。"""
    holds: list[str] = []
    ok = True
    if HOLD_FILE.exists():
        holds.append(f"标记文件 {HOLD_FILE} 存在")
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT updated_by FROM blackboard WHERE key = ?",
            (f"{BB_HOLD_PREFIX}{today}",)).fetchone()
        conn.close()
        if row:
            holds.append(f"黑板键 {BB_HOLD_PREFIX}{today}（by={row[0]}）")
    except Exception as exc:  # noqa: BLE001
        ok = False
        log(f"[warn] 取消状态读取失败（按不关机处理）: {exc}")
    return holds, ok


def main() -> int:
    dry = "--dry-run" in sys.argv
    today = time.strftime("%Y%m%d")
    stamp = time.strftime("%F %T")
    holds, ok = read_holds(today)

    if holds:
        msg = f"\n[{stamp}] 22:00 关机跳过：{'；'.join(holds)}\n"
        log("关机跳过：" + "；".join(holds))
        if not dry:
            bb_append(msg)
        return 0

    if not ok:
        log("取消状态不可读 ⇒ 本次不关机（fail-safe）")
        if not dry:
            bb_append(f"\n[{stamp}] 22:00 未关机：取消状态不可读（fail-safe）\n")
        return 0

    if dry:
        log("DRY-RUN：无阻止指令，正式运行会执行 systemctl poweroff")
        return 0

    log("22:00 关机执行（无阻止指令）→ systemctl poweroff")
    bb_append(f"\n[{stamp}] 22:00 关机执行（无阻止指令）→ systemctl poweroff\n")
    subprocess.run(["systemctl", "poweroff"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
