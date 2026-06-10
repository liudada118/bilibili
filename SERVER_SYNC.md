# Bilibili 本地服务端桥接

`sync_bilibili_server.py` 用于把本机采集到的 B 站私信同步到客服系统，并从客服系统拉取待发送回复，再由本机调用 B 站私信接口发送。

服务端不需要访问本地电脑。所有请求都由本地脚本主动发起。

## config.json 配置

```json
{
  "report_webhook_url": "https://你的服务端/api/bilibili/messages/batch",
  "report_api_key": "可选",
  "reply_pending_url": "https://你的服务端/api/bilibili/replies/pending",
  "reply_update_url": "https://你的服务端/api/bilibili/replies/update",
  "reply_api_key": "可选，默认可复用 report_api_key",
  "server_bearer_token": "可选",
  "server_sync_interval_seconds": 600,
  "server_sync_jitter_seconds": 80,
  "upload_batch_size": 20,
  "upload_retries": 3,
  "upload_retry_delay_seconds": 3,
  "upload_batch_delay_seconds": 0.5,
  "reply_limit_per_sync": 20
}
```

## 上传消息

本地脚本向 `report_webhook_url` 发送：

```json
{
  "messages": [
    {
      "sender_uid": "B站用户UID",
      "sender_name": "用户昵称",
      "content": "私信内容",
      "msg_type": "text",
      "direction": "receive",
      "timestamp": 1717920000000,
      "company": "可选",
      "phone": "可选"
    }
  ]
}
```

只有服务端返回 2xx 后，消息才会写入本地上传去重状态：

```text
data/state/bilibili_report_uploaded_msg_ids.json
```

历史消息较多时，本地脚本会按 `upload_batch_size` 分批上传。每一批服务端返回 2xx 后会立即标记这一批为已上传；如果后续批次失败，成功批次不会重复上传，失败批次会在下一轮继续重试。

## 拉取待回复

本地脚本从 `reply_pending_url` 拉取待回复任务。建议服务端返回：

```json
{
  "replies": [
    {
      "reply_id": "服务端回复任务ID",
      "sender_uid": "B站用户UID",
      "content": "要发送的回复内容"
    }
  ]
}
```

兼容字段：

- 任务 ID：`reply_id`、`task_id`、`id`、`outbox_id`、`message_id`
- B 站用户 UID：`sender_uid`、`talker_id`、`receiver_uid`、`uid`、`user_id`、`to_uid`
- 回复内容：`content`、`message`、`text`、`reply_content`

## 回写回复结果

本地脚本向 `reply_update_url` 回写：

```json
{
  "reply_id": "服务端回复任务ID",
  "task_id": "服务端回复任务ID",
  "sender_uid": "B站用户UID",
  "talker_id": "B站用户UID",
  "content": "回复内容",
  "status": "sent",
  "outbox_id": 1,
  "updated_at": 1717920000000
}
```

失败时 `status` 为 `failed`，并附带 `error`。

本地已发送过的服务端任务会记录到：

```text
data/state/bilibili_server_reply_task_ids.json
```

用于避免同一个回复任务在本机重复发送。

## 运行

只测试流程，不上传、不发送：

```powershell
python sync_bilibili_server.py --once --dry-run
```

执行一次，允许发送服务端下发的回复：

```powershell
python sync_bilibili_server.py --once --send-replies
```

长期运行：

```powershell
python sync_bilibili_server.py --loop --send-replies
```

默认间隔为 `600 + random(-80, 80)` 秒，即约 8 分 40 秒到 11 分 20 秒。

## B 站 412 风控处理

如果发送 B 站私信时返回：

```text
Bilibili HTTP 412
{"code":-412,"message":"request was banned"}
```

说明请求被 B 站风控拦截。发送动作发生在本地电脑，不是服务端直接请求 B 站。

建议在本地配置里补齐浏览器 Cookie：

```json
{
  "sessdata": "...",
  "bili_jct": "...",
  "dedeuserid": "...",
  "buvid3": "...",
  "buvid4": "...",
  "extra_cookie": "也可以粘贴浏览器里其他 Cookie"
}
```

脚本会带上更接近浏览器的请求头，并在遇到 `-412` 后写入 `data/state/bilibili_send_cooldown.json`，默认 1 小时内不再继续发送私信，避免连续重试加重风控。
