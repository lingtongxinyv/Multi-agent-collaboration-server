"""DSH Suite 统一面板后端（FastAPI, :8010）。

- 账号/会话/配额/审计
- 用户端：任务提交（站娘/worker 执行）、沙箱文件管理
- 管理端：监控、告警、账号、worker 审批、persona 导入导出、SMTP、服务管理
- 前端 SPA 挂载 /
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import (FastAPI, File, Form, HTTPException, Query, Request, Response,
                     UploadFile)
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import re

from . import auth, drive, memory, monitor, sandbox
from .db import get_db, load_quest_token

RESTARTABLE = {"dsh-admin", "dsh-panel", "dsh-hub", "nginx", "cpolar-dsh"}
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
# 分发给用户的 agent/连接器文件所在目录。用 env 可覆盖：以 root 跑脚本时
# Path.home() 会指向 /root（影子目录），铁律 #2 同源的老坑。
WORKERS_DIR = Path(os.environ.get("BELLHUB_WORKERS_DIR",
                                  str(Path.home() / "dsh-suite" / "workers")))
# SSE 心跳间隔（秒）：DSH 事件是块级的，工具执行期间可能数分钟不产生字节，
# 必须主动发注释帧保活（防 nginx/cpolar 空闲超时断流），同时让客户端断开
# 能在 ≤ 该间隔内被察觉（此前要等下一次写事件才发现，实测拖了 49 秒）
SSE_HEARTBEAT_SEC = 15
# 消费者（浏览器）超过该秒数不来取数据即判定为已断开：同步生成器被 starlette
# 遗弃时不会触发 GeneratorExit，必须靠这个看门狗兜底（心跳 15s 一次，30s 即漏两次）
SSE_STALL_SEC = 30

app = FastAPI(title="DSH Suite Panel", docs_url=None, redoc_url=None)

# 站娘对话接口（2026-10-22）：给多 Agent 框架补会话语义（线程/游标/伙伴 token）。
# 逻辑与表结构都在 app/chat.py，这里只挂路由；失败不阻断面板其它功能。
try:
    from .chat import router as chat_router
    from .chat_page import page_router as chat_page_router
    app.include_router(chat_router)
    app.include_router(chat_page_router)   # /chat 极简网页（免构建）
except Exception as _chat_exc:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger(__name__).warning("chat router 未挂载: %s", _chat_exc)


def _latest_dsh_token() -> str | None:
    """从 dsh-admin 日志解析最新一次启动打印的 web token。

    2026-11-02 修正：原先只取 journal 最后 300 行，而 token 只在服务启动时打印一次。
    dsh-onebot 每分钟写一条重连错误，约 2.5 小时就把启动行顶出窗口，/dsh-entry 必然
    报 502「无法获取 DSH token」（此时 dsh-admin 其实正常运行）。改为按「本次开机」
    范围 + 只匹配启动行扫描，结果与日志总量无关；同一开机内多次重启仍取最新一条。
    """
    try:
        out = subprocess.run(
            ["journalctl", "-u", "dsh-admin", "-b", "--no-pager",
             "--grep", "dsh web:.*token="],
            capture_output=True, text=True, timeout=10,
        )
        toks = re.findall(r"token=([A-Za-z0-9_-]{20,})", out.stdout)
        return toks[-1] if toks else None
    except Exception:
        return None


@app.get("/dsh-entry")
def dsh_entry(request: Request):
    """管理员账号密码登录后的 DSH 一键入口：校验会话后携带最新 token 跳转。"""
    try:
        account = auth.current_account(request)
        if account["role"] != "admin":
            raise HTTPException(403)
    except HTTPException:
        return RedirectResponse("/login", 302)
    token = _latest_dsh_token()
    if token is None:
        raise HTTPException(502, "无法获取 DSH token，请确认 dsh-admin 服务正在运行")
    return RedirectResponse(f"/dsh/?token={token}", 302)


@app.on_event("startup")
def _startup() -> None:
    db = get_db()
    # 星铃委托板发布凭据：启动时确保存在（0600），不落库不进 git
    load_quest_token()
    # 守护复位：SSE 中断残留的 running 会话超过 30 分钟视为失联，复位为 failed
    for s in db.list_running_sessions(1800):
        db.set_session_status(s["id"], "failed")
    monitor.start_worker()


# ── 认证 ────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    old: str
    new: str


@app.post("/api/auth/login")
def login(body: LoginBody, response: Response):
    account = get_db().verify_login(body.username.strip(), body.password)
    if account is None:
        raise HTTPException(401, "用户名或密码错误")
    auth.issue_session(response, account)
    get_db().audit("auth.login", account["username"], account["role"])
    return {
        "id": account["id"], "username": account["username"], "role": account["role"],
        "display_name": account["display_name"],
        "must_change_pw": bool(account["must_change_pw"]),
    }


@app.post("/api/auth/logout")
def logout(response: Response):
    auth.clear_session(response)
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    account = auth.current_account(request)
    # 跨天先归零再展示：否则用户次日打开面板看到的「今日用量」还是昨天的数字
    # （额度其实会在开始新一轮对话时重置，但展示路径不会，看起来就是"额度不刷新"）
    account = get_db().get_account_fresh(account["id"]) or account
    return {
        "id": account["id"], "username": account["username"], "role": account["role"],
        "display_name": account["display_name"],
        "must_change_pw": bool(account["must_change_pw"]),
        "quota": {
            "tokens_daily": account["quota_tokens_daily"],
            "concurrent": account["quota_concurrent"],
            "disk_mb": account["quota_disk_mb"],
            "used_tokens": account["used_tokens"],
            "used_disk_mb": account["used_disk_mb"],
        },
    }


@app.post("/api/auth/password")
def change_password(body: PasswordBody, request: Request):
    account = auth.current_account(request)
    from .db import hash_password, verify_password
    if not verify_password(body.old, account["password_hash"]):
        raise HTTPException(400, "原密码错误")
    if len(body.new) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    get_db().update_account(account["id"], {"password": body.new})
    return {"ok": True}


# ── 沙箱文件（用户端） ──────────────────────────────────────────

@app.get("/api/files")
def files_list(request: Request, path: str = ""):
    account = auth.current_account(request)
    try:
        return sandbox.list_dir(account["username"], path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/files/content", response_class=PlainTextResponse)
def files_read(request: Request, path: str):
    account = auth.current_account(request)
    try:
        return sandbox.read_file(account["username"], path)
    except (ValueError, FileNotFoundError, PermissionError) as exc:
        raise HTTPException(400, str(exc))


class FileBody(BaseModel):
    path: str
    content: str = ""


@app.put("/api/files/content")
def files_write(body: FileBody, request: Request):
    account = auth.current_account(request)
    try:
        return sandbox.write_file(account["username"], body.path, body.content)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/files")
def files_delete(request: Request, path: str = Query(...)):
    account = auth.current_account(request)
    try:
        sandbox.delete_path(account["username"], path)
        return {"ok": True}
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/files/download")
def files_download(request: Request, path: str):
    account = auth.current_account(request)
    try:
        root = sandbox.sandbox_root_for(account["username"])
        target = sandbox.resolve_safe(root, path)
        if not target.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(target, filename=target.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── 用户端：个人网盘（助手 2026-10-13 阶段一）────────────────────
# 存储 ~/dsh-users/<username>/drive/，配额独立于代码沙箱（quota_drive_mb）。
# 安全：所有路径经 drive._resolve() 逐段校验（拒绝 .. / . / 绝对路径）；
# 配额在服务端强制；上传走流式写入 .tmp 后原子改名。

@app.get("/api/drive/list")
def drive_list(request: Request, path: str = ""):
    account = auth.current_account(request)
    try:
        return drive.list_dir(account["username"], path)
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(400, str(exc))


class DrivePathBody(BaseModel):
    path: str = Field(default="", max_length=1000)


class DriveUploadBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


@app.post("/api/drive/upload")
async def drive_upload(request: Request, file: UploadFile = File(...), path: str = Form(...)):
    """流式上传：边收边写盘（1MB 块），2GB 文件也不会撑爆内存；超配额立即中止。"""
    account = auth.current_account(request)
    if not path.strip():
        raise HTTPException(400, "必须提供目标路径")
    try:
        # UploadFile.file 是 SpooledTemporaryFile，直接当流读
        result = drive.save_stream(account["username"], path, file.file)
        db = get_db()
        db.audit(f"drive.upload:{path}", account["username"], account["role"],
                 f"size={result['size']}")
        return result
    except PermissionError as exc:
        raise HTTPException(413, str(exc))
    except (ValueError, IsADirectoryError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/drive/download")
def drive_download(request: Request, path: str):
    """流式下载，支持 Range 断点续传。"""
    account = auth.current_account(request)
    try:
        info = drive.file_info(account["username"], path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc))
    size = info["size"]
    start, end = 0, size - 1
    status = 200
    range_hdr = request.headers.get("range", "")
    if range_hdr.startswith("bytes="):
        spec = range_hdr[6:].split(",")[0].strip()
        a, _, b = spec.partition("-")
        try:
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            elif b:                      # bytes=-500 → 末尾 500 字节
                start = max(0, size - int(b))
            end = min(end, size - 1)
            if start > end or start >= size:
                raise ValueError("范围非法")
            status = 206
        except ValueError:
            start, end, status = 0, size - 1, 200
    length = end - start + 1
    headers = {
        # 用 drive.content_disposition()：中文/emoji 文件名需按 RFC 6266 编码，
        # 直接放裸文件名会触发 starlette 的 latin-1 编码错误（下载 500）
        "Content-Disposition": drive.content_disposition(info["name"]),
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(drive.iter_file(account["username"], path, start, length),
                             status_code=status, media_type="application/octet-stream",
                             headers=headers)


@app.post("/api/drive/mkdir")
def drive_mkdir(body: DrivePathBody, request: Request):
    account = auth.current_account(request)
    try:
        return drive.mkdir(account["username"], body.path)
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(400, str(exc))


class DriveRenameBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    new_name: str = Field(min_length=1, max_length=255)


@app.post("/api/drive/rename")
def drive_rename(body: DriveRenameBody, request: Request):
    account = auth.current_account(request)
    try:
        return drive.rename_path(account["username"], body.path, body.new_name)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        raise HTTPException(400, str(exc))


class DriveDeleteBody(BaseModel):
    paths: list[str] = Field(min_length=1)


@app.post("/api/drive/delete")
def drive_delete(body: DriveDeleteBody, request: Request):
    account = auth.current_account(request)
    try:
        result = drive.delete_paths(account["username"], body.paths)
        get_db().audit(f"drive.delete:{len(body.paths)}", account["username"],
                       account["role"], ",".join(body.paths)[:200])
        return result
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/drive/preview")
def drive_preview(request: Request, path: str):
    """图片/文本在线预览（上限 2MB）。"""
    account = auth.current_account(request)
    try:
        content, mime = drive.read_preview(account["username"], path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))
    return Response(content, media_type=mime)


# ── 用户端：分片上传与跨会话续传（助手 2026-10-13）──────────────────
# 协议：init（拿到 sid 与已传分片）→ chunk × N（可乱序/可重传）→ complete（拼接）
# 断线后重新 init 同一文件即可从已传位置继续；分片暂存 .upload/ 不计入用户配额。

class DriveUploadInitBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    size: int = Field(gt=0)
    sha256: str = Field(default="", max_length=64)
    chunk_size: int | None = None


@app.post("/api/drive/upload/init")
def drive_upload_init(body: DriveUploadInitBody, request: Request):
    account = auth.current_account(request)
    try:
        return drive.upload_init(account, body.path, body.size,
                                 body.sha256, body.chunk_size or drive.CHUNK_SIZE)
    except PermissionError as exc:
        raise HTTPException(413, str(exc))
    except (ValueError, IsADirectoryError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/drive/upload/chunk")
async def drive_upload_chunk(request: Request, sid: str = Form(...),
                             index: int = Form(...), data: UploadFile = File(...)):
    """保存一个分片。幂等：同 index 重传直接覆盖（便于失败重试）。"""
    account = auth.current_account(request)
    try:
        payload = await data.read()
        return drive.save_chunk(account, sid, int(index), payload)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class DriveUploadSidBody(BaseModel):
    sid: str = Field(min_length=8, max_length=64)


@app.post("/api/drive/upload/complete")
def drive_upload_complete(body: DriveUploadSidBody, request: Request):
    account = auth.current_account(request)
    try:
        result = drive.complete_upload(account, body.sid)
        db = get_db()
        db.audit(f"drive.upload_chunked:{result['relpath']}", account["username"],
                 account["role"], f"size={result['size']}")
        return result
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/drive/upload/status")
def drive_upload_status(request: Request, sid: str = Query(...)):
    """查询会话进度（用于页面刷新后恢复进度显示）。"""
    account = auth.current_account(request)
    up = get_db().get_drive_upload(sid)
    if up is None or up["owner"] != account["username"]:
        raise HTTPException(404, "上传会话不存在")
    view = drive.upload_view(up)
    view["direct"] = int(up["size"]) <= drive.DIRECT_MAX
    return view


@app.post("/api/drive/upload/cancel")
def drive_upload_cancel(body: DriveUploadSidBody, request: Request):
    account = auth.current_account(request)
    try:
        return drive.cancel_upload(account, body.sid)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


# ── 用户端：网盘分享（管理员选定的安全策略：仅登录用户可访问）────────

class DriveShareBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    note: str = Field(default="", max_length=500)
    expire_hours: float | None = None


@app.post("/api/drive/share")
def drive_share_create(body: DriveShareBody, request: Request):
    account = auth.current_account(request)
    try:
        return drive.create_share(account["username"], body.path, body.note, body.expire_hours)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/drive/share")
def drive_share_list(request: Request):
    account = auth.current_account(request)
    return get_db().list_drive_shares(account["username"])


@app.delete("/api/drive/share/{sid}")
def drive_share_delete(sid: str, request: Request):
    account = auth.current_account(request)
    if not get_db().delete_drive_share(sid, account["username"]):
        raise HTTPException(404, "分享不存在")
    return {"ok": True}


@app.get("/api/drive/shared")
def drive_shared_list(request: Request):
    """所有有效的分享（仅登录用户可见；管理员已确认不开放游客访问）。"""
    auth.current_account(request)
    out = []
    for sh in get_db().list_all_drive_shares():
        ok, why = drive.share_is_valid(sh)
        if ok:
            out.append({**sh, "owner_is_me": False})
    acc = auth.current_account(request)
    for item in out:
        item["owner_is_me"] = item["owner"] == acc["username"]
    return out


@app.get("/api/drive/shared/{sid}")
def drive_shared_get(sid: str, request: Request):
    """访问他人分享的文件（需登录 + 分享有效）。"""
    auth.current_account(request)
    sh = get_db().get_drive_share(sid)
    ok, why = drive.share_is_valid(sh)
    if not ok:
        raise HTTPException(404, why)
    try:
        info = drive.file_info(sh["owner"], sh["relpath"])
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, f"分享的文件已不存在: {exc}")
    return StreamingResponse(drive.iter_file(sh["owner"], sh["relpath"]),
                             media_type="application/octet-stream",
                             headers={"Content-Disposition":
                                      drive.content_disposition(info["name"])})


# ── 管理端：网盘视图与配额 ────────────────────────────────────────

@app.get("/api/admin/drive")
def admin_drive(request: Request):
    """管理员：所有用户的网盘占用与配额。"""
    auth.require_admin(request)
    db = get_db()
    rows = []
    for acc in db.list_accounts():
        used, quota = drive.get_quotas(acc)
        rows.append({"username": acc["username"], "role": acc["role"],
                     "used_mb": round(used, 2), "quota_mb": quota,
                     "shares": len(db.list_drive_shares(acc["username"]))})
    return {"users": rows, "max_file_mb": drive.max_file_bytes() // drive.MB,
            "shares_total": len(db.list_all_drive_shares())}


class DriveQuotaPatch(BaseModel):
    quota_drive_mb: int = Field(ge=1, le=1024 * 1024)


@app.patch("/api/admin/drive/{uid}")
def admin_drive_quota(uid: str, body: DriveQuotaPatch, request: Request):
    auth.require_admin(request)
    db = get_db()
    if db.get_account(uid) is None:
        raise HTTPException(404, "账号不存在")
    db.update_account(uid, {"quota_drive_mb": body.quota_drive_mb})
    return {"ok": True, "quota_drive_mb": body.quota_drive_mb}


@app.get("/api/admin/drive/uploads")
def admin_drive_uploads(request: Request):
    """管理员：所有未完成的分片上传（半成品）。"""
    auth.require_admin(request)
    db = get_db()
    rows = []
    for u in db.list_drive_uploads():
        v = drive.upload_view(u)
        rows.append({k: v[k] for k in ("sid", "owner", "dest_path", "name", "size",
                                       "received", "total_chunks", "created_at", "updated_at")})
    keep_days = drive.RESUME_KEEP_DAYS
    return {"uploads": rows, "keep_days": keep_days,
            "total_mb": round(sum((r["size"] or 0) for r in rows) / drive.MB, 2)}


@app.post("/api/admin/drive/uploads/cleanup")
def admin_drive_uploads_cleanup(request: Request, days: float = Query(default=drive.RESUME_KEEP_DAYS),
                                all_users: bool = Query(default=True)):
    """手动清理半成品：all_users=True 用默认保留期清理全部，False 时 days=0 表示立刻清光。"""
    admin = auth.require_admin(request)
    result = drive.cleanup_expired_uploads(keep_days=max(0.0, days))
    get_db().audit(f"drive.cleanup_uploads:{result['removed']}", admin["username"],
                   admin["role"], f"orphans={result['orphans']} freed={result['freed_mb']}MB")
    return result


# ── 管理端：账号 ────────────────────────────────────────────────

class AccountBody(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8)
    role: str = Field(pattern="^(admin|user)$")
    display_name: str = ""


class AccountPatch(BaseModel):
    display_name: str | None = None
    disabled: bool | None = None
    password: str | None = None
    quota_tokens_daily: int | None = None
    quota_concurrent: int | None = None
    quota_disk_mb: int | None = None
    quota_drive_mb: int | None = None


@app.get("/api/admin/accounts")
def admin_accounts(request: Request):
    auth.require_admin(request)
    return get_db().list_accounts()


@app.post("/api/admin/accounts")
def admin_create_account(body: AccountBody, request: Request):
    admin = auth.require_admin(request)
    db = get_db()
    if db.get_account_by_name(body.username):
        raise HTTPException(400, "用户名已存在")
    return db.create_account(body.username, body.password, body.role, body.display_name)


@app.patch("/api/admin/accounts/{uid}")
def admin_patch_account(uid: str, body: AccountPatch, request: Request):
    auth.require_admin(request)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if body.password is not None:
        if len(body.password) < 8:
            raise HTTPException(400, "密码至少 8 位")
        fields["password"] = body.password
    get_db().update_account(uid, fields)
    return {"ok": True}


@app.delete("/api/admin/accounts/{uid}")
def admin_delete_account(uid: str, request: Request):
    auth.require_admin(request)
    acc = get_db().get_account(uid)
    if acc is None:
        raise HTTPException(404, "账号不存在")
    if acc["role"] == "admin":
        raise HTTPException(400, "不能删除管理员账号")
    get_db().delete_account(uid)
    return {"ok": True}


# ── 管理端：监控/告警/审计 ──────────────────────────────────────

@app.get("/api/admin/monitor/current")
def admin_monitor(request: Request):
    auth.require_admin(request)
    return monitor.current_metrics()


@app.get("/api/admin/monitor/history")
def admin_monitor_history(request: Request, hours: int = 24):
    auth.require_admin(request)
    return get_db().history(min(hours, 168))


@app.get("/api/admin/alerts")
def admin_alerts(request: Request, unack: bool = False):
    auth.require_admin(request)
    return get_db().list_alerts(unack)


class AckBody(BaseModel):
    ids: list[int]


@app.post("/api/admin/alerts/ack")
def admin_ack_alerts(body: AckBody, request: Request):
    auth.require_admin(request)
    get_db().ack_alerts(body.ids)
    return {"ok": True}


@app.get("/api/admin/alerts/rules")
def admin_rules(request: Request):
    auth.require_admin(request)
    return get_db().list_rules()


class RulePatch(BaseModel):
    op: str | None = None
    threshold: float | None = None
    duration_sec: int | None = None
    enabled: bool | None = None
    notify_email: bool | None = None


@app.patch("/api/admin/alerts/rules/{rid}")
def admin_patch_rule(rid: str, body: RulePatch, request: Request):
    auth.require_admin(request)
    get_db().update_rule(rid, {k: v for k, v in body.model_dump().items() if v is not None})
    return {"ok": True}


@app.get("/api/admin/audit")
def admin_audit(request: Request, limit: int = 200):
    auth.require_admin(request)
    return get_db().list_audit(min(limit, 1000))


# ── 管理端：SMTP ────────────────────────────────────────────────

class SmtpBody(BaseModel):
    smtp_host: str
    smtp_port: int = 465
    smtp_user: str
    smtp_pass: str
    smtp_from: str = ""
    smtp_to: str


@app.get("/api/admin/smtp")
def admin_get_smtp(request: Request):
    auth.require_admin(request)
    db = get_db()
    return {
        "smtp_host": db.get_setting("smtp_host"), "smtp_port": db.get_setting("smtp_port", "465"),
        "smtp_user": db.get_setting("smtp_user"), "smtp_to": db.get_setting("smtp_to"),
        "configured": bool(db.get_setting("smtp_host")),
    }


@app.post("/api/admin/smtp")
def admin_set_smtp(body: SmtpBody, request: Request):
    auth.require_admin(request)
    db = get_db()
    for key, value in body.model_dump().items():
        db.set_setting(key, str(value))
    sent = monitor.notify_email("DSH 面板 SMTP 测试", "这是一封测试邮件，配置成功。")
    return {"ok": True, "test_sent": sent}


# ── 管理端：persona 导入/导出 ───────────────────────────────────

def _archive_from(card: dict, prefix: str, suffix: str) -> dict:
    """伙伴-A 风格人格卡 + DSH persona 映射的导出归档。"""
    return {
        "format": "dsh-suite-persona",
        "version": 1,
        "meta": {
            "name": card.get("name", ""),
            "description": card.get("description", ""),
            "exportedAt": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        "card": card,
        "persona": {"prefix": prefix, "suffix": suffix},
    }


@app.get("/api/admin/persona")
def admin_get_persona(request: Request):
    auth.require_admin(request)
    return get_db().get_persona()


class PersonaBody(BaseModel):
    card: dict
    prefix: str = ""
    suffix: str = ""


@app.put("/api/admin/persona")
def admin_set_persona(body: PersonaBody, request: Request):
    admin = auth.require_admin(request)
    if not body.card.get("name") or not body.prefix:
        raise HTTPException(400, "人格卡需要 name，prefix 不能为空")
    if len(body.prefix) > 100_000:
        raise HTTPException(400, "prefix 过大")
    get_db().set_persona(body.card, body.prefix, body.suffix)
    _write_dsh_patch(get_db().get_persona())
    return {"ok": True, "note": "已保存并生成 DSH 补丁；可通过服务管理重启 dsh-admin 生效"}


@app.get("/api/admin/persona/export")
def admin_export_persona(request: Request):
    auth.require_admin(request)
    row = get_db().get_persona()
    archive = _archive_from(row["card"], row["prefix"], row["suffix"])
    name = (row["card"].get("name") or "persona").replace("/", "_")
    return Response(
        content=json.dumps(archive, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}.persona.json"'})


@app.post("/api/admin/persona/import")
def admin_import_persona(archive: dict, request: Request):
    admin = auth.require_admin(request)
    """导入归档：支持完整归档（format=dsh-suite-persona）或裸卡片 {card, prefix, suffix}。"""
    if archive.get("format") == "dsh-suite-persona":
        card = archive.get("card") or {}
        persona = archive.get("persona") or {}
        prefix, suffix = persona.get("prefix", ""), persona.get("suffix", "")
    elif "card" in archive and "prefix" in archive:
        card, prefix, suffix = archive["card"], archive.get("prefix", ""), archive.get("suffix", "")
    else:
        raise HTTPException(400, "无法识别人格归档：需要 format=dsh-suite-persona 或 {card, prefix}")
    if not card.get("name") or not prefix:
        raise HTTPException(400, "人格卡需要 name，prefix 不能为空")
    get_db().set_persona(card, prefix, suffix)
    _write_dsh_patch(get_db().get_persona())
    get_db().audit("persona.import", admin["username"], "admin", card.get("name", ""))
    return {"ok": True, "name": card["name"]}


def _write_dsh_patch(row: dict) -> None:
    """改写 web 预设 standard.patch.yml 与 headless cordis.patch.yml 中 persona 前缀。"""
    prefix = row["prefix"]
    suffix = row.get("suffix") or ""

    # ── web 标准补丁 ────────────────────────────────────────────
    patch_path = Path.home() / "deepseek-harness/packages/bundle/web-app/presets/standard.patch.yml"
    try:
        text = patch_path.read_text(encoding="utf-8")
    except OSError as exc:
        get_db().raise_alert("warn", "persona", "standard.patch.yml 读取失败", str(exc))
        return
    lines = text.splitlines(keepends=True)
    in_persona = False
    replaced_prefix = replaced_suffix = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "- id: persona":
            in_persona = True
            continue
        if in_persona:
            if stripped.startswith("- id: ") and stripped != "- id: persona":
                in_persona = False
            elif "prefix:" in line and not replaced_prefix:
                indent = line[: len(line) - len(line.lstrip())]
                lines[i] = f'{indent}prefix: {json.dumps(prefix, ensure_ascii=False)}\n'
                replaced_prefix = True
            elif "suffix:" in line and not replaced_suffix:
                indent = line[: len(line) - len(line.lstrip())]
                lines[i] = f'{indent}suffix: {json.dumps(suffix, ensure_ascii=False)}\n'
                replaced_suffix = True
    if not replaced_prefix:
        get_db().raise_alert("warn", "persona", "persona 行定位失败", "standard.patch.yml 结构变化")
        return
    patch_path.write_text("".join(lines), encoding="utf-8")

    # ── headless 补丁（personaPrefix 折行 YAML → 双引号单行 JSON 标量） ──
    headless_path = Path.home() / "deepseek-harness/packages/bundle/headless/cordis.patch.yml"
    try:
        text = headless_path.read_text(encoding="utf-8")
    except OSError as exc:
        get_db().raise_alert("warn", "persona", "cordis.patch.yml 读取失败", str(exc))
        return
    lines = text.splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("personaPrefix:"):
            indent = line[: len(line) - len(line.lstrip())]
            # 若为折行/字面块标量，跳过其缩进值行
            j = i + 1
            if stripped.rstrip(":").endswith((">-", "|-", ">", "|")):
                while j < len(lines) and lines[j].strip() and \
                        len(lines[j]) - len(lines[j].lstrip()) > len(indent):
                    j += 1
            lines[i] = f'{indent}personaPrefix: {json.dumps(prefix, ensure_ascii=False)}\n'
            del lines[i + 1: j]
            replaced = True
            break
    if not replaced:
        get_db().raise_alert("warn", "persona", "personaPrefix 定位失败", "cordis.patch.yml 结构变化")
        return
    headless_path.write_text("".join(lines), encoding="utf-8")


# ── 管理端：worker 管理 ─────────────────────────────────────────

class WorkerBody(BaseModel):
    name: str = Field(min_length=2, max_length=32)
    capabilities: list[str] = []
    user_available: bool = False


@app.get("/api/admin/workers")
def admin_workers(request: Request):
    auth.require_admin(request)
    return get_db().list_workers()


@app.post("/api/admin/workers")
def admin_add_worker(body: WorkerBody, request: Request):
    auth.require_admin(request)
    db = get_db()
    if db.one("SELECT 1 FROM workers WHERE name=?", (body.name,)):
        raise HTTPException(400, "worker 名已存在")
    token = db.admin_add_worker(body.name, body.capabilities, body.user_available)
    return {"token": token, "note": "token 仅此一次显示，请立即保存"}


class WorkerPatch(BaseModel):
    approved: bool | None = None
    user_available: bool | None = None


@app.patch("/api/admin/workers/{wid}")
def admin_patch_worker(wid: str, body: WorkerPatch, request: Request):
    auth.require_admin(request)
    get_db().update_worker(wid, {k: v for k, v in body.model_dump().items() if v is not None})
    return {"ok": True}


@app.delete("/api/admin/workers/{wid}")
def admin_delete_worker(wid: str, request: Request):
    auth.require_admin(request)
    get_db().delete_worker(wid)
    return {"ok": True}


# ── 管理端：服务管理（sudo 白名单） ─────────────────────────────

class ServiceAction(BaseModel):
    action: str = Field(pattern="^(restart|status)$")


@app.post("/api/admin/services/{name}")
def admin_service(name: str, body: ServiceAction, request: Request):
    admin = auth.require_admin(request)
    if name not in RESTARTABLE:
        raise HTTPException(400, "服务不在白名单")
    # sudoers 白名单只授权带 .service 后缀的写法（is-active 才是通配）：
    # 传裸名会被 sudo 判成未授权命令，`-n` 下直接失败
    # （"interactive authentication is required"）→ 接口 500。
    # 2026-10-13 服务管理页「重启」全部失败、dsh-admin 重启不了的根因。
    unit = name if name.endswith(".service") else f"{name}.service"
    if body.action == "status":
        state = subprocess.run(["systemctl", "is-active", unit],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return {"service": name, "state": state}
    try:
        subprocess.run(["sudo", "-n", "systemctl", "restart", unit],
                       capture_output=True, text=True, timeout=30, check=True)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(500, f"重启失败: {exc.stderr.strip()[:300]}")
    get_db().audit(f"service.restart:{name}", admin["username"], "admin")
    return {"ok": True, "note": f"{name} 已重启"}


TESTS_DIR = Path(__file__).resolve().parents[3] / "tests"


@app.post("/api/admin/selftest")
def admin_selftest(request: Request):
    """一键跑「智能体链接」验收套件（管理员）。

    套件全部使用隔离临时库并断言 DB_PATH，不会碰到真实 panel.db（铁律 #2）。
    面板以 agent 身份运行，因此脚本里的相对路径正好指向本仓库。
    """
    admin = auth.require_admin(request)
    script = TESTS_DIR / "run_all.sh"
    if not script.is_file():
        raise HTTPException(404, "自检脚本不存在：tests/run_all.sh")
    try:
        r = subprocess.run(["bash", str(script), "--bin"], capture_output=True, text=True,
                           timeout=900, cwd=str(TESTS_DIR))
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "自检超时（>15 分钟），请到服务器上手动跑 tests/run_all.sh")
    lines = (r.stdout or "").splitlines()
    get_db().audit("selftest.run", admin["username"], "admin", f"exit={r.returncode}")
    return {
        "ok": r.returncode == 0,
        "exit_code": r.returncode,
        "passed": sum(1 for ln in lines if ln.startswith("✅")),
        "failed": sum(1 for ln in lines if ln.startswith("❌")),
        "summary": "\n".join(lines[-80:]),
        "stderr": (r.stderr or "")[-2000:],
    }


# ── 管理端：代码中心（网页 Git / 编辑 / 部署 / 交站娘） ──────────

from . import codecenter
from . import assistant

class RepoBody(BaseModel):
    repo: str

class CommitBody(RepoBody):
    message: str = Field(min_length=1, max_length=500)
    paths: list[str] = Field(min_length=1)

class FilePathBody(RepoBody):
    path: str

class FileWriteBody(FilePathBody):
    content: str = ""

class DeployBody(RepoBody):
    mode: str = Field(pattern="^(restart|full)$")

class SendToAssistantBody(RepoBody):
    paths: list[str] = Field(min_length=1)
    note: str = ""
    session_id: str | None = None


class AssistantConfigBody(BaseModel):
    api_key: str = Field(min_length=8, pattern=r"^sk-")


@app.get("/api/admin/assistant/config")
def assistant_get_config(request: Request):
    auth.require_admin(request)
    key = get_db().get_setting("assistant_api_key") or ""
    return {"configured": bool(key), "key_hint": (key[:6] + "…" + key[-4:]) if len(key) > 12 else ""}


@app.put("/api/admin/assistant/config")
def assistant_set_config(body: AssistantConfigBody, request: Request):
    auth.require_admin(request)
    get_db().set_setting("assistant_api_key", body.api_key.strip())
    get_db().audit("assistant.config", "admin", "admin")
    return {"ok": True}


class AssistantChatBody(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = None


@app.post("/api/admin/assistant/chat")
def assistant_chat(body: AssistantChatBody, request: Request):
    auth.require_admin(request)
    key = get_db().get_setting("assistant_api_key") or ""
    try:
        result = assistant.send_prompt(body.text, key, body.session_id)
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc))
    return result


# ── 会话（工作站模式：会话即任务，SSE 流式回复） ──────────────────

class SessionCreateBody(BaseModel):
    title: str = Field(default="", max_length=120)


@app.post("/api/sessions")
def create_session(body: SessionCreateBody, request: Request):
    account = auth.current_account(request)
    return get_db().create_session(account, body.title)


@app.get("/api/sessions")
def list_sessions(request: Request):
    account = auth.current_account(request)
    return get_db().list_sessions(account["id"])


@app.get("/api/sessions/{sid}")
def get_session_detail(sid: str, request: Request):
    account = auth.current_account(request)
    db = get_db()
    session = db.get_session(sid)
    if session is None or session["user_id"] != account["id"]:
        raise HTTPException(404, "会话不存在")
    messages = db.list_session_messages(sid)
    return {**session, "messages": messages}


class SessionRenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)


@app.patch("/api/sessions/{sid}")
def rename_session(sid: str, body: SessionRenameBody, request: Request):
    account = auth.current_account(request)
    db = get_db()
    session = db.get_session(sid)
    if session is None or session["user_id"] != account["id"]:
        raise HTTPException(404, "会话不存在")
    db.rename_session(sid, body.title)
    return {"ok": True}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, request: Request):
    account = auth.current_account(request)
    db = get_db()
    session = db.get_session(sid)
    if session is None or session["user_id"] != account["id"]:
        raise HTTPException(404, "会话不存在")
    db.delete_session(sid)
    return {"ok": True}


class SessionPinBody(BaseModel):
    pinned: bool


@app.post("/api/sessions/{sid}/pin")
def pin_session(sid: str, body: SessionPinBody, request: Request):
    account = auth.current_account(request)
    db = get_db()
    session = db.get_session(sid)
    if session is None or session["user_id"] != account["id"]:
        raise HTTPException(404, "会话不存在")
    db.toggle_pin_session(sid, body.pinned)
    return {"ok": True}


class SessionStreamBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    refs: list[str] = []  # 引用文件相对路径（沙箱内）


@app.post("/api/sessions/{sid}/stream")
def session_stream(sid: str, body: SessionStreamBody, request: Request):
    """SSE 流式对话：每次用户消息触发一次站娘执行，逐事件推送。"""
    account = auth.current_account(request)
    db = get_db()
    session = db.get_session(sid)
    if session is None or session["user_id"] != account["id"]:
        raise HTTPException(404, "会话不存在")
    if session["status"] == "running":
        raise HTTPException(409, "会话正在执行中，请等待完成")
    ok, reason = db.check_quota(account["id"], 2_000)
    if not ok:
        raise HTTPException(429, reason)
    key = db.get_setting("assistant_api_key") or ""
    if not key:
        raise HTTPException(503, "站娘尚未配置 API Key，请联系管理员")

    # 读取引用文件（沙箱内，防越权）
    refs_content: list[tuple[str, str]] = []
    for ref_path in body.refs[:20]:
        try:
            content = sandbox.read_file(account["username"], ref_path)
            refs_content.append((ref_path, content))
        except (ValueError, FileNotFoundError):
            pass

    # 记录用户消息 + 标记运行中
    db.append_chat_msg(sid, account["id"], "user", body.text)
    db.set_session_status(sid, "running")
    db.set_session_title_if_default(sid, body.text[:80])

    prompt = memory.build_prompt(
        account["id"], account["username"], account["role"], body.text)
    sandbox_root = Path.home() / "dsh-users" / account["username"]
    sandbox_root.mkdir(parents=True, exist_ok=True)
    dsh_sid = session["dsh_session_id"] or None

    # ── 本轮执行的生命周期：与 HTTP 连接彻底解耦 ──────────────────────
    # 收尾（状态 / token / 落库）一律由 pump 线程负责。此前把它挂在 HTTP
    # 生成器的 finally 上，而 starlette 在客户端断开时可能直接遗弃同步生成器
    # （不调用 close()），finally 永不执行 → 会话永久停在 running、后续发送
    # 被 409 拒绝（2026-10-13 事故根因）。
    state = {
        "last_pull": time.time(),   # 消费者最近一次来取数据的时刻
        "ended": False,             # 生成器已收到结束哨兵
        "finished": False,          # 整轮已收尾
        "interrupted": False,       # 判定为客户端断开
        "failed": False,            # 供 done 事件使用
        "error": "",                # 失败原因原文（写进审计，供事后诊断）
    }
    proc_holder: list = []          # [Popen]：断连时用于主动终止站娘子进程

    def kill_proc() -> None:
        if proc_holder and proc_holder[0].poll() is None:
            try:
                proc_holder[0].kill()
            except Exception:  # noqa: BLE001
                pass

    def finalize(*, interrupted: bool, had_error: bool, text: str,
                 tokens: int, new_sid: str | None) -> None:
        """整轮收尾（DB 写）。由 pump 线程调用，与 HTTP 连接是否存活无关。"""
        try:
            if interrupted or had_error:
                db.set_session_status(sid, "failed")
                # 病因留痕：失败原因此前只出现在浏览器里，事后无法判断到底是
                # 面板配额 / 模型额度 / 网络 / 设备侧问题 —— 事后诊断全靠这条审计
                reason = (state.get("error")
                          or ("站娘报错（无详情）" if had_error else "客户端断开（无错误文本）"))
                if interrupted:
                    # 断连是主动 kill 子进程导致的，优先说明断连这个真实病因
                    reason = (f"客户端断开（站娘进程已被终止）；站娘侧原始信息：{reason}"
                              if state.get("error") else "客户端断开（站娘进程已被终止）")
                try:
                    db.audit(f"session.failed:{sid}", account["username"], account["role"],
                             f"{reason[:400]} ｜ 任务：{body.text[:120]}")
                except Exception:  # noqa: BLE001
                    pass
                # 中断/报错也要保住已生成的内容，否则刷新后永久丢失
                partial = (text or "").strip()
                if partial:
                    db.append_chat_msg(
                        sid, account["id"], "assistant",
                        partial + "\n\n> ⚠ 本轮被中断（连接断开或站娘报错），以上为已生成内容。")
            else:
                if new_sid:
                    db.set_session_status(sid, "done", dsh_session_id=new_sid)
                else:
                    db.set_session_status(sid, "done")
                if text:
                    db.append_chat_msg(sid, account["id"], "assistant", text)
                if tokens > 0:
                    db.add_session_tokens(sid, tokens)
                    db.add_tokens(account["id"], tokens)
                memory.record_session_interaction(
                    account["id"], body.text, text or "(无回复)")
        except Exception:  # noqa: BLE001
            # 收尾本身失败也必须让状态离开 running，否则会话会一直卡住
            try:
                db.set_session_status(sid, "failed")
            except Exception:  # noqa: BLE001
                pass
        finally:
            state["finished"] = True

    q: "queue.Queue[dict | None]" = queue.Queue()

    def pump() -> None:
        """后台线程：消费站娘事件流并负责落库，主生成器只负责转发。"""
        final_text = ""
        streamed: list[str] = []
        tokens = 0
        new_sid = dsh_sid
        had_error = False
        try:
            for ev in assistant.stream_prompt(
                    prompt, key, session_id=dsh_sid, cwd=sandbox_root,
                    refs=refs_content,
                    on_start=lambda p: proc_holder.append(p)):
                etype = ev.get("type")
                if etype == "session" and ev.get("session_id"):
                    new_sid = ev["session_id"]
                    db.set_session_status(sid, "running", dsh_session_id=new_sid)
                elif etype == "final":
                    final_text = ev.get("text") or ""
                    tokens = ev.get("tokens") or 0
                    new_sid = ev.get("session_id") or new_sid
                elif etype == "text":
                    streamed.append(ev.get("text") or "")
                elif etype == "error":
                    had_error = True
                    if not state["error"]:
                        state["error"] = ev.get("message") or "站娘报错（无详情）"
                q.put(ev)
        except assistant.AssistantError as exc:
            had_error = True
            state["error"] = state["error"] or str(exc)
            q.put({"type": "_error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            had_error = True
            state["error"] = state["error"] or f"内部错误: {exc}"
            q.put({"type": "_error", "message": f"内部错误: {exc}"})
        finally:
            state["failed"] = had_error or state["interrupted"]
            q.put(None)             # 结束哨兵
            finalize(interrupted=state["interrupted"], had_error=had_error,
                     text=final_text or "".join(streamed), tokens=tokens,
                     new_sid=new_sid)

    def watchdog() -> None:
        """看门狗：消费者超过 SSE_STALL_SEC 不来取数据，说明客户端已断开而
        starlette 又遗弃了这个同步生成器（不会触发 GeneratorExit）。此时主动
        杀掉站娘子进程，让 pump 走失败收尾，而不是让会话永远停在 running。"""
        while not (state["ended"] or state["finished"]):
            time.sleep(3)
            if state["ended"] or state["finished"]:
                return
            if time.time() - state["last_pull"] > SSE_STALL_SEC:
                state["interrupted"] = True
                kill_proc()
                return

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    def event_stream():
        try:
            while True:
                state["last_pull"] = time.time()    # 客户端还在拉取
                try:
                    event = q.get(timeout=SSE_HEARTBEAT_SEC)
                except queue.Empty:
                    # SSE 注释帧：只为保活，前端解析时忽略非 data: 行
                    yield ": ping\n\n"
                    continue
                if event is None:
                    state["ended"] = True
                    break
                etype = event.get("type")
                if etype == "_error":
                    msg = event.get("message") or "内部错误"
                    yield f"data: {json.dumps({'type': 'error', 'message': msg}, ensure_ascii=False)}\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # 此处禁止 yield（断连时 yield 会抛 RuntimeError），也禁止做收尾：
            # 收尾统一归 pump 线程，避免"客户端一断就永远收不了尾"。
            if not state["ended"]:
                state["interrupted"] = True
                kill_proc()
        # done 事件仅在正常完成路径发送（断开时客户端已不可达）
        yield f"data: {json.dumps({'type': 'done', 'status': 'failed' if state['failed'] else 'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── 我的设备（用户自有设备与 agent 接入） ─────────────────────────

class DeviceBody(BaseModel):
    name: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    desc: str = Field(default="", max_length=200)


@app.get("/api/devices")
def devices_list(request: Request):
    account = auth.current_account(request)
    devices = get_db().list_devices(account["id"])
    # 在线判定：90 秒内有心跳
    for d in devices:
        d["online"] = (time.time() - d["last_seen"]) < 90
    return devices


@app.post("/api/devices")
def device_create(body: DeviceBody, request: Request):
    account = auth.current_account(request)
    db = get_db()
    if db.one("SELECT 1 FROM devices WHERE user_id=? AND name=?",
              (account["id"], body.name)):
        raise HTTPException(400, "设备名已存在")
    device, token = db.create_device(account["id"], body.name, body.desc)
    return {**device, "token": token}  # token 仅此一次明文返回


@app.delete("/api/devices/{did}")
def device_delete(did: str, request: Request):
    account = auth.current_account(request)
    db = get_db()
    device = db.get_device(did)
    if device is None or device["user_id"] != account["id"]:
        raise HTTPException(404, "设备不存在")
    db.delete_device(did)
    return {"ok": True}


DEVICE_AGENT_FILES = WORKERS_DIR
DEVICE_AGENT_WHITELIST = ("device_agent.py", "device_handler.py", "shell_handler.py",
                          "termux_handler.py",       # 助手 2026-10-14: 安卓(Termux)白名单处理器
                          "bellhub_mcp_bridge.py",   # 助手 2026-10-13: MCP stdio 桥，供 Trae 等 IDE 接入
                          "bellhub_connector.py")    # 助手 2026-10-14: 即装即用连接器（配对+派活）


@app.get("/api/devices/agent-files")
def device_agent_files(request: Request):
    """可下载的 agent 文件清单（版本/大小/修改时间/短哈希）。

    下载页据此显示「这一版是什么」，用户下载后可自行核对哈希，
    避免"以为拿到新版其实是旧文件"。
    """
    auth.current_account(request)
    out = []
    for fname in DEVICE_AGENT_WHITELIST:
        path = DEVICE_AGENT_FILES / fname
        if not path.is_file():
            continue
        data = path.read_bytes()
        version = None
        if fname.endswith(".py"):
            m = re.search(rb'^VERSION\s*=\s*"([^"]+)"', data, re.M)
            if m:
                version = m.group(1).decode("utf-8", "replace")
        out.append({
            "name": fname,
            "size": len(data),
            "mtime": path.stat().st_mtime,
            "version": version,
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    return out


@app.get("/api/devices/agent-files/{fname}")
def device_agent_file(fname: str, request: Request):
    """设备 agent 客户端文件下载（登录用户即可，白名单三个文件）。"""
    auth.current_account(request)
    if fname not in DEVICE_AGENT_WHITELIST:
        raise HTTPException(404, "文件不存在")
    target = DEVICE_AGENT_FILES / fname
    if not target.is_file():
        raise HTTPException(502, "服务端文件缺失，请联系管理员")
    # 强制每次校验（no-cache = 带 If-None-Match 回源）：这几个文件会随版本更新，
    # 若让浏览器按启发式规则复用本地副本，用户可能拿到上一版却以为已更新
    return FileResponse(target, filename=fname,
                        headers={"Cache-Control": "no-cache, must-revalidate"})


class CommandBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@app.post("/api/devices/{did}/command")
def device_command(did: str, body: CommandBody, request: Request):
    account = auth.current_account(request)
    db = get_db()
    device = db.get_device(did)
    if device is None or device["user_id"] != account["id"]:
        raise HTTPException(404, "设备不存在")
    cmd = db.queue_device_command(did, body.text, source=account["username"])
    return cmd


@app.get("/api/devices/{did}/commands")
def device_commands(did: str, request: Request, limit: int = 50):
    account = auth.current_account(request)
    db = get_db()
    device = db.get_device(did)
    if device is None or device["user_id"] != account["id"]:
        raise HTTPException(404, "设备不存在")
    return db.list_device_commands(did, max(1, min(limit, 200)))


# ── 智能体链接：浏览器授权配对 + 我的智能体（助手 2026-10-14） ────
#
# 目标（管理员 2026-10-14）：接入不再依赖管理员手工发全站共享密钥。
# 流程：连接器 POST /api/link/device-code 拿到 (device_code, user_code)
#   → 自动打开浏览器到 /link?code=<user_code>（用户已登录）
#   → 用户点"确认"（POST /api/link/approve）
#   → 连接器 GET /api/link/device-code/<device_code> 一次性换取自己的凭据。

class PairStartBody(BaseModel):
    name: str = Field(default="connector", max_length=32)
    host: str = Field(default="", max_length=64)
    platform: str = Field(default="", max_length=32)
    capabilities: list = Field(default_factory=list)


class PairCodeBody(BaseModel):
    user_code: str = Field(min_length=4, max_length=16)


_pair_hits: dict[str, list[float]] = {}


def _pair_rate_ok(ip: str, limit: int = 12, window: float = 60.0) -> bool:
    """公开端点的朴素限流：每 IP 每分钟 12 次。"""
    ts = time.time()
    hits = [t for t in _pair_hits.get(ip, []) if ts - t < window]
    hits.append(ts)
    if len(_pair_hits) > 5000:      # 防内存膨胀（正常远达不到）
        _pair_hits.clear()
    _pair_hits[ip] = hits
    return len(hits) <= limit


@app.post("/api/link/device-code")
def link_device_code(body: PairStartBody, request: Request):
    """连接器发起配对（公开端点 + 每 IP 限流）。"""
    ip = request.client.host if request.client else "?"
    if not _pair_rate_ok(ip):
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    name = re.sub(r"[^A-Za-z0-9_.-]", "", body.name)[:32] or "connector"
    caps = [c for c in body.capabilities if isinstance(c, (str, dict))][:20]
    pair = get_db().create_pair_code(name, body.host, body.platform, caps)
    return {
        "device_code": pair["device_code"],
        "user_code": pair["user_code"],
        "expires_in": max(0, int(pair["expires_at"] - time.time())),
        "interval": pair["interval"],
        "verification_uri": f"/link?code={pair['user_code']}",
    }


@app.get("/api/link/device-code/{device_code}")
def link_device_poll(device_code: str):
    """连接器轮询；批准后一次性换取自己的长期凭据（库里只存哈希）。"""
    db = get_db()
    row = db.get_pair_by_device_code(device_code)
    if row is None:
        raise HTTPException(404, "配对请求不存在")
    if row["status"] == "approved":
        got = db.consume_pair(device_code)
        if got is None:
            return {"status": "consumed", "note": "凭据已发放过；如需重新获取请重新配对"}
        agent, token, username = got
        return {"status": "approved", "agent_id": agent["id"], "agent_name": agent["name"],
                "token": token, "username": username, "platform": agent["platform"]}
    if row["status"] == "pending" and row["expires_at"] < time.time():
        return {"status": "expired"}
    return {"status": row["status"]}


@app.get("/api/link/pair-code")
def link_pair_lookup(code: str, request: Request):
    """确认页用：先给用户看清楚"这台设备是谁、要接什么"，再点确认。"""
    auth.current_account(request)
    p = get_db().get_pair_by_user_code(code)
    if p is None or p["expires_at"] < time.time():
        raise HTTPException(404, "配对码无效或已过期")
    return {"user_code": p["user_code"], "agent_name": p["agent_name"], "host": p["host"],
            "platform": p["platform"], "capabilities": json.loads(p["capabilities"] or "[]"),
            "expires_at": p["expires_at"], "status": p["status"]}


@app.post("/api/link/approve")
def link_approve(body: PairCodeBody, request: Request):
    account = auth.current_account(request)
    pair = get_db().approve_pair(body.user_code, account["id"])
    if pair is None:
        raise HTTPException(404, "配对码无效或已过期")
    return {"ok": True, "user_code": pair["user_code"], "agent_name": pair["agent_name"],
            "host": pair["host"], "platform": pair["platform"]}


@app.post("/api/link/deny")
def link_deny(body: PairCodeBody, request: Request):
    account = auth.current_account(request)
    if not get_db().deny_pair(body.user_code, account["id"]):
        raise HTTPException(404, "配对码无效或已过期")
    return {"ok": True}


@app.get("/api/link/agents")
def link_agents(request: Request):
    """我的智能体名册：连接器与 IDE 反向接入统一在这里管理。"""
    account = auth.current_account(request)
    out = []
    for a in get_db().list_user_agents(account["id"]):
        out.append({
            "id": a["id"], "name": a["name"], "agent_type": a["agent_type"],
            "platform": a["platform"], "host": a["host"],
            "capabilities": json.loads(a["capabilities"] or "[]"),
            "online": (time.time() - a["last_seen"]) < 90,
            "last_seen": a["last_seen"], "last_ping_at": a["last_ping_at"],
            "registered_at": a["registered_at"],
        })
    return out


@app.delete("/api/link/agents/{aid}")
def link_agent_delete(aid: str, request: Request):
    account = auth.current_account(request)
    if not get_db().delete_user_agent(account["id"], aid):
        raise HTTPException(404, "队员不存在")
    return {"ok": True}


@app.post("/api/link/agents/{aid}/ping")
def link_agent_ping(aid: str, request: Request):
    """面板上的「测试连接」：如实回报**由心跳推出的**状态，不伪造在线。

    连接器是主动轮询方，面板无法反向探测它，所以这里唯一的真相是
    「最近 90 秒有没有心跳」。旧实现无条件返回 online、还顺带把 last_seen
    刷成 now()，等于点一下就自欺欺人（2026-10-14 修）。
    """
    account = auth.current_account(request)
    db = get_db()
    a = db.get_user_agent(account["id"], aid)
    if a is None:
        raise HTTPException(404, "队员不存在")
    db.touch_agent(aid, ping=True)          # 只记「探测过」，不动 last_seen
    online = (time.time() - (a["last_seen"] or 0)) < 90
    return {"ok": True, "name": a["name"], "agent_type": a["agent_type"],
            "status": "online" if online else "offline", "online": online,
            "last_seen": a["last_seen"], "checked_at": time.time(),
            "note": "" if online else "最近 90 秒没有心跳：连接器可能没在跑"
                                      "（bellhub-connector run），或电脑休眠 / 窗口被关"}


@app.get("/api/link/tasks")
def link_tasks(request: Request, limit: int = 20):
    """我名下队员最近的子任务 —— 队伍视图用（只查自己的队员，天然隔离）。"""
    account = auth.current_account(request)
    out = []
    for s in get_db().list_user_subtasks(account["id"], limit):
        out.append({
            "id": s["id"], "parent_task_id": s["parent_task_id"], "step": s["step"],
            "title": s["title"], "assignee": s["assignee"],
            "capability": s["capability"], "status": s["status"],
            "result": (s["result"] or "")[:2000],
            "created_at": s["created_at"], "finished_at": s["finished_at"],
        })
    return out


class DispatchBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    title: str = Field(default="", max_length=120)
    capability: str = Field(default="", max_length=32)
    agent: str = Field(default="", max_length=40)


@app.post("/api/link/dispatch")
def link_dispatch(body: DispatchBody, request: Request):
    """面板直接派活给用户自己的队员（不依赖站娘会话，所有用户即刻可用）。

    · 指定 agent 时允许离线排队（连接器下次上线会领走）；
    · 不指定时按能力自动挑**在线**队员，挑不到就明确报错，而不是建一条没人领的任务。
    """
    account = auth.current_account(request)
    db = get_db()
    title = body.title.strip() or body.prompt.strip()[:40]
    if body.agent:
        agent = db.one("SELECT * FROM agents WHERE user_id=? AND name=?",
                       (account["id"], body.agent))
        if agent is None:
            raise HTTPException(404, "队员不存在")
    else:
        agent = db.pick_user_agent(account["id"], body.capability)
        if agent is None:
            hint = "你还没有在线的队员：先在这台电脑上把连接器跑起来（bellhub-connector run）"
            if body.capability:
                hint += f"，或换一个能力标签（当前要求：{body.capability}）"
            raise HTTPException(400, hint)
    task = db.create_task(account, title, body.prompt, "link-dispatch")
    sub = db.create_subtask(task["id"], 1, title, body.prompt, body.capability,
                            agent["name"])
    online = (time.time() - (agent["last_seen"] or 0)) < 90
    return {"task_id": task["id"], "subtask_id": sub["id"], "assignee": agent["name"],
            "online": online,
            "note": "" if online else "该队员当前离线，任务已排队，等它上线后自动执行"}


# 预编译连接器分发（助手 2026-10-14）：把 build_connector.sh/.ps1 的产物放进
# workers/dist-connector/ 即自动出现在「我的智能体」页，用户下载即可双击使用。
CONNECTOR_BIN_DIR = Path(os.environ.get(
    "BELLHUB_CONNECTOR_BIN_DIR", str(WORKERS_DIR / "dist-connector")))
CONNECTOR_BINS = {
    "win-x64": ("bellhub-connector.exe", "Windows x64 单文件（双击即用）"),
    "linux-x64": ("bellhub-connector", "Linux x86_64 单文件"),
}


def _connector_build_info(key: str) -> dict | None:
    spec = CONNECTOR_BINS.get(key)
    if spec is None:
        return None
    path = CONNECTOR_BIN_DIR / spec[0]
    if not path.is_file():
        return None
    data = path.read_bytes()
    # 产物的版本由构建脚本写成旁文件（面板读不到二进制内部的 VERSION）。
    # 没有旁文件时 version=None —— 连接器会退回用源码版本比较，不会假装知道。
    sidecar = path.with_name(path.name + ".version")
    version = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else None
    return {"platform": key, "file": spec[0], "label": spec[1], "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "mtime": path.stat().st_mtime,
            "version": version}


@app.get("/api/link/connector-builds")
def link_connector_builds(request: Request):
    """已就绪的预编译连接器（未构建的平台不出现）。"""
    auth.current_account(request)
    return [info for key in CONNECTOR_BINS if (info := _connector_build_info(key))]


@app.get("/api/link/connector-builds/{platform}")
def link_connector_build(platform: str, request: Request):
    auth.current_account(request)
    spec = CONNECTOR_BINS.get(platform)
    if spec is None:
        raise HTTPException(404, "不支持的平台")
    path = CONNECTOR_BIN_DIR / spec[0]
    if not path.is_file():
        raise HTTPException(404, "该平台还没有预编译版本：请用源码版连接器，"
                                 "或在对应系统上运行 workers/build_connector 脚本后重试")
    return FileResponse(path, filename=spec[0],
                        headers={"Cache-Control": "no-cache, must-revalidate"})


def _connector_source_version() -> str | None:
    """从源码文件里读 VERSION —— 连接器自检"站点上最新是哪一版"用。"""
    src = WORKERS_DIR / "bellhub_connector.py"
    if not src.is_file():
        return None
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"',
                  src.read_text(encoding="utf-8", errors="replace"), re.M)
    return m.group(1) if m else None


@app.get("/api/link/connector-latest")
def link_connector_latest(request: Request):
    """连接器版本自检（需已配对的 agent 凭据）。

    刻意不做成公开端点：版本/构建面没必要暴露在公网，而连接器本来就持有
    自己的 agent 凭据，用它鉴权最自然（`X-Agent-Auth: name:token`）。
    """
    name, _, token = request.headers.get("x-agent-auth", "").partition(":")
    agent = get_db().verify_agent(name, token) if name and token else None
    if agent is None:
        raise HTTPException(401, "需要已配对的连接器凭据（X-Agent-Auth: name:token）")
    src = WORKERS_DIR / "bellhub_connector.py"
    return {
        "version": _connector_source_version(),
        "source": "bellhub_connector.py",
        **(_file_digest(src) if src.is_file() else {}),
        "builds": [info for key in CONNECTOR_BINS if (info := _connector_build_info(key))],
    }


def _file_digest(path: Path) -> dict:
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "mtime": path.stat().st_mtime}


def _agent_from_request(request: Request) -> dict | None:
    name, _, token = request.headers.get("x-agent-auth", "").partition(":")
    return get_db().verify_agent(name, token) if name and token else None


@app.get("/api/link/connector-artifact")
def link_connector_artifact(request: Request, kind: str = "bin", platform: str = ""):
    """连接器自升级取件（agent 凭据鉴权；sha256 放响应头供调用方逐字节核对）。

    kind=bin    取该平台的打包产物（该平台未构建则 404，调用方据此降级）
    kind=source 取源码版 bellhub_connector.py
    """
    if _agent_from_request(request) is None:
        raise HTTPException(401, "需要已配对的连接器凭据（X-Agent-Auth: name:token）")
    if kind == "source":
        path = WORKERS_DIR / "bellhub_connector.py"
        if not path.is_file():
            raise HTTPException(404, "服务端缺少源码版连接器")
    elif kind == "bin":
        spec = CONNECTOR_BINS.get(platform.strip())
        if spec is None:
            raise HTTPException(404, f"不支持的平台: {platform or '（未指定）'}")
        path = CONNECTOR_BIN_DIR / spec[0]
        if not path.is_file():
            raise HTTPException(404, f"{platform} 还没有预编译产物：请让管理员在对应系统上跑 "
                                     f"workers/build_connector 脚本，或改用源码版")
    else:
        raise HTTPException(400, "kind 只能是 bin 或 source")
    digest = _file_digest(path)
    return FileResponse(
        path, filename=path.name,
        headers={"Cache-Control": "no-cache, must-revalidate",
                 "X-Bellhub-Sha256": digest["sha256"],
                 "X-Bellhub-Size": str(digest["size"]),
                 "X-Bellhub-Version": _connector_source_version() or ""})


# ── 站娘记忆管理 ──────────────────────────────────────────────────

@app.get("/api/memory")
def memory_get(request: Request):
    account = auth.current_account(request)
    return get_db().get_memory(account["id"])


class MemoryBody(BaseModel):
    summary: str = Field(max_length=8000)


@app.put("/api/memory")
def memory_put(body: MemoryBody, request: Request):
    account = auth.current_account(request)
    db = get_db()
    mem = db.get_memory(account["id"])
    db.set_memory(account["id"], body.summary, mem["recent"])
    return {"ok": True}


@app.delete("/api/memory")
def memory_clear(request: Request):
    account = auth.current_account(request)
    get_db().set_memory(account["id"], "", [])
    return {"ok": True}


@app.get("/api/admin/memory/{uid}")
def admin_memory_get(uid: str, request: Request):
    auth.require_admin(request)
    return get_db().get_memory(uid)


@app.get("/api/admin/code/repos")
def code_repos(request: Request):
    auth.require_admin(request)
    return codecenter.repo_overview()


@app.get("/api/admin/code/status")
def code_status(repo: str, request: Request):
    auth.require_admin(request)
    try:
        return codecenter.status(repo)
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/admin/code/log")
def code_log(repo: str, request: Request, limit: int = 30):
    auth.require_admin(request)
    try:
        return codecenter.log(repo, limit)
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/admin/code/diff")
def code_diff(repo: str, path: str, request: Request):
    auth.require_admin(request)
    try:
        return {"text": codecenter.diff(repo, path)}
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/admin/code/tree")
def code_tree(repo: str, request: Request, path: str = ""):
    auth.require_admin(request)
    try:
        return codecenter.tree(repo, path)
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/admin/code/file")
def code_file(repo: str, path: str, request: Request):
    auth.require_admin(request)
    try:
        return {"path": path, "content": codecenter.read_file(repo, path)}
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/admin/code/file")
def code_write_file(body: FileWriteBody, request: Request):
    admin = auth.require_admin(request)
    try:
        codecenter.write_file(body.repo, body.path, body.content)
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))
    get_db().audit(f"code.write:{body.repo}", admin["username"], "admin", body.path)
    return {"ok": True}


@app.post("/api/admin/code/commit")
def code_commit(body: CommitBody, request: Request):
    admin = auth.require_admin(request)
    try:
        sha = codecenter.commit(body.repo, body.message, body.paths)
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))
    get_db().audit(f"code.commit:{body.repo}", admin["username"], "admin", body.message[:120])
    return {"ok": True, "sha": sha}


@app.post("/api/admin/code/push")
def code_push(body: RepoBody, request: Request):
    admin = auth.require_admin(request)
    try:
        note = codecenter.push(body.repo)
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))
    get_db().audit(f"code.push:{body.repo}", admin["username"], "admin")
    return {"ok": True, "note": note}


@app.post("/api/admin/code/pull")
def code_pull(body: RepoBody, request: Request):
    admin = auth.require_admin(request)
    try:
        note = codecenter.pull(body.repo)
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))
    get_db().audit(f"code.pull:{body.repo}", admin["username"], "admin")
    return {"ok": True, "note": note}


@app.post("/api/admin/code/deploy")
def code_deploy(body: DeployBody, request: Request):
    admin = auth.require_admin(request)
    try:
        job_id = codecenter.deploy(body.repo, body.mode)
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))
    get_db().audit(f"code.deploy:{body.repo}", admin["username"], "admin", body.mode)
    return {"ok": True, "job_id": job_id}


@app.get("/api/admin/code/deploy/history")
def code_deploy_history(request: Request):
    auth.require_admin(request)
    return codecenter.deploy_history()


@app.get("/api/admin/code/deploy/{job_id}")
def code_deploy_status(job_id: str, request: Request):
    auth.require_admin(request)
    try:
        return codecenter.deploy_status(job_id)
    except codecenter.CodeError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/admin/code/send-to-assistant")
def code_send_to_assistant(body: SendToAssistantBody, request: Request):
    admin = auth.require_admin(request)
    key = get_db().get_setting("assistant_api_key") or ""
    try:
        text = codecenter.build_assistant_payload(body.repo, body.paths, body.note)
        result = assistant.send_prompt(text, key, body.session_id)
    except codecenter.CodeError as exc:
        raise HTTPException(400, str(exc))
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc))
    get_db().audit(f"code.assistant:{body.repo}", admin["username"], "admin", ",".join(body.paths)[:200])
    return result


# ── 前端 SPA ────────────────────────────────────────────────────

# ── 星铃委托板（用户任务板）──────────────────────────────────────


class QuestBody(BaseModel):
    title: str
    detail: str = ""
    category: str = "side"
    rarity: str = "common"
    target: int = 1
    reward: str = ""


class QuestPublishBody(BaseModel):
    """服务侧发布（站娘/值守/外部脚本）。user 可按用户名指定收件人。"""
    user: str = "admin"
    title: str
    detail: str = ""
    category: str = "side"
    rarity: str = "common"
    target: int = 1
    reward: str = ""
    origin: str = "助手"
    ref: str = ""


def _quest_service_auth(request: Request) -> None:
    """校验 X-Quest-Token：与 data/quest-publish.token 常量时间比较。"""
    supplied = request.headers.get("x-quest-token", "")
    expected = load_quest_token()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "委托发布凭据无效")


@app.get("/api/quests")
def quests_list(request: Request):
    account = auth.current_account(request)
    db = get_db()
    rows = db.list_quests(account["id"])
    return {"quests": rows, "stats": db.quest_stats(account["id"], rows)}


@app.get("/api/quests/board")
def quests_board(request: Request, user: str = Query(default="admin")):
    """服务侧只读：读取某位用户的委托板。"""
    _quest_service_auth(request)
    db = get_db()
    account = db.one("SELECT * FROM accounts WHERE username=?", (user,))
    if account is None:
        raise HTTPException(404, f"账号不存在：{user}")
    rows = db.list_quests(account["id"])
    return {"user": user, "quests": rows, "stats": db.quest_stats(account["id"], rows)}


@app.post("/api/quests/publish")
def quests_publish(body: QuestPublishBody, request: Request):
    """站娘给管理员挂委托：同一 (user, ref) 幂等更新，不重复挂单。"""
    _quest_service_auth(request)
    if not body.title.strip():
        raise HTTPException(400, "委托标题不能为空")
    db = get_db()
    account = db.one("SELECT * FROM accounts WHERE username=?", (body.user,))
    if account is None:
        raise HTTPException(404, f"账号不存在：{body.user}")
    row, created = db.create_quest(
        account["id"], body.title, body.detail, body.category, body.rarity,
        body.target, body.reward, origin=body.origin, ref=body.ref)
    db.audit("quest.publish", (body.origin or "service").strip()[:24], "service",
             f"{account['username']} ref={body.ref.strip()[:120]} {body.title.strip()[:60]}")
    return {"quest": row, "created": created}


@app.post("/api/quests")
def quests_create(body: QuestBody, request: Request):
    account = auth.current_account(request)
    if not body.title.strip():
        raise HTTPException(400, "委托标题不能为空")
    row, _created = get_db().create_quest(
        account["id"], body.title, body.detail, body.category, body.rarity,
        body.target, body.reward)
    return row or {}


@app.post("/api/quests/{qid}/claim")
def quests_claim(qid: str, request: Request):
    account = auth.current_account(request)
    if not get_db().claim_quest(account["id"], qid):
        raise HTTPException(409, "这单已被接取或已完成")
    return {"ok": True}


@app.post("/api/quests/{qid}/complete")
def quests_complete(qid: str, request: Request):
    account = auth.current_account(request)
    if not get_db().complete_quest(account["id"], qid):
        raise HTTPException(409, "这单已经归档了")
    return {"ok": True}


if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str, request: Request):
        if full_path.startswith("api/"):
            return JSONResponse({"error": "not found"}, status_code=404)
        candidate = (FRONTEND_DIST / full_path).resolve()
        if full_path and candidate.is_file() and str(candidate).startswith(str(FRONTEND_DIST)):
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


def main() -> None:
    import uvicorn
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8010
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
