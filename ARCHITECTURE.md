# 项目架构文档

最后更新于：2026-06-08

## 项目概览

本项目是一个本地运行的 Bilibili 私信收集和客服面板原型。它使用 Python 标准库提供本地 HTTP 服务，使用 SQLite 存储会话、私信和客服回复发送记录。

当前范围只包含 B 站私信，不包含评论、动态评论、多客服坐席、权限系统或生产部署能力。

## 技术栈

| 类型 | 技术 |
| --- | --- |
| 后端语言 | Python 3.11 |
| HTTP 服务 | `http.server.ThreadingHTTPServer` |
| 数据库 | SQLite |
| 前端 | 内嵌 HTML/CSS/JavaScript |
| 外部请求 | `urllib.request` |
| 依赖管理 | 无第三方依赖 |

## 目录结构

```text
.
├── bilibili_dm_local.py      # 本地服务、采集器、数据库访问、网页面板
├── export_bilibili_leads.py  # 按线索接入文档导出 B站私信线索
├── config.example.json       # 配置示例
├── config.json               # 本地运行配置，首次启动自动生成，不应提交
├── data/
│   └── bilibili_dm.sqlite3   # 本地 SQLite 数据库，首次启动自动生成
├── README.md                 # 使用说明
├── ARCHITECTURE.md           # 架构文档
└── .gitignore
```

## 核心模块与数据流

```mermaid
flowchart LR
    A["本地登录配置"] --> B["AppConfig"]
    C["Bilibili 私信接口"] --> D["BilibiliClient"]
    B --> D
    D --> E["Collector 轮询器"]
    E --> F["会话发现与历史补拉"]
    F --> G["normalize_message 标准化"]
    G --> H["Store SQLite"]
    H --> I["本地 API"]
    I --> J["网页客服面板"]
    J --> K["/api/send"]
    K --> L["outbox 发送记录"]
    L --> D
```

### `AppConfig`

负责读取、保存 `config.json` 和环境变量，生成运行配置。敏感登录态包括 `sessdata`、`bili_jct`、`dedeuserid` 和 `access_key`。网页面板可通过 `/api/config` 在本地保存配置，接口返回时只返回脱敏状态。

### `BilibiliClient`

封装 B 站网页端私信接口请求：

- `auth_check()`：校验当前登录态
- `pull()`：通过 `api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions` 拉取私信会话
- `history()`：通过 `api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs` 按会话拉历史私信
- `send_text()`：通过 `api.vc.bilibili.com/web_im/v1/web_im/send_msg` 发送文本私信

这些接口属于适配层，后续如果 B 站接口字段变化，优先修改该模块和消息标准化逻辑。

### `Collector`

负责定时轮询、立即同步、会话发现、历史补拉和 mock 模式。同步流程会读取游标、拉取数据、标准化消息、写入 SQLite，并记录最后同步时间和错误信息。首次发现会话后会按配置补拉部分历史记录，并将会话列表里的 `last_msg` 作为最新消息入库。

### `Store`

负责 SQLite 表结构和读写逻辑，包括：

- `conversations`：会话
- `messages`：私信消息
- `outbox`：客服回复发送记录
- `poll_state`：轮询游标、历史补拉状态

消息使用 `platform + msg_id` 做唯一约束，避免重复写入。

标准化逻辑会将 B 站原始消息类型渲染为页面可读内容。普通文本读取 `content.content`，图片、系统通知、撤回或引用消息会转成摘要文本。

### 网页面板

`INDEX_HTML` 内嵌在 `bilibili_dm_local.py`，通过本地 API 展示会话、消息、轮询状态，并支持：

- 本地保存 B 站登录态
- 校验登录态是否有效
- 切换 mock/真实模式
- 立即同步、启动/停止轮询
- 会话列表显示“待回复/已回复”状态并支持筛选
- 手动补拉历史
- 模拟私信和发送回复

### `export_bilibili_leads.py`

