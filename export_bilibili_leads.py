#!/usr/bin/env python3
"""
Export Bilibili DM conversations as lead payloads.

The output fields follow the Bilibili section in `各平台线索接入文档.md`:
msg_id, sender_uid, sender_name, receiver_uid, msg_type, content, timestamp.

Extra lead fields are included for import/deduplication:
externalUserId, source, status, reply_status.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import bilibili_dm_local as app


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "bilibili_dm.sqlite3"
CONFIG_PATH = ROOT / "config.json"
OUT_DIR = ROOT / "data" / "exports"
STATE_DIR = ROOT / "data" / "state"
REPORT_STATE_PATH = STATE_DIR / "bilibili_report_uploaded_msg_ids.json"
STATUSES = ("all", "pending", "replied")
RUN_STAMP = time.strftime("%Y%m%d_%H%M%S")
IGNORED_REPORT_CONTENTS = {"UP主加油！看好你噢~"}
IGNORED_REPORT_SENDER_NAMES = {"哔哩哔哩智能机", "UP主小助手"}
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9]\d{9})(?!\d)")
TEL_RE = re.compile(r"(?<!\d)(0\d{2,3}[-\s]?\d{7,8})(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
COMPANY_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9（）()·\-]{2,40}"
    r"(?:有限责任公司|股份有限公司|有限公司|集团|工厂|厂|公司|工作室|中心|门店|店))"
)
NAME_PATTERNS = [
    re.compile(r"(?:我叫|本人|联系人|姓名|称呼|我是)\s*[:：]?\s*([\u4e00-\u9fff]{2,4})(?:先生|女士|小姐|经理|总)?"),
    re.compile(r"([\u4e00-\u9fff]{2,4})(?:先生|女士|小姐|经理|总)"),
]
INTENT_KEYWORDS = (
    "做",
    "生产",
    "厂家",
    "主营",
    "行业",
    "业务",
    "采购",
    "咨询",
    "了解",
    "需要",
    "定制",
    "合作",
    "报价",
    "价格",
    "产品",
    "设备",
    "项目",
    "传感",
    "测量",
    "测试",
)


def fetch_bili_name(client: app.BilibiliClient, uid: str, cache: dict[str, str]) -> str:
    if not uid:
        return ""
    if uid in cache:
        return cache[uid]
    try:
        payload = client._request(
            "GET",
            "https://api.bilibili.com/x/web-interface/card",
            params={"mid": uid, "photo": "false"},
        )
        data = payload.get("data") if isinstance(payload, dict) else {}
        card = data.get("card") if isinstance(data, dict) else {}
        name = str(card.get("name") or "") if isinstance(card, dict) else ""
    except Exception:
        name = ""
    cache[uid] = name
    return name


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_report_state() -> set[str]:
    if not REPORT_STATE_PATH.exists():
        return set()
    try:
        data = json.loads(REPORT_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if isinstance(data, list):
        return {str(item) for item in data}
    if isinstance(data, dict):
        return {str(item) for item in data.get("uploaded_msg_ids", [])}
    return set()


def save_report_state(uploaded_msg_ids: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": int(time.time()),
        "uploaded_msg_ids": sorted(uploaded_msg_ids),
    }
    REPORT_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_rows() -> list[dict[str, Any]]:
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
            SELECT *
            FROM (
                SELECT
                    m.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY m.conversation_id
                        ORDER BY m.timestamp DESC, m.id DESC
                    ) AS rn
                FROM messages m
                WHERE m.direction='inbound'
            )
            WHERE rn=1
        )
        SELECT
            c.id AS conversation_id,
            c.talker_id,
            c.display_name,
            c.avatar_url,
            COALESCE(s.last_inbound_at, 0) AS last_inbound_at,
            COALESCE(s.last_outbound_at, 0) AS last_outbound_at,
            COALESCE(s.inbound_count, 0) AS inbound_count,
            COALESCE(s.outbound_count, 0) AS outbound_count,
            CASE
                WHEN COALESCE(s.inbound_count, 0)=0 THEN 'no_inbound'
                WHEN COALESCE(s.last_inbound_at, 0) > COALESCE(s.last_outbound_at, 0) THEN 'pending'
                ELSE 'replied'
            END AS reply_status,
            li.msg_id,
            li.sender_uid,
            li.receiver_uid,
            li.msg_type,
            li.content,
            li.timestamp AS message_timestamp
        FROM conversations c
        LEFT JOIN stats s ON s.conversation_id=c.id
        LEFT JOIN latest_inbound li ON li.conversation_id=c.id
        WHERE COALESCE(s.inbound_count, 0) > 0
        ORDER BY reply_status ASC, last_inbound_at DESC, c.id DESC
    """
    with open_db() as conn:
        return [dict(row) for row in conn.execute(query).fetchall()]


