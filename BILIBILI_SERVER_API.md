# B 站私信本地桥接服务端接口文档

本文档定义服务端需要提供的 3 个接口，用于配合本地脚本 `sync_bilibili_server.py` 接入真实 B 站私信。

本地电脑负责：

- 定时采集 B 站私信
- 上传新私信到服务端
- 从服务端拉取待发送回复
- 调用 B 站私信接口发送
- 回写发送成功或失败结果

服务端不需要访问本地电脑。

## 通用规则

### 请求头

本地脚本默认使用 JSON 请求：

```http
Content-Type: application/json; charset=utf-8
Accept: application/json
```

如果配置了 `report_api_key` 或 `reply_api_key`，本地脚本会额外发送：

```http
X-API-Key: 你的 API Key
```

如果配置了 `server_bearer_token`，本地脚本会额外发送：

```http
Authorization: Bearer 你的 token
```

### 时间格式

所有 `timestamp` / `updated_at` 使用毫秒时间戳。

示例：

```json
1717920000000
```

### 用户唯一标识

B 站用户用 `sender_uid` 作为唯一标识。

服务端建议用下面的组合做消息去重：

```text
platform = bilibili
sender_uid
timestamp
content
```

如果后续本地上传体里扩展了 `msg_id`，优先用：

```text
platform = bilibili
msg_id
```

## 接口 1：批量接收 B 站私信

### 用途

本地脚本把新采集到、且本地未上传过的 B 站私信批量上报给服务端。

### 请求

```http
POST /api/bilibili/messages/batch
```

### 请求体

