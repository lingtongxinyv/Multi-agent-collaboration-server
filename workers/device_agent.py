#!/usr/bin/env python3
"""DSH Suite 用户设备 Agent 客户端（跨平台：Windows / Linux / macOS / Termux）。

把你的电脑接入铃枢 BellHub：在主站「我的设备」页添加设备拿到 token，
本客户端轮询 Hub 心跳领指令，交给 handler 执行后回传结果。
站娘在工作站对话里可通过 device_list / device_send / device_results
给你的设备下发指令并读取结果。

配置 config.json（与本文件同目录，首次运行自动生成）：
{
  "hub": "https://agent.example.com/mcp-hub",
  "name": "我的设备页添加的设备名",
  "token": "我的设备页发放的 token",
  "handler": "device_handler.py",
  "poll_interval": 15
}

handler 约定：`python <handler> <cmd_id> <text>`，stdout 为结果；
退出码非 0 视为失败，stderr 作为错误信息。
官方附带两个 handler：
- device_handler.py  默认，仅回显指令（安全，接不到敏感操作）
- shell_handler.py   可选，把指令当 shell 命令执行（⚠ 自行启用，token 泄露等于交出设备 shell）
在 config.json 的 "handler" 字段切换。

agent 内置指令（本文件自己回答，不经 handler，全部只读、无副作用）：
  !help / !ping / !version / !info / !ps
用于远程确认设备上跑着几个 agent 实例、版本、运行环境——排障与巡检用。
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import urllib.error
import urllib.request

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
# 版本标记：启动时会打印，也会响应 !version，便于远程确认设备上跑的是哪一版
VERSION = "2026-10-14c"
START_TS = time.time()
# 内置指令前缀：以它开头的指令由 agent 自己回答，不交给本地 handler
BUILTIN_PREFIX = "!"
HANDLER_TIMEOUT = 1800


def _builtin_help(config: dict) -> str:
    return (
        "agent 内置指令（由 agent 自己回答，不经本地 handler，全部只读）：\n"
        "  !help      显示本帮助\n"
        "  !ping      存活探测（返回时间戳与本次运行的 PID）\n"
        "  !version   agent 版本号\n"
        "  !info      agent 与运行环境信息（版本/PID/运行时长/主机名/系统/Python/配置摘要）\n"
        "  !ps        本机 Python 进程列表（用于发现重复运行的 agent 实例）\n"
        f"  其他任何文本 → 交给本地 handler 执行（当前 handler：{config.get('handler') or 'device_handler.py'}）"
    )


def _run_fixed(cmd: list[str], timeout: int = 20) -> str:
    """执行一条**写死的**命令（不接受任何外部拼接），返回 stdout+stderr。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
        return ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return f"({cmd[0]} 执行失败: {exc})"


