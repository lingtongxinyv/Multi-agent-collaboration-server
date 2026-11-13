#!/usr/bin/env python3
"""忆兮知识库去噪：软删除指定来源的陈旧文档。

规则（默认）：source in ('repo','session')
  - repo：历史 repo 扫描留下的 Windows 本机路径（Typings/stubs、安装包 LICENSE/README、
    Electron 运行时的 Python 标准库文本），文件在服务器上不存在，collector 已关闭。
  - session：Trae session_memory 的 JSONL 会话流水，属于历史记录而非可复用知识。
  - memory 源暂不动（含 user_profile / project_memory 等个人资料），待管理员圈定。

安全设计：
- 默认 dry-run，只打印统计与样本，不改库。
- --apply 时才执行：先导出 JSONL 留档，再 status=missing + 清 chunks（软删除，documents 行保留）。
- 不改任何 kb-notes 文件；不动 inbox / kb 源。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

DB = "/opt/kb/data/kb.sqlite3"
ARCHIVE_DIR = "/opt/kb/data/archive"
PAT = re.compile(r"^[A-Za-z]:[\\/]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sources", default="repo,session", help="要清理的来源，逗号分隔")
    ap.add_argument("--archive", default="", help="JSONL 留档路径（--apply 时必填）")
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    wanted = {x.strip() for x in args.sources.split(",") if x.strip()}
    rows = con.execute(
        "SELECT id, source, path, title FROM documents WHERE status='active'"
    ).fetchall()
    targets = [r for r in rows if r["source"] in wanted]

    doc_ids = [r["id"] for r in targets]
    qmarks = ",".join("?" * len(doc_ids)) or "NULL"
    chunk_n, text_bytes = con.execute(
        f"SELECT count(*), coalesce(sum(length(text)),0) FROM chunks WHERE doc_id IN ({qmarks})",
        doc_ids,
    ).fetchone()

    by_source: dict[str, int] = {}
    for r in targets:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1

    print(f"库内 active 文档：{len(rows)}")
    print(f"命中来源 {sorted(wanted)}：{len(targets)} 篇 / {chunk_n} 块 / 文本 {text_bytes/1024/1024:.2f} MiB")
    print("按来源：", json.dumps(by_source, ensure_ascii=False))
    for r in targets[:8]:
        print(f"  - [{r['source']}] {r['path'][:100]}")

    if not args.apply:
        print("\n[dry-run] 未修改。加 --apply --archive <file.jsonl> 执行。")
        return 0

    if not args.archive:
        print("--apply 必须同时给 --archive")
        return 2

    os.makedirs(os.path.dirname(args.archive), exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(args.archive, "w", encoding="utf-8") as fh:
        for r in targets:
            chunks = con.execute(
                "SELECT seq, heading, text FROM chunks WHERE doc_id=? ORDER BY seq", (r["id"],)
            ).fetchall()
            text = "\n".join(c["text"] for c in chunks)
            fh.write(json.dumps({
                "id": r["id"], "source": r["source"], "path": r["path"],
                "title": r["title"], "chunks": len(chunks), "text": text,
            }, ensure_ascii=False) + "\n")
    print(f"留档：{args.archive}（{os.path.getsize(args.archive)/1024/1024:.2f} MiB）")

    now = stamp
    changed = 0
    with con:
        for r in targets:
            con.execute("UPDATE documents SET status='missing', updated_at=? WHERE id=?", (now, r["id"]))
            con.execute("DELETE FROM chunks WHERE doc_id=?", (r["id"],))
            changed += 1
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"已软删除 {changed} 篇；剩余 active：",
          con.execute("SELECT count(*) FROM documents WHERE status='active'").fetchone()[0])
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
