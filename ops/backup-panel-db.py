#!/usr/bin/env python3
"""panel.db 每日备份（2026-10-17 管理员令 · 伙伴脚本）。

- 用 sqlite3 在线备份 API（.backup），对正在写的库安全，不锁表；
- 落盘 data/backups/panel-YYYYMMDD-HHMMSS.db，保留最近 BACKUP_KEEP 份；
- 校验：备份后 PRAGMA integrity_check 必须 ok，否则保留 .bad 标记并退出 1；
- 结果追加写黑板 ops/backup-log（追加式，防湮灭）。

cron（agent）：
  30 21 * * * ~/.local/bin/uv run --project ~/dsh-suite python ops/backup-panel-db.py
"""
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "panel.db"
BKDIR = DB.parent / "backups"
KEEP = 14
BB_KEY = "ops/backup-log"


def bb_append(key: str, text: str) -> None:
    try:
        conn = sqlite3.connect(DB, timeout=10)
        conn.execute(
            "INSERT INTO blackboard (key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value = value || excluded.value, updated_at = excluded.updated_at",
            (key, text, time.time(), "伙伴"))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001 —— 备份成功但写板失败不应误报
        print(f"[warn] blackboard append failed: {exc}", file=sys.stderr)


def main() -> int:
    BKDIR.mkdir(parents=True, exist_ok=True)
    BKDIR.chmod(0o700)  # 2026-10-19 助手：备份目录含凭据副本，收 700
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BKDIR / f"panel-{stamp}.db"
    src = sqlite3.connect(DB, timeout=30)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)          # 在线备份：源库不必关，读写不阻塞
        dst.commit()
    finally:
        dst.close()
        src.close()
    # 2026-10-19 助手（管理员令 Q4）：凭据库落盘即收 600；并把快照从 WAL 切回 DELETE，
    # 避免留下同权限的 -wal/-shm 副本（v1 曾留 0B wal + 32K shm）。
    dest.chmod(0o600)
    try:
        jm = sqlite3.connect(dest)
        jm.execute("PRAGMA journal_mode=DELETE")
        jm.close()
    except sqlite3.Error as exc:  # noqa: BLE001 —— 权限已收，WAL 切换失败不应误报
        print(f"[warn] journal_mode reset failed: {exc}", file=sys.stderr)
    for sfx in ("-wal", "-shm"):
        leftover = Path(f"{dest}{sfx}")
        if leftover.exists():
            leftover.chmod(0o600)
    check = sqlite3.connect(dest)
    ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    check.close()
    if ok != "ok":
        dest.rename(dest.with_suffix(".db.bad"))
        bb_append(BB_KEY, f"\n[{stamp}] 备份完整性异常（{ok}）→ 已标 .bad\n")
        return 1
    size = dest.stat().st_size
    # 滚动清理：只保留最近 KEEP 份好备份
    olds = sorted(BKDIR.glob("panel-*.db"))
    for old in olds[:-KEEP] if len(olds) > KEEP else []:
        old.unlink()
    bb_append(BB_KEY, f"\n[{stamp}] 备份成功 {dest.name} {size}B integrity=ok "
                      f"(保留最近{KEEP}份，当前{min(len(olds), KEEP)}份)\n")
    print(f"backup ok: {dest} ({size}B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
