"""智能体链接 S1 接口端到端测试（真实 HTTP + 隔离数据库）。

铁律 #2：显式锁定 DSH_SUITE_DATA 并断言，绝不让测试碰到真实 panel.db。
铁律 #4：以子进程起 uvicorn，用 urllib 走真实 HTTP，验证状态码与越权。

用法：~/dsh-suite/.venv/bin/python tests/test_link_api.py
"""
import hashlib
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── 仓库内相对定位（2026-10-14 从 /root/1012 迁入 tests/）──
REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"
WORKERS = REPO / "workers"
MCPHUB = REPO / "mcp-hub"

ROOT = Path(tempfile.mkdtemp(prefix="linkapi-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT)
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"DB_PATH 未锁定: {D.DB_PATH}"
print(f"[env] DSH_SUITE_DATA={ROOT}")

# 预置两个账号（server 启动前写库）
_D = D.get_db()
_D.create_account("alice", "pw-alice-1", "user", "Alice")
_D.create_account("bob", "pw-bob-1", "user", "Bob")
del _D

passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if (extra and not cond) else ""))


class Client:
    """带 cookie jar 的极简 HTTP 客户端；返回 (status, json)。"""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with self.op.open(r, timeout=15) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw[:200]}

    def get(self, p):
        return self.req("GET", p)

    def get_raw(self, p):
        """下载类端点：拿原始字节与状态码。"""
        r = urllib.request.Request(BASE + p, method="GET")
        try:
            with self.op.open(r, timeout=20) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, p, b=None):
        return self.req("POST", p, b if b is not None else {})

    def delete(self, p):
        return self.req("DELETE", p)


BIN_DIR = ROOT / "bins"
BIN_DIR.mkdir()
(BIN_DIR / "bellhub-connector").write_bytes(b"FAKE-LINUX-BINARY")
(BIN_DIR / "bellhub-connector.exe").write_bytes(b"FAKE-WIN-BINARY")
# 版本旁文件：面板读不到二进制内部的 VERSION，靠它告诉连接器"产物是哪一版"
(BIN_DIR / "bellhub-connector.version").write_text("2099-01-01.1\n", encoding="utf-8")

env = {**os.environ, "DSH_SUITE_DATA": str(ROOT),
       "BELLHUB_CONNECTOR_BIN_DIR": str(BIN_DIR),
       "BELLHUB_WORKERS_DIR": str(WORKERS)}
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
     "--port", str(PORT), "--log-level", "warning"],
    cwd=str(BACKEND), env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

