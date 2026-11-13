"""长期运行的家务验收：过期配对码清理 + 僵尸子任务收尸。

为什么值得单独一套：这两件事都**不会报错**，只会在跑了几周之后表现为
"表越来越大"和"任务永远显示进行中"。必须自动查。

  A. purge_pair_codes：过期很久的清掉、刚过期的留着（宽限期）、还在有效期的不动
  B. reap_stale_subtasks：超过阈值仍 running 的标失败并给原因；刚领的不能误杀
  C. 回收后 assignee 保留（看得出是谁没回报），且状态可被重新派活
  D. housekeeping() 幂等：没有可清的东西时返回 0 且不写审计

用法：~/dsh-suite/.venv/bin/python tests/test_lifecycle.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"

ROOT = Path(tempfile.mkdtemp(prefix="lifecycle-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT / "data")

sys.path.insert(0, str(BACKEND))
from app import db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"隔离失败: {D.DB_PATH}"
from app import monitor  # noqa: E402

db = D.get_db()
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} " + name + (f"  → {detail}" if detail and not cond else ""))


def age_pair(device_code: str, seconds: float) -> None:
    """把某个配对码的过期时间往前推 seconds 秒。"""
    with db.tx() as c:
        c.execute("UPDATE pair_codes SET expires_at=? WHERE device_code=?",
                  (D.now() - seconds, device_code))


print("[A] 过期配对码清理（带宽限期）")
fresh = db.create_pair_code("fresh-pc")
expired_recently = db.create_pair_code("recent-pc")
expired_long_ago = db.create_pair_code("old-pc")
consumed = db.create_pair_code("used-pc")
age_pair(expired_recently["device_code"], 60)          # 刚过期 1 分钟 → 宽限期内，留
age_pair(expired_long_ago["device_code"], 3 * 3600)    # 过期 3 小时 → 该清
age_pair(consumed["device_code"], 5 * 3600)            # 用过且过期 5 小时 → 该清

removed = db.purge_pair_codes(grace_sec=3600)
check("清理返回条数正确（2 条）", removed == 2, str(removed))
check("刚过期的仍在（宽限期内保留证据）",
      db.get_pair_by_device_code(expired_recently["device_code"]) is not None)
check("过期 3 小时的已清掉",
      db.get_pair_by_device_code(expired_long_ago["device_code"]) is None)
check("用过的旧记录也清掉",
      db.get_pair_by_device_code(consumed["device_code"]) is None)
check("仍在有效期的不受影响",
      db.get_pair_by_device_code(fresh["device_code"]) is not None)

print("\n[B] 僵尸子任务收尸")
stale = db.create_subtask("p1", 1, "休眠时领的活", "做点事", "general", assignee="pc-1")
freshsub = db.create_subtask("p1", 2, "刚领的活", "做点事", "general", assignee="pc-1")
done = db.create_subtask("p1", 3, "已完成的活", "做点事", "general", assignee="pc-1")
db.claim_subtask(stale["id"], "pc-1")
db.claim_subtask(freshsub["id"], "pc-1")
db.claim_subtask(done["id"], "pc-1")
db.complete_subtask(done["id"], "做完了", 0)
check("领取时记录了 started_at", db.get_subtask(stale["id"])["started_at"] > 0,
      str(db.get_subtask(stale["id"])["started_at"]))

with db.tx() as c:                                      # 把其中一个的领取时刻推回 3 小时前
    c.execute("UPDATE subtasks SET started_at=? WHERE id=?", (D.now() - 3 * 3600, stale["id"]))

reaped = db.reap_stale_subtasks(hours=2)
check("收尸条数正确（1 条）", reaped == 1, str(reaped))
old = db.get_subtask(stale["id"])
check("僵尸任务被标失败", old["status"] == "failed", old["status"])
check("失败原因说清是超时未回报", "超时未回报" in (old["result"] or ""), old["result"][:80])
check("assignee 保留（看得出是谁没回报）", old["assignee"] == "pc-1", old["assignee"])
check("刚领的任务没被误杀", db.get_subtask(freshsub["id"])["status"] == "running",
      db.get_subtask(freshsub["id"])["status"])
check("已完成的任务不受影响", db.get_subtask(done["id"])["status"] == "done")

print("\n[C] 回收后可重新派活")
again = db.reset_subtask(stale["id"], "pc-2")
check("reset 后回到 pending", again["status"] == "pending", again["status"])
check("可改派给另一台机器", again["assignee"] == "pc-2", again["assignee"])
check("失败原因已清空", again["result"] == "")

print("\n[D] housekeeping 幂等且无事时不写审计")
before = len(db.query("SELECT id FROM audit_log WHERE action='subtask.reap'"))
first = monitor.housekeeping()
second = monitor.housekeeping()
check("housekeeping 返回计数字段",
      set(first) == {"pair_codes_purged", "subtasks_reaped"}, str(first))
check("第二次没有可清的东西", second["pair_codes_purged"] == 0 and second["subtasks_reaped"] == 0,
      str(second))
check("无事时不新增审计",
      len(db.query("SELECT id FROM audit_log WHERE action='subtask.reap'")) == before,
      "多写了审计")

print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
