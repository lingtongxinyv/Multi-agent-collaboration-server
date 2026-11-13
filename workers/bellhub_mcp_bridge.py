#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BellHub MCP stdio 桥（供 Trae / VS Code / Claude Desktop 等 MCP 宿主接入）。

作者：站娘助手  版本 2026-10-22.1
运行位置：任意一台能访问 BellHub 的机器（Windows / macOS / Linux 均可）。
         与 Hub 同机时走 127.0.0.1（免密钥）；异地时自动回落到公网端点，
         用 config.json 的 mcp_token（或同目录 mcp_token.txt）做 Bearer 鉴权。

【版本史】
- v2026-10-13.1  初版：本机优先、失败回落公网。缺陷：没有密钥通道（异地必 401），
                 且拿到会话 ID 后不再回落（initialize 成功而 tools/list 报连接失败）。
- v2026-10-13.2  修上述两个缺陷：支持 Bearer 密钥；端点落定后粘住。
- v2026-10-13.3  本版：把「配置读不到」从静默失败改成大声报错（v2 曾因此让人白查
                 两轮），并新增纯文本密钥文件 mcp_token.txt（绕开 JSON 语法陷阱），
                 config.json 支持 UTF-8(BOM)/UTF-16/GBK 多种编码。
- v2026-10-22.1  修「Hub 一重启，桥就永久瘫」：Hub 的 MCP 会话是内存态，重启后旧
                 Mcp-Session-Id 一律 404 Session not found；旧版只在**连接层**失败时
                 清会话，404 属 HTTP 层 ⇒ 会话 ID 永不清除，此后每条工具调用都报错，
                 直到重启 Trae 才恢复（2026-10-22 管理员报「BellHub MCP 又断了」的根因：
                 Linux 侧每次关机再开机都会触发一次）。现在识别会话失效 → 自动重新
                 initialize + notifications/initialized → 重试本条消息一次。

【它做什么】
把 MCP 宿主的 stdio 协议转发到 BellHub Hub 的 streamable-http 端点，
让 Trae 这样的 IDE 能直接调用助手的全部工具（device_send / task_poll /
sandbox_read / blackboard_read …）。

【依赖】纯标准库 urllib，无需 pip install。

【配置】① config.json（放本文件同目录，公网访问时必需 mcp_token）：
{
  "hub": "https://agent.example.com/mcp-hub",
  "name": "desktop-01",
  "token": "",
  "mcp_token": "<服务器 ~/dsh-suite/data/mcp_token 的内容>"
}
       ② 或者最省事的写法：同目录放一个纯文本 mcp_token.txt，内容就一行密钥。
          ★ 密钥来源优先级：环境变量 BELLHUB_MCP_TOKEN → config.json 的 mcp_token
            → 同目录 mcp_token.txt

【Trae / VS Code 的 mcp.json 配置示例】
{
  "mcpServers": {
    "bellhub": {
      "command": "python",
      "args": ["D:\\16650\\Documents\\ling shu\\bellhub_mcp_bridge.py"],
      "env": { "BELLHUB_MCP_TOKEN": "<token>" }
    }
  }
}

【协议要点（streamable-http）】
- initialize → 服务端在响应头返回 Mcp-Session-Id，后续请求必须带上；
- 通知类消息（notifications/*）无 id、服务端返回 202，桥不回写 stdout；
- 响应可能是 SSE 帧（"event: message\\ndata: {...}"），需拆出 data 行。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from urllib import error, request

VERSION = "2026-10-22.1"
_BASE = Path(__file__).resolve().parent
CONFIG_PATH = _BASE / "config.json"
TOKEN_FILE = _BASE / "mcp_token.txt"          # 纯文本密钥（一行），可选
DEFAULT_HUB = "https://agent.example.com/mcp-hub"
LOCAL_HUB = "http://127.0.0.1:8765"           # 与 Hub 同机时的直连地址（免密钥）
TIMEOUT = 120

_lock = threading.Lock()
# endpoint：本会话已落定的 /**/mcp/ 基址（落定后粘住，避免每条消息都去撞死端口）
# token/source：启动时解析好的公网密钥及其来源（用于日志自证）
_session = {"id": None, "endpoint": None, "token": "", "source": ""}


def log(msg: str) -> None:
    """日志走 stderr —— stdout 只能有 JSON-RPC，混入任何字符都会污染协议。"""
    print(f"[bellhub-bridge] {msg}", file=sys.stderr, flush=True)


def decode_text(raw: bytes) -> str:
    """按 BOM 猜编码解码；Windows 记事本默认可能存成 UTF-16 或带 BOM 的 UTF-8。"""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "replace")


