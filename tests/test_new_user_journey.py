"""新用户全程验收：从零到绿灯，按「我的智能体」向导的四步走一遍。

这不是重复 test_connector_e2e —— 那个测的是机制，这个测的是**用户视角的引导是否闭环**：
    ① 下载连接器（面板点一下）
    ② bellhub-connector pair
    ③ 浏览器点「确认」
    ④ bellhub-connector run / autostart（让它常驻）
外加一条"文档与产品一致"检查：向导里写的命令必须真的存在于前端产物里，
否则用户照着做会撞空。

用法：~/dsh-suite/.venv/bin/python tests/test_new_user_journey.py
"""
import hashlib
import http.cookiejar
import json
import os
import re
import socket
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

ROOT = Path(tempfile.mkdtemp(prefix="journey-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT / "data")
PANEL_PORT, HUB_PORT = 8094, 8093
BASE = f"http://127.0.0.1:{PANEL_PORT}"
HUB = f"http://127.0.0.1:{HUB_PORT}"

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"隔离失败: {D.DB_PATH}"

PASS, FAIL, STEPS = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def step(text: str) -> None:
    STEPS.append(text)
    print(f"\n▸ 第 {len(STEPS)} 步：{text}")


# ── 假的本机 AI（用户电脑上"已装好的 AI 应用"）──────────────────
FAKE_BIN = ROOT / "bin"
FAKE_BIN.mkdir()
fake = FAKE_BIN / "dsh"
fake.write_text('#!/bin/sh\necho "FAKE-DSH-OK args=$*"\n', encoding="utf-8")
fake.chmod(0o755)

env = {**os.environ, "DSH_SUITE_DATA": str(ROOT / "data"),
       "MCP_TOKEN_FILE": str(ROOT / "mcp_token"),
       "BELLHUB_WORKERS_DIR": str(WORKERS)}
panel = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                          "--port", str(PANEL_PORT), "--log-level", "warning"],
                         cwd=str(BACKEND), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
hub = subprocess.Popen([sys.executable, str(REPO / "mcp-hub" / "hub.py"),
                        "127.0.0.1", str(HUB_PORT)],
                       cwd=str(REPO / "mcp-hub"), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/api/auth/me", timeout=1)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(0.5)
for _ in range(60):
    try:
        urllib.request.urlopen(HUB + "/agent/heartbeat", data=b"{}", timeout=1)
        break
    except urllib.error.HTTPError:
        break          # 401 = hub 就绪
    except Exception:
        time.sleep(0.5)

CONN_HOME = ROOT / "conn"
conn_env = {**os.environ, "BELLHUB_HOME": str(CONN_HOME), "PYTHONUNBUFFERED": "1",
            "BELLHUB_HUB_URL": HUB, "PATH": f"{FAKE_BIN}:{os.environ['PATH']}"}


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _req(self, method, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.op.open(r, timeout=60) as resp:
                b = resp.read()
                return resp.status, (b if raw else (json.loads(b) if b else {}))
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, (b if raw else json.loads(b or b"{}"))
            except json.JSONDecodeError:
                return e.code, (b if raw else {})

    def get(self, p, raw=False):
        return self._req("GET", p, raw=raw)

    def post(self, p, b=None):
        return self._req("POST", p, b if b is not None else {})


try:
    # ═══ 第 0 步：一个全新账号（模拟"所有用户"里的任何一位）═══
    print("═" * 62)
    print("新用户全程验收：从零到绿灯（隔离环境，不碰真实数据）")
    print("═" * 62)
    D.get_db().create_account("newbie", "pw-newbie-1", "user", "新用户")
    me = Client()
    st, r = me.post("/api/auth/login", {"username": "newbie", "password": "pw-newbie-1"})
    check("新账号可登录", st == 200 and r.get("username") == "newbie", f"{st} {r}")
    st, agents = me.get("/api/link/agents")
    check("初始没有任何队员", st == 200 and agents == [], f"{st} {agents}")
    st, r = me.post("/api/link/dispatch", {"prompt": "先试试"})
    check("没有队员时派活报错且指路", st == 400 and "连接器" in str(r), f"{st} {r}")

    # ═══ ① 下载连接器 ═══
    step("下载连接器（面板点一下）")
    st, blob = me.get("/api/devices/agent-files/bellhub_connector.py", raw=True)
    real = CONNECTOR.read_bytes()
    check("能下载到连接器", st == 200 and blob == real, f"HTTP {st} len={len(blob) if blob else 0}")
    check("下载内容与仓库一致（可校对哈希）",
          hashlib.sha256(blob).hexdigest() == hashlib.sha256(real).hexdigest())
    st, meta = me.get("/api/devices/agent-files")
    one = next((x for x in meta if x["name"] == "bellhub_connector.py"), {})
    check("下载页标出版本与 sha256", bool(one.get("version")) and len(one.get("sha256", "")) == 64,
          str(one)[:150])

    # ═══ ② 运行 pair ═══
    step("运行 bellhub-connector pair（终端里跑一次）")
    proc = subprocess.Popen([sys.executable, str(CONNECTOR), "pair", "--base", BASE,
                             "--no-browser", "--name", "newbie-pc"],
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
        if m and code is None:
            code = m.group(1)
            break
    check("连接器出示 8 位配对码", bool(code), "没读到配对码")

    # ═══ ③ 浏览器里点「确认」 ═══
    step("在浏览器里点「确认」（确认页把请求绑到自己的账号）")
    st, info = me.get(f"/api/link/pair-code?code={code}")
    check("确认页能显示设备信息", st == 200 and info.get("agent_name") == "newbie-pc", f"{st} {info}")
    st, r = me.post("/api/link/approve", {"user_code": code})
    check("确认成功", st == 200 and r.get("ok"), f"{st} {r}")
    proc.wait(timeout=30)
    check("pair 正常结束", proc.returncode == 0, f"rc={proc.returncode}")
    cfg_file = CONN_HOME / "connector.json"
    cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
    check("凭据已落到本机（0600）", cfg_file.is_file() and oct(cfg_file.stat().st_mode)[-3:] == "600")
    st, agents = me.get("/api/link/agents")
    check("面板名册里出现这台机器", st == 200 and len(agents) == 1
          and agents[0]["name"] == "newbie-pc", f"{st} {agents}")

    # ═══ ④ 让它常驻（run / autostart）═══
    step("让它常驻（run 或 autostart）—— 这一步决定它是否真的在线")
    rd = subprocess.run([sys.executable, str(CONNECTOR), "doctor"], env=conn_env,
                        capture_output=True, text=True, timeout=90)
    check("doctor 说可以开工", rd.returncode == 0 and "一切正常" in rd.stdout,
          f"rc={rd.returncode} {rd.stdout[-200:]}")

    st, r = me.post("/api/link/dispatch", {"prompt": "统计一下今天的日志行数", "capability": "general"})
    check("面板派活成功（此时队员在线）", st == 200 and r.get("assignee") == "newbie-pc", f"{st} {r}")
    sid = r.get("subtask_id")

    rr = subprocess.run([sys.executable, str(CONNECTOR), "run", "--once"], env=conn_env,
                        capture_output=True, text=True, timeout=120)
    check("run 一轮即完成派活", rr.returncode == 0, f"rc={rr.returncode} {rr.stdout[-160:]}")

    st, tasks = me.get("/api/link/tasks")
    done = next((t for t in tasks if t["id"] == sid), {})
    check("用户在「最近的派活记录」看到完成", done.get("status") == "done", str(done)[:200])
    check("结果来自本机 AI", "FAKE-DSH-OK" in (done.get("result") or ""), str(done)[:200])

    # ═══ 文档 ↔ 产品一致性 ═══
    print("\n── 附加核对：向导里写的命令必须真的存在于前端产物里 ──")
    dist = REPO / "panel" / "frontend" / "dist"
    idx = dist / "index.html"
    bundle = ""
    if idx.is_file():
        m = re.search(r'assets/(index-[A-Za-z0-9_-]+\.js)', idx.read_text(encoding="utf-8"))
        if m and (dist / "assets" / m.group(1)).is_file():
            bundle = (dist / "assets" / m.group(1)).read_text(encoding="utf-8", errors="replace")
    check("拿到已构建的前端产物", bool(bundle), f"{idx}")
    for cmd in ("bellhub-connector pair", "bellhub-connector run",
                "bellhub-connector autostart", "bellhub-connector doctor"):
        check(f"向导提到 {cmd}", cmd in bundle)
    check("向导是四步（含常驻那步）", "四步接入" in bundle and "这一步别跳过" in bundle)
finally:
    for p in (panel, hub):
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()

print("\n" + "═" * 62)
print(f"用户可见步骤：{len(STEPS)} 步")
for i, s in enumerate(STEPS, 1):
    print(f"  {i}. {s}")
print("═" * 62)
print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