try:
    for _ in range(60):        # 等就绪
        try:
            urllib.request.urlopen(BASE + "/api/auth/me", timeout=1)
            break
        except urllib.error.HTTPError:
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit("server 未在 30 秒内就绪\n" + (proc.stdout.read() if proc.stdout else ""))

    anon = Client()
    alice = Client()
    bob = Client()

    print("\n[1] 连接器发起配对（公开端点）")
    st, pair = anon.post("/api/link/device-code", {
        "name": "alice-pc", "host": "alice-pc.local", "platform": "windows",
        "capabilities": [{"type": "code", "label": "Claude Code"}]})
    check("POST /api/link/device-code = 200", st == 200, f"{st} {pair}")
    dc, uc = pair.get("device_code"), pair.get("user_code")
    check("拿到 device_code / user_code", bool(dc) and len(uc) == 8)
    check("verification_uri 带 user_code", uc in pair.get("verification_uri", ""))
    check("非法 name 被清洗", anon.post("/api/link/device-code",
          {"name": "../../etc/passwd"})[1].get("user_code") is not None)

    print("\n[2] 未登录不能审批")
    st, _ = anon.get(f"/api/link/pair-code?code={uc}")
    check("匿名查配对详情 = 401", st == 401, st)
    st, _ = anon.post("/api/link/approve", {"user_code": uc})
    check("匿名审批 = 401", st == 401, st)

    print("\n[3] 登录后确认")
    st, me = alice.post("/api/auth/login", {"username": "alice", "password": "pw-alice-1"})
    check("alice 登录成功", st == 200 and me.get("username") == "alice", f"{st} {me}")
    st, info = alice.get(f"/api/link/pair-code?code={uc}")
    check("确认页拿到设备信息", st == 200 and info.get("host") == "alice-pc.local", f"{st} {info}")
    st, r = alice.post("/api/link/approve", {"user_code": uc.lower()})
    check("审批成功（小写码也可）", st == 200 and r.get("ok") is True, f"{st} {r}")
    st, _ = alice.post("/api/link/approve", {"user_code": uc})
    check("同一码不能重复审批 = 404", st == 404, st)

    print("\n[4] 连接器换取凭据")
    st, poll = anon.get(f"/api/link/device-code/{dc}")
    check("轮询返回 approved", st == 200 and poll.get("status") == "approved", f"{st} {poll}")
    token, agent_id = poll.get("token"), poll.get("agent_id")
    check("拿到长期 token（48 位）", isinstance(token, str) and len(token) == 48)
    check("返回绑定用户名 alice", poll.get("username") == "alice")
    st, poll2 = anon.get(f"/api/link/device-code/{dc}")
    check("token 不会二次发放", poll2.get("status") != "approved", poll2)
    st, _ = anon.get("/api/link/device-code/deadbeef")
    check("不存在的 device_code = 404", st == 404, st)

    print("\n[5] 名册与测试连接")
    st, agents = alice.get("/api/link/agents")
    check("alice 名册 1 人", st == 200 and len(agents) == 1, f"{st} {agents}")
    check("名册不含 token_hash", agents and "token_hash" not in agents[0])
    check("能力标签已解析", agents and agents[0]["capabilities"][0]["label"] == "Claude Code")
    check("在线判定为真", agents and agents[0]["online"] is True)
    st, r = alice.post(f"/api/link/agents/{agent_id}/ping")
    check("测试连接 = 200", st == 200 and r.get("ok") is True, f"{st} {r}")

    print("\n[5b] 连接器版本自检端点")
    aname = agents[0]["name"]
    st, r = anon.get("/api/link/connector-latest")
    check("无凭据 = 401", st == 401, st)
    st, r = anon.get("/api/link/connector-latest")
    req = urllib.request.Request(BASE + "/api/link/connector-latest",
                                 headers={"X-Agent-Auth": f"{aname}:wrong-token"})
    try:
        urllib.request.urlopen(req, timeout=10)
        st2 = 200
    except urllib.error.HTTPError as e:
        st2 = e.code
    check("错 token = 401", st2 == 401, st2)
    req = urllib.request.Request(BASE + "/api/link/connector-latest",
                                 headers={"X-Agent-Auth": f"{aname}:{token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        latest = json.loads(resp.read().decode())
    check("有效凭据可查最新版本", resp.status == 200 and bool(latest.get("version")),
          str(latest))
    check("返回可用的预编译产物", isinstance(latest.get("builds"), list), str(latest))

    print("\n[5c] 自升级取件端点（sha256 供逐字节核对）")
    real_src = Path(str(WORKERS / "bellhub_connector.py"))
    st, raw = alice.get_raw(f"/api/link/connector-artifact?kind=source"
                            f"&_={aname}")   # get_raw 不带 X-Agent-Auth，先确认会 401
    check("取件也必须带凭据（匿名 401）", st == 401, st)

    def agent_get(path: str) -> tuple[int, bytes, dict]:
        rq = urllib.request.Request(BASE + path,
                                    headers={"X-Agent-Auth": f"{aname}:{token}"})
        try:
            with urllib.request.urlopen(rq, timeout=20) as rp:
                return rp.status, rp.read(), {k.lower(): v for k, v in rp.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}

    st, raw, hdrs = agent_get("/api/link/connector-artifact?kind=source")
    check("源码版可取到", st == 200 and raw.startswith(b"#!/usr/bin/env python3"), f"{st} {raw[:30]}")
    check("响应头 sha256 与真实文件一致",
          hdrs.get("x-bellhub-sha256") == hashlib.sha256(real_src.read_bytes()).hexdigest(),
          hdrs.get("x-bellhub-sha256", "")[:16])
    check("响应头带大小", hdrs.get("x-bellhub-size") == str(real_src.stat().st_size),
          hdrs.get("x-bellhub-size", ""))
    st, raw, hdrs = agent_get("/api/link/connector-artifact?kind=bin&platform=linux-x64")
    check("可取的预编译产物二进制", st == 200 and raw == b"FAKE-LINUX-BINARY", f"{st} {raw[:30]}")
    check("产物 sha256 头正确",
          hdrs.get("x-bellhub-sha256") == hashlib.sha256(b"FAKE-LINUX-BINARY").hexdigest(),
          hdrs.get("x-bellhub-sha256", "")[:16])
    st, raw_latest, _ = agent_get("/api/link/connector-latest")
    latest2 = json.loads(raw_latest.decode() or "{}")
    lin = next((b for b in latest2.get("builds", []) if b["platform"] == "linux-x64"), {})
    check("产物版本来自版本旁文件", lin.get("version") == "2099-01-01.1", str(lin)[:160])
    win = next((b for b in latest2.get("builds", []) if b["platform"] == "win-x64"), {})
    check("没有旁文件时版本为 null（不假装知道）", win.get("version") is None, str(win)[:160])
    st, raw, _ = agent_get("/api/link/connector-artifact?kind=bin&platform=win-x64")
    check("Windows 产物同样可取", st == 200 and raw == b"FAKE-WIN-BINARY", f"{st} {raw[:30]}")
    st, _, _ = agent_get("/api/link/connector-artifact?kind=bin&platform=plan9")
    check("不支持的平台 = 404", st == 404, st)
    st, _, _ = agent_get("/api/link/connector-artifact?kind=bogus")
    check("非法 kind = 400", st == 400, st)

    print("\n[6] 跨用户隔离")
    bob.post("/api/auth/login", {"username": "bob", "password": "pw-bob-1"})
    st, agents_b = bob.get("/api/link/agents")
    check("bob 名册为空", st == 200 and agents_b == [], f"{st} {agents_b}")
    st, _ = bob.delete(f"/api/link/agents/{agent_id}")
    check("bob 撤销 alice 的队员 = 404", st == 404, st)
    st, _ = bob.post(f"/api/link/agents/{agent_id}/ping")
    check("bob 探测 alice 的队员 = 404", st == 404, st)

    print("\n[7] 派活记录（队伍视图数据）")
    aname = agents[0]["name"]
    db2 = D.get_db()
    sub = db2.create_subtask("ptask-1", 1, "本机跑一下", "列出当前目录", "general", assignee=aname)
    db2.claim_subtask(sub["id"], aname)          # complete_subtask 仅对 running 生效
    db2.complete_subtask(sub["id"], "FAKE-OUTPUT", 0)
    st, tasks = alice.get("/api/link/tasks?limit=10")
    check("alice 看到自己的派活记录", st == 200 and len(tasks) == 1, f"{st} {tasks}")
    check("记录含状态与结果", tasks and tasks[0]["status"] == "done"
          and "FAKE-OUTPUT" in tasks[0]["result"], str(tasks)[:200])
    st, tasks_b = bob.get("/api/link/tasks")
    check("bob 看不到 alice 的派活记录", st == 200 and tasks_b == [], f"{st} {tasks_b}")

    print("\n[7b] 面板直接派活（会话外的入口）")
    st, r = bob.post("/api/link/dispatch", {"prompt": "随便跑一下"})
    check("没有队员时给出可操作报错", st == 400 and "连接器" in str(r), f"{st} {r}")
    st, r = alice.post("/api/link/dispatch", {"prompt": "列出当前目录", "capability": "code"})
    check("按能力自动挑在线队员", st == 200 and r.get("assignee") == aname, f"{st} {r}")
    check("新任务进入 pending", r.get("subtask_id")
          and D.get_db().get_subtask(r["subtask_id"])["status"] == "pending", str(r))
    check("提示该队员在线", r.get("online") is True and r.get("note") == "", str(r))
    st, r2 = alice.post("/api/link/dispatch", {"prompt": "点名派活", "title": "点名",
                                               "agent": aname})
    check("可点名指定队员", st == 200 and r2.get("assignee") == aname, f"{st} {r2}")
    check("任务标题生效", D.get_db().get_task(r2["task_id"])["title"] == "点名", str(r2))
    st, r3 = alice.post("/api/link/dispatch", {"prompt": "x", "agent": "不存在"})
    check("点名不存在的队员 = 404", st == 404, st)
    st, r4 = alice.post("/api/link/dispatch", {"prompt": "x", "capability": "no-such-cap"})
    check("能力不匹配时明确报错", st == 400 and "能力标签" in str(r4), f"{st} {r4}")
    st, r5 = bob.post("/api/link/dispatch", {"prompt": "x", "agent": aname})
    check("不能点名别人的队员 = 404", st == 404, st)
    st, r6 = anon.post("/api/link/dispatch", {"prompt": "x"})
    check("未登录不能派活 = 401", st == 401, st)

    print("\n[8] 撤销")
    st, r = alice.delete(f"/api/link/agents/{agent_id}")
    check("alice 撤销成功", st == 200 and r.get("ok") is True, f"{st} {r}")
    check("撤销后名册清空", alice.get("/api/link/agents")[1] == [])

    print("\n[9] 限流（公开端点）")
    codes = [anon.post("/api/link/device-code", {"name": "spam"})[0] for _ in range(16)]
    check("超出限额返回 429", 429 in codes, str(codes))

    print("\n[10] 预编译产物分发")
    st, builds = anon.get("/api/link/connector-builds")
    check("未登录不能列产物 = 401", st == 401, st)
    st, builds = alice.get("/api/link/connector-builds")
    plats = {b["platform"] for b in builds} if isinstance(builds, list) else set()
    check("列出两个平台的产物", st == 200 and plats == {"win-x64", "linux-x64"}, f"{st} {builds}")
    lin = next((b for b in builds if b["platform"] == "linux-x64"), {}) if isinstance(builds, list) else {}
    check("大小与 sha256 正确", lin.get("size") == len(b"FAKE-LINUX-BINARY")
          and lin.get("sha256") == hashlib.sha256(b"FAKE-LINUX-BINARY").hexdigest(), str(lin))
    st, raw = alice.get_raw("/api/link/connector-builds/linux-x64")
    check("能下载到二进制", st == 200 and raw == b"FAKE-LINUX-BINARY", f"{st} {raw[:40]}")
    st, _ = anon.get_raw("/api/link/connector-builds/linux-x64")
    check("未登录不能下载 = 401", st == 401, st)
    st, _ = alice.get_raw("/api/link/connector-builds/plan9")
    check("不支持的平台 = 404", st == 404, st)

    print("\n[10b] 设备接入清单（安卓 handler 已挂上）")
    st, files = alice.get("/api/devices/agent-files")
    names = {f["name"] for f in files} if isinstance(files, list) else set()
    check("清单含 termux_handler.py", st == 200 and "termux_handler.py" in names, str(sorted(names)))
    check("清单含连接器与默认 handler",
          "bellhub_connector.py" in names and "device_handler.py" in names, str(sorted(names)))
    tm = next((f for f in files if f["name"] == "termux_handler.py"), {}) if isinstance(files, list) else {}
    check("安卓 handler 带版本与哈希", bool(tm.get("version")) and len(tm.get("sha256", "")) == 64,
          str(tm)[:160])
    da = next((f for f in files if f["name"] == "device_agent.py"), {}) if isinstance(files, list) else {}
    check("设备 agent 已是带 autostart 的新版",
          str(da.get("version", "")).startswith("2026-10-14"), str(da)[:160])

finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()

print(f"\n结果：{len(passed)} 项通过，{len(failed)} 项失败")
if failed:
    print("失败项：" + "；".join(failed))
    sys.exit(1)
print("全部通过 ✅")