def load_inbound_messages() -> dict[int, list[dict[str, Any]]]:
    query = """
        SELECT conversation_id, content, timestamp, msg_type
        FROM messages
        WHERE direction='inbound'
        ORDER BY conversation_id ASC, timestamp ASC, id ASC
    """
    messages: dict[int, list[dict[str, Any]]] = {}
    with open_db() as conn:
        for row in conn.execute(query):
            messages.setdefault(int(row["conversation_id"]), []).append(dict(row))
    return messages


def load_report_message_rows() -> list[dict[str, Any]]:
    query = """
        WITH stats AS (
            SELECT
                conversation_id,
                MAX(CASE WHEN direction='inbound' THEN timestamp ELSE NULL END) AS last_inbound_at,
                MAX(CASE WHEN direction='outbound' THEN timestamp ELSE NULL END) AS last_outbound_at,
                SUM(CASE WHEN direction='inbound' THEN 1 ELSE 0 END) AS inbound_count
            FROM messages
            GROUP BY conversation_id
        )
        SELECT
            m.conversation_id,
            c.talker_id,
            c.display_name,
            m.msg_id,
            m.sender_uid,
            m.receiver_uid,
            m.msg_type,
            m.content,
            m.timestamp,
            CASE
                WHEN COALESCE(s.inbound_count, 0)=0 THEN 'no_inbound'
                WHEN COALESCE(s.last_inbound_at, 0) > COALESCE(s.last_outbound_at, 0) THEN 'pending'
                ELSE 'replied'
            END AS reply_status
        FROM messages m
        JOIN conversations c ON c.id=m.conversation_id
        LEFT JOIN stats s ON s.conversation_id=m.conversation_id
        WHERE m.direction='inbound'
        ORDER BY m.timestamp DESC, m.id DESC
    """
    with open_db() as conn:
        return [dict(row) for row in conn.execute(query).fetchall()]


def build_leads(rows: list[dict[str, Any]], client: app.BilibiliClient) -> list[dict[str, Any]]:
    name_cache: dict[str, str] = {}
    self_uid = str(client.config.self_uid or "")
    leads: list[dict[str, Any]] = []
    for row in rows:
        sender_uid = str(row.get("sender_uid") or row.get("talker_id") or "")
        sender_name = str(row.get("display_name") or "") or fetch_bili_name(client, sender_uid, name_cache)
        if not sender_name:
            sender_name = f"B站用户_{sender_uid}"
        ts = int(row.get("message_timestamp") or row.get("last_inbound_at") or 0)
        if is_ignored_report_sender_name(sender_name):
            continue
        content = str(row.get("content") or "")
        if not content:
            content = "[空消息]"
        leads.append(
            {
                "reply_status": row["reply_status"],
                "externalUserId": f"bili_{sender_uid}",
                "source": "bilibili",
                "status": "new_lead",
                "conversation_id": row["conversation_id"],
                "talker_id": row["talker_id"],
                "msg_id": str(row.get("msg_id") or ""),
                "sender_uid": sender_uid,
                "sender_name": sender_name,
                "receiver_uid": str(row.get("receiver_uid") or self_uid),
                "msg_type": str(row.get("msg_type") or "text"),
                "content": content,
                "timestamp": ts * 1000 if ts and ts < 10_000_000_000 else ts,
                "message_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "",
                "last_inbound_at": row["last_inbound_at"],
                "last_outbound_at": row["last_outbound_at"],
                "inbound_count": row["inbound_count"],
                "outbound_count": row["outbound_count"],
            }
        )
    return leads


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def is_ignored_report_content(content: str) -> bool:
    return compact_text(content) in IGNORED_REPORT_CONTENTS


def is_ignored_report_sender_name(sender_name: str) -> bool:
    return compact_text(sender_name) in IGNORED_REPORT_SENDER_NAMES


def first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def extract_name(text: str) -> str:
    for pattern in NAME_PATTERNS:
        value = first_match(pattern, text)
        if value and value not in {"你好", "您好", "我们", "这个", "那个", "怎么", "可以", "需要", "咨询"}:
            return value
    return ""


def extract_intent(text: str) -> str:
    sentences = [part.strip() for part in re.split(r"[。！？!?；;\n\r]", text) if part.strip()]
    hits: list[str] = []
    for sentence in sentences:
        if any(keyword in sentence for keyword in INTENT_KEYWORDS):
            hits.append(sentence[:120])
        if len(hits) >= 3:
            break
    return "；".join(hits)


