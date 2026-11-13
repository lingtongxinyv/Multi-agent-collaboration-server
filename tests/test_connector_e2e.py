"""连接器端到端：浏览器授权配对 → 领活 → 交给本机 AI → 回报（隔离环境）。

覆盖：
  · pair 子进程真的能拿到 device-code、被审批后落盘凭据
  · run --once 真的把子任务跑完并把结果写回库
  · 高危任务在无人值守时被拒绝（分级闸门生效）
  · auto_approve_high_risk=true 时才放行高危

用法：~/dsh-suite/.venv/bin/python tests/test_connector_e2e.py
"""
import http.cookiejar
import hashlib
import json
import os
import re
import shutil
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

ROOT = Path(tempfile.mkdtemp(prefix="linkconn-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT)
PORT = 8100
BASE = f"http://127.0.0.1:{PORT}"
CONNECTOR = str(WORKERS / "bellhub_connector.py")
# CONNECTOR_BIN 指向打包好的单文件二进制时，用它替代 [python, 源码] 调用方式
CONNECTOR_BIN = os.environ.get("CONNECTOR_BIN")
CONN_CMD = [CONNECTOR_BIN] if CONNECTOR_BIN else None


def conn(*parts: str) -> list[str]:
    """连接器调用命令：二进制或 [解释器, 源码]。"""
    return [*(CONN_CMD or [PY, CONNECTOR]), *parts]
PY = sys.executable

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"DB_PATH 未锁定: {D.DB_PATH}"
print(f"[env] DSH_SUITE_DATA={ROOT}")

_d = D.get_db()
ALICE = _d.create_account("alice", "pw-alice-1", "user", "Alice")

# 假的 dsh 队员：位于 PATH 最前，验证"派活→本机 AI→回传"链路而不是真模型
FAKE_BIN = ROOT / "bin"
FAKE_BIN.mkdir()
fake = FAKE_BIN / "dsh"
fake.write_text("#!/bin/sh\necho \"FAKE-DSH-OK args=$*\"\n", encoding="utf-8")
fake.chmod(0o755)
# 自定义适配器（来自 connector.json，验证"不必等官方适配"）
myai = FAKE_BIN / "myai"
myai.write_text("#!/bin/sh\necho \"FAKE-MYAI-OK args=$*\"\n", encoding="utf-8")
myai.chmod(0o755)

CONN_HOME = ROOT / "conn"
CONN_HOME.mkdir(parents=True, exist_ok=True)
# 配对前就写好自定义适配器：pair 阶段应把它一起探测并启用
(CONN_HOME / "connector.json").write_text(json.dumps({
    "base_url": BASE, "name": "e2e-pc",
    "custom_agents": [{"key": "myai", "label": "我的 AI", "exe": "myai",
                       "cmd": "myai chat {prompt}", "take": ["mycap", "general"]}],
}, ensure_ascii=False, indent=2), encoding="utf-8")
passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if (extra and not cond) else ""))


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def post(self, path, body):
        r = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                   headers={"Content-Type": "application/json"})
        try:
            with self.op.open(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {}


env = {**os.environ, "DSH_SUITE_DATA": str(ROOT),
       "MCP_TOKEN_FILE": str(ROOT / "mcp_token"),
       "BELLHUB_WORKERS_DIR": str(WORKERS)}
srv = subprocess.Popen([PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                        "--port", str(PORT), "--log-level", "warning"],
                       cwd=str(BACKEND), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
# Hub 是独立进程（生产由 nginx 挂在 /mcp-hub/）；测试里直连，用 BELLHUB_HUB_URL 指定
HUB = subprocess.Popen([PY, str(MCPHUB / "hub.py"), "127.0.0.1", "8101"],
                       cwd=str(MCPHUB), env=env,
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
        urllib.request.urlopen("http://127.0.0.1:8101/agent/heartbeat",
                               data=b"{}", timeout=1)
        break
    except urllib.error.HTTPError:
        break          # 401 = hub 已就绪
    except Exception:
        time.sleep(0.5)

conn_env = {**os.environ, "DSH_SUITE_DATA": str(ROOT), "BELLHUB_HOME": str(CONN_HOME),
            "PYTHONUNBUFFERED": "1", "BELLHUB_HUB_URL": "http://127.0.0.1:8101",
            "PATH": f"{FAKE_BIN}:{os.environ['PATH']}"}

try:
    print("\n[1] 连接器 pair：浏览器授权配对")
    proc = subprocess.Popen(conn("pair", "--base", BASE, "--no-browser", "--name", "e2e-pc"),
                            env=conn_env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    code = None
    buf = []
    deadline = time.time() + 40
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        buf.append(line.rstrip())
        m = re.search(r"配对码：\s*([A-Z0-9]{8})", line)
        if m and code is None:
            code = m.group(1)
            print(f"  · 连接器出示配对码 {code}")
            cli = Client()
            st, _ = cli.post("/api/auth/login", {"username": "alice", "password": "pw-alice-1"})
            check("alice 登录", st == 200, st)
            st, r = cli.post("/api/link/approve", {"user_code": code})
            check("面板确认配对", st == 200 and r.get("ok"), f"{st} {r}")
    proc.wait(timeout=30)
    check("pair 退出码 0", proc.returncode == 0, f"{proc.returncode}\n" + "\n".join(buf[-8:]))
    cfg_path = CONN_HOME / "connector.json"
    check("凭据已落盘", cfg_path.is_file())
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    check("落盘含 token 与账号", len(cfg.get("token", "")) == 48 and cfg.get("username") == "alice",
          str({k: (v[:8] + "…" if k == "token" else v) for k, v in cfg.items()}))
    check("凭据文件权限 0600", oct(cfg_path.stat().st_mode)[-3:] == "600",
          oct(cfg_path.stat().st_mode)[-3:])
    agent_name = cfg.get("name", "")
    check("enabled 自动登记了 dsh 队员", "dsh" in (cfg.get("enabled") or []), str(cfg.get("enabled")))
    check("自定义适配器也被登记", "myai" in (cfg.get("enabled") or []), str(cfg.get("enabled")))

    print("\n[2] run --once：领活 → 本机 AI → 回报")
    sub1 = D.get_db().create_subtask("parent-e2e", 1, "翻译一句话",
                                     "把 hello 翻译成中文", "general", assignee=agent_name)
    r1 = subprocess.run(conn("run", "--once"), env=conn_env,
                        capture_output=True, text=True, timeout=120)
    print("  · 连接器输出：" + " / ".join(r1.stdout.strip().splitlines()[-4:]))
    got = D.get_db().get_subtask(sub1["id"])
    check("子任务状态 done", got["status"] == "done", got["status"])
    check("结果来自本机 AI", "FAKE-DSH-OK" in (got["result"] or ""), (got["result"] or "")[:120])

    print("\n[2b] 自定义 CLI 队员按能力标签接活")
    sub_m = D.get_db().create_subtask("parent-e2e", 9, "自定义队员的活",
                                      "帮我算一下 1+1", "mycap", assignee=agent_name)
    subprocess.run(conn("run", "--once"), env=conn_env,
                   capture_output=True, text=True, timeout=120)
    got_m = D.get_db().get_subtask(sub_m["id"])
    check("自定义适配器被选中执行", got_m["status"] == "done"
          and "FAKE-MYAI-OK" in (got_m["result"] or ""),
          f"{got_m['status']} {(got_m['result'] or '')[:80]}")

    print("\n[3] 高危闸门：无人值守必须拒绝")
    sub2 = D.get_db().create_subtask("parent-e2e", 2, "危险操作",
                                     "删除这台电脑 D 盘所有文件", "general", assignee=agent_name)
    r2 = subprocess.run(conn("run", "--once"), env=conn_env,
                        capture_output=True, text=True, timeout=120)
    got2 = D.get_db().get_subtask(sub2["id"])
    check("高危任务被拒（failed）", got2["status"] == "failed", got2["status"])
    check("拒绝原因说得清", "无人值守" in (got2["result"] or ""), (got2["result"] or "")[:120])
    check("未落到本机 AI 执行", "FAKE-DSH-OK" not in (got2["result"] or ""))

    print("\n[4] auto_approve_high_risk=true 时才放行")
    cfg["auto_approve_high_risk"] = True
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    sub3 = D.get_db().create_subtask("parent-e2e", 3, "危险操作2",
                                     "删除临时目录里的旧日志", "general", assignee=agent_name)
    subprocess.run(conn("run", "--once"), env=conn_env,
                   capture_output=True, text=True, timeout=120)
    got3 = D.get_db().get_subtask(sub3["id"])
    check("显式放行后执行成功", got3["status"] == "done" and "FAKE-DSH-OK" in (got3["result"] or ""),
          f"{got3['status']} {(got3['result'] or '')[:80]}")

    print("\n[4b] 一键写入 IDE 配置 + 版本自检")
    ide = ROOT / "ide"
    ide.mkdir()
    target = ide / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {"other": {"url": "http://x"}}, "keep": 1}),
                      encoding="utf-8")
    r4b = subprocess.run(conn("print-config", "trae", "--write", "--path", str(target)),
                         env=conn_env, capture_output=True, text=True, timeout=60)
    check("写入成功", r4b.returncode == 0, (r4b.stdout + r4b.stderr)[-200:])
    data = json.loads(target.read_text(encoding="utf-8"))
    check("并入 bellhub 且不破坏其它条目",
          data["mcpServers"]["other"]["url"] == "http://x" and data.get("keep") == 1
          and "bellhub" in data["mcpServers"], str(data)[:200])
    check("凭据已带进配置",
          "Bearer " in data["mcpServers"]["bellhub"]["headers"]["Authorization"], str(data)[:160])
    check("改动前先备份", len(list(ide.glob("mcp.json.bak-*"))) == 1,
          str(list(ide.glob('*'))))
    r4b2 = subprocess.run(conn("print-config", "trae", "--write", "--path", str(target)),
                          env=conn_env, capture_output=True, text=True, timeout=60)
    check("重复写入幂等（不新增备份）",
          "已是" in r4b2.stdout and len(list(ide.glob("mcp.json.bak-*"))) == 1,
          r4b2.stdout[-200:])
    bad = ide / "bad.json"
    bad.write_text("{bad json", encoding="utf-8")
    rbad = subprocess.run(conn("print-config", "trae", "--write", "--path", str(bad)),
                          env=conn_env, capture_output=True, text=True, timeout=60)
    check("坏 JSON 拒绝且原文件不动",
          rbad.returncode == 1 and bad.read_text(encoding="utf-8") == "{bad json",
          f"rc={rbad.returncode} {rbad.stdout[-120:]}")

    rv = subprocess.run(conn("version", "--check"), env=conn_env,
                        capture_output=True, text=True, timeout=60)
    check("版本自检连到站点", "站点最新版本" in rv.stdout, rv.stdout[-200:])
    check("与站点同版 → 已是最新", rv.returncode == 0 and "已是最新" in rv.stdout,
          f"rc={rv.returncode} {rv.stdout[-160:]}")

    print("\n[4b2] doctor：一条命令自检（配对/站点/领活通道/本机队员）")
    rd = subprocess.run(conn("doctor"), env=conn_env, capture_output=True, text=True, timeout=90)
    check("doctor 全绿退出 0", rd.returncode == 0, f"rc={rd.returncode}\n{rd.stdout[-400:]}")
    check("报出已配对账号", "alice" in rd.stdout, rd.stdout[-300:])
    check("报出版本最新", "已是最新" in rd.stdout, rd.stdout[-300:])
    check("报出领活通道正常", "领活通道正常" in rd.stdout, rd.stdout[-300:])
    check("报出本机队员", "DeepSeek Harness" in rd.stdout or "dsh" in rd.stdout, rd.stdout[-300:])
    check("结论明确", "一切正常" in rd.stdout, rd.stdout[-200:])

    print("\n[4c] 自升级：校验 sha256 → 备份 → 替换 → 回读自检")
    sut_dir = ROOT / "sut"
    sut_dir.mkdir()
    sut = sut_dir / "bellhub_connector.py"
    shutil.copy2(CONNECTOR, sut)          # 复制一份再升级，绝不改动仓库里的真文件
    src_sha = hashlib.sha256(Path(CONNECTOR).read_bytes()).hexdigest()

    rno = subprocess.run([PY, str(sut), "update", "--force"], env=conn_env,
                         capture_output=True, text=True, timeout=120)
    check("非交互未加 --yes 拒绝替换（安全默认）",
          rno.returncode == 6 and "安全默认" in rno.stdout,
          f"rc={rno.returncode} {rno.stdout[-200:]}")
    check("拒绝后原文件未被改动",
          hashlib.sha256(sut.read_bytes()).hexdigest() == src_sha)

    rup = subprocess.run([PY, str(sut), "update", "--yes", "--force"], env=conn_env,
                         capture_output=True, text=True, timeout=120)
    check("update --yes 成功", rup.returncode == 0, (rup.stdout + rup.stderr)[-300:])
    check("输出含哈希校验与备份", "哈希校验通过" in rup.stdout and "备份" in rup.stdout,
          rup.stdout[-300:])
    baks = list(sut_dir.glob("bellhub_connector.py.bak-*"))
    check("旧版已备份（回滚点）", len(baks) == 1, str(sorted(p.name for p in sut_dir.glob('*'))))
    check("替换后内容与服务端一致",
          hashlib.sha256(sut.read_bytes()).hexdigest() == src_sha)

    print("\n[5] 凭据被撤销后连接器必须自知")
    with D.get_db().tx() as c:
        c.execute("UPDATE agents SET token_hash='x' WHERE user_id=?", (ALICE["id"],))
    r5 = subprocess.run(conn("run", "--once"), env=conn_env,
                        capture_output=True, text=True, timeout=60)
    check("凭据失效时以非零码退出", r5.returncode == 5, f"rc={r5.returncode}")
    check("提示重新配对", "重新运行 pair" in r5.stdout, r5.stdout[-200:])

finally:
    for p in (srv, HUB):
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()

print(f"\n结果：{len(passed)} 项通过，{len(failed)} 项失败")
if failed:
    print("失败项：" + "；".join(failed))
    sys.exit(1)
print("全部通过 ✅")
