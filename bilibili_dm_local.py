#!/usr/bin/env python3
"""
Local Bilibili private-message collector.

This is intentionally stdlib-only so it can run in a fresh local workspace:

    python bilibili_dm_local.py

Configure credentials through config.json or environment variables. The Bilibili
private-message endpoints used here are not guaranteed public stable APIs, so
the collector keeps the adapter isolated from the local storage and UI.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_DB_PATH = APP_DIR / "data" / "bilibili_dm.sqlite3"


def now_ts() -> int:
    return int(time.time())


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def parse_json_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = value.strip()
    if not value:
        return value
    if value[0] not in "[{":
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def first_present(data: dict[str, Any], names: list[str], default: Any = None) -> Any:
    for name in names:
        value = data.get(name)
        if value is not None and value != "":
            return value
    return default


@dataclass
class AppConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    db_path: str = str(DEFAULT_DB_PATH)
    poll_interval_seconds: int = 45
    poll_jitter_seconds: int = 0
    request_timeout_seconds: int = 15
    self_uid: str = ""
    sessdata: str = ""
    bili_jct: str = ""
    dedeuserid: str = ""
    buvid3: str = ""
    buvid4: str = ""
    device_id: str = ""
    extra_cookie: str = ""
    access_key: str = ""
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
    )
    session_pages_per_sync: int = 5
    history_sessions_per_sync: int = 10
    history_pages_per_session: int = 2
    auto_start: bool = False
    mock_mode: bool = False

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        raw: dict[str, Any] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8-sig"))

        def get(name: str, env: str | None = None, default: Any = None) -> Any:
            env_name = env or f"BILI_{name.upper()}"
            return os.environ.get(env_name, raw.get(name, default))

        return cls(
            host=str(get("host", "BILI_HOST", cls.host)),
            port=int(get("port", "BILI_PORT", cls.port)),
            db_path=str(get("db_path", "BILI_DB_PATH", cls.db_path)),
            poll_interval_seconds=int(
                get("poll_interval_seconds", "BILI_POLL_INTERVAL_SECONDS", cls.poll_interval_seconds)
            ),
            poll_jitter_seconds=int(get("poll_jitter_seconds", "BILI_POLL_JITTER_SECONDS", cls.poll_jitter_seconds)),
            request_timeout_seconds=int(
                get("request_timeout_seconds", "BILI_REQUEST_TIMEOUT_SECONDS", cls.request_timeout_seconds)
            ),
            self_uid=str(get("self_uid", "BILI_SELF_UID", "") or ""),
            sessdata=str(get("sessdata", "BILI_SESSDATA", "") or ""),
            bili_jct=str(get("bili_jct", "BILI_BILI_JCT", "") or ""),
            dedeuserid=str(get("dedeuserid", "BILI_DEDEUSERID", "") or ""),
            buvid3=str(get("buvid3", "BILI_BUVID3", "") or ""),
            buvid4=str(get("buvid4", "BILI_BUVID4", "") or ""),
            device_id=str(get("device_id", "BILI_DEVICE_ID", "") or ""),
            extra_cookie=str(get("extra_cookie", "BILI_EXTRA_COOKIE", "") or ""),
            access_key=str(get("access_key", "BILI_ACCESS_KEY", "") or ""),
            user_agent=str(get("user_agent", "BILI_USER_AGENT", cls.user_agent)),
            session_pages_per_sync=int(
                get("session_pages_per_sync", "BILI_SESSION_PAGES_PER_SYNC", cls.session_pages_per_sync)
            ),
            history_sessions_per_sync=int(
                get("history_sessions_per_sync", "BILI_HISTORY_SESSIONS_PER_SYNC", cls.history_sessions_per_sync)
            ),
            history_pages_per_session=int(
                get("history_pages_per_session", "BILI_HISTORY_PAGES_PER_SESSION", cls.history_pages_per_session)
            ),
            auto_start=str(get("auto_start", "BILI_AUTO_START", cls.auto_start)).lower() in {"1", "true", "yes"},
            mock_mode=str(get("mock_mode", "BILI_MOCK_MODE", cls.mock_mode)).lower() in {"1", "true", "yes"},
        )

    def has_auth(self) -> bool:
        return bool(self.access_key or self.sessdata)

    def cookie_header(self) -> str:
        cookies: list[str] = []
        if self.sessdata:
            cookies.append(f"SESSDATA={self.sessdata}")
        if self.bili_jct:
            cookies.append(f"bili_jct={self.bili_jct}")
        if self.dedeuserid:
            cookies.append(f"DedeUserID={self.dedeuserid}")
        if self.buvid3:
            cookies.append(f"buvid3={self.buvid3}")
        if self.buvid4:
            cookies.append(f"buvid4={self.buvid4}")
        if self.extra_cookie:
            cookies.append(self.extra_cookie.strip().strip(";"))
        return "; ".join(part for part in cookies if part)

    def public_view(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "db_path": self.db_path,
            "poll_interval_seconds": self.poll_interval_seconds,
            "poll_jitter_seconds": self.poll_jitter_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "self_uid": self.self_uid,
            "has_sessdata": bool(self.sessdata),
            "has_bili_jct": bool(self.bili_jct),
            "has_buvid3": bool(self.buvid3),
            "has_buvid4": bool(self.buvid4),
            "has_access_key": bool(self.access_key),
            "session_pages_per_sync": self.session_pages_per_sync,
            "history_sessions_per_sync": self.history_sessions_per_sync,
            "history_pages_per_session": self.history_pages_per_session,
            "mock_mode": self.mock_mode,
            "auto_start": self.auto_start,
        }

    def file_view(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "db_path": self.db_path,
            "poll_interval_seconds": self.poll_interval_seconds,
            "poll_jitter_seconds": self.poll_jitter_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "self_uid": self.self_uid,
            "sessdata": self.sessdata,
            "bili_jct": self.bili_jct,
            "dedeuserid": self.dedeuserid,
            "buvid3": self.buvid3,
            "buvid4": self.buvid4,
            "device_id": self.device_id,
            "extra_cookie": self.extra_cookie,
            "access_key": self.access_key,
            "user_agent": self.user_agent,
            "session_pages_per_sync": self.session_pages_per_sync,
            "history_sessions_per_sync": self.history_sessions_per_sync,
            "history_pages_per_session": self.history_pages_per_session,
            "auto_start": self.auto_start,
            "mock_mode": self.mock_mode,
        }

    def apply_patch(self, patch: dict[str, Any]) -> None:
        string_fields = {
            "self_uid",
            "sessdata",
            "bili_jct",
            "dedeuserid",
            "buvid3",
            "buvid4",
            "device_id",
            "extra_cookie",
            "access_key",
            "user_agent",
        }
        int_fields = {
            "poll_interval_seconds",
            "poll_jitter_seconds",
            "request_timeout_seconds",
            "session_pages_per_sync",
            "history_sessions_per_sync",
            "history_pages_per_session",
        }
        bool_fields = {"auto_start", "mock_mode"}
        for key in string_fields:
            if key in patch:
                setattr(self, key, str(patch.get(key) or "").strip())
        for key in int_fields:
            if key in patch:
                value = int(patch.get(key) or 0)
                if key == "history_pages_per_session":
                    value = min(20, max(1, value))
                elif key == "session_pages_per_sync":
                    value = min(20, max(1, value))
                elif key == "history_sessions_per_sync":
                    value = min(200, max(1, value))
                elif key == "poll_jitter_seconds":
                    value = min(3600, max(0, value))
                else:
                    value = max(5, value)
                setattr(self, key, value)
        for key in bool_fields:
            if key in patch:
                value = patch.get(key)
                setattr(self, key, str(value).lower() in {"1", "true", "yes", "on"})


def save_config_file(config_path: Path, config: AppConfig) -> None:
    if not config.device_id:
        config.device_id = str(uuid.uuid4())
    config_path.write_text(json.dumps(config.file_view(), ensure_ascii=False, indent=2), encoding="utf-8")


class Store:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL DEFAULT 'bilibili',
                    talker_id TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT NOT NULL DEFAULT '',
                    last_msg_id TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT '',
                    unread_count INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(platform, talker_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL DEFAULT 'bilibili',
                    msg_id TEXT NOT NULL,
                    conversation_id INTEGER NOT NULL,
                    talker_id TEXT NOT NULL,
                    sender_uid TEXT NOT NULL DEFAULT '',
                    receiver_uid TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    msg_type TEXT NOT NULL DEFAULT 'text',
                    content TEXT NOT NULL DEFAULT '',
                    timestamp INTEGER NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                    UNIQUE(platform, msg_id)
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER,
                    talker_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    raw_response TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    sent_at INTEGER,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS poll_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )

    def upsert_conversation(self, talker_id: str, display_name: str = "", avatar_url: str = "", raw: Any = None) -> int:
        if not talker_id:
            talker_id = "unknown"
        raw_text = json_dumps(raw or {})
        ts = now_ts()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations(talker_id, display_name, avatar_url, updated_at, raw_json)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(platform, talker_id) DO UPDATE SET
                    display_name = COALESCE(NULLIF(excluded.display_name, ''), conversations.display_name),
                    avatar_url = COALESCE(NULLIF(excluded.avatar_url, ''), conversations.avatar_url),
                    updated_at = MAX(conversations.updated_at, excluded.updated_at),
                    raw_json = excluded.raw_json
                """,
                (str(talker_id), display_name or "", avatar_url or "", ts, raw_text),
            )
            row = conn.execute(
                "SELECT id FROM conversations WHERE platform='bilibili' AND talker_id=?",
                (str(talker_id),),
            ).fetchone()
            return int(row["id"])

    def insert_message(self, message: dict[str, Any]) -> bool:
        talker_id = str(message.get("talker_id") or "unknown")
        conversation_id = self.upsert_conversation(
            talker_id=talker_id,
            display_name=str(message.get("display_name") or ""),
            avatar_url=str(message.get("avatar_url") or ""),
            raw=message.get("conversation_raw") or {},
        )
        msg_id = str(message.get("msg_id") or f"local-{talker_id}-{message.get('timestamp')}-{time.time_ns()}")
        content = str(message.get("content") or "")
        timestamp = int(message.get("timestamp") or now_ts())
        direction = str(message.get("direction") or "inbound")
        with self._lock, self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO messages(
                        msg_id, conversation_id, talker_id, sender_uid, receiver_uid,
                        direction, msg_type, content, timestamp, raw_json, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        msg_id,
                        conversation_id,
                        talker_id,
                        str(message.get("sender_uid") or ""),
                        str(message.get("receiver_uid") or ""),
                        direction,
                        str(message.get("msg_type") or "text"),
                        content,
                        timestamp,
                        json_dumps(message.get("raw") or message),
                        now_ts(),
                    ),
                )
            except sqlite3.IntegrityError:
                return False

            unread_delta = 1 if direction == "inbound" else 0
            conn.execute(
                """
                UPDATE conversations
                SET last_msg_id=?, last_message=?, unread_count=unread_count+?,
                    updated_at=MAX(updated_at, ?)
                WHERE id=?
                """,
                (msg_id, content, unread_delta, timestamp, conversation_id),
            )
            return True

    def list_conversations(self, reply_status: str = "all") -> list[dict[str, Any]]:
        allowed_statuses = {"all", "pending", "replied", "no_inbound"}
        if reply_status not in allowed_statuses:
            reply_status = "all"
        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH stats AS (
                    SELECT
                        conversation_id,
                        MAX(CASE WHEN direction='inbound' THEN timestamp ELSE NULL END) AS last_inbound_at,
                        MAX(CASE WHEN direction='outbound' THEN timestamp ELSE NULL END) AS last_outbound_at,
                        SUM(CASE WHEN direction='inbound' THEN 1 ELSE 0 END) AS inbound_count,
                        SUM(CASE WHEN direction='outbound' THEN 1 ELSE 0 END) AS outbound_count
                    FROM messages
                    GROUP BY conversation_id
                ),
                last_rows AS (
                    SELECT conversation_id, direction AS last_direction, msg_type AS last_msg_type
                    FROM (
                        SELECT
                            conversation_id,
                            direction,
                            msg_type,
                            ROW_NUMBER() OVER (
                                PARTITION BY conversation_id
                                ORDER BY timestamp DESC, id DESC
                            ) AS rn
                        FROM messages
                    )
                    WHERE rn=1
                )
                SELECT
                    c.id, c.talker_id, c.display_name, c.avatar_url, c.last_msg_id,
                    c.last_message, c.unread_count, c.updated_at,
                    COALESCE(s.last_inbound_at, 0) AS last_inbound_at,
                    COALESCE(s.last_outbound_at, 0) AS last_outbound_at,
                    COALESCE(s.inbound_count, 0) AS inbound_count,
                    COALESCE(s.outbound_count, 0) AS outbound_count,
                    COALESCE(l.last_direction, '') AS last_direction,
                    COALESCE(l.last_msg_type, '') AS last_msg_type,
                    CASE
                        WHEN COALESCE(s.inbound_count, 0)=0 THEN 'no_inbound'
                        WHEN COALESCE(s.last_inbound_at, 0) > COALESCE(s.last_outbound_at, 0) THEN 'pending'
                        ELSE 'replied'
                    END AS reply_status
                FROM conversations c
                LEFT JOIN stats s ON s.conversation_id=c.id
                LEFT JOIN last_rows l ON l.conversation_id=c.id
                ORDER BY updated_at DESC, id DESC
                LIMIT 200
                """
            ).fetchall()
            items = [dict(row) for row in rows]
            if reply_status == "all":
                return items
            return [item for item in items if item["reply_status"] == reply_status]

    def list_messages(self, conversation_id: int | None, talker_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if conversation_id:
                rows = conn.execute(
                    """
                    SELECT id, msg_id, talker_id, sender_uid, receiver_uid, direction,
                           msg_type, content, timestamp, created_at
                    FROM messages
                    WHERE conversation_id=?
                    ORDER BY timestamp ASC, id ASC
                    LIMIT 500
                    """,
                    (conversation_id,),
                ).fetchall()
            elif talker_id:
                rows = conn.execute(
                    """
                    SELECT id, msg_id, talker_id, sender_uid, receiver_uid, direction,
                           msg_type, content, timestamp, created_at
                    FROM messages
                    WHERE talker_id=?
                    ORDER BY timestamp ASC, id ASC
                    LIMIT 500
                    """,
                    (talker_id,),
                ).fetchall()
            else:
                rows = []
            return [dict(row) for row in rows]

    def create_outbox(self, talker_id: str, content: str) -> int:
        conversation_id = self.upsert_conversation(talker_id)
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO outbox(conversation_id, talker_id, content, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (conversation_id, str(talker_id), content, now_ts()),
            )
            return int(cur.lastrowid)

    def update_outbox(self, outbox_id: int, status: str, raw_response: Any = None, error: str = "") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET status=?, attempts=attempts+1, raw_response=?, error=?, sent_at=?
                WHERE id=?
                """,
                (
                    status,
                    json_dumps(raw_response or {}),
                    error,
                    now_ts() if status == "sent" else None,
                    outbox_id,
                ),
            )

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "conversations": int(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]),
                "messages": int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
                "pending_outbox": int(
                    conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
                ),
            }

    def set_state(self, key: str, value: Any) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO poll_state(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, json_dumps(value), now_ts()),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM poll_state WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def delete_state(self, key: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM poll_state WHERE key=?", (key,))


class BilibiliClient:
    NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
    PULL_URL = "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
    HISTORY_URL = "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"
    SEND_URL = "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg"

    def __init__(self, config: AppConfig):
        self.config = config

    def _request(self, method: str, url: str, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = None
        headers = {
            "User-Agent": self.config.user_agent,
            "Referer": "https://message.bilibili.com/",
            "Origin": "https://message.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        cookie = self.config.cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Bilibili HTTP {exc.code}: {raw[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Bilibili request failed: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Bilibili returned non-JSON response: {raw[:500]}") from exc

    def pull(self, last_cursor: Any = None) -> Any:
        params: dict[str, Any] = {
            "session_type": 1,
            "group_fold": 1,
            "unfollow_fold": 0,
            "sort_rule": 2,
            "build": 0,
            "mobi_app": "web",
        }
        if self.config.access_key:
            params["access_key"] = self.config.access_key
        if last_cursor:
            params["end_ts"] = last_cursor
        return self._request("GET", self.PULL_URL, params=params)

    def history(self, talker_id: str, begin_seqno: str | int | None = None) -> Any:
        params: dict[str, Any] = {
            "sender_device_id": 1,
            "talker_id": talker_id,
            "session_type": 1,
            "size": 50,
            "build": 0,
            "mobi_app": "web",
        }
        if begin_seqno:
            params["begin_seqno"] = begin_seqno
        if self.config.access_key:
            params["access_key"] = self.config.access_key
        return self._request("GET", self.HISTORY_URL, params=params)

    def auth_check(self) -> Any:
        return self._request("GET", self.NAV_URL)

    @staticmethod
    def _ensure_success_response(response: Any, action: str) -> Any:
        if isinstance(response, dict):
            code = response.get("code")
            if code not in (None, 0):
                message = response.get("message") or response.get("msg") or response
                raise RuntimeError(f"{action} failed: code={code}, message={message}")
        return response

    def send_text(self, talker_id: str, content: str) -> Any:
        if not self.config.bili_jct:
            raise RuntimeError("Sending requires bili_jct csrf token in config.json")
        if not self.config.device_id:
            self.config.device_id = str(uuid.uuid4())
        timestamp = now_ts()
        data = {
            "msg[sender_uid]": str(self.config.self_uid),
            "msg[receiver_id]": str(talker_id),
            "msg[receiver_type]": "1",
            "msg[msg_type]": "1",
            "msg[msg_status]": "0",
            "msg[content]": json_dumps({"content": content}),
            "msg[timestamp]": str(timestamp),
            "msg[new_face_version]": "1",
            "msg[dev_id]": self.config.device_id,
            "build": "0",
            "from_firework": "0",
            "mobi_app": "web",
            "csrf": self.config.bili_jct,
            "csrf_token": self.config.bili_jct,
        }
        if self.config.access_key:
            data["access_key"] = self.config.access_key
        response = self._request("POST", self.SEND_URL, data=data)
        return self._ensure_success_response(response, "Bilibili send_text")


def walk_values(value: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for child in value.values():
            found.append(child)
            found.extend(walk_values(child))
    elif isinstance(value, list):
        for child in value:
            found.append(child)
            found.extend(walk_values(child))
    return found


def extract_message_candidates(payload: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for value in walk_values(payload):
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            keys = set(item)
            if keys & {"msg_id", "msg_key", "msg_seqno", "msg_seq", "content"} and keys & {
                "sender_uid",
                "sender",
                "talker_id",
                "receiver_id",
                "receiver_uid",
            }:
                candidates.append(item)
    deduped: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in candidates:
        marker = id(item)
        if marker not in seen:
            seen.add(marker)
            deduped.append(item)
    return deduped


def extract_session_candidates(payload: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for value in walk_values(payload):
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            keys = set(item)
            if "last_msg" in keys or "talker_id" in keys or "account_info" in keys:
                talker_id = session_talker_id(item)
                if talker_id:
                    candidates.append(item)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        talker_id = session_talker_id(item)
        if talker_id and talker_id not in seen:
            seen.add(talker_id)
            deduped.append(item)
    return deduped


def session_talker_id(raw: dict[str, Any]) -> str:
    talker_id = str(first_present(raw, ["talker_id", "talker", "session_id", "mid"], "") or "")
    account_info = raw.get("account_info")
    if not talker_id and isinstance(account_info, dict):
        talker_id = str(first_present(account_info, ["mid", "uid", "id"], "") or "")
    last_msg = raw.get("last_msg")
    if not talker_id and isinstance(last_msg, dict):
        talker_id = str(first_present(last_msg, ["talker_id", "sender_uid", "receiver_uid"], "") or "")
    return talker_id


def normalize_session(raw: dict[str, Any]) -> dict[str, Any]:
    account_info = raw.get("account_info")
    if not isinstance(account_info, dict):
        account_info = {}
    return {
        "talker_id": session_talker_id(raw),
        "display_name": str(first_present(account_info, ["name", "uname", "nickname"], "")),
        "avatar_url": str(first_present(account_info, ["face", "avatar", "avatar_url"], "")),
        "raw": raw,
    }


def session_last_message(raw: dict[str, Any]) -> dict[str, Any] | None:
    talker_id = session_talker_id(raw)
    last_msg = raw.get("last_msg")
    if not talker_id or not isinstance(last_msg, dict):
        return None
    message = dict(last_msg)
    message.setdefault("talker_id", talker_id)
    return message


def normalize_message(raw: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    content_raw = first_present(raw, ["content", "msg_content", "text"], "")
    sender_uid = str(first_present(raw, ["sender_uid", "sender", "from_uid", "mid"], "") or "")
    receiver_uid = str(first_present(raw, ["receiver_uid", "receiver_id", "to_uid"], "") or "")
    talker_id = str(first_present(raw, ["talker_id", "talker", "session_id"], "") or "")
    if not talker_id:
        if config.self_uid and sender_uid == config.self_uid:
            talker_id = receiver_uid
        else:
            talker_id = sender_uid or receiver_uid or "unknown"

    timestamp = int(first_present(raw, ["timestamp", "msg_time", "ctime", "time"], now_ts()) or now_ts())
    if timestamp > 9999999999:
        timestamp = int(timestamp / 1000)

    direction = "outbound" if config.self_uid and sender_uid == config.self_uid else "inbound"
    msg_type_raw = first_present(raw, ["msg_type", "type"], "text")
    msg_type = "text" if str(msg_type_raw) in {"1", "text"} else str(msg_type_raw)
    content = render_message_content(msg_type, content_raw)
    msg_id = str(
        first_present(
            raw,
            ["msg_id", "msg_key", "msg_seqno", "msg_seq", "id"],
            f"{talker_id}-{timestamp}-{hash(content)}",
        )
    )

    user_info = first_present(raw, ["user_info", "account_info", "sender_info"], {}) or {}
    if not isinstance(user_info, dict):
        user_info = {}

    return {
        "msg_id": msg_id,
        "talker_id": talker_id,
        "sender_uid": sender_uid,
        "receiver_uid": receiver_uid,
        "direction": direction,
        "msg_type": msg_type,
        "content": content,
        "timestamp": timestamp,
        "display_name": str(first_present(user_info, ["name", "uname", "nickname"], "")),
        "avatar_url": str(first_present(user_info, ["face", "avatar", "avatar_url"], "")),
        "raw": raw,
    }


def render_message_content(msg_type: str, content_raw: Any) -> str:
    parsed_content = parse_json_maybe(content_raw)
    if msg_type == "text":
        if isinstance(parsed_content, dict):
            return str(first_present(parsed_content, ["content", "text", "message"], json_dumps(parsed_content)))
        return str(parsed_content or "")

    if msg_type == "2":
        if isinstance(parsed_content, dict):
            url = first_present(parsed_content, ["url", "image_url", "src"], "")
            return f"[图片] {url}".strip()
        return "[图片]"

    if msg_type == "5":
        return "[撤回或引用消息]"

    if msg_type == "10":
        if isinstance(parsed_content, dict):
            title = str(first_present(parsed_content, ["title"], "") or "").strip()
            text = str(first_present(parsed_content, ["text", "jump_text"], "") or "").strip()
            uri = str(first_present(parsed_content, ["jump_uri", "jump_uri_2", "jump_uri_3"], "") or "").strip()
            parts = [part for part in [title, text, uri] if part]
            return " / ".join(parts) if parts else "[系统通知]"
        return str(parsed_content or "[系统通知]")

    if isinstance(parsed_content, dict):
        return str(first_present(parsed_content, ["content", "text", "title", "message"], json_dumps(parsed_content)))
    return str(parsed_content or f"[非文本消息:{msg_type}]")


class Collector:
    def __init__(self, config: AppConfig, store: Store):
        self.config = config
        self.store = store
        self.client = BilibiliClient(config)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.last_error = ""
        self.last_sync_at = 0

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="bilibili-dm-poller", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{exc}\n{traceback.format_exc(limit=3)}"
            wait_seconds = self.next_wait_seconds()
            self._stop.wait(wait_seconds)

    def next_wait_seconds(self) -> int:
        jitter = max(0, int(self.config.poll_jitter_seconds or 0))
        offset = random.randint(-jitter, jitter) if jitter else 0
        return max(5, int(self.config.poll_interval_seconds) + offset)

    def sync_once(self) -> dict[str, Any]:
        if self.config.mock_mode and not self.config.has_auth():
            return self._mock_tick()
        if not self.config.has_auth():
            raise RuntimeError("No Bilibili auth configured. Fill config.json or enable mock_mode.")

        payloads: list[Any] = []
        sessions_raw: list[dict[str, Any]] = []
        cursor: Any = None
        for _ in range(self.config.session_pages_per_sync):
            payload = self.client.pull(cursor)
            payloads.append(payload)
            page_sessions = extract_session_candidates(payload)
            if not page_sessions:
                break
            sessions_raw.extend(page_sessions)
            data = payload.get("data") if isinstance(payload, dict) else {}
            has_more = bool(data.get("has_more")) if isinstance(data, dict) else False
            session_ts_values = [
                int(item.get("session_ts") or 0)
                for item in page_sessions
                if str(item.get("session_ts") or "").isdigit()
            ]
            if not has_more or not session_ts_values:
                break
            cursor = min(session_ts_values)

        sessions = [normalize_session(item) for item in sessions_raw]
        for session in sessions:
            if session["talker_id"]:
                self.store.upsert_conversation(
                    session["talker_id"],
                    display_name=session["display_name"],
                    avatar_url=session["avatar_url"],
                    raw=session["raw"],
                )

        raw_messages: list[dict[str, Any]] = []
        for payload in payloads:
            raw_messages.extend(extract_message_candidates(payload))
        for session_raw in sessions_raw:
            last_msg = session_last_message(session_raw)
            if last_msg:
                raw_messages.append(last_msg)

        history_seen = 0
        history_inserted = 0
        history_processed = 0
        for session in sessions:
            if not session["talker_id"]:
                continue
            if self.store.get_state(f"history_backfilled:{session['talker_id']}", False):
                continue
            if history_processed >= self.config.history_sessions_per_sync:
                break
            result = self.sync_history(session["talker_id"])
            history_processed += 1
            history_seen += int(result["seen"])
            history_inserted += int(result["inserted"])

        messages = [normalize_message(item, self.config) for item in raw_messages]
        inserted = 0
        for message in sorted(messages, key=lambda item: item["timestamp"]):
            if self.store.insert_message(message):
                inserted += 1
        self.last_error = ""
        self.last_sync_at = now_ts()
        return {
            "inserted": inserted + history_inserted,
            "seen": len(messages) + history_seen,
            "sessions": len(sessions),
            "session_pages": len(payloads),
            "history_processed": history_processed,
            "source": "bilibili",
        }

    def sync_history(self, talker_id: str) -> dict[str, Any]:
        begin_seqno = self.store.get_state(f"history_begin_seqno:{talker_id}")
        seen = 0
        inserted = 0
        for _ in range(self.config.history_pages_per_session):
            payload = self.client.history(talker_id, begin_seqno)
            raw_messages = extract_message_candidates(payload)
            if not raw_messages:
                break
            normalized = [normalize_message(item, self.config) for item in raw_messages]
            for message in sorted(normalized, key=lambda item: item["timestamp"]):
                if self.store.insert_message(message):
                    inserted += 1
            seen += len(normalized)
            seqnos = [
                int(value)
                for value in (
                    first_present(item, ["msg_seqno", "msg_seq", "seqno", "sequence"], None)
                    for item in raw_messages
                )
                if str(value or "").isdigit()
            ]
            if not seqnos:
                break
            next_begin = max(0, min(seqnos) - 1)
            if next_begin == begin_seqno:
                break
            begin_seqno = next_begin
            self.store.set_state(f"history_begin_seqno:{talker_id}", begin_seqno)
        self.store.set_state(f"history_backfilled:{talker_id}", True)
        return {"inserted": inserted, "seen": seen}

    def _mock_tick(self) -> dict[str, Any]:
        message = {
            "msg_id": f"mock-{time.time_ns()}",
            "talker_id": "10001",
            "sender_uid": "10001",
            "receiver_uid": self.config.self_uid,
            "direction": "inbound",
            "msg_type": "text",
            "content": f"本地模拟私信 {time.strftime('%H:%M:%S')}",
            "timestamp": now_ts(),
            "display_name": "本地测试用户",
            "raw": {"mock": True},
        }
        inserted = 1 if self.store.insert_message(message) else 0
        self.last_error = ""
        self.last_sync_at = now_ts()
        return {"inserted": inserted, "seen": 1, "source": "mock"}


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Bilibili 私信本地收集系统</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #fff;
      --line: #dde1e7;
      --text: #1d2630;
      --muted: #667085;
      --accent: #00a1d6;
      --accent-dark: #0088b7;
      --danger: #c03434;
      --ok: #208a55;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .app {
      height: 100vh;
      display: grid;
      grid-template-rows: auto auto 1fr;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 18px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }
    h1 {
      font-size: 18px;
      margin: 0;
      font-weight: 650;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      min-height: 34px;
      padding: 0 12px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 14px;
    }
    button.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    button.primary:hover { background: var(--accent-dark); }
    button:disabled {
      cursor: not-allowed;
      opacity: .55;
    }
    .status {
      font-size: 13px;
      color: var(--muted);
      display: flex;
      gap: 10px;
      align-items: center;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--danger);
      display: inline-block;
    }
    .dot.on { background: var(--ok); }
    .setup {
      display: none;
      grid-template-columns: repeat(4, minmax(160px, 1fr));
      gap: 10px;
      padding: 12px 18px;
      background: #fff;
      border-bottom: 1px solid var(--line);
    }
    .setup.open { display: grid; }
    .field {
      display: grid;
      gap: 5px;
      min-width: 0;
    }
    .field label {
      color: var(--muted);
      font-size: 12px;
    }
    .field input {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      font: inherit;
    }
    .setup-actions {
      display: flex;
      gap: 8px;
      align-items: end;
      flex-wrap: wrap;
    }
    main {
      display: grid;
      grid-template-columns: 320px 1fr;
      min-height: 0;
    }
    aside {
      background: var(--panel);
      border-right: 1px solid var(--line);
      overflow: auto;
    }
    .filters {
      position: sticky;
      top: 0;
      z-index: 2;
      display: flex;
      gap: 6px;
      padding: 10px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }
    .filters button {
      flex: 1;
      min-height: 30px;
      padding: 0 8px;
      font-size: 13px;
    }
    .filters button.active {
      border-color: var(--accent);
      color: var(--accent-dark);
      background: #eaf7fc;
    }
    .conv {
      width: 100%;
      padding: 14px 14px;
      border-bottom: 1px solid var(--line);
      cursor: pointer;
      display: grid;
      gap: 5px;
      background: #fff;
    }
    .conv.active { background: #eaf7fc; }
    .conv-title {
      font-weight: 650;
      display: flex;
      justify-content: space-between;
      gap: 10px;
    }
    .conv-id, .conv-last, .time {
      color: var(--muted);
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .badge {
      min-width: 20px;
      height: 20px;
      border-radius: 10px;
      background: var(--accent);
      color: white;
      font-size: 12px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 6px;
    }
    .reply-pill {
      border-radius: 999px;
      font-size: 12px;
      padding: 2px 7px;
      white-space: nowrap;
      border: 1px solid var(--line);
      color: var(--muted);
      background: #f8fafc;
    }
    .reply-pill.pending {
      color: #a33b00;
      background: #fff3e8;
      border-color: #ffd4aa;
    }
    .reply-pill.replied {
      color: #16704b;
      background: #e8f7ef;
      border-color: #bfe8cf;
    }
    .chat {
      min-width: 0;
      display: grid;
      grid-template-rows: 1fr auto;
      background: #eef1f4;
    }
    .messages {
      overflow: auto;
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .msg {
      max-width: min(680px, 78%);
      padding: 10px 12px;
      border-radius: 8px;
      background: #fff;
      border: 1px solid var(--line);
      line-height: 1.5;
      word-break: break-word;
    }
    .msg.outbound {
      align-self: flex-end;
      background: #dff5fd;
      border-color: #bde9f7;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }
    .composer {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      padding: 12px;
      background: var(--panel);
      border-top: 1px solid var(--line);
    }
    textarea {
      width: 100%;
      resize: vertical;
      min-height: 44px;
      max-height: 140px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      font: inherit;
    }
    .empty {
      color: var(--muted);
      padding: 28px;
      text-align: center;
    }
    @media (max-width: 760px) {
      .app { height: auto; min-height: 100vh; }
      main { grid-template-columns: 1fr; }
      aside { max-height: 38vh; border-right: 0; border-bottom: 1px solid var(--line); }
      header { align-items: flex-start; height: auto; min-height: 56px; padding: 10px 12px; gap: 10px; flex-direction: column; }
      .setup { grid-template-columns: 1fr; padding: 12px; }
      .msg { max-width: 92%; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <h1>Bilibili 私信本地收集系统</h1>
      <div class="toolbar">
        <div class="status"><span id="dot" class="dot"></span><span id="statusText">加载中</span></div>
        <button id="syncBtn">立即同步</button>
        <button id="startBtn" class="primary">启动轮询</button>
        <button id="stopBtn">停止</button>
        <button id="mockBtn">模拟私信</button>
        <button id="settingsBtn">登录配置</button>
        <button id="authBtn">校验登录</button>
        <button id="backfillBtn">补拉历史</button>
      </div>
    </header>
    <section id="setupPanel" class="setup">
      <div class="field">
        <label for="selfUidInput">当前账号 UID</label>
        <input id="selfUidInput" autocomplete="off" placeholder="用于区分收发方向" />
      </div>
      <div class="field">
        <label for="sessdataInput">SESSDATA</label>
        <input id="sessdataInput" autocomplete="off" placeholder="留空保持不变" />
      </div>
      <div class="field">
        <label for="biliJctInput">bili_jct</label>
        <input id="biliJctInput" autocomplete="off" placeholder="发送私信需要" />
      </div>
      <div class="field">
        <label for="dedeUserIdInput">DedeUserID</label>
        <input id="dedeUserIdInput" autocomplete="off" placeholder="通常等于账号 UID" />
      </div>
      <div class="field">
        <label for="buvid3Input">buvid3</label>
        <input id="buvid3Input" autocomplete="off" placeholder="浏览器 Cookie 里的 buvid3" />
      </div>
      <div class="field">
        <label for="buvid4Input">buvid4</label>
        <input id="buvid4Input" autocomplete="off" placeholder="浏览器 Cookie 里的 buvid4" />
      </div>
      <div class="field">
        <label for="pollIntervalInput">轮询间隔秒</label>
        <input id="pollIntervalInput" type="number" min="10" step="5" />
      </div>
      <div class="field">
        <label for="pollJitterInput">轮询随机浮动秒</label>
        <input id="pollJitterInput" type="number" min="0" max="3600" step="5" />
      </div>
      <div class="field">
        <label for="historySessionsInput">每次补拉会话数</label>
        <input id="historySessionsInput" type="number" min="1" max="200" />
      </div>
      <div class="field">
        <label for="sessionPagesInput">每次拉取会话页数</label>
        <input id="sessionPagesInput" type="number" min="1" max="20" />
      </div>
      <div class="field">
        <label for="historyPagesInput">每个会话历史页数</label>
        <input id="historyPagesInput" type="number" min="1" max="20" />
      </div>
      <div class="setup-actions">
        <button id="saveConfigBtn" class="primary">保存配置</button>
        <button id="realModeBtn">切到真实模式</button>
      </div>
    </section>
    <main>
      <aside>
        <div class="filters">
          <button class="active" data-filter="all">全部</button>
          <button data-filter="pending">待回复</button>
          <button data-filter="replied">已回复</button>
        </div>
        <div id="conversations"><div class="empty">暂无会话</div></div>
      </aside>
      <section class="chat">
        <div id="messages" class="messages"><div class="empty">选择一个会话</div></div>
        <div class="composer">
          <textarea id="content" placeholder="输入回复内容"></textarea>
          <button id="sendBtn" class="primary">发送</button>
        </div>
      </section>
    </main>
  </div>
  <script>
    let selected = null;
    let conversations = [];
    let conversationFilter = "all";

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
      return data;
    }

    function fmtTime(ts) {
      if (!ts) return "";
      return new Date(ts * 1000).toLocaleString();
    }

    function replyLabel(status) {
      if (status === "pending") return "待回复";
      if (status === "replied") return "已回复";
      return "无来信";
    }

    async function refreshStatus() {
      const data = await api("/api/status");
      document.getElementById("dot").classList.toggle("on", data.running);
      document.getElementById("statusText").textContent =
        `${data.running ? "轮询中" : "已停止"} · 会话 ${data.counts.conversations} · 消息 ${data.counts.messages}`;
      document.getElementById("startBtn").disabled = data.running;
      document.getElementById("stopBtn").disabled = !data.running;
      document.getElementById("mockBtn").style.display = data.config.mock_mode ? "" : "none";
      fillConfig(data.config);
    }

    function fillConfig(config) {
      const map = {
        selfUidInput: config.self_uid || "",
        pollIntervalInput: config.poll_interval_seconds || 45,
        pollJitterInput: config.poll_jitter_seconds || 0,
        sessionPagesInput: config.session_pages_per_sync || 5,
        historySessionsInput: config.history_sessions_per_sync || 10,
        historyPagesInput: config.history_pages_per_session || 2
      };
      for (const [id, value] of Object.entries(map)) {
        const node = document.getElementById(id);
        if (node && document.activeElement !== node) node.value = value;
      }
    }

    function configPayload(overrides = {}) {
      const payload = {
        self_uid: document.getElementById("selfUidInput").value.trim(),
        poll_interval_seconds: Number(document.getElementById("pollIntervalInput").value || 45),
        poll_jitter_seconds: Number(document.getElementById("pollJitterInput").value || 0),
        session_pages_per_sync: Number(document.getElementById("sessionPagesInput").value || 5),
        history_sessions_per_sync: Number(document.getElementById("historySessionsInput").value || 10),
        history_pages_per_session: Number(document.getElementById("historyPagesInput").value || 2),
        ...overrides
      };
      const sessdata = document.getElementById("sessdataInput").value.trim();
      const biliJct = document.getElementById("biliJctInput").value.trim();
      const dedeUserId = document.getElementById("dedeUserIdInput").value.trim();
      const buvid3 = document.getElementById("buvid3Input").value.trim();
      const buvid4 = document.getElementById("buvid4Input").value.trim();
      if (sessdata) payload.sessdata = sessdata;
      if (biliJct) payload.bili_jct = biliJct;
      if (dedeUserId) payload.dedeuserid = dedeUserId;
      if (buvid3) payload.buvid3 = buvid3;
      if (buvid4) payload.buvid4 = buvid4;
      return payload;
    }

    async function refreshConversations() {
      const data = await api(`/api/conversations?reply_status=${conversationFilter}`);
      conversations = data.items;
      const root = document.getElementById("conversations");
      if (!conversations.length) {
        root.innerHTML = '<div class="empty">暂无会话</div>';
        return;
      }
      root.innerHTML = conversations.map(item => `
        <div class="conv ${selected && selected.id === item.id ? "active" : ""}" data-id="${item.id}">
          <div class="conv-title">
            <span>${escapeHtml(item.display_name || "用户 " + item.talker_id)}</span>
            <span class="reply-pill ${item.reply_status}">${replyLabel(item.reply_status)}</span>
          </div>
          <div class="conv-id">UID ${escapeHtml(item.talker_id)}</div>
          <div class="conv-last">${escapeHtml(item.last_message || "")}</div>
          <div class="time">最后来信 ${fmtTime(item.last_inbound_at)} · 最后回复 ${fmtTime(item.last_outbound_at)}</div>
        </div>
      `).join("");
      root.querySelectorAll(".conv").forEach(el => {
        el.addEventListener("click", () => {
          selected = conversations.find(item => String(item.id) === el.dataset.id);
          refreshConversations();
          refreshMessages();
        });
      });
    }

    async function refreshMessages() {
      const root = document.getElementById("messages");
      if (!selected) {
        root.innerHTML = '<div class="empty">选择一个会话</div>';
        return;
      }
      const data = await api(`/api/messages?conversation_id=${selected.id}`);
      if (!data.items.length) {
        root.innerHTML = '<div class="empty">暂无消息</div>';
        return;
      }
      root.innerHTML = data.items.map(item => `
        <div class="msg ${item.direction}">
          <div class="meta">${item.direction === "outbound" ? "我" : "对方"} · ${fmtTime(item.timestamp)}</div>
          <div>${escapeHtml(item.content)}</div>
        </div>
      `).join("");
      root.scrollTop = root.scrollHeight;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }[ch]));
    }

    async function run(action) {
      try {
        await action();
        await refreshStatus();
        await refreshConversations();
        if (selected) await refreshMessages();
      } catch (err) {
        alert(err.message);
      }
    }

    document.getElementById("syncBtn").onclick = () => run(() => api("/api/sync", { method: "POST", body: "{}" }));
    document.getElementById("startBtn").onclick = () => run(() => api("/api/start", { method: "POST", body: "{}" }));
    document.getElementById("stopBtn").onclick = () => run(() => api("/api/stop", { method: "POST", body: "{}" }));
    document.querySelectorAll(".filters button").forEach(button => {
      button.addEventListener("click", () => {
        conversationFilter = button.dataset.filter || "all";
        document.querySelectorAll(".filters button").forEach(item => {
          item.classList.toggle("active", item === button);
        });
        run(async () => {});
      });
    });
    document.getElementById("settingsBtn").onclick = () => {
      document.getElementById("setupPanel").classList.toggle("open");
    };
    document.getElementById("saveConfigBtn").onclick = () => run(async () => {
      await api("/api/config", { method: "POST", body: JSON.stringify(configPayload()) });
      document.getElementById("sessdataInput").value = "";
      document.getElementById("biliJctInput").value = "";
      document.getElementById("dedeUserIdInput").value = "";
      document.getElementById("buvid3Input").value = "";
      document.getElementById("buvid4Input").value = "";
    });
    document.getElementById("realModeBtn").onclick = () => run(async () => {
      await api("/api/config", { method: "POST", body: JSON.stringify(configPayload({ mock_mode: false })) });
    });
    document.getElementById("authBtn").onclick = () => run(async () => {
      const result = await api("/api/auth/check", { method: "POST", body: "{}" });
      const data = result.response && result.response.data ? result.response.data : {};
      alert(data.isLogin ? `登录有效：${data.uname || data.mid}` : "登录态无效或已过期");
    });
    document.getElementById("backfillBtn").onclick = () => run(async () => {
      const result = await api("/api/history/backfill", {
        method: "POST",
        body: JSON.stringify({ force: true })
      });
      alert(`补拉完成：看到 ${result.seen} 条，新增 ${result.inserted} 条`);
    });
    document.getElementById("mockBtn").onclick = () => run(() => api("/api/mock/inbound", {
      method: "POST",
      body: JSON.stringify({ talker_id: "10001", nickname: "本地测试用户", content: "这是一条本地模拟私信" })
    }));
    document.getElementById("sendBtn").onclick = () => run(async () => {
      if (!selected) throw new Error("先选择会话");
      const textarea = document.getElementById("content");
      const content = textarea.value.trim();
      if (!content) throw new Error("回复内容不能为空");
      await api("/api/send", { method: "POST", body: JSON.stringify({ talker_id: selected.talker_id, content }) });
      textarea.value = "";
    });

    setInterval(() => run(async () => {}), 5000);
    run(async () => {});
  </script>
</body>
</html>
"""


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "BiliDMLocal/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            self.send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/status"):
            self.send_json(
                {
                    "ok": True,
                    "running": self.server.collector.is_running(),  # type: ignore[attr-defined]
                    "last_sync_at": self.server.collector.last_sync_at,  # type: ignore[attr-defined]
                    "last_error": self.server.collector.last_error,  # type: ignore[attr-defined]
                    "counts": self.server.store.counts(),  # type: ignore[attr-defined]
                    "config": self.server.config.public_view(),  # type: ignore[attr-defined]
                }
            )
            return
        if self.path.startswith("/api/config"):
            self.send_json({"ok": True, "config": self.server.config.public_view()})  # type: ignore[attr-defined]
            return
        if self.path.startswith("/api/conversations"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            reply_status = query.get("reply_status", ["all"])[0]
            self.send_json(  # type: ignore[attr-defined]
                {"ok": True, "items": self.server.store.list_conversations(reply_status)}
            )
            return
        if self.path.startswith("/api/messages"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            conversation_id = int(query.get("conversation_id", ["0"])[0] or 0)
            talker_id = query.get("talker_id", [None])[0]
            self.send_json(
                {
                    "ok": True,
                    "items": self.server.store.list_messages(conversation_id or None, talker_id),  # type: ignore[attr-defined]
                }
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path.startswith("/api/config"):
                body = self.read_json()
                patch: dict[str, Any] = {}
                for key, value in body.items():
                    if value == "":
                        continue
                    patch[key] = value
                self.server.config.apply_patch(patch)  # type: ignore[attr-defined]
                if self.server.config.mock_mode and self.server.config.has_auth():  # type: ignore[attr-defined]
                    self.server.config.mock_mode = False  # type: ignore[attr-defined]
                save_config_file(self.server.config_path, self.server.config)  # type: ignore[attr-defined]
                self.send_json({"ok": True, "config": self.server.config.public_view()})  # type: ignore[attr-defined]
                return
            if self.path.startswith("/api/auth/check"):
                if not self.server.config.has_auth():  # type: ignore[attr-defined]
                    raise RuntimeError("请先保存 SESSDATA 或 access_key")
                response = self.server.collector.client.auth_check()  # type: ignore[attr-defined]
                data = response.get("data") if isinstance(response, dict) else {}
                if isinstance(data, dict) and data.get("mid") and not self.server.config.self_uid:  # type: ignore[attr-defined]
                    self.server.config.self_uid = str(data["mid"])  # type: ignore[attr-defined]
                    save_config_file(self.server.config_path, self.server.config)  # type: ignore[attr-defined]
                self.send_json({"ok": True, "response": response, "config": self.server.config.public_view()})  # type: ignore[attr-defined]
                return
            if self.path.startswith("/api/history/backfill"):
                body = self.read_json()
                talker_id = str(body.get("talker_id") or "")
                force = bool(body.get("force"))
                targets: list[str] = []
                if talker_id:
                    targets = [talker_id]
                else:
                    targets = [str(item["talker_id"]) for item in self.server.store.list_conversations()]  # type: ignore[attr-defined]
                inserted = 0
                seen = 0
                for target in targets[: self.server.config.history_sessions_per_sync]:  # type: ignore[attr-defined]
                    if force:
                        self.server.store.delete_state(f"history_backfilled:{target}")  # type: ignore[attr-defined]
                    result = self.server.collector.sync_history(target)  # type: ignore[attr-defined]
                    inserted += int(result["inserted"])
                    seen += int(result["seen"])
                self.send_json({"ok": True, "inserted": inserted, "seen": seen, "targets": len(targets)})
                return
            if self.path.startswith("/api/start"):
                self.server.collector.start()  # type: ignore[attr-defined]
                self.send_json({"ok": True})
                return
            if self.path.startswith("/api/stop"):
                self.server.collector.stop()  # type: ignore[attr-defined]
                self.send_json({"ok": True})
                return
            if self.path.startswith("/api/sync"):
                result = self.server.collector.sync_once()  # type: ignore[attr-defined]
                self.send_json({"ok": True, "result": result})
                return
            if self.path.startswith("/api/send") or self.path.startswith("/api/reply"):
                body = self.read_json()
                talker_id = str(body.get("talker_id") or body.get("sender_uid") or body.get("receiver_uid") or "")
                content = str(body.get("content") or body.get("message") or body.get("text") or "").strip()
                if not talker_id or not content:
                    raise ValueError("talker_id/sender_uid and content are required")
                outbox_id = self.server.store.create_outbox(talker_id, content)  # type: ignore[attr-defined]
                try:
                    if self.server.config.mock_mode and not self.server.config.has_auth():  # type: ignore[attr-defined]
                        response = {"mock": True, "code": 0}
                    else:
                        response = self.server.collector.client.send_text(talker_id, content)  # type: ignore[attr-defined]
                    self.server.store.update_outbox(outbox_id, "sent", response)  # type: ignore[attr-defined]
                    self.server.store.insert_message(  # type: ignore[attr-defined]
                        {
                            "msg_id": f"local-out-{outbox_id}",
                            "talker_id": talker_id,
                            "sender_uid": self.server.config.self_uid,  # type: ignore[attr-defined]
                            "direction": "outbound",
                            "msg_type": "text",
                            "content": content,
                            "timestamp": now_ts(),
                            "raw": {"outbox_id": outbox_id, "response": response},
                        }
                    )
                    self.send_json({"ok": True, "outbox_id": outbox_id, "response": response})
                except Exception as exc:  # noqa: BLE001
                    self.server.store.update_outbox(outbox_id, "failed", error=str(exc))  # type: ignore[attr-defined]
                    raise
                return
            if self.path.startswith("/api/mock/inbound"):
                body = self.read_json()
                talker_id = str(body.get("talker_id") or "10001")
                content = str(body.get("content") or f"本地模拟私信 {time.strftime('%H:%M:%S')}")
                nickname = str(body.get("nickname") or "本地测试用户")
                inserted = self.server.store.insert_message(  # type: ignore[attr-defined]
                    {
                        "msg_id": f"mock-{time.time_ns()}",
                        "talker_id": talker_id,
                        "sender_uid": talker_id,
                        "receiver_uid": self.server.config.self_uid,  # type: ignore[attr-defined]
                        "direction": "inbound",
                        "msg_type": "text",
                        "content": content,
                        "timestamp": now_ts(),
                        "display_name": nickname,
                        "raw": {"mock": True},
                    }
                )
                self.send_json({"ok": True, "inserted": inserted})
                return
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def send_json(self, data: Any, status: int = 200) -> None:
        self.send_text(json_dumps(data), "application/json; charset=utf-8", status=status)

    def send_text(self, text: str, content_type: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")


class AppServer(ThreadingHTTPServer):
    config_path: Path
    config: AppConfig
    store: Store
    collector: Collector


def create_example_config(path: Path) -> None:
    if path.exists():
        return
    example = {
        "host": "127.0.0.1",
        "port": 8765,
        "db_path": "data/bilibili_dm.sqlite3",
        "poll_interval_seconds": 45,
        "poll_jitter_seconds": 0,
        "request_timeout_seconds": 15,
        "self_uid": "",
        "sessdata": "",
        "bili_jct": "",
        "dedeuserid": "",
        "buvid3": "",
        "buvid4": "",
        "extra_cookie": "",
        "access_key": "",
        "auto_start": False,
        "mock_mode": True,
    }
    path.write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local Bilibili private-message collector")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    create_example_config(config_path)
    config = AppConfig.load(config_path)
    if not Path(config.db_path).is_absolute():
        config.db_path = str((APP_DIR / config.db_path).resolve())

    store = Store(config.db_path)
    collector = Collector(config, store)
    server = AppServer((config.host, config.port), ApiHandler)
    server.config_path = config_path
    server.config = config
    server.store = store
    server.collector = collector

    if config.auto_start:
        collector.start()

    url = f"http://{config.host}:{config.port}"
    print(f"Bilibili DM local collector is running: {url}")
    print(f"Config: {config_path.resolve()}")
    print(f"Database: {Path(config.db_path).resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        collector.stop()
        server.server_close()


if __name__ == "__main__":
    main()
