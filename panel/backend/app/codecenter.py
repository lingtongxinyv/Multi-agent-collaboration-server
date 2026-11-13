"""管理端代码中心：白名单仓库的网页 Git、在线编辑、部署流水线、交站娘部署。

安全边界：
- 仓库仅限 REPOS 白名单（本模块唯一事实来源）
- 文件读写路径 resolve 后必须落在对应仓库内，且禁止进入 .git/
- 全部操作写审计日志（main.py 挂载处完成）
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024 * 1024

REPOS: dict[str, Path] = {
    "dsh-suite": Path.home() / "dsh-suite",
    "deepseek-harness": Path.home() / "deepseek-harness",
}

_deploy_lock = threading.Lock()
_deploy_jobs: dict[str, dict] = {}
_git_locks: dict[str, threading.Lock] = {name: threading.Lock() for name in REPOS}


class CodeError(Exception):
    pass


def _repo_dir(repo: str) -> Path:
    path = REPOS.get(repo)
    if path is None or not path.is_dir():
        raise CodeError(f"未知仓库: {repo}")
    return path


def _safe_path(repo: str, rel: str) -> Path:
    """解析仓库内相对路径，拒绝穿越与 .git 内部。"""
    root = _repo_dir(repo).resolve()
    rel = (rel or "").strip().lstrip("/")
    target = (root / rel).resolve() if rel else root
    if target != root and not str(target).startswith(str(root) + os.sep):
        raise CodeError("路径越界")
    if ".git" in target.relative_to(root).parts:
        raise CodeError("禁止访问 .git 内部")
    return target


def _node_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = f"{Path.home() / '.local/nodejs/bin'}:" + env.get("PATH", "")
    return env


def _git(repo: str, *args: str, timeout: int = 30) -> str:
    with _git_locks[repo]:
        proc = subprocess.run(
            ["git", "-C", str(_repo_dir(repo)), "--no-pager", *args],
            capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise CodeError(f"git {' '.join(args[:2])} 失败: {proc.stderr.strip()[:400]}")
    return proc.stdout


def repo_overview() -> list[dict]:
    out = []
    for name, path in REPOS.items():
        if not path.is_dir():
            out.append({"name": name, "path": str(path), "exists": False})
            continue
        try:
            branch_line = _git(name, "status", "--porcelain=v1", "-b").splitlines()[0]
            branch = branch_line.replace("## ", "").split("...")[0].split()[0]
            dirty = sum(1 for line in _git(name, "status", "--porcelain=v1").splitlines() if line.strip())
            remote = _git(name, "remote", "get-url", "origin").strip()
        except CodeError as exc:
            out.append({"name": name, "path": str(path), "exists": True, "error": str(exc)})
            continue
        out.append({"name": name, "path": str(path), "exists": True,
                    "branch": branch, "dirty": dirty, "remote": remote or None})
    return out


def status(repo: str) -> dict:
    text = _git(repo, "status", "--porcelain=v1", "-b")
    lines = text.splitlines()
    branch_line = lines[0].replace("## ", "") if lines else ""
    ahead = behind = 0
    for part in branch_line.split(","):
        part = part.strip()
        if part.startswith("ahead "):
            ahead = int(part[6:].split("[")[0])
        elif part.startswith("behind "):
            behind = int(part[7:].split("[")[0])
    entries = []
    for line in lines[1:]:
        if len(line) < 4:
            continue
        entries.append({"x": line[0], "y": line[1], "path": line[3:]})
    return {"branch": branch_line.split("...")[0].split("[")[0].strip(),
            "ahead": ahead, "behind": behind, "entries": entries}


def diff(repo: str, rel: str) -> str:
    path = _safe_path(repo, rel)
    rel_in_repo = str(path.relative_to(_repo_dir(repo).resolve()))
    tracked = _git(repo, "ls-files", "--", rel_in_repo).strip()
    if tracked:
        return _git(repo, "diff", "HEAD", "--", rel_in_repo) or "(无改动)"
    proc = subprocess.run(
        ["git", "-C", str(_repo_dir(repo)), "--no-pager", "diff", "--no-index", "--", "/dev/null", rel_in_repo],
        capture_output=True, text=True, timeout=30)
    return proc.stdout or "(新文件且无内容差异)"


def log(repo: str, limit: int = 30) -> list[dict]:
    text = _git(repo, "log", f"-n{min(limit, 100)}", "--date=iso",
                "--pretty=format:%h%x09%ad%x09%s")
    items = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            items.append({"hash": parts[0], "date": parts[1], "msg": parts[2]})
    return items


def commit(repo: str, message: str, paths: list[str]) -> str:
    if not message.strip():
        raise CodeError("提交信息不能为空")
    if not paths:
        raise CodeError("未选择文件")
    rels = [str(_safe_path(repo, p).relative_to(_repo_dir(repo).resolve())) for p in paths]
    _git(repo, "add", "--", *rels)
    _git(repo, "commit", "-m", message.strip())
    return _git(repo, "rev-parse", "--short", "HEAD").strip()


def push(repo: str) -> str:
    out = _git(repo, "push", timeout=120)
    return out.strip() or "已推送"


def pull(repo: str) -> str:
    out = _git(repo, "pull", "--ff-only", timeout=120)
    return out.strip() or "已是最新"


def tree(repo: str, rel: str) -> dict:
    base = _safe_path(repo, rel)
    if not base.is_dir():
        raise CodeError("不是目录")
    st = {e["path"]: (e["x"], e["y"]) for e in status(repo)["entries"]}
    prefix = ""
    if base != _repo_dir(repo).resolve():
        prefix = str(base.relative_to(_repo_dir(repo).resolve())) + "/"
    dirs: list[str] = []
    files: list[dict] = []
    try:
        for item in sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if item.name == ".git":
                continue
            if item.is_dir():
                dirs.append(item.name)
            else:
                rel_key = prefix + item.name
                xy = st.get(rel_key)
                try:
                    size = item.stat().st_size
                except OSError:
                    size = -1
                files.append({"name": item.name, "size": size, "state": xy})
    except PermissionError as exc:
        raise CodeError(f"无权限读取目录: {exc}")
    return {"dirs": dirs, "files": files}


def read_file(repo: str, rel: str) -> str:
    path = _safe_path(repo, rel)
    if not path.is_file():
        raise CodeError("不是文件")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise CodeError("文件超过 2MB，请用 SSH 编辑")
    if b"\0" in raw[:8192]:
        raise CodeError("二进制文件不支持在线编辑")
    return raw.decode("utf-8", errors="replace")


def write_file(repo: str, rel: str, content: str) -> None:
    path = _safe_path(repo, rel)
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise CodeError("内容超过 2MB")
    if path.exists() and not path.is_file():
        raise CodeError("目标不是普通文件")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── 部署流水线 ──────────────────────────────────────────────────

PIPELINES: dict[str, dict[str, list[list[str]]]] = {
    "dsh-suite": {
        # 注意：sudoers 白名单只授权带 .service 后缀的写法，裸名会被 sudo 拒绝
        # （interactive authentication is required）→ 部署流水线静默失败。
        "restart": [["重启面板服务", ["sudo", "-n", "systemctl", "restart", "dsh-panel.service"]]],
        "full": [
            ["构建前端 (vite)", ["bash", "-lc", "cd ~/dsh-suite/panel/frontend && npm run build"]],
            ["重启面板服务", ["sudo", "-n", "systemctl", "restart", "dsh-panel.service"]],
            ["重启 MCP Hub", ["sudo", "-n", "systemctl", "restart", "dsh-hub.service"]],
        ],
    },
    "deepseek-harness": {
        "restart": [["重启 DSH 服务", ["sudo", "-n", "systemctl", "restart", "dsh-admin.service"]]],
        "full": [
            ["构建 client 库", ["bash", "-lc", "cd ~/deepseek-harness && pnpm build:lib:client"]],
            ["构建 web 前端", ["bash", "-lc", "cd ~/deepseek-harness && pnpm build:web"]],
            ["重启 DSH 服务", ["sudo", "-n", "systemctl", "restart", "dsh-admin.service"]],
        ],
    },
}


def deploy(repo: str, mode: str) -> str:
    steps = PIPELINES.get(repo, {}).get(mode)
    if not steps:
        raise CodeError(f"不支持的动作: {repo}/{mode}")
    with _deploy_lock:
        running = [j for j in _deploy_jobs.values() if j["status"] == "running" and j["repo"] == repo]
        if running:
            raise CodeError("该仓库已有部署任务在运行")
        job_id = time.strftime("deploy-%Y%m%d-%H%M%S")
        job = {"id": job_id, "repo": repo, "mode": mode, "status": "running",
               "logs": [], "steps": [s[0] for s in steps], "started": time.time()}
        _deploy_jobs[job_id] = job
        # 只保留最近 10 个任务
        for old in sorted(_deploy_jobs)[:-10]:
            if _deploy_jobs[old]["status"] != "running":
                del _deploy_jobs[old]
    thread = threading.Thread(target=_run_deploy, args=(job_id, steps), daemon=True)
    thread.start()
    return job_id


def _run_deploy(job_id: str, steps: list[list[str]]) -> None:
    job = _deploy_jobs[job_id]
    def log(msg: str) -> None:
        job["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    try:
        for name, cmd in steps:
            job["current"] = name
            log(f"▶ {name}")
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=900, env=_node_env())
            tail = (proc.stdout or "").strip().splitlines()[-5:]
            for line in tail:
                log(f"  {line}")
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
                for line in err:
                    log(f"  ! {line}")
                raise CodeError(f"{name} 失败 (exit {proc.returncode})")
            log(f"✔ {name} 完成")
        job["status"] = "done"
        job["current"] = None
        log("部署成功")
    except Exception as exc:  # noqa: BLE001 —— 线程内兜底，记录失败状态
        job["status"] = "failed"
        job["current"] = None
        log(f"✘ 部署失败: {exc}")


def deploy_status(job_id: str) -> dict:
    job = _deploy_jobs.get(job_id)
    if job is None:
        raise CodeError("任务不存在")
    return {k: job[k] for k in ("id", "repo", "mode", "status", "logs", "steps", "current")}


def deploy_history() -> list[dict]:
    return [{k: j[k] for k in ("id", "repo", "mode", "status", "started")}
            for j in sorted(_deploy_jobs.values(), key=lambda x: x["started"], reverse=True)]


# ── 交站娘部署：任务载荷组装 ────────────────────────────────────

MAX_PAYLOAD = 60_000  # 发给站娘的文本总量上限（字符）


def build_assistant_payload(repo: str, paths: list[str], note: str) -> str:
    """把指定文件内容组装成站娘可执行的任务文本。"""
    root = _repo_dir(repo).resolve()
    sections: list[str] = []
    used = 0
    for rel in paths:
        path = _safe_path(repo, rel)
        rel_in_repo = str(path.relative_to(root))
        if not path.is_file():
            raise CodeError(f"不是文件: {rel_in_repo}")
        raw = path.read_bytes()
        if b"\0" in raw[:8192]:
            raise CodeError(f"二进制文件无法发送: {rel_in_repo}")
        content = raw.decode("utf-8", errors="replace")
        if used + len(content) > MAX_PAYLOAD:
            content = content[: max(0, MAX_PAYLOAD - used)] + "\n…(超长截断)"
        used += len(content)
        sections.append(f"--- 文件: {rel_in_repo} ---\n{content}")
        if used >= MAX_PAYLOAD:
            break
    head = (
        f"以下是仓库 {repo}（位于服务器 ~/deepseek-harness 或 ~/dsh-suite 对应位置）"
        "的相关文件，请根据说明在服务器上完成部署/修改并执行需要的构建与重启。\n"
    )
    if note.strip():
        head += f"任务说明：{note.strip()}\n"
    return head + "\n\n".join(sections)
