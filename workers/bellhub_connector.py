#!/usr/bin/env python3
"""铃枢连接器 BellHub Connector —— 即装即用的一体化接入端。

设计目标（管理员 2026-10-14 确认）：
  · 单文件、零第三方依赖 → 可打包成 Windows .exe / Linux 二进制，双击即用；
  · 浏览器授权配对（设备码式）→ 用户不手抄密钥、管理员不发密钥；
  · 队员 = 本机 AI 应用：站娘派活给本机 CLI 型 AI，**连接器自己不执行用户命令**；
  · 分级闸门：读/查类直接交给本机 AI；涉及写文件/跑命令/安装的活，先在本机弹确认。

流程：
  1) bellhub-connector pair            首次配对（浏览器里点一下"确认"）
  2) bellhub-connector detect          列出本机可编入队伍的 AI 应用
  3) bellhub-connector run             常驻：心跳 → 领活 → 交给本机 AI → 回报
  4) bellhub-connector autostart       设为登录即启（一次安装、之后全自动）
  5) bellhub-connector print-config trae [--write]
                                       打印（或直接写入）"让 IDE 反向调用铃枢"的配置
  6) bellhub-connector version --check 版本自检，对不上就告诉你去哪儿拿新版
  7) bellhub-connector update [--yes] 可选自动升级：校验 sha256 → 备份 → 替换 → 回读自检
  8) bellhub-connector doctor        一条命令自检：配对/站点/领活通道/本机队员/自启

安全边界（务必知情）：
  · 本文件不含任何密钥；配对换来的凭据只存在本机 ~/.bellhub/connector.json（0600）。
  · 连接器不直接跑用户命令；它把 prompt 交给本机 AI 应用，由该 AI 自己完成。
  · 高危活默认需本机确认；无终端时自动拒绝并回传原因，绝不静默执行。

扩展（不必等官方适配）：在 connector.json 里加自定义 CLI 队员 ——
  "custom_agents": [
    {"key": "myai", "label": "我的 AI", "exe": "myai",
     "cmd": "myai chat {prompt}", "take": ["general", "code"]}
  ]
只要该命令"给一个 prompt 就能跑完并退出"即可。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

VERSION = "2026-10-19.6"
DEFAULT_BASE = "https://agent.example.com"
CONFIG_DIR = Path(os.environ.get("BELLHUB_HOME", str(Path.home() / ".bellhub")))
CONFIG_PATH = CONFIG_DIR / "connector.json"
STATE_PATH = CONFIG_DIR / "state.json"
UA = f"BellHubConnector/{VERSION}"

# ── 本机 AI 应用适配器 ───────────────────────────────────────────
# 每项：命令名 → 适配器。command 为 None 表示需要用户自行配置。
# take: 该适配器能吃的活（能力标签），用于和站娘派下来的 capability 匹配。
ADAPTERS: dict[str, dict] = {
    "claude": {
        "label": "Claude Code",
        "exe": ["claude"],
        # dsh_headless 只属于 DeepSeek Harness：Claude Code 抢了它只会让
        # 派给 DSH 的活跑到 claude 上（2026-10-19 实测：一句 chat 探针被 claude 接走并报未登录）
        "take": ["code", "general", "write"],
        "cmd": ["claude", "-p", "{prompt}"],
        "note": "命令行型，可被派活；需先在本机 claude 登录。",
    },
    "codex": {
        "label": "Codex CLI",
        "exe": ["codex"],
        "take": ["code", "general"],
        "cmd": ["codex", "exec", "{prompt}"],
        "note": "免费版易在等 stdin 时挂住；本连接器已强制 DEVNULL + 超时。",
    },
    "dsh": {
        "label": "DeepSeek Harness",
        "exe": ["dsh"],
        "take": ["code", "general", "dsh_headless", "write"],
        # 2026-10-19 核实：`dsh headless "任务"` 才是无头一次性执行；
        # 原来的 `dsh --profile headless` 是错的（headless 是子命令，不是 profile）
        "cmd": ["dsh", "headless", "{prompt}"],
        # 桌面版/旧版 CLI 要的是 --profile；desktop-01 实测报「--profile <name> is required」
        "cmd_fallbacks": [["dsh", "--profile", "headless", "{prompt}"]],
        "note": "铃枢自家队员，headless 模式可直接被派活。",
    },
    "ollama": {
        "label": "Ollama（本地模型，轻量活）",
        "exe": [],
        "take": ["chat", "summary", "general"],
        "http": "http://127.0.0.1:11434",
        "model": "qwen3:1.7b",
        "note": "HTTP 型；显存小的机器只建议轻量模型。",
    },
    "openclaw": {
        "label": "OpenClaw",
        "exe": ["openclaw"],
        "take": ["code", "general", "write"],
        # 一次性回合：openclaw agent --message "任务"（官方 CLI，脚本里就这么写）
        "cmd": ["openclaw", "agent", "--message", "{prompt}"],
        "note": "开源个人 AI 代理；走 --message 单次回合，需本机已配好 agent 与模型。",
    },
    "aider": {
        "label": "Aider",
        "exe": ["aider"],
        "take": ["code", "write", "general"],
        "cmd": ["aider", "--message", "{prompt}", "--yes"],
        "note": "官方 scripting 写法（--message/--yes）；在 git 仓库目录里效果最好。",
    },
    "gemini": {
        "label": "Gemini CLI",
        "exe": ["gemini"],
        "take": ["code", "general", "write"],
        "cmd": ["gemini", "-p", "{prompt}"],
        "note": "-p 才是非交互模式；需先在本机完成 Google 账号授权。",
    },
    "opencode": {
        "label": "OpenCode",
        "exe": ["opencode"],
        "take": ["code", "general", "write"],
        "cmd": ["opencode", "-p", "{prompt}"],
        "note": "非交互：-p/--prompt，结果打到 stdout 后退出。",
    },
}

# 高危关键词（启发式，不是沙箱）：命中即要求本机确认
RISKY = (
    # 只留"真会动系统/丢数据"的意图词。2026-10-19：原先含 执行/运行/命令/脚本/写入/
    # 保存到/服务 与裸 ">"，导致「不要执行任何命令」这类普通派活也被判高危 →
    # 弹本机确认 → 无人值守时直接卡死链路（当晚实测）。
    "删除", "删掉", "清空", "卸载", "格式化", "覆盖", "替换",
    "安装", "注册表", "计划任务", "开机启动", "关机", "重启",
    "rm ", "del ", "install", "pip install", "npm install",
    "curl ", "wget ", "chmod", "sudo", "shutdown", "reboot", "taskkill",
    "reg add", "mkfs",
)


def apply_custom_adapters(cfg: dict) -> list[str]:
    """把用户在 connector.json 里定义的 CLI 队员并入适配器表。

    任何"给一个 prompt 就能跑完并退出"的命令行 AI 都能接进来，不必等官方适配：

        "custom_agents": [
          {"key": "myai", "label": "我的 AI", "exe": "myai",
           "cmd": "myai chat {prompt}", "take": ["general", "code"]}
        ]

    返回本次新登记的 key 列表（用于自检打印）。
    """
    added: list[str] = []
    for spec in cfg.get("custom_agents") or []:
        if not isinstance(spec, dict):
            continue
        key = str(spec.get("key") or "").strip()[:24]
        cmd = str(spec.get("cmd") or "").strip()
        if not key or key in ADAPTERS or "{prompt}" not in cmd:
            continue
        parts = cmd.split()
        ADAPTERS[key] = {
            "label": str(spec.get("label") or key)[:40],
            "exe": [str(spec.get("exe") or parts[0])],
            "take": [str(t) for t in (spec.get("take") or ["general"])][:8],
            "cmd": parts,
            "note": str(spec.get("note") or "自定义适配器（来自 connector.json）")[:120],
        }
        added.append(key)
    return added


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── HTTP ────────────────────────────────────────────────────────

def http(method: str, base: str, path: str, body: dict | None = None,
         headers: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    """极简 JSON 请求；把 HTTP 错误也当"有内容的结果"返回，便于自证式报错。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:300]}
    except urllib.error.URLError as exc:
        return 0, {"error": f"网络不可达: {exc.reason}"}


