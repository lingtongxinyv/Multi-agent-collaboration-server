#!/usr/bin/env python3
"""MAVLink WSS 中继：wss://agent.example.com/mavlink  ⇄  127.0.0.1:5760（SITL TCP）。

安全边界（硬编码，不接受配置/环境变量改写）
- 只监听 127.0.0.1:8766：公网面只有 nginx 的 443/80，本进程不开任何外网口。
- 只向后端 127.0.0.1:5760 建连：目标地址是常量，握手后还会用 getpeername 复核。
- 握手必须先过一次性 token（TTL 30 分钟，见 tokens.py）；校验失败直接 401，不给 101。
- 只收二进制帧（MAVLink 字节流）；文本帧、未掩码帧、超大帧一律断链。

运行：`python3 ops/mavlink/relay.py`，由 systemd 单元 dsh-mavlink-relay.service 托管。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import signal
import struct
import sys
import time
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tokens  # noqa: E402  (同目录模块，CLI 与中继共用一份存储逻辑)

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8766
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 5760
ALLOWED_PATH = "/mavlink"

MAX_FRAME = 1 << 20          # 单帧上限 1 MiB（MAVLink 最大帧远小于此）
MAX_CONNECTIONS = 4          # 演示链路够用；超出直接 503，防占满
HEADER_LIMIT = 8192
HANDSHAKE_TIMEOUT = 5.0
PING_INTERVAL = 15.0
PONG_GRACE = 60.0
READ_CHUNK = 16384
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

log = logging.getLogger("mavlink-relay")


class ProtocolError(Exception):
    """对端违反 WebSocket 协议；携带关闭码，便于回帧。"""

    def __init__(self, reason: str, code: int = 1002) -> None:
        super().__init__(reason)
        self.code = code


class Closed(Exception):
    """对端已发关闭帧或连接已断。"""


class KeepaliveTimeout(Exception):
    """客户端在 PONG_GRACE 内没有任何字节；用 4408 回帧，让断开原因可区分。"""

    code = 4408
    reason = "keepalive-timeout"


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def _http_error(code: int, reason: str, body: str) -> bytes:
    return (
        f"HTTP/1.1 {code} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body.encode())}\r\n"
        "Connection: close\r\n"
        "\r\n"
        f"{body}"
    ).encode()


async def _read_http_head(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_TIMEOUT)
    except asyncio.LimitOverrunError as exc:
        raise ProtocolError("header too large", 1009) from exc
    if len(raw) > HEADER_LIMIT:
        raise ProtocolError("header too large", 1009)
    try:
        text = raw.decode("latin-1")
    except UnicodeDecodeError as exc:  # latin-1 不会失败，保留兜底
        raise ProtocolError("bad header encoding") from exc
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        raise ProtocolError("empty request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers


class Peer:
    """一条已升级的 WS 连接；负责帧编解码与统计。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.write_lock = asyncio.Lock()
        self.last_rx = time.monotonic()
        self.ws_bytes_in = 0
        self.ws_bytes_out = 0
        self.tcp_bytes_in = 0
        self.tcp_bytes_out = 0
        self.closing = False

    async def _write_frame(self, opcode: int, payload: bytes) -> None:
        head = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            head.append(length)
        elif length < 65536:
            head.append(126)
            head += struct.pack("!H", length)
        else:
            head.append(127)
            head += struct.pack("!Q", length)
        async with self.write_lock:
            self.writer.write(bytes(head) + payload)
            await self.writer.drain()
            self.ws_bytes_out += length

    async def send_binary(self, payload: bytes) -> None:
        await self._write_frame(0x2, payload)

    async def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        await self._write_frame(opcode, payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closing:
            return
        self.closing = True
        try:
            await self._send_control(0x8, struct.pack("!H", code) + reason.encode()[:120])
        except (ConnectionError, RuntimeError):
            pass
        try:
            self.writer.close()
        except (ConnectionError, RuntimeError):
            pass

    async def recv_message(self) -> bytes:
        """读一条完整二进制消息；控制帧就地处理。"""
        buffer = bytearray()
        while True:
            fin, opcode, payload = await self._read_frame()
            self.last_rx = time.monotonic()
            if opcode == 0x8:
                raise Closed()
            if opcode == 0x9:  # ping → pong
                await self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # pong（last_rx 已刷新）
                continue
            if opcode == 0x1:
                raise ProtocolError("text frame not accepted", 1003)
            if opcode == 0x0:
                if not buffer:
                    raise ProtocolError("unexpected continuation frame")
                buffer += payload
            elif opcode == 0x2:
                if buffer:
                    raise ProtocolError("interleaved data frame")
                buffer = bytearray(payload)
            else:
                raise ProtocolError(f"unsupported opcode {opcode}")
            if len(buffer) > MAX_FRAME:
                raise ProtocolError("message too large", 1009)
            if fin:
                return bytes(buffer)

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        head = await self.reader.readexactly(2)
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        if b0 & 0x70:
            raise ProtocolError("reserved bits set")
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if not b1 & 0x80:
            raise ProtocolError("client frames must be masked")
        if length == 126:
            length = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
        if length > MAX_FRAME:
            raise ProtocolError("frame too large", 1009)
        mask = await self.reader.readexactly(4)
        payload = bytearray(await self.reader.readexactly(length))
        for i in range(length):
            payload[i] ^= mask[i & 3]
        return fin, opcode, bytes(payload)


async def _pump_ws_to_tcp(peer: Peer, tcp_writer: asyncio.StreamWriter) -> None:
    while True:
        message = await peer.recv_message()
        if not message:
            continue
        peer.ws_bytes_in += len(message)
        tcp_writer.write(message)
        peer.tcp_bytes_out += len(message)
        await tcp_writer.drain()


async def _pump_tcp_to_ws(peer: Peer, tcp_reader: asyncio.StreamReader) -> None:
    while True:
        chunk = await tcp_reader.read(READ_CHUNK)
        if not chunk:
            return
        peer.tcp_bytes_in += len(chunk)
        await peer.send_binary(chunk)


async def _keepalive(peer: Peer) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL)
        if time.monotonic() - peer.last_rx > PONG_GRACE:
            log.info("keepalive timeout: no client traffic for %.0fs", PONG_GRACE)
            raise KeepaliveTimeout()
        await peer._send_control(0x9, b"ka")


