# Bilibili 私信本地收集系统

这是一个本地运行的 B 站私信采集和客服面板原型。当前版本只接入“私信”，不包含视频评论、动态评论或多客服分配。

## 功能

- 本地网页面板查看会话和私信
- 会话列表标记“待回复/已回复”
- SQLite 本地存储会话、消息和发送记录
- 定时轮询 B 站私信接口
- 支持立即同步、启动轮询、停止轮询
- 支持本地模拟私信，方便没有登录态时先验证界面
- 客服回复通过 outbox 记录发送状态

## 快速启动

```powershell
python bilibili_dm_local.py
```

启动后打开：

```text
http://127.0.0.1:8765
```

第一次运行会自动创建 `config.json` 和 `data/bilibili_dm.sqlite3`。默认 `mock_mode` 为 `true`，可以先在页面点击“模拟私信”测试本地收集流程。

## 接入真实 B 站私信

推荐直接在本地页面操作：

1. 打开 `http://127.0.0.1:8765`
2. 点击“登录配置”
3. 填入 `SESSDATA`、`bili_jct`、`DedeUserID` 和当前账号 UID
4. 点击“保存配置”
5. 点击“切到真实模式”
6. 点击“校验登录”
7. 校验通过后点击“立即同步”或“启动轮询”

也可以手动编辑 `config.json`：

```json
{
  "self_uid": "你的B站UID",
  "sessdata": "浏览器 Cookie 里的 SESSDATA",
  "bili_jct": "浏览器 Cookie 里的 bili_jct",
  "dedeuserid": "浏览器 Cookie 里的 DedeUserID",
  "mock_mode": false,
  "auto_start": true
}
```

然后重启：

```powershell
python bilibili_dm_local.py
```

说明：

- `SESSDATA` 用于读取私信。
- `bili_jct` 是 CSRF token，发送私信时需要。
- `self_uid` 用于判断消息方向，缺失时可能无法准确区分自己发出的消息。
- `history_sessions_per_sync` 控制每次补拉多少个会话。
- `history_pages_per_session` 控制每个会话补拉多少页历史。
- 当前适配的是 B 站网页端私信接口，包含会话列表、会话消息和文本发送。它仍属于非稳定公开接口，生产使用前需要做账号风控和接口变动预案。
- B 站网页端历史接口更适合获取当前可见会话和最近消息，不能保证无限制拉取账号全量历史私信。
- 非文本消息会转成摘要显示，例如图片、系统通知、撤回或引用消息。
- `session_pages_per_sync` 控制每次拉取多少页会话列表，默认 5 页。
- “待回复”的判断规则：最后一条用户来信时间晚于最后一条我方回复时间。

## 本地 API

```text
GET  /api/status
GET  /api/conversations
GET  /api/conversations?reply_status=pending
GET  /api/conversations?reply_status=replied
GET  /api/messages?conversation_id=<id>
POST /api/start
POST /api/stop
POST /api/sync
POST /api/send
POST /api/mock/inbound
POST /api/config
POST /api/auth/check
POST /api/history/backfill
```

`POST /api/send` 请求体：

```json
{
  "talker_id": "对方UID",
  "content": "回复内容"
}
```

## 数据文件

- 配置：`config.json`
- 数据库：`data/bilibili_dm.sqlite3`

不要把真实 `config.json` 提交或发给别人，里面可能包含账号登录态。