def http_bytes(method: str, base: str, path: str, headers: dict | None = None,
               timeout: int = 180) -> tuple[int, bytes, dict]:
    """取原始字节（下载更新包用），同时把响应头小写化返回，便于读 sha256。"""
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in exc.headers.items()}
    except urllib.error.URLError as exc:
        return 0, str(getattr(exc, "reason", exc)).encode(), {}


# ── 配置与状态 ───────────────────────────────────────────────────

def load_json(path: Path, default: dict) -> dict:
    if not path.is_file():
        return default
    try:
        # 兼容 Windows 记事本写出的 UTF-8 BOM / UTF-16
        for enc in ("utf-8-sig", "utf-16", "gbk"):
            try:
                return json.loads(path.read_text(encoding=enc))
            except UnicodeError:
                continue
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"配置文件语法错误：{path}\n  {exc}\n"
                    f"  提示：Windows 记事本另存易产生 BOM；用本连接器自身的命令写配置更稳。")
    except OSError as exc:
        raise SystemExit(f"读取配置失败：{path}（{exc}）")
    return default


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_config() -> dict:
    cfg = load_json(CONFIG_PATH, {})
    cfg.setdefault("base_url", DEFAULT_BASE)
    cfg.setdefault("name", socket.gethostname().split(".")[0][:32] or "connector")
    cfg.setdefault("enabled", [])
    cfg.setdefault("auto_approve_high_risk", False)
    cfg.setdefault("confirm_timeout", 120)
    cfg.setdefault("heartbeat", 15)
    return cfg


# ── 本机 AI 探测 ─────────────────────────────────────────────────

def detect() -> dict[str, dict]:
    """返回本机可用的 AI 应用：{key: {label, kind, cmd|model, note}}。"""
    found: dict[str, dict] = {}
    for key, a in ADAPTERS.items():
        if a.get("http"):
            st, _ = http("GET", a["http"], "/api/tags", timeout=3)
            if st == 200:
                found[key] = {"label": a["label"], "kind": "http", "model": a["model"],
                              "note": a["note"], "take": a["take"]}
            continue
        for exe in a["exe"]:
            path = shutil.which(exe)
            if path:
                found[key] = {"label": a["label"], "kind": "cli", "path": path,
                              "note": a["note"], "take": a["take"]}
                break
    return found


def capabilities_payload(found: dict[str, dict]) -> list[dict]:
    return [{"type": t, "label": a["label"], "app": k}
            for k, a in found.items() for t in a["take"]]


# ── 配对（浏览器授权）────────────────────────────────────────────

def cmd_pair(args: argparse.Namespace) -> int:
    base = args.base or load_config()["base_url"]
    cfg = load_config()
    apply_custom_adapters(cfg)          # 先并入用户自定义的 CLI 队员
    name = args.name or cfg["name"]
    found = detect()
    print(f"铃枢连接器 {VERSION}")
    print(f"  站点    : {base}")
    print(f"  本机名  : {name}")
    print(f"  检测到  : {', '.join(a['label'] for a in found.values()) or '（无，稍后可引导安装）'}")

    st, res = http("POST", base, "/api/link/device-code", {
        "name": name, "host": socket.gethostname(),
        "platform": f"{platform.system().lower()}-{platform.machine()}",
        "capabilities": capabilities_payload(found)}, timeout=20)
    if st != 200:
        print(f"✗ 配对请求失败（HTTP {st}）：{res.get('detail') or res}")
        return 2

    code, dc = res["user_code"], res["device_code"]
    url = base.rstrip("/") + res["verification_uri"]
    print()
    print("┌" + "─" * 52)
    print(f"│ 请在浏览器里确认：这台电脑接入你的铃枢账号")
    print(f"│   配对码： {code}")
    print(f"│   地址  ： {url}")
    print("└" + "─" * 52)
    if not args.no_browser:
        try:
            webbrowser.open(url)
            print("（已尝试自动打开浏览器；打不开就手动复制上面的地址）")
        except Exception:
            pass

    deadline = time.time() + max(60, int(res.get("expires_in", 900)))
    interval = max(2, int(res.get("interval", 3)))
    print("\n等待你在浏览器里点「确认」…（Ctrl+C 取消）")
    while time.time() < deadline:
        time.sleep(interval)
        st, poll = http("GET", base, f"/api/link/device-code/{dc}", timeout=20)
        status = poll.get("status")
        if status == "approved" and poll.get("token"):
            cfg.update({"base_url": base, "name": poll.get("agent_name", name),
                        "token": poll["token"], "username": poll.get("username", ""),
                        "agent_id": poll.get("agent_id", ""), "enabled": list(found)})
            save_json(CONFIG_PATH, cfg)
            print(f"\n✅ 已接入：队员名「{cfg['name']}」，归属账号 {cfg['username']}")
            print(f"   凭据已写入 {CONFIG_PATH}（0600，不外传）")
            if not found:
                print("\n⚠ 本机还没检测到可被派活的 AI 应用。建议装一个（任选）：")
                print("   · Claude Code：命令行 AI，装好后 `claude` 登录")
                print("   · Ollama：本地模型，推荐 qwen3:1.7b（约 1.4GB，4GB 显存够用）")
                print("   装完重新运行 `bellhub-connector detect` 即可。")
            print("\n下一步：`bellhub-connector run` 让它常驻（可加开机自启）。")
            return 0
        if status in ("denied", "expired", "consumed"):
            print(f"\n✗ 配对未完成（状态：{status}）。重新运行 pair 再试。")
            return 3
        print("  · 仍在等待确认…")
    print("\n✗ 配对码已过期，请重新运行 pair。")
    return 4