按 `各平台线索接入文档.md` 的 B站私信字段要求，从 SQLite 中导出线索文件。导出按会话聚合，一条客户会话对应一条线索，取该客户最后一条来信作为 `msg_id/content/timestamp` 来源，并补充 `externalUserId/source/status/reply_status` 等字段。

脚本同时生成客户线索版 CSV/JSON，通过用户来信内容提取 `name/contact_name/company/phone/email/business/message/notes`。姓名、公司和业务意向来自文本规则抽取，抽不到时姓名使用 B站昵称或 `B站用户_UID` 兜底。

导出结果位于 `data/exports/`：

- `bilibili_leads_all.json` / `.csv`
- `bilibili_leads_pending.json` / `.csv`
- `bilibili_leads_replied.json` / `.csv`
- `bilibili_customer_leads_all_*.json` / `.csv`
- `bilibili_customer_leads_pending_*.json` / `.csv`
- `bilibili_customer_leads_replied_*.json` / `.csv`

## API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 本地客服网页面板 |
| `GET` | `/api/status` | 获取运行状态、统计和脱敏配置 |
| `GET` | `/api/config` | 获取脱敏配置 |
| `GET` | `/api/conversations` | 获取会话列表，支持 `reply_status=all/pending/replied/no_inbound` |
| `GET` | `/api/messages?conversation_id=<id>` | 获取指定会话消息 |
| `POST` | `/api/config` | 保存本地配置 |
| `POST` | `/api/auth/check` | 校验 B 站登录态 |
| `POST` | `/api/start` | 启动轮询线程 |
| `POST` | `/api/stop` | 停止轮询线程 |
| `POST` | `/api/sync` | 立即同步一次 |
| `POST` | `/api/history/backfill` | 手动补拉会话历史 |
| `POST` | `/api/send` | 发送客服回复 |
| `POST` | `/api/mock/inbound` | 写入一条本地模拟私信 |

## 环境变量与配置

配置优先级：环境变量高于 `config.json`。

| 配置项 | 环境变量 | 说明 |
| --- | --- | --- |
| `host` | `BILI_HOST` | 本地服务监听地址 |
| `port` | `BILI_PORT` | 本地服务端口 |
| `db_path` | `BILI_DB_PATH` | SQLite 数据库路径 |
| `poll_interval_seconds` | `BILI_POLL_INTERVAL_SECONDS` | 轮询间隔 |
| `request_timeout_seconds` | `BILI_REQUEST_TIMEOUT_SECONDS` | 请求超时时间 |
| `self_uid` | `BILI_SELF_UID` | 当前 B 站账号 UID |
| `sessdata` | `BILI_SESSDATA` | B 站登录态 |
| `bili_jct` | `BILI_BILI_JCT` | CSRF token |
| `dedeuserid` | `BILI_DEDEUSERID` | B 站 UID Cookie |
| `extra_cookie` | `BILI_EXTRA_COOKIE` | 额外 Cookie |
| `access_key` | `BILI_ACCESS_KEY` | 可选 access key |
| `session_pages_per_sync` | `BILI_SESSION_PAGES_PER_SYNC` | 每次同步拉取的会话列表页数 |
| `history_sessions_per_sync` | `BILI_HISTORY_SESSIONS_PER_SYNC` | 每次补拉历史的会话数 |
| `history_pages_per_session` | `BILI_HISTORY_PAGES_PER_SESSION` | 每个会话补拉历史页数 |
| `auto_start` | `BILI_AUTO_START` | 启动时自动轮询 |
| `mock_mode` | `BILI_MOCK_MODE` | 无登录态时使用模拟数据 |

## 风险与边界

- 当前使用的 B 站网页端私信接口不是明确稳定公开的主站私信开放 API，字段和鉴权方式可能变化。
- 本地版本没有用户登录、权限隔离和 HTTPS，不适合直接暴露到公网。
- `config.json` 可能包含账号登录态，应仅保存在本机。
- 当前发送能力只实现文本私信，图片、表情、富文本需要后续扩展。
- 历史补拉受接口分页和风控限制影响，首次补拉页数不宜过大。
- B 站网页端历史接口不能保证无限制拉取账号全量历史私信，当前实现以当前可见会话和最近消息为主。

