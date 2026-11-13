"""Hub MCP 每用户凭据与越权边界（真实 HTTP + JSON-RPC over streamable-http）。

验证：
  · 无凭据 + 公网标记 → 401
  · 全局密钥 → 管理员身份（全站）
  · 用户 agent 凭据 → link_ping 报出自己账号、device_list 只能看自己
  · 用户凭据不能越权操作别人的设备 / 不能调站娘专属工具

用法：~/dsh-suite/.venv/bin/python tests/test_hub_scoping.py
"""
import http.cookiejar  # noqa: F401  (保持与其它测试一致的导入风格)
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

ROOT = Path(tempfile.mkdtemp(prefix="hubscope-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT)
PORT = 8102
HUB = f"http://127.0.0.1:{PORT}"
GLOBAL_TOKEN = "global-admin-token-for-test"

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"DB_PATH 未锁定: {D.DB_PATH}"
print(f"[env] DSH_SUITE_DATA={ROOT}")

db = D.get_db()
alice = db.create_account("alice", "pw-alice-1", "user", "Alice")
bob = db.create_account("bob", "pw-bob-1", "user", "Bob")
db.create_device(alice["id"], "alice-pc", "Alice 的电脑")
db.create_device(bob["id"], "bob-pc", "Bob 的电脑")

pair = db.create_pair_code("alice-agent", "alice-pc", "linux", [{"type": "code", "label": "Claude Code"}])
db.approve_pair(pair["user_code"], alice["id"])
_, ALICE_TOKEN, _ = db.consume_pair(pair["device_code"])

pair_b = db.create_pair_code("bob-agent", "bob-pc", "linux", [])
db.approve_pair(pair_b["user_code"], bob["id"])
_, BOB_TOKEN, _ = db.consume_pair(pair_b["device_code"])

passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if (extra and not cond) else ""))