# ── 派活：本机 AI 执行 ────────────────────────────────────────────

def risk_of(prompt: str) -> str:
    low = prompt.lower()
    return "high" if any(k in low for k in RISKY) else "low"


def read_line_timeout(timeout: int) -> str | None:
    """带超时读一行；超时返回 None。Windows 用 msvcrt，POSIX 用 select。

    2026-10-19：原来 confirm_on_host 收下 timeout 却直接 input()，无人值守时
    无限阻塞 —— 心跳随之停摆、hub 判离线、任务永远 running。这里补上真正的超时。
    """
    timeout = max(1, int(timeout))
    if os.name == "nt":
        import msvcrt
        buf: list[str] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    print()
                    return "".join(buf)
                if ch == "\x1b":                    # ESC = 立即拒绝
                    print()
                    return "n"
                if ch == "\x08":
                    if buf:
                        buf.pop()
                        sys.stdout.write("\b \b")
                        sys.stdout.flush()
                    continue
                if ch in ("\x00", "\xe0"):          # 方向键等两字节键，吞掉
                    msvcrt.getwch()
                    continue
                buf.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
            else:
                time.sleep(0.05)
        print()
        return None
    import select
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        return None
    if not ready:
        return None
    return sys.stdin.readline().rstrip("\r\n")


def confirm_on_host(prompt: str, timeout: int) -> tuple[bool, str]:
    """本机确认闸门。无 TTY 时拒绝（绝不静默执行），有 TTY 也最多等 timeout 秒。"""
    if not sys.stdin or not sys.stdin.isatty():
        return False, "本机无人值守（无终端），高危任务自动拒绝"
    print("\n" + "!" * 60)
    print("站娘请求在这台电脑上执行一项可能改动系统的任务：")
    print("  " + prompt.strip().replace("\n", "\n  ")[:600])
    print("!" * 60)
    print(f"（{max(1, int(timeout))}s 内不回答 = 自动拒绝，不会拖住心跳）")
    try:
        ans = read_line_timeout(timeout)
    except (KeyboardInterrupt, EOFError):
        print()
        return False, "确认输入中断，已拒绝"
    if ans is None:
        return False, f"本机 {int(timeout)}s 内无人确认，已按拒绝处理"
    ans = ans.strip().lower()
    if ans == "y":
        return True, ""
    if ans == "a":
        return True, "allow_session"
    return False, "用户在本机拒绝了该任务"


class SessionAllow:
    def __init__(self) -> None:
        self.allow = False


SESSION = SessionAllow()


def _is_usage_error(text: str) -> bool:
    """CLI 的"用法不对"特征（不是任务失败）。"""
    low = (text or "").lower()
    marks = ("--profile <name> is required", "unknown command", "unrecognized",
             "unknown option", "invalid option", "usage:", "命令不存在", "unexpected argument")
    return any(m in low for m in marks)


def run_local_ai(app_key: str, prompt: str, cfg: dict, timeout: int = 900) -> tuple[bool, str]:
    """把活交给本机 AI 应用执行；连接器本身不解释用户命令。"""
    a = ADAPTERS.get(app_key)
    if a is None:
        return False, f"未配置的队员类型：{app_key}"
    if a.get("http"):
        st, res = http("POST", a["http"], "/api/generate", {
            "model": cfg.get("ollama_model", a["model"]), "prompt": prompt, "stream": False},
            timeout=timeout)
        if st != 200:
            return False, f"Ollama 调用失败（HTTP {st}）：{res}"
        return True, str(res.get("response", ""))[:20000]
    # 命令行候选：主写法 → 备用写法。DSH 各版本对"无头执行"的写法不一致
    # （新版是 `dsh headless`，桌面版/旧版要 `dsh --profile <name>`），
    # connector.json 里还可以用 dsh_cmd 直接给完整参数表。
    override_cmd = cfg.get(f"{app_key}_cmd")
    base = override_cmd if isinstance(override_cmd, list) and override_cmd else a["cmd"]
    candidates = [base] + [c for c in (a.get("cmd_fallbacks") or []) if list(c) != list(base)]
    # 子进程的工作目录：默认连接器自己所在目录。
    # 2026-10-19 desktop-01 实测：以前固定用 home（C:\Users\16650），而 TEMP 正是
    # C:\Users\16650\AppData\Local\Temp —— DSH 的沙箱要求"临时目录必须在工作区之外"，
    # 于是所有 shell 调用在真正执行前就被拒（Windows ACL temp root must be outside the workspace）。
    workdir = str(cfg.get("workdir") or Path(__file__).resolve().parent)
    if not Path(workdir).is_dir():
        workdir = str(Path.home())
    last_err = ""
    for idx, parts in enumerate(candidates):
        flat = " ".join((prompt or "").splitlines()) or prompt
        cmd = [flat if part == "{prompt}" else part for part in parts]
        # 桌面版/绿色版可能不在 PATH：connector.json 里 dsh_exe / claude_exe 指定绝对路径
        override = (cfg.get(f"{app_key}_exe") or "").strip()
        if override:
            cmd[0] = override
        # Windows：npm 装出来的 CLI 是 dsh.cmd / dsh.ps1，甚至只有无扩展名的 bash 垫片。
        # shutil.which() 会按 PATHEXT 找到它（所以 detect 认为可用），但 subprocess 直接
        # CreateProcess("dsh") 不吃 PATHEXT → FileNotFoundError「命令不存在：dsh」。
        resolved = shutil.which(cmd[0])
        if resolved:
            low = resolved.lower()
            if os.name != "nt":
                cmd[0] = resolved             # POSIX：extensionless 脚本本来就能直接跑
            elif low.endswith((".cmd", ".bat")):
                cmd = ["cmd", "/c", resolved, *cmd[1:]]
            elif low.endswith(".ps1"):
                cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                       "-File", resolved, *cmd[1:]]
            elif low.endswith((".exe", ".com")):
                cmd[0] = resolved
            else:                              # 无扩展名垫片 → 找同名 .cmd
                alt = shutil.which(resolved + ".cmd")
                if not alt:
                    return False, (f"找到 {resolved}，但它在 Windows 上不是可执行入口"
                                   f"（没有同名 .cmd/.exe）")
                cmd = ["cmd", "/c", alt, *cmd[1:]]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,  # 铁律 #3：绝不留给子进程等 stdin 的机会
                timeout=timeout, cwd=workdir)
        except FileNotFoundError:
            return False, f"命令不存在：{cmd[0]}（可能未安装或不在 PATH）"
        except subprocess.TimeoutExpired:
            return False, f"本机 AI 执行超时（>{timeout}s），已终止"
        out = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        if proc.returncode == 0:
            return True, out[:20000] or "（无输出）"
        last_err = f"{a['label']} 退出码 {proc.returncode}：{out[:2000]}"
        # 只有"参数不对/子命令不认识"这种用法错误才换下一种写法；
        # 任务本身失败不重试，免得同一个活跑两遍。
        if idx + 1 < len(candidates) and _is_usage_error(out):
            continue
        break
    return False, last_err or "本机 AI 没有可用写法"


