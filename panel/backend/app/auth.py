"""会话认证：HMAC 签名的 httpOnly cookie（panel_session）。

payload = {uid, role, exp, iat}；签名密钥见 db.load_secret()。
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from typing import Any

from fastapi import HTTPException, Request, Response

from .db import get_db, load_secret

COOKIE_NAME = "panel_session"
DAY = 86400
MAX_AGE = 7 * DAY

_secret = load_secret()


def _sign(body: bytes) -> str:
    return hmac.new(_secret, body, "sha256").hexdigest()


def _encode(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"v1.{body.decode().rstrip('=')}.{_sign(body)}"


def _decode(token: str) -> dict[str, Any] | None:
    try:
        version, body, sig = token.split(".", 2)
        if version != "v1":
            return None
        padded = body + "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
        if not hmac.compare_digest(sig, _sign(padded.encode())):
            return None
        payload = json.loads(raw)
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def issue_session(response: Response, account: dict) -> None:
    now = int(time.time())
    token = _encode({
        "uid": account["id"], "role": account["role"],
        "iat": now, "exp": now + MAX_AGE,
    })
    response.set_cookie(
        COOKIE_NAME, token, max_age=MAX_AGE, path="/", httponly=True,
        samesite="lax", secure=False)  # 前置 TLS 由 cpolar 边缘终结


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_account(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    payload = _decode(token) if token else None
    if payload is None:
        raise HTTPException(401, "未登录或会话已过期")
    account = get_db().get_account(payload["uid"])
    if account is None or account["disabled"]:
        raise HTTPException(401, "账号不可用")
    return account


def require_admin(request: Request) -> dict:
    account = current_account(request)
    if account["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return account
