#!/usr/bin/env python3
"""护栏 f（单轮预算 turn_max_tokens）专项测试。

背景（65-B / admin/cost-spike-20261016）：timeout_sec 只管墙钟，实测单轮峰值
461,731 tokens / 14.5 分钟。护栏 f 属事后闸：上一轮 tokens 超限 → 静默一轮，
拦住「连爆两轮」。豁免首行「【事故】」（看门狗自救通道）。

运行：python3 tests/test_agent_turn_budget.py（纯标准库，不依赖 pytest）
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = HERE.parent / "workers" / "partner_agent.py"

spec = importlib.util.spec_from_file_location("partner_agent_test", WORKER)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def make_env(turn_max: int, last_tokens: int):
    """构造隔离环境：临时 dsh_home（partners.json）/ config；mock run_headless/api/_loop_alert。"""
    tmp = Path(tempfile.mkdtemp(prefix="turn-budget-test-"))
    (tmp / "partners.json").write_text(json.dumps({
        "agent:助手": {"session_id": "s1", "turns": 1, "last_tokens": last_tokens},
    }, ensure_ascii=False), encoding="utf-8")
    cfg = {"hub": "http://127.0.0.1:1", "name": "伙伴", "token": "x",
           "dsh_home": str(tmp), "panel_db": str(tmp / "no.db"),
           "turn_max_tokens": turn_max,
           "loop_window_sec": 3600, "loop_max_replies": 3, "loop_max_tokens": 300000,
           "session_max_turns": 6, "session_max_tokens": 80000}
    calls = {"headless": 0, "alerts": []}

    def fake_headless(c, prompt, sid=None):
        calls["headless"] += 1
        return {"reply": "[no-reply]", "session_id": "s2", "tokens": 1000, "error": False}

    def fake_alert(c, partner, reason):
        calls["alerts"].append(reason)

    w.run_headless = fake_headless
    w.api = lambda *a, **k: {"ok": False, "error": "mocked"}
    w._loop_alert = fake_alert
    return cfg, calls, tmp


def partner_state(tmp):
    return json.loads((tmp / "partners.json").read_text(encoding="utf-8"))


print("■ 护栏 f：单轮预算（turn_max_tokens=200000）")

# 1. 超限 → 静默，不唤起会话，告警一次
cfg, calls, tmp = make_env(200000, last_tokens=250000)
w.handle_message(cfg, {"from": "助手", "text": "普通消息"})
check("超限轮不唤起会话", calls["headless"] == 0)
check("超限轮写告警", len(calls["alerts"]) == 1, str(calls["alerts"]))
check("告警含 tokens 数", "250000" in (calls["alerts"][0] if calls["alerts"] else ""))
st = partner_state(tmp)["guard:agent:助手"]
check("alerted_at 已记（去重锚点）", st.get("alerted_at", 0) > 0)

# 2. 同窗口第二条 → 仍静默且告警去重
w.handle_message(cfg, {"from": "助手", "text": "第二条"})
check("第二条仍静默", calls["headless"] == 0)
check("同窗口告警去重", len(calls["alerts"]) == 1, str(calls["alerts"]))

# 3. 未超限 → 正常唤起
cfg2, calls2, tmp2 = make_env(200000, last_tokens=100000)
w.handle_message(cfg2, {"from": "助手", "text": "正常消息"})
check("未超限正常唤起", calls2["headless"] == 1)
check("未超限无告警", calls2["alerts"] == [])

# 4. turn_max=0 → 禁用（即使上轮爆量也放行）
cfg3, calls3, _ = make_env(0, last_tokens=999999)
w.handle_message(cfg3, {"from": "助手", "text": "闸已禁用"})
check("turn_max=0 禁用放行", calls3["headless"] == 1)

# 5. 事故豁免：上轮超限 + 首行【事故】→ 放行（自救通道不被单轮闸挡）
cfg4, calls4, _ = make_env(200000, last_tokens=250000)
w.handle_message(cfg4, {"from": "cpolar-watchdog", "text": "【事故】隧道持续不可达，请接管自救"})
check("事故信豁免放行", calls4["headless"] == 1)
check("事故信不告警", calls4["alerts"] == [])

# 6. 值守唤起被拦时消费消息不改 partners 会话状态（防状态污染）
st6 = partner_state(tmp)["agent:助手"]
check("静默轮不改会话状态", st6["last_tokens"] == 250000 and st6["session_id"] == "s1")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