def pick_adapter(capability: str, enabled: list[str], found: dict[str, dict]) -> str | None:
    """按能力标签挑本机队员；没有精确匹配就退到 enabled 里的第一个。"""
    for key in enabled:
        if key in found and capability and capability in found[key].get("take", []):
            return key
    for key in enabled:
        if key in found:
            return key
    return None


def handle_subtask(sub: dict, cfg: dict, found: dict[str, dict], authed: dict) -> None:
    sid, prompt = sub["id"], sub.get("prompt") or sub.get("title") or ""
    cap = sub.get("capability", "")
    log(f"领到任务 #{sid}｜{sub.get('title')}｜能力={cap or 'general'}")
    app_key = pick_adapter(cap, cfg.get("enabled") or [], found)
    if app_key is None:
        msg = ("本机没有可用的 AI 队员。请在装有 Claude Code / Codex / DSH / Ollama 的机器上运行，"
               "或先安装其中一个后重跑 detect。")
        log("✗ " + msg)
        post(authed, "POST", "/agent/fail", {"subtask_id": sid, "error": msg})
        return

    risky = risk_of(prompt)
    if risky == "high" and not cfg.get("auto_approve_high_risk") and not SESSION.allow:
        ok, note = confirm_on_host(prompt, int(cfg.get("confirm_timeout", 120)))
        if note == "allow_session":
            SESSION.allow = True
        if not ok:
            log("✗ 本机拒绝：" + note)
            post(authed, "POST", "/agent/fail", {"subtask_id": sid, "error": note})
            return

    log(f"交给本机队员「{ADAPTERS[app_key]['label']}」执行…")
    ok, out = run_local_ai(app_key, prompt, cfg)
    if ok:
        log(f"✅ 完成（{len(out)} 字符）")
        post(authed, "POST", "/agent/complete", {"subtask_id": sid, "result": out})
    else:
        log("✗ 失败：" + out[:300])
        post(authed, "POST", "/agent/fail", {"subtask_id": sid, "error": out})


# ── 常驻循环 ────────────────────────────────────────────────────

def hub_base(cfg: dict) -> str:
    """Agent REST 在 Hub 上（nginx 下挂在 /mcp-hub/），配对 API 在面板上。

    BELLHUB_HUB_URL 可直连 Hub（本机调试/无 nginx 场景用）。
    """
    return os.environ.get("BELLHUB_HUB_URL") or (cfg["base_url"].rstrip("/") + "/mcp-hub")


def post(cfg: dict, method: str, path: str, body: dict) -> tuple[int, dict]:
    return http(method, hub_base(cfg), path, body,
                headers={"X-Agent-Auth": f"{cfg['name']}:{cfg['token']}"})


