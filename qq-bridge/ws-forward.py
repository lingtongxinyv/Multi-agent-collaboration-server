#!/usr/bin/env python3
"""NapCat WS 的本机转发器：127.0.0.1:3001 ↔ 容器 IP:3001。

为什么需要它：
  nginx 的 `/onebot-ws/` 原来直接 proxy 到容器 IP（172.19.0.2）。compose 没映射
  宿主 3001（映射要重建容器，可能掉 QQ 登录），而容器一旦重建 IP 就会变 ⇒ 写死
  IP 的 nginx 会 502。
  这里起一个本机固定端口，**每次新连接时**用 `docker inspect` 解析当前容器 IP
  （缓存 10 秒），所以容器换 IP 也不用改 nginx。

用法（systemd：dsh-napcat-ws-forward.service，User=agent）：
  .venv/bin/python qq-bridge/ws-forward.py
"""

from __future__ import annotations

import asyncio
import subprocess
import time

LISTEN = ("127.0.0.1", 3001)
UPSTREAM_PORT = 3001
CONTAINER = "napcat"
CACHE_SEC = 10.0

_ip_cache: dict = {"ip": "", "at": 0.0}


def log(msg: str) -> None:
    print(f"[ws-forward {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def container_ip() -> str:
    now = time.time()
    if _ip_cache["ip"] and now - _ip_cache["at"] < CACHE_SEC:
        return _ip_cache["ip"]
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", CONTAINER],
            capture_output=True, text=True, timeout=5)
        ip = (out.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        log(f"解析容器 IP 失败: {exc}")
        return _ip_cache["ip"]
    if ip:
        if ip != _ip_cache["ip"]:
            log(f"容器 IP = {ip}")
        _ip_cache.update(ip=ip, at=now)
    return ip


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    ip = container_ip()
    if not ip:
        client_w.close()
        return
    try:
        up_r, up_w = await asyncio.wait_for(
            asyncio.open_connection(ip, UPSTREAM_PORT), timeout=5)
    except Exception as exc:  # noqa: BLE001
        log(f"连 {ip}:{UPSTREAM_PORT} 失败: {exc}")
        client_w.close()
        return
    await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))


async def main() -> None:
    server = await asyncio.start_server(handle, *LISTEN)
    log(f"监听 {LISTEN[0]}:{LISTEN[1]} → {CONTAINER}:{UPSTREAM_PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
