"""MAVLink WSS 中继与一次性 token 的封闭测试（无外部依赖，只用回环端口）。

覆盖：token 一次性/过期/缺失、后端故障不烧 token、代理双向字节流、文本帧拒绝、
以及 relay 只接受 /mavlink 路径。中继与假后端都监听随机回环端口，不碰真实的
5760/8766，也不读写真实 token 存储。
"""
from __future__ import annotations

import asyncio
import base64
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops" / "mavlink"))

os.environ["DSH_MAVLINK_TOKEN_FILE"] = str(Path(tempfile.mkdtemp(prefix="mavlink-tokens-")) / "tokens.json")

import relay  # noqa: E402
import tokens  # noqa: E402


class FakeBackend:
    """假 SITL 后端：连上就持续下发 DOWNSTREAM，并收集客户端上传的字节。"""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.received = bytearray()
        self.connected = 0
        self._writer: asyncio.StreamWriter | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connected += 1
        self._writer = writer
        try:
            while True:
                writer.write(b"DOWNSTREAM")
                await writer.drain()
                await asyncio.sleep(0.02)
                if reader.at_eof():
                    return
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), 0.02)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    return
                self.received += chunk
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None


async def _read_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = raw.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        if line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return status, headers


async def _ws_connect(port: int, path: str, token_header: str = "") -> tuple[int, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode()
    extra = f"X-Mavlink-Token: {token_header}\r\n" if token_header else ""
    writer.write(
        (
            f"GET {path} HTTP/1.1\r\n"
            "Host: agent.example.com\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"{extra}"
            "\r\n"
        ).encode()
    )
    await writer.drain()
    status, headers = await _read_head(reader)
    return status, reader, writer


async def _send_frame(writer: asyncio.StreamWriter, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    header = bytearray([0x80 | opcode])
    if len(payload) < 126:
        header.append(0x80 | len(payload))
    else:
        header.append(0x80 | 126)
        header += struct.pack("!H", len(payload))
    writer.write(bytes(header) + mask + masked)
    await writer.drain()


async def _recv_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    b0, b1 = await asyncio.wait_for(reader.readexactly(2), 5)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    return b0 & 0x0F, await reader.readexactly(length)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tokens.revoke_all()
        self.backend = FakeBackend()
        await self.backend.start()
        self.relay = relay.Relay("127.0.0.1", 0, "127.0.0.1", self.backend.port)
        self.port = await self.relay.start()

    async def asyncTearDown(self) -> None:
        await self.relay.aclose()
        await self.backend.stop()

    async def test_proxy_roundtrip_and_one_time_token(self) -> None:
        token = tokens.mint(ttl=60, label="test")["token"]
        status, reader, writer = await _ws_connect(self.port, f"/mavlink?token={token}")
        self.assertEqual(status, 101)

        opcode, payload = await _recv_frame(reader)
        self.assertEqual(opcode, 0x2)
        self.assertEqual(payload, b"DOWNSTREAM")

        await _send_frame(writer, 0x2, b"UPSTREAM")
        await asyncio.sleep(0.2)
        self.assertIn(b"UPSTREAM", bytes(self.backend.received))

        # 同一个 token 第二次连接必须被拒（一次性）
        status2, _, writer2 = await _ws_connect(self.port, f"/mavlink?token={token}")
        self.assertEqual(status2, 401)
        writer2.close()
        writer.close()

    async def test_token_via_header(self) -> None:
        token = tokens.mint(ttl=60, label="header")["token"]
        status, reader, writer = await _ws_connect(self.port, "/mavlink", token_header=token)
        self.assertEqual(status, 101)
        opcode, payload = await _recv_frame(reader)
        self.assertEqual(opcode, 0x2)
        self.assertTrue(payload)
        writer.close()

    async def test_missing_and_unknown_token(self) -> None:
        status, _, writer = await _ws_connect(self.port, "/mavlink")
        self.assertEqual(status, 401)
        writer.close()
        status, _, writer = await _ws_connect(self.port, "/mavlink?token=mt1_bogus")
        self.assertEqual(status, 401)
        writer.close()

    async def test_expired_token(self) -> None:
        token = tokens.mint(ttl=1, label="expired")["token"]
        await asyncio.sleep(1.05)
        status, _, writer = await _ws_connect(self.port, f"/mavlink?token={token}")
        self.assertEqual(status, 401)
        writer.close()

    async def test_backend_down_does_not_burn_token(self) -> None:
        await self.backend.stop()
        token = tokens.mint(ttl=60, label="retry")["token"]
        status, _, writer = await _ws_connect(self.port, f"/mavlink?token={token}")
        self.assertEqual(status, 502)
        writer.close()
        # 后端恢复后同一个 token 仍可用（握手失败不消耗一次性凭据）
        await self.backend.start()
        self.relay.target_port = self.backend.port
        status, reader, writer = await _ws_connect(self.port, f"/mavlink?token={token}")
        self.assertEqual(status, 101)
        writer.close()

    async def test_wrong_path_and_text_frame(self) -> None:
        token = tokens.mint(ttl=60, label="path")["token"]
        status, _, writer = await _ws_connect(self.port, f"/other?token={token}")
        self.assertEqual(status, 404)
        writer.close()

        token2 = tokens.mint(ttl=60, label="text")["token"]
        status, reader, writer = await _ws_connect(self.port, f"/mavlink?token={token2}")
        self.assertEqual(status, 101)
        await _send_frame(writer, 0x1, b"hello")
        for _ in range(50):  # 关闭前可能先收到若干下行数据帧
            opcode, payload = await _recv_frame(reader)
            if opcode == 0x8:
                break
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1003)
        writer.close()

    async def test_keepalive_timeout_close_code_4408(self) -> None:
        relay.PING_INTERVAL, relay.PONG_GRACE = 0.05, 0.2
        try:
            token = tokens.mint(ttl=60, label="keepalive")["token"]
            status, reader, writer = await _ws_connect(self.port, f"/mavlink?token={token}")
            self.assertEqual(status, 101)
            opcode, payload = 0x0, b""
            for _ in range(200):  # 关闭前可能先收到若干下行数据帧
                opcode, payload = await _recv_frame(reader)
                if opcode == 0x8:
                    break
            self.assertEqual(opcode, 0x8)
            self.assertEqual(struct.unpack("!H", payload[:2])[0], 4408)
            self.assertIn(b"keepalive-timeout", payload[2:])
            writer.close()
        finally:
            relay.PING_INTERVAL, relay.PONG_GRACE = 15.0, 60.0

    async def test_healthz_local(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        self.assertIn(b"200", raw.split(b"\r\n")[0])
        writer.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