def one_round(cfg: dict, found: dict, hb: int, backoff: int) -> tuple[int, int, bool]:
    """跑一轮心跳+领活。返回 (退出码或 -1 表示继续, 新的 backoff, 本轮是否成功)。

    第三个返回值专给 `--once` 用：单轮模式下 hub 不可达必须**非零退出**，
    否则计划任务 / cron 会把"根本没连上"当成成功。
    """
    st, res = post(cfg, "POST", "/agent/heartbeat", {})
    if st in (401, 403):
        print(f"✗ 凭据被拒（HTTP {st}）：{res.get('error') or res}。请重新运行 pair。")
        return 5, backoff, False
    if st != 200:
        log(f"心跳异常 HTTP {st}：{res}；{backoff}s 后重试")
        return -1, min(backoff * 2, 120), False
    backoff = hb
    st, poll = post(cfg, "POST", "/agent/poll", {})
    if st != 200:
        log(f"领取任务异常 HTTP {st}：{res}")
        return -1, backoff, False
    for m in poll.get("messages", []):
        log(f"✉ 来自 {m.get('from')}：{m.get('text', '')[:200]}")
    for sub in poll.get("subtasks", []):
        if post(cfg, "POST", "/agent/claim", {"subtask_id": sub["id"]})[0] == 200:
            handle_subtask(sub, cfg, found, cfg)
        else:
            log(f"任务 {sub['id']} 已被别人领走")
    return -1, backoff, True


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.get("token"):
        print("✗ 尚未配对。先运行：bellhub-connector pair")
        return 2
    apply_custom_adapters(cfg)
    found = detect()
    if args.refresh:
        cfg["enabled"] = list(found)
        save_json(CONFIG_PATH, cfg)
    if getattr(args, "only", ""):
        want = [k.strip() for k in args.only.split(",") if k.strip()]
        bad = [k for k in want if k not in ADAPTERS]
        if bad:
            print(f"✗ --only 里有不认识的队员：{', '.join(bad)}；"
                  f"可选：{', '.join(ADAPTERS)}", file=sys.stderr)
            return 2
        missing = [k for k in want if k not in found]
        if missing:
            print(f"⚠ --only 里这些本机没探测到，将跳过：{', '.join(missing)}")
        cfg["enabled"] = [k for k in want if k in found]
    if getattr(args, "allow_high_risk", False):
        cfg["auto_approve_high_risk"] = True
    enabled = cfg.get("enabled") or list(found)
    print(f"铃枢连接器 {VERSION}｜队员名 {cfg['name']}｜站点 {cfg['base_url']}")
    print(f"  本机可用 AI：{', '.join(ADAPTERS[k]['label'] for k in found) or '（无）'}")
    print(f"  已启用     ：{', '.join(ADAPTERS[k]['label'] for k in enabled if k in ADAPTERS) or '（无）'}")
    print(f"  高危闸门   ：{'自动放行' if cfg.get('auto_approve_high_risk') else '本机确认'}")
    hb = max(5, int(cfg.get("heartbeat", 15)))
    backoff = hb

    # ── 独立心跳线程（2026-10-19）────────────────────────────────
    # 原先心跳和领活/执行挤在同一个循环里：本机 AI 一跑就是几分钟，这期间一个心跳
    # 都发不出去，hub 90s 就判离线 —— 派活面板显示"没有在线队员"，长任务还会被
    # 误判成卡死（当晚实测：19:11:21 领活后心跳停摆，18 分钟后仍是 running）。
    # 现在心跳单独一条守护线程，执行/确认期间照常每 hb 秒一跳。
    beat_state = {"stop": False, "auth_failed": False}

    def beat_forever() -> None:
        while not beat_state["stop"]:
            try:
                st, res = post(cfg, "POST", "/agent/heartbeat", {})
                if st in (401, 403):
                    beat_state["auth_failed"] = True
                    log(f"心跳线程：凭据被拒（HTTP {st}），请重新 pair")
                    return
                if st != 200:
                    log(f"心跳线程：HTTP {st}（稍后重试）")
            except Exception as exc:                      # noqa: BLE001
                log(f"心跳线程异常：{exc}")
            time.sleep(hb)

    if not getattr(args, "once", False):
        threading.Thread(target=beat_forever, daemon=True,
                         name="bellhub-heartbeat").start()
        print(f"  心跳     ：独立线程，每 {hb}s 一跳（长任务执行中也不断）")
        print("  Ctrl+C 退出。\n")
    while True:
        try:
            rc, backoff, round_ok = one_round(cfg, found, hb, backoff)
        except KeyboardInterrupt:
            beat_state["stop"] = True
            print("\n已退出（凭据保留，下次 run 继续）。")
            return 0
        except Exception as exc:                       # noqa: BLE001
            log(f"循环异常：{exc}；{backoff}s 后重试")
            rc, backoff, round_ok = -1, min(backoff * 2, 120), False
        if rc >= 0:
            return rc
        if getattr(args, "once", False):
            if round_ok:
                log("单轮模式结束（--once）：本轮成功。")
                return 0
            # 单轮模式 + 没连上 = 失败退出，供计划任务/cron 判断
            print("✗ 单轮模式结束，但本轮未成功（hub 不可达或返回异常）——退出码 9。"
                  "\n  排查：bellhub-connector doctor")
            return 9
        time.sleep(backoff)


# ── 反向接入：让 IDE 调用铃枢 ─────────────────────────────────────

def mcp_snippet(cfg: dict, client: str) -> str:
    url = cfg["base_url"].rstrip("/") + "/mcp-hub/mcp/"
    token = cfg.get("token", "<先运行 pair>")
    server = {"url": url, "headers": {"Authorization": f"Bearer {token}"}}
    if client in ("cursor", "vscode"):
        server["type"] = "http"
    return json.dumps({"mcpServers": {"bellhub": server}}, ensure_ascii=False, indent=2)


def cmd_print_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    # IDE 反向接入用的也是这台机器配对时拿到的凭据：一机一凭据，按账号隔离
    snippet = mcp_snippet(cfg, args.client)
    if args.write:
        path = client_config_path(args.client, args.path)
        if path is None:
            print("✗ 不知道这个客户端的默认配置路径，请用 --path 指定它的 mcp.json")
            return 2
        ok, msg = write_client_config(path, json.loads(snippet)["mcpServers"]["bellhub"])
        print(("✅ " if ok else "✗ ") + msg)
        if ok:
            print("  下一步：重启该客户端，然后在它里面问一句「列出我的设备」验证是否接通。")
        return 0 if ok else 1
    print(f"把下面这段放进 {args.client} 的 MCP 配置里（不要提交到代码仓库）：\n")
    print(snippet)
    if args.client == "trae":
        print("\nTrae CN：用户级 %APPDATA%\\Trae CN\\User\\mcp.json（别写进项目里的 .trae/mcp.json，容易误提交）")
    print("\n提示：URL 末尾的斜杠不能少；令牌等价于「在这台电脑上派活」的权限，只在自有设备使用。")
    print("      也可以直接用 `print-config --write` 让连接器替你写进配置文件（自动备份原文件）。")
    return 0


# 各客户端 MCP 配置的默认位置（苹果/华为设备不在支持范围内，故不列 macOS 路径）
CLIENT_PATHS: dict[str, dict[str, str]] = {
    "trae": {"windows": r"%APPDATA%\Trae CN\User\mcp.json"},
    "cursor": {"all": "~/.cursor/mcp.json"},
    "claude": {"all": "~/.claude.json"},
    "vscode": {"windows": r"%APPDATA%\Code\User\mcp.json",
               "linux": "~/.config/Code/User/mcp.json"},
    "generic": {},
}


def client_config_path(client: str, override: str | None) -> Path | None:
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    spec = CLIENT_PATHS.get(client) or {}
    plat = platform.system().lower()
    raw = spec.get(plat) or spec.get("all")
    if not raw:
        return None
    return Path(os.path.expandvars(raw)).expanduser()


def write_client_config(path: Path, server: dict) -> tuple[bool, str]:
    """把 bellhub 服务器合并进客户端的 mcp.json：只改 mcpServers.bellhub。

    · 有变化才动文件：内容已一致时直接返回，不制造无谓的备份与 mtime 抖动；
    · 备份名带短随机后缀：同一秒内连写两次也不会互相覆盖（会把原始内容弄丢）。
    """
    existed = path.is_file()
    data: dict = {}
    backup: Path | None = None
    if existed:
        try:
            data = load_json(path, {})
        except SystemExit as exc:
            return False, str(exc)
        if not isinstance(data, dict):
            return False, f"{path} 顶层不是 JSON 对象，未改动"
    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    if not isinstance(servers, dict):
        return False, f"{path} 里的 mcpServers 不是对象，未改动"
    if servers.get("bellhub") == server:
        return True, f"{path} 里的 bellhub 配置已是最新，无需改动"

    if existed:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        if backup.exists():
            backup = path.with_name(f"{path.name}.bak-{stamp}-{os.urandom(2).hex()}")
        shutil.copy2(path, backup)
    servers["bellhub"] = server
    path.parent.mkdir(parents=True, exist_ok=True)
    # 不用 PowerShell 的 -Encoding UTF8（会写 BOM，客户端可能解析失败）
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tail = f"（原文件已备份为 {backup.name}）" if backup else "（新建）"
    return True, f"已写入 {path} {tail}"


