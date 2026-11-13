"""个人网盘（Drive）：每个用户独立的文件空间 ~/dsh-users/<username>/drive/。

设计者：站娘助手  版本 2026-10-13.2（阶段一 + 分片续传）

与代码沙箱的关系
----------------
沙箱 = ~/dsh-users/<username>/（工作站里跑的代码与文件）
网盘 = ~/dsh-users/<username>/drive/（用户自己的文件柜，**故意独立**）
两者共用 accounts 表但**配额分开记账**：
  quota_disk_mb / used_disk_mb  → 沙箱（原有）
  quota_drive_mb / used_drive_mb → 网盘（本次新增）

安全约束
--------
1. 所有路径参数都是相对路径，经 resolve_safe() 强制限制在**本用户网盘根**内，
   拒绝 `..`、拒绝越界（与沙箱同一套被验证过的逻辑）。
2. 配额由**服务端**强制；客户端的配额显示只是提示，不能作为依据。
3. 上传先落 `.tmp/` 再原子改名，中途失败不会留下半截文件。
4. 网盘根 0750：同机其他用户不可读。

配额单位统一用 MB（与 accounts.quota_drive_mb 一致）。
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from .db import get_db

MB = 1048576

# 用户文件柜根（与沙箱同级，但走 drive/ 子目录）
# DSH_DRIVE_ROOT 可覆盖：隔离测试用临时根，避免把文件写进真实 ~/dsh-users；
# 同类做法见 main.py 的 WORKERS_DIR，也顺手避掉 root 的 Path.home() 影子目录坑。
DRIVE_USERS_ROOT = Path(os.environ.get("DSH_DRIVE_ROOT", str(Path.home() / "dsh-users")))
DRIVE_DIRNAME = "drive"

# 单文件上限（MB）：默认 2GB，可在面板 settings 里用 drive_max_file_mb 覆盖
DEFAULT_MAX_FILE_MB = 2048
# 新建用户的默认网盘配额（MB）
DEFAULT_QUOTA_MB = 1024
# 预览大小上限（MB）
PREVIEW_MAX_MB = 2

UPLOAD_CHUNK = 1024 * 1024          # 每次读写 1MB，2GB 上传也不会撑爆内存


def content_disposition(filename: str) -> str:
    """构造可安全放进响应头的 Content-Disposition（支持中文/emoji 文件名）。

    ⚠ starlette 用 **latin-1** 编码响应头（responses.py 的 init_headers），
    直接把中文文件名塞进 header 会抛
        UnicodeEncodeError: 'latin-1' codec can't encode characters ...
    导致下载接口 500（2026-10-13 实测：中文名文件下载必崩）。
    按 RFC 6266/5987 同时给出 ASCII 回退名与 UTF-8 百分号编码名即可。
    """
    from urllib.parse import quote
    safe = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename, safe='')}"


def drive_root_for(username: str) -> Path:
    """返回（并按需创建）该用户的网盘根目录。"""
    root = DRIVE_USERS_ROOT / username / DRIVE_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o750)
    except OSError:
        pass
    return root


def _resolve(root: Path, relpath: str) -> Path:
    """把相对路径解析到网盘根内；拒绝 `..` 与任何越界。

    ⚠ 必须在**字符串层**逐段校验：PurePosixPath 会先归一化，
    "a/./../../etc/passwd".parts 会变成 ('..','etc','passwd')，
    若只看归一化结果，`./` 段的越界组合就有机会绕过检查（2026-10-13 单测实证）。
    """
    raw = (relpath or "").strip().replace("\\", "/")
    # 绝对路径/盘符/UNC 一律拒绝：否则 "/etc/passwd".strip("/") 会被静默当成
    # 相对路径收下（2026-10-13 单测发现），语义含混且易被误用
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or raw.startswith("//"):
        raise ValueError("不允许绝对路径")
    raw = raw.strip("/")
    if not raw:
        return root.resolve()
    segs = raw.split("/")
    if any(s in ("", ".", "..") for s in segs):
        raise ValueError("路径不允许包含 . / .. / 空段")
    target = (root / Path(*segs)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and not str(target).startswith(str(root_resolved) + os.sep):
        raise ValueError("路径越出网盘范围")
    return target


def normalize_rel(relpath: str) -> str:
    rel = PurePosixPath((relpath or "").strip().replace("\\", "/").strip("/"))
    return "" if not rel.parts else str(rel)


# ── 配额 ────────────────────────────────────────────────────────

def get_quotas(account: dict) -> tuple[float, float]:
    """返回 (已用MB, 配额MB)。列缺失时用默认值兜底（兼容旧库未迁移的情况）。"""
    used = account.get("used_drive_mb")
    quota = account.get("quota_drive_mb")
    return (float(used) if used is not None else 0.0,
            float(quota) if quota else float(DEFAULT_QUOTA_MB))


def max_file_bytes() -> int:
    db = get_db()
    raw = db.get_setting("drive_max_file_mb", str(DEFAULT_MAX_FILE_MB))
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_FILE_MB
    return max(1, mb) * MB


def usage_mb(username: str) -> float:
    """重算网盘占用（MB）。"""
    root = DRIVE_USERS_ROOT / username / DRIVE_DIRNAME
    if not root.exists():
        return 0.0
    total = 0
    for f in root.rglob("*"):
        # 内部工作目录不计入配额：
        #   .upload/ 分片暂存（半成品不该占用户配额，否则传一半就把配额锁死）
        #   .tmp/    上传落地前的临时文件
        if any(part in (UPLOAD_DIRNAME, ".tmp") for part in f.parts):
            continue
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return round(total / MB, 2)


def refresh_drive_usage(username: str) -> float:
    """重算并写回 accounts.used_drive_mb，返回新值。"""
    db = get_db()
    acc = db.get_account_by_name(username)
    if acc is None:
        return 0.0
    used = usage_mb(username)
    db.update_account_quiet(acc["id"], {"used_drive_mb": used})
    return used


def refresh_all_drive_usage() -> None:
    db = get_db()
    for acc in db.list_accounts():
        db.update_account_quiet(acc["id"], {"used_drive_mb": usage_mb(acc["username"])})


def _check_quota(account: dict, add_bytes: int, replace_bytes: int = 0) -> None:
    """配额强制：已用 + 新增 - 被覆盖 ≈ 必须 ≤ 配额。"""
    used_mb, quota_mb = get_quotas(account)
    delta_mb = (add_bytes - replace_bytes) / MB
    if used_mb + delta_mb > quota_mb:
        free_mb = max(0.0, quota_mb - used_mb)
        raise PermissionError(
            f"网盘配额不足：已用 {used_mb:.1f}MB / 配额 {quota_mb:.0f}MB，"
            f"本次需要 {delta_mb:.1f}MB，剩余 {free_mb:.1f}MB")


# ── 浏览 ────────────────────────────────────────────────────────

def list_dir(username: str, relpath: str = "") -> dict:
    """列出目录内容 + 当前配额概况。"""
    root = drive_root_for(username)
    target = _resolve(root, relpath)
    if not target.exists():
        raise FileNotFoundError(f"目录不存在: {relpath}")
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录: {relpath}")
    rel = normalize_rel(relpath)
    items: list[dict] = []
    for entry in sorted(target.iterdir(), key=lambda p: (p.name.startswith("."), not p.is_dir(), p.name.lower())):
        if entry.name in (".tmp", UPLOAD_DIRNAME):     # 内部工作目录不对外展示
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        is_dir = entry.is_dir()
        items.append({
            "name": entry.name,
            "type": "dir" if is_dir else "file",
            "size": None if is_dir else st.st_size,
            "mtime": int(st.st_mtime),
            "relpath": f"{rel}/{entry.name}" if rel else entry.name,
        })
    acc = get_db().get_account_by_name(username) or {}
    used_mb, quota_mb = get_quotas(acc)
    return {
        "path": rel,
        "parent": str(PurePosixPath(rel).parent) if rel else "",
        "items": items,
        "used_mb": round(used_mb, 2),
        "quota_mb": quota_mb,
        "max_file_mb": max_file_bytes() // MB,
    }


def stat_path(username: str, relpath: str) -> dict:
    root = drive_root_for(username)
    target = _resolve(root, relpath)
    if not target.exists():
        raise FileNotFoundError(f"不存在: {relpath}")
    st = target.stat()
    return {"name": target.name, "relpath": normalize_rel(relpath),
            "is_dir": target.is_dir(), "size": st.st_size, "mtime": int(st.st_mtime)}


# ── 上传 ────────────────────────────────────────────────────────

def save_stream(username: str, relpath: str, stream: BinaryIO,
                chunk: int = UPLOAD_CHUNK) -> dict:
    """把上传流写入目标位置：边收边写，超配额立即中止并清理。

    返回 {relpath, size, replaced}；抛 PermissionError(配额) / ValueError(路径、超限)。
    """
    db = get_db()
    acc = db.get_account_by_name(username)
    if acc is None:
        raise PermissionError("账号不存在")

    rel = normalize_rel(relpath)
    if not rel:
        raise ValueError("必须提供目标文件名")

    root = drive_root_for(username)
    target = _resolve(root, rel)
    if target.is_dir():
        raise IsADirectoryError("目标是一个目录")

    limit = max_file_bytes()
    old_size = target.stat().st_size if target.exists() else 0
    if old_size > 0:
        # 覆盖上传：先把旧文件大小从配额占用里"还"出去（refresh 时会重算，这里只做快速校验）
        pass

    tmp_dir = root / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex}.part"
    written = 0
    try:
        with open(tmp, "wb") as fh:
            while True:
                buf = stream.read(chunk)
                if not buf:
                    break
                written += len(buf)
                if written > limit:
                    raise ValueError(f"文件超过单文件上限 {limit // MB}MB")
                # 写入过程中按块校验配额，避免收完 2GB 才发现超额
                _check_quota(acc, written, old_size)
                fh.write(buf)
            fh.flush()
            os.fsync(fh.fileno())
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, target)          # 原子替换，中途失败不留半截文件
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    used = refresh_drive_usage(username)
    return {"relpath": rel, "size": written, "replaced": old_size > 0, "used_mb": used}


# ── 下载 ────────────────────────────────────────────────────────

def iter_file(username: str, relpath: str, start: int = 0,
              length: int | None = None) -> Iterator[bytes]:
    """按块产出文件内容，支持 Range 断点续传（不把整个文件读进内存）。"""
    root = drive_root_for(username)
    target = _resolve(root, relpath)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在: {relpath}")
    remaining = length
    with open(target, "rb") as fh:
        if start:
            fh.seek(start)
        while True:
            want = UPLOAD_CHUNK if remaining is None else min(UPLOAD_CHUNK, remaining)
            if want <= 0:
                break
            buf = fh.read(want)
            if not buf:
                break
            if remaining is not None:
                remaining -= len(buf)
            yield buf


def file_info(username: str, relpath: str) -> dict:
    root = drive_root_for(username)
    target = _resolve(root, relpath)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在: {relpath}")
    st = target.stat()
    return {"path": target, "name": target.name, "size": st.st_size,
            "mtime": int(st.st_mtime), "relpath": normalize_rel(relpath)}


# ── 删除 / 重命名 / 新建目录 ─────────────────────────────────────

def delete_paths(username: str, relpaths: list[str]) -> dict:
    """删除文件或目录（目录递归），返回删除数量与最新占用。"""
    root = drive_root_for(username)
    deleted = 0
    for rp in relpaths:
        target = _resolve(root, rp)
        if target == root.resolve():
            raise ValueError("不能删除网盘根目录")
        if target.is_dir():
            shutil.rmtree(target)
            deleted += 1
        elif target.exists():
            target.unlink()
            deleted += 1
    used = refresh_drive_usage(username)
    return {"deleted": deleted, "used_mb": used}


def rename_path(username: str, relpath: str, new_name: str) -> dict:
    """重命名（同目录内），new_name 只允许文件名本身。"""
    if "/" in new_name or "\\" in new_name or new_name in ("", ".", ".."):
        raise ValueError("新名称不能包含路径分隔符")
    root = drive_root_for(username)
    src = _resolve(root, relpath)
    if not src.exists():
        raise FileNotFoundError(f"不存在: {relpath}")
    dst = src.parent / new_name
    if not str(dst.resolve()).startswith(str(root.resolve()) + os.sep):
        raise ValueError("路径越出网盘范围")
    if dst.exists():
        raise FileExistsError(f"目标已存在: {new_name}")
    os.rename(src, dst)
    return {"relpath": normalize_rel(str(dst.relative_to(root.resolve())))}


def mkdir(username: str, relpath: str) -> dict:
    root = drive_root_for(username)
    target = _resolve(root, relpath)
    if target.exists():
        raise FileExistsError(f"已存在: {relpath}")
    target.mkdir(parents=True, exist_ok=False)
    return {"relpath": normalize_rel(relpath)}


def read_preview(username: str, relpath: str, max_bytes: int = PREVIEW_MAX_MB * MB) -> tuple[bytes, str]:
    """读取预览内容（文本或图片），超限拒绝。返回 (内容, mime)。"""
    import mimetypes
    root = drive_root_for(username)
    target = _resolve(root, relpath)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在: {relpath}")
    size = target.stat().st_size
    if size > max_bytes:
        raise ValueError(f"文件过大（预览上限 {max_bytes // MB}MB）")
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return target.read_bytes(), mime


# ── 分享 ────────────────────────────────────────────────────────

def create_share(owner: str, relpath: str, note: str = "",
                 expire_hours: float | None = None) -> dict:
    """创建分享：仅登录用户可通过 /api/drive/shared/{sid} 访问（管理员选定的安全策略）。"""
    root = drive_root_for(owner)
    target = _resolve(root, relpath)
    if not target.exists():
        raise FileNotFoundError(f"不存在: {relpath}")
    expires_at = time.time() + expire_hours * 3600 if expire_hours else None
    return get_db().create_drive_share(owner, normalize_rel(relpath), target.name,
                                       note, expires_at)


def share_is_valid(share: dict) -> tuple[bool, str]:
    if share is None:
        return False, "分享不存在"
    exp = share.get("expires_at")
    if exp and time.time() > float(exp):
        return False, "分享已过期"
    return True, ""


# ── 分片上传与跨会话续传（助手 2026-10-13）──────────────────────────
# 设计要点：
#   1. 会话识别三层回退：路径+大小（最快）→ 内容哈希（改名/移动后仍可命中）→ 新建
#   2. 分片落盘 <user>/drive/.upload/<sid>/<index>.part，**不计入用户配额**
#      （否则半成品会锁死配额，用户既传不完也删不掉）
#   3. complete 时再次校验总大小与配额，超限则拒绝并**保留会话**供用户处理
#   4. 拼接用临时文件 + os.replace 原子改名，失败不留半截目标文件
#   5. 半成品 7 天未更新自动清理；管理端可手动清理

CHUNK_SIZE = 8 * 1024 * 1024        # 8MB/片：公网 0.8MB/s 下单片约 10 秒
DIRECT_MAX = 8 * 1024 * 1024        # ≤8MB 走一次性直传，不切片
RESUME_KEEP_DAYS = 7                # 半成品保留天数
UPLOAD_DIRNAME = ".upload"


def _upload_root(username: str) -> Path:
    root = drive_root_for(username) / UPLOAD_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def chunks_of(upload: dict) -> list[int]:
    """返回该会话已收到的分片序号（升序）。"""
    d = _upload_root(upload["owner"]) / upload["sid"]
    if not d.is_dir():
        return []
    out: list[int] = []
    for f in d.glob("*.part"):
        try:
            out.append(int(f.stem))
        except ValueError:
            continue
    return sorted(out)


def upload_view(u: dict) -> dict:
    """会话的可读视图（供接口与管理端返回）。"""
    got = chunks_of(u)
    return {
        "sid": u["sid"], "owner": u["owner"], "dest_path": u["dest_path"],
        "name": u["name"], "size": u["size"], "sha256": u["sha256"],
        "chunk_size": u["chunk_size"], "total_chunks": u["total_chunks"],
        "uploaded": got, "received": len(got),
        "created_at": u["created_at"], "updated_at": u["updated_at"],
    }


def upload_init(account: dict, dest_path: str, size: int,
                sha256: str = "", chunk_size: int = CHUNK_SIZE) -> dict:
    """创建或复用分片上传会话。

    识别顺序：① 路径+大小 ② 内容哈希 ③ 新建。
    复用时会校验 total_chunks 一致（换了 chunk_size 视作不同会话，避免拼错）。
    """
    username = account["username"]
    rel = normalize_rel(dest_path)
    if not rel:
        raise ValueError("必须提供目标文件名")
    if size <= 0:
        raise ValueError("文件大小非法")
    limit = max_file_bytes()
    if size > limit:
        raise ValueError(f"文件超过单文件上限 {limit // MB}MB")
    _check_quota(account, size)          # 提前拦，别让用户白传一场

    root = drive_root_for(username)
    target = _resolve(root, rel)
    if target.is_dir():
        raise IsADirectoryError("目标是一个目录")
    chunk_size = max(256 * 1024, min(int(chunk_size or CHUNK_SIZE), 64 * MB))
    total = (size + chunk_size - 1) // chunk_size

    db = get_db()
    resumed = False
    upload = db.find_drive_upload(username, rel, size)
    if upload and int(upload["total_chunks"]) == total:
        resumed = True
        if sha256 and not upload["sha256"]:
            db.update_drive_upload_hash(upload["sid"], sha256)   # 补录哈希
            upload = db.get_drive_upload(upload["sid"]) or upload
    else:
        upload = None
        if sha256:
            by_hash = db.find_drive_upload_by_hash(username, sha256)
            if by_hash and int(by_hash["total_chunks"]) == total:
                upload, resumed = by_hash, True
        if upload is None:
            sid = uuid.uuid4().hex
            (root / UPLOAD_DIRNAME / sid).mkdir(parents=True, exist_ok=True)
            upload = db.create_drive_upload(sid, username, rel, target.name,
                                            size, sha256, chunk_size, total)
    db.touch_drive_upload(upload["sid"])
    view = upload_view(upload)
    view["resumed"] = resumed
    view["direct"] = size <= DIRECT_MAX
    return view


def save_chunk(account: dict, sid: str, index: int, data: bytes) -> dict:
    """保存一个分片（幂等：同 index 重传直接覆盖）。"""
    db = get_db()
    upload = db.get_drive_upload(sid)
    if upload is None or upload["owner"] != account["username"]:
        raise PermissionError("上传会话不存在或不属于当前用户")
    total = int(upload["total_chunks"])
    if index < 0 or index >= total:
        raise ValueError(f"分片序号越界（应在 0..{total - 1}）")
    chunk_size = int(upload["chunk_size"])
    body = bytes(data or b"")
    if not body:
        raise ValueError("分片为空")
    if len(body) > chunk_size:
        raise ValueError(f"分片超过 {chunk_size // MB}MB 上限")
    expected = chunk_size if index < total - 1 else int(upload["size"]) - chunk_size * (total - 1)
    if index < total - 1 and len(body) != expected:
        raise ValueError(f"分片大小不符：期望 {expected} 字节，收到 {len(body)}")

    d = _upload_root(upload["owner"]) / sid
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{index}.part.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, d / f"{index}.part")      # 原子落片，失败不留坏片
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    db.touch_drive_upload(sid)
    return {"index": index, "received": len(chunks_of(upload)),
            "total_chunks": total, "bytes": len(body)}


def complete_upload(account: dict, sid: str) -> dict:
    """校验分片齐全 → 顺序拼接 → 原子改名到目标 → 清理会话。"""
    db = get_db()
    upload = db.get_drive_upload(sid)
    if upload is None or upload["owner"] != account["username"]:
        raise PermissionError("上传会话不存在或不属于当前用户")
    total = int(upload["total_chunks"])
    size = int(upload["size"])
    got = set(chunks_of(upload))
    missing = [i for i in range(total) if i not in got]
    if missing:
        raise ValueError(f"分片不完整，缺失 {len(missing)} 片"
                         f"（如 {','.join(str(i) for i in missing[:5])}）")

    acc = db.get_account_by_name(account["username"])
    if acc is None:
        raise PermissionError("账号不存在")
    root = drive_root_for(account["username"])
    target = _resolve(root, upload["dest_path"])
    old_size = target.stat().st_size if target.exists() else 0
    _check_quota(acc, size, old_size)     # 拼接前最后一道配额闸

    d = _upload_root(account["username"]) / sid
    assembled = d / "__assembled.tmp"
    written = 0
    try:
        with open(assembled, "wb") as out:
            for i in range(total):
                part = d / f"{i}.part"
                with open(part, "rb") as fh:
                    shutil.copyfileobj(fh, out, UPLOAD_CHUNK)
                written += part.stat().st_size
            out.flush()
            os.fsync(out.fileno())
        if written != size:
            raise ValueError(f"拼接结果大小不符：期望 {size}，实际 {written}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(assembled, target)     # 原子替换
    except Exception:
        try:
            assembled.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    shutil.rmtree(d, ignore_errors=True)
    db.delete_drive_upload(sid)
    used = refresh_drive_usage(account["username"])
    return {"relpath": upload["dest_path"], "size": written,
            "replaced": old_size > 0, "used_mb": used}


def cancel_upload(account: dict, sid: str) -> dict:
    """放弃上传：删除分片与会话记录。"""
    db = get_db()
    upload = db.get_drive_upload(sid)
    if upload is None or upload["owner"] != account["username"]:
        raise PermissionError("上传会话不存在或不属于当前用户")
    shutil.rmtree(_upload_root(account["username"]) / sid, ignore_errors=True)
    db.delete_drive_upload(sid)
    return {"ok": True, "sid": sid}


def cleanup_expired_uploads(keep_days: float = RESUME_KEEP_DAYS,
                            owner: str | None = None) -> dict:
    """清理超过保留期的半成品（默认 7 天），并回收 DB 无记录的孤儿目录。"""
    db = get_db()
    cutoff = time.time() - keep_days * 86400
    removed, freed, orphans = 0, 0, 0

    for u in db.expired_drive_uploads(cutoff):
        if owner and u["owner"] != owner:
            continue
        d = _upload_root(u["owner"]) / u["sid"]
        if d.is_dir():
            freed += sum(f.stat().st_size for f in d.glob("*.part") if f.is_file())
            shutil.rmtree(d, ignore_errors=True)
        db.delete_drive_upload(u["sid"])
        removed += 1

    users = [owner] if owner else [acc["username"] for acc in db.list_accounts()]
    live = {x["sid"] for x in db.list_drive_uploads(owner)}
    for uname in users:
        updir = DRIVE_USERS_ROOT / uname / DRIVE_DIRNAME / UPLOAD_DIRNAME
        if not updir.is_dir():
            continue
        for child in updir.iterdir():
            if not child.is_dir() or child.name in live:
                continue
            # 空目录（没有任何分片）没有续传价值：中途放弃的会话只会留下它，
            # 无需等保留期，直接回收（2026-10-13 实测发现这类残壳会一直累积）
            has_parts = any(child.glob("*.part"))
            if not has_parts:
                shutil.rmtree(child, ignore_errors=True)
                orphans += 1
                continue
            try:
                if child.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            freed += sum(f.stat().st_size for f in child.glob("*.part") if f.is_file())
            shutil.rmtree(child, ignore_errors=True)
            orphans += 1

    return {"removed": removed, "orphans": orphans, "freed_mb": round(freed / MB, 2)}