def extract_customer_info(lead: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, str]:
    text = compact_text("\n".join(str(item.get("content") or "") for item in messages))
    phone = first_match(PHONE_RE, text) or first_match(TEL_RE, text)
    email_match = EMAIL_RE.search(text)
    email = email_match.group(0) if email_match else ""
    company = first_match(COMPANY_RE, text)
    contact_name = extract_name(text)
    intent = extract_intent(text)
    latest_content = str(lead.get("content") or "")
    notes_parts = [
        f"B站昵称：{lead.get('sender_name') or ''}",
        f"B站UID：{lead.get('sender_uid') or ''}",
        f"回复状态：{lead.get('reply_status') or ''}",
    ]
    if intent:
        notes_parts.append(f"意向摘要：{intent}")
    if latest_content:
        notes_parts.append(f"最近来信：{latest_content}")

    flags = []
    for key, value in {
        "contact_name": contact_name,
        "company": company,
        "phone": phone,
        "email": email,
        "business": intent,
    }.items():
        if value:
            flags.append(key)

    return {
        "name": contact_name or str(lead.get("sender_name") or ""),
        "contact_name": contact_name,
        "company": company,
        "phone": phone,
        "email": email,
        "business": intent,
        "message": latest_content,
        "notes": "；".join(part for part in notes_parts if part),
        "extraction_flags": ",".join(flags),
    }