## 更新日志

| 日期 | 变更类型 | 简要描述 |
| --- | --- | --- |
| 2026-06-08 | 初始化 | 创建项目架构文档 |
| 2026-06-08 | 新增功能 | 新增本地 Bilibili 私信收集系统，包含轮询采集、SQLite 存储、本地客服面板和 mock 测试能力 |
| 2026-06-08 | 新增功能 | 增加真实登录配置、登录态校验、会话历史补拉和页面配置入口 |
| 2026-06-08 | 优化重构 | 优化 B 站私信补拉逻辑，补入会话最新消息并增强非文本消息摘要渲染 |
| 2026-06-08 | 新增功能 | 增加多页会话同步和待回复/已回复状态筛选 |
| 2026-06-08 | 新增功能 | 增加 B站线索导出脚本，按接入文档字段输出全部、待回复、已回复线索 |
| 2026-06-08 | 新增功能 | 增加客户资料版线索导出，支持从私信内容抽取姓名、公司、电话、邮箱和业务意向 |

## 项目进度

| 完成日期 | 完成的功能/工作 | 说明 |
| --- | --- | --- |
| 2026-06-08 | 本地私信采集服务 | 使用 Python 标准库实现本地 HTTP 服务和 B 站私信适配层 |
| 2026-06-08 | SQLite 消息存储 | 建立会话、消息、outbox 和轮询状态表 |
| 2026-06-08 | 本地客服面板 | 支持查看会话、查看消息、启动/停止轮询、立即同步和发送回复 |
| 2026-06-08 | 本地 mock 测试 | 无 B 站登录态时可写入模拟私信验证流程 |
| 2026-06-08 | 真实私信接入入口 | 支持在本地页面保存 Cookie、校验登录态、切换真实模式和补拉历史 |
| 2026-06-08 | 消息完整性优化 | 修正已补拉会话阻塞后续会话的问题，补入会话 `last_msg` 并重算已有消息展示内容 |
| 2026-06-08 | 会话回复状态 | 基于最后用户来信和最后我方回复时间判断待回复/已回复，并支持前端筛选 |
| 2026-06-08 | B站线索导出 | 生成符合接入文档字段的 JSON/CSV 文件，支持全部、待回复和已回复三种范围 |
| 2026-06-08 | 客户字段抽取 | 从 B站私信内容中抽取客户资料字段并生成 customer_leads CSV/JSON |

## 2026-06-09 更新

- 新增 B站私信“消息上报格式”导出，输出 `bilibili_report_messages_{all|pending|replied}_*.json`。
- 批量 JSON 结构为 `{ "messages": [...] }`。
- 单条消息字段严格限定为 `sender_uid`、`sender_name`、`content`、`msg_type`、`direction`、`timestamp`，并在识别到时附加 `company`、`phone`。
- 同步生成 JSONL 和 CSV，JSONL 每行是一条可单条上报的消息对象。

## 2026-06-09 本地上传去重

- `export_bilibili_leads.py` 新增本地上传去重状态文件：`data/state/bilibili_report_uploaded_msg_ids.json`。
- 导出脚本会根据 B站消息 `msg_id` 过滤已处理消息，生成 `bilibili_report_messages_new_{all|pending|replied}_*.json`。
- 服务器只需要接收消息和回复消息；重复线索筛选在本地完成。
- 如需重新全量上传，删除 `data/state/bilibili_report_uploaded_msg_ids.json` 后重新运行导出脚本。

## 2026-06-09 轮询随机浮动

- `bilibili_dm_local.py` 新增 `poll_jitter_seconds` 配置。
- 当前配置为 `poll_interval_seconds=600`、`poll_jitter_seconds=80`。
- 实际每轮 B站私信列表查询间隔为 `600 + random(-80, 80)` 秒，即 520 到 680 秒之间。

## 2026-06-09 B站待回复自动回复

