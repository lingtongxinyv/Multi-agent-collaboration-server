"""文档漂移检查：UI/文案里承诺的命令与能力，必须真的存在于产品实现里。

为什么值得单独一套：
  这个项目已经因为"文案与实现不同步"踩过三次（向导漏掉 run、空状态仍写三步、
  Home 仍写三步接入）。这类错不会报错、不会崩，只会让用户照着做撞空 —— 必须自动查。

检查方向（都是"UI 承诺 → 实现必须存在"）：
  A. 连接器子命令：TSX 与已构建产物里出现的 `bellhub-connector <x>` 必须是真子命令
  B. 设备 agent 子命令：`device_agent.py <x>` 必须是真的
  C. 安卓 handler 动词：UI 列出的动词必须都在 termux_handler 的实现分支里
  D. 文案漂移：不得再出现"三步接入"（向导已是四步）
  E. 构建一致性：TSX 里出现的每条命令，必须也出现在**已构建的前端产物**里（防止改完忘了 build）
  F. 版本串格式：三个脚本的 VERSION 都必须是 日期形态

用法：~/dsh-suite/.venv/bin/python tests/test_docs_drift.py
"""
import os
import re
import sys
from pathlib import Path

# BELLHUB_DRIFT_ROOT 可把检查指向一份临时副本 —— 用于"故意注入漂移"验证本检查器真的会红
REPO = Path(os.environ.get("BELLHUB_DRIFT_ROOT") or Path(__file__).resolve().parents[1])
WORKERS = REPO / "workers"
PAGES = REPO / "panel" / "frontend" / "src" / "pages"
DIST = REPO / "panel" / "frontend" / "dist"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


# ── 实现侧：真有哪些子命令 / 动词 ────────────────────────────────
connector_src = read(WORKERS / "bellhub_connector.py")
agent_src = read(WORKERS / "device_agent.py")
termux_src = read(WORKERS / "termux_handler.py")

connector_subs = set(re.findall(r'sub\.add_parser\(\s*"([A-Za-z0-9_-]+)"', connector_src))
agent_subs = set()
for group in re.findall(r"_args\[0\] in \(([^)]*)\)", agent_src):
    agent_subs |= set(re.findall(r'"([^"]+)"', group))
agent_subs -= {"-h", "--help", "help", "boot", "selftest", "check"}   # 别名不算"要对齐"的正式名
termux_verbs = set(re.findall(r'verb == "([a-z]+)"', termux_src)) | {"help"}

print(f"[impl] 连接器子命令 {sorted(connector_subs)}")
print(f"[impl] 设备 agent 子命令 {sorted(agent_subs)}")
print(f"[impl] 安卓 handler 动词 {sorted(termux_verbs)}")

# ── 已构建的前端产物 ────────────────────────────────────────────
bundle = ""
idx = DIST / "index.html"
if idx.is_file():
    m = re.search(r'assets/(index-[A-Za-z0-9_-]+\.js)', read(idx))
    if m and (DIST / "assets" / m.group(1)).is_file():
        bundle = read(DIST / "assets" / m.group(1))
print(f"[ui]  已构建产物 {'找到：' + m.group(1) if bundle else '未找到'}")

agents_tsx = read(PAGES / "Agents.tsx")
devices_tsx = read(PAGES / "Devices.tsx")
home_tsx = read(PAGES / "Home.tsx")
ui_text = agents_tsx + devices_tsx + home_tsx

print("\n[A] 连接器子命令：UI 提到但实现没有 = 漂移")
mentioned = set(re.findall(r"bellhub-connector\s+([a-z][a-z-]*)", ui_text))
check("UI 至少提到若干子命令", len(mentioned) >= 4, str(sorted(mentioned)))
unknown = sorted(mentioned - connector_subs)
check("UI 提到的连接器子命令都真实存在", not unknown, f"未知：{unknown}")
for must in ("pair", "run", "autostart", "doctor", "print-config", "update", "version", "detect"):
    check(f"实现里确有 `{must}` 子命令", must in connector_subs, str(sorted(connector_subs)))

print("\n[B] 设备 agent 子命令")
agent_mentioned = set(re.findall(r"device_agent\.py\s+([a-z][a-z-]*)", ui_text))
check("UI 至少提到 doctor 与 autostart",
      {"doctor", "autostart"} <= agent_mentioned, str(sorted(agent_mentioned)))
for must in ("doctor", "autostart"):
    check(f"实现里确有 `{must}`", must in agent_subs, str(sorted(agent_subs)))
unknown_a = sorted(agent_mentioned - agent_subs)
check("UI 提到的 agent 子命令都真实存在", not unknown_a, f"未知：{unknown_a}")

print("\n[C] 安卓 handler 动词：UI 承诺的能力必须都实现")
advertised = set(re.findall(r"<code>(battery|wifi|location|clipboard|toast|notify|vibrate|torch|volume|photo)\b",
                            devices_tsx))
