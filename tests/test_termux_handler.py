"""Termux 手机 handler 测试：白名单映射 + 绝无 shell 注入 + 缺依赖时明说。

用假的 termux-* 可执行文件模拟手机，重点验证**注入不可能发生**：
用户文本只会落在单个 argv 参数位上，且全程 shell=False。

用法：~/dsh-suite/.venv/bin/python tests/test_termux_handler.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ── 仓库内相对定位（2026-10-14 从 /root/1012 迁入 tests/）──
REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"
WORKERS = REPO / "workers"
MCPHUB = REPO / "mcp-hub"

HANDLER = str(WORKERS / "termux_handler.py")
ROOT = Path(tempfile.mkdtemp(prefix="termux-"))
FAKE = ROOT / "bin"
FAKE.mkdir()
MARKER = ROOT / "pwned"
passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if (extra and not cond) else ""))


ARGV_ECHO = '#!/bin/sh\necho "ARGV_COUNT:$#"\nfor a in "$@"; do echo "ARG:$a"; done\n'


def fake(name: str, body: str = ARGV_ECHO) -> None:
    p = FAKE / name
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)


fake("termux-battery-status", '#!/bin/sh\necho \'{"percentage": 87, "temperature": 31.5}\'\n')
fake("termux-wifi-connectioninfo", '#!/bin/sh\necho \'{"ssid": "home-wifi"}\'\n')
for n in ("termux-notification", "termux-clipboard-set", "termux-clipboard-get",
          "termux-toast", "termux-vibrate", "termux-torch", "termux-volume",
          "termux-location", "termux-camera-photo"):
    fake(n)

ENV = {**os.environ, "PATH": f"{FAKE}:{os.environ['PATH']}"}
EMPTY_PATH_ENV = {**os.environ, "PATH": str(ROOT / "empty")}
(ROOT / "empty").mkdir()


def run(text: str, env: dict | None = None) -> tuple[int, str]:
    r = subprocess.run([sys.executable, HANDLER, "7", text], env=env or ENV,
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


print("[1] 基本动作映射")
st, out = run("help")
check("help 列出白名单", st == 0 and "白名单模式" in out, out[:120])
st, out = run("battery")
check("battery 返回并美化 JSON", st == 0 and '"percentage": 87' in out, out[:160])
st, out = run("wifi")
check("wifi 走固定命令", st == 0 and "home-wifi" in out, out[:120])

print("\n[2] 参数按「独立 argv」传递（不是拼成一条命令）")
st, out = run("notify 到家了 | 记得关灯")
check("notify 标题/内容分列成两个参数",
      st == 0 and "ARG:--title" in out and "ARG:到家了" in out
      and "ARG:--content" in out and "ARG:记得关灯" in out, out[:220])
st, out = run("clipboard set 你好 世界")
check("clipboard set 整段作为单个参数",
      st == 0 and "ARGV_COUNT:1" in out and "ARG:你好 世界" in out, out[:220])
st, out = run("volume music 7")
check("volume 通道+数值分开传", st == 0 and "ARG:music" in out and "ARG:7" in out, out[:200])
st, out = run("vibrate 300")
check("vibrate 毫秒作为独立参数", st == 0 and "ARG:-d" in out and "ARG:300" in out, out[:200])

print("\n[3] 参数校验：非法输入一律拒绝")
for bad, why in [("torch maybe", "torch 非 on/off"), ("volume music 99", "音量越界"),
                 ("volume bogus 3", "通道不在白名单"), ("volume music abc", "音量非数字")]:
    st, out = run(bad)
    check(f"拒绝：{why}", st == 1 and ("用法" in out or "0-15" in out), f"{st} {out[:80]}")

print("\n[4] 注入不可能发生（最关键的安全属性）")
st, out = run("battery; touch " + str(MARKER))
check("动词必须精确匹配（battery; 被整体拒绝）", st == 1 and "不支持的指令" in out,
      f"{st} {out[:80]}")
check("分号注入未被执行", not MARKER.exists(), f"exists={MARKER.exists()}")
st, out = run("battery && touch " + str(MARKER))
check("&& 注入未被执行（多余尾巴被忽略）", not MARKER.exists(), f"{st} exists={MARKER.exists()}")
st, out = run("clipboard set x; touch " + str(MARKER))
check("分号原样作为一个参数（无 shell 解释）",
      st == 0 and "ARG:x; touch" in out.replace("\n", " ") and not MARKER.exists(),
      out[:200].replace("\n", " "))
st, out = run("$(touch " + str(MARKER) + ")")
check("命令替换未被展开", st == 1 and not MARKER.exists(), f"{st} exists={MARKER.exists()}")
st, out = run("rm -rf " + str(ROOT))
check("任意命令被拒（白名单之外）", st == 1 and "不支持的指令" in out and ROOT.exists(), out[:120])
src = Path(HANDLER).read_text(encoding="utf-8")
check("源码里没有 shell=True", "shell=True" not in src)

print("\n[5] 缺 Termux:API 时要说清怎么装")
st, out = run("battery", EMPTY_PATH_ENV)
check("提示 pkg install termux-api", st == 1 and "termux-api" in out and "Termux:API" in out,
      out[:160])
st, out = run("help", EMPTY_PATH_ENV)
check("help 不依赖外部命令", st == 0 and "白名单模式" in out, out[:80])

print(f"\n结果：{len(passed)} 项通过，{len(failed)} 项失败")
if failed:
    print("失败项：" + "；".join(failed))
    sys.exit(1)
print("全部通过 ✅")
