#!/usr/bin/env python3
"""伙伴跨工作区上下文恢复器 —— 从共享黑板拉回记忆。

背景：Trae 侧记忆按「工作区路径哈希」隔离，换工作区后老记忆不会被自动加载。
本脚本把服务器上的共享黑板（panel.db.blackboard）当作唯一权威记忆，
一条命令即可恢复上下文，无需依赖任何单个工作区。

用法：
    python3 ~/dsh-suite/ops/context-restore.py          # 概览：列出全部条目 + 打印核心条目全文
    python3 ~/dsh-suite/ops/context-restore.py -n 5     # 只显示最近 5 条列表
    python3 ~/dsh-suite/ops/context-restore.py --key K  # 打印单条全文
    python3 ~/dsh-suite/ops/context-restore.py --all    # 打印全部条目全文（较长）
"""
from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(os.environ.get("DSH_SUITE_DATA", str(Path.home() / "dsh-suite/data"))) / "panel.db"

# 每次恢复都要通读的「核心条目」：固定键 + 前缀取最新（跨天不再失效）
# 2026-10-15 修：原先把 admin/ops-log-20261014 的日期写死 ⇒ 跨天即失效
# （助手 2026-10-14 指出的第 1 条改进）。且原核心集不含挂账索引，一并纳入。
CORE_FIXED_KEYS = ("admin/partner-session-handoff", "admin/open-items")
CORE_PREFIX_KEYS = ("admin/ops-log-",)


def core_keys(rows) -> list:
    """核心键解析：固定键 + 每个前缀取 updated_at 最新的一条。

    rows = [(key, value, updated_at, updated_by), ...]
    """
    keys = list(CORE_FIXED_KEYS)
    for pref in CORE_PREFIX_KEYS:
        hits = [r for r in rows if r[0].startswith(pref)]
        if hits:
            keys.append(max(hits, key=lambda r: r[2])[0])
    return keys


def ts(x: float) -> str:
    return datetime.datetime.fromtimestamp(x).strftime("%Y-%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser(description="从共享黑板恢复伙伴上下文")
    ap.add_argument("-n", "--list", type=int, default=15, metavar="N",
                    help="列表显示最近 N 条（默认 15；传 0 显示全部）")
    ap.add_argument("--key", help="只打印指定 key 的全文")
    ap.add_argument("--all", action="store_true", help="打印全部条目全文")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: 黑板库不存在: {DB_PATH}", file=sys.stderr)
        return 1

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT key, value, updated_at, updated_by FROM blackboard ORDER BY updated_at DESC").fetchall()

    if args.key:
        hit = [r for r in rows if r[0] == args.key]
        if not hit:
            print(f"（无条目: {args.key}）")
            return 0
        k, v, t, by = hit[0]
        print(f"### {k}   [{ts(t)}]   by={by}\n{'=' * 64}\n{v}\n")
        return 0

    print(f"共享黑板 · {DB_PATH}")
    print(f"共 {len(rows)} 条条目，按更新时间倒序：\n")

    show = rows if args.list == 0 else rows[:args.list]
    by_key = {r[0]: r for r in rows}
    core = core_keys(rows)
    core_set = set(core)
    for k, _v, t, by in show:
        star = "★" if k in core_set else " "
        print(f" {star} [{ts(t)}] by={by:<10} {k}")

    # 概览模式：打印核心条目全文（跳过 --all 已覆盖的情况）
    if not args.all:
        for k in core:
            if k in by_key:
                _k, v, t, by = by_key[k]
                print(f"\n{'─' * 64}\n★ {k}   [{ts(t)}]   by={by}\n{'─' * 64}\n{v}")
    else:
        for k, v, t, by in rows:
            print(f"\n{'─' * 64}\n{k}   [{ts(t)}]   by={by}\n{'─' * 64}\n{v}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
