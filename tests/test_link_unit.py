"""智能体链接 S1 单元测试：设备码配对 + 每用户队员凭据（隔离环境）。

铁律 #2：必须显式锁定 DSH_SUITE_DATA 并断言 DB_PATH，否则以 root 运行会连到
影子库 /root/dsh-suite/data/panel.db。

用法：~/.local/bin/uv run --project ~/dsh-suite python tests/test_link_unit.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

# ── 仓库内相对定位（2026-10-14 从 /root/1012 迁入 tests/）──
REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"
WORKERS = REPO / "workers"
MCPHUB = REPO / "mcp-hub"

ROOT = Path(tempfile.mkdtemp(prefix="linkunit-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT)

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"DB_PATH 未锁定到临时目录: {D.DB_PATH}"
print(f"[env] DSH_SUITE_DATA={ROOT}  DB_PATH={D.DB_PATH}")

db = D.get_db()
alice = db.create_account("alice", "pw-alice-1", "user", "Alice")
bob = db.create_account("bob", "pw-bob-1", "user", "Bob")

passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra and not cond else ""))


print("\n[1] 配对码生命周期")
pair = db.create_pair_code("alice-pc", "alice-pc.local", "windows",
                           [{"type": "code", "label": "Claude Code"}])
check("create_pair_code 返回 device_code", len(pair["device_code"]) == 48, pair)
check("user_code 为 8 位且无易混字符", len(pair["user_code"]) == 8
      and not (set(pair["user_code"]) & set("0O1I")), pair["user_code"])
check("未审批时 consume 失败", db.consume_pair(pair["device_code"]) is None)

print("\n[2] 用户审批 → 连接器换凭据")
p = db.get_pair_by_user_code(pair["user_code"].lower())   # 小写也应能匹配
check("user_code 大小写不敏感可查到", p is not None and p["device_code"] == pair["device_code"])
check("他人 code 查不到", db.get_pair_by_user_code("ZZZZZZZZ") is None)

ap = db.approve_pair(pair["user_code"], alice["id"])
check("approve_pair 绑定到 alice", ap is not None and ap["status"] == "approved"
      and ap["user_id"] == alice["id"])

got = db.consume_pair(pair["device_code"])
check("consume_pair 成功返回 (agent, token, username)", got is not None)
agent, token, uname = got
check("绑定用户名正确", uname == "alice", uname)
check("agent 归属 alice", agent["user_id"] == alice["id"])
check("token 长度 48", len(token) == 48, len(token))
check("token_hash 不落明文", agent["token_hash"] != token and len(agent["token_hash"]) == 64)
check("配对记录标记 consumed",
      db.get_pair_by_device_code(pair["device_code"])["status"] == "consumed")

print("\n[3] 防重放 / 防越权")
check("同一 device_code 不能二次 consume", db.consume_pair(pair["device_code"]) is None)
check("verify_agent_token 能反查", (db.verify_agent_token(token) or {}).get("id") == agent["id"])
check("错误 token 反查失败", db.verify_agent_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None)
check("空 token 反查失败", db.verify_agent_token("") is None)

owned = db.list_user_agents(alice["id"])
check("alice 名册含该队员", [a["id"] for a in owned] == [agent["id"]])
check("bob 名册为空（隔离）", db.list_user_agents(bob["id"]) == [])
check("bob 不能按 id 取 alice 的队员", db.get_user_agent(bob["id"], agent["id"]) is None)
check("get_user_agent 不返回 token_hash",
      "token_hash" not in db.get_user_agent(alice["id"], agent["id"]))

print("\n[4] 重配对 = 轮换 token，不产生僵尸队员")
pair2 = db.create_pair_code("alice-pc", "alice-pc.local", "windows", [])
db.approve_pair(pair2["user_code"], alice["id"])
agent2, token2, _ = db.consume_pair(pair2["device_code"])
check("第二次配对沿用同一 agent id", agent2["id"] == agent["id"], f"{agent2['id']} vs {agent['id']}")
check("token 已轮换", token2 != token)
check("旧 token 失效", db.verify_agent_token(token) is None)
check("新 token 有效", db.verify_agent_token(token2) is not None)
check("名册仍只有 1 个队员", len(db.list_user_agents(alice["id"])) == 1)

print("\n[5] 撞名处理：另一用户同名机器不互相顶掉")
pair3 = db.create_pair_code("alice-pc", "bob-pc.local", "linux", [])
db.approve_pair(pair3["user_code"], bob["id"])
bob_agent, bob_token, _ = db.consume_pair(pair3["device_code"])
check("bob 的 agent 名被加后缀", bob_agent["name"] != "alice-pc", bob_agent["name"])
check("bob 自己名册 1 人", len(db.list_user_agents(bob["id"])) == 1)
check("alice 名册不受影响仍 1 人", len(db.list_user_agents(alice["id"])) == 1)
check("两 token 都能各自反查", db.verify_agent_token(bob_token)["user_id"] == bob["id"]
      and db.verify_agent_token(token2)["user_id"] == alice["id"])

print("\n[6] 拒绝与过期")
pair4 = db.create_pair_code("x", "", "", [])
check("deny_pair 成功", db.deny_pair(pair4["user_code"], bob["id"]))
check("被拒后不能再审批", db.approve_pair(pair4["user_code"], bob["id"]) is None)

pair5 = db.create_pair_code("y", "", "", [])
with db.tx() as c:  # 人为把有效期改到过去
    c.execute("UPDATE pair_codes SET expires_at=? WHERE device_code=?", (time.time() - 1,
                                                                         pair5["device_code"]))
check("过期码不能审批", db.approve_pair(pair5["user_code"], bob["id"]) is None)
check("过期码不能 consume", db.consume_pair(pair5["device_code"]) is None)

print("\n[7] 撤销队员")
check("bob 不能撤销 alice 的队员", db.delete_user_agent(bob["id"], agent2["id"]) is False)
check("alice 撤销成功", db.delete_user_agent(alice["id"], agent2["id"]) is True)
check("撤销后 token 立即失效", db.verify_agent_token(token2) is None)
check("名册清空", db.list_user_agents(alice["id"]) == [])

print(f"\n结果：{len(passed)} 项通过，{len(failed)} 项失败")
if failed:
    print("失败项：" + "；".join(failed))
    sys.exit(1)
print("全部通过 ✅")
