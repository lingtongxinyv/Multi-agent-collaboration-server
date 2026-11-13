"""站娘记忆层：按用户隔离的长期记忆 + 近期互动（参考 伙伴-A 分层思路的简化版）。

- 存储：panel.db memories 表（summary 长期记忆 / recent 近期互动 JSON / chat_session_id）
- 读路径：执行任务/对话前，把用户身份 + 长期记忆 + 近期互动拼进 prompt
- 写路径：每次任务/对话结束记录一条互动；recent 超阈值时后台调用站娘
  把最旧的一批压缩合并进 summary（自动提炼）
- 管理：用户可在「设置」页查看/编辑/清空自己的记忆
"""

from __future__ import annotations

import threading
import time

from .db import get_db

RECENT_LIMIT = 12      # recent 超过该条数触发提炼
DISTILL_BATCH = 6      # 每次把最旧的 N 条并入 summary
ENTRY_MAX = 300        # 单条互动记录截断长度

_distilling: set[str] = set()
_distill_lock = threading.Lock()


def _clip(text: str, limit: int = ENTRY_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_prompt(user_id: str, username: str, role: str, text: str) -> str:
    """把用户身份与记忆拼到请求前面；无记忆时原样返回。"""
    mem = get_db().get_memory(user_id)
    role_label = "管理员" if role == "admin" else "普通用户"
    sections = [f"【当前对话者】{username}（{role_label}）"]
    if mem["summary"]:
        sections.append(f"【关于该用户的长期记忆】\n{mem['summary']}")
    if mem["recent"]:
        lines = []
        for entry in mem["recent"][-RECENT_LIMIT:]:
            lines.append(f"- {entry}")
        sections.append("【最近的互动记录】\n" + "\n".join(lines))
    if len(sections) == 1 and not mem["summary"] and not mem["recent"]:
        return text
    sections.append(f"【本次请求】\n{text}")
    return "\n\n".join(sections)


def record_interaction(user_id: str, user_text: str, reply: str, channel: str = "chat") -> None:
    """记录一轮互动：写入对话历史 + recent 记忆；超阈值后台触发提炼。"""
    db = get_db()
    db.append_chat(user_id, "user", user_text, channel)
    db.append_chat(user_id, "assistant", reply, channel)
    mem = db.get_memory(user_id)
    stamp = time.strftime("%m-%d %H:%M")
    mem["recent"].append(f"[{stamp}] 用户：{_clip(user_text)} ／ 站娘：{_clip(reply)}")
    db.set_memory(user_id, mem["summary"], mem["recent"])
    if len(mem["recent"]) > RECENT_LIMIT:
        _trigger_distill(user_id)


def record_session_interaction(user_id: str, user_text: str, reply: str) -> None:
    """会话模式下的记忆记录：跳过 chat_messages 写入（由 SSE 路由的
    append_chat_msg 负责），只更新 recent 记忆 + 触发提炼。"""
    db = get_db()
    mem = db.get_memory(user_id)
    stamp = time.strftime("%m-%d %H:%M")
    mem["recent"].append(f"[{stamp}] 用户：{_clip(user_text)} ／ 站娘：{_clip(reply)}")
    db.set_memory(user_id, mem["summary"], mem["recent"])
    if len(mem["recent"]) > RECENT_LIMIT:
        _trigger_distill(user_id)


def _trigger_distill(user_id: str) -> None:
    with _distill_lock:
        if user_id in _distilling:
            return
        _distilling.add(user_id)
    threading.Thread(target=_distill, args=(user_id,), daemon=True).start()


def _distill(user_id: str) -> None:
    """后台提炼：把最旧的 DISTILL_BATCH 条互动压缩进长期记忆。"""
    try:
        from . import assistant
        db = get_db()
        key = db.get_setting("assistant_api_key") or ""
        if not key:
            return
        mem = db.get_memory(user_id)
        batch, rest = mem["recent"][:DISTILL_BATCH], mem["recent"][DISTILL_BATCH:]
        if not batch:
            return
        prompt = (
            "你在维护用户档案的记忆摘要。请把「新增互动」中的关键信息（用户偏好、习惯、"
            "重要事实、已达成的约定、任务结果）合并进「已有长期记忆」，去重后输出不超过 "
            "300 字的记忆正文。直接输出记忆内容本身，不要任何解释、标题或客套话。\n\n"
            f"【已有长期记忆】\n{mem['summary'] or '（空）'}\n\n"
            f"【新增互动】\n" + "\n".join(batch)
        )
        result = assistant.send_prompt(prompt, key)
        new_summary = result["reply"].strip()
        if new_summary and not new_summary.startswith("(站娘未返回"):
            db.set_memory(user_id, new_summary[:8000], rest)
    except Exception:
        pass  # 提炼失败不影响主流程，下次再试
    finally:
        with _distill_lock:
            _distilling.discard(user_id)