class MCP:
    """极简 streamable-http MCP 客户端。"""

    def __init__(self, token: str | None, proxy: bool = True):
        self.token = token
        self.proxy = proxy
        self.sid = None

    def _headers(self, extra=None):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.proxy:
            h["X-BellHub-Proxy"] = "1"
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        h.update(extra or {})
        return h

    def raw(self, payload, extra=None):
        req = urllib.request.Request(HUB + "/mcp/", data=json.dumps(payload).encode(),
                                     method="POST", headers=self._headers(extra))
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, body, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

    @staticmethod
    def _parse(body: str):
        out = []
        for line in body.splitlines():
            if line.startswith("data: "):
                try:
                    out.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
        if not out and body.strip().startswith("{"):
            try:
                out.append(json.loads(body))
            except json.JSONDecodeError:
                pass
        return out

    def connect(self):
        st, body, hdrs = self.raw({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2024-11-05",
                                              "capabilities": {},
                                              "clientInfo": {"name": "scope-test", "version": "1"}}})
        if st != 200:
            return st, body
        self.sid = hdrs.get("mcp-session-id") or hdrs.get("Mcp-Session-Id")
        self.raw({"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"Mcp-Session-Id": self.sid} if self.sid else None)
        return st, body

    def call(self, tool: str, args: dict):
        st, body, _ = self.raw({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                "params": {"name": tool, "arguments": args}},
                               {"Mcp-Session-Id": self.sid} if self.sid else None)
        if st != 200:
            return st, body
        msgs = self._parse(body)
        if not msgs:
            return st, body
        res = msgs[-1].get("result", {})
        content = res.get("content") or []
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return st, text


env = {**os.environ, "DSH_SUITE_DATA": str(ROOT), "MCP_TOKEN": GLOBAL_TOKEN,
       "MCP_TOKEN_FILE": str(ROOT / "mcp_token")}
(ROOT / "mcp_token").write_text(GLOBAL_TOKEN, encoding="utf-8")
proc = subprocess.Popen([sys.executable, str(MCPHUB / "hub.py"),
                         "127.0.0.1", str(PORT)],
                        cwd=str(MCPHUB), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(60):
    try:
        urllib.request.urlopen(HUB + "/agent/heartbeat", data=b"{}", timeout=1)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(0.5)

try:
    print("\n[1] 无凭据的公网请求必须 401")
    st, body, _ = MCP(None).raw({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2024-11-05",
                                            "capabilities": {},
                                            "clientInfo": {"name": "x", "version": "1"}}})
    check("匿名 + 公网标记 = 401", st == 401, f"{st} {body[:120]}")

    print("\n[2] 全局密钥 = 管理员身份（全站）")
    admin = MCP(GLOBAL_TOKEN)
    st, _ = admin.connect()
    check("管理员 initialize 200", st == 200, st)
    st, txt = admin.call("link_ping", {})
    check("link_ping 返回管理员身份", st == 200 and '"identity": "root"' in txt, txt[:200])
    st, txt = admin.call("device_list", {"user_name": "bob"})
    check("管理员可看 bob 的设备", st == 200 and "bob-pc" in txt, txt[:200])

    print("\n[3] 用户凭据 = 只能看自己")
    a = MCP(ALICE_TOKEN)
    st, _ = a.connect()
    check("alice initialize 200", st == 200, st)
    st, txt = a.call("link_ping", {})
    check("身份为 agent", st == 200 and '"identity": "agent"' in txt, txt[:200])
    check("账号名为 alice", '"username": "alice"' in txt, txt[:200])
    check("权限范围写明仅本账号", "仅账号 alice" in txt, txt[:200])
    check("能报出本机能力标签", "Claude Code" in txt, txt[:200])

    st, txt = a.call("device_list", {"user_name": "alice"})
    check("alice 能看自己的设备", st == 200 and "alice-pc" in txt, txt[:200])
    st, txt = a.call("device_list", {"user_name": "bob"})
    check("alice 看 bob 的设备被拒", "不能操作" in txt, txt[:200])
    st, txt = a.call("device_list", {"user_name": ""})
    check("留空 user_name 自动收敛到本人", st == 200 and "alice-pc" in txt and "bob-pc" not in txt,
          txt[:200])

    st, txt = a.call("device_send", {"user_name": "bob", "device_name": "bob-pc", "text": "whoami"})
    check("alice 不能给 bob 的设备下指令", "不能操作" in txt, txt[:200])

    print("\n[4] 站娘专属工具对用户凭据关闭")
    st, txt = a.call("task_poll", {})
    check("task_poll 被拒", "仅站娘" in txt, txt[:200])
    st, txt = a.call("subtask_create", {"parent_task_id": "p", "step": 1, "title": "t", "prompt": "x"})
    check("subtask_create 被拒", "仅站娘" in txt, txt[:200])
    st, txt = a.call("blackboard_write", {"key": "k", "value": "v"})
    check("blackboard_write 被拒", "仅站娘" in txt, txt[:200])
    st, txt = a.call("subtask_reassign", {"subtask_id": "x"})
    check("subtask_reassign 被拒", "仅站娘" in txt, txt[:200])
    st, txt = a.call("agent_list", {})
    check("agent_list 只列自己", "alice-agent" in txt and "bob-agent" not in txt, txt[:200])

    print("\n[5] 站娘专属工具对管理员开放（队伍编排）")
    st, txt = admin.call("task_create", {"user_name": "alice", "title": "端到端验收", "prompt": "跑一遍"})
    check("task_create 成功", st == 200 and "端到端验收" in txt, txt[:200])
    tid = json.loads(txt)["id"] if st == 200 else ""
    st, txt = admin.call("subtask_create", {"parent_task_id": tid, "step": 1,
                                            "title": "本机跑一下", "prompt": "列出当前目录",
                                            "capability": "general"})
    check("subtask_create 成功", st == 200 and "本机跑一下" in txt, txt[:200])
    sid = json.loads(txt)["id"] if st == 200 else ""
    st, txt = admin.call("subtask_status", {"parent_task_id": tid})
    check("subtask_status 能看到", st == 200 and "本机跑一下" in txt, txt[:200])

    print("\n[5b] 失败改派：退回队列并换能力标签")
    db.claim_subtask(sid, "alice-agent")
    db.fail_subtask(sid, "本机拒绝执行")
    check("子任务已 failed", db.get_subtask(sid)["status"] == "failed")
    st, txt = admin.call("subtask_reassign", {"subtask_id": sid, "capability": "code",
                                              "assignee": "alice-agent"})
    check("subtask_reassign 成功", st == 200 and '"status": "pending"' in txt, txt[:200])
    check("能力标签已更新", db.get_subtask(sid)["capability"] == "code")
    check("失败结果已清空", db.get_subtask(sid)["result"] == "")
    st, txt = admin.call("subtask_reassign", {"subtask_id": "no-such"})
    check("不存在的子任务报错", "不存在" in txt, txt[:200])

    print("\n[5c] 会话内派活入口：一站派活 → 收活 → 自动改派")
    st, txt = admin.call("dispatch_to_agent", {"user_name": "alice", "prompt": "统计日志行数",
                                               "title": "统计", "capability": "code"})
    check("一站派活成功并给出任务号", st == 200 and "task_id" in txt and "alice-agent" in txt,
          txt[:200])
    d = json.loads(txt) if st == 200 else {}
    dtask, dsub = d.get("task_id", ""), d.get("subtask_id", "")
    check("子任务已指派给在线队员", db.get_subtask(dsub)["assignee"] == "alice-agent", str(d))

    st, txt = admin.call("task_report", {"task_id": dtask})
    check("收活：未完成时 all_done=false", st == 200 and '"all_done": false' in txt, txt[:200])

    db.claim_subtask(dsub, "alice-agent")
    db.complete_subtask(dsub, "共 1234 行", 0)
    st, txt = admin.call("task_report", {"task_id": dtask})
    check("收活：完成后 all_done=true", '"all_done": true' in txt, txt[:200])
    check("汇总里带得出结果", "共 1234 行" in txt, txt[:200])
    st, txt = admin.call("task_report", {"task_id": "no-such"})
    check("收活：不存在的任务报错", "不存在" in txt, txt[:200])

    st, txt = admin.call("dispatch_to_agent", {"user_name": "alice", "prompt": "会失败的活",
                                               "title": "失败样本", "capability": "code"})
    d2 = json.loads(txt) if st == 200 else {}
    db.claim_subtask(d2["subtask_id"], "alice-agent")
    db.fail_subtask(d2["subtask_id"], "本机拒绝执行")
    st, txt = admin.call("dispatch_retry", {"task_id": d2["task_id"]})
    check("自动改派：把失败项退回队列", st == 200 and '"from": "alice-agent"' in txt, txt[:200])
    check("退回后状态为 pending", db.get_subtask(d2["subtask_id"])["status"] == "pending",
          db.get_subtask(d2["subtask_id"])["status"])
    st, txt = admin.call("dispatch_to_agent", {"user_name": "bob", "prompt": "x",
                                               "capability": "code"})
    check("bob 无在线队员 → 明确回报而不是空任务", "没有在线的队员" in txt, txt[:200])
    st, txt = admin.call("dispatch_to_agent", {"user_name": "nobody", "prompt": "x"})
    check("不存在的用户报错", "用户不存在" in txt, txt[:200])

    st, txt = a.call("dispatch_to_agent", {"user_name": "alice", "prompt": "x"})
    check("用户凭据不能调 dispatch_to_agent", "仅站娘" in txt, txt[:200])
    st, txt = a.call("task_report", {"task_id": dtask})
    check("用户凭据不能调 task_report", "仅站娘" in txt, txt[:200])
    st, txt = a.call("dispatch_retry", {"task_id": dtask})
    check("用户凭据不能调 dispatch_retry", "仅站娘" in txt, txt[:200])

    print("\n[6] 沙箱工具不能跨账号")
    b = MCP(BOB_TOKEN)
    b.connect()
    st, txt = b.call("sandbox_list", {"task_id": tid})
    check("bob 访问 alice 任务的沙箱被拒", "不能访问" in txt, txt[:200])

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
