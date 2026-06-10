#!/usr/bin/env python3
"""
Auto-reply to unreplied Bilibili private-message conversations.

Default mode is dry-run. Use --send to actually send messages.
Already auto-replied talker IDs are stored locally to prevent duplicate replies.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
from pathlib import Path
from typing import Any

import bilibili_dm_local as app
import export_bilibili_leads as export


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_DIR = ROOT / "data" / "state"
AUTO_REPLY_STATE_PATH = STATE_DIR / "bilibili_auto_replied_talker_ids.json"
DEFAULT_REPLY = """您好呀!欢迎咨询[矩侨工业]为了方便为您推荐最适合的解决方案和对接人员，可以简单描述一下问题吗?我会安排人员与您联系。
1.您公司名称:
2.联系手机号/微信:
3.主要咨询业务方向(机器人/养老医疗/家具/汽车等)
4.您的基础需求是:"""


def load_state() -> set[str]:
    if not AUTO_REPLY_STATE_PATH.exists():
        return set()
    try:
        data = json.loads(AUTO_REPLY_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if isinstance(data, list):
        return {str(item) for item in data}
    if isinstance(data, dict):
        return {str(item) for item in data.get("talker_ids", [])}
    return set()


def save_state(talker_ids: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    AUTO_REPLY_STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": int(time.time()),
                "talker_ids": sorted(talker_ids),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_pending_conversations(db_path: str) -> list[dict[str, Any]]:
    query = """
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
        latest_inbound AS (
            SELECT conversation_id, content
            FROM (
                SELECT
                    conversation_id,
                    content,
                    ROW_NUMBER() OVER (
                        PARTITION BY conversation_id
                        ORDER BY timestamp DESC, id DESC
                    ) AS rn
                FROM messages
                WHERE direction='inbound'
            )
            WHERE rn=1
        )
        SELECT
            c.id AS conversation_id,
            c.talker_id,
            c.display_name,
            c.last_message,
            COALESCE(li.content, '') AS latest_inbound_content,
            COALESCE(s.last_inbound_at, 0) AS last_inbound_at,
            COALESCE(s.last_outbound_at, 0) AS last_outbound_at,
            COALESCE(s.inbound_count, 0) AS inbound_count,
            COALESCE(s.outbound_count, 0) AS outbound_count
        FROM conversations c
        JOIN stats s ON s.conversation_id=c.id
        LEFT JOIN latest_inbound li ON li.conversation_id=c.id
        WHERE COALESCE(s.inbound_count, 0) > 0
          AND COALESCE(s.last_inbound_at, 0) > COALESCE(s.last_outbound_at, 0)
        ORDER BY s.last_inbound_at ASC, c.id ASC
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(query).fetchall()]
    finally:
        conn.close()


def record_outbound_message(store: app.Store, config: app.AppConfig, talker_id: str, content: str, response: Any) -> None:
    outbox_id = store.create_outbox(talker_id, content)
    store.update_outbox(outbox_id, "sent", response)
    store.insert_message(
        {
            "msg_id": f"auto-out-{outbox_id}",
            "talker_id": talker_id,
            "sender_uid": config.self_uid,
            "receiver_uid": talker_id,
            "direction": "outbound",
            "msg_type": "text",
            "content": content,
            "timestamp": int(time.time()),
            "raw": {"outbox_id": outbox_id, "auto_reply": True, "response": response},
        }
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-reply to pending Bilibili conversations")
    parser.add_argument("--send", action="store_true", help="Actually send replies. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=0, help="Max conversations to reply. 0 means no limit.")
    parser.add_argument("--delay-min", type=float, default=8.0, help="Minimum delay between sends.")
    parser.add_argument("--delay-max", type=float, default=18.0, help="Maximum delay between sends.")
    parser.add_argument("--message", default=DEFAULT_REPLY, help="Reply message text.")
    parser.add_argument("--include-already-auto-replied", action="store_true", help="Ignore local auto-reply state.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = app.AppConfig.load(CONFIG_PATH)
    if not Path(config.db_path).is_absolute():
        config.db_path = str((ROOT / config.db_path).resolve())
    store = app.Store(config.db_path)
    client = app.BilibiliClient(config)
    auto_replied = load_state()
    pending = load_pending_conversations(config.db_path)
    targets = []
    ignored = []
    for item in pending:
        talker_id = str(item["talker_id"])
        if export.is_ignored_report_content(str(item.get("latest_inbound_content") or "")):
            ignored.append(talker_id)
            continue
        if not args.include_already_auto_replied and talker_id in auto_replied:
            continue
        targets.append(item)
    if args.limit > 0:
        targets = targets[: args.limit]

    result: dict[str, Any] = {
        "dry_run": not args.send,
        "pending_total": len(pending),
        "ignored_total": len(ignored),
        "targets": len(targets),
        "state_file": str(AUTO_REPLY_STATE_PATH),
        "talker_ids": [str(item["talker_id"]) for item in targets],
    }

    if not args.send:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    sent: list[str] = []
    failed: list[dict[str, str]] = []
    for index, item in enumerate(targets):
        talker_id = str(item["talker_id"])
        try:
            response = client.send_text(talker_id, args.message)
            record_outbound_message(store, config, talker_id, args.message, response)
            auto_replied.add(talker_id)
            save_state(auto_replied)
            sent.append(talker_id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"talker_id": talker_id, "error": str(exc)})
        if index < len(targets) - 1:
            time.sleep(random.uniform(args.delay_min, args.delay_max))

    result.update({"sent": sent, "failed": failed})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