def build_customer_leads(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inbound_messages = load_inbound_messages()
    rows: list[dict[str, Any]] = []
    for lead in leads:
        info = extract_customer_info(lead, inbound_messages.get(int(lead["conversation_id"]), []))
        rows.append(
            {
                "name": info["name"],
                "contact_name": info["contact_name"],
                "company": info["company"],
                "phone": info["phone"],
                "email": info["email"],
                "business": info["business"],
                "message": info["message"],
                "source": "bilibili",
                "externalUserId": lead["externalUserId"],
                "status": lead["status"],
                "reply_status": lead["reply_status"],
                "sender_uid": lead["sender_uid"],
                "sender_name": lead["sender_name"],
                "msg_id": lead["msg_id"],
                "msg_type": lead["msg_type"],
                "timestamp": lead["timestamp"],
                "message_time": lead["message_time"],
                "notes": info["notes"],
                "extraction_flags": info["extraction_flags"],
            }
        )
    return rows


def build_report_messages(client: app.BilibiliClient) -> list[dict[str, Any]]:
    rows = load_report_message_rows()
    inbound_messages = load_inbound_messages()
    name_cache: dict[str, str] = {}
    report_messages: list[dict[str, Any]] = []
    for row in rows:
        sender_uid = str(row.get("sender_uid") or row.get("talker_id") or "")
        sender_name = str(row.get("display_name") or "") or fetch_bili_name(client, sender_uid, name_cache)
        if not sender_name:
            sender_name = f"B站用户_{sender_uid}"
        content = str(row.get("content") or "")
        if not content:
            content = "[空消息]"
        if is_ignored_report_sender_name(sender_name):
            continue
        if is_ignored_report_content(content):
            continue
        lead_for_extract = {
            "sender_uid": sender_uid,
            "sender_name": sender_name,
            "reply_status": row.get("reply_status") or "",
            "content": content,
        }
        info = extract_customer_info(
            lead_for_extract,
            inbound_messages.get(int(row["conversation_id"]), []),
        )
        timestamp = int(row.get("timestamp") or 0)
        item = {
            "sender_uid": sender_uid,
            "sender_name": sender_name,
            "content": content,
            "msg_type": str(row.get("msg_type") or "text"),
            "direction": "receive",
            "timestamp": timestamp * 1000 if timestamp and timestamp < 10_000_000_000 else timestamp,
        }
        if info.get("company"):
            item["company"] = info["company"]
        if info.get("phone"):
            item["phone"] = info["phone"]
        item["_reply_status"] = row.get("reply_status") or ""
        item["_msg_id"] = str(row.get("msg_id") or "")
        report_messages.append(item)
    return report_messages


def filter_new_report_messages(messages: list[dict[str, Any]], uploaded_msg_ids: set[str]) -> list[dict[str, Any]]:
    return [message for message in messages if str(message.get("_msg_id") or "") not in uploaded_msg_ids]


def write_outputs(status: str, leads: list[dict[str, Any]]) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"bilibili_leads_{status}_{RUN_STAMP}.json"
    csv_path = OUT_DIR / f"bilibili_leads_{status}_{RUN_STAMP}.csv"
    json_path.write_text(json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8")
    fieldnames = [
        "reply_status",
        "externalUserId",
        "source",
        "status",
        "conversation_id",
        "talker_id",
        "msg_id",
        "sender_uid",
        "sender_name",
        "receiver_uid",
        "msg_type",
        "content",
        "timestamp",
        "message_time",
        "last_inbound_at",
        "last_outbound_at",
        "inbound_count",
        "outbound_count",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(leads)
    return json_path, csv_path


def write_report_message_outputs(status: str, messages: list[dict[str, Any]]) -> dict[str, str]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    clean_messages = []
    for message in messages:
        item = dict(message)
        item.pop("_reply_status", None)
        item.pop("_msg_id", None)
        clean_messages.append(item)

    batch_json_path = OUT_DIR / f"bilibili_report_messages_{status}_{RUN_STAMP}.json"
    jsonl_path = OUT_DIR / f"bilibili_report_messages_{status}_{RUN_STAMP}.jsonl"
    csv_path = OUT_DIR / f"bilibili_report_messages_{status}_{RUN_STAMP}.csv"

    batch_json_path.write_text(
        json.dumps({"messages": clean_messages}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with jsonl_path.open("w", encoding="utf-8", newline="") as f:
        for message in clean_messages:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    fieldnames = [
        "sender_uid",
        "sender_name",
        "content",
        "msg_type",
        "direction",
        "timestamp",
        "company",
        "phone",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(clean_messages)

    return {
        "batch_json": str(batch_json_path),
        "single_jsonl": str(jsonl_path),
        "csv": str(csv_path),
    }


def public_report_message(message: dict[str, Any]) -> dict[str, Any]:
    item = dict(message)
    item.pop("_reply_status", None)
    item.pop("_msg_id", None)
    return item


def mark_report_messages_uploaded(messages: list[dict[str, Any]]) -> int:
    uploaded_msg_ids = load_report_state()
    before = len(uploaded_msg_ids)
    uploaded_msg_ids.update(
        str(message.get("_msg_id") or "")
        for message in messages
        if message.get("_msg_id")
    )
    save_report_state(uploaded_msg_ids)
    return len(uploaded_msg_ids) - before


def export_new_report_messages(client: app.BilibiliClient, reply_status: str = "all") -> list[dict[str, Any]]:
    report_messages = build_report_messages(client)
    new_messages = filter_new_report_messages(report_messages, load_report_state())
    if reply_status == "all":
        return new_messages
    return [message for message in new_messages if message.get("_reply_status") == reply_status]


def write_customer_outputs(status: str, leads: list[dict[str, Any]]) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"bilibili_customer_leads_{status}_{RUN_STAMP}.json"
    csv_path = OUT_DIR / f"bilibili_customer_leads_{status}_{RUN_STAMP}.csv"
    json_path.write_text(json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8")
    fieldnames = [
        "name",
        "contact_name",
        "company",
        "phone",
        "email",
        "business",
        "message",
        "source",
        "externalUserId",
        "status",
        "reply_status",
        "sender_uid",
        "sender_name",
        "msg_id",
        "msg_type",
        "timestamp",
        "message_time",
        "notes",
        "extraction_flags",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(leads)
    return json_path, csv_path


def main() -> None:
    config = app.AppConfig.load(CONFIG_PATH)
    client = app.BilibiliClient(config)
    leads = build_leads(load_rows(), client)
    report_messages = build_report_messages(client)
    uploaded_msg_ids = load_report_state()
    new_report_messages = filter_new_report_messages(report_messages, uploaded_msg_ids)
    outputs: dict[str, dict[str, Any]] = {}
    for status in STATUSES:
        filtered = leads if status == "all" else [lead for lead in leads if lead["reply_status"] == status]
        filtered_report_messages = (
            report_messages
            if status == "all"
            else [message for message in report_messages if message.get("_reply_status") == status]
        )
        filtered_new_report_messages = (
            new_report_messages
            if status == "all"
            else [message for message in new_report_messages if message.get("_reply_status") == status]
        )
        json_path, csv_path = write_outputs(status, filtered)
        customer_leads = build_customer_leads(filtered)
        customer_json_path, customer_csv_path = write_customer_outputs(status, customer_leads)
        report_paths = write_report_message_outputs(status, filtered_report_messages)
        new_report_paths = write_report_message_outputs(f"new_{status}", filtered_new_report_messages)
        outputs[status] = {
            "count": len(filtered),
            "message_count": len(filtered_report_messages),
            "new_message_count": len(filtered_new_report_messages),
            "json": str(json_path),
            "csv": str(csv_path),
            "customer_json": str(customer_json_path),
            "customer_csv": str(customer_csv_path),
            "report_batch_json": report_paths["batch_json"],
            "report_single_jsonl": report_paths["single_jsonl"],
            "report_csv": report_paths["csv"],
            "new_report_batch_json": new_report_paths["batch_json"],
            "new_report_single_jsonl": new_report_paths["single_jsonl"],
            "new_report_csv": new_report_paths["csv"],
        }
    mark_report_messages_uploaded(new_report_messages)
    print(json.dumps(outputs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
