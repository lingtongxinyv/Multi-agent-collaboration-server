#!/usr/bin/env python3
"""伙伴 ↔ 助手 agent 直连 · Trae 侧收发小工具

经 mcp-hub 的 Agent 通道收发消息，与服务器侧值守分身共用同一身份「伙伴」。
纯标准库、无第三方依赖。

用法：
  python3 ~/dsh-suite/ops/agent-mail.py whoami
  python3 ~/dsh-suite/ops/agent-mail.py send 助手 "消息正文"
  python3 ~/dsh-suite/ops/agent-mail.py read                 # 只读列出最近 20 条
  python3 ~/dsh-suite/ops/agent-mail.py read --peer 助手 -n 10
  python3 ~/dsh-suite/ops/agent-mail.py read --id 5          # 打印某条全文

约定：
  · send 走 REST /agent/message（X-Agent-Auth），hub 负责投递到对端收件箱；
  · read 对 panel.db 以 mode=ro 只读，**不消费**消息（不抢服务器值守分身的活）；
  · 对端「助手」的入站由 dsh-resident.service（值守分身）持续轮询并自动回信。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

SUITE = Path.home() / "dsh-suite"
CONFIGS = [SUITE / "workers/partner.config.json", Path("/opt/dsh-partner/config.json")]
DB_PATH = SUITE / "data/panel.db"
HUB = "http://127.0.0.1:8765"


def load_identity() -> tuple[str, str, str]:
    for c in CONFIGS:
        try:
            cfg = json.loads(Path(c).read_text(encoding="utf-8"))
            if cfg.get("token"):
                return cfg["name"], cfg["token"], str(c)
        except Exception:  # noqa: BLE001
            continue
    sys.exit("找不到可用凭据：workers/partner.config.json 或 /opt/dsh-partner/config.json")


def cmd_whoami() -> None:
    name, token, src = load_identity()
    print(f"身份 = {name}（token {token[:6]}…，凭据来源 {src}）\nHub = {HUB}")


def cmd_send(to: str, text: str) -> None:
    if not text.strip():
        sys.exit("拒绝发送空消息（hub 侧不校验 text，空消息会静默入队：#158 事故）")
    name, token, src = load_identity()
    req = urllib.request.Request(f"{HUB}/agent/message",
        data=json.dumps({"to_agent": to, "text": text}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    req.add_header("X-Agent-Auth", quote(name, safe="") + ":" + token)
    try:
        out = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        print(f"已发送 → {to}（id={out.get('message_id')}，身份={name}）")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"发送失败: {exc}")


def cmd_read(peer: str | None, n: int, mid: int | None) -> None:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    if mid is not None:
        row = con.execute(
            "SELECT id,ts,from_agent,to_agent,delivered,text FROM agent_messages WHERE id=?", (mid,)).fetchone()
        if row is None:
            sys.exit(f"无此消息 id={mid}")
        i, t, fr, to, d, txt = row
        print(f"# id={i} [{time.strftime('%m-%d %H:%M', time.localtime(t))}] {fr} → {to} delivered={d}\n{'-' * 60}\n{txt}")
        return
    q = "SELECT id,ts,from_agent,to_agent,delivered,length(text) FROM agent_messages"
    args: tuple = ()
    if peer:
        q += " WHERE from_agent=? OR to_agent=?"
        args = (peer, peer)
    q += " ORDER BY id DESC LIMIT ?"
    args = (*args, n)
    rows = list(con.execute(q, args))
    print(f"最近 {len(rows)} 条 agent 消息（时间倒序）：")
    for i, t, fr, to, d, ln in rows:
        mark = "✓" if d else "·"
        print(f"  {mark} #{i:<3} [{time.strftime('%m-%d %H:%M', time.localtime(t))}] {fr:>4} → {to:<4} {ln}字")


def main() -> None:
    ap = argparse.ArgumentParser(description="伙伴↔助手 agent 直连收发工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    sp = sub.add_parser("send"); sp.add_argument("to"); sp.add_argument("text")
    rp = sub.add_parser("read"); rp.add_argument("--peer"); rp.add_argument("-n", type=int, default=20)
    rp.add_argument("--id", type=int)
    a = ap.parse_args()
    if a.cmd == "whoami":
        cmd_whoami()
    elif a.cmd == "send":
        cmd_send(a.to, a.text)
    else:
        cmd_read(a.peer, a.n, a.id)


if __name__ == "__main__":
    main()
