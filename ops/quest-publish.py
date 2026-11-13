#!/usr/bin/env python3
"""星铃委托板 · 站娘侧发布小工具（助手 / 值守分身 / 外部脚本共用）

把「服务器侧需要管理员处理的事」直接挂到管理员的星铃委托板上，而不是只写在聊天
或黑板里。纯标准库，无第三方依赖；凭据读 data/quest-publish.token（0600）。

用法：
  python3 ~/dsh-suite/ops/quest-publish.py publish \
      --title "J3 · 运行面 A′/B 改造" \
      --detail "需要择时 restart dsh-partner" \
      --category main --rarity epic --reward "断掉 agent→root 链" \
      --ref open-items:j3-runtime

  python3 ~/dsh-suite/ops/quest-publish.py list            # 管理员板上的委托
  python3 ~/dsh-suite/ops/quest-publish.py list --open     # 只看未完成

约定：
  · 同一 (user, ref) 重复发布 = 只更新内容、保留状态与进度 ⇒ 可以每轮重发而
    不会刷屏；ref 建议用稳定键，如 `open-items:j3-runtime`。
  · 不传 ref = 每次都新挂一张（慎用，容易重复）。
  · 本工具只做「挂/看」，不做「接取/完成」——那两步留给管理员自己点，
    这样板上的「已完成」永远代表管理员确认过。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
# 以 root 跑时 Path.home() = /root，会指到影子目录；按候选顺序找真实数据目录
_CANDIDATES = ([Path(os.environ["DSH_SUITE_DATA"])] if os.environ.get("DSH_SUITE_DATA") else [])
_CANDIDATES += [HOME / "dsh-suite/data", Path.home() / "dsh-suite/data"]
DATA_DIR = next((p for p in _CANDIDATES if (p / "quest-publish.token").exists()), _CANDIDATES[0])
TOKEN_PATH = DATA_DIR / "quest-publish.token"
BASE = os.environ.get("PANEL_BASE", "http://127.0.0.1:8010").rstrip("/")

CATS = {"main": "主线", "side": "支线", "bounty": "悬赏"}
RARITY = {"common": "普通", "rare": "稀有", "epic": "史诗", "legend": "传说"}
STATUS = {"open": "待接取", "active": "进行中", "done": "已完成"}


def token() -> str:
    try:
        value = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        sys.exit(f"找不到发布凭据 {TOKEN_PATH}；请先重启面板一次：sudo systemctl restart dsh-panel")
    if not value:
        sys.exit(f"发布凭据为空：{TOKEN_PATH}")
    return value


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-Quest-Token": token()})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        sys.exit(f"面板返回 {exc.code}：{detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"连不上面板 {BASE}：{exc.reason}")


def fmt(q: dict) -> str:
    origin = q.get("origin") or "self"
    who = "管理员自己" if origin == "self" else origin
    return (f"· [{STATUS.get(q['status'], q['status'])}]「{q['title']}」"
            f"（{CATS.get(q['category'], q['category'])}/{RARITY.get(q.get('rarity'), q.get('rarity'))}，"
            f"委托人 {who}，ref={q.get('ref') or '-'}）")


def cmd_publish(args: argparse.Namespace) -> None:
    if not args.title.strip():
        sys.exit("--title 不能为空")
    body = {
        "user": args.user, "title": args.title, "detail": args.detail,
        "category": args.category, "rarity": args.rarity, "target": args.target,
        "reward": args.reward, "origin": args.origin, "ref": args.ref,
    }
    out = call("POST", "/api/quests/publish", body)
    q = out.get("quest") or {}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if out.get("created"):
        print(f"已挂上委托 #{q.get('id')}：{q.get('title')}")
    else:
        print(f"已更新委托 #{q.get('id')}（同 ref 幂等，未重复挂单）：{q.get('title')}")
    print(f"  委托人 {q.get('origin')} · {CATS.get(q.get('category'), q.get('category'))}"
          f" · {STATUS.get(q.get('status'), q.get('status'))}")


def cmd_list(args: argparse.Namespace) -> None:
    out = call("GET", f"/api/quests/board?user={args.user}")
    rows = out.get("quests") or []
    if args.open:
        rows = [r for r in rows if r["status"] != "done"]
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    stats = out.get("stats") or {}
    print(f"{args.user} 的委托板：待接取 {stats.get('open', 0)} / 进行中 "
          f"{stats.get('active', 0)} / 已完成 {stats.get('done', 0)}"
          f" · 站娘挂账 {stats.get('staff_open', 0)}")
    for q in rows:
        print(fmt(q))


def main() -> None:
    parser = argparse.ArgumentParser(description="星铃委托板 · 站娘侧发布工具")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("publish", help="挂一张委托（同 ref 幂等更新）")
    p.add_argument("--title", required=True)
    p.add_argument("--detail", default="")
    p.add_argument("--category", default="side", choices=list(CATS))
    p.add_argument("--rarity", default="common", choices=list(RARITY))
    p.add_argument("--target", type=int, default=1, help="目标步数（1~99）")
    p.add_argument("--reward", default="", help="给管理员的回报/收益，短句")
    p.add_argument("--ref", default="", help="幂等键，如 open-items:j3-runtime")
    p.add_argument("--origin", default="助手", help="委托人署名（默认 助手）")
    p.add_argument("--user", default="admin", help="收件人用户名（默认 admin）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_publish)

    ls = sub.add_parser("list", help="读委托板")
    ls.add_argument("--user", default="admin")
    ls.add_argument("--open", action="store_true", help="只看未完成")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    args = parser.parse_args()
    if not getattr(args, "cmd", None):
        parser.print_help()
        sys.exit(2)
    args.func(args)


if __name__ == "__main__":
    main()