def _proc_list() -> str:
    """列出本机 Python 进程。命令写死、不拼接用户输入，只读。"""
    system = platform.system()
    if system == "Windows":
        out = _run_fixed(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"])
        out2 = _run_fixed(["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"])
        rows = [l for l in (out + "\n" + out2).splitlines()
                if l.strip() and "没有运行" not in l and "No tasks" not in l]
        detail = _run_fixed(["wmic", "process", "where", "name like '%python%'",
                             "get", "ProcessId,CommandLine", "/format:csv"])
        return "Windows python 进程：\n" + ("\n".join(rows) or "（未发现 python.exe）") + \
               ("\n\n命令行明细（wmic，可能因系统版本不可用）：\n" + detail if detail and "执行失败" not in detail else "")
    # Linux / macOS
    ps = _run_fixed(["ps", "-eo", "pid,ppid,etime,cmd"])
    hits = [l for l in ps.splitlines() if "device_agent" in l or "python" in l.lower()]
    return f"{system} 相关进程：\n" + ("\n".join(hits) or "（未发现 python/device_agent 进程）")


def run_builtin(config: dict, text: str) -> tuple[str, bool] | None:
    """处理 `!` 内置指令。返回 (输出, 是否成功)；返回 None 表示非内置指令，交给 handler。"""
    if not text.startswith(BUILTIN_PREFIX):
        return None
    cmd = text.split(None, 1)[0].lower()
    if cmd in ("!help", "!?"):
        return _builtin_help(config), True
    if cmd == "!ping":
        return (f"pong｜本机时间 {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
                f"｜agent pid {os.getpid()}｜版本 {VERSION}"), True
    if cmd == "!version":
        return f"agent 版本 {VERSION}", True
    if cmd == "!info":
        try:
            host = socket.gethostname()
        except Exception:  # noqa: BLE001
            host = "?"
        token = str(config.get("token") or "")
        masked = (token[:4] + "…" + token[-4:]) if len(token) > 10 else ("已配置" if token else "未配置")
        handler = config.get("handler") or "device_handler.py"
        handler_path = Path(__file__).resolve().parent / str(handler)
        lines = [
            f"agent 版本      : {VERSION}",
            f"进程            : pid={os.getpid()} ppid={os.getppid()} "
            f"运行时长={time.time() - START_TS:.0f}s",
            f"主机            : {host}",
            f"系统            : {platform.system()} {platform.release()} ({platform.machine()})",
            f"Python          : {sys.version.split()[0]}（{sys.executable}）",
            f"工作目录        : {Path(__file__).resolve().parent}",
            f"hub             : {config.get('hub')}",
            f"设备名          : {config.get('name')}",
            f"token           : {masked}",
            f"轮询间隔        : {config.get('poll_interval', 15)}s",
            f"handler         : {handler}（{'存在' if handler_path.exists() else '★不存在'}）",
            f"内置指令        : 已启用（发送 {BUILTIN_PREFIX}help 查看）",
        ]
        return "\n".join(lines), True
    if cmd == "!ps":
        return _proc_list(), True
    return (f"未知内置指令 {cmd}\n\n" + _builtin_help(config)), False


def dispatch(config: dict, cmd_id: str, text: str) -> tuple[str, bool]:
    """先看是不是 agent 内置指令，否则交给本地 handler。"""
    builtin = run_builtin(config, text)
    if builtin is not None:
        return builtin
    return run_handler(config, cmd_id, text)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps({
            "hub": "https://agent.example.com/mcp-hub",
            "name": "",
            "token": "",
            "handler": "device_handler.py",
            "poll_interval": 15,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[device] 已生成默认配置 {CONFIG_PATH}，填写 name/token 后重新运行")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def api(config: dict, path: str, body: dict | None = None) -> dict:
    url = config["hub"].rstrip("/") + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    req.add_header("X-Device-Auth", f"{config['name']}:{config['token']}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return {"error": f"HTTP {exc.code}"}


def run_handler(config: dict, cmd_id: str, text: str) -> tuple[str, bool]:
    """执行 handler：返回 (输出, 是否成功)。"""
    name = config.get("handler") or "device_handler.py"
    if not isinstance(name, str):
        return (f"config.json 的 handler 字段必须是字符串文件名，"
                f"当前是 {type(name).__name__}（请改为 \"device_handler.py\" 或 \"shell_handler.py\"）"), False
    handler = Path(__file__).resolve().parent / name
    if not handler.exists():
        return f"handler 不存在: {name}", False
    # ⚠ cmd_id 一律强转 str：Hub 的 device_commands.id 是自增整数（int），
    # 直接塞进参数表会让 subprocess 抛
    # TypeError: expected str, bytes or os.PathLike object, not int
    # （2026-10-13 实证：设备上所有指令都因此失败，与模型额度无关）
    #
    # ⚠ 编码：强制子进程用 UTF-8 输出、父进程也按 UTF-8 解码。Windows 中文系统
    # 默认代码页是 cp936，遇到 emoji/生僻字会让 handler 的 print 抛
    # UnicodeEncodeError（指令明明发出去了却回传失败），两侧统一 UTF-8 才能
    # 保证"中文/符号/emoji 往返保真"。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run([sys.executable, str(handler), str(cmd_id), str(text)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=HANDLER_TIMEOUT,
                          cwd=str(handler.parent))
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()[-2000:]
    return (output or "(handler 无输出)"), proc.returncode == 0


def main() -> None:
    config = load_config()
    if not config.get("name") or not config.get("token"):
        print("[device] 请在 config.json 填写 name（我的设备页的设备名）与 token 后重新运行")
        sys.exit(1)
    interval = max(5, int(config.get("poll_interval", 15)))
    print(f"[device] agent 版本 {VERSION}｜{config['name']} 已启动，hub={config['hub']}，"
          f"心跳 {interval}s，handler={config.get('handler')}")
    print(f"[device] 内置指令已启用：{BUILTIN_PREFIX}help / {BUILTIN_PREFIX}ping / "
          f"{BUILTIN_PREFIX}version / {BUILTIN_PREFIX}info / {BUILTIN_PREFIX}ps")
    while True:
        try:
            beat = api(config, "/device/heartbeat")
            if "error" in beat:
                print(f"[device] 心跳失败: {beat['error']}")
            else:
                for cmd in beat.get("commands", []):
                    # id 可能是 int（自增主键）也可能是 str，统一转字符串再交给 handler
                    cmd_id = str(cmd.get("id", ""))
                    cmd_text = cmd.get("text") or ""
                    if not isinstance(cmd_text, str):
                        cmd_text = json.dumps(cmd_text, ensure_ascii=False)
                    print(f"[device] 执行指令 {cmd_id}: {cmd_text[:80]}")
                    try:
                        output, ok = dispatch(config, cmd_id, cmd_text)
                    except Exception as exc:
                        # 带上异常类型，便于远端一眼看出是权限/网络/参数问题
                        output, ok = f"handler 异常: {type(exc).__name__}: {exc}", False
                    api(config, "/device/report", {
                        "cmd_id": cmd["id"],
                        "status": "done" if ok else "failed",
                        "result": output,
                    })
                    print(f"[device] 指令 {cmd_id} {'完成' if ok else '失败'}")
        except KeyboardInterrupt:
            print("[device] 退出")
            break
        except Exception as exc:
            print(f"[device] 轮询异常: {exc}")
        time.sleep(interval)


def is_termux() -> bool:
    """是否运行在安卓 Termux 里（Termux 会设置 PREFIX=/data/data/com.termux/files/usr）。"""
    return ("com.termux" in os.environ.get("PREFIX", "")
            or bool(os.environ.get("TERMUX_VERSION"))
            or Path("/data/data/com.termux/files/usr").is_dir())


def windows_startup_dir() -> Path:
    """当前用户的"启动"文件夹（登录即运行，写入无需管理员）。"""
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _self_command() -> str:
    """让本 agent 重新跑起来的完整命令行（自启用）。"""
    py = sys.executable
    if os.name == "nt":
        cand = Path(py).with_name("pythonw.exe")   # 后台跑且不弹黑框
        py = str(cand) if cand.exists() else py
    return f'"{py}" "{Path(__file__).resolve()}"'


USAGE = (
    "用法：\n"
    "  python device_agent.py                      启动设备 agent（默认）\n"
    "  python device_agent.py autostart            设为开机/登录即启\n"
    "  python device_agent.py autostart --remove   取消自启\n"
    "  python device_agent.py doctor               一条命令自检（配置/handler/连通性）\n"
    "  python device_agent.py --help               显示本帮助\n"
    "自启形态：Termux → ~/.termux/boot/ 脚本（需装 Termux:Boot）；\n"
    "          Windows → 任务计划程序（登录时）；Linux → systemd --user 单元。"
)


def cmd_autostart(remove: bool) -> int:
    """开机/登录即启。安卓走 Termux:Boot，Windows 走任务计划，Linux 走 systemd --user。"""
    cmd = _self_command()
    workdir = str(Path(__file__).resolve().parent)

    if is_termux():
        boot_dir = Path.home() / ".termux" / "boot"
        script = boot_dir / "50-bellhub-agent.sh"
        if remove:
            if script.exists():
                script.unlink()
                print(f"✅ 已删除 {script}")
            else:
                print(f"（本来就没有 {script}）")
            return 0
        boot_dir.mkdir(parents=True, exist_ok=True)
        log = Path.home() / "bellhub-agent.log"
        script.write_text(
            "#!/data/data/com.termux/files/usr/bin/sh\n"
            "# 由 device_agent.py autostart 生成；Termux:Boot 会执行本目录下所有脚本\n"
            "termux-wake-lock 2>/dev/null || true\n"
            f"cd {workdir} || exit 1\n"
            f"exec {cmd} >> {log} 2>&1\n", encoding="utf-8")
        script.chmod(0o755)
        print(f"✅ 已写入开机脚本：{script}")
        print("   还需要两步（否则不会触发）：")
        print("   1) 安装 Termux:Boot 应用并打开一次（F-Droid / Google Play 同名 app）")
        print("   2) 在系统设置里给 Termux 关掉电池优化（否则安卓会在后台杀掉它）")
        print(f"   日志：{log}（自启后出问题先看它）")
        return 0

    if os.name == "nt":
        # 2026-10-14 修：schtasks /SC ONLOGON 需要管理员，普通用户会"拒绝访问"。
        # 改为当前用户"启动"文件夹 + VBS（窗口样式 0 = 后台无黑框），无需管理员。
        startup = windows_startup_dir()
        vbs = startup / "bellhub-device-agent.vbs"
        if remove:
            if vbs.exists():
                vbs.unlink()
                print(f"✅ 已删除开机启动项：{vbs}")
            else:
                print("（本来就没有开机启动项）")
            subprocess.run(["schtasks", "/Delete", "/TN", "BellHubDeviceAgent", "/F"],
                           capture_output=True, text=True)   # 清历史任务，失败无所谓
            return 0
        try:
            startup.mkdir(parents=True, exist_ok=True)
            vbs.write_text('CreateObject("WScript.Shell").Run "'
                           + cmd.replace('"', '""') + '", 0, False\n', encoding="utf-8")
        except OSError as exc:
            print(f"✗ 写启动项失败：{exc}")
            print(f"   可手动把 `{cmd}` 的快捷方式放进：{startup}")
            return 1
        print(f"✅ 已写入开机启动项：{vbs}")
        print("   下次登录会自动在后台运行（无窗口），不需要管理员权限。")
        return 0

    # Linux / 其它类 Unix：systemd --user
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit = unit_dir / "bellhub-device.service"
    if remove:
        r = subprocess.run(["systemctl", "--user", "disable", "--now",
                            "bellhub-device.service"], capture_output=True, text=True)
        if unit.exists():
            unit.unlink()
        print((r.stderr or r.stdout).strip() or "✅ 已停止并移除自启")
        return 0
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=BellHub Device Agent（铃枢设备 agent）\n\n"
        f"[Service]\nType=simple\nWorkingDirectory={workdir}\nExecStart={cmd}\n"
        "Restart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n",
        encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "bellhub-device.service"],
                       capture_output=True, text=True)
    print((r.stdout or r.stderr).strip())
    if r.returncode == 0:
        print(f"✅ 已注册 {unit}（登录后自动运行；`loginctl enable-linger $USER` 让未登录也常驻）")
    else:
        print(f"⚠ 单元文件已写好（{unit}），但 systemctl --user 未生效——"
              f"常见于没有 user session 的环境；可在桌面会话里手动执行："
              f"systemctl --user enable --now bellhub-device.service")
    return 0 if r.returncode == 0 else 1


def autostart_installed() -> bool:
    if is_termux():
        return (Path.home() / ".termux" / "boot" / "50-bellhub-agent.sh").is_file()
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Query", "/TN", "BellHubDeviceAgent"],
                           capture_output=True, text=True)
        return r.returncode == 0
    return (Path.home() / ".config" / "systemd" / "user" / "bellhub-device.service").is_file()


