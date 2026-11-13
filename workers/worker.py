#!/usr/bin/env python3
"""DSH Suite 协作 Worker 客户端（跨平台：Windows / Linux / Termux）。

认证两种方式（管理员面板支持任一）：
1. 面板直接添加 worker 拿到 token → 配置到 config.json
2. 自助注册（register）→ 管理员在面板审批 → 用返回的 token

配置 config.json（同目录）：
{
  "hub": "https://agent.example.com/mcp-hub",
  "name": "win-desk-01",
  "token": "",
  "capabilities": ["windows", "coding", "browser"],
  "workdir": "./workdir",
  "poll_interval": 10
}

任务执行：claim 后把 {task.json, prompt.txt} 写进 workdir/tasks/<id>/，
调用同目录 handler.py（可自行替换业务逻辑），把 handler 输出作为结果回传。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import urllib.error
import urllib.request

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
HANDLER = Path(__file__).resolve().parent / "handler.py"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps({
            "hub": "https://agent.example.com/mcp-hub",
            "name": "unnamed-worker",
            "token": "",
            "capabilities": [],
            "workdir": "./workdir",
            "poll_interval": 10,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[worker] 已生成默认配置 {CONFIG_PATH}，请填写后重新运行")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def api(config: dict, path: str, body: dict | None = None) -> dict:
    url = config["hub"].rstrip("/") + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if config.get("token"):
        req.add_header("X-Worker-Auth", f"{config['name']}:{config['token']}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return {"error": f"HTTP {exc.code}"}


def register(config: dict) -> None:
    result = api(config, "/worker/register",
                 {"name": config["name"], "capabilities": config.get("capabilities", [])})
    if "error" in result:
        print(f"[worker] 注册失败: {result['error']}")
        sys.exit(1)
    config["token"] = result["token"]
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[worker] 注册完成，token 已写入配置。approved={result['approved']}")
    if not result["approved"]:
        print("[worker] 等待管理员在面板审批后即可领任务")


def run_handler(task_dir: Path) -> str:
    """调用 handler.py <task_dir>，返回 stdout 作为结果。"""
    if not HANDLER.exists():
        HANDLER.write_text(
            "# 任务处理器：读取 task.json，输出结果到 stdout\n"
            "import json, sys\n"
            "from pathlib import Path\n"
            "task = json.loads((Path(sys.argv[1]) / 'task.json').read_text(encoding='utf-8'))\n"
            "print(f\"已收到任务: {task['title']}，请实现 handler.py 的业务逻辑\")\n",
            encoding="utf-8")
    proc = subprocess.run([sys.executable, str(HANDLER), str(task_dir)],
                          capture_output=True, text=True, timeout=1800, cwd=str(task_dir))
    output = proc.stdout.strip()
    if proc.returncode != 0:
        raise RuntimeError(f"handler 退出码 {proc.returncode}: {proc.stderr.strip()[-2000:]}")
    return output or "(handler 无输出)"


def process_one(config: dict, task: dict) -> None:
    task_id = task["id"]
    task_dir = Path(config.get("workdir", "./workdir")).expanduser() / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    (task_dir / "prompt.txt").write_text(task.get("prompt", ""), encoding="utf-8")
    print(f"[worker] 执行任务 {task_id}: {task['title']}")
    claim = api(config, "/worker/claim", {"task_id": task_id})
    if "task" not in claim:
        print(f"[worker] 认领失败: {claim.get('error')}")
        return
    try:
        result = run_handler(task_dir)
        api(config, "/worker/complete",
            {"task_id": task_id, "result": result, "tokens_used": 0})
        print(f"[worker] 任务 {task_id} 完成")
    except Exception as exc:
        api(config, "/worker/fail", {"task_id": task_id, "error": str(exc)[:2000]})
        print(f"[worker] 任务 {task_id} 失败: {exc}")


def main() -> None:
    config = load_config()
    if len(sys.argv) > 1 and sys.argv[1] == "register":
        register(config)
        return
    if not config.get("token"):
        print("[worker] 尚无 token：先运行 `python worker.py register`，或在面板添加 worker 后把 token 填入 config.json")
        sys.exit(1)
    print(f"[worker] {config['name']} 启动，hub={config['hub']}，轮询 {config.get('poll_interval', 10)}s")
    while True:
        try:
            api(config, "/worker/heartbeat")
            poll = api(config, "/worker/poll")
            tasks = poll.get("tasks", [])
            if tasks:
                process_one(config, tasks[0])
                continue  # 领到任务后立即再查
        except KeyboardInterrupt:
            print("[worker] 退出")
            break
        except Exception as exc:
            print(f"[worker] 轮询异常: {exc}")
        time.sleep(config.get("poll_interval", 10))


if __name__ == "__main__":
    main()
