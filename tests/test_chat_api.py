"""对话接口的最小回归测试（不联网、不碰生产库）。

跑法：
    cd dsh-suite && .venv/bin/python -m pytest tests/test_chat_api.py -q

覆盖：
  1. 建线程 / 发消息 / 值班应答落库（模型调用已桩替换）
  2. 游标增量读：after 之前的消息不再返回
  3. peer 身份发 /task 被拒（admin 闸）
  4. 每日 token 预算累计
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DSH_SUITE_DATA", tempfile.mkdtemp(prefix="chat-test-"))

import hashlib  # noqa: E402
import time  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import chat  # noqa: E402
from app.chat import router  # noqa: E402
from app.db import get_db  # noqa: E402

PEER_TOKEN = "test-peer-token-0123456789"
AUTH = {"Authorization": f"Bearer {PEER_TOKEN}"}


def _seed_peer() -> None:
    db = get_db()
    with db.tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO chat_peers (id,name,kind,token_hash,capabilities,"
            "created_at,last_seen,enabled,note) VALUES (?,?,?,?,?,?,?,1,?)",
            ("p1", "伙伴-A", "peer",
             hashlib.sha256(PEER_TOKEN.encode()).hexdigest(), "[]", time.time(), 0, "test"))


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_chat_flow(monkeypatch):
    chat._ensure_schema()
    _seed_peer()
    monkeypatch.setattr(
        chat, "_call_model",
        lambda messages, max_tokens=700, ident=None: ("测试回复", 7),
    )
    client = _client()

    r = client.post("/api/chat/threads", headers=AUTH,
                    json={"title": "t", "participants": ["伙伴-A"]})
    assert r.status_code == 200, r.text
    tid = r.json()["thread"]["id"]
    assert "助手" in r.json()["thread"]["participants"]

    r = client.post(f"/api/chat/threads/{tid}/messages", headers=AUTH,
                    json={"text": "在吗", "wait": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"]["text"] == "测试回复"
    assert body["reply"]["meta"]["mode"] == "duty"
    reply_id = body["reply"]["id"]

    # 游标：after=reply_id 之后没有新消息
    r = client.get(f"/api/chat/threads/{tid}?after={reply_id}", headers=AUTH)
    assert r.json()["posts"] == []
    assert r.json()["cursor"] == reply_id

    # 全量读
    r = client.get(f"/api/chat/threads/{tid}?after=0", headers=AUTH)
    roles = [p["role"] for p in r.json()["posts"]]
    assert roles == ["user", "assistant"]

    # peer 不能派活
    r = client.post(f"/api/chat/threads/{tid}/messages", headers=AUTH,
                    json={"text": "/task 删库跑路"})
    assert "管理员" in r.json()["reply"]["text"]

    # 预算累计（_call_model 已被桩替换，这里直接验证计数原语）
    before = chat._budget_used()
    chat._budget_add(7)
    assert chat._budget_used() == before + 7


def test_voice_prompt_only_injects_relevant_facts(monkeypatch):
    monkeypatch.setattr(chat, "_persona_text", lambda: "助手人格")
    monkeypatch.setattr(chat, "_facts_text", lambda f: "SERVER_FACTS")
    monkeypatch.setattr(chat, "_fact_override", lambda f: "SCHEDULE_FACTS")
    monkeypatch.setattr(chat, "_history", lambda tid, limit: [])
    thread = {"id": "voice", "kind": "esp32-voice"}
    ident = {"id": "admin:test", "name": "管理员", "kind": "admin"}

    normal = chat._build_messages(thread, ident, "你还记得我吗", {})
    assert "SERVER_FACTS" not in normal[0]["content"]
    assert "SCHEDULE_FACTS" not in normal[0]["content"]
    assert normal[-1]["content"] == "你还记得我吗"

    server = chat._build_messages(thread, ident, "服务器负载怎么样", {})
    assert "SERVER_FACTS" in server[0]["content"]
    assert "SCHEDULE_FACTS" not in server[0]["content"]

    schedule = chat._build_messages(thread, ident, "今晚会关机吗", {})
    assert "SERVER_FACTS" in schedule[0]["content"]
    assert "SCHEDULE_FACTS" in schedule[0]["content"]
    assert "SCHEDULE_FACTS" in schedule[-1]["content"]


def test_emotion_parser_handles_split_invalid_missing_and_trailing_markers():
    parser = chat._EmotionParser()
    assert parser.feed("[") == ""
    assert parser.feed("[emotion:sur") == ""
    assert parser.feed("prised]]你好") == "你好"
    assert parser.finish() == ""
    assert parser.emotion == "surprised"

    invalid = chat._EmotionParser()
    assert invalid.feed("[[emotion:excited]]正文") == "正文"
    assert invalid.emotion == "neutral"
    missing = chat._EmotionParser()
    assert missing.feed("[[emotion:]]正文") == "正文"
    assert missing.emotion == "neutral"

    absent = chat._EmotionParser()
    assert absent.feed("普通[文本") == "普通[文本"
    assert absent.finish() == ""
    assert absent.emotion is None
    trailing = chat._EmotionParser()
    assert trailing.feed("正文[[emotion:ha") == "正文"
    assert trailing.finish() == ""
    assert trailing.emotion is None


def test_voice_sync_and_sse_strip_emotion_before_storage(monkeypatch):
    chat._ensure_schema()
    _seed_peer()
    monkeypatch.setattr(
        chat, "_call_model",
        lambda messages, max_tokens=700, ident=None: ("[[emotion:happy]]\n今天很开心。", 7),
    )
    client = _client()
    created = client.post(
        "/api/chat/threads", headers=AUTH,
        json={"title": "voice-sync", "participants": ["伙伴-A"], "kind": "esp32-voice"},
    )
    tid = created.json()["thread"]["id"]
    response = client.post(
        f"/api/chat/threads/{tid}/messages", headers=AUTH,
        json={"text": "分享一件开心的事", "wait": True},
    )
    reply = response.json()["reply"]
    assert reply["text"] == "今天很开心。"
    assert reply["meta"]["emotion"] == "happy"
    assert "[[emotion:" not in reply["text"]

    def fake_stream(messages, state, max_tokens=700, ident=None):
        yield "[[em"
        yield "otion:ha"
        yield "ppy]]\n很"
        yield "开心。"

    monkeypatch.setattr(chat, "_stream_model", fake_stream)
    created = client.post(
        "/api/chat/threads", headers=AUTH,
        json={"title": "voice-stream", "participants": ["伙伴-A"], "kind": "esp32-voice"},
    )
    tid = created.json()["thread"]["id"]
    with client.stream(
        "POST", f"/api/chat/threads/{tid}/messages/stream", headers=AUTH,
        json={"text": "再讲一次", "wait": True},
    ) as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200, body
    assert 'event: emotion\ndata: {"emotion": "happy"}' in body
    assert body.index("event: emotion") < body.index("event: delta")
    assert "[[emotion:" not in body
    stored = get_db().one(
        "SELECT * FROM chat_posts WHERE thread_id=? AND role='assistant'", (tid,)
    )
    assert stored["text"] == "很开心。"
    assert '"emotion": "happy"' in stored["meta"]


def test_emotion_prompt_is_limited_to_esp32_voice_threads(monkeypatch):
    monkeypatch.setattr(chat, "_persona_text", lambda: "助手人格")
    monkeypatch.setattr(chat, "_facts_text", lambda f: "SERVER_FACTS")
    monkeypatch.setattr(chat, "_fact_override", lambda f: "SCHEDULE_FACTS")
    monkeypatch.setattr(chat, "_history", lambda tid, limit: [])
    admin = {"id": "admin:test", "name": "管理员", "kind": "admin"}
    public = {"id": "peer:test", "name": "访客", "kind": "peer", "caps": ["public"]}
    for ident in (admin, public):
        voice = chat._build_messages({"id": "v", "kind": "esp32-voice"}, ident, "你好", {})
        normal = chat._build_messages({"id": "n", "kind": "peer"}, ident, "你好", {})
        assert "[[emotion:<枚举>]]" in voice[0]["content"]
        assert "[[emotion:<枚举>]]" not in normal[0]["content"]


def test_auth_required():
    client = _client()
    assert client.get("/api/chat/health").status_code == 401
    bad = client.get("/api/chat/health", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
