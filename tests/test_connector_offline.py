"""连接器离线韧性验收：hub 不在时不能崩、要能自愈、单轮模式要如实报失败。

覆盖三种"连不上"的形态：
  A. hub 地址写错 / 服务没起 → clear 报错 + 退避重试，**不崩**
  B. hub 后来起来了 → 自动恢复并继续领活（不需要人工重启）
  C. `run --once`（计划任务/自检用法）在连不上时必须**非零退出**（退出码 9）
  D. `doctor` 在同样情况下给出可操作的排查顺序

用法：~/dsh-suite/.venv/bin/python tests/test_connector_offline.py
"""
import hashlib  # noqa: F401  (与其它套件保持一致的导入风格)
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"
WORKERS = REPO / "workers"
CONNECTOR = WORKERS / "bellhub_connector.py"

ROOT = Path(tempfile.mkdtemp(prefix="offline-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT / "data")
PANEL_PORT, HUB_PORT = 8091, 8090
BASE = f"http://127.0.0.1:{PANEL_PORT}"
HUB = f"http://127.0.0.1:{HUB_PORT}"

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"隔离失败: {D.DB_PATH}"
db = D.get_db()
alice = db.create_account("offline-user", "pw-offline-1", "user", "离线验收用户")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


FAKE_BIN = ROOT / "bin"
FAKE_BIN.mkdir()
fake = FAKE_BIN / "dsh"
fake.write_text('#!/bin/sh\necho "FAKE-DSH-OK"\n', encoding="utf-8")
fake.chmod(0o755)

panel_env = {**os.environ, "DSH_SUITE_DATA": str(ROOT / "data"),
             "MCP_TOKEN_FILE": str(ROOT / "mcp_token"),
             "BELLHUB_WORKERS_DIR": str(WORKERS)}
panel = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                          "--port", str(PANEL_PORT), "--log-level", "warning"],
                         cwd=str(BACKEND), env=panel_env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/api/auth/me", timeout=1)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(0.5)

CONN_HOME = ROOT / "conn"
conn_env = {**os.environ, "BELLHUB_HOME": str(CONN_HOME), "PYTHONUNBUFFERED": "1",
            "BELLHUB_HUB_URL": HUB, "PATH": f"{FAKE_BIN}:{os.environ['PATH']}"}
hub_proc = None


def approve_pair() -> str:
    """起连接器 pair 拿码，用面板确认，返回队员名。"""
    proc = subprocess.Popen([sys.executable, str(CONNECTOR), "pair", "--base", BASE,
                             "--no-browser", "--name", "offline-pc"],
                            env=conn_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    code = None
    deadline = time.time() + 40
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        m = re.search(r"配对码：\s*([A-Z0-9]{8})", line)
        if m:
            code = m.group(1)
            break
    if code:
        req = urllib.request.Request(
            BASE + "/api/link/approve", data=json.dumps({"user_code": code}).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Cookie": ""})  # 下面用会话再发一次
        # 用面板登录后的会话确认
        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        r = urllib.request.Request(BASE + "/api/auth/login",
                                   data=json.dumps({"username": "offline-user",
                                                    "password": "pw-offline-1"}).encode(),
                                   method="POST", headers={"Content-Type": "application/json"})
        op.open(r, timeout=15)
        r = urllib.request.Request(BASE + "/api/link/approve",
                                   data=json.dumps({"user_code": code}).encode(), method="POST",
                                   headers={"Content-Type": "application/json"})
        op.open(r, timeout=15)
    proc.wait(timeout=30)
    cfg_file = CONN_HOME / "connector.json"
    cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
    # 心跳调小，让"恢复"在一两秒内发生
    if cfg:
        cfg["heartbeat"] = 3
        cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg.get("name", "")


try:
    print("① 配对（此时 hub 还没起 —— 配对只走面板）")
    member = approve_pair()
    check("配对成功拿到队员名", member == "offline-pc", member)

    print("\n② [A] hub 不在：run --once 必须如实报失败")
    sub = db.create_subtask("offline-parent", 1, "断网时的活", "随便做点事", "general",
                            assignee=member)
    t0 = time.time()
    r1 = subprocess.run([sys.executable, str(CONNECTOR), "run", "--once"], env=conn_env,
                        capture_output=True, text=True, timeout=60)
    check("单轮模式非零退出（退出码 9）", r1.returncode == 9, f"rc={r1.returncode}")
    check("明确说清未成功而非静默成功",
          "未成功" in r1.stdout and "退出码 9" in r1.stdout, r1.stdout[-200:])
    check("提示去跑 doctor", "doctor" in r1.stdout, r1.stdout[-160:])
    check("任务仍然待领取（没被误标完成）",
          db.get_subtask(sub["id"])["status"] == "pending", db.get_subtask(sub["id"])["status"])
    print(f"     （耗时 {time.time() - t0:.1f}s）")

    print("\n③ [D] doctor 在同样情况下给出排查顺序")
    rdoc = subprocess.run([sys.executable, str(CONNECTOR), "doctor"], env=conn_env,
                          capture_output=True, text=True, timeout=90)
    check("doctor 非零退出", rdoc.returncode == 3, f"rc={rdoc.returncode}")
    check("doctor 指出领活通道不通",
          "领活通道" in rdoc.stdout or "站点不可达" in rdoc.stdout, rdoc.stdout[-260:])

    print("\n④ [B] 常驻循环：先断（hub 还没起）→ 再连上，必须在**同一个进程内**自愈")
    # 关键顺序：先启动 run（hub 不在），让它真的经历失败与退避，再把 hub 拉起来。
    # （上一版把 hub 先起了，等于没测到"自愈"，断言因此失败 —— 测试顺序即测试有效性。）
    runner = subprocess.Popen([sys.executable, str(CONNECTOR), "run"], env=conn_env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(6)                      # 心跳 3s：够它失败一次并进入退避
    hub_proc = subprocess.Popen([sys.executable, str(REPO / "mcp-hub" / "hub.py"),
                                 "127.0.0.1", str(HUB_PORT)],
                                cwd=str(REPO / "mcp-hub"), env=panel_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            urllib.request.urlopen(HUB + "/agent/heartbeat", data=b"{}", timeout=1)
            break
        except urllib.error.HTTPError:
            break
        except Exception:
            time.sleep(0.3)

    deadline = time.time() + 45
    status = "pending"
    while time.time() < deadline:
        status = db.get_subtask(sub["id"])["status"]
        if status == "done":
            break
        time.sleep(1)
    check("连接器自愈后把积压的任务做完了", status == "done", f"status={status}")
    got = db.get_subtask(sub["id"])
    check("结果来自本机 AI", "FAKE-DSH-OK" in (got.get("result") or ""), str(got)[:160])

    # 再做一件新活，证明循环没停
    sub2 = db.create_subtask("offline-parent", 2, "恢复后的活", "再来一件", "general",
                             assignee=member)
    deadline = time.time() + 30
    while time.time() < deadline:
        if db.get_subtask(sub2["id"])["status"] == "done":
            break
        time.sleep(1)
    check("恢复后仍能连续领活", db.get_subtask(sub2["id"])["status"] == "done",
          db.get_subtask(sub2["id"])["status"])

    runner.terminate()
    try:
        out, _ = runner.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        runner.kill()
        out, _ = runner.communicate()
    check("同一进程内先出现退避提示（证明真的经历了断网）",
          "心跳异常" in out or "循环异常" in out, out[-300:])
    check("同一进程内随后恢复正常（自愈而非重启）",
          "领到任务" in out or "完成" in out, out[-300:])
    check("进程未被异常终止（是我们主动停的）", runner.returncode in (-15, 0, None),
          str(runner.returncode))
finally:
    for p in (hub_proc, panel):
        if p is not None:
            p.terminate()
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()

print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
