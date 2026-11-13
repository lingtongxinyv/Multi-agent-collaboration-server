#!/usr/bin/env python3
"""MAVLink WSS 中继的一次性接入 token（签发 / 校验 / 吊销）。

设计约束
- 只存 SHA-256 摘要：token 明文只在签发时输出一次，落盘文件被读也不能直接复用。
- 一次性：中继在 WS 握手通过的同一步把条目标记 used_at，之后同一 token 再连一律拒绝。
- 传输：优先放在 `X-Mavlink-Token` 请求头（不落任何日志）；`?token=` 只留给手工调试，
  nginx 对该 location 关了 access_log，但上游报错时 error_log 仍可能带出 URI。
- TTL 默认 30 分钟（1800s），与管理员 2026-10-20 的 P3 裁定一致。
- 存储用 flock + 原子替换，中继（agent 用户）与 CLI 并发安全。

用法::

    python3 ops/mavlink/tokens.py mint --label qgc-win --ttl 1800
    python3 ops/mavlink/tokens.py list
    python3 ops/mavlink/tokens.py revoke --all

路径可用 DSH_MAVLINK_TOKEN_FILE 覆盖（测试用）。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_STORE = Path.home() / "dsh-suite/data/mavlink-tokens.json"
DEFAULT_TTL = 1800
# 过期后仍保留一段时间，便于排查；正常条目 TTL 到期即失去作用。
RETAIN_SEC = 86400


def store_path() -> Path:
    """返回 token 存储路径（可被 DSH_MAVLINK_TOKEN_FILE 覆盖）。"""
    return Path(os.environ.get("DSH_MAVLINK_TOKEN_FILE") or DEFAULT_STORE)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _Locked:
    """对 <store>.lock 加排他锁；锁文件独立，避免替换 JSON 时丢锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(str(self.path) + ".lock", "a+", encoding="utf-8")
        fcntl.flock(self.fh, fcntl.LOCK_EX)

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _load() -> dict:
    path = store_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"version": 1, "tokens": {}}
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "tokens": {}}
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        return {"version": 1, "tokens": {}}
    return data


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mavlink-tokens.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mint(ttl: int = DEFAULT_TTL, label: str = "", now: float | None = None) -> dict:
    """签发一个一次性 token；返回 {token, expires, label}。明文仅此一次返回。"""
    if ttl <= 0:
        raise ValueError("ttl must be positive")
    now = time.time() if now is None else now
    token = "mt1_" + secrets.token_urlsafe(32)
    entry = {
        "created": round(now, 3),
        "expires": round(now + ttl, 3),
        "label": label[:64],
        "used_at": None,
    }
    with _Locked(store_path()):
        data = _load()
        data["tokens"][_digest(token)] = entry
        _prune(data, now)
        _save(data)
    return {"token": token, "expires": entry["expires"], "ttl": ttl, "label": entry["label"]}


def check(token: str, now: float | None = None) -> tuple[bool, str]:
    """只校验不核销：用于「先确认凭据再建后端连接」的顺序，避免后端故障烧掉 token。"""
    now = time.time() if now is None else now
    if not token or len(token) > 200:
        return False, "malformed"
    with _Locked(store_path()):
        entry = _load()["tokens"].get(_digest(token))
    if entry is None:
        return False, "unknown"
    if entry.get("used_at"):
        return False, "already_used"
    if float(entry.get("expires", 0)) <= now:
        return False, "expired"
    return True, "ok"


def consume(token: str, now: float | None = None) -> tuple[bool, str]:
    """校验并核销 token。返回 (是否通过, 原因码)。通过时条目已被标记 used。"""
    now = time.time() if now is None else now
    if not token or len(token) > 200:
        return False, "malformed"
    key = _digest(token)
    with _Locked(store_path()):
        data = _load()
        entry = data["tokens"].get(key)
        if entry is None:
            return False, "unknown"
        if entry.get("used_at"):
            return False, "already_used"
        if float(entry.get("expires", 0)) <= now:
            return False, "expired"
        entry["used_at"] = round(now, 3)
        data["tokens"][key] = entry
        _prune(data, now)
        _save(data)
    return True, "ok"


def _prune(data: dict, now: float) -> None:
    tokens = data.get("tokens", {})
    for key in [
        k
        for k, v in tokens.items()
        if float(v.get("expires", 0)) + RETAIN_SEC <= now
    ]:
        tokens.pop(key, None)


def list_tokens(now: float | None = None) -> list[dict]:
    """列出条目摘要（不含 token 明文，只给摘要前 8 位）。"""
    now = time.time() if now is None else now
    data = _load()
    out = []
    for key, entry in sorted(data.get("tokens", {}).items(), key=lambda kv: kv[1].get("created", 0)):
        out.append(
            {
                "id": key[:8],
                "label": entry.get("label", ""),
                "created": entry.get("created"),
                "expires": entry.get("expires"),
                "used_at": entry.get("used_at"),
                "state": "used"
                if entry.get("used_at")
                else ("expired" if float(entry.get("expires", 0)) <= now else "live"),
            }
        )
    return out


def revoke_all() -> int:
    """清空所有 token（用于事故处置）；返回清除条数。"""
    with _Locked(store_path()):
        data = _load()
        count = len(data.get("tokens", {}))
        data["tokens"] = {}
        _save(data)
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAVLink 中继一次性 token 管理")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mint_p = sub.add_parser("mint", help="签发一次性 token")
    mint_p.add_argument("--label", default="", help="备注（写入审计，如 qgc-win）")
    mint_p.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="有效期秒数（默认 1800）")
    mint_p.add_argument("--json", action="store_true", help="输出 JSON")
    sub.add_parser("list", help="列出条目摘要")
    revoke_p = sub.add_parser("revoke", help="吊销")
    revoke_p.add_argument("--all", action="store_true", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "mint":
        out = mint(ttl=args.ttl, label=args.label)
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        else:
            expires = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(out["expires"]))
            print(out["token"])
            print(f"# label={out['label'] or '-'} ttl={out['ttl']}s expires={expires}", file=sys.stderr)
        return 0
    if args.cmd == "list":
        for row in list_tokens():
            print(
                f"{row['id']}  {row['state']:<7} label={row['label'] or '-':<16} "
                f"created={time.strftime('%m-%d %H:%M:%S', time.localtime(row['created'] or 0))}"
            )
        return 0
    if args.cmd == "revoke":
        n = revoke_all()
        print(f"revoked {n}", file=sys.stderr)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
