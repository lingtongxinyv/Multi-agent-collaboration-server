#!/usr/bin/env python3
"""黑板原子追加工具 —— 双方写板统一入口（防整值覆盖湮灭，根治 2026-10-16 §63-附事故）。

用法：
  python3 ops/bb-append.py <key> <正文文件>            # 把文件内容追加到 key
  python3 ops/bb-append.py <key> -                    # 从 stdin 读正文
  python3 ops/bb-append.py <key> <正文文件> --new      # 新建键（已存在则报错，防误覆盖）
  python3 ops/bb-append.py <key> <正文文件> --author 名字  # 署名（默认 $DSH_BB_AUTHOR 或 admin）

语义与保证：
  · 追加走 SQL 层原子操作 `value = value || ?`，无「读-改-写」竞争窗口；
  · 旧值不存在时按新建处理；旧值为空串一律硬拒（退化成覆盖 = §63-附 事故形态）；
  · 追加后回读断言双条件：尾部 == 追加段 且 长度 = 旧+新；
  · 输出追加前后的字节数与 sha256 前 16 位，供上板留痕（§61 五项同报精神）。
"""
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

# 与面板/Hub 同一份库：优先 DSH_SUITE_DATA，其次 仓库/data
DATA_DIR = Path(os.environ.get("DSH_SUITE_DATA") or (Path(__file__).resolve().parent.parent / "data"))
DB = DATA_DIR / "panel.db"
DEFAULT_AUTHOR = os.environ.get("DSH_BB_AUTHOR") or "admin"


def main() -> int:
    argv = sys.argv[1:]
    is_new = "--new" in argv
    author = DEFAULT_AUTHOR
    if "--author" in argv:
        i = argv.index("--author")
        if i + 1 >= len(argv):
            print("拒绝：--author 后面要跟署名")
            return 2
        author = argv[i + 1].strip() or DEFAULT_AUTHOR
        del argv[i:i + 2]
    args = [a for a in argv if a != "--new"]
    if len(args) != 2:
        print(__doc__)
        return 2
    key, src = args
    text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    if not text.strip():
        print("拒绝：追加正文为空")
        return 2

    db = sqlite3.connect(str(DB), timeout=15)
    row = db.execute("SELECT value FROM blackboard WHERE key=?", (key,)).fetchone()
    old = row[0] if row else None

    if old is None:
        if len(text) > 100_000:
            print(f"拒绝：新值 {len(text)} 字符超上限 100000")
            return 2
        db.execute("INSERT INTO blackboard(key,value,updated_at,updated_by) VALUES(?,?,?,?)",
                   (key, text, time.time(), author))
        db.commit()
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        print(f"新建 {key}: {len(text)}B sha16={h}")
        return 0

    if not old:
        print("拒绝：旧值为空串（覆盖事故形态），请人工处理")
        return 3
    if is_new:
        print(f"拒绝：--new 但 {key} 已存在（{len(old)}B）")
        return 3
    if len(old) + len(text) > 100_000:
        print(f"拒绝：追加后 {len(old) + len(text)} 字符超上限 100000，请先做索引裁剪/归档")
        return 2

    old_len, old_h = len(old), hashlib.sha256(old.encode()).hexdigest()[:16]
    db.execute("UPDATE blackboard SET value = value || ?, updated_at=?, updated_by=? WHERE key=?",
               (text, time.time(), author, key))
    db.commit()
    new = db.execute("SELECT value FROM blackboard WHERE key=?", (key,)).fetchone()[0]
    db.close()

    ok_tail = new.endswith(text)
    ok_len = len(new) == old_len + len(text)
    if not (ok_tail and ok_len):
        print(f"断言失败: tail={ok_tail} len={ok_len}（{len(new)} vs {old_len}+{len(text)}）")
        return 3
    new_h = hashlib.sha256(new.encode()).hexdigest()[:16]
    print(f"追加 {key}: {old_len}({old_h}) + {len(text)} → {len(new)}B sha16={new_h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