async def _serve_peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     target_host: str, target_port: int) -> None:
    peer_id = f"{writer.get_extra_info('peername')}"
    started = time.monotonic()
    peer = Peer(reader, writer)
    tcp_writer = None
    reason = "eof"
    try:
        request_line, headers = await _read_http_head(reader)
        method, target, _ = (request_line.split(" ") + ["", "", ""])[:3]
        parts = urlsplit(target)
        if method != "GET":
            writer.write(_http_error(405, "Method Not Allowed", "GET only\n"))
            reason = "method"
            return
        if not headers.get("upgrade", "").lower() == "websocket":
            if parts.path == "/healthz":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nok\n")
                reason = "healthz"
            else:
                writer.write(_http_error(426, "Upgrade Required", "websocket endpoint\n"))
                reason = "no-upgrade"
            return
        if parts.path != ALLOWED_PATH:
            writer.write(_http_error(404, "Not Found", "unknown path\n"))
            reason = "bad-path"
            return
        key = headers.get("sec-websocket-key", "")
        version = headers.get("sec-websocket-version", "")
        if version != "13" or len(key) < 16:
            writer.write(_http_error(400, "Bad Request", "bad websocket handshake\n"))
            reason = "bad-handshake"
            return

        token = headers.get("x-mavlink-token") or (parse_qs(parts.query).get("token") or [""])[0]
        ok, why = tokens.check(token)
        if not ok:
            log.warning("token rejected (%s) peer=%s", why, peer_id)
            writer.write(_http_error(401, "Unauthorized", f"token {why}\n"))
            reason = f"token-{why}"
            return

        # 后端连接必须先建立：后端不可用时不要给客户端 101。
        try:
            tcp_reader, tcp_writer = await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port), 5.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            log.error("backend %s:%s unavailable: %r", target_host, target_port, exc)
            writer.write(_http_error(502, "Bad Gateway", "backend unavailable\n"))
            reason = "backend-down"
            return
        backend = tcp_writer.get_extra_info("peername") or ()
        if tuple(backend[:2]) != (target_host, target_port):
            log.error("backend peer mismatch: %r", backend)
            writer.write(_http_error(502, "Bad Gateway", "backend mismatch\n"))
            reason = "backend-mismatch"
            return

        # 核销放在 101 之前、后端已就绪之后：后端故障不烧 token，握手成功才是一次性消耗。
        ok, why = tokens.consume(token)
        if not ok:
            log.warning("token lost race (%s) peer=%s", why, peer_id)
            writer.write(_http_error(401, "Unauthorized", f"token {why}\n"))
            reason = f"token-{why}"
            return

        proto = headers.get("sec-websocket-protocol", "")
        accept = _accept_key(key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
        )
        if proto:
            response += f"Sec-WebSocket-Protocol: {proto.split(',')[0].strip()}\r\n"
        response += "\r\n"
        writer.write(response.encode())
        await writer.drain()
        log.info("connection open peer=%s token_ok=1", peer_id)

        tasks = [
            asyncio.create_task(_pump_ws_to_tcp(peer, tcp_writer)),
            asyncio.create_task(_pump_tcp_to_ws(peer, tcp_reader)),
            asyncio.create_task(_keepalive(peer)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        first = next(iter(done))
        exc = first.exception()
        if isinstance(exc, Closed):
            reason = "client-close"
        elif isinstance(exc, KeepaliveTimeout):
            reason = exc.reason
            await peer.close(exc.code, exc.reason)
        elif isinstance(exc, ProtocolError):
            reason = f"protocol:{exc}"
            await peer.close(exc.code, str(exc))
        elif isinstance(exc, (ConnectionError, OSError, asyncio.IncompleteReadError)):
            reason = "disconnect"
        elif exc is not None:
            reason = f"error:{exc!r}"
            log.exception("peer task failed")
        else:
            reason = "closed"
    except (ProtocolError,) as exc:
        reason = f"protocol:{exc}"
        try:
            writer.write(_http_error(400, "Bad Request", "bad websocket\n"))
        except (ConnectionError, RuntimeError):
            pass
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as exc:
        reason = type(exc).__name__
    finally:
        if tcp_writer is not None:
            tcp_writer.close()
            try:
                await tcp_writer.wait_closed()
            except (ConnectionError, RuntimeError):
                pass
        try:
            writer.close()
        except (ConnectionError, RuntimeError):
            pass
        dur = time.monotonic() - started
        log.info(
            "connection closed peer=%s dur=%.1fs ws_in=%d ws_out=%d tcp_in=%d tcp_out=%d reason=%s",
            peer_id,
            dur,
            peer.ws_bytes_in,
            peer.ws_bytes_out,
            peer.tcp_bytes_in,
            peer.tcp_bytes_out,
            reason,
        )


class Relay:
    """中继进程：限并发 + 统一关闭。"""

    def __init__(self, listen_host: str = LISTEN_HOST, listen_port: int = LISTEN_PORT,
                 target_host: str = TARGET_HOST, target_port: int = TARGET_PORT) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.sem = asyncio.Semaphore(MAX_CONNECTIONS)
        self.server: asyncio.AbstractServer | None = None
        self.stop = asyncio.Event()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.sem.locked():
            try:
                writer.write(_http_error(503, "Service Unavailable", "too many connections\n"))
                await writer.drain()
            finally:
                writer.close()
            return
        async with self.sem:
            await _serve_peer(reader, writer, self.target_host, self.target_port)

    async def start(self) -> int:
        """启动监听；返回实际端口（传 0 时由内核分配，测试用）。"""
        self.server = await asyncio.start_server(
            self._handle, self.listen_host, self.listen_port, backlog=8,
            limit=HEADER_LIMIT + 1024,
        )
        port = self.server.sockets[0].getsockname()[1]
        log.info("relay listening on %s:%s → %s:%s", self.listen_host, port,
                 self.target_host, self.target_port)
        return port

    async def run(self) -> None:
        await self.start()
        async with self.server:  # type: ignore[arg-type]
            await self.stop.wait()

    async def aclose(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        self.stop.set()


async def _amain() -> int:
    relay = Relay()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, relay.stop.set)
        except NotImplementedError:
            pass
    await relay.run()
    log.info("relay stopped")
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("MAVLINK_RELAY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s mavlink-relay %(levelname)s %(message)s",
    )
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
