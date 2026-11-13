"""用户沙箱：~/dsh-users/<username>/ 下的受限文件操作。

所有路径参数必须是相对路径，服务端强制限制在用户沙箱根内（防目录穿越）。
配额 1GB 由 accounts.quota_disk_mb 控制，used_disk_mb 定期重算。
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from .db import get_db

SANDBOX_ROOT = Path.home() / "dsh-users"


def sandbox_root_for(username: str) -> Path:
    root = SANDBOX_ROOT / username
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o750)
    return root


def resolve_safe(root: Path, relpath: str) -> Path:
    """把相对路径解析到沙箱内绝对路径，拒绝任何穿越。"""
    rel = PurePosixPath(relpath.strip("/"))
    if any(part in ("..",) for part in rel.parts):
        raise ValueError("路径不允许包含 ..")
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    # 必须比较「路径段」而不是字符串前缀：/x/alice2 也以 /x/alice 开头。
    if target != root_resolved and not str(target).startswith(str(root_resolved) + os.sep):
        raise ValueError("路径越出沙箱范围")
    return target


def sandbox_usage_mb(username: str) -> float:
    root = SANDBOX_ROOT / username
    if not root.exists():
        return 0.0
    total = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    return round(total / 1048576, 2)


def refresh_disk_usage() -> None:
    """重算所有账号的沙箱占用，写入 accounts.used_disk_mb。"""
    db = get_db()
    for acc in db.list_accounts():
        db.update_account_quiet(acc["id"], {"used_disk_mb": sandbox_usage_mb(acc["username"])})


def list_dir(username: str, relpath: str = "") -> list[dict]:
    root = sandbox_root_for(username)
    target = resolve_safe(root, relpath)
    items = []
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        stat = entry.stat()
        items.append({
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
            "size": stat.st_size if entry.is_file() else None,
            "mtime": int(stat.st_mtime),
            "relpath": str((PurePosixPath(relpath) / entry.name)) if relpath else entry.name,
        })
    return items


def read_file(username: str, relpath: str, max_bytes: int = 2 * 1024 * 1024) -> str:
    root = sandbox_root_for(username)
    target = resolve_safe(root, relpath)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在: {relpath}")
    if target.stat().st_size > max_bytes:
        raise ValueError("文件过大（面板预览上限 2MB）")
    return target.read_text(encoding="utf-8", errors="replace")


def write_file(username: str, relpath: str, content: str) -> dict:
    db = get_db()
    acc = db.get_account_by_name(username)
    if acc is None:
        raise PermissionError("账号不存在")
    root = sandbox_root_for(username)
    target = resolve_safe(root, relpath)
    new_size = len(content.encode("utf-8"))
    old_size = target.stat().st_size if target.exists() else 0
    delta_mb = (new_size - old_size) / 1048576
    if acc["used_disk_mb"] + delta_mb > acc["quota_disk_mb"]:
        raise PermissionError(f"磁盘配额不足（已用 {acc['used_disk_mb']}/{acc['quota_disk_mb']} MB）")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    db.update_account_quiet(acc["id"], {"used_disk_mb": sandbox_usage_mb(username)})
    return {"relpath": relpath, "size": new_size}


def delete_path(username: str, relpath: str) -> None:
    import shutil
    root = sandbox_root_for(username)
    target = resolve_safe(root, relpath)
    if target == root:
        raise ValueError("不能删除沙箱根目录")
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    get_db().update_account_quiet(
        get_db().get_account_by_name(username)["id"],
        {"used_disk_mb": sandbox_usage_mb(username)})
