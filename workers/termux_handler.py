#!/usr/bin/env python3
"""Termux 手机处理器（安卓）：把指令映射成**固定的** Termux:API 调用。

为什么单独写一个而不是用 shell_handler.py：
  手机是与管理员形影不离的设备，一旦 shell 化，token 泄露等于把手机交出去。
  本 handler **不接受任意命令**，只认下列动词，且每个动词都对应一条写死的 argv ——
  用户文本只会作为**一个独立的参数**传入，全程 shell=False，永远没有字符串拼命令的机会。

用法：在 config.json 里把 "handler" 改为 "termux_handler.py"（device_agent 会自动
以 `python termux_handler.py <cmd_id> <text>` 调用本文件）。

支持的指令：
  help                        显示本帮助
  battery                     电池状态（电量/温度/充电中？）
  wifi                        当前 Wi-Fi 连接信息
  location                    当前位置（需在手机上授权定位）
  clipboard                   读剪贴板
  clipboard set <文本>        写剪贴板
  toast <文本>                屏幕浮层提示
  notify <标题> | <内容>      发系统通知
  vibrate [毫秒]              震动（默认 500）
  torch on|off                手电筒开关
  volume <通道> <0-15>        音量；通道 = music/call/ring/alarm/notification/system
  photo [back|front]          拍照并返回文件路径（需存储权限）

前置条件（缺一不可）：
  1) Termux 里执行：pkg install python termux-api
  2) 另外安装 Termux:API 应用（F-Droid 或 Google Play 同名 app），否则 termux-* 命令无效
  3) 定位/拍照首次使用会在手机上弹权限，需要管理员点允许
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

VERSION = "2026-10-14.1"
TIMEOUT = 30
VOLUME_STREAMS = ("music", "call", "ring", "alarm", "notification", "system")

HELP = (
    "Termux 手机处理器（白名单模式，不执行任意命令）支持：\n"
    "  help / battery / wifi / location / clipboard / clipboard set <文本>\n"
    "  toast <文本> / notify <标题> | <内容> / vibrate [毫秒]\n"
    "  torch on|off / volume <通道> <0-15> / photo [back|front]\n"
    "通道：music / call / ring / alarm / notification / system"
)


def call(argv: list[str], cwd: str | None = None) -> tuple[str, bool]:
    """执行一条固定 argv；用户文本只会落在单个参数位上（shell=False）。"""
    exe = shutil.which(argv[0])
    if exe is None:
        return (f"缺少命令 {argv[0]}：请先 `pkg install termux-api` 并安装 Termux:API 应用", False)
    try:
        proc = subprocess.run([exe, *argv[1:]], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT, cwd=cwd)
    except subprocess.TimeoutExpired:
        return (f"{argv[0]} 执行超时（>{TIMEOUT}s）", False)
    except OSError as exc:
        return (f"{argv[0]} 启动失败：{exc}", False)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return (f"{argv[0]} 退出码 {proc.returncode}：{err or out or '无输出'}", False)
    return (out or err or "（无输出）"), True


def pretty(raw: str) -> str:
    """termux-api 多数返回 JSON：能解析就美化，方便站娘与用户阅读。"""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=1)
    except (json.JSONDecodeError, TypeError):
        return raw


def dispatch(text: str) -> tuple[str, bool]:
    parts = text.strip().split(None, 1)
    if not parts:
        return HELP, False
    verb = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if verb in ("help", "!help", "?"):
        return HELP, True
    if verb == "battery":
        out, ok = call(["termux-battery-status"])
        return pretty(out), ok
    if verb == "wifi":
        out, ok = call(["termux-wifi-connectioninfo"])
        return pretty(out), ok
    if verb == "location":
        out, ok = call(["termux-location", "-p", "network"])
        return pretty(out), ok
    if verb == "clipboard":
        if rest.lower().startswith("set "):
            return call(["termux-clipboard-set", rest[4:]])
        return call(["termux-clipboard-get"])
    if verb == "toast":
        if not rest:
            return "用法：toast <文本>", False
        return call(["termux-toast", "-g", "middle", rest])
    if verb == "notify":
        title, sep, content = rest.partition("|")
        title, content = title.strip() or "铃枢", content.strip() if sep else ""
        argv = ["termux-notification", "--title", title]
        if content:
            argv += ["--content", content]
        return call(argv)
    if verb == "vibrate":
        ms = rest if rest.isdigit() else "500"
        return call(["termux-vibrate", "-d", ms])
    if verb == "torch":
        if rest.lower() not in ("on", "off"):
            return "用法：torch on|off", False
        return call(["termux-torch", rest.lower()])
    if verb == "volume":
        chunk = rest.split()
        if len(chunk) != 2 or chunk[0].lower() not in VOLUME_STREAMS or not chunk[1].isdigit():
            return "用法：volume <通道> <0-15>；通道 = " + "/".join(VOLUME_STREAMS), False
        if not 0 <= int(chunk[1]) <= 15:
            return "音量必须在 0-15 之间", False
        return call(["termux-volume", chunk[0].lower(), chunk[1]])
    if verb == "photo":
        cam = "1" if rest.strip().lower() == "front" else "0"
        cmd_id = sys.argv[1] if len(sys.argv) > 1 else "x"
        # 有共享存储就写 /sdcard/DCIM，否则落到当前目录；文件名不含用户文本
        base = "/sdcard/DCIM" if os.path.isdir("/sdcard/DCIM") else "."
        return call(["termux-camera-photo", "-c", cam, f"{base}/bellhub-{cmd_id}.jpg"])

    # ⚠ 关键安全属性：认不出的指令**绝不**交给 shell，只回帮助
    return (f"不支持的指令：{verb}\n（本 handler 是白名单模式，不会执行任意命令）\n\n" + HELP), False


def main() -> int:
    text = sys.argv[2] if len(sys.argv) > 2 else ""
    out, ok = dispatch(text)
    print(out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
