"""值守 worker「回合/成本预算」护栏回归测试（护栏 v2026-10-16d）。

背景（2026-10-16 事故）：两值守 07:42–09:16 以「复核回合」互相回信 18 轮，累计
2,376,740 tokens，当日余额消耗 7.01（前一日全天仅 1.92）。原护栏是频率型
（同对端 6 条/600s），对**慢循环**（5–8 分钟/跳）永不触发；而会话滚动又把「6 轮」
计数随新会话清零 ⇒ 没有任何闸拦得住。

本测试验证新增的 d/e 两闸：
  · 跨会话/跨重启持久（落 partners.json，滚动与重启都不清零）
  · **拦在 run_headless 之前**（不拦在这里就等于没省到钱）
  · 窗口滑出后自动放行
  · 豁免看门狗自救通道（首行「【事故】」）
  · 黑板告警每窗口只写一次

用法：~/dsh-suite/.venv/bin/python tests/test_agent_loop_budget.py
"""
import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKERS = REPO / "workers"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def load_worker(stem: str, display: str):
    spec = importlib.util.spec_from_file_location(f"{stem}_agent_t", WORKERS / f"{stem}_agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_env(stem: str, display: str):
    root = Path(tempfile.mkdtemp(prefix=f"loopbudget-{stem}-"))
    home = root / "dsh_home"
    home.mkdir()
    db = root / "panel.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE blackboard(key TEXT PRIMARY KEY, value TEXT,"
                " updated_at REAL, updated_by TEXT)")
    con.commit()
    con.close()
    cfg = {"name": display, "hub": "http://127.0.0.1:8765", "token": "t",
           "dsh_home": str(home), "panel_db": str(db),
           "loop_window_sec": 3600, "loop_max_replies": 3, "loop_max_tokens": 300000}
    return cfg, db, home


def bb_value(db: Path, key_like: str):
    con = sqlite3.connect(db)
    rows = con.execute("SELECT key,value FROM blackboard WHERE key LIKE ?", (key_like,)).fetchall()
    con.close()
    return rows


def run_suite(stem: str, display: str, exempt_partner: str):
    print(f"\n{'=' * 62}\n【{stem}】{display}@{stem} 值守 worker\n{'=' * 62}")
    mod = load_worker(stem, display)
    cfg, db, home = make_env(stem, display)

    calls = {"headless": [], "sent": []}

    def fake_headless(_cfg, prompt, sid=None):
        calls["headless"].append(prompt)
        return {"reply": "收到，已处理。", "session_id": "sess-x", "tokens": 1200, "error": None}

    def fake_api(_cfg, path, body=None):
        calls["sent"].append((path, body))
        return {"ok": True}

    mod.run_headless = fake_headless
    mod.api = fake_api

    quiet = lambda: contextlib.redirect_stdout(io.StringIO())

    print("\n[1] 新对端在预算内放行")
    check("预算未用完 → 放行", mod._loop_budget_exceeded(cfg, "甲") == "")

    print("\n[2] 回合预算：回满 3 条即拦")
    for _ in range(3):
        mod._guard_record(cfg, "甲", 1000)
    why = mod._loop_budget_exceeded(cfg, "甲")
    check("回满 3 条后被拦", why != "", why)
    check("拦截原因写明上限", "上限 3" in why, why)
    check("另一个对端不受影响", mod._loop_budget_exceeded(cfg, "乙") == "")

    print("\n[3] 被拦时【不唤起模型】（真省钱的那一步）")
    calls["headless"].clear()
    calls["sent"].clear()
    with quiet():
        mod.handle_message(cfg, {"from": "甲", "text": "第十一轮复核，请回信"})
    check("未调用 run_headless", calls["headless"] == [], str(calls["headless"])[:60])
    check("未发出任何消息", calls["sent"] == [], str(calls["sent"])[:60])

    print("\n[4] 豁免看门狗自救通道（首行「【事故】」）")
    calls["headless"].clear()
    with quiet():
        mod.handle_message(cfg, {"from": "甲", "text": f"{exempt_partner}cpolar 隧道公网不可达，请接管诊断"})
    check("事故类消息仍会唤起（不被预算静默）", len(calls["headless"]) == 1)

    print("\n[5] 窗口滑出后自动放行")
    pj = home / "partners.json"
    data = json.loads(pj.read_text(encoding="utf-8"))
    old = data[f"guard:agent:甲"]
    stale = json.loads(json.dumps(old))
    stale["replies"] = [t - 7200 for t in stale["replies"]]
    stale["tokens"] = [[t - 7200, v] for t, v in stale["tokens"]]
    data[f"guard:agent:甲"] = stale
    pj.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    check("2 小时前的回复不再计数 → 放行", mod._loop_budget_exceeded(cfg, "甲") == "")

    print("\n[6] 成本预算：token 累计超限即拦（条数没超也拦）")
    mod._guard_record(cfg, "丙", 350000)
    why2 = mod._loop_budget_exceeded(cfg, "丙")
    check("累计 350k tokens 被拦", why2 != "", why2)
    check("拦截原因按 token 计", "tokens" in why2, why2)

    print("\n[7] 黑板告警：同一窗口只写一次，且不爆键")
    before = bb_value(db, "admin/%-loop-budget-%")
    mod._loop_budget_exceeded(cfg, "甲")   # 再触发一次
    after = bb_value(db, "admin/%-loop-budget-%")
    check("告警键存在且唯一", len(after) == 1, str(after)[:80])
    check("重复触发不重复追加", before[0][1] == after[0][1] if before else False)
    check("告警正文含触发原因", "回合/成本预算触发" in after[0][1])

    print("\n[8] 放行路径：正常唤起 + 发出 + 记账")
    calls["headless"].clear()
    calls["sent"].clear()
    with quiet():
        mod.handle_message(cfg, {"from": "丁", "text": "请帮我查一下磁盘占用"})
    check("放行时唤起模型", len(calls["headless"]) == 1)
    check("放行时已发回信", any(p == "/agent/message" for p, _ in calls["sent"]),
          str([p for p, _ in calls["sent"]]))
    st = mod._guard_load(cfg, "丁")
    check("已记账 1 条回复", len(st["replies"]) == 1, str(st))
    check("已记账本轮 tokens", st["tokens"] and st["tokens"][0][1] == 1200, str(st["tokens"]))

    print("\n[9] 会话滚动/进程重启不清零（持久化在 partners.json）")
    pj_data = json.loads(pj.read_text(encoding="utf-8"))
    check("guard 状态在 partners.json 里", "guard:agent:丁" in pj_data)
    check("没有污染会话键", any(k.startswith("agent:") for k in pj_data))


def main():
    for stem, display, exempt in (("resident", "助手", "【事故】"),
                                  ("partner", "伙伴", "【事故】")):
        run_suite(stem, display, exempt)
    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