def load_config() -> dict:
    """读取 config.json。读不到/写错时大声报错 —— 静默忽略是 v2 最大的坑。"""
    if not CONFIG_PATH.is_file():
        log(f"提示：未找到 {CONFIG_PATH}（仅同机直连可忽略；公网访问需要密钥，"
            f"也可改建同目录 {TOKEN_FILE.name}）")
        return {}
    try:
        text = decode_text(CONFIG_PATH.read_bytes())
    except Exception as exc:                       # noqa: BLE001
        log(f"⚠ 读取 {CONFIG_PATH.name} 失败：{type(exc).__name__}: {exc}")
        return {}
    try:
        cfg = json.loads(text)
        if not isinstance(cfg, dict):
            raise ValueError("顶层不是 JSON 对象")
    except Exception as exc:                       # noqa: BLE001
        log(f"⚠ {CONFIG_PATH.name} 解析失败，已忽略该文件：{type(exc).__name__}: {exc}")
        log("  常见原因：用了中文全角引号“”、末尾多逗号、括号不配对，"
            "或实际文件名是 config.json.txt（记事本另存为的经典坑，"
            "请在资源管理器「查看→文件扩展名」里确认）")
        return {}
    log(f"已加载配置 {CONFIG_PATH}")
    return cfg


def resolve_token(cfg: dict) -> tuple[str, str]:
    """密钥来源优先级：环境变量 → config.json 的 mcp_token → 同目录 mcp_token.txt。"""
    env = (os.environ.get("BELLHUB_MCP_TOKEN") or "").strip()
    if env:
        return env, "环境变量 BELLHUB_MCP_TOKEN"
    cfg_tok = str(cfg.get("mcp_token") or "").strip()
    if cfg_tok:
        return cfg_tok, "config.json 的 mcp_token 字段"
    if TOKEN_FILE.is_file():
        try:
            raw = decode_text(TOKEN_FILE.read_bytes()).strip()
            first = raw.splitlines()[0].strip() if raw else ""
        except Exception as exc:                   # noqa: BLE001
            log(f"⚠ 读取 {TOKEN_FILE.name} 失败：{type(exc).__name__}: {exc}")
            return "", ""
        if first:
            return first, TOKEN_FILE.name
        log(f"⚠ {TOKEN_FILE.name} 是空文件")
    return "", ""


def build_urls(cfg: dict) -> list[str]:
    """端点候选顺序：本机直连 → config.hub 指定的公网地址。"""
    pub = (cfg.get("hub") or DEFAULT_HUB).rstrip("/")
    urls = [f"{LOCAL_HUB}/mcp/"]
    pub_url = f"{pub}/mcp/"
    if pub_url != urls[0]:
        urls.append(pub_url)
    return urls


def _headers(cfg: dict) -> dict:
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if _session["id"]:
        h["Mcp-Session-Id"] = _session["id"]
    if _session["token"]:
        # 公网端点必需；发给本机 Hub 也无害（本机不校验）
        h["Authorization"] = f"Bearer {_session['token']}"
    name, dev_token = cfg.get("name"), cfg.get("token")
    if name and dev_token:
        # 仅作会话标识；Hub 不校验该头
        h["X-BellHub-Client"] = f"{name}:{str(dev_token)[:8]}"
    return h


