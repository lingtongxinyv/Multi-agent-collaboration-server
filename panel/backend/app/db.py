"""SQLite 数据层：账号、配额、审计、告警、任务、worker、persona、设置。

单文件库 $DSH_SUITE_DATA/panel.db（默认 ~/dsh-suite/data/panel.db）。
默认配额：每日 100_000 token、5 并发、1024 MB 磁盘（可按账号覆盖，面板可调）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import pbkdf2_hmac, sha256
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("DSH_SUITE_DATA", str(Path.home() / "dsh-suite/data")))
DB_PATH = DATA_DIR / "panel.db"
SECRET_PATH = DATA_DIR / "panel.secret"
# 星铃委托板的服务侧发布凭据（助手/值守/外部脚本用；不落库、不进 git）
QUEST_TOKEN_PATH = DATA_DIR / "quest-publish.token"

DEFAULT_QUOTA = {"tokens_daily": 100_000, "concurrent": 5, "disk_mb": 1024}

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','user')),
  display_name TEXT NOT NULL DEFAULT '',
  disabled INTEGER NOT NULL DEFAULT 0,
  must_change_pw INTEGER NOT NULL DEFAULT 0,
  quota_tokens_daily INTEGER NOT NULL,
  quota_concurrent INTEGER NOT NULL,
  quota_disk_mb INTEGER NOT NULL,
  used_date TEXT NOT NULL DEFAULT '',
  used_tokens INTEGER NOT NULL DEFAULT 0,
  used_disk_mb REAL NOT NULL DEFAULT 0,
  quota_drive_mb INTEGER NOT NULL DEFAULT 1024,
  used_drive_mb REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor TEXT NOT NULL,
  actor_role TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  level TEXT NOT NULL CHECK(level IN ('info','warn','critical')),
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alert_rules (
  id TEXT PRIMARY KEY,
  metric TEXT NOT NULL,
  op TEXT NOT NULL,
  threshold REAL NOT NULL,
  duration_sec INTEGER NOT NULL DEFAULT 300,
  enabled INTEGER NOT NULL DEFAULT 1,
  notify_email INTEGER NOT NULL DEFAULT 1,
  last_fired REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  user_id TEXT NOT NULL,
  user_name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'chat',
  title TEXT NOT NULL,
  prompt TEXT NOT NULL,
  assignee TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','running','awaiting_approval','done','failed','rejected','cancelled')),
  result TEXT NOT NULL DEFAULT '',
  tokens_used INTEGER NOT NULL DEFAULT 0,
  sandbox_used INTEGER NOT NULL DEFAULT 0,
  finished_at REAL
);
CREATE TABLE IF NOT EXISTS quests (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'side'
    CHECK(category IN ('main','side','daily','bounty')),
  rarity TEXT NOT NULL DEFAULT 'common'
    CHECK(rarity IN ('common','rare','epic','legend')),
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN ('open','active','done')),
  progress INTEGER NOT NULL DEFAULT 0,
  target INTEGER NOT NULL DEFAULT 1,
  reward TEXT NOT NULL DEFAULT '',
  day TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'self',
  ref TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quests_user_day ON quests(user_id, day);
CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  token_hash TEXT NOT NULL,
  capabilities TEXT NOT NULL DEFAULT '[]',
  user_available INTEGER NOT NULL DEFAULT 0,
  approved INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'offline',
  last_seen REAL NOT NULL DEFAULT 0,
  registered_at REAL NOT NULL,
  host TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS persona (
  id INTEGER PRIMARY KEY CHECK(id = 1),
  card TEXT NOT NULL,
  prefix TEXT NOT NULL,
  suffix TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blackboard (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL,
  updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS memories (
  user_id TEXT PRIMARY KEY,
  summary TEXT NOT NULL DEFAULT '',
  recent TEXT NOT NULL DEFAULT '[]',
  chat_session_id TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'chat',
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages(user_id, channel, id);
CREATE TABLE IF NOT EXISTS monitor_samples (
  ts INTEGER PRIMARY KEY,
  cpu REAL, mem REAL, disk REAL, net_rx REAL, net_tx REAL, load1 REAL
);
CREATE INDEX IF NOT EXISTS idx_monitor_ts ON monitor_samples(ts);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  token_hash TEXT NOT NULL,
  agent_type TEXT NOT NULL DEFAULT 'adapter',
  capabilities TEXT NOT NULL DEFAULT '[]',
  tools TEXT NOT NULL DEFAULT '[]',
  host TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT '',
  approved INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'offline',
  last_seen REAL NOT NULL DEFAULT 0,
  last_ping_at REAL NOT NULL DEFAULT 0,
  user_id TEXT NOT NULL DEFAULT '',
  registered_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS subtasks (
  id TEXT PRIMARY KEY,
  parent_task_id TEXT NOT NULL,
  step INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL,
  prompt TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL DEFAULT '',
  capability TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','running','done','failed','skipped')),
  result TEXT NOT NULL DEFAULT '',
  depends_on TEXT NOT NULL DEFAULT '',
  tokens_used INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  started_at REAL NOT NULL DEFAULT 0,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_subtasks_parent ON subtasks(parent_task_id, step);
CREATE INDEX IF NOT EXISTS idx_subtasks_status ON subtasks(status);
CREATE TABLE IF NOT EXISTS agent_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_to ON agent_messages(to_agent, delivered, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  pinned INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'idle'
    CHECK(status IN ('idle','running','done','failed','cancelled')),
  dsh_session_id TEXT NOT NULL DEFAULT '',
  tokens_used INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, pinned, updated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  desc TEXT NOT NULL DEFAULT '',
  token_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'offline',
  last_seen REAL NOT NULL DEFAULT 0,
  host TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id, name);
CREATE TABLE IF NOT EXISTS device_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','sent','done','failed')),
  result TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'user',
  created_at REAL NOT NULL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_devcmd_device ON device_commands(device_id, status, id);
CREATE TABLE IF NOT EXISTS drive_shares (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  relpath TEXT NOT NULL,
  name TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_drive_shares_owner ON drive_shares(owner, created_at);
CREATE TABLE IF NOT EXISTS drive_uploads (
  sid TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  dest_path TEXT NOT NULL,
  name TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL DEFAULT '',
  chunk_size INTEGER NOT NULL,
  total_chunks INTEGER NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drive_uploads_owner ON drive_uploads(owner, updated_at);
-- 设备授权配对（浏览器授权 / 设备码式）：连接器出示 device_code，用户在面板输入
-- user_code 确认后，连接器凭 device_code 换取属于自己的长期凭据（agents.token_hash）。
-- 助手 2026-10-14：让"链接"不再依赖管理员手工发密钥。
CREATE TABLE IF NOT EXISTS pair_codes (
  device_code TEXT PRIMARY KEY,
  user_code TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','approved','consumed','denied')),
  user_id TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL DEFAULT '',
  agent_name TEXT NOT NULL DEFAULT '',
  host TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT '',
  capabilities TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  approved_at REAL,
  consumed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_pair_user_code ON pair_codes(user_code, status);
"""


def now() -> float:
    return time.time()


def today() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        candidate = pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
        return candidate.hex() == digest_hex
    except ValueError:
        return False


def load_secret() -> bytes:
    """会话签名密钥；首次运行生成并持久化（0600）。"""
    if SECRET_PATH.exists():
        data = SECRET_PATH.read_bytes().strip()
        if data:
            return data
    secret = os.urandom(48).hex().encode()
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRET_PATH.write_bytes(secret)
    SECRET_PATH.chmod(0o600)
    return secret


def load_quest_token() -> str:
    """星铃委托板发布凭据（服务侧，明文随机串；首次运行生成并持久化 0600）。

    与 panel.secret 分开：panel.secret 是会话签名密钥，泄露即等于任意账号，
    而本 token 只允许「往某个用户的委托板上挂一条委托」，权限面小得多。
    """
    try:
        data = QUEST_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if data:
            return data
    except FileNotFoundError:
        pass
    token = os.urandom(24).hex()
    QUEST_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEST_TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    QUEST_TOKEN_PATH.chmod(0o600)
    return token


