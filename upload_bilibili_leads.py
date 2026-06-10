#!/usr/bin/env python3
"""
Upload new Bilibili private messages to a server on a fixed interval.

The server receives:

    {"messages": [{sender_uid, sender_name, content, msg_type, direction, timestamp, ...}]}

Messages are marked uploaded only after the HTTP request returns 2xx.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import bilibili_dm_local as app
import export_bilibili_leads as export


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "data" / "upload_logs"


def get_config_value(config: app.AppConfig, key: str, default: Any = "") -> Any:
    env_name = f"BILI_{key.upper()}"
    if os.environ.get(env_name) is not None:
        return os.environ[env_name]
    path = CONFIG_PATH
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if key in raw:
                return raw[key]
        except json.JSONDecodeError:
            pass
    return getattr(config, key, default)


def post_json(url: str, payload: dict[str, Any], api_key: str = "", timeout: int = 30) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "bilibili-local-lead-uploader/1.0",
    }
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Upload HTTP {exc.code}: {raw[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Upload request failed: {exc}") from exc
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {"raw": raw}
    return {"status": status, "response": data}


def write_upload_snapshot(payload: dict[str, Any], suffix: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"bilibili_upload_{time.strftime('%Y%m%d_%H%M%S')}_{suffix}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_once(config: app.AppConfig, args: argparse.Namespace) -> dict[str, Any]:
    store = app.Store(config.db_path)
    collector = app.Collector(config, store)
    if args.sync_first:
        collector.sync_once()

    client = app.BilibiliClient(config)
    internal_messages = export.export_new_report_messages(client, args.reply_status)
    public_messages = [export.public_report_message(message) for message in internal_messages]
    payload = {"messages": public_messages}

    if not public_messages:
        return {"ok": True, "uploaded": 0, "skipped": "no_new_messages"}

    if args.dry_run:
        snapshot = write_upload_snapshot(payload, "dry_run")
        return {"ok": True, "uploaded": 0, "dry_run": True, "count": len(public_messages), "file": str(snapshot)}

    if not args.url:
        snapshot = write_upload_snapshot(payload, "no_url")
        return {
            "ok": False,
            "uploaded": 0,
            "error": "Missing upload URL. Set report_webhook_url in config.json or pass --url.",
            "file": str(snapshot),
        }

    result = post_json(args.url, payload, api_key=args.api_key, timeout=args.timeout)
    marked = export.mark_report_messages_uploaded(internal_messages)
    snapshot = write_upload_snapshot(
        {
            "request": payload,
            "upload_result": result,
            "marked_uploaded": marked,
        },
        "success",
    )
    return {
        "ok": True,
        "uploaded": len(public_messages),
        "marked_uploaded": marked,
        "status": result["status"],
        "file": str(snapshot),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload new Bilibili private messages")
    parser.add_argument("--url", default="", help="Batch upload URL. Overrides config report_webhook_url.")
    parser.add_argument("--api-key", default="", help="Optional X-API-Key header. Overrides config report_api_key.")
    parser.add_argument("--interval", type=int, default=600, help="Loop interval seconds. Default 600.")
    parser.add_argument("--loop", action="store_true", help="Run forever and upload every interval.")
    parser.add_argument("--once", action="store_true", help="Run once and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Write payload to file without POST or marking uploaded.")
    parser.add_argument("--sync-first", action="store_true", default=True, help="Sync Bilibili before upload.")
    parser.add_argument("--no-sync-first", action="store_false", dest="sync_first", help="Skip Bilibili sync before upload.")
    parser.add_argument(
        "--reply-status",
        choices=["all", "pending", "replied"],
        default="all",
        help="Which messages to upload based on conversation reply status.",
    )
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = app.AppConfig.load(CONFIG_PATH)
    if not Path(config.db_path).is_absolute():
        config.db_path = str((ROOT / config.db_path).resolve())
    args.url = args.url or str(get_config_value(config, "report_webhook_url", "") or "")
    args.api_key = args.api_key or str(get_config_value(config, "report_api_key", "") or "")

    while True:
        try:
            result = run_once(config, args)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        print(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **result}, ensure_ascii=False))
        if args.once or not args.loop:
            break
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    main()
