"""元测试：验证面板「智能体链接 · 验收自检」按钮背后的端点真的能跑整套。

⚠ 不要把它加进 run_all.sh 的 SUITES —— 那会形成无限递归
（面板自检 → run_all.sh → 本文件 → 面板自检）。所以它叫 meta_ 前缀而不是 test_。

用法：~/dsh-suite/.venv/bin/python ~/dsh-suite/tests/meta_selftest_check.py
"""
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

ROOT = Path(tempfile.mkdtemp(prefix="selftest-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT)
REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"
PORT = 8097                      # 刻意避开各套件用的 8099-8103
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"DB_PATH 未锁定: {D.DB_PATH}"
passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if (extra and not cond) else ""))


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def post(self, path, body, timeout=900):
        r = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                   headers={"Content-Type": "application/json"})
        try:
            with self.op.open(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except json.JSONDecodeError:
                return e.code, {}


env = {**os.environ, "DSH_SUITE_DATA": str(ROOT)}
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
    st, _ = anon.post("/api/admin/selftest", {})
    check("未登录不能跑自检 = 401", st == 401, st)

    admin = Client()
    st, me = admin.post("/api/auth/login", {"username": "admin", "password": "admin123"})
    check("管理员登录（隔离库的种子账号）", st == 200, f"{st} {me}")

    t0 = time.time()
    st, res = admin.post("/api/admin/selftest", {})
    dt = time.time() - t0
    print(f"  · 自检耗时 {dt:.1f}s")
    check("自检接口 = 200", st == 200, f"{st} {str(res)[:200]}")
    check("整体通过（ok=true）", res.get("ok") is True, str(res)[:400])
    check("至少 7 套通过（含二进制）", (res.get("passed") or 0) >= 7, str(res.get("passed")))
    check("没有失败套件", (res.get("failed") or 0) == 0, str(res.get("failed")))
    check("摘要里有结果行", "结果：" in (res.get("summary") or ""), (res.get("summary") or "")[-200:])
    check("审计留痕", any("selftest" in (r.get("action") or "")
                        for r in D.get_db().query("SELECT action FROM audit_log ORDER BY id DESC LIMIT 20")),
          "no audit row")
finally:
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except subprocess.TimeoutExpired:
        srv.kill()

print(f"\n结果：{len(passed)} 项通过，{len(failed)} 项失败")
if failed:
    print("失败项：" + "；".join(failed))
    sys.exit(1)
print("全部通过 ✅")
