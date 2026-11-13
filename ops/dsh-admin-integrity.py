#!/usr/bin/env python3
"""dsh-admin 代码指纹巡检（J1 的 C 层）——把「root 运行的代码」做成可证伪清单。

背景：dsh-admin 以 root 跑，而代码目录对 agent 可写；只要改一个文件再重启就会提权。
本脚本每天把该目录的文件哈希与 root 侧基线比对，变了就落一条告警（source=integrity）。

- 只读巡检目录，不改动被巡检内容。
- 基线 /opt/dsh-integrity/baseline.sha256（root:root 0644）——agent 只能读、不能改。
- 默认 check；`--init` / `--accept` 重写基线（需 root）。
- 环境变量仅供测试：DSH_INTEGRITY_ROOT / DSH_INTEGRITY_BASE / DSH_INTEGRITY_DB。
"""
import hashlib
import os
import sqlite3
import sys
import time

ROOT = os.environ.get("DSH_INTEGRITY_ROOT", str(Path.home() / "deepseek-harness"))
BASE = os.environ.get("DSH_INTEGRITY_BASE", "/opt/dsh-integrity/baseline.sha256")
DB = os.environ.get("DSH_INTEGRITY_DB", str(Path.home() / "dsh-suite/data/panel.db"))

SKIP_DIRS = {".git", "node_modules", "dist", "build", "coverage", ".venv",
             "__pycache__", ".turbo", ".next", ".pnpm-store"}
SKIP_EXT = {".log", ".db", ".db-wal", ".db-shm", ".pid", ".sock"}


def manifest(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1] in SKIP_EXT:
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                continue
            out[os.path.relpath(path, root)] = digest
    return out


def load_base():
    base = {}
    with open(BASE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            digest, rel = line.split("  ", 1)
            base[rel] = digest
    return base


def write_base(cur):
    if os.geteuid() != 0:
        print("需要 root 才能写基线", file=sys.stderr)
        return 3
    os.makedirs(os.path.dirname(BASE), exist_ok=True)
    tmp = BASE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for rel in sorted(cur):
            fh.write("%s  %s\n" % (cur[rel], rel))
    os.chmod(tmp, 0o644)
    os.replace(tmp, BASE)
    print("基线已写：%s（%d 个文件）" % (BASE, len(cur)))
    return 0


def raise_alert(title, detail):
    try:
        con = sqlite3.connect(DB, timeout=10)
        con.execute(
            "INSERT INTO alerts(ts, level, source, title, detail) VALUES(?,?,?,?,?)",
            (time.time(), "warn", "integrity", title, detail[:1500]))
        con.commit()
        con.close()
        print("已落告警：", title)
    except Exception as exc:  # noqa: BLE001
        print("告警写入失败：%s" % exc, file=sys.stderr)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    cur = manifest(ROOT)
    if mode in ("--init", "--accept", "init", "accept"):
        return write_base(cur)
    if not os.path.exists(BASE):
        print("NO-BASELINE：先跑 --init（root）")
        return 2

    base = load_base()
    added = sorted(set(cur) - set(base))
    removed = sorted(set(base) - set(cur))
    changed = sorted(r for r in set(cur) & set(base) if cur[r] != base[r])
    if not (added or removed or changed):
        print("OK 文件数=%d 根=%s" % (len(cur), ROOT))
        return 0

    parts = []
    for tag, items in (("新增", added), ("删除", removed), ("改动", changed)):
        if items:
            parts.append("%s %d：%s" % (tag, len(items), ", ".join(items[:8])))
    detail = "\n".join(parts)
    print("MISMATCH 根=%s\n%s" % (ROOT, detail))
    raise_alert("dsh-admin 代码指纹变化（%d 个文件）" % (len(added) + len(removed) + len(changed)),
                "root 运行代码被改动，需人工确认：\n" + detail)
    return 1


if __name__ == "__main__":
    sys.exit(main())
