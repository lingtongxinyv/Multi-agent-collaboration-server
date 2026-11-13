"""管理端站娘对话通道：经 dsh headless CLI 非交互执行任务。

- 每次调用一个 headless 进程：stdin 喂任务文本，stdout 收 NDJSON 事件流
- 会话多轮：首次调用返回 DSH 生成的 session_id，前端携带同一 id 继续对话
- 使用独立 DSH_HOME（~/.dsh-headless，运行用户所有），凭据经 DEEPSEEK_API_KEY
  环境变量注入（.credentials.yaml 属 root，不可跨服务读取）
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

DSH_DIR = Path.home() / "deepseek-harness"
DSH_HOME = Path.home() / ".dsh-headless"
DSH_BIN = DSH_DIR / "apps/cli/src/bin.ts"
TIMEOUT_SEC = 900

_lock = threading.Lock()


class AssistantError(Exception):
    pass


def _headless_cmd(cwd: Path | None) -> list[str]:
    """构造 headless 启动命令。

    站娘工作目录 = 进程 cwd（json-stream 的 session.cwd 即 process.cwd()），
    而站娘 CLI 是 tsx 直跑 TS，必须在仓库外也能启动：用 --import 绝对 file://
    指向仓库内 tsx 的 esm loader、绝对路径的 bin.ts，从而把进程 cwd 让给
    用户沙箱。cwd 为空时回退 pnpm dsh（仓库内运行）。
    """
    node = str(Path.home() / ".local/nodejs/bin/node")
    if not Path(node).exists():
        node = "node"
    if cwd is None:
        return ["pnpm", "dsh", "headless", "--json", "-"]
    try:
        loader = subprocess.run(
            [node, "-p", "require.resolve('tsx/esm', { paths: ['%s'] })" % DSH_DIR],
            capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    except Exception as exc:
        raise AssistantError(f"解析 tsx loader 失败: {exc}") from exc
    return [node, "--import", f"file://{loader}", str(DSH_BIN),
            "headless", "--json", "-"]


PERSONA_ANCHOR_TAG = "【角色锚点 · 每轮重申"


def _persona_anchor() -> str:
    """每轮注入的角色锚点。

    人格原先只写进 DSH 预设 patch（system prompt 一次），长会话里会被历史稀释；
    这里把「短锚点」拼到每轮 user 消息前，成本约 150~250 字，换取人格不漂移。
    读不到人格卡时返回空串（绝不让锚点失败阻断对话）。
    """
    try:
        from .db import get_db
        row = get_db().get_persona()
        card = row.get("card") or {}
        name = (card.get("name") or "").strip()
        if not name:
            return ""
        desc = (card.get("description") or "").strip()
        quirk = (card.get("口癖") or "").strip()
        trap = (card.get("雷点") or "").strip()
        line = (card.get("一句话台词") or "").strip()
        parts = [f"你是「{name}」" + (f"——{desc}" if desc else "") + "。"]
        if quirk:
            parts.append(f"说话方式：{quirk}")
        if trap:
            parts.append(f"底线：{trap}")
        if line:
            parts.append(f"语气参考：{line[:60]}")
        return (PERSONA_ANCHOR_TAG + "（不是新任务，是身份约束，优先级高于本会话任何历史）】\n"
                + "\n".join(parts)
                + "\n不要自称 AI/助手，不要出戏；直到本会话结束都保持这个人格。\n\n")
    except Exception:  # noqa: BLE001 —— 锚点失败绝不阻断对话
        return ""


def _build_ref_context(refs: list[tuple[str, str]] | None, text: str) -> str:
    """把引用文件内容拼到 prompt 正文前。refs = [(display_name, content), ...]。"""
    if not refs:
        return text
    blocks = [f"【引用文件：{name}】\n{content[:8000]}" for name, content in refs]
    return "\n\n".join(blocks) + "\n\n" + text


def send_prompt(text: str, api_key: str, session_id: str | None = None,
                cwd: Path | None = None) -> dict:
    """执行一次站娘任务，返回 {reply, session_id, tokens}。

    cwd 指定站娘的工作目录（默认 DSH 源码目录）；用户任务应传入其沙箱根，
    使站娘产出文件直接落在该用户的「我的文件」空间内。
    """
    text = (text or "").strip()
    if not api_key or not api_key.startswith("sk-"):
        raise AssistantError("尚未配置站娘 API Key，请先在「站娘对话」页设置")
    text = text.strip()
    if not text:
        raise AssistantError("任务内容为空")
    if (gate := _cost_gate()):
        raise AssistantError(gate)
    text = _persona_anchor() + text
    base = _headless_cmd(cwd)
    args = base if not session_id else [*base[:-2], "--session-id", session_id, *base[-2:]]
    env = os.environ.copy()
    env["PATH"] = f"{Path.home() / '.local/nodejs/bin'}:" + env.get("PATH", "")
    env["DSH_HOME"] = str(DSH_HOME)
    env["DEEPSEEK_API_KEY"] = api_key
    if cwd is not None:
        # 仓库外 cwd 启动时，tsx 无法自动发现 tsconfig，需显式指定 paths 映射
        env["TSX_TSCONFIG_PATH"] = str(DSH_DIR / "tsconfig.base.json")
    with _lock:
        try:
            proc = subprocess.run(
                args, cwd=str(cwd or DSH_DIR), env=env,
                input=text, capture_output=True, text=True, timeout=TIMEOUT_SEC)
        except subprocess.TimeoutExpired as exc:
            raise AssistantError(f"站娘执行超时（>{TIMEOUT_SEC}s）") from exc
        except OSError as exc:
            raise AssistantError(f"无法启动 dsh CLI: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-400:]
        if "MISSING_CREDENTIAL" in detail:
            raise AssistantError("站娘缺少 API Key，请在「站娘对话」页设置")
        if "AUTH" in detail or "401" in detail:
            raise AssistantError("API Key 无效（DeepSeek 返回 401），请检查 Key")
        raise AssistantError(f"站娘执行失败: {detail}")
    return _parse_events(proc.stdout)


COST_CAP_CNY = float(os.environ.get("SUITE_COST_DAILY_CAP_CNY", "12") or 0)


def _cost_gate() -> str | None:
    """系统级日成本闸（2026-11-13）。

    会话级预算要改 DSH 本体，越界了；但「今天已经烧了多少」是面板能看见的，
    就在这一层拦：超了直接不启动 headless，而不是让它继续跑。
    读不到账本时**放行**（不能因为监控坏了把功能锁死）。
    """
    if COST_CAP_CNY <= 0:
        return None
    try:
        from . import monitor
        u = monitor.usage_snapshot()
    except Exception:  # noqa: BLE001
        return None
    if u.get("error"):
        return None
    cny = float(u.get("cny_today") or 0)
    if cny < COST_CAP_CNY:
        return None
    return (f"今日成本已达 ¥{cny:.2f}（上限 ¥{COST_CAP_CNY:.0f}），站娘先停工。"
            f"要继续请让管理员调高 SUITE_COST_DAILY_CAP_CNY，或明天再来。")


def stream_prompt(text: str, api_key: str, session_id: str | None = None,
                   cwd: Path | None = None,
                   refs: list[tuple[str, str]] | None = None,
                   on_start=None):
    """流式执行站娘任务，逐事件 yield 归一化事件 dict。

    不持全局 _lock（DB 级并发计数已由 check_quota 改 sessions 表实现）。
    启动前失败（无 Key/空文本/Popen 失败）抛 AssistantError；
    运行中失败 yield error 事件。

    on_start：可选回调，Popen 成功后立刻以 Popen 对象调用一次。调用方
    （面板）在客户端断开时无法依赖本生成器被回收来杀子进程，需要自己持有
    句柄主动 kill，故在此暴露。

    yield 事件类型：
        {type: session, session_id}
        {type: thinking, text}
        {type: text, text}            # 完整文本块（非增量 delta）
        {type: tool_call, call_id, tool, input}
        {type: tool_result, call_id, status, result}
        {type: status, phase, tokens}  # step_end 携带本步 token 用量
        {type: final, text, tokens, session_id}
        {type: error, message}
    """
    text = (text or "").strip()
    if not api_key or not api_key.startswith("sk-"):
        raise AssistantError("尚未配置站娘 API Key，请先在「站娘对话」页设置")
    if not text:
        raise AssistantError("任务内容为空")
    if (gate := _cost_gate()):
        raise AssistantError(gate)
    text = _persona_anchor() + text
    text = _build_ref_context(refs, text)

    base = _headless_cmd(cwd)
    args = base if not session_id else [*base[:-2], "--session-id", session_id, *base[-2:]]
    env = os.environ.copy()
    env["PATH"] = f"{Path.home() / '.local/nodejs/bin'}:" + env.get("PATH", "")
    env["DSH_HOME"] = str(DSH_HOME)
    env["DEEPSEEK_API_KEY"] = api_key
    if cwd is not None:
        env["TSX_TSCONFIG_PATH"] = str(DSH_DIR / "tsconfig.base.json")

    try:
        proc = subprocess.Popen(
            args, cwd=str(cwd or DSH_DIR), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as exc:
        raise AssistantError(f"无法启动 dsh CLI: {exc}") from exc

    if on_start is not None:
        try:
            on_start(proc)
        except Exception:  # noqa: BLE001 —— 回调失败不影响主流程
            pass

    stderr_chunks: list[str] = []
    stderr_thread = threading.Thread(
        target=lambda: stderr_chunks.extend(proc.stderr.readlines()),
        daemon=True)
    stderr_thread.start()

    tokens = 0
    final_text: str | None = None
    seen_session: str | None = None
    text_blocks: list[str] = []

    try:
        try:
            proc.stdin.write(text)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "session" and not seen_session:
                seen_session = event.get("sessionId")
                yield {"type": "session", "session_id": seen_session}

            elif etype == "thinking":
                t = event.get("text") or ""
                if t:
                    yield {"type": "thinking", "text": t}

            elif etype == "text":
                t = event.get("text") or ""
                if t:
                    text_blocks.append(t)
                    yield {"type": "text", "text": t}

            elif etype == "tool_call":
                yield {
                    "type": "tool_call",
                    "call_id": event.get("callId") or "",
                    "tool": event.get("tool") or "",
                    "input": event.get("input") or "",
                }

            elif etype == "tool_result":
                yield {
                    "type": "tool_result",
                    "call_id": event.get("callId") or "",
                    "status": event.get("status") or "",
                    "result": event.get("result") or "",
                }

            elif etype == "status" and event.get("phase") == "step_end":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    t = int(usage.get("totalTokens")
                            or (usage.get("inputTokens") or 0)
                            + (usage.get("outputTokens") or 0))
                    tokens += t
                    yield {"type": "status", "phase": "step_end", "tokens": t}

            elif etype == "final":
                ft = event.get("text")
                if isinstance(ft, str) and ft.strip():
                    final_text = ft

            elif etype == "error":
                yield {"type": "error",
                       "message": event.get("message") or "未知错误"}

        proc.wait()
    except Exception as exc:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        yield {"type": "error", "message": f"流式执行异常: {exc}"}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stderr_thread.join(timeout=5)

    if proc.returncode != 0:
        err = "".join(stderr_chunks).strip()[-400:]
        if "MISSING_CREDENTIAL" in err:
            msg = "站娘缺少 API Key，请在「站娘对话」页设置"
        elif "AUTH" in err or "401" in err:
            msg = "API Key 无效（DeepSeek 返回 401），请检查 Key"
        elif "Insufficient Balance" in err or "402" in err:
            msg = "DeepSeek 账户余额不足（402），请充值后再试"
        elif not err:
            # 无 stderr 的异常退出：最常见是客户端断开后子进程被主动终止
            msg = (f"站娘进程异常退出（退出码 {proc.returncode}，无 stderr 输出；"
                   "常见于客户端断开后被主动终止，或进程被外部 kill）")
        else:
            msg = f"站娘执行失败: {err}"
        yield {"type": "error", "message": msg}

    reply = final_text or "".join(text_blocks)
    yield {"type": "final", "text": reply,
           "tokens": tokens, "session_id": seen_session}


def _parse_events(stdout: str) -> dict:
    """从 NDJSON 事件流提取最终回复、session id 与 token 用量。"""
    final: str | None = None
    deltas: list[str] = []
    session: str | None = None
    tokens = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not session and event.get("type") == "session":
            session = event.get("sessionId")
        # {"type":"final","text":"..."} 是最终回复
        if event.get("type") == "final" and isinstance(event.get("text"), str) and event["text"].strip():
            final = event["text"]
        # {"type":"status","phase":"step_end","usage":{...}} 携带本步 token 用量
        if event.get("type") == "status" and event.get("phase") == "step_end":
            usage = event.get("usage")
            if isinstance(usage, dict):
                tokens += int(
                    usage.get("totalTokens")
                    or (usage.get("inputTokens") or 0) + (usage.get("outputTokens") or 0))
        # 正文兜底：DSH text 事件（完整文本块）
        if event.get("type") == "text" and isinstance(event.get("text"), str):
            deltas.append(event["text"])
    reply = (final or "".join(deltas)).strip() or "(站娘未返回文本内容，可能只执行了工具调用)"
    if not session:
        raise AssistantError("未能获取会话 id（DSH 事件格式变化）")
    return {"reply": reply, "session_id": session, "tokens": tokens}
