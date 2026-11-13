#!/usr/bin/env python3
"""生成「bellhub-chat 一键接入包」：一个 .py 文件，对方机器上直接跑就能说话。

用法（服务器上，管理员）：
    ./ops/chat-bundle.py --name 伙伴-A
    ./ops/chat-bundle.py --name 伙伴 --base https://agent.example.com
    ./ops/chat-bundle.py --name 伙伴-A --rotate     # 作废旧 token 再发新的

它做三件事：
  1. 在 panel 建一个对话伙伴（POST /api/chat/peers），拿到**一次性** token；
  2. 把 `scripts/bellhub-chat-install.sh` 模板里的 BASE/TOKEN/NAME 填好；
  3. 落盘到 `data/chat-bundles/bellhub-chat-<名字>.sh`（0600），打印用法与 sha256。

token 只写进这个文件、不打印到终端；文件丢了就 --rotate 重来。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "scripts" / "bellhub-chat-client.py"
TOKEN_FILE = REPO / "data" / "mcp_token"
OUT_DIR = REPO / "data" / "chat-bundles"
DEFAULT_BASE = "https://agent.example.com"
LOCAL_API = "http://127.0.0.1:8010"


def die(msg: str) -> None:
    sys.exit(f"❌ {msg}")


def call(path: str, body: dict | None = None, method: str | None = None,
         token: str = "", base: str = LOCAL_API) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}


def admin_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        die(f"读不到管理员密钥：{TOKEN_FILE}")


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 bellhub-chat 一键接入包")
    ap.add_argument("--name", required=True, help="伙伴名（字母数字与 _ . -，≤40）")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"对方要连的地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--rotate", action="store_true", help="同名伙伴已存在时：作废旧凭据再发新的")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    name = args.name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", name):
        die("名字只允许字母数字与 _ . -，最长 40")
    if not TEMPLATE.is_file():
        die(f"模板不存在：{TEMPLATE}")

    tok = admin_token()
    code, peers = call("/api/chat/peers", token=tok)
    if code != 200:
        die(f"取伙伴列表失败（HTTP {code}）：{peers.get('detail')}")

    old = next((p for p in peers.get("peers", []) if p["name"] == name and p["enabled"]), None)
    if old and not args.rotate:
        die(f"伙伴「{name}」已存在（id={old['id']}）。要换新凭据用 --rotate；"
            f"要停用旧的：DELETE /api/chat/peers/{old['id']}")
    if old and args.rotate:
        c, _ = call(f"/api/chat/peers/{old['id']}", method="DELETE", token=tok)
        if c != 200:
            die(f"作废旧凭据失败（HTTP {c}）")
        print(f"已停用旧伙伴 {name}（{old['id']}）")

    code, res = call("/api/chat/peers", {"name": name, "kind": "peer",
                                         "note": "bellhub-chat 一键包"}, token=tok)
    if code != 200:
        die(f"建伙伴失败（HTTP {code}）：{res.get('detail')}")
    token = res["token"]

    text = TEMPLATE.read_text(encoding="utf-8")
    for key, val in (("__BASE__", args.base.rstrip("/")), ("__TOKEN__", token),
                     ("__NAME__", name)):
        if key not in text:
            die(f"模板缺占位符 {key}")
        text = text.replace(key, val)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    out = out_dir / f"bellhub-chat-{safe}.py"
    out.write_text(text, encoding="utf-8")
    out.chmod(0o600)

    # 同时产出一份 MCP 适配器配置：伙伴-A 自带 mcp_adapter，填这个就能直接调 chat_* 工具
    mcp_out = out_dir / f"bellhub-mcp-{safe}.json"
    mcp_out.write_text(json.dumps({
        f"hub-{safe}": {
            "name": f"hub-{safe}",
            "transport": "streamable-http",
            "url": args.base.rstrip("/") + "/mcp-hub/mcp/",
            "headers": {"Authorization": f"Bearer {token}"},
            "enabled": True,
        }
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mcp_out.chmod(0o600)

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"✅ 已生成：{out}")
    print(f"   MCP 配置：{mcp_out}（给 伙伴-A 的 mcp_adapter 用）")
    print(f"   sha256 {digest}")
    print(f"   大小 {out.stat().st_size} 字节（含凭据，权限 0600）")
    print()
    print("把这个文件拷到对方机器，然后（三选一）：")
    print(f"   python3 {out.name}                    # Linux/macOS：直接进聊天模式")
    print(f"   py {out.name}                         # Windows：直接进聊天模式")
    print(f"   python3 {out.name} ask \"在吗\"       # 只问一句")
    print()
    print("   Windows 也可以双击这个 .py（装了 Python 的话）")
    print()
    print("⚠️ 安装包含明文 token：装完删除；要换凭据就 --rotate 重新生成。")


if __name__ == "__main__":
    main()
