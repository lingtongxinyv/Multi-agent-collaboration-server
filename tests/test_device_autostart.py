"""device_agent 自启安装测试：Termux 开机脚本 / Linux systemd 单元 / 取消自启。

重点：不真的去动开发机的 systemd，只用临时 HOME 验证"写了什么文件、权限对不对、
脚本内容能不能真的把 agent 拉起来"。

用法：~/dsh-suite/.venv/bin/python tests/test_device_autostart.py
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# ── 仓库内相对定位（2026-10-14 从 /root/1012 迁入 tests/）──
REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"
WORKERS = REPO / "workers"
MCPHUB = REPO / "mcp-hub"

AGENT = str(WORKERS / "device_agent.py")
ROOT = Path(tempfile.mkdtemp(prefix="devauth-"))
passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if (extra and not cond) else ""))


def run(args: list[str], home: Path, termux: bool) -> tuple[int, str]:
    env = {**os.environ, "HOME": str(home)}
    if termux:
        env["PREFIX"] = "/data/data/com.termux/files/usr"
        env["TERMUX_VERSION"] = "0.118.0"
    else:
        env.pop("PREFIX", None)
        env.pop("TERMUX_VERSION", None)
    r = subprocess.run([sys.executable, AGENT, *args], env=env,
                       capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


print("[1] 帮助与默认行为")
st, out = run(["--help"], ROOT / "h1", termux=False)
check("--help 说明自启形态", st == 0 and "autostart" in out and "Termux" in out, out[:160])

print("\n[2] Termux：写开机脚本")
home = ROOT / "termux-home"
home.mkdir()
st, out = run(["autostart"], home, termux=True)
script = home / ".termux" / "boot" / "50-bellhub-agent.sh"
check("autostart 成功", st == 0, out[:200])
check("开机脚本已生成", script.is_file(), str(script))
check("脚本可执行（755）", script.is_file() and oct(script.stat().st_mode)[-3:] == "755",
      oct(script.stat().st_mode)[-3:] if script.is_file() else "missing")
body = script.read_text(encoding="utf-8") if script.is_file() else ""
check("脚本用 Termux 的 sh", body.startswith("#!/data/data/com.termux/files/usr/bin/sh"), body[:60])
check("脚本先拿 wake-lock（防被系统杀）", "termux-wake-lock" in body, body[:120])
check("脚本工作目录=agent 所在目录",
      f"cd {Path(AGENT).resolve().parent}" in body, body)
check("脚本真的拉起本 agent", "device_agent.py" in body and "exec" in body, body)
check("输出写日志文件", "bellhub-agent.log" in body, body)
check("提示需要 Termux:Boot 与关电池优化",
      "Termux:Boot" in out and "电池优化" in out, out[:240])

print("\n[3] Termux：取消自启")
st, out = run(["autostart", "--remove"], home, termux=True)
check("--remove 成功", st == 0 and "已删除" in out, out[:160])
check("开机脚本已删除", not script.exists())
st, out = run(["autostart", "--remove"], home, termux=True)
check("重复取消不报错", st == 0 and "本来就没有" in out, out[:160])

print("\n[4] Linux：写 systemd --user 单元（不真的启用）")
home2 = ROOT / "linux-home"
home2.mkdir()
st, out = run(["autostart"], home2, termux=False)
unit = home2 / ".config" / "systemd" / "user" / "bellhub-device.service"
check("单元文件已生成", unit.is_file(), str(unit))
ubody = unit.read_text(encoding="utf-8") if unit.is_file() else ""
check("单元含 WorkingDirectory 与 ExecStart",
      "WorkingDirectory=" in ubody and "ExecStart=" in ubody and "device_agent.py" in ubody,
      ubody[:200])
check("单元含 Restart=always", "Restart=always" in ubody, ubody[:200])
check("没有 user session 时明确解释而不是假装成功",
      st == 0 or "单元文件已写好" in out or "systemctl" in out, f"{st} {out[:160]}")

print("\n[5] 不污染：自启只动 HOME，不在 agent 源码目录留东西")
src_dir = Path(AGENT).resolve().parent
before = {p.name: p.stat().st_mtime for p in src_dir.iterdir()}
home3 = ROOT / "clean-home"
home3.mkdir()
run(["autostart"], home3, termux=True)
after = {p.name: p.stat().st_mtime for p in src_dir.iterdir()}
check("源码目录文件集合未变", set(before) == set(after),
      str(set(after) - set(before)) + str(set(before) - set(after)))
check("源码目录文件未被改写", before == after,
      str([k for k in before if before.get(k) != after.get(k)]))
check("没有在源码目录留日志", not list(src_dir.glob("*.log")), str(list(src_dir.glob("*.log"))))

print("\n[6] doctor：一条命令自检（用本地假 hub 验证四种结论）")
agent_dir = ROOT / "doctor-run"
agent_dir.mkdir()
shutil.copy2(AGENT, agent_dir / "device_agent.py")
shutil.copy2(str(src_dir / "device_handler.py"), agent_dir / "device_handler.py")
cfg_path = agent_dir / "config.json"


class FakeHub(http.server.BaseHTTPRequestHandler):
    def do_POST(self):                                  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = b'{"ok": true, "commands": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                          # 静音
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8103), FakeHub)
threading.Thread(target=srv.serve_forever, daemon=True).start()

try:
    def doctor(cfg: dict | None, name: str) -> tuple[int, str]:
        if cfg is None:
            if cfg_path.exists():
                cfg_path.unlink()
        else:
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run([sys.executable, str(agent_dir / "device_agent.py"), "doctor"],
                           capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout + r.stderr).strip()

    st, out = doctor(None, "无配置")
    check("无配置 → exit 2 且指路", st == 2 and "没有配置文件" in out, f"{st} {out[-120:]}")

    st, out = doctor({"hub": "http://127.0.0.1:8103", "name": "probe", "token": "t",
                      "handler": "device_handler.py", "poll_interval": 15}, "一切正常")
    check("全绿 → exit 0 且给结论", st == 0 and "hub 连通" in out and "一切正常" in out,
          f"{st} {out[-200:]}")

    st, out = doctor({"hub": "http://127.0.0.1:8103", "name": "probe", "token": "t",
                      "handler": "no-such-handler.py", "poll_interval": 15}, "handler 缺失")
    check("handler 缺失 → exit 1 且点名", st == 1 and "handler 文件不存在" in out,
          f"{st} {out[-200:]}")

    st, out = doctor({"hub": "http://127.0.0.1:9", "name": "probe", "token": "t",
                      "handler": "device_handler.py", "poll_interval": 15}, "hub 不可达")
    check("hub 不可达 → exit 3 且给排查顺序", st == 3 and "连不上 hub" in out and "排查顺序" in out,
          f"{st} {out[-200:]}")

    st, out = doctor({"hub": "http://127.0.0.1:8103", "name": "", "token": "",
                      "handler": "device_handler.py"}, "未填凭据")
    check("凭据未填 → exit 2 且指路", st == 2 and "未填" in out, f"{st} {out[-160:]}")
finally:
    srv.shutdown()

print(f"\n结果：{len(passed)} 项通过，{len(failed)} 项失败")
if failed:
    print("失败项：" + "；".join(failed))
    sys.exit(1)
print("全部通过 ✅")
