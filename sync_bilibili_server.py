#!/usr/bin/env python3
"""
Local bridge between Bilibili private messages and a customer-service server.

Flow:
1. Poll Bilibili and store new private messages locally.
2. Upload only new inbound messages to the server.
3. Pull pending reply tasks from the server.
4. Optionally send those replies through Bilibili from this local machine.
5. Report each reply task result back to the server.

The server never needs to call the local computer. This script only makes
outbound HTTP requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import bilibili_dm_local as app
import export_bilibili_leads as export


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "data" / "server_sync_logs"
STATE_DIR = ROOT / "data" / "state"
REPLY_STATE_PATH = STATE_DIR / "bilibili_server_reply_task_ids.json"
SEND_COOLDOWN_STATE_PATH = STATE_DIR / "bilibili_send_cooldown.json"


def load_raw_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}


def config_value(raw: dict[str, Any], key: str, default: Any = "") -> Any:
    env_name = f"BILI_{key.upper()}"
    if os.environ.get(env_name) is not None:
        return os.environ[env_name]
    return raw.get(key, default)


def json_request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    api_key: str = "",
    bearer_token: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "bilibili-local-server-sync/1.0",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if api_key:
        headers["X-API-Key"] = api_key
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} HTTP {exc.code}: {raw[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc

    text = raw.strip()
    if not text:
        body: Any = {}
    else:
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {"raw": raw}
    return {"status": status, "body": body}


def validate_json_api_response(result: dict[str, Any], action: str) -> None:
    body = result.get("body")
    if isinstance(body, dict) and "raw" in body:
        raw = str(body.get("raw") or "").lstrip()
        if raw.startswith("<!doctype") or raw.startswith("<html") or "<html" in raw[:500].lower():
            raise RuntimeError(f"{action} returned HTML instead of JSON API response")
        raise RuntimeError(f"{action} returned non-JSON response: {raw[:300]}")
    if isinstance(body, dict) and body.get("ok") is False:
        raise RuntimeError(f"{action} returned ok=false: {json.dumps(body, ensure_ascii=False)[:500]}")


def post_json(
    url: str,
    payload: dict[str, Any],
    api_key: str = "",
    bearer_token: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    return json_request("POST", url, payload, api_key=api_key, bearer_token=bearer_token, timeout=timeout)


def post_json_with_retry(
    url: str,
    payload: dict[str, Any],
    api_key: str = "",
    bearer_token: str = "",
    timeout: int = 30,
    retries: int = 3,
    retry_delay: float = 3.0,
) -> dict[str, Any]:
    last_error: Exception | None = None
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        try:
            result = post_json(
                url,
                payload,
                api_key=api_key,
                bearer_token=bearer_token,
                timeout=timeout,
            )
            validate_json_api_response(result, "POST")
            result["attempt"] = attempt
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts:
                time.sleep(max(0.1, retry_delay) * attempt)
    raise RuntimeError(str(last_error)) from last_error


def get_json(
    url: str,
    api_key: str = "",
    bearer_token: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    return json_request("GET", url, None, api_key=api_key, bearer_token=bearer_token, timeout=timeout)


def write_snapshot(prefix: str, payload: dict[str, Any]) -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    chunk_size = max(1, size)
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


def load_reply_state() -> set[str]:
    if not REPLY_STATE_PATH.exists():
        return set()
    try:
        data = json.loads(REPLY_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if isinstance(data, list):
        return {str(item) for item in data}
    if isinstance(data, dict):
        return {str(item) for item in data.get("sent_task_ids", [])}
    return set()


def save_reply_state(sent_task_ids: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPLY_STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": int(time.time()),
                "sent_task_ids": sorted(sent_task_ids),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_send_cooldown() -> dict[str, Any]:
    if not SEND_COOLDOWN_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(SEND_COOLDOWN_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_send_cooldown(until_ts: int, reason: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEND_COOLDOWN_STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": int(time.time()),
                "until_ts": int(until_ts),
                "reason": reason,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def active_send_cooldown() -> dict[str, Any] | None:
    data = load_send_cooldown()
    until_ts = int(data.get("until_ts") or 0)
    now = int(time.time())
    if until_ts > now:
        return {
            "until_ts": until_ts,
            "remaining_seconds": until_ts - now,
            "reason": str(data.get("reason") or ""),
        }
    return None


def is_bilibili_banned_error(error: Exception | str) -> bool:
    text = str(error)
    return "request was banned" in text or '"code":-412' in text or "HTTP 412" in text


def first_value(data: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = data.get(name)
        if value is not None and value != "":
            return str(value)
    return ""


def extract_reply_tasks(response_body: Any) -> list[dict[str, Any]]:
    if isinstance(response_body, list):
        items = response_body
    elif isinstance(response_body, dict):
        data = response_body.get("data")
        if isinstance(data, dict):
            items = data.get("replies") or data.get("items") or data.get("tasks") or []
        elif isinstance(data, list):
            items = data
        else:
            items = response_body.get("replies") or response_body.get("items") or response_body.get("tasks") or []
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def normalize_reply_task(task: dict[str, Any]) -> dict[str, str]:
    task_id = first_value(task, ("reply_id", "task_id", "id", "outbox_id", "message_id"))
    talker_id = first_value(task, ("sender_uid", "talker_id", "receiver_uid", "uid", "user_id", "to_uid"))
    content = first_value(task, ("content", "message", "text", "reply_content"))
    if not task_id and talker_id and content:
        task_id = hashlib.sha256(f"{talker_id}\n{content}".encode("utf-8")).hexdigest()
    return {
        "task_id": task_id,
        "talker_id": talker_id,
        "content": content.strip(),
    }


def record_outbound_message(
    store: app.Store,
    config: app.AppConfig,
    talker_id: str,
    content: str,
    response: Any,
    task_id: str,
) -> int:
    outbox_id = store.create_outbox(talker_id, content)
    store.update_outbox(outbox_id, "sent", response)
    store.insert_message(
        {
            "msg_id": f"server-out-{outbox_id}",
            "talker_id": talker_id,
            "sender_uid": config.self_uid,
            "receiver_uid": talker_id,
            "direction": "outbound",
            "msg_type": "text",
            "content": content,
            "timestamp": int(time.time()),
            "raw": {"outbox_id": outbox_id, "server_reply_task_id": task_id, "response": response},
        }
    )
    return outbox_id


def load_latest_inbound_contents(db_path: str) -> dict[str, str]:
    query = """
        SELECT talker_id, content
        FROM (
            SELECT
                talker_id,
                content,
                ROW_NUMBER() OVER (
                    PARTITION BY talker_id
                    ORDER BY timestamp DESC, id DESC
                ) AS rn
            FROM messages
            WHERE direction='inbound'
        )
        WHERE rn=1
    """
    with sqlite3.connect(db_path) as conn:
        return {str(row[0]): str(row[1] or "") for row in conn.execute(query).fetchall()}


def build_reply_update(
    task: dict[str, str],
    status: str,
    outbox_id: int | None = None,
    response: Any = None,
    error: str = "",
    skipped: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reply_id": task["task_id"],
        "task_id": task["task_id"],
        "sender_uid": task["talker_id"],
        "talker_id": task["talker_id"],
        "content": task["content"],
        "status": status,
        "updated_at": int(time.time() * 1000),
    }
    if status == "sent":
        payload["sent_at"] = payload["updated_at"]
        payload["platform"] = "bilibili"
        payload["platform_status"] = "sent"
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict) and data.get("msg_key") is not None:
            payload["bilibili_msg_key"] = str(data.get("msg_key"))
            payload["platform_msg_id"] = str(data.get("msg_key"))
    if outbox_id is not None:
        payload["outbox_id"] = outbox_id
    if response is not None:
        payload["response"] = response
    if error:
        payload["error"] = error
    if skipped:
        payload["skipped"] = True
    return payload


def upload_new_messages(
    config: app.AppConfig,
    client: app.BilibiliClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    internal_messages = export.export_new_report_messages(client, args.reply_status)
    if not internal_messages:
        return {"ok": True, "uploaded": 0, "skipped": "no_new_messages"}

    public_messages = [export.public_report_message(message) for message in internal_messages]
    payload = {"messages": public_messages}
    if args.dry_run or not args.upload_url:
        suffix = "upload_dry_run" if args.dry_run else "upload_no_url"
        file_path = write_snapshot(suffix, payload)
        return {
            "ok": bool(args.dry_run),
            "uploaded": 0,
            "count": len(public_messages),
            "file": file_path,
            "error": "" if args.dry_run else "Missing upload URL.",
        }

    internal_batches = chunks(internal_messages, args.upload_batch_size)
    uploaded = 0
    marked = 0
    batch_results: list[dict[str, Any]] = []
    failed_batches: list[dict[str, Any]] = []

    for batch_index, internal_batch in enumerate(internal_batches, start=1):
        public_batch = [export.public_report_message(message) for message in internal_batch]
        batch_payload = {"messages": public_batch}
        try:
            result = post_json_with_retry(
                args.upload_url,
                batch_payload,
                api_key=args.upload_api_key,
                bearer_token=args.bearer_token,
                timeout=config.request_timeout_seconds,
                retries=args.upload_retries,
                retry_delay=args.upload_retry_delay,
            )
            marked_now = export.mark_report_messages_uploaded(internal_batch)
            uploaded += len(public_batch)
            marked += marked_now
            batch_results.append(
                {
                    "batch": batch_index,
                    "count": len(public_batch),
                    "status": result["status"],
                    "attempt": result.get("attempt"),
                    "marked_uploaded": marked_now,
                }
            )
        except Exception as exc:  # noqa: BLE001
            failed_batches.append({"batch": batch_index, "count": len(public_batch), "error": str(exc)})
            break

        if args.upload_batch_delay > 0 and batch_index < len(internal_batches):
            time.sleep(args.upload_batch_delay)

    file_path = write_snapshot(
        "upload_batches",
        {
            "total_new": len(internal_messages),
            "uploaded": uploaded,
            "marked_uploaded": marked,
            "batch_size": args.upload_batch_size,
            "batches": batch_results,
            "failed_batches": failed_batches,
        },
    )
    if failed_batches:
        return {
            "ok": False,
            "uploaded": uploaded,
            "marked_uploaded": marked,
            "failed_batches": failed_batches,
            "file": file_path,
        }
    return {
        "ok": True,
        "uploaded": uploaded,
        "marked_uploaded": marked,
        "batches": len(batch_results),
        "file": file_path,
    }


def report_reply_update(
    args: argparse.Namespace,
    config: app.AppConfig,
    update_payload: dict[str, Any],
) -> dict[str, Any]:
    if not args.reply_update_url:
        return {"ok": False, "skipped": "missing_reply_update_url"}
    if args.dry_run:
        return {"ok": True, "dry_run": True, "payload": update_payload}
    try:
        result = post_json(
            args.reply_update_url,
            update_payload,
            api_key=args.reply_api_key,
            bearer_token=args.bearer_token,
            timeout=config.request_timeout_seconds,
        )
        validate_json_api_response(result, "reply update")
        write_snapshot("reply_update", {"request": update_payload, "result": result})
        return result
    except Exception as exc:
        write_snapshot("reply_update_failed", {"request": update_payload, "error": str(exc)})
        raise


def process_reply_tasks(
    config: app.AppConfig,
    store: app.Store,
    client: app.BilibiliClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if not args.reply_pending_url:
        return {"ok": True, "skipped": "missing_reply_pending_url", "tasks": 0}

    response = get_json(
        args.reply_pending_url,
        api_key=args.reply_api_key,
        bearer_token=args.bearer_token,
        timeout=config.request_timeout_seconds,
    )
    validate_json_api_response(response, "reply pending")
    tasks = [normalize_reply_task(item) for item in extract_reply_tasks(response["body"])]
    tasks = [task for task in tasks if task["talker_id"] and task["content"]]
    if args.reply_limit > 0:
        tasks = tasks[: args.reply_limit]

    sent_task_ids = load_reply_state()
    latest_inbound_contents = load_latest_inbound_contents(config.db_path)
    results: list[dict[str, Any]] = []
    cooldown = active_send_cooldown()
    if cooldown and args.send_replies:
        return {
            "ok": True,
            "tasks": len(tasks),
            "skipped": "bilibili_send_cooldown",
            "cooldown": cooldown,
            "results": [],
        }

    for index, task in enumerate(tasks):
        latest_content = latest_inbound_contents.get(task["talker_id"], "")
        if export.is_ignored_report_content(latest_content):
            update_payload = build_reply_update(
                task,
                "failed",
                error="ignored latest inbound content",
                skipped=True,
            )
            update_result = report_reply_update(args, config, update_payload)
            results.append(
                {
                    "task_id": task["task_id"],
                    "talker_id": task["talker_id"],
                    "status": "skipped_ignored_content",
                    "update": update_result,
                }
            )
            continue
        if task["task_id"] in sent_task_ids:
            update_payload = build_reply_update(task, "sent", skipped=True)
            update_result = report_reply_update(args, config, update_payload)
            results.append({"task_id": task["task_id"], "status": "already_sent", "update": update_result})
            continue

        if not args.send_replies:
            results.append({"task_id": task["task_id"], "status": "dry_run_not_sent", "talker_id": task["talker_id"]})
            continue

        try:
            bili_response = client.send_text(task["talker_id"], task["content"])
            outbox_id = record_outbound_message(store, config, task["talker_id"], task["content"], bili_response, task["task_id"])
            sent_task_ids.add(task["task_id"])
            save_reply_state(sent_task_ids)
            update_payload = build_reply_update(task, "sent", outbox_id=outbox_id, response=bili_response)
            update_result = report_reply_update(args, config, update_payload)
            results.append(
                {
                    "task_id": task["task_id"],
                    "talker_id": task["talker_id"],
                    "status": "sent",
                    "outbox_id": outbox_id,
                    "update": update_result,
                }
            )
        except Exception as exc:  # noqa: BLE001
            update_payload = build_reply_update(task, "failed", error=str(exc))
            update_result = report_reply_update(args, config, update_payload)
            results.append(
                {
                    "task_id": task["task_id"],
                    "talker_id": task["talker_id"],
                    "status": "failed",
                    "error": str(exc),
                    "update": update_result,
                }
            )
            if is_bilibili_banned_error(exc):
                save_send_cooldown(
                    int(time.time()) + int(args.bilibili_ban_cooldown_seconds),
                    str(exc),
                )
                results.append(
                    {
                        "status": "bilibili_send_cooldown_started",
                        "cooldown_seconds": int(args.bilibili_ban_cooldown_seconds),
                    }
                )
                break

        if args.send_replies and index < len(tasks) - 1:
            time.sleep(random.uniform(args.reply_delay_min, args.reply_delay_max))

    return {"ok": True, "tasks": len(tasks), "results": results}


def run_once(config: app.AppConfig, args: argparse.Namespace) -> dict[str, Any]:
    export.DB_PATH = Path(config.db_path)
    store = app.Store(config.db_path)
    collector = app.Collector(config, store)
    client = collector.client

    sync_result: dict[str, Any] | None = None
    if args.sync_first:
        sync_result = collector.sync_once()

    upload_result = {"ok": True, "skipped": "disabled"}
    if not args.no_upload:
        upload_result = upload_new_messages(config, client, args)

    reply_result = {"ok": True, "skipped": "disabled", "tasks": 0}
    if not args.no_replies:
        reply_result = process_reply_tasks(config, store, client, args)

    return {
        "ok": bool(upload_result.get("ok", True)) and bool(reply_result.get("ok", True)),
        "sync": sync_result,
        "upload": upload_result,
        "replies": reply_result,
    }


def next_wait_seconds(interval: int, jitter: int) -> int:
    offset = random.randint(-max(0, jitter), max(0, jitter)) if jitter else 0
    return max(10, interval + offset)


def parse_args() -> argparse.Namespace:
    raw = load_raw_config()
    parser = argparse.ArgumentParser(description="Sync Bilibili DMs with a customer-service server")
    parser.add_argument("--upload-url", default=str(config_value(raw, "report_webhook_url", "") or ""))
    parser.add_argument("--upload-api-key", default=str(config_value(raw, "report_api_key", "") or ""))
    parser.add_argument("--reply-pending-url", default=str(config_value(raw, "reply_pending_url", "") or ""))
    parser.add_argument("--reply-update-url", default=str(config_value(raw, "reply_update_url", "") or ""))
    parser.add_argument(
        "--reply-api-key",
        default=str(config_value(raw, "reply_api_key", config_value(raw, "report_api_key", "")) or ""),
    )
    parser.add_argument("--bearer-token", default=str(config_value(raw, "server_bearer_token", "") or ""))
    parser.add_argument("--interval", type=int, default=int(config_value(raw, "server_sync_interval_seconds", 600) or 600))
    parser.add_argument("--jitter", type=int, default=int(config_value(raw, "server_sync_jitter_seconds", 80) or 80))
    parser.add_argument("--upload-batch-size", type=int, default=int(config_value(raw, "upload_batch_size", 20) or 20))
    parser.add_argument("--upload-retries", type=int, default=int(config_value(raw, "upload_retries", 3) or 3))
    parser.add_argument(
        "--upload-retry-delay",
        type=float,
        default=float(config_value(raw, "upload_retry_delay_seconds", 3) or 3),
    )
    parser.add_argument(
        "--upload-batch-delay",
        type=float,
        default=float(config_value(raw, "upload_batch_delay_seconds", 0.5) or 0.5),
    )
    parser.add_argument("--reply-limit", type=int, default=int(config_value(raw, "reply_limit_per_sync", 20) or 20))
    parser.add_argument("--reply-delay-min", type=float, default=8.0)
    parser.add_argument("--reply-delay-max", type=float, default=18.0)
    parser.add_argument(
        "--bilibili-ban-cooldown-seconds",
        type=int,
        default=int(config_value(raw, "bilibili_ban_cooldown_seconds", 3600) or 3600),
    )
    parser.add_argument("--reply-status", choices=["all", "pending", "replied"], default="all")
    parser.add_argument("--send-replies", action="store_true", help="Actually send server reply tasks to Bilibili.")
    parser.add_argument("--dry-run", action="store_true", help="Do not POST uploads or send Bilibili replies.")
    parser.add_argument("--loop", action="store_true", help="Run forever.")
    parser.add_argument("--once", action="store_true", help="Run once and exit.")
    parser.add_argument("--no-upload", action="store_true", help="Skip uploading new messages.")
    parser.add_argument("--no-replies", action="store_true", help="Skip pulling and sending server replies.")
    parser.add_argument("--sync-first", action="store_true", default=True)
    parser.add_argument("--no-sync-first", action="store_false", dest="sync_first")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = app.AppConfig.load(CONFIG_PATH)
    if not Path(config.db_path).is_absolute():
        config.db_path = str((ROOT / config.db_path).resolve())

    while True:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = run_once(config, args)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        print(json.dumps({"time": started, **result}, ensure_ascii=False, indent=2))

        if args.once or not args.loop:
            break
        wait_seconds = next_wait_seconds(args.interval, args.jitter)
        print(json.dumps({"next_run_in_seconds": wait_seconds}, ensure_ascii=False))
        time.sleep(wait_seconds)


if __name__ == "__main__":
    main()
