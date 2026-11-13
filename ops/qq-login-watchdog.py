#!/usr/bin/env python3
"""QQ 登录态看门狗（助手 2026-11-02）

背景：2026-10-30 18:48 起 NapCat 快速登录失效 → 没人发现 → 11-02 中午 NapCat 的
登录服务自己也断了，「二维码」循环彻底静默 5.5 小时；插件侧只表现为每分钟一条
`forward WS error: socket hang up`。入口静默腐烂 5 天，靠人肉看日志才发现。

本脚本每 2 分钟跑一次（systemd timer）：
  1. 探 OneBot `get_login_info`，能拿到 user_id 就算在线 → 什么都不做；
  2. 不在线且现有二维码超过 4 分钟没刷新 → `docker restart napcat` 造一张新码
     （失败重试限流：10 分钟内最多一次），等新码落盘；
  3. 把新码拷到 qq-login-qr.png，并把「扫码链接 + 落盘路径」挂到管理员星铃委托板
     （ref 幂等：qq-login:scan，重复只更新同一张卡，不刷屏）。

只读探针 + 有节流的容器重启；不碰 NapCat 配置、不碰插件配置、不碰凭据。
DRY_RUN=1 只打印计划，不重启、不挂账。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ENV_FILE = Path.home() / "dsh-suite/data/qq-bridge/env"
QUEST_TOKEN = Path.home() / "dsh-suite/data/quest-publish.token"
QUEST_URL = "http://127.0.0.1:8010/api/quests/publish"
QR_OUT = Path.home() / "dsh-suite/data/qq-bridge/qq-login-qr.png"
STATE = Path.home() / "dsh-suite/data/qq-bridge/.watchdog-state.json"

CONTAINER = "napcat"
QR_TTL_SEC = 240          # 二维码超过 4 分钟没刷新 = 循环已死
RESTART_MIN_INTERVAL = 600  # 10 分钟内最多重启一次
DRY = os.environ.get("DRY_RUN") == "1"


def log(msg: str) -> None:
    print(f"[qq-watchdog {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_env() -> dict:
    out = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


def api_ok(base: str, token: str) -> tuple[bool, str]:
    req = urllib.request.Request(
        f"{base}/get_login_info",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        uid = (data.get("data") or {}).get("user_id")
        if uid:
            return True, str(uid)
        return False, "no user_id"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def qr_mtime() -> float:
    try:
        r = docker("exec", CONTAINER, "stat", "-c", "%Y", "/app/napcat/cache/qrcode.png")
        return float((r.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def fresh_qr_url() -> str:
    try:
        r = docker("logs", "--since", "10m", CONTAINER, timeout=20)
        text = r.stdout + r.stderr
    except Exception:  # noqa: BLE001
        return ""
    url = ""
    for line in text.splitlines():
        if "txz.qq.com/p?k=" in line:
            url = line.split("txz.qq.com/p?k=")[-1].strip().split()[0]
            url = "https://txz.qq.com/p?k=" + url
    return url


def publish(title: str, detail: str) -> None:
    token = QUEST_TOKEN.read_text(encoding="utf-8").strip()
    payload = {
        "user": "admin",
        "title": title[:80],
        "detail": detail[:300],
        "category": "main",
        "rarity": "rare",
        "target": 1,
        "reward": "QQ 上能继续找到助手",
        "ref": "qq-login:scan",
        "origin": "助手",
    }
    req = urllib.request.Request(
        QUEST_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Quest-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        log(f"quest -> {resp.status}")


def main() -> int:
    env = load_env()
    base = os.environ.get("ONEBOT_API") or env.get("ONEBOT_API") or "http://127.0.0.1:3000"
    token = env.get("ONEBOT_TOKEN", "")

    ok, info = api_ok(base, token)
    if ok:
        if STATE.exists():
            STATE.unlink(missing_ok=True)
            log(f"已恢复登录 user_id={info}（看门狗状态清零）")
        return 0

    log(f"未登录：{info}")
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    last_restart = float(state.get("last_restart") or 0)
    now = time.time()
    mtime = qr_mtime()

    if now - mtime > QR_TTL_SEC and now - last_restart > RESTART_MIN_INTERVAL:
        log(f"二维码已 {int(now - mtime)}s 未刷新，重启 {CONTAINER} 造新码")
        if not DRY:
            docker("restart", CONTAINER, timeout=60)
            last_restart = now
            for _ in range(20):
                time.sleep(3)
                if qr_mtime() > now - 5:
                    break
    elif now - mtime > QR_TTL_SEC:
        log(f"二维码陈旧，但 {int(now - last_restart)}s 前刚重启过 → 本轮不重复重启")

    url = fresh_qr_url()
    png_ok = False
    if not DRY:
        r = docker("cp", f"{CONTAINER}:/app/napcat/cache/qrcode.png", str(QR_OUT))
        png_ok = r.returncode == 0
        STATE.write_text(json.dumps({"last_restart": last_restart, "at": now}), encoding="utf-8")
        detail = (
            "NapCat 掉登录态，QQ 通道不通（插件只会每分钟报 socket hang up，不报警）。"
            f"新二维码：{QR_OUT}；解码链接：{'https://txz.qq.com/p?k=' + url if url else '（尚未生成，稍后重试）'}。"
            "用手机 QQ 扫一下即可，扫完看门狗自动清零。"
        )
        try:
            publish("QQ 掉线了：扫一下码让助手回到 QQ", detail)
        except Exception as exc:  # noqa: BLE001
            log(f"挂账失败（不影响重启逻辑）：{exc}")
    else:
        log(f"[DRY] 会拷贝二维码={QR_OUT}，链接={url or '无'}，并挂账")

    log(f"qr_age={int(now - mtime) if mtime else 'NA'}s url={'有' if url else '无'} png={png_ok}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"看门狗自身异常：{type(exc).__name__}: {exc}")
        sys.exit(1)
