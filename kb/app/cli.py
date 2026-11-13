"""命令行入口：python -m app.cli <命令>"""
from __future__ import annotations

import argparse
import json
import sys

from . import config
from .answer import answer as gen_answer
from .embed import Embedder
from .ingest import run_ingest
from .search import Searcher
from .store import Store


def _searcher() -> Searcher:
    cfg = config.load()
    return Searcher(Store(cfg), Embedder(cfg))


def cmd_add_root(args) -> int:
    """把一个目录追加到 repos/notes 源列表，写回 kb_config.json。"""
    from pathlib import Path

    target = Path(args.path)
    if not target.exists():
        print(f"目录不存在：{target}")
        return 1
    path = str(target.resolve()).replace("\\", "/")
    raw = {}
    if config.CONFIG_FILE.exists():
        raw = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    section = raw.setdefault("sources", {}).setdefault(args.kind, {})
    roots = section.setdefault("roots", [])
    if path in roots:
        print(f"已存在，未重复添加：{path}")
        return 0
    roots.append(path)
    config.CONFIG_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已加入 {args.kind}：{path}")
    print("下一步：python -m app.cli ingest --only " + ("repo" if args.kind == "repos" else "notes"))
    return 0


def cmd_ingest(args) -> int:
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    run_ingest(config.load(reload=True), only=only)
    return 0


def cmd_watch(args) -> int:
    """常驻增量同步：固定间隔重扫，内容没变自动跳过。"""
    import time

    cfg = config.load(reload=True)
    interval = max(30, int(args.interval))
    print(f"[watch] 每 {interval}s 扫描一次，Ctrl+C 退出")
    try:
        while True:
            run_ingest(cfg, verbose=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[watch] 已停止")
    return 0


def cmd_backfill(_args) -> int:
    from .ingest import backfill_vectors

    backfill_vectors(config.load(reload=True))
    return 0


def cmd_stats(_args) -> int:
    cfg = config.load()
    store = Store(cfg)
    embedder = Embedder(cfg)
    data = store.stats()
    data["embedding"] = {"model": embedder.model, "available": embedder.available()}
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_search(args) -> int:
    hits = _searcher().search(args.query, args.top_k)
    if not hits:
        print("没有匹配内容。")
        return 1
    for i, r in enumerate(hits, 1):
        print(f"[{i}] {r['path']}:{r['start_line']}-{r['end_line']}  ({r['source']}, score={r['score']})")
        if r["heading"]:
            print(f"    标题：{r['heading']}")
        print("    " + r["text"][:300].replace("\n", "\n    "))
        print()
    return 0


def cmd_ask(args) -> int:
    cfg = config.load()
    hits = _searcher().search(args.query, args.top_k)
    out = gen_answer(args.query, hits, cfg)
    print(out["answer"])
    print("\n---- 出处 ----")
    for i, r in enumerate(out["citations"], 1):
        print(f"[{i}] {r['path']}:{r['start_line']}-{r['end_line']}")
    return 0


def cmd_serve(_args) -> int:
    from .server import serve

    serve()
    return 0


def cmd_mcp(_args) -> int:
    from .mcp_server import main

    main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kb", description="忆兮 · 个人 AI 知识库")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="采集并入库（自动增量）")
    p_ing.add_argument("--only", help="只处理指定源：memory,session,repo,notes,inbox")
    p_ing.set_defaults(func=cmd_ingest)

    p_add = sub.add_parser("add-root", help="往知识库追加一个目录")
    p_add.add_argument("path")
    p_add.add_argument("--kind", choices=["repos", "notes"], default="repos")
    p_add.set_defaults(func=cmd_add_root)

    p_sea = sub.add_parser("search", help="检索原文片段")
    p_sea.add_argument("query")
    p_sea.add_argument("--top-k", type=int, default=8)
    p_sea.set_defaults(func=cmd_search)

    p_ask = sub.add_parser("ask", help="提问并生成回答")
    p_ask.add_argument("query")
    p_ask.add_argument("--top-k", type=int, default=6)
    p_ask.set_defaults(func=cmd_ask)

    p_watch = sub.add_parser("watch", help="常驻增量同步")
    p_watch.add_argument("--interval", type=int, default=600, help="扫描间隔秒数，默认 600")
    p_watch.set_defaults(func=cmd_watch)

    p_bk = sub.add_parser("backfill", help="给缺向量的分块补建向量")
    p_bk.set_defaults(func=cmd_backfill)

    p_sta = sub.add_parser("stats", help="查看库统计")
    p_sta.set_defaults(func=cmd_stats)

    p_srv = sub.add_parser("serve", help="启动网页服务")
    p_srv.set_defaults(func=cmd_serve)

    p_mcp = sub.add_parser("mcp", help="以 MCP stdio 服务启动")
    p_mcp.set_defaults(func=cmd_mcp)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