def cmd_doctor() -> int:
    """一条命令自检：配置/handler/依赖/连通性，逐项说清，失败给出下一步。"""
    ok = True
    print(f"铃枢设备 agent 自检｜版本 {VERSION}")
    print(f"  运行环境 : Python {sys.version.split()[0]}｜{platform.system()} {platform.machine()}"
          f"{'（Termux/安卓）' if is_termux() else ''}")
    print(f"  脚本目录 : {Path(__file__).resolve().parent}")

    if not CONFIG_PATH.is_file():
        print(f"✗ 没有配置文件：{CONFIG_PATH}")
        print("  下一步：先运行一次 `python device_agent.py` 生成它，再填 name/token")
        return 2
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:                       # noqa: BLE001
        print(f"✗ 配置读取失败：{type(exc).__name__}: {exc}")
        print("  常见原因：记事本存成了 config.json.txt、UTF-16/BOM、全角引号、末尾多逗号")
        return 2

    name, token = cfg.get("name"), cfg.get("token")
    handler = cfg.get("handler") or "device_handler.py"
    print(f"  设备名   : {name or '（未填）'}")
    print(f"  hub      : {cfg.get('hub')}")
    print(f"  handler  : {handler}")
    print(f"  心跳间隔 : {cfg.get('poll_interval', 15)}s")
    if not name or not token:
        print("✗ name / token 未填")
        print("  下一步：面板「我的设备」添加设备并复制 token，填进 config.json 后重跑 doctor")
        return 2

    hp = Path(__file__).resolve().parent / handler
    if hp.is_file():
        print(f"  ✅ handler 存在：{hp}")
    else:
        print(f"✗ handler 文件不存在：{hp}（config.json 的 handler 只写文件名，别写路径）")
        ok = False

    if is_termux():
        missing = [c for c in ("termux-battery-status", "termux-notification", "termux-toast")
                   if shutil.which(c) is None]
        if missing:
            print(f"✗ 缺 Termux:API 命令：{', '.join(missing)}")
            print("  下一步：`pkg install termux-api`，并另外安装 Termux:API 应用")
            ok = False
        else:
            print("  ✅ Termux:API 可用（termux_handler.py 的动作能生效）")

    try:
        beat = api(cfg, "/device/heartbeat")
    except Exception as exc:                       # noqa: BLE001
        beat = {"error": f"{type(exc).__name__}: {exc}"}
    if "error" in beat:
        print(f"✗ 连不上 hub：{beat['error']}")
        print("  排查顺序：① hub 地址是否写对（远程必须是 https://agent.example.com/mcp-hub）"
              "② 这台设备能不能上网 ③ name/token 是否与面板完全一致")
        return 3
    queued = len(beat.get("commands", []))
    print(f"  ✅ hub 连通（本次取到 {queued} 条待执行指令）")

    if autostart_installed():
        print("  ✅ 已设置开机自启")
    else:
        print("  · 未设置开机自启（想要就运行 `python device_agent.py autostart`）")

    print("\n结论：" + ("一切正常，用 `python device_agent.py` 启动即可"
                    if ok else "有项目未通过（见上面 ✗），按提示处理后重跑 doctor"))
    return 0 if ok else 1


if __name__ == "__main__":
    _args = sys.argv[1:]
    if _args and _args[0] in ("autostart", "boot"):
        sys.exit(cmd_autostart("--remove" in _args))
    if _args and _args[0] in ("doctor", "selftest", "check"):
        sys.exit(cmd_doctor())
    if _args and _args[0] in ("-h", "--help", "help"):
        print(USAGE)
        sys.exit(0)
    main()