check("UI 广告了动词清单", len(advertised) >= 8, str(sorted(advertised)))
missing = sorted(advertised - termux_verbs)
check("广告里的动词都实现了", not missing, f"缺：{missing}")
check("实现里没有未广告的危险动词（白名单之外）",
      termux_verbs <= advertised | {"help"}, f"多出：{sorted(termux_verbs - advertised - {'help'})}")

print("\n[D] 文案漂移：向导已是四步")
check("Agents 页写的是四步接入", "四步接入" in agents_tsx)
for name, text in (("Agents.tsx", agents_tsx), ("Home.tsx", home_tsx),
                   ("Devices.tsx", devices_tsx), ("已构建产物", bundle)):
    check(f"{name} 无「三步接入」残留", "三步接入" not in text, "仍有旧文案")

print("\n[E] 构建一致性：源码改完必须重新构建")
if not bundle:
    check("前端产物存在（未构建无法核对）", False, str(DIST))
else:
    for cmd in sorted(mentioned | {"device_agent.py doctor", "device_agent.py autostart"}):
        full = cmd if cmd.startswith("device_agent") else f"bellhub-connector {cmd}"
        check(f"产物里含 `{full}`", full in bundle)

# ── 更多实现侧：设备内置指令 / 白名单 / MCP 工具 ──────────────
hub_src = read(REPO / "mcp-hub" / "hub.py")
main_src = read(REPO / "panel" / "backend" / "app" / "main.py")

builtin_impl = set(re.findall(r'cmd == "(![a-z]+)"', agent_src)) | set(
    re.findall(r'cmd in \(([^)]*)\)', agent_src)[0].split(", ") and
    re.findall(r'"(![a-z?]+)"', agent_src))
builtin_impl = {b for b in builtin_impl if b.startswith("!")}
ui_builtins = set(re.findall(r"cmd: '(!\w+)'", devices_tsx))
ui_files = set(re.findall(r"name: '([\w.]+\.py)'", devices_tsx))
whitelist = set(re.findall(r'"([\w.]+\.py)"',
                          re.search(r"DEVICE_AGENT_WHITELIST = \(([^)]*)\)", main_src).group(1)
                          if re.search(r"DEVICE_AGENT_WHITELIST = \(([^)]*)\)", main_src) else ""))
mcp_impl = set(re.findall(r"def (\w+)\(", hub_src))
ui_mcp = set(re.findall(r"\b(device_(?:list|send|results))\b", devices_tsx))

print("\n[G] 设备内置指令：UI 列的必须都实现")
check("UI 列了内置指令", len(ui_builtins) >= 4, str(sorted(ui_builtins)))
check("UI 列的内置指令都实现了", ui_builtins <= builtin_impl,
      f"未实现：{sorted(ui_builtins - builtin_impl)}（实现：{sorted(builtin_impl)}）")
check("实现的内置指令都在 UI 里说明过",
      {b for b in builtin_impl if b not in ("!?",)} <= ui_builtins,
      f"未说明：{sorted(builtin_impl - ui_builtins)}")

print("\n[H] 设备文件：UI 让下的文件必须存在、且在后端白名单里（否则用户下到 404）")
for f in sorted(ui_files):
    check(f"文件存在：{f}", (WORKERS / f).is_file())
    check(f"在下载白名单里：{f}", f in whitelist, f"白名单={sorted(whitelist)}")

print("\n[I] 站娘的设备能力：UI 描述的三件事，hub 里必须都有对应工具")
# 设备页是用中文描述能力的（看设备 / 下指令 / 取结果），并不写工具名 ——
# 所以这里查"能力描述 ↔ 工具实现"，而不是查字面工具名。
ui_caps = ("看有哪些设备", "下发指令", "取回结果")
check("设备页描述了三项能力", all(c in devices_tsx for c in ui_caps),
      str([c for c in ui_caps if c not in devices_tsx]))
for tool, cap in zip(("device_list", "device_send", "device_results"), ui_caps):
    check(f"hub 里有 {tool} 工具（对应「{cap}」）", f"def {tool}(" in hub_src)

print("\n[J] config.json 字段：UI 让人填的字段必须被 agent 读取")
for field in ("name", "token", "handler", "hub", "poll_interval"):
    check(f"agent 读 config 的 `{field}`", f'"{field}"' in agent_src or f"'{field}'" in agent_src,
          "未在 device_agent.py 里出现")

print("\n[K] 连接器自定义适配器字段：UI 提到的必须被实现支持")
if "custom_agents" in agents_tsx:
    check("实现支持 custom_agents", "custom_agents" in connector_src)
if "connector-builds" in agents_tsx or "connector-builds" in bundle:
    check("后端有 /api/link/connector-builds", "connector-builds" in main_src)

print("\n[F] 版本串形态")
for fname in ("bellhub_connector.py", "device_agent.py", "termux_handler.py"):
    ver = re.search(r'^VERSION\s*=\s*"([^"]+)"', read(WORKERS / fname), re.M)
    check(f"{fname} 有 VERSION 且是日期形态",
          bool(ver) and bool(re.match(r"\d{4}-\d{2}-\d{2}", ver.group(1))),
          ver.group(1) if ver else "缺失")

print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
