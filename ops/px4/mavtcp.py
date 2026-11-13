#!/usr/bin/env python3
"""MAVLink TCP endpoint bridge（2026-10-20 修复版）。

把 PX4 SITL 的 UDP 流（默认 127.0.0.1:18570 → :14550）桥成 TCP 服务（默认 :5760），
供 QGC / WSS 中继连接。修复要点：

1. 客户端集合改为 dict[socket, addr]，删除一律走 drop() 且幂等：原实现里
   UDP 转发失败与客户端可读事件可能在同一轮 select 中同时出现，第二次
   clients.remove() 抛 ValueError 直接把进程（容器 PID 1）带走。
2. drop() 对 unregister/close 分别兜底：已关闭 fd 再 unregister 会抛
   KeyError/ValueError，不能让它冒泡。
3. BlockingIOError 不当成断链；只有 EOF/真错误才摘客户端。
4. UDP socket 损坏（O_SError）时每 5s 重建并重新绑定，不靠重启容器。
5. 客户端上限 8，TCP keepalive 打开，SIGTERM/SIGINT 优雅退出。

部署：宿主 /root/PX4/.mavtcp.py（只读挂载进容器 px4-t4 的 /bridge.py），
仓库副本 ops/px4/mavtcp.py 为唯一源；改了要 `docker restart px4-t4` 才生效
（ro bind mount 换 inode 不会自动进容器）。
"""
from __future__ import annotations

import argparse
import errno
import selectors
import signal
import socket
import sys
import time

MAX_CLIENTS = 8
UDP_REBIND_SEC = 5.0
LOG_PREFIX = "bridge"


def log(message: str) -> None:
    print(f"[{time.strftime('%F %T')}] {LOG_PREFIX}: {message}", flush=True)