def post(url: str, payload: dict, cfg: dict) -> tuple[dict | None, str | None, str]:
    """POST 一条 JSON-RPC 消息。

    返回 (数据, 错误文案, 错误类别)：
      ok    —— 成功（数据可能是 None，代表通知类 202 空响应）
      conn  —— 连接层失败（可以换端点重试）
      http  —— 服务端返回 4xx/5xx（配置问题，原样上报）
      parse —— 响应不是合法 JSON（协议问题，原样上报）
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=body, headers=_headers(cfg), method="POST")
    try:
        with request.urlopen(req, timeout=TIMEOUT) as resp:
            sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
            if sid:
                _session["id"] = sid
            raw = resp.read().decode("utf-8", "replace")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        sid = exc.headers.get("Mcp-Session-Id") if exc.headers else None
        if sid:
            _session["id"] = sid
        if exc.code == 401:
            return None, ("BellHub 拒绝了请求（401）：公网端点需要密钥。"
                          "请把服务器 ~/dsh-suite/data/mcp_token 的内容填进本目录 "
                          f"config.json 的 mcp_token 字段（或新建 {TOKEN_FILE.name}，"
                          f"只放一行密钥）。原始响应: {raw[:200]}"), "http"
        if exc.code == 406:
            return None, ("Hub 要求 Accept 同时包含 application/json 与 text/event-stream，"
                          f"当前请求头不被接受（406）。原始响应: {raw[:200]}"), "http"
        return None, f"Hub 返回 HTTP {exc.code}: {raw[:200] or '(无正文)'}", "http"
    except Exception as exc:                       # noqa: BLE001
        return None, f"连接 Hub 失败: {type(exc).__name__}: {exc}", "conn"

    if not raw.strip():
        return None, None, "ok"                    # 通知类：无正文，属正常
    # SSE 帧可能带 "event: message" / "id: ..." 等行，取 data 行
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        return json.loads(raw), None, "ok"
    except json.JSONDecodeError:
        return None, f"无法解析 Hub 响应: {raw[:200]}", "parse"


def _is_session_lost(err: str | None) -> bool:
    """Hub 重启后旧会话会回 404 Session not found —— 这是「可自动恢复」的失效。"""
    low = (err or "").lower()
    if "session not found" in low:
        return True
    return "http 404" in low and "session" in low


def reinit(url: str, cfg: dict) -> tuple[bool, str | None]:
    """重开一次 MCP 会话：initialize 拿新 Mcp-Session-Id → notifications/initialized。"""
    _session["id"] = None
    payload = {"jsonrpc": "2.0", "id": "bridge-reinit", "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "bellhub-bridge", "version": VERSION}}}
    _res, err, kind = post(url, payload, cfg)
    if kind != "ok" or not _session["id"]:
        return False, err or "Hub 未返回 Mcp-Session-Id"
    post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, cfg)
    return True, None


def send(obj: dict) -> None:
    with _lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def reply(msg: dict, res: dict | None, err: str | None) -> None:
    if "id" not in msg:                            # 通知：无需回复
        return
    if err:
        send({"jsonrpc": "2.0", "id": msg["id"],
              "error": {"code": -32000, "message": err}})
    else:
        send(res if res is not None else {"jsonrpc": "2.0", "id": msg["id"], "result": {}})


def handle(msg: dict, cfg: dict) -> None:
    # 已落定端点 → 只走它；否则本机优先、失败回落公网
    urls = [_session["endpoint"]] if _session["endpoint"] else build_urls(cfg)
    err: str | None = None
    for idx, url in enumerate(urls):
        res, err, kind = post(url, msg, cfg)
        if kind == "http" and msg.get("method") != "initialize" and _is_session_lost(err):
            log("会话已失效（Hub 重启过），重开一次会话后重试本条")
            ok, why = reinit(url, cfg)
            if ok:
                res, err, kind = post(url, msg, cfg)
            else:
                err = f"重开会话失败：{why}"
        if kind == "ok":
            if _session["endpoint"] != url:
                log(f"端点锁定为 {url}")
                _session["endpoint"] = url
                if url != urls[0] and not _session["token"]:
                    log("⚠ 正在使用公网端点但未配置密钥 —— 若随后的请求返回 401，"
                        "请配置 mcp_token（见文件头「配置」一节）")
            reply(msg, res, None)
            return
        if kind != "conn" or idx + 1 >= len(urls):
            break                                  # 非连接问题，或已无候选 → 上报
        log(f"{url} 连不上（{err}），改用下一个端点")
        _session["id"] = None                      # 换端点＝换会话，清掉旧会话 ID
    if _session["endpoint"] and err and "连接 Hub 失败" in err:
        # 已落定端点整体失效：解锁，下一条消息重新探测（Hub 重启/网络切换场景）
        log("已落定端点失联，下一条消息将重新探测")
        _session["endpoint"] = None
        _session["id"] = None
    reply(msg, None, err)


def main() -> None:
    cfg = load_config()
    _session["token"], _session["source"] = resolve_token(cfg)
    urls = build_urls(cfg)
    key_desc = (f"已配置（来源：{_session['source']}，长度 {len(_session['token'])}）"
                if _session["token"] else "未配置（仅同机直连可用）")
    log(f"v{VERSION} 就绪｜程序目录 {_BASE}")
    log(f"候选端点 {urls}｜密钥 {key_desc}｜设备 {cfg.get('name') or '(未配置)'}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"忽略非法 JSON: {line[:80]}")
            continue
        try:
            handle(msg, cfg)
        except Exception as exc:                   # noqa: BLE001
            log(f"处理 {msg.get('method')} 出错: {type(exc).__name__}: {exc}")
            if "id" in msg:
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "error": {"code": -32603, "message": str(exc)}})


if __name__ == "__main__":
    main()
