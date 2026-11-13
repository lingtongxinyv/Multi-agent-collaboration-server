"""元验收：用无头浏览器真点一遍「我的智能体」页（Playwright）。

⚠ 与 meta_selftest_check.py 同理，**不列入** run_all.sh：
   它依赖 Playwright（装在 tests/visual/node_modules，root-only）且要真起浏览器。
   请在服务器上以 root 手动运行：

       ~/dsh-suite/.venv/bin/python ~/dsh-suite/tests/meta_ui_check.py

它做的事：
   1) 起一个隔离面板（临时 DSH_SUITE_DATA，端口 8092，直接服务已构建的 dist）
   2) 建一个全新账号，用无头 Chromium 真登录
   3) 打开 /agents，核对四步向导与下载/复制按钮
   4) 起真连接器 pair 拿到配对码 → 在页面上填码点「确认接入」→ 刷新看名册
   5) 用页面上的「派活」表单派一条活，看记录里出现
"""
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
NODE = str(Path.home() / ".local/nodejs/bin/node")
UI_SCRIPT = Path(__file__).resolve().parent / "meta_ui_agents.mjs"

ROOT = Path(tempfile.mkdtemp(prefix="ui-check-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT / "data")
PORT = 8092
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"隔离失败: {D.DB_PATH}"
D.get_db().create_account("ui-user", "pw-ui-1", "user", "界面验收用户")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


FAKE_BIN = ROOT / "bin"
FAKE_BIN.mkdir()
fake = FAKE_BIN / "dsh"
fake.write_text('#!/bin/sh\necho "FAKE-DSH-OK"\n', encoding="utf-8")
fake.chmod(0o755)

env = {**os.environ, "DSH_SUITE_DATA": str(ROOT / "data"),
       "MCP_TOKEN_FILE": str(ROOT / "mcp_token"),
       "BELLHUB_WORKERS_DIR": str(WORKERS)}
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

# 连接器配对（真跑，拿到真配对码；页面里要填它）
conn_home = ROOT / "conn"
proc = subprocess.Popen([sys.executable, str(CONNECTOR), "pair", "--base", BASE,
                         "--no-browser", "--name", "ui-pc"],
                        env={**os.environ, "BELLHUB_HOME": str(conn_home), "PYTHONUNBUFFERED": "1",
                             "PATH": f"{FAKE_BIN}:{os.environ['PATH']}"},
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
print(f"[env] 隔离面板 {BASE}｜配对码 {code}\n")

try:
    if not code:
        check("连接器给出配对码", False, "没读到配对码")
    else:
        r = subprocess.run([NODE, str(UI_SCRIPT), BASE, "ui-user", "pw-ui-1", "ui-pc", code,
                            "界面验收：统计日志行数"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=300,
                           env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": "/root/.cache/ms-playwright"})
        out = r.stdout.strip()
        payload = None
        for line in reversed(out.splitlines()):
            if line.strip().startswith('{"results"'):
                payload = json.loads(line)
                break
        if payload is None:
            print(out[-2000:])
            print(r.stderr[-2000:])
            check("浏览器脚本产出结果", False, f"rc={r.returncode}")
        else:
            for item in payload["results"]:
                check(item["name"], item["ok"], item["detail"])
        # 连接器应已拿到凭据（页面确认那一下真的生效）
        cfg = conn_home / "connector.json"
        check("页面确认使连接器拿到凭据",
              cfg.is_file() and len(json.loads(cfg.read_text(encoding='utf-8')).get('token', '')) == 48,
              str(cfg))
finally:
    proc.terminate()
    srv.terminate()
    for p in (proc, srv):
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()

print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
