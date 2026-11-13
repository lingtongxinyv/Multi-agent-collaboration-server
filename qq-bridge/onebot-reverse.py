#!/usr/bin/env python3
"""OneBot v11 Reverse WS Server —— 让外部 NapCat 主动连过来（只收不回）。

用途：另一台机器上的 NapCat（某个 QQ 小号）通过 `websocketClients` 连到本端点，
把事件推给我们。**这个号归 伙伴-A，服务器只做通道**（管理员 2026-10-23 裁定）：
  · 鉴权：`?access_token=` 或 `Authorization: Bearer`，比对 REVERSE_WS_TOKEN；
  · 收到事件：追加到 data/qq-bridge/reverse-events.jsonl（带连接来源与时间），
    并把 message / message_sent 归一后转给本机桥（127.0.0.1:8770 /publish）→ WS 广播；
  · 收到动作请求（带 echo 的 API 调用）：一律回 {"status":"failed","retcode":1404}，
    **永不代发**——回消息只能由 伙伴-A 自己做；
  · 「只收不回」是终态，不是验收占位。

环境变量（systemd EnvironmentFile = data/qq-bridge/reverse.env）：
  REVERSE_WS_TOKEN  必填
  REVERSE_WS_PORT   默认 3002
  REVERSE_WS_PATH   默认 /onebot-reverse
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from pathlib import Path

import websockets

DATA = Path(os.environ.get("QQ_BRIDGE_DATA", str(Path.home() / "dsh-suite/data/qq-bridge")))
EVENTS = DATA / "reverse-events.jsonl"
TOKEN = os.environ.get("REVERSE_WS_TOKEN", "").strip()
PORT = int(os.environ.get("REVERSE_WS_PORT", "3002"))
PATH = os.environ.get("REVERSE_WS_PATH", "/onebot-reverse")
PUBLISH_URL = os.environ.get("QQ_BRIDGE_PUBLISH", "http://127.0.0.1:8770/publish").strip()
BRIDGE_ENV = Path(os.environ.get("QQ_BRIDGE_ENV", str(Path.home() / "dsh-suite/data/qq-bridge/env")))


def bridge_token() -> str:
    """桥的内部发布 token：优先环境变量，其次读桥自己的 env 文件（同 uid 可读）。"""
    tok = os.environ.get("QQ_BRIDGE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        for line in BRIDGE_ENV.read_text(encoding="utf-8").splitlines():
            key, _, val = line.partition("=")
            if key.strip() == "QQ_BRIDGE_TOKEN":
                return val.strip()
    except OSError:
        pass
    return ""


def text_of(payload: dict) -> str:
    segs = payload.get("message")
    if isinstance(segs, list):
        parts = [str(s.get("data", {}).get("text", "")) for s in segs
                 if isinstance(s, dict) and s.get("type") == "text"]
        text = "".join(parts).strip()
        if text:
            return text
    return str(payload.get("raw_message") or "").strip()


def publish(event: dict) -> None:
    """把事件转给本机桥的 WS 广播（只转发；不建线程、不回 QQ）。"""
    token = bridge_token()
    if not token:
        log("桥 token 缺失：事件只落盘，未广播")
        return
    req = urllib.request.Request(
        PUBLISH_URL,
        data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        log(f"广播失败（事件已落盘）: {exc}")


def log(msg: str) -> None:
    print(f"[onebot-reverse {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check_auth(ws) -> bool:
    if not TOKEN:
        return False
    supplied = ws.request.path
    # websockets 新版把路径放在 ws.request.path；query 需自己解析
    from urllib.parse import parse_qs, urlparse
    u = urlparse(ws.request.path)
    q = parse_qs(u.query)
    tok = (q.get("access_token") or [""])[0]
    if not tok:
        auth = ws.request.headers.get("Authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return tok == TOKEN


def append_event(ws, payload: dict) -> None:
    rec = {"ts": time.time(), "peer": getattr(ws, "remote_address", None), "event": payload}
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"写事件失败: {exc}")


async def handler(ws):
    if not check_auth(ws):
        log(f"鉴权失败: {getattr(ws, 'remote_address', None)}")
        await ws.close(code=4401, reason="bad token")
        return
    log(f"NapCat 接入（{getattr(ws, 'remote_address', None)}）")
    try:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if "echo" in payload and "post_type" not in payload:
                # 对端在等一个动作结果：明确回「能力未开」，别让它挂住
                await ws.send(json.dumps({
                    "status": "failed", "retcode": 1404, "data": None,
                    "echo": payload.get("echo"),
                    "message": "receive-only endpoint: replies belong to the account owner (伙伴-A)",
                }, ensure_ascii=False))
                continue
            if payload.get("post_type") == "meta_event" and payload.get("meta_event_type") == "heartbeat":
                continue          # 心跳不落盘，避免日志膨胀
            append_event(ws, payload)
            ptype = payload.get("post_type")
            if ptype in ("message", "message_sent"):
                text = text_of(payload)[:800]
                if text:
                    sender = payload.get("sender") or {}
                    qq = str(payload.get("user_id") or "")
                    await asyncio.to_thread(publish, {
                        "type": "qq_out" if ptype == "message_sent" else "qq_in",
                        "qq": qq,
                        "name": str(sender.get("card") or sender.get("nickname") or qq),
                        "text": text,
                        "ts": time.time(),
                        "self_id": str(payload.get("self_id") or ""),
                    })
            if ptype == "message":
                log(f"消息事件: {payload.get('user_id')} -> {str(payload.get('raw_message'))[:60]}")
    except websockets.ConnectionClosed:
        pass
    except Exception as exc:  # noqa: BLE001
        log(f"连接异常: {exc}")
    finally:
        log("NapCat 断开")


async def main() -> None:
    if not TOKEN:
        log("⚠️ REVERSE_WS_TOKEN 为空：所有连接都会被拒（4401）")
    async with websockets.serve(handler, "127.0.0.1", PORT):
        log(f"监听 ws://127.0.0.1:{PORT}{PATH}（只收不回：事件转发给桥广播）")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