class Bridge:
    def __init__(self, tcp_port: int, px4_udp: tuple[str, int], listen_udp: int) -> None:
        self.tcp_port = tcp_port
        self.px4_udp = px4_udp
        self.listen_udp = listen_udp
        self.clients: dict[socket.socket, tuple[str, int]] = {}
        self.sel = selectors.DefaultSelector()
        self.udp: socket.socket | None = None
        self.udp_broken_since: float | None = None
        self.running = True
        self.totals = {"udp_in": 0, "udp_out": 0, "tcp_in": 0, "tcp_dropped": 0, "clients_seen": 0}

    # ── socket 管理 ────────────────────────────────────────────────
    def open_udp(self) -> bool:
        """打开（或重建）UDP socket；成功返回 True。"""
        self.close_udp()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.listen_udp))
            sock.setblocking(False)
        except OSError as exc:
            log(f"udp bind :{self.listen_udp} failed: {exc}; retry in {UDP_REBIND_SEC:.0f}s")
            sock.close()
            self.udp_broken_since = self.udp_broken_since or time.monotonic()
            return False
        self.udp = sock
        self.sel.register(sock, selectors.EVENT_READ, ("udp", None))
        if self.udp_broken_since is not None:
            log(f"udp re-bound :{self.listen_udp} after {time.monotonic() - self.udp_broken_since:.1f}s")
            self.udp_broken_since = None
        return True

    def close_udp(self) -> None:
        if self.udp is None:
            return
        try:
            self.sel.unregister(self.udp)
        except (KeyError, ValueError, OSError):
            pass
        try:
            self.udp.close()
        except OSError:
            pass
        self.udp = None

    def mark_udp_broken(self, exc: BaseException) -> None:
        log(f"udp socket broken: {exc!r}; will re-open")
        self.udp_broken_since = time.monotonic()
        self.close_udp()

    def drop(self, conn: socket.socket, reason: str) -> None:
        """幂等摘客户端：同一 fd 在 UDP/CLI 两条事件里都可能被摘一次。"""
        addr = self.clients.pop(conn, None)
        if addr is None:
            return
        try:
            self.sel.unregister(conn)
        except (KeyError, ValueError, OSError):
            pass
        try:
            conn.close()
        except OSError:
            pass
        self.totals["tcp_dropped"] += 1
        log(f"client gone {addr} ({reason}) peers={len(self.clients)}")

    def accept(self) -> None:
        try:
            conn, addr = self.srv.accept()
        except OSError as exc:
            log(f"accept failed: {exc!r}")
            return
        if len(self.clients) >= MAX_CLIENTS:
            log(f"reject {addr}: client limit {MAX_CLIENTS} reached")
            try:
                conn.close()
            except OSError:
                pass
            return
        conn.setblocking(False)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        self.clients[conn] = addr
        self.totals["clients_seen"] += 1
        self.sel.register(conn, selectors.EVENT_READ, ("cli", conn))
        log(f"client connected {addr} peers={len(self.clients)}")

    def forward_udp(self) -> None:
        if self.udp is None:
            return
        try:
            data, _src = self.udp.recvfrom(65535)
        except BlockingIOError:
            return
        except OSError as exc:
            self.mark_udp_broken(exc)
            return
        self.totals["udp_in"] += 1
        for conn in list(self.clients):
            try:
                conn.sendall(data)
            except BlockingIOError:
                # 内核发送缓冲满：这条丢给下一个 UDP 包重试，不算断链
                continue
            except OSError as exc:
                self.drop(conn, f"write {exc.errno or exc}")

    def forward_client(self, conn: socket.socket) -> None:
        try:
            data = conn.recv(65535)
        except BlockingIOError:
            return
        except OSError as exc:
            self.drop(conn, f"read {exc.errno or exc}")
            return
        if not data:
            self.drop(conn, "eof")
            return
        if self.udp is None:
            return
        try:
            self.udp.sendto(data, self.px4_udp)
            self.totals["udp_out"] += 1
        except BlockingIOError:
            return
        except OSError as exc:
            # ICMP 端口不可达等：PX4 暂不在线，丢这一包即可，别崩
            if exc.errno != errno.ECONNREFUSED:
                self.mark_udp_broken(exc)
            return
        self.totals["tcp_in"] += 1

    # ── 主循环 ─────────────────────────────────────────────────────
    def run(self) -> int:
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("0.0.0.0", self.tcp_port))
        self.srv.listen(8)
        self.srv.setblocking(False)
        self.sel.register(self.srv, selectors.EVENT_READ, ("srv", None))

        if not self.open_udp():
            log(f"udp :{self.listen_udp} not ready yet; retrying while serving TCP")

        log(
            f"tcp :{self.tcp_port} <-> udp {self.px4_udp[0]}:{self.px4_udp[1]} "
            f"(stream in on :{self.listen_udp}) ready"
        )
        next_udp_retry = time.monotonic()
        while self.running:
            try:
                events = self.sel.select(timeout=1.0)
            except InterruptedError:
                continue
            except OSError as exc:
                log(f"select failed: {exc!r}")
                continue
            for key, _mask in events:
                kind, conn = key.data
                try:
                    if kind == "srv":
                        self.accept()
                    elif kind == "udp":
                        self.forward_udp()
                    elif kind == "cli" and conn is not None:
                        self.forward_client(conn)
                except Exception as exc:  # 单事件失败不能带走进程（历史 ValueError 事故）
                    log(f"event {kind} failed: {exc!r}")
                    if kind == "cli" and conn is not None:
                        self.drop(conn, "event-error")
            if self.udp is None and time.monotonic() >= next_udp_retry:
                next_udp_retry = time.monotonic() + UDP_REBIND_SEC
                if self.open_udp():
                    next_udp_retry = 0.0
        self.shutdown()
        return 0

    def shutdown(self) -> None:
        log(
            "stopping: udp_in={udp_in} udp_out={udp_out} tcp_in={tcp_in} "
            "dropped={tcp_dropped} clients_seen={clients_seen}".format(**self.totals)
        )
        for conn in list(self.clients):
            self.drop(conn, "shutdown")
        self.close_udp()
        try:
            self.sel.unregister(self.srv)
        except (KeyError, ValueError, OSError):
            pass
        try:
            self.srv.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="MAVLink TCP endpoint bridge (SITL)")
    parser.add_argument("--tcp-port", type=int, default=5760)
    parser.add_argument("--px4-udp", default="127.0.0.1:18570")
    parser.add_argument("--listen-udp", type=int, default=14550)
    args = parser.parse_args()
    host, _, port = args.px4_udp.rpartition(":")
    bridge = Bridge(args.tcp_port, (host or "127.0.0.1", int(port)), args.listen_udp)

    def _stop(signum: int, _frame: object) -> None:
        log(f"signal {signum}: stopping")
        bridge.running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return bridge.run()


if __name__ == "__main__":
    sys.exit(main())