class DB:
    """线程安全的 sqlite 访问（WAL）。"""

    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with _lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._migrate()
        self._seed()

    def _migrate(self) -> None:
        """幂等列迁移：每次启动检查并补齐缺失列。"""
        with _lock:
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(chat_messages)")}
            if "session_id" not in cols:
                self._conn.execute(
                    "ALTER TABLE chat_messages ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, id)")
            # 个人网盘（助手 2026-10-13）：配额独立于代码沙箱，旧库补齐两列
            acols = {r["name"] for r in self._conn.execute("PRAGMA table_info(accounts)")}
            if "quota_drive_mb" not in acols:
                self._conn.execute(
                    "ALTER TABLE accounts ADD COLUMN quota_drive_mb INTEGER NOT NULL DEFAULT 1024")
            if "used_drive_mb" not in acols:
                self._conn.execute(
                    "ALTER TABLE accounts ADD COLUMN used_drive_mb REAL NOT NULL DEFAULT 0")
            # 智能体链接（助手 2026-10-14）：agents 绑定到具体用户，才能做到
            # "每个用户各自的队员"，而不是全站共用一份管理员密钥。
            gcols = {r["name"] for r in self._conn.execute("PRAGMA table_info(agents)")}
            if "user_id" not in gcols:
                self._conn.execute(
                    "ALTER TABLE agents ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            if "last_ping_at" not in gcols:
                self._conn.execute(
                    "ALTER TABLE agents ADD COLUMN last_ping_at REAL NOT NULL DEFAULT 0")
            if "platform" not in gcols:
                self._conn.execute(
                    "ALTER TABLE agents ADD COLUMN platform TEXT NOT NULL DEFAULT ''")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id, status)")
            # 子任务领取时刻（2026-10-14）：用于回收"领了活却再没回报"的僵尸任务
            scols = {r["name"] for r in self._conn.execute("PRAGMA table_info(subtasks)")}
            if "started_at" not in scols:
                self._conn.execute(
                    "ALTER TABLE subtasks ADD COLUMN started_at REAL NOT NULL DEFAULT 0")
            # 星铃委托板（2026-10-22）：委托人 + 幂等键
            #   origin = 谁挂的委托（'self' 管理员自己 / '助手' 站娘 / 其他服务名）
            #   ref    = 外部幂等键（同一 ref 重复发布只更新、不重复挂单）
            qcols = {r["name"] for r in self._conn.execute("PRAGMA table_info(quests)")}
            if "origin" not in qcols:
                self._conn.execute(
                    "ALTER TABLE quests ADD COLUMN origin TEXT NOT NULL DEFAULT 'self'")
            if "ref" not in qcols:
                self._conn.execute(
                    "ALTER TABLE quests ADD COLUMN ref TEXT NOT NULL DEFAULT ''")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_quests_user_ref"
                " ON quests(user_id, ref) WHERE ref <> ''")
            self._conn.commit()

    @contextmanager
    def tx(self):
        with _lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with _lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ── 初始化种子数据 ──────────────────────────────────────────────

    def _seed(self) -> None:
        with self.tx() as c:
            if not c.execute("SELECT 1 FROM accounts WHERE role='admin' LIMIT 1").fetchone():
                c.execute(
                    "INSERT INTO accounts (id, username, password_hash, role, display_name,"
                    " must_change_pw, quota_tokens_daily, quota_concurrent, quota_disk_mb, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "admin", hash_password("admin123"), "admin", "管理员",
                     1, DEFAULT_QUOTA["tokens_daily"], DEFAULT_QUOTA["concurrent"],
                     DEFAULT_QUOTA["disk_mb"], now()),
                )
            # 默认告警规则
            defaults = [
                ("cpu_high", "cpu", ">", 90, 300),
                ("mem_high", "mem", ">", 90, 300),
                ("disk_high", "disk", ">", 90, 600),
                ("load_high", "load1", ">", 4.0, 300),
            ]
            for rid, metric, op, threshold, dur in defaults:
                c.execute(
                    "INSERT OR IGNORE INTO alert_rules (id, metric, op, threshold, duration_sec, enabled, notify_email)"
                    " VALUES (?,?,?,?,?,1,1)", (rid, metric, op, threshold, dur))
            # 默认人格（助手）
            if not c.execute("SELECT 1 FROM persona WHERE id=1").fetchone():
                card_path = Path(os.environ.get(
                    "DSH_SUITE_PERSONA", str(Path.home() / "dsh-suite/persona/resident.json")))
                try:
                    archive = json.loads(card_path.read_text(encoding="utf-8"))
                    if archive.get("format") == "dsh-suite-persona":
                        card = archive.get("card") or {}
                        prefix = (archive.get("persona") or {}).get("prefix", "")
                    else:
                        card, prefix = archive, archive.get("personality", "")
                except Exception:
                    card, prefix = {"name": "助手", "personality": "你是助手，服务器运维站娘。"}, ""
                c.execute(
                    "INSERT INTO persona (id, card, prefix, suffix, updated_at) VALUES (1,?,?,?,?)",
                    (json.dumps(card, ensure_ascii=False), prefix, "", now()))

    # ── 账号 ───────────────────────────────────────────────────────

    def create_account(self, username: str, password: str, role: str, display_name: str = "") -> dict:
        uid = uuid.uuid4().hex
        with self.tx() as c:
            c.execute(
                "INSERT INTO accounts (id, username, password_hash, role, display_name,"
                " quota_tokens_daily, quota_concurrent, quota_disk_mb, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, username, hash_password(password), role, display_name or username,
                 DEFAULT_QUOTA["tokens_daily"], DEFAULT_QUOTA["concurrent"],
                 DEFAULT_QUOTA["disk_mb"], now()))
        self.audit(f"account.create:{username}", "panel", "admin")
        return self.get_account(uid)

    def get_account(self, uid: str) -> dict | None:
        return self.one("SELECT * FROM accounts WHERE id=?", (uid,))

    def get_account_by_name(self, username: str) -> dict | None:
        return self.one("SELECT * FROM accounts WHERE username=?", (username,))

    def list_accounts(self) -> list[dict]:
        # 管理端列表展示的是"今日用量"，先做一次全表跨天归零，
        # 否则管理员看到的也是昨天的数字（2026-10-14 修）
        self.rollover_quota()
        rows = self.query("SELECT * FROM accounts ORDER BY created_at")
        return [strip_sensitive(r) for r in rows]

    def update_account(self, uid: str, fields: dict[str, Any]) -> None:
        self._update_account_fields(uid, fields)
        self.audit(f"account.update:{uid}", "panel", "admin")

    def update_account_quiet(self, uid: str, fields: dict[str, Any]) -> None:
        """不写审计的账号字段更新（监控线程高频调用）。"""
        self._update_account_fields(uid, fields)

    def _update_account_fields(self, uid: str, fields: dict[str, Any]) -> None:
        allowed = {"display_name", "disabled", "quota_tokens_daily", "quota_concurrent",
                   "quota_disk_mb", "must_change_pw", "used_disk_mb", "used_tokens",
                   "used_date", "quota_drive_mb", "used_drive_mb"}
        sets, vals = [], []
        for key, value in fields.items():
            if key in allowed:
                # allowed 里全部是 NOT NULL 列，value=None 会让 UPDATE 抛
                # sqlite3.IntegrityError（2026-10-13 11:42 管理端改账号 500 的
                # 根因）。在 DB 层统一拦截，调用方不必各自过滤。
                if value is None:
                    continue
                sets.append(f"{key}=?")
                vals.append(value)
        if "password" in fields:
            sets.append("password_hash=?")
            vals.append(hash_password(fields["password"]))
            sets.append("must_change_pw=0")
        if not sets:
            return
        vals.append(uid)
        with self.tx() as c:
            c.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)
        # 审计只在 update_account 里写一次：update_account_quiet 按约定不写审计，
        # 此前两边都写导致每条改动产生两条重复审计记录。

    def delete_account(self, uid: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM accounts WHERE id=? AND role!='admin'", (uid,))
        self.audit(f"account.delete:{uid}", "panel", "admin")

    def verify_login(self, username: str, password: str) -> dict | None:
        acc = self.get_account_by_name(username)
        if acc is None or acc["disabled"]:
            return None
        if not verify_password(password, acc["password_hash"]):
            self.audit(f"auth.fail:{username}", username, "?")
            return None
        return acc

    # ── 配额 ───────────────────────────────────────────────────────

    def rollover_quota(self, uid: str | None = None) -> int:
        """每日额度归零：把 used_date 不是「今天」的账号 used_tokens 清成 0。

        为什么必须是独立方法（2026-10-14 修）：跨天归零原来只写在 check_quota() 里，
        而 check_quota() 只在**开始新一轮对话**时才被调用；所有展示路径
        （/api/auth/me 的 quota、管理端账号列表）都直接读 accounts.used_tokens 原始值，
        于是用户次日打开面板看到的仍是昨天（甚至更早）的"今日用量"，
        表现为「词元额度不会每日刷新」——昨天用满的账号第二天还会一直显示"已用完"。

        uid=None 表示扫描全部账号（管理端列表与定时兜底用）。返回实际归零的账号数。
        """
        d = today()
        with self.tx() as c:
            if uid is None:
                cur = c.execute(
                    "UPDATE accounts SET used_date=?, used_tokens=0 WHERE used_date!=?", (d, d))
            else:
                cur = c.execute(
                    "UPDATE accounts SET used_date=?, used_tokens=0 WHERE id=? AND used_date!=?",
                    (d, uid, d))
            return cur.rowcount

    def get_account_fresh(self, uid: str) -> dict | None:
        """取账号前先做跨天归零——凡是要把 used_tokens 展示给人看的地方都用它。"""
        self.rollover_quota(uid)
        return self.get_account(uid)

    def check_quota(self, uid: str, est_tokens: int) -> tuple[bool, str]:
        acc = self.get_account(uid)
        if acc is None:
            return False, "账号不存在"
        if acc["disabled"]:
            return False, "账号已禁用"
        stale = acc["used_date"] != today()
        if stale:
            # 跨天首用：先归零（与展示路径共用同一段逻辑，避免两处口径漂移）
            self.rollover_quota(uid)
        used = 0 if stale else acc["used_tokens"]
        if used >= acc["quota_tokens_daily"]:
            return False, f"今日 token 配额已用完（{acc['quota_tokens_daily']}）"
        if used + est_tokens > acc["quota_tokens_daily"]:
            return False, "本次请求超出剩余配额"
        running = self.one(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id=? AND status='running'",
            (uid,))["n"]
        if running >= acc["quota_concurrent"]:
            return False, f"并发会话已达上限（{acc['quota_concurrent']}）"
        if acc["used_disk_mb"] >= acc["quota_disk_mb"]:
            return False, f"磁盘配额已用完（{acc['quota_disk_mb']} MB）"
        return True, ""

    def add_tokens(self, uid: str, tokens: int) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE accounts SET used_tokens = used_tokens + ? WHERE id=? "
                "AND used_date=?", (max(0, tokens), uid, today()))
            # 跨天首用时重置再累计
            c.execute(
                "UPDATE accounts SET used_date=?, used_tokens=? WHERE id=? AND used_date!=?",
                (today(), max(0, tokens), uid, today()))

    # ── 审计 ───────────────────────────────────────────────────────

    def audit(self, action: str, actor: str, actor_role: str, detail: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO audit_log (ts, actor, actor_role, action, detail) VALUES (?,?,?,?,?)",
                (now(), actor, actor_role, action, detail[:4000]))

    def list_audit(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # ── 告警 ───────────────────────────────────────────────────────

    def raise_alert(self, level: str, source: str, title: str, detail: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO alerts (ts, level, source, title, detail) VALUES (?,?,?,?,?)",
                (now(), level, source, title, detail[:4000]))

    def list_alerts(self, unack_only: bool = False, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM alerts"
        if unack_only:
            sql += " WHERE acknowledged=0"
        return self.query(sql + " ORDER BY id DESC LIMIT ?", (limit,))

    def ack_alerts(self, ids: list[int]) -> None:
        with self.tx() as c:
            c.executemany("UPDATE alerts SET acknowledged=1 WHERE id=?", [(i,) for i in ids])

    def list_rules(self) -> list[dict]:
        return self.query("SELECT * FROM alert_rules ORDER BY id")

    def update_rule(self, rid: str, fields: dict[str, Any]) -> None:
        allowed = {"op", "threshold", "duration_sec", "enabled", "notify_email"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        if not sets:
            return
        with self.tx() as c:
            c.execute(f"UPDATE alert_rules SET {', '.join(sets)} WHERE id=?",
                      [*fields.values(), rid])

    def mark_rule_fired(self, rid: str, ts: float) -> None:
        with self.tx() as c:
            c.execute("UPDATE alert_rules SET last_fired=? WHERE id=?", (ts, rid))

    # ── 任务 ───────────────────────────────────────────────────────

    def create_task(self, user: dict, title: str, prompt: str, kind: str = "chat",
                    assignee: str = "") -> dict:
        tid = uuid.uuid4().hex[:16]
        with self.tx() as c:
            c.execute(
                "INSERT INTO tasks (id, ts, user_id, user_name, kind, title, prompt, assignee)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (tid, now(), user["id"], user["username"], kind, title, prompt, assignee))
        return self.get_task(tid)

    def get_task(self, tid: str) -> dict | None:
        return self.one("SELECT * FROM tasks WHERE id=?", (tid,))

    def list_tasks(self, user_id: str | None = None, limit: int = 200) -> list[dict]:
        if user_id:
            return self.query(
                "SELECT * FROM tasks WHERE user_id=? ORDER BY ts DESC LIMIT ?", (user_id, limit))
        return self.query("SELECT * FROM tasks ORDER BY ts DESC LIMIT ?", (limit,))

    def set_task_status(self, tid: str, status: str, result: str | None = None,
                        tokens: int | None = None) -> None:
        with self.tx() as c:
            if result is not None:
                c.execute("UPDATE tasks SET status=?, result=?, finished_at=? WHERE id=?",
                          (status, result[:100_000], now(), tid))
            else:
                c.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
            if tokens is not None:
                c.execute("UPDATE tasks SET tokens_used=? WHERE id=?", (tokens, tid))

    def claim_task(self, tid: str, claimer: str) -> dict | None:
        """原子认领：pending → running；抢不到返回 None。"""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE tasks SET status='running' WHERE id=? AND status='pending'",
                (tid,))
            if cur.rowcount != 1:
                return None
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(row)

    def complete_task(self, tid: str, claimer: str, result: str, tokens: int) -> dict | None:
        """完成任务并计量 token（写回任务与账号配额）。"""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE tasks SET status='done', result=?, tokens_used=?, finished_at=?"
                " WHERE id=? AND status='running'", (result[:100_000], tokens, now(), tid))
            if cur.rowcount != 1:
                return None
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        task = dict(row)
        if task["tokens_used"] > 0:
            self.add_tokens(task["user_id"], task["tokens_used"])
        return task

    # ── workers ────────────────────────────────────────────────────

    def register_worker(self, name: str, capabilities: list[str], host: str) -> tuple[dict, str]:
        """注册或重注册 worker，返回 (worker, token)。已批准的同名 worker 重注册保持批准态并换发 token。"""
        wid = uuid.uuid4().hex[:12]
        token = os.urandom(24).hex()
        with self.tx() as c:
            old = c.execute("SELECT * FROM workers WHERE name=?", (name,)).fetchone()
            if old:
                c.execute(
                    "UPDATE workers SET token_hash=?, capabilities=?, host=?, last_seen=? WHERE id=?",
                    (sha256(token.encode()).hexdigest(), json.dumps(capabilities), host, now(), old["id"]))
                wid = old["id"]
            else:
                c.execute(
                    "INSERT INTO workers (id, name, token_hash, capabilities, approved, registered_at, host)"
                    " VALUES (?,?,?,?,0,?,?)",
                    (wid, name, sha256(token.encode()).hexdigest(),
                     json.dumps(capabilities), now(), host))
        self.audit(f"worker.register:{name}", name, "worker")
        return (self.one("SELECT * FROM workers WHERE id=?", (wid,)), token)

    def admin_add_worker(self, name: str, capabilities: list[str], user_available: bool) -> str:
        """管理面板直接添加：生成 token，自动批准。返回明文 token（仅此一次）。"""
        wid = uuid.uuid4().hex[:12]
        token = os.urandom(24).hex()
        with self.tx() as c:
            c.execute(
                "INSERT INTO workers (id, name, token_hash, capabilities, user_available, approved,"
                " status, registered_at) VALUES (?,?,?,?,?,1,'offline',?)",
                (wid, name, sha256(token.encode()).hexdigest(),
                 json.dumps(capabilities), int(user_available), now()))
        self.audit(f"worker.create:{name}", "panel", "admin")
        return token

    def verify_worker(self, name: str, token: str) -> dict | None:
        w = self.one("SELECT * FROM workers WHERE name=?", (name,))
        if w is None:
            return None
        if w["token_hash"] != sha256(token.encode()).hexdigest():
            return None
        with self.tx() as c:
            c.execute("UPDATE workers SET status='online', last_seen=? WHERE id=?", (now(), w["id"]))
        return w

    def list_workers(self) -> list[dict]:
        rows = self.query("SELECT * FROM workers ORDER BY registered_at")
        return [strip_sensitive(r) for r in rows]

    def update_worker(self, wid: str, fields: dict[str, Any]) -> None:
        allowed = {"user_available", "approved", "status"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        if not sets:
            return
        with self.tx() as c:
            c.execute(f"UPDATE workers SET {', '.join(sets)} WHERE id=?", [*fields.values(), wid])
        self.audit(f"worker.update:{wid}", "panel", "admin", json.dumps(fields, ensure_ascii=False))

    def delete_worker(self, wid: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM workers WHERE id=?", (wid,))
        self.audit(f"worker.delete:{wid}", "panel", "admin")

    # ── persona ────────────────────────────────────────────────────

    def get_persona(self) -> dict:
        row = self.one("SELECT * FROM persona WHERE id=1") or {}
        row["card"] = json.loads(row.get("card") or "{}")
        return row

    def set_persona(self, card: dict, prefix: str, suffix: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO persona (id, card, prefix, suffix, updated_at) VALUES (1,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET card=excluded.card, prefix=excluded.prefix,"
                " suffix=excluded.suffix, updated_at=excluded.updated_at",
                (json.dumps(card, ensure_ascii=False), prefix, suffix, now()))
        self.audit("persona.update", "panel", "admin")

    # ── 设置 ───────────────────────────────────────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ── 监控历史 ───────────────────────────────────────────────────

    # ── 站娘记忆与对话历史 ──────────────────────────────────────────

    def get_memory(self, user_id: str) -> dict:
        row = self.one("SELECT * FROM memories WHERE user_id=?", (user_id,))
        if row is None:
            return {"summary": "", "recent": [], "chat_session_id": ""}
        try:
            row["recent"] = json.loads(row["recent"] or "[]")
        except json.JSONDecodeError:
            row["recent"] = []
        return row

    def set_memory(self, user_id: str, summary: str, recent: list) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO memories (user_id, summary, recent, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET summary=excluded.summary,"
                " recent=excluded.recent, updated_at=excluded.updated_at",
                (user_id, summary[:8000], json.dumps(recent[-60:], ensure_ascii=False), now()))

    def set_chat_session(self, user_id: str, session_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO memories (user_id, chat_session_id, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET chat_session_id=excluded.chat_session_id,"
                " updated_at=excluded.updated_at",
                (user_id, session_id, now()))

    def append_chat(self, user_id: str, role: str, text: str, channel: str = "chat") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO chat_messages (user_id, role, text, channel, ts) VALUES (?,?,?,?,?)",
                (user_id, role, text[:50_000], channel, now()))

    def list_chat(self, user_id: str, channel: str = "chat", limit: int = 200) -> list[dict]:
        rows = self.query(
            "SELECT role, text, ts FROM chat_messages WHERE user_id=? AND channel=?"
            " ORDER BY id DESC LIMIT ?", (user_id, channel, limit))
        rows.reverse()
        return rows

    def clear_chat(self, user_id: str, channel: str = "chat") -> None:
        with self.tx() as c:
            c.execute("DELETE FROM chat_messages WHERE user_id=? AND channel=?", (user_id, channel))
            c.execute(
                "INSERT INTO memories (user_id, chat_session_id, updated_at) VALUES (?,'',?)"
                " ON CONFLICT(user_id) DO UPDATE SET chat_session_id='', updated_at=excluded.updated_at",
                (user_id, now()))

    # ── sessions（会话即任务模型） ───────────────────────────────

    def create_session(self, user: dict, title: str = "") -> dict:
        sid = uuid.uuid4().hex[:16]
        with self.tx() as c:
            c.execute(
                "INSERT INTO sessions (id, user_id, title, created_at, updated_at)"
                " VALUES (?,?,?,?,?)",
                (sid, user["id"], title, now(), now()))
        return self.get_session(sid)

    def get_session(self, sid: str) -> dict | None:
        return self.one("SELECT * FROM sessions WHERE id=?", (sid,))

    def list_sessions(self, user_id: str, limit: int = 200) -> list[dict]:
        return self.query(
            "SELECT * FROM sessions WHERE user_id=?"
            " ORDER BY pinned DESC, updated_at DESC LIMIT ?", (user_id, limit))

    def rename_session(self, sid: str, title: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                      (title[:120], now(), sid))

    def delete_session(self, sid: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM chat_messages WHERE session_id=?", (sid,))
            c.execute("DELETE FROM sessions WHERE id=?", (sid,))

    def toggle_pin_session(self, sid: str, pinned: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE sessions SET pinned=?, updated_at=? WHERE id=?",
                      (1 if pinned else 0, now(), sid))

    def set_session_status(self, sid: str, status: str,
                           dsh_session_id: str | None = None) -> None:
        sets = ["status=?", "updated_at=?"]
        vals: list[Any] = [status, now()]
        if dsh_session_id is not None:
            sets.append("dsh_session_id=?")
            vals.append(dsh_session_id)
        vals.append(sid)
        with self.tx() as c:
            c.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", vals)

    def touch_session(self, sid: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), sid))

    def set_session_title_if_default(self, sid: str, title: str) -> None:
        """首条消息后自动取标题：仅当当前标题为空时设置。"""
        with self.tx() as c:
            c.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=? AND (title='' OR title IS NULL)",
                (title[:80], now(), sid))

    def append_chat_msg(self, session_id: str, user_id: str, role: str, text: str,
                        channel: str = "chat") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO chat_messages (user_id, role, text, channel, session_id, ts)"
                " VALUES (?,?,?,?,?,?)",
                (user_id, role, text[:50_000], channel, session_id, now()))
            c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), session_id))

    def list_session_messages(self, session_id: str, limit: int = 500) -> list[dict]:
        rows = self.query(
            "SELECT role, text, ts FROM chat_messages WHERE session_id=?"
            " ORDER BY id DESC LIMIT ?", (session_id, limit))
        rows.reverse()
        return rows

    def add_session_tokens(self, sid: str, tokens: int) -> None:
        """累计会话 token 用量（仅展示用，账号配额仍走 add_tokens）。"""
        with self.tx() as c:
            c.execute(
                "UPDATE sessions SET tokens_used = tokens_used + ? WHERE id=?",
                (max(0, tokens), sid))

    def list_running_sessions(self, older_than_sec: float) -> list[dict]:
        """启动时守护复位：running 状态超过指定秒数的会话（用于 SSE 中断残留）。"""
        cutoff = now() - older_than_sec
        return self.query(
            "SELECT * FROM sessions WHERE status='running' AND updated_at < ?", (cutoff,))

    def bb_read(self, key: str) -> str | None:
        row = self.one("SELECT value FROM blackboard WHERE key=?", (key,))
        return row["value"] if row else None

    def bb_write(self, key: str, value: str, by: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO blackboard (key, value, updated_at, updated_by) VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (key, value[:100_000], now(), by))

    def bb_list(self) -> list[dict]:
        return self.query("SELECT key, updated_at, updated_by, length(value) AS size FROM blackboard ORDER BY key")

    def save_sample(self, sample: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO monitor_samples (ts, cpu, mem, disk, net_rx, net_tx, load1)"
                " VALUES (?,?,?,?,?,?,?)",
                (sample["ts"], sample["cpu"], sample["mem"], sample["disk"],
                 sample["net_rx"], sample["net_tx"], sample["load1"]))
            c.execute("DELETE FROM monitor_samples WHERE ts < ?", (sample["ts"] - 8 * 86400,))

    def history(self, hours: int = 24) -> list[dict]:
        since = int(now() - hours * 3600)
        return self.query(
            "SELECT * FROM monitor_samples WHERE ts >= ? ORDER BY ts", (since,))

    # ── agents（Hub-Adapter 枢纽模式） ──────────────────────────────

    def register_agent(self, name: str, agent_type: str, capabilities: list,
                       tools: list, host: str) -> tuple[dict, str]:
        """注册或重注册 Agent 适配器，返回 (agent, token)。已批准的同名重注册保持批准态。"""
        aid = uuid.uuid4().hex[:12]
        token = os.urandom(24).hex()
        caps_json = json.dumps(capabilities, ensure_ascii=False)
        tools_json = json.dumps(tools, ensure_ascii=False)
        with self.tx() as c:
            old = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
            if old:
                c.execute(
                    "UPDATE agents SET token_hash=?, agent_type=?, capabilities=?, tools=?,"
                    " host=?, status='online', last_seen=? WHERE id=?",
                    (sha256(token.encode()).hexdigest(), agent_type, caps_json, tools_json,
                     host, now(), old["id"]))
                aid = old["id"]
            else:
                c.execute(
                    "INSERT INTO agents (id, name, token_hash, agent_type, capabilities, tools,"
                    " host, approved, status, last_seen, registered_at) VALUES (?,?,?,?,?,"
                    "?,?,0,'online',?,?)",
                    (aid, name, sha256(token.encode()).hexdigest(), agent_type, caps_json,
                     tools_json, host, now(), now()))
        self.audit(f"agent.register:{name}", name, "agent")
        return (self.one("SELECT * FROM agents WHERE id=?", (aid,)), token)

    def admin_add_agent(self, name: str, agent_type: str, capabilities: list,
                        tools: list) -> str:
        """管理面板直接添加 Agent：自动批准，返回明文 token。"""
        aid = uuid.uuid4().hex[:12]
        token = os.urandom(24).hex()
        with self.tx() as c:
            c.execute(
                "INSERT INTO agents (id, name, token_hash, agent_type, capabilities, tools,"
                " approved, status, registered_at) VALUES (?,?,?,?,?,?,1,'offline',?)",
                (aid, name, sha256(token.encode()).hexdigest(), agent_type,
                 json.dumps(capabilities, ensure_ascii=False),
                 json.dumps(tools, ensure_ascii=False), now()))
        self.audit(f"agent.create:{name}", "panel", "admin")
        return token

    def verify_agent(self, name: str, token: str) -> dict | None:
        a = self.one("SELECT * FROM agents WHERE name=?", (name,))
        if a is None:
            return None
        if a["token_hash"] != sha256(token.encode()).hexdigest():
            return None
        with self.tx() as c:
            c.execute("UPDATE agents SET status='online', last_seen=? WHERE id=?", (now(), a["id"]))
        return a

    def list_agents(self) -> list[dict]:
        rows = self.query("SELECT * FROM agents ORDER BY registered_at")
        return [strip_sensitive(r) for r in rows]

    def update_agent(self, aid: str, fields: dict[str, Any]) -> None:
        allowed = {"approved", "status", "agent_type"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        if not sets:
            return
        with self.tx() as c:
            c.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id=?", [*fields.values(), aid])
        self.audit(f"agent.update:{aid}", "panel", "admin", json.dumps(fields, ensure_ascii=False))

    def delete_agent(self, aid: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM agents WHERE id=?", (aid,))
        self.audit(f"agent.delete:{aid}", "panel", "admin")

    def online_agents_with_capability(self, capability: str) -> list[dict]:
        """查找在线且已批准、具备某能力类型的 Agent。

        在线判定以 last_seen（90s 心跳）为准，**不信 status 列**：status 只在心跳时
        被写成 online、从不回落，直接查它会选出早就不在的机器（2026-10-14 修）。
        """
        rows = self.query("SELECT * FROM agents WHERE approved=1")
        ts = now()
        result = []
        for r in rows:
            if ts - (r["last_seen"] or 0) > 90:
                continue
            caps = json.loads(r.get("capabilities") or "[]")
            if any(isinstance(c, dict) and c.get("type") == capability
                   or isinstance(c, str) and c == capability for c in caps):
                result.append(r)
        return result

    # ── 智能体链接：浏览器授权配对 + 用户自有队员（助手 2026-10-14） ──
    #
    # 动机：原来「把 AI 客户端接进铃枢」靠管理员手工发一份全站共享密钥，
    # 既拦住了普通用户，又让一次泄露影响所有人。改成设备码授权（OAuth
    # device flow 同款）：连接器出示 device_code，用户在自己面板输入 user_code
    # 确认，连接器再凭 device_code 换取**只属于自己账号**的长期凭据。

    PAIR_TTL = 900        # 配对码有效期（秒）
    PAIR_INTERVAL = 3     # 建议连接器轮询间隔（秒）

    @staticmethod
    def _user_code() -> str:
        """人读友好码：8 位，去掉 0/O/1/I 等易混字符。"""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(alphabet[b % len(alphabet)] for b in os.urandom(8))

    def create_pair_code(self, agent_name: str = "", host: str = "",
                         platform: str = "", capabilities: list | None = None) -> dict:
        """连接器发起配对：device_code 给它自己轮询，user_code 给人看。"""
        dc, uc, ts = os.urandom(24).hex(), self._user_code(), now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO pair_codes (device_code, user_code, status, agent_name, host,"
                " platform, capabilities, created_at, expires_at)"
                " VALUES (?,?,'pending',?,?,?,?,?,?)",
                (dc, uc, agent_name[:32], host[:64], platform[:32],
                 json.dumps(capabilities or [], ensure_ascii=False), ts, ts + self.PAIR_TTL))
        return {"device_code": dc, "user_code": uc, "expires_at": ts + self.PAIR_TTL,
                "interval": self.PAIR_INTERVAL}

    def get_pair_by_device_code(self, dc: str) -> dict | None:
        return self.one("SELECT * FROM pair_codes WHERE device_code=?", (dc,))

    def get_pair_by_user_code(self, uc: str) -> dict | None:
        return self.one(
            "SELECT * FROM pair_codes WHERE user_code=? AND status IN ('pending','approved')"
            " ORDER BY created_at DESC LIMIT 1", (uc.strip().upper(),))

    def approve_pair(self, user_code: str, user_id: str) -> dict | None:
        """用户在面板确认：把这次配对绑定到自己的账号。"""
        uc = user_code.strip().upper()
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM pair_codes WHERE user_code=? AND status='pending'"
                " AND expires_at>? ORDER BY created_at DESC LIMIT 1", (uc, now())).fetchone()
            if row is None:
                return None
            c.execute("UPDATE pair_codes SET status='approved', user_id=?, approved_at=?"
                      " WHERE device_code=?", (user_id, now(), row["device_code"]))
        self.audit("pair.approve", user_id, "user", uc)
        return self.get_pair_by_device_code(row["device_code"])

    def deny_pair(self, user_code: str, user_id: str) -> bool:
        uc = user_code.strip().upper()
        with self.tx() as c:
            cur = c.execute("UPDATE pair_codes SET status='denied'"
                            " WHERE user_code=? AND status='pending'", (uc,))
            if cur.rowcount == 0:
                return False
        self.audit("pair.deny", user_id, "user", uc)
        return True

    def consume_pair(self, device_code: str) -> tuple[dict, str, str] | None:
        """连接器凭 device_code 换取长期凭据，返回 (agent, token, username)。

        同一用户重复配对 = 轮换 token（而不是留下同名僵尸队员）。
        名字全局唯一，撞名时自动加短后缀，避免两个用户的同名机器互相顶掉。
        """
        with self.tx() as c:
            row = c.execute("SELECT * FROM pair_codes WHERE device_code=?",
                            (device_code,)).fetchone()
            if row is None or row["status"] != "approved" or row["expires_at"] < now():
                return None
            user = c.execute("SELECT * FROM accounts WHERE id=?", (row["user_id"],)).fetchone()
            if user is None:
                return None
            desired = (row["agent_name"] or "connector")[:32]
            token, th, ts = os.urandom(24).hex(), None, now()
            th = sha256(token.encode()).hexdigest()
            mine = c.execute("SELECT id FROM agents WHERE user_id=? AND name=?",
                             (row["user_id"], desired)).fetchone()
            if mine:
                aid, name = mine["id"], desired
                c.execute(
                    "UPDATE agents SET token_hash=?, agent_type='connector', capabilities=?,"
                    " host=?, platform=?, approved=1, status='online', last_seen=? WHERE id=?",
                    (th, row["capabilities"], row["host"], row["platform"], ts, aid))
            else:
                clash = c.execute("SELECT id FROM agents WHERE name=?", (desired,)).fetchone()
                if clash:  # 别的用户占了同名 → 加短后缀
                    desired = f"{desired[:27]}-{os.urandom(2).hex()}"
                aid, name = uuid.uuid4().hex[:12], desired
                c.execute(
                    "INSERT INTO agents (id, name, token_hash, agent_type, capabilities, tools,"
                    " host, platform, approved, status, last_seen, registered_at, user_id)"
                    " VALUES (?,?,?,'connector',?,'[]',?,?,1,'online',?,?,?)",
                    (aid, name, th, row["capabilities"], row["host"], row["platform"],
                     ts, ts, row["user_id"]))
            c.execute("UPDATE pair_codes SET status='consumed', agent_id=?, consumed_at=?"
                      " WHERE device_code=?", (aid, ts, device_code))
        agent = self.one("SELECT * FROM agents WHERE id=?", (aid,))
        self.audit(f"pair.consume:{name}", user["username"], "user", row["host"])
        return agent, token, user["username"]

    def list_user_agents(self, user_id: str) -> list[dict]:
        return self.query("SELECT * FROM agents WHERE user_id=? ORDER BY registered_at",
                          (user_id,))

    def pick_user_agent(self, user_id: str, capability: str = "",
                        online_within: float = 90.0) -> dict | None:
        """挑一个该用户**在线**、已批准、能力匹配的队员（心跳最新的优先）。

        面板「派活」与站娘 `dispatch_to_agent` 共用：先按能力筛，再按新鲜度排序，
        这样同一个能力有多个队员时会自然落在最近还活着的那台机器上。
        """
        rows = self.query("SELECT * FROM agents WHERE user_id=? AND approved=1", (user_id,))
        ts = now()
        best: dict | None = None
        for a in rows:
            if ts - (a["last_seen"] or 0) > online_within:
                continue
            caps = json.loads(a["capabilities"] or "[]")
            tags = {c.get("type") if isinstance(c, dict) else c for c in caps}
            if capability and capability not in tags:
                continue
            if best is None or (a["last_seen"] or 0) > (best["last_seen"] or 0):
                best = a
        return strip_sensitive(best) if best else None

    def get_user_agent(self, user_id: str, aid: str) -> dict | None:
        a = self.one("SELECT * FROM agents WHERE id=? AND user_id=?", (aid, user_id))
        return strip_sensitive(a) if a else None

    def delete_user_agent(self, user_id: str, aid: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM agents WHERE id=? AND user_id=?", (aid, user_id))
            if cur.rowcount == 0:
                return False
        self.audit(f"agent.revoke:{aid}", user_id, "user")
        return True

    def list_user_subtasks(self, user_id: str, limit: int = 20) -> list[dict]:
        """某用户名下队员最近的子任务 —— 队伍视图用（只看自己的队员，天然隔离）。"""
        names = [a["name"] for a in self.list_user_agents(user_id)]
        if not names:
            return []
        marks = ",".join("?" * len(names))
        return self.query(
            f"SELECT * FROM subtasks WHERE assignee IN ({marks})"
            f" ORDER BY created_at DESC LIMIT ?", (*names, max(1, min(limit, 100))))

    def reset_subtask(self, sid: str, assignee: str | None = None,
                      capability: str | None = None) -> dict | None:
        """把失败/卡住的子任务退回 pending，可换队员或换能力标签后重试（站娘派活用）。"""
        if self.get_subtask(sid) is None:
            return None
        sets = ["status='pending'", "result=''", "finished_at=NULL"]
        params: list = []
        if assignee is not None:
            sets.append("assignee=?")
            params.append(assignee)
        if capability is not None:
            sets.append("capability=?")
            params.append(capability)
        params.append(sid)
        with self.tx() as c:
            c.execute(f"UPDATE subtasks SET {', '.join(sets)} WHERE id=?", params)
        self.audit(f"subtask.reset:{sid}", "助手", "agent", assignee or "")
        return self.get_subtask(sid)

    # ── 长期运行的家务（助手 2026-10-14）──────────────────────────

    def purge_pair_codes(self, grace_sec: float = 3600.0) -> int:
        """清掉"过期很久"的配对码记录，返回删除条数。

        配对码只在 15 分钟内有用；用过/被拒/过期的记录留着只会让表越长越大。
        宽限期（默认 1 小时）是为了让排障时还能看到刚刚发生过什么。
        """
        cutoff = now() - grace_sec
        with self.tx() as c:
            cur = c.execute("DELETE FROM pair_codes WHERE expires_at < ?", (cutoff,))
        removed = cur.rowcount or 0
        if removed:
            self.audit("pair.purge", "system", "agent", f"removed={removed}")
        return removed

    def reap_stale_subtasks(self, hours: float | None = None) -> int:
        """把"领了活但超过 N 小时没回报"的子任务标为失败，返回处理条数。

        这类僵尸任务的典型来源：连接器领活后电脑休眠/被关机/被撤销凭据。
        不收尸的话它会永远停在 running，用户看到的"进行中"是假的，
        而且再也不会有人重试它。回收后 assignee 保留（便于看出是谁没回报），
        站娘可用 dispatch_retry / subtask_reassign 重新派给别的队员。

        阈值可配置：settings 表里的 `subtask_reap_hours`（默认 2 小时）。
        真正会跑很久的活应该把它调大，否则会把还在干活的队员误判成僵尸。
        """
        if hours is None:
            try:
                hours = float(self.get_setting("subtask_reap_hours", "2") or 2)
            except (TypeError, ValueError):
                hours = 2.0
        cutoff = now() - hours * 3600
        with self.tx() as c:
            cur = c.execute(
                "UPDATE subtasks SET status='failed', result=?, finished_at=?"
                " WHERE status='running' AND started_at > 0 AND started_at < ?",
                (f"超时未回报（超过 {hours:g} 小时）——连接器可能已离线/休眠；可重新派活",
                 now(), cutoff))
        reaped = cur.rowcount or 0
        if reaped:
            self.audit("subtask.reap", "system", "agent", f"reaped={reaped}")
        return reaped

    def verify_agent_token(self, token: str) -> dict | None:
        """按 token 反查 Agent —— MCP 面「每用户独立凭据」鉴权靠它，无需知道名字。"""
        if not token:
            return None
        a = self.one("SELECT * FROM agents WHERE token_hash=?",
                     (sha256(token.encode()).hexdigest(),))
        if a is None:
            return None
        with self.tx() as c:
            c.execute("UPDATE agents SET status='online', last_seen=? WHERE id=?",
                      (now(), a["id"]))
        return a

    def touch_agent(self, aid: str, ping: bool = False) -> None:
        """ping=True 只记「探测过」，**绝不刷新 last_seen**。

        last_seen 是心跳的专属事实：面板点一下「测试连接」就把它改成 now()，
        会把离线队员伪造成在线（名册的 90s 在线窗口与 pick_user_agent 都据此判定），
        于是它立刻变得「可派活」，而实际上那台机器上没有任何进程在跑（2026-10-14 修）。
        """
        with self.tx() as c:
            if ping:
                c.execute("UPDATE agents SET last_ping_at=? WHERE id=?", (now(), aid))
            else:
                c.execute("UPDATE agents SET status='online', last_seen=? WHERE id=?",
                          (now(), aid))

    # ── subtasks（站娘任务拆解后的子任务 DAG） ─────────────────────

    def create_subtask(self, parent_id: str, step: int, title: str, prompt: str,
                       capability: str, assignee: str = "",
                       depends_on: str = "") -> dict:
        sid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO subtasks (id, parent_task_id, step, title, prompt, assignee,"
                " capability, status, depends_on, created_at) VALUES (?,?,?,?,?,?,?,"
                " 'pending', ?, ?)",
                (sid, parent_id, step, title, prompt, assignee, capability, depends_on, now()))
        return self.one("SELECT * FROM subtasks WHERE id=?", (sid,))

    def list_subtasks(self, parent_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM subtasks WHERE parent_task_id=? ORDER BY step", (parent_id,))

    def get_subtask(self, sid: str) -> dict | None:
        return self.one("SELECT * FROM subtasks WHERE id=?", (sid,))

    def claim_subtask(self, sid: str, claimer: str) -> dict | None:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE subtasks SET status='running', assignee=?, started_at=?"
                " WHERE id=? AND status='pending'",
                (claimer, now(), sid))
            if cur.rowcount != 1:
                return None
            row = c.execute("SELECT * FROM subtasks WHERE id=?", (sid,)).fetchone()
        return dict(row)

    def complete_subtask(self, sid: str, result: str, tokens: int = 0) -> dict | None:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE subtasks SET status='done', result=?, tokens_used=?, finished_at=?"
                " WHERE id=? AND status='running'",
                (result[:50_000], tokens, now(), sid))
            if cur.rowcount != 1:
                return None
            row = c.execute("SELECT * FROM subtasks WHERE id=?", (sid,)).fetchone()
        return dict(row)

    def fail_subtask(self, sid: str, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET status='failed', result=?, finished_at=? WHERE id=?",
                (reason[:2000], now(), sid))

    def pending_subtasks(self, agent_name: str | None = None) -> list[dict]:
        """可认领的子任务：无依赖或依赖已 done 的。"""
        rows = self.query(
            "SELECT * FROM subtasks WHERE status='pending' ORDER BY step")
        result = []
        for r in rows:
            deps = [d.strip() for d in (r.get("depends_on") or "").split(",") if d.strip()]
            if not deps:
                result.append(r)
                continue
            done = all(
                self.one("SELECT status FROM subtasks WHERE id=?", (d,))["status"] == "done"
                for d in deps if d)
            if done:
                result.append(r)
        if agent_name:
            result = [r for r in result if not r["assignee"] or r["assignee"] == agent_name]
        return result

    def all_subtasks_done(self, parent_id: str) -> bool:
        rows = self.query(
            "SELECT status FROM subtasks WHERE parent_task_id=?", (parent_id,))
        return bool(rows) and all(r["status"] == "done" for r in rows)

    def aggregate_subtask_results(self, parent_id: str) -> str:
        rows = self.query(
            "SELECT step, title, result FROM subtasks WHERE parent_task_id=?"
            " ORDER BY step", (parent_id,))
        parts = []
        for r in rows:
            parts.append(f"【步骤{r['step']}】{r['title']}\n{r['result']}")
        return "\n\n".join(parts)

    # ── agent_messages（Agent 间消息中转） ─────────────────────────

    def send_agent_message(self, task_id: str, from_agent: str, to_agent: str,
                           text: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO agent_messages (ts, task_id, from_agent, to_agent, text, delivered)"
                " VALUES (?,?,?,?,?,0)", (now(), task_id, from_agent, to_agent, text[:50_000]))
            return cur.lastrowid

    def poll_agent_messages(self, agent_name: str) -> list[dict]:
        rows = self.query(
            "SELECT * FROM agent_messages WHERE to_agent=? AND delivered=0"
            " ORDER BY id LIMIT 20", (agent_name,))
        if rows:
            ids = [str(r["id"]) for r in rows]
            with self.tx() as c:
                c.execute(
                    f"UPDATE agent_messages SET delivered=1 WHERE id IN ({','.join(ids)})")
        return rows

    def list_agent_messages(self, task_id: str = "") -> list[dict]:
        if task_id:
            return self.query(
                "SELECT * FROM agent_messages WHERE task_id=? ORDER BY id", (task_id,))
        return self.query("SELECT * FROM agent_messages ORDER BY id DESC LIMIT 200")

    # ── 用户设备（我的设备） ─────────────────────────────────────

    def create_device(self, user_id: str, name: str, desc: str) -> tuple[dict, str]:
        """用户添加自有设备，返回 (device, token)。token 仅此一次明文展示。"""
        did = uuid.uuid4().hex[:12]
        token = os.urandom(24).hex()
        with self.tx() as c:
            c.execute(
                "INSERT INTO devices (id, user_id, name, desc, token_hash, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (did, user_id, name, desc, sha256(token.encode()).hexdigest(), now()))
        self.audit(f"device.create:{did}", user_id, "user", name)
        return self.get_device(did), token

    def get_device(self, did: str) -> dict | None:
        d = self.one("SELECT * FROM devices WHERE id=?", (did,))
        return strip_sensitive(d) if d else None

    def list_devices(self, user_id: str) -> list[dict]:
        rows = self.query(
            "SELECT * FROM devices WHERE user_id=? ORDER BY created_at", (user_id,))
        return [strip_sensitive(r) for r in rows]

    def device_online(self, did: str, host: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE devices SET status='online', last_seen=?, host=? WHERE id=?",
                      (now(), host, did))

    def delete_device(self, did: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM devices WHERE id=?", (did,))
            c.execute("DELETE FROM device_commands WHERE device_id=?", (did,))
        self.audit(f"device.delete:{did}", "panel", "user", "")

    def verify_device(self, name: str, token: str) -> dict | None:
        """设备客户端认证（name + token）。在线状态由 heartbeat 路由刷新。"""
        d = self.one("SELECT * FROM devices WHERE name=?", (name,))
        if d is None or d["token_hash"] != sha256(token.encode()).hexdigest():
            return None
        return d

    def queue_device_command(self, did: str, text: str, source: str = "user") -> dict:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO device_commands (device_id, text, source, created_at)"
                " VALUES (?,?,?,?)", (did, text, source, now()))
            cid = cur.lastrowid
        self.audit(f"device.cmd:{cid}", source, "user", text[:100])
        return self.one("SELECT * FROM device_commands WHERE id=?", (cid,))

    def take_device_commands(self, did: str, limit: int = 5) -> list[dict]:
        """设备心跳时取走 pending 指令（pending→sent）。

        ⚠ SELECT 与 UPDATE 必须在**同一个事务**里完成：设备端可能同时跑着多个
        agent 实例（2026-10-13 实测：用户重跑 agent 时旧实例没停，心跳从
        4 次/分变成 8 次/分），分两步做会让两个实例各自 SELECT 到同一条 pending
        指令、各执行一次 —— handler 有副作用时等于重复执行。
        """
        with self.tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM device_commands WHERE device_id=? AND status='pending'"
                " ORDER BY id LIMIT ?", (did, limit)).fetchall()]
            if rows:
                ids = [str(r["id"]) for r in rows]
                c.execute(f"UPDATE device_commands SET status='sent'"
                          f" WHERE id IN ({','.join(ids)})")
        return rows

    def finish_device_command(self, cid: str, status: str, result: str) -> dict | None:
        try:
            cid_int = int(cid)
        except (TypeError, ValueError):
            return None
        with self.tx() as c:
            c.execute(
                "UPDATE device_commands SET status=?, result=?, finished_at=? WHERE id=?"
                " AND status='sent'", (status, result[:20_000], now(), cid_int))
        return self.one("SELECT * FROM device_commands WHERE id=?", (cid_int,))

    def list_device_commands(self, did: str, limit: int = 50) -> list[dict]:
        return self.query(
            "SELECT * FROM device_commands WHERE device_id=? ORDER BY id DESC LIMIT ?",
            (did, limit))

    # ── 个人网盘分享（助手 2026-10-13）─────────────────────────────

    def create_drive_share(self, owner: str, relpath: str, name: str,
                           note: str = "", expires_at: float | None = None) -> dict:
        sid = uuid.uuid4().hex
        with self.tx() as c:
            c.execute(
                "INSERT INTO drive_shares (id, owner, relpath, name, note, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (sid, owner, relpath, name, note[:500], now(), expires_at))
        return self.one("SELECT * FROM drive_shares WHERE id=?", (sid,))

    def list_drive_shares(self, owner: str) -> list[dict]:
        return self.query(
            "SELECT * FROM drive_shares WHERE owner=? ORDER BY created_at DESC", (owner,))

    def list_all_drive_shares(self) -> list[dict]:
        return self.query("SELECT * FROM drive_shares ORDER BY created_at DESC")

    def get_drive_share(self, sid: str) -> dict | None:
        return self.one("SELECT * FROM drive_shares WHERE id=?", (sid,))

    def delete_drive_share(self, sid: str, owner: str | None = None) -> bool:
        with self.tx() as c:
            if owner is None:
                cur = c.execute("DELETE FROM drive_shares WHERE id=?", (sid,))
            else:
                cur = c.execute("DELETE FROM drive_shares WHERE id=? AND owner=?", (sid, owner))
        return cur.rowcount > 0

    # ── 分片上传会话（助手 2026-10-13 分片/续传）─────────────────────

    def create_drive_upload(self, sid: str, owner: str, dest_path: str, name: str,
                            size: int, sha256: str, chunk_size: int,
                            total_chunks: int) -> dict:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO drive_uploads (sid, owner, dest_path, name, size, sha256,"
                " chunk_size, total_chunks, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, owner, dest_path, name, size, sha256, chunk_size, total_chunks, ts, ts))
        return self.one("SELECT * FROM drive_uploads WHERE sid=?", (sid,))

    def get_drive_upload(self, sid: str) -> dict | None:
        return self.one("SELECT * FROM drive_uploads WHERE sid=?", (sid,))

    def find_drive_upload(self, owner: str, dest_path: str, size: int) -> dict | None:
        """按「用户+目标路径+大小」找最近一个会话（续传第一层匹配，最快）。"""
        return self.one(
            "SELECT * FROM drive_uploads WHERE owner=? AND dest_path=? AND size=?"
            " ORDER BY updated_at DESC LIMIT 1", (owner, dest_path, size))

    def find_drive_upload_by_hash(self, owner: str, sha256: str) -> dict | None:
        """按内容哈希找会话（续传第二层：改名/移动后也能命中）。"""
        if not sha256:
            return None
        return self.one(
            "SELECT * FROM drive_uploads WHERE owner=? AND sha256=? AND sha256!=''"
            " ORDER BY updated_at DESC LIMIT 1", (owner, sha256))

    def touch_drive_upload(self, sid: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE drive_uploads SET updated_at=? WHERE sid=?", (now(), sid))

    def update_drive_upload_hash(self, sid: str, sha256: str) -> None:
        """补录内容哈希（前端先开始上传、后算完 SHA-256 时回填）。"""
        with self.tx() as c:
            c.execute("UPDATE drive_uploads SET sha256=?, updated_at=? WHERE sid=?",
                      (sha256 or "", now(), sid))

    def delete_drive_upload(self, sid: str, owner: str | None = None) -> bool:
        with self.tx() as c:
            if owner is None:
                cur = c.execute("DELETE FROM drive_uploads WHERE sid=?", (sid,))
            else:
                cur = c.execute("DELETE FROM drive_uploads WHERE sid=? AND owner=?", (sid, owner))
        return cur.rowcount > 0

    def list_drive_uploads(self, owner: str | None = None) -> list[dict]:
        if owner is None:
            return self.query("SELECT * FROM drive_uploads ORDER BY updated_at DESC")
        return self.query(
            "SELECT * FROM drive_uploads WHERE owner=? ORDER BY updated_at DESC", (owner,))

    def expired_drive_uploads(self, older_than_ts: float) -> list[dict]:
        return self.query("SELECT * FROM drive_uploads WHERE updated_at < ?", (older_than_ts,))


    # ── 星铃委托板（questboard）────────────────────────────────────

    _QUEST_DAILY = (
        ("daily-checkin", "在委托板上留下今天的印记",
         "点一下「完成」就算数。微小，但每天都有。", "daily", "common", 1, "星铃 ×1"),
        ("daily-chat", "和站娘说一句话",
         "在站娘对话里发过一条消息即自动归档（后端按真实会话核验）。", "daily", "common", 1, "星铃 ×2"),
        ("daily-file", "整理一件云端文件",
         "当天有过上传/整理记录即自动归档（后端按审计记录核验）。", "daily", "rare", 1, "星铃 ×3"),
    )

    def _seed_daily_quests(self, uid: str) -> str:
        day = today()
        with self.tx() as c:
            for qid, title, detail, cat, rar, target, reward in self._QUEST_DAILY:
                c.execute(
                    "INSERT OR IGNORE INTO quests (id, user_id, title, detail, category, rarity,"
                    " status, progress, target, reward, day, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,'open',0,?,?,?,?,?)",
                    (f"{qid}:{day}", uid, title, detail, cat, rar, target, reward, day, now(), now()))
        return day

    def _auto_verify_quests(self, uid: str, day: str) -> None:
        """两条每日委托按真实记录核验：不靠用户自报，完成即真实发生过。"""
        try:
            start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except ValueError:
            return
        with self.tx() as c:
            c.execute(
                "UPDATE quests SET progress=1, status='done', updated_at=?"
                " WHERE user_id=? AND day=? AND id=? AND status!='done'"
                " AND EXISTS (SELECT 1 FROM sessions WHERE user_id=? AND updated_at>=?)",
                (now(), uid, day, f"daily-chat:{day}", uid, start))
            c.execute(
                "UPDATE quests SET progress=1, status='done', updated_at=?"
                " WHERE user_id=? AND day=? AND id=? AND status!='done'"
                " AND EXISTS (SELECT 1 FROM audit_log WHERE ts>=?"
                "  AND (action LIKE 'drive.%' OR action LIKE 'file.%'))",
                (now(), uid, day, f"daily-file:{day}", start))

    def list_quests(self, uid: str) -> list[dict]:
        day = self._seed_daily_quests(uid)
        self._auto_verify_quests(uid, day)
        return self.query(
            "SELECT * FROM quests WHERE user_id=? AND (day='' OR day=?)"
            " ORDER BY CASE category WHEN 'main' THEN 0 WHEN 'bounty' THEN 1"
            " WHEN 'side' THEN 2 ELSE 3 END, created_at DESC",
            (uid, day))

    def claim_quest(self, uid: str, qid: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE quests SET status='active', updated_at=?"
                " WHERE id=? AND user_id=? AND status='open'", (now(), qid, uid))
            return cur.rowcount > 0

    def complete_quest(self, uid: str, qid: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE quests SET status='done', progress=target, updated_at=?"
                " WHERE id=? AND user_id=? AND status IN ('open','active')", (now(), qid, uid))
            return cur.rowcount > 0

    def create_quest(self, uid: str, title: str, detail: str = "",
                     category: str = "side", rarity: str = "common",
                     target: int = 1, reward: str = "",
                     origin: str = "self", ref: str = "") -> tuple[dict | None, bool]:
        """挂一张委托。返回 (row, created)。

        ref 非空时按 (user_id, ref) 幂等：已存在则只更新内容，保留原状态/进度，
        这样「站娘挂账」可以每轮重发同一件事而不刷屏。
        """
        if category not in ("main", "side", "bounty"):
            category = "side"
        if rarity not in ("common", "rare", "epic", "legend"):
            rarity = "common"
        try:
            target = max(1, min(int(target), 99))
        except (TypeError, ValueError):
            target = 1
        title = (title or "").strip()[:80]
        detail = (detail or "").strip()[:300]
        reward = (reward or "").strip()[:24]
        origin = (origin or "self").strip()[:24] or "self"
        ref = (ref or "").strip()[:120]
        if ref:
            existing = self.one(
                "SELECT * FROM quests WHERE user_id=? AND ref=?", (uid, ref))
            if existing:
                with self.tx() as c:
                    c.execute(
                        "UPDATE quests SET title=?, detail=?, category=?, rarity=?,"
                        " target=?, reward=?, origin=?, updated_at=? WHERE id=?",
                        (title, detail, category, rarity, target, reward, origin,
                         now(), existing["id"]))
                return self.one("SELECT * FROM quests WHERE id=?", (existing["id"],)), False
        qid = uuid.uuid4().hex[:16]
        with self.tx() as c:
            c.execute(
                "INSERT INTO quests (id, user_id, title, detail, category, rarity, status,"
                " progress, target, reward, day, origin, ref, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,'open',0,?,?,'',?,?,?,?)",
                (qid, uid, title, detail, category, rarity,
                 target, reward, origin, ref, now(), now()))
        return self.one("SELECT * FROM quests WHERE id=?", (qid,)), True

    def quest_stats(self, uid: str, rows: list[dict] | None = None) -> dict:
        rows = rows if rows is not None else self.list_quests(uid)
        done = [r for r in rows if r["status"] == "done"]
        stars = 0
        for r in done:
            rw = r.get("reward") or ""
            digits = "".join(ch for ch in rw.split("×")[-1] if ch.isdigit())
            stars += int(digits) if digits else 1
        daily = [r for r in rows if r["category"] == "daily"]
        return {
            "total": len(rows),
            "done": len(done),
            "active": sum(1 for r in rows if r["status"] == "active"),
            "open": sum(1 for r in rows if r["status"] == "open"),
            "stars": stars,
            "daily_total": len(daily),
            "daily_done": sum(1 for r in daily if r["status"] == "done"),
            "staff_open": sum(1 for r in rows if (r.get("origin") or "self") != "self"
                              and r["status"] != "done"),
        }


def strip_sensitive(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "password_hash" and k != "token_hash"}


_db: DB | None = None


def get_db() -> DB:
    global _db
    if _db is None:
        _db = DB()
    return _db
