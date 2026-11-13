#!/usr/bin/env python3
"""配额「每日归零」回归测试（2026-10-14，针对"词元额度不会每日刷新"）。

背景：跨天归零原先只写在 check_quota() 里，而它只在开始新一轮对话时调用；
展示路径（/api/auth/me、管理端账号列表）直接读 accounts.used_tokens 原始值，
导致用户次日仍看到昨天的"今日用量"。本测试锁定修复后的行为。

用法（在服务器上，任意目录）：
    python3 ~/dsh-suite/ops/test_quota_rollover.py
它会把线上 panel.db 复制到临时目录后再改，**绝不触碰线上库**。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

SUITE = Path(__file__).resolve().parents[1]        # ~/dsh-suite
LIVE_DB = Path(os.environ.get("DSH_SUITE_DATA", str(SUITE / "data"))) / "panel.db"
BACKEND = SUITE / "panel" / "backend"

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(name)


def main() -> int:
    if not LIVE_DB.exists():
        print(f"找不到 {LIVE_DB}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="quota-test-"))
    shutil.copy2(LIVE_DB, tmp / "panel.db")
    os.environ["DSH_SUITE_DATA"] = str(tmp)          # 必须在 import 之前设置
    sys.path.insert(0, str(BACKEND))
    from app.db import DB, today                     # noqa: E402

    t = today()
    old = "2000-01-01"                               # 任意"非今天"
    db = DB()

    with sqlite3.connect(tmp / "panel.db") as c:
        c.row_factory = sqlite3.Row
        uids = [r["id"] for r in c.execute("SELECT id FROM accounts ORDER BY created_at LIMIT 3")]
        admin_uid = c.execute(
            "SELECT id FROM accounts WHERE role='admin' LIMIT 1").fetchone()["id"]
    while len(uids) < 3:                             # 账号不足时临时造一个
        uids.append(db.create_account(f"quota-t{len(uids)}", "x" * 12, "user")["id"])

    def setu(uid: str, used: int, used_date: str) -> None:
        with sqlite3.connect(tmp / "panel.db") as c:
            c.execute("UPDATE accounts SET used_tokens=?, used_date=? WHERE id=?",
                      (used, used_date, uid))
            c.commit()

    def getu(uid: str) -> tuple[int, str]:
        with sqlite3.connect(tmp / "panel.db") as c:
            r = c.execute("SELECT used_tokens, used_date FROM accounts WHERE id=?", (uid,)).fetchone()
        return int(r[0]), str(r[1])

    quota = int(db.get_account(uids[0])["quota_tokens_daily"])
    print(f"临时库：{tmp}\n今天={t} 测试账号={len(uids)} 个，配额={quota}\n")

    print("[1] 展示路径跨天归零（本次 bug 的核心）")
    setu(uids[0], 99_999, old)
    check("get_account_fresh 跨天 -> 0", db.get_account_fresh(uids[0])["used_tokens"], 0)
    check("且写回日期为今天", getu(uids[0])[1], t)

    print("[2] 当日用量不得被清掉")
    setu(uids[0], 5_000, t)
    check("get_account_fresh 当日 -> 保留", db.get_account_fresh(uids[0])["used_tokens"], 5_000)
    check("日期不变", getu(uids[0])[1], t)

    print("[3] 昨天用满 → 今天必须放行")
    setu(uids[0], quota, old)
    ok, reason = db.check_quota(uids[0], 2_000)
    check("check_quota ok", ok, True)
    check("库内用量已归零", getu(uids[0])[0], 0)

    print("[4] 今天用满 → 仍然拒绝")
    setu(uids[0], quota, t)
    ok, reason = db.check_quota(uids[0], 2_000)
    check("check_quota 拒绝", ok, False)
    check("拒绝文案", "已用完" in reason, True)

    print("[5] 今天余量不足 → 拒绝（防止刚好卡线放行）")
    setu(uids[0], quota - 1_000, t)
    ok, reason = db.check_quota(uids[0], 2_000)
    check("check_quota 拒绝", ok, False)
    check("拒绝文案", reason, "本次请求超出剩余配额")

    print("[6] 余额充足 → 放行")
    setu(uids[0], 1_000, t)
    ok, _ = db.check_quota(uids[0], 2_000)
    check("check_quota ok", ok, True)

    print("[7] add_tokens：跨天首笔按本笔计")
    setu(uids[0], 80_000, old)
    db.add_tokens(uids[0], 1_234)
    check("跨天后用量=本笔", getu(uids[0]), (1_234, t))

    print("[8] add_tokens：当日累加")
    setu(uids[0], 1_000, t)
    db.add_tokens(uids[0], 234)
    check("当日累加", getu(uids[0])[0], 1_234)

    print("[9] 全表扫描（管理端列表 / 定时兜底）")
    for i, uid in enumerate(uids):
        setu(uid, 10_000 * (i + 1), old)
    n = db.rollover_quota()
    check("至少归零 3 个账号", n >= 3, True)
    check("全部清零", [getu(u)[0] for u in uids], [0, 0, 0])

    print("[10] 从未用过的账号（used_date=''）")
    setu(uids[1], 555, "")
    check("空日期 -> 0", db.get_account_fresh(uids[1])["used_tokens"], 0)

    print("[11] 已归零的账号不被重复打扰（幂等）")
    setu(uids[2], 0, t)
    check("第二次调用返回 0 个变更", db.rollover_quota(uids[2]), 0)

    print("[12] 管理员账号同样适用")
    setu(admin_uid, 7_801_832, old)
    check("admin 跨天归零", db.get_account_fresh(admin_uid)["used_tokens"], 0)

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：{FAILED}")
        return 1
    print("✅ 全部通过（线上 panel.db 未被修改，临时库可直接删除）")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