```json
{
  "messages": [
    {
      "sender_uid": "123456",
      "sender_name": "B站用户昵称",
      "content": "你好，我想咨询机器人项目",
      "msg_type": "text",
      "direction": "receive",
      "timestamp": 1717920000000,
      "company": "某某科技有限公司",
      "phone": "13800138000"
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `messages` | array | 是 | 消息数组 |
| `sender_uid` | string | 是 | B 站用户 UID，用于识别唯一客户 |
| `sender_name` | string | 是 | B 站用户昵称 |
| `content` | string | 是 | 私信内容 |
| `msg_type` | string | 是 | 当前主要为 `text` |
| `direction` | string | 是 | 固定为 `receive` |
| `timestamp` | number | 是 | 消息时间，毫秒时间戳 |
| `company` | string | 否 | 本地从消息里识别出的公司名 |
| `phone` | string | 否 | 本地从消息里识别出的手机号或电话 |

### 成功响应

```json
{
  "ok": true,
  "received": 1,
  "inserted": 1,
  "duplicated": 0
}
```

### 响应字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | boolean | 是否处理成功 |
| `received` | number | 服务端收到的消息数量 |
| `inserted` | number | 新增数量 |
| `duplicated` | number | 服务端判定重复的数量 |

### 失败响应

```json
{
  "ok": false,
  "error": "invalid api key"
}
```

### 本地脚本行为

只有该接口返回 HTTP `2xx` 时，本地脚本才会把这些消息标记为“已上传”。

如果服务端返回 `4xx` / `5xx` 或请求失败，本地不会标记已上传，下次仍会重试。

历史消息较多时，本地脚本会分批调用该接口。默认每批 20 条，可通过 `upload_batch_size` 调整。服务端只需要按当前请求体里的 `messages` 数组处理即可，不需要关心这是第几批。

## 接口 2：获取待发送回复

### 用途

本地脚本定时从服务端拉取待发送的 B 站回复任务。

### 请求

```http
GET /api/bilibili/replies/pending
```

### 查询参数

当前本地脚本不强制要求查询参数。

服务端如果需要，也可以忽略所有查询参数，直接返回当前账号的待发送任务。

### 成功响应

推荐响应格式：

```json
{
  "ok": true,
  "replies": [
    {
      "reply_id": "reply_10001",
      "sender_uid": "123456",
      "content": "您好，请问您主要咨询哪个业务方向？"
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `replies` | array | 是 | 待发送回复任务 |
| `reply_id` | string | 是 | 服务端回复任务唯一 ID |
| `sender_uid` | string | 是 | 要回复的 B 站用户 UID |
| `content` | string | 是 | 要发送的回复内容 |

### 兼容字段

本地脚本也兼容以下字段名：

任务 ID：

```text
reply_id / task_id / id / outbox_id / message_id
```

B 站用户 UID：

```text
sender_uid / talker_id / receiver_uid / uid / user_id / to_uid
```

回复内容：

```text
content / message / text / reply_content
```

### 空任务响应

```json
{
  "ok": true,
  "replies": []
}
```

### 服务端建议

服务端建议只返回状态为 `pending` 的任务。

如果服务端支持任务锁定，可以在被本地拉取后改成：

```text
pending -> claimed
```

如果暂时不做锁定，也可以保持 `pending`，本地会用 `reply_id` 做本地去重，避免同一个任务重复发送。

## 接口 3：回写回复发送结果

### 用途

本地脚本发送 B 站私信后，把发送成功或失败结果回写给服务端。

### 请求

```http
POST /api/bilibili/replies/update
```

### 成功发送请求体

```json
{
  "reply_id": "reply_10001",
  "task_id": "reply_10001",
  "sender_uid": "123456",
  "talker_id": "123456",
  "content": "您好，请问您主要咨询哪个业务方向？",
  "status": "sent",
  "outbox_id": 12,
  "updated_at": 1717920000000,
  "response": {
    "code": 0,
    "message": "0"
  }
}
```

### 发送失败请求体

```json
{
  "reply_id": "reply_10001",
  "task_id": "reply_10001",
  "sender_uid": "123456",
  "talker_id": "123456",
  "content": "您好，请问您主要咨询哪个业务方向？",
  "status": "failed",
  "updated_at": 1717920000000,
  "error": "Bilibili HTTP 412: ..."
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `reply_id` | string | 是 | 服务端回复任务 ID |
| `task_id` | string | 是 | 同 `reply_id`，兼容字段 |
| `sender_uid` | string | 是 | B 站用户 UID |
| `talker_id` | string | 是 | 同 `sender_uid`，兼容字段 |
| `content` | string | 是 | 实际发送的回复内容 |
| `status` | string | 是 | `sent` 或 `failed` |
| `outbox_id` | number | 否 | 本地 outbox 记录 ID，成功时存在 |
| `updated_at` | number | 是 | 回写时间，毫秒时间戳 |
| `response` | object | 否 | B 站接口原始响应摘要 |
| `error` | string | 否 | 失败原因 |

### 成功响应

```json
{
  "ok": true
}
```

### 失败响应

```json
{
  "ok": false,
  "error": "reply task not found"
}
```

## 服务端状态建议

回复任务建议使用以下状态：

```text
pending  待本地发送
claimed  已被本地拉取，可选
sent     已发送成功
failed   发送失败
```

最小可用版本只需要：

```text
pending
sent
failed
```

## 服务端最小实现要求

必须实现：

```text
POST /api/bilibili/messages/batch
GET  /api/bilibili/replies/pending
POST /api/bilibili/replies/update
```

本地 `config.json` 对应填写：

```json
{
  "report_webhook_url": "https://你的服务端/api/bilibili/messages/batch",
  "reply_pending_url": "https://你的服务端/api/bilibili/replies/pending",
  "reply_update_url": "https://你的服务端/api/bilibili/replies/update",
  "report_api_key": "可选",
  "reply_api_key": "可选"
}
```

## 本地联调命令

只测试，不真实上传、不真实发送：

```powershell
python sync_bilibili_server.py --once --dry-run
```

只上传消息，不处理回复：

```powershell
python sync_bilibili_server.py --once --no-replies
```

完整跑一次，并允许发送 B 站回复：

```powershell
python sync_bilibili_server.py --once --send-replies
```

长期运行：

```powershell
python sync_bilibili_server.py --loop --send-replies
```

默认轮询间隔：

```text
600 + random(-80, 80) 秒
```

也就是约 8 分 40 秒到 11 分 20 秒。