- 新增 `auto_reply_pending_bilibili.py`。
- 默认 dry-run，只统计待回复会话，不发送。
- 使用 `--send` 才会实际调用 B站私信发送接口。
- 已自动回复过的 `talker_id` 会记录到 `data/state/bilibili_auto_replied_talker_ids.json`，避免重复自动回复同一会话。
- 自动回复会写入本地 `outbox` 和 `messages` 表，使会话状态变为已回复。

## 2026-06-09 服务端联调发送规则

- 本地回复接口支持 `POST /api/send` 和 `POST /api/reply`。
- 请求体可使用 `{ "talker_id": "B站UID", "content": "回复内容" }`。
- 为了方便外部服务端回调，也兼容 `{ "sender_uid": "B站UID", "content": "回复内容" }`。
- `message` 或 `text` 可作为 `content` 的兼容字段。
- 本地服务会把成功发送的回复写入 `outbox` 和 `messages`，并更新会话回复状态。

## 2026-06-09 本地服务端桥接脚本

- 新增 `sync_bilibili_server.py`，把 B 站私信本地采集、消息批量上传、服务端待回复拉取、本地发送 B 站私信、回复结果回写合并为一个长期运行脚本。
- 新增 `SERVER_SYNC.md`，记录服务端上传消息、待回复任务、回复结果回写的 JSON 规则和运行命令。
- `config.example.json` 新增 `reply_pending_url`、`reply_update_url`、`reply_api_key`、`server_bearer_token`、`server_sync_interval_seconds`、`server_sync_jitter_seconds`、`reply_limit_per_sync` 等配置项。
- 服务端回复任务本地去重状态保存到 `data/state/bilibili_server_reply_task_ids.json`，避免同一任务重复发送。
- 长期运行默认支持 600 秒间隔和 80 秒随机浮动，适配本地电脑主动轮询服务端的架构。

## 2026-06-09 B站无效私信上报过滤

- `export_bilibili_leads.py` 新增 `IGNORED_REPORT_CONTENTS` 过滤规则。
- 内容精确等于 `UP主加油！看好你噢~` 的 B 站私信不会进入 `build_report_messages()` 结果，因此不会被 `sync_bilibili_server.py` 上传到服务端。
- 过滤前会做空白压缩和首尾空白清理，避免因为前后空格导致误上传。

## 2026-06-09 B站无效私信回复过滤

- `auto_reply_pending_bilibili.py` 在筛选待自动欢迎语回复时，会读取每个会话最新一条用户来信。
- 如果最新用户来信内容等于 `UP主加油！看好你噢~`，该会话不会进入自动回复目标列表。
- `sync_bilibili_server.py` 在处理服务端下发的回复任务前，也会检查该 B 站用户最新来信；命中无效内容时不会调用 B 站发送接口，并回写跳过原因。

## 2026-06-09 B站消息上传批量重试优化

- `sync_bilibili_server.py` 新增 `upload_batch_size`、`upload_retries`、`upload_retry_delay_seconds`、`upload_batch_delay_seconds` 配置。
- 历史消息较多时不再一次性 POST 全部消息，而是分批上传；每批成功后立即写入本地上传去重状态。
- 若某一批上传失败，已成功批次不会重复上传，失败批次会保留到下一轮继续重试。
- 用于解决服务端 HTTPS 连接在大批量上传时出现 `SSL: UNEXPECTED_EOF_WHILE_READING` 的问题。

## 2026-06-09 B站系统账号上传过滤

- `export_bilibili_leads.py` 新增 `IGNORED_REPORT_SENDER_NAMES` 过滤规则。
- 昵称精确等于 `哔哩哔哩智能机` 或 `UP主小助手` 的 B 站私信不会进入线索导出和消息上报 payload。
- 过滤前会做空白压缩和首尾空白清理，避免因为前后空格导致误上传。

## 2026-06-09 同步脚本数据库路径修正

- `sync_bilibili_server.py` 在每次运行时会把 `export_bilibili_leads.DB_PATH` 设置为 `config.db_path`。
- 修复本地网页服务和上传脚本读取不同 SQLite 文件时，网页能看到新消息但上传脚本读不到的问题。
- 当前以本地服务实际配置的 `db_path` 为准，保证上传数据源和 `127.0.0.1:8765` 页面展示一致。