def self_platform() -> str:
    return "win-x64" if platform.system().lower() == "windows" else "linux-x64"


def is_termux() -> bool:
    """是否运行在安卓 Termux（用于自启形态与提示语）。"""
    return ("com.termux" in os.environ.get("PREFIX", "")
            or bool(os.environ.get("TERMUX_VERSION")))


def resolve_latest(res: dict, frozen: bool) -> tuple[str | None, str]:
    """站点上"与本机同形态"的最新版本 → (版本, 来源说明)。

    打包版不能拿源码版本来比自己：产物没重建时源码更新了，二进制其实还是旧的。
    所以打包版优先看同平台产物的版本旁文件（构建脚本写入），拿不到才退回源码版本。
    """
    builds = {b.get("platform"): b for b in (res.get("builds") or []) if isinstance(b, dict)}
    if frozen:
        b = builds.get(self_platform()) or {}
        if b.get("version"):
            return b["version"], f"预编译产物 {self_platform()}"
    return res.get("version"), "源码版"


def cmd_version(args: argparse.Namespace) -> int:
    """版本自检：对不上就告诉用户去哪儿拿新版（不擅自替换正在运行的文件）。"""
    print(f"本机连接器版本：{VERSION}"
          f"（{'打包单文件' if getattr(sys, 'frozen', False) else '源码版'}）")
    if not args.check:
        return 0
    cfg = load_config()
    if not cfg.get("token"):
        print("✗ 尚未配对，无法查询站点版本。先运行：bellhub-connector pair")
        return 2
    st, res = http("GET", cfg["base_url"], "/api/link/connector-latest", None,
                   {"X-Agent-Auth": f"{cfg['name']}:{cfg['token']}"}, timeout=20)
    if st != 200:
        print(f"✗ 查询失败（HTTP {st}）：{res.get('detail') or res}")
        return 3
    latest, source = resolve_latest(res, bool(getattr(sys, "frozen", False)))
    print(f"站点最新版本：{latest or '（未知）'}（来自{source}）")
    if latest == VERSION:
        print("✅ 已是最新版")
        return 0
    print("⚠ 站点上是另一个版本，获取方式：")
    for b in res.get("builds") or []:
        print(f"   · {b['label']}：面板「我的智能体」页下载 {b['file']}"
              f"（{b['size']} 字节，sha256 {b['sha256'][:12]}…）")
    print("   · 或重新下载源码版 bellhub_connector.py")
    print("   拿到后停止旧的 run（Ctrl+C / autostart --remove），替换文件，再 autostart。")
    return 1


