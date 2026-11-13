"""配对竞态验收：两个人/两台机器同时操作时，不能出现"两边都成功"或"凭据串号"。

为什么单独一套：`test_link_unit.py` 覆盖的是**线性**流程（建码→审批→换凭据→重放被拒），
但真实使用里"同时"才是最容易出问题的形态。本套用真 HTTP + 多线程压这几个点：

  A. 两个用户同时确认同一个配对码 → 只能有一个成功，另一个必须 404（不能两边都拿到设备）
  B. 同一个 device_code 被两个轮询者同时取件 → 只能发一次凭据
  C. 同一用户同时配两台机器 → 两个独立队员、各自凭据，互不覆盖
  D. 两个用户同时各配一台 → 各自绑定各自账号，不串号
  E. 同一用户同一台机器重复配对（顺序）→ 轮换凭据、不产生僵尸队员

用法：~/dsh-suite/.venv/bin/python tests/test_pairing_races.py
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"

ROOT = Path(tempfile.mkdtemp(prefix="pairrace-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT / "data")
PORT = 8089
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"隔离失败: {D.DB_PATH}"
db = D.get_db()
ALICE = db.create_account("alice", "pw-alice-1", "user", "Alice")
BOB = db.create_account("bob", "pw-bob-1", "user", "Bob")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


class Client:
    def __init__(self, user: str = "", pw: str = ""):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        if user:
            self.post("/api/auth/login", {"username": user, "password": pw})

    def _do(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.op.open(r, timeout=30) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return e.code, {}

    def get(self, p):
        return self._do("GET", p)

    def post(self, p, b=None):
        return self._do("POST", p, b if b is not None else {})


def start_pair(client: Client, name: str) -> dict:
    st, r = client.post("/api/link/device-code",
                        {"name": name, "host": f"{name}.local", "platform": "linux",
                         "capabilities": [{"type": "code", "label": "Claude Code"}]})
    assert st == 200, f"建码失败: {st} {r}"
    return r


def run_concurrently(fns):
    """并发跑一批函数，返回按原顺序排列的结果。"""
    out = [None] * len(fns)

    def wrap(i, fn):
        try:
            out[i] = fn()
        except Exception as exc:                       # noqa: BLE001
            out[i] = ("EXC", repr(exc))

    threads = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return out


env = {**os.environ, "DSH_SUITE_DATA": str(ROOT / "data"),
       "BELLHUB_WORKERS_DIR": str(REPO / "workers")}
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                        "--port", str(PORT), "--log-level", "warning"],
                       cwd=str(BACKEND), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/api/auth/me", timeout=1)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(0.5)

try:
    anon = Client()
    a = Client("alice", "pw-alice-1")
    b = Client("bob", "pw-bob-1")

    print("[A] 两个用户同时确认同一个配对码")
    pair = start_pair(anon, "contested-pc")
    code, dc = pair["user_code"], pair["device_code"]
    res = run_concurrently([
        lambda: a.post("/api/link/approve", {"user_code": code}),
        lambda: b.post("/api/link/approve", {"user_code": code}),
    ])
    codes = sorted(r[0] for r in res)
    check("恰好一个成功、一个 404", codes == [200, 404], str(res))
    st, poll = anon.get(f"/api/link/device-code/{dc}")
    check("凭据只发给一次", st == 200 and poll.get("status") == "approved"
          and len(poll.get("token", "")) == 48, f"{st} {poll}")
    owner = db.one("SELECT user_id FROM agents WHERE name=? OR name LIKE ?",
                   ("contested-pc", "contested-pc-%"))
    winner = "alice" if owner and owner["user_id"] == ALICE["id"] else (
        "bob" if owner and owner["user_id"] == BOB["id"] else "?")
    st_a, ag_a = a.get("/api/link/agents")
    st_b, ag_b = b.get("/api/link/agents")
    check("设备只归赢家一人（另一方名册里没有）",
          (len(ag_a) == 1) != (len(ag_b) == 1), f"winner={winner} alice={ag_a} bob={ag_b}")

    print("\n[B] 同一个 device_code 被两个轮询者同时取件")
    pair2 = start_pair(anon, "race-poll-pc")
    a.post("/api/link/approve", {"user_code": pair2["user_code"]})
    res = run_concurrently([
        lambda: anon.get(f"/api/link/device-code/{pair2['device_code']}"),
        lambda: anon.get(f"/api/link/device-code/{pair2['device_code']}"),
    ])
    tokens = [r[1].get("token") for r in res if r[0] == 200]
    got = [t for t in tokens if t]
    check("最多只发出一个凭据", len(got) <= 1, str(res))
    check("另一个拿到的是已发放状态而非令牌",
          any(r[1].get("status") in ("consumed", "approved") and not r[1].get("token")
              for r in res), str(res))

    print("\n[C] 同一用户同时配两台机器")
    p1 = start_pair(anon, "alice-pc-1")
    p2 = start_pair(anon, "alice-pc-2")
    res = run_concurrently([
        lambda: (a.post("/api/link/approve", {"user_code": p1["user_code"]}),
                 anon.get(f"/api/link/device-code/{p1['device_code']}")),
        lambda: (a.post("/api/link/approve", {"user_code": p2["user_code"]}),
                 anon.get(f"/api/link/device-code/{p2['device_code']}")),
    ])
    tok1 = res[0][1][1].get("token")
    tok2 = res[1][1][1].get("token")
    check("两台机器各拿到凭据", bool(tok1) and bool(tok2), str(res)[:200])
    check("两个凭据不相同", tok1 != tok2)
    names = {x["name"] for x in a.get("/api/link/agents")[1]}
    check("名册里出现两个队员", {"alice-pc-1", "alice-pc-2"} <= names, str(sorted(names)))
    u1 = db.verify_agent_token(tok1)
    u2 = db.verify_agent_token(tok2)
    check("两个凭据各自可用且互不覆盖",
          u1 is not None and u2 is not None and u1["name"] != u2["name"],
          f"{u1 and u1['name']} / {u2 and u2['name']}")

    print("\n[D] 两个用户同时各配一台")
    pa = start_pair(anon, "shared-name-pc")
    pb = start_pair(anon, "shared-name-pc")
    res = run_concurrently([
        lambda: (a.post("/api/link/approve", {"user_code": pa["user_code"]}),
                 anon.get(f"/api/link/device-code/{pa['device_code']}")),
        lambda: (b.post("/api/link/approve", {"user_code": pb["user_code"]}),
                 anon.get(f"/api/link/device-code/{pb['device_code']}")),
    ])
    ta = db.verify_agent_token(res[0][1][1].get("token") or "")
    tb = db.verify_agent_token(res[1][1][1].get("token") or "")
    check("两台同名机器各自绑定各自账号",
          ta and tb and {ta["user_id"], tb["user_id"]} == {ALICE["id"], BOB["id"]},
          f"{ta and ta['user_id']} / {tb and tb['user_id']}")
    check("同名不会互相顶掉（名字被去重）", ta and tb and ta["name"] != tb["name"],
          f"{ta and ta['name']} / {tb and tb['name']}")

    print("\n[E] 同一用户同一台机器重复配对（顺序）→ 轮换而非新增")
    before = len([x for x in a.get("/api/link/agents")[1] if x["name"] == "alice-pc-1"])
    p3 = start_pair(anon, "alice-pc-1")
    a.post("/api/link/approve", {"user_code": p3["user_code"]})
    st, poll3 = anon.get(f"/api/link/device-code/{p3['device_code']}")
    tok3 = poll3.get("token")
    after = len([x for x in a.get("/api/link/agents")[1] if x["name"] == "alice-pc-1"])
    check("仍然是 1 个 alice-pc-1（没变成 2 个）", before == 1 and after == 1,
          f"before={before} after={after}")
    check("旧凭据已被新凭据顶掉", db.verify_agent_token(tok1) is None
          and db.verify_agent_token(tok3) is not None, "旧凭据仍可用")
finally:
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except subprocess.TimeoutExpired:
        srv.kill()

print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