## 2026-06-09 B站发送风控冷却保护

- `sync_bilibili_server.py` 新增 B 站 `HTTP 412 / request was banned` 检测。
- 遇到该错误后写入 `data/state/bilibili_send_cooldown.json`，默认 3600 秒内不再调用 B 站私信发送接口。
- 冷却期间仍可继续采集 B 站私信和上传消息，但不会处理服务端待回复任务，避免连续重试加重风控。
- 新增配置 `bilibili_ban_cooldown_seconds`，默认值为 3600。

## 2026-06-09 B站 412 请求头与 Cookie 优化

- `bilibili_dm_local.py` 新增 `buvid3`、`buvid4` 配置字段，并纳入 Cookie Header。
- 本地配置页新增 `buvid3`、`buvid4` 输入框，可从浏览器 B 站 Cookie 复制填写。
- B 站 API 请求头新增 `Accept-Language`、`Cache-Control`、`Pragma`、`Sec-Fetch-*` 等浏览器化字段。
- B 站私信发送时优先使用 `buvid3` 作为 `msg[dev_id]`，降低本地脚本固定设备 ID 带来的风控特征。
- `SERVER_SYNC.md` 补充 B 站 `-412 request was banned` 的本地发送说明和 Cookie 补齐建议。

## 2026-06-09 B站发送响应业务码校验

- `BilibiliClient.send_text()` 新增 B 站 JSON 响应业务码校验。
- 只有响应 `code` 为空或 `0` 时才视为发送成功；如 `code=-400`、`code=-412` 会抛出异常。
- 修复此前 HTTP 200 但 B 站业务返回 `code=-400` 时，本地误把 outbox 标记为 `sent` 并回写服务器成功的问题。
- 已将此前误标成功的 outbox 记录纠正为 `failed`，并从本地已发送任务状态中移除对应 task_id。

## 2026-06-09 B站发送 -400 参数修正

- `BilibiliClient.send_text()` 补充 `msg[timestamp]`、`msg[new_face_version]` 和 `from_firework` 参数。
- 新增 `device_id` 配置，默认生成稳定 UUID，用于 `msg[dev_id]`；不再把 `buvid3` 直接作为 `msg[dev_id]`。
- `config.example.json` 新增 `device_id` 字段。
- 该修正用于解决 B 站发送接口 HTTP 200 但业务返回 `code=-400, message=请求错误` 的参数不合法问题。

## 2026-06-09 回复结果回写增强

- `sync_bilibili_server.py` 的 `sent` 回写 payload 新增 `sent_at`、`platform=bilibili`、`platform_status=sent`。
- 当 B 站发送响应包含 `data.msg_key` 时，回写 `bilibili_msg_key` 和 `platform_msg_id`，便于服务端绑定真实平台消息 ID。
- 每次 `reply_update` 请求和响应会写入 `data/server_sync_logs/reply_update_*.json`，失败则写入 `reply_update_failed_*.json`。
- 排查到不应同时运行多个 `sync_bilibili_server.py`，否则可能造成任务竞争；测试阶段保留单个 30 秒轮询进程。

## 2026-06-10 Windows 启动脚本

- 新增 `run_bilibili_sync.bat`，双击后按 `600 +/- 80` 秒循环执行完整桥接：采集上传、拉取回复、本地发送、回写状态。
- 新增 `run_bilibili_reply_only.bat`，双击后按 `600 +/- 80` 秒循环执行回复桥接，不上传新消息。
- bat 内容使用 ASCII 输出，避免 Windows CMD 在中文编码下解析异常。

## 2026-06-10 Windows 启动脚本拆分

- `run_bilibili_sync_test_30s.bat` 用于测试，按 `30 +/- 5` 秒循环执行完整桥接。
- `run_bilibili_sync.bat` 用于正式运行，按 `600 +/- 80` 秒循环执行完整桥接。
- 移除回复专用 bat，避免测试和正式启动入口混淆。