def replace_file(target: Path, data: bytes) -> tuple[bool, str]:
    """就地把 target 换成新内容；成功返回 (True, 备份路径)，失败自动回滚。

    先"改名再写新文件"而不是直接覆盖：Windows 上运行中的 .exe 不能删/覆盖，
    但**可以改名**（Linux 同理），这样正在跑的进程继续用旧 inode，新文件落到原路径。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    if backup.exists():
        backup = target.with_name(f"{target.name}.bak-{stamp}-{os.urandom(2).hex()}")
    tmp = target.with_name(target.name + ".new")
    try:
        mode = target.stat().st_mode
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        os.replace(target, backup)
        try:
            os.replace(tmp, target)
        except OSError as exc:                    # 写新文件失败 → 把备份换回去
            os.replace(backup, target)
            return False, f"写入失败，已回滚原文件：{exc}"
    except OSError as exc:
        return False, f"替换失败（原文件未动）：{exc}"
    return True, str(backup)


def cmd_update(args: argparse.Namespace) -> int:
    """可选自动升级：查最新版 → 下载 → **校验 sha256** → 备份 → 替换 → 回读自检。

    安全默认：非交互环境不加 --yes 一律拒绝替换；任何一步失败都保留原文件。
    """
    cfg = load_config()
    if not cfg.get("token"):
        print("✗ 尚未配对。先运行：bellhub-connector pair")
        return 2
    auth = {"X-Agent-Auth": f"{cfg['name']}:{cfg['token']}"}
    st, res = http("GET", cfg["base_url"], "/api/link/connector-latest", None, auth, timeout=20)
    if st != 200:
        print(f"✗ 查询失败（HTTP {st}）：{res.get('detail') or res}")
        return 3
    frozen = bool(getattr(sys, "frozen", False))
    platform_key = self_platform()
    latest, source = resolve_latest(res, frozen)
    print(f"本机 {VERSION}（{'打包版' if frozen else '源码版'}）｜"
          f"站点 {latest or '（未知）'}（来自{source}）")
    if latest == VERSION and not args.force and not args.check:
        print("✅ 已是最新版（要强制重装请加 --force）")
        return 0
    if args.check:
        return 0 if latest == VERSION else 1

    target = Path(sys.executable) if frozen else Path(__file__).resolve()
    # 打包版优先取同平台产物，失败再退源码；源码版直接取源码
    attempts = ([("bin", platform_key), ("source", "")] if frozen else [("source", "")])
    data: bytes | None = None
    want_sha = ""
    for kind, plat in attempts:
        q = f"?kind={kind}" + (f"&platform={plat}" if plat else "")
        st, raw, hdrs = http_bytes("GET", cfg["base_url"],
                                   "/api/link/connector-artifact" + q, auth)
        if st == 200 and raw:
            data, want_sha = raw, hdrs.get("x-bellhub-sha256", "")
            print(f"已下载 {kind}{('/' + plat) if plat else ''}：{len(raw)} 字节")
            break
        print(f"  · {kind}{('/' + plat) if plat else ''} 不可用（HTTP {st}）")
    if data is None:
        print("✗ 站点上没有可用的更新包，稍后再试或手动下载")
        return 4

    got = hashlib.sha256(data).hexdigest()
    if want_sha and got != want_sha:
        print(f"✗ 哈希不符，已放弃：期望 {want_sha[:16]}… 实际 {got[:16]}…")
        return 5
    print(f"哈希校验通过：{got[:16]}…")

    if not args.yes:
        if not (sys.stdin and sys.stdin.isatty()):
            print("✗ 非交互环境未加 --yes，已放弃替换（安全默认）")
            return 6
        ans = input(f"用它替换 {target}？会先备份原文件 [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消，未改动任何文件")
            return 0

    ok, msg = replace_file(target, data)
    if not ok:
        print("✗ " + msg)
        return 7
    if hashlib.sha256(target.read_bytes()).hexdigest() != got:   # 回读自检
        print(f"✗ 替换后自检失败，原文件已备份在 {msg}")
        return 8
    print(f"✅ 已更新（旧版备份：{msg}）")
    print("   重启生效：Ctrl+C 结束当前 run 后重新 bellhub-connector run；"
          "用了 autostart 的话——Linux：systemctl --user restart bellhub-connector，"
          "Windows：任务计划程序里结束并重新运行 BellHubConnector。")
    return 0


def autostart_installed() -> bool:
    if is_termux():
        return (Path.home() / ".termux" / "boot" / "50-bellhub-connector.sh").is_file()
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Query", "/TN", "BellHubConnector"],
                           capture_output=True, text=True)
        return r.returncode == 0
    return (Path.home() / ".config" / "systemd" / "user" / "bellhub-connector.service").is_file()


def cmd_doctor(_args: argparse.Namespace) -> int:
    """一条命令自检：配对/站点/枢纽/本机队员/自启，逐项说清并给出下一步。"""
    cfg = load_config()
    apply_custom_adapters(cfg)
    frozen = bool(getattr(sys, "frozen", False))
    ok = True
    print(f"铃枢连接器自检｜版本 {VERSION}（{'打包单文件' if frozen else '源码版'}）")
    print(f"  运行环境 : Python {sys.version.split()[0]}｜{platform.system()} {platform.machine()}"
          f"{'（Termux/安卓）' if is_termux() else ''}")
    print(f"  配置     : {CONFIG_PATH}")
    print(f"  站点     : {cfg['base_url']}")
    print(f"  队员名   : {cfg['name']}｜账号：{cfg.get('username') or '（未记录）'}")
    if not cfg.get("token"):
        print("✗ 尚未配对：运行 `bellhub-connector pair`，在浏览器里点一次确认即可")
        return 2

    auth = {"X-Agent-Auth": f"{cfg['name']}:{cfg['token']}"}
    st, res = http("GET", cfg["base_url"], "/api/link/connector-latest", None, auth, timeout=20)
    if st != 200:
        print(f"✗ 站点不可达或凭据被拒（HTTP {st}）：{res.get('detail') or res}")
        print("  排查：① 站点地址对不对 ② 这台机器能否上网 ③ 队员是否被撤销（面板可重建）")
        return 3
    latest, source = resolve_latest(res, frozen)
    if latest == VERSION:
        print(f"  ✅ 版本已是最新（{VERSION}）")
    else:
        print(f"  ⚠ 站点是 {latest or '未知'}（来自{source}），本机 {VERSION}："
              f"运行 `bellhub-connector update` 升级")
        ok = False

    st, hb = post(cfg, "POST", "/agent/heartbeat", {})
    if st != 200:
        print(f"✗ 领活通道不通（HTTP {st}）：{hb.get('error') or hb}")
        return 3
    print(f"  ✅ 领活通道正常（待领取子任务 {hb.get('pending_subtasks', 0)} 个，"
          f"未读消息 {hb.get('messages', 0)} 条）")

    found = detect()
    enabled = [k for k in (cfg.get("enabled") or []) if k in found]
    if found:
        print(f"  ✅ 本机队员：{', '.join(found[k]['label'] for k in found)}")
        if enabled:
            print(f"     已启用：{', '.join(enabled)}")
        else:
            print("     ⚠ 没有一个已启用的队员：运行 `bellhub-connector run --refresh` 重新探测")
            ok = False
    else:
        print("✗ 本机没检测到可被派活的 AI 应用")
        print("  可装任一：Claude Code / Codex CLI / DeepSeek Harness / Gemini CLI / Aider / "
              "OpenCode / Ollama（轻量本地模型）")
        ok = False

    if autostart_installed():
        print("  ✅ 已设置开机自启")
    else:
        print("  · 未设置开机自启（想要就运行 `bellhub-connector autostart`）")

    print("\n结论：" + ("一切正常，站娘可以把活派过来了"
                    if ok else "有项目需要处理（见上面 ⚠/✗），按提示处理后重跑 doctor"))
    return 0 if ok else 1


def cmd_detect(_args: argparse.Namespace) -> int:
    apply_custom_adapters(load_config())
    found = detect()
    print(f"铃枢连接器 {VERSION}｜本机 AI 探测")
    if not found:
        print("  未检测到可被派活的 AI 应用。可安装：Claude Code / Codex CLI / DSH / Ollama")
        return 1
    for k, a in found.items():
        print(f"  ✅ {a['label']:<28} {a['kind']:<5} {a.get('path') or a.get('model')}")
        print(f"      可接的活：{', '.join(a['take'])}｜{a['note']}")
    return 0


def _self_command() -> str:
    """返回"让本连接器以 run 模式启动"的完整命令行（自启用）。"""
    if getattr(sys, "frozen", False):          # PyInstaller 打包后
        return f'"{sys.executable}" run'
    py = sys.executable
    if os.name == "nt":                        # 源码版在 Windows 上用 pythonw：无控制台窗口
        cand = Path(py).with_name("pythonw.exe")
        if cand.exists():
            py = str(cand)
    return f'"{py}" "{Path(__file__).resolve()}" run'


def windows_startup_dir() -> Path:
    """当前用户的"启动"文件夹（登录即运行，写入无需管理员）。"""
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def cmd_autostart(args: argparse.Namespace) -> int:
    """把连接器注册成登录即启的后台常驻（一次安装、之后全自动）。"""
    cmd = _self_command()
    if is_termux():
        # 安卓 Termux 没有 systemd --user，用 Termux:Boot 的开机脚本目录
        boot_dir = Path.home() / ".termux" / "boot"
        script = boot_dir / "50-bellhub-connector.sh"
        if args.remove:
            if script.exists():
                script.unlink()
                print(f"✅ 已删除 {script}")
            else:
                print(f"（本来就没有 {script}）")
            return 0
        boot_dir.mkdir(parents=True, exist_ok=True)
        log = Path.home() / "bellhub-connector.log"
        script.write_text(
            "#!/data/data/com.termux/files/usr/bin/sh\n"
            "# 由 bellhub-connector autostart 生成；Termux:Boot 会执行本目录下所有脚本\n"
            "termux-wake-lock 2>/dev/null || true\n"
            f"exec {cmd} >> {log} 2>&1\n", encoding="utf-8")
        script.chmod(0o755)
        print(f"✅ 已写入开机脚本：{script}")
        print("   还需：安装 Termux:Boot 并打开一次；关掉 Termux 的电池优化。")
        print("   ⚠ 开机脚本只在重启后触发 —— 现在就想在线，请在另一个 Termux 会话执行："
              "bellhub-connector run")
        print(f"   日志：{log}")
        return 0
    if platform.system() == "Windows":
        # 2026-10-14 修：schtasks /SC ONLOGON 需要管理员权限，普通用户会得到"拒绝访问"。
        # 改为写当前用户的"启动"文件夹 + 一个 VBS（窗口样式 0 = 后台无黑框），
        # 既不需要管理员，用户也能在"启动"里一眼看到并自行删除。
        startup = windows_startup_dir()
        vbs = startup / "bellhub-connector.vbs"
        legacy = startup / "bellhub-connector.cmd"
        if args.remove:
            removed = []
            for f in (vbs, legacy):
                if f.exists():
                    f.unlink()
                    removed.append(f.name)
            subprocess.run(["schtasks", "/Delete", "/TN", "BellHubConnector", "/F"],
                           capture_output=True, text=True)   # 清历史的计划任务，失败无所谓
            print(f"✅ 已移除开机自启：{', '.join(removed)}" if removed else "（本来就没有开机启动项）")
            return 0
        try:
            startup.mkdir(parents=True, exist_ok=True)
            # VBS 里内嵌双引号要写成两个双引号
            vbs.write_text('CreateObject("WScript.Shell").Run "'
                           + cmd.replace('"', '""') + '", 0, False\n', encoding="utf-8")
        except OSError as exc:
            print(f"✗ 写启动项失败：{exc}")
            print(f"   可手动把 `{cmd}` 的快捷方式放进：{startup}")
            return 1
        print(f"✅ 已写入开机启动项：{vbs}")
        print("   下次登录会自动在后台运行（无窗口），不需要管理员权限。")
        print("   现在就生效：另开一个终端跑一次 bellhub-connector run。")
        return 0
    # Linux：systemd --user 单元
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit = unit_dir / "bellhub-connector.service"
    if args.remove:
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        "bellhub-connector.service"], capture_output=True, text=True)
        if unit.exists():
            unit.unlink()
        print("✅ 已停止并移除开机自启")
        return 0
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=BellHub Connector（铃枢连接器）\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={_self_command()}\nRestart=always\nRestartSec=10\n\n"
        "[Install]\nWantedBy=default.target\n", encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "bellhub-connector.service"],
                       capture_output=True, text=True)
    print((r.stdout or r.stderr).strip())
    print(f"✅ 已注册 {unit}（登录后自动运行；`loginctl enable-linger $USER` 可让它在你未登录时也常驻）")
    return 0 if r.returncode == 0 else 1


def cmd_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    has = "已配对" if cfg.get("token") else "未配对"
    print(f"铃枢连接器 {VERSION}｜{has}")
    print(f"  配置：{CONFIG_PATH}")
    print(f"  站点：{cfg['base_url']}｜队员名：{cfg['name']}｜账号：{cfg.get('username') or '-'}")
    if cfg.get("token"):
        st, res = post(cfg, "POST", "/agent/heartbeat", {})
        print(f"  连通性：HTTP {st}｜{res}")
    print(f"  已启用：{', '.join(cfg.get('enabled') or []) or '-'}")
    return 0


def main() -> int:
    # 打包成单文件二进制后 stdout 默认是块缓冲：用户会看不到"配对码"这类实时提示，
    # 自动化读取也会一直等不到输出（2026-10-14 实测踩到）。这里强制行缓冲。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)      # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(prog="bellhub-connector",
                                description="铃枢连接器：一次配对，让站娘把活派给本机 AI 应用")
    sub = p.add_subparsers(dest="cmd")

    pp = sub.add_parser("pair", help="浏览器授权配对（首次使用）")
    pp.add_argument("--base", default=None, help=f"站点地址（默认 {DEFAULT_BASE}）")
    pp.add_argument("--name", default=None, help="队员名（默认本机主机名）")
    pp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    pp.set_defaults(func=cmd_pair)

    pr = sub.add_parser("run", help="常驻：领活并交给本机 AI 执行")
    pr.add_argument("--refresh", action="store_true", help="启动时重新探测并覆盖启用列表")
    pr.add_argument("--once", action="store_true", help="只跑一轮就退出（自检/计划任务用）")
    pr.add_argument("--only", default="",
                    help="只启用这些本机队员（逗号分隔，如 dsh,ollama），覆盖配置里的 enabled")
    pr.add_argument("--allow-high-risk", action="store_true",
                    help="本次运行自动放行高危任务（不改配置文件）")
    pr.set_defaults(func=cmd_run)

    pd = sub.add_parser("detect", help="列出本机可编入队伍的 AI 应用")
    pd.set_defaults(func=cmd_detect)

    ps = sub.add_parser("status", help="查看配对状态与连通性")
    ps.set_defaults(func=cmd_status)

    pa = sub.add_parser("autostart", help="设为登录即启的后台常驻（或 --remove 取消）")
    pa.add_argument("--remove", action="store_true", help="取消开机自启")
    pa.set_defaults(func=cmd_autostart)

    pc = sub.add_parser("print-config", help="打印或写入 IDE 反向接入铃枢的 MCP 配置")
    pc.add_argument("client", choices=["trae", "claude", "cursor", "vscode", "generic"])
    pc.add_argument("--write", action="store_true",
                    help="直接写进该客户端的配置文件（自动备份原文件）")
    pc.add_argument("--path", default=None, help="自定义配置文件路径（配合 --write）")
    pc.set_defaults(func=cmd_print_config)

    pv = sub.add_parser("version", help="版本自检（--check 对比站点上的最新版）")
    pv.add_argument("--check", action="store_true", help="向站点查询最新版本")
    pv.set_defaults(func=cmd_version)

    pu = sub.add_parser("update", help="升级连接器自身（校验 sha256、备份、失败自动回滚）")
    pu.add_argument("--check", action="store_true", help="只检查是否有新版")
    pu.add_argument("--yes", action="store_true", help="非交互确认替换（自动化用）")
    pu.add_argument("--force", action="store_true", help="同版本也重装一遍（修复文件损坏）")
    pu.set_defaults(func=cmd_update)

    pdoc = sub.add_parser("doctor", help="一条命令自检（配对/站点/领活通道/本机队员/自启）")
    pdoc.set_defaults(func=cmd_doctor)

    p.add_argument("--version", action="version", version=f"bellhub-connector {VERSION}")
    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
