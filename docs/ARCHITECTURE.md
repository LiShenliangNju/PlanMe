# 🧱 PlanMe 架构与数据流

本文档补充 `README.md` 中的架构概览，说明各模块职责、关键数据流与配置加载方式。

---

## 一、总体划分

PlanMe 由**主系统（FastAPI）** 与**可插拔的后台服务**（如 homework 扫描器）组成，统一经**单一入口 `main.py`** 启动；所有路由集中注册到 `api`，所有服务由 `app` 编排启停。

| 部件 | 进程 | 入口 | 职责 |
| --- | --- | --- | --- |
| **主系统 + 后台服务** | FastAPI (`uvicorn`, 8000) + 同进程后台任务 | `main.py`（`app.factory.create_app`） | 理解自然语言、调用 Ollama、写入 iCloud；按配置统一拉起 homework 等后台服务 |
| **QQ 作业扫描器** | 主程序**同进程后台任务** | `core/homework/scanner.py` 的 `HomeworkScanner`（由 `app.factory` 的 `lifespan` 启停） | 监听 QQ 群消息；**文本管道**识别作业 → 私聊确认 → 转发主系统；**图片管道**抓图 → OCR → 存档 |
| **图片 OCR 层** | 同上（扫描器内的协程 + 并发信号量） | `core/homework/ocr.py` 的 `ImageOCR` | 调用本地视觉模型 `qwen2.5vl` 把图片转 Markdown |
| **存储层** | SQLite（WAL） | `core/homework/message_store.py` 的 `MessageStore` | 消息去重、**作业状态持久化**、**OCR 存档**；供 API 直读 |
| **NapCat 集成层** | 内存事件总线（`core/napcat/feed.py`） | `app` 装配 | 聚合 qqbot 推送 + 建议日程，经 `/api/homework/feed`、`/api/napcat` 暴露给 Web |

**扫描器不做日历写入**，只把作业转成自然语言 `POST /api/chat` 交给主系统处理——主系统是唯一写入 iCloud 的入口，便于审计与复用。

### 状态归属原则（重要设计约定）

| 数据性质 | 存放位置 | 读取方 | 重启后 |
| --- | --- | --- | --- |
| **权威结果**（作业列表、OCR 存档） | SQLite | Web 直读 `/api/homework/items`、`/api/lecture/notes` | ✅ 保留 |
| **实时流水**（"刚推了条私聊"） | 内存 `FeedBus` | `/api/homework/feed`、`/api/napcat/pushes` | ❌ 清空（符合预期） |
| **运行时待确认队列** | 内存 `notifier.pending` | `/api/homework/pending` | 由 db 中 `status=pending` 的记录**自动 rehydrate** |

早期版本 Web 只能读内存，因为 `messages` 表只存原文、不存决策结果；现在识别结果与状态都落到 `homework_items`，Web 的"我的作业列表"才具备历史回看能力。

---

## 二、配置加载链路

```
main.py  ──►  app.factory.create_app()
        │  app/__init__.py 统一把 <ROOT>/.config 注入 sys.path（替代原先散落在各模块的重复 sys.path.insert）
        ▼
.config/settings.py  ──►  Settings(BaseSettings)
        │  • 默认值：OLLAMA_HOST / OLLAMA_MODEL / TIMEZONE / ENABLE_HOMEWORK ...
        │  • env_file = ".env"（可被 .env 覆盖）
        │  • 把 CALDAV_CONFIG_FILE / HMWK_SCRN_CONFIG_FILE 注入 os.environ
        ▼
caldav.get_calendars() 读取 .config/caldav/calendar.conf
core/homework/scanner.py 读取 .config/hmwk_scnr/config.yaml
        │  • qq / detector / scheduler / storage / notifier
        └► image:  图片 OCR 专属段（group_whitelist / model / throttle_seconds / max_concurrency）
```

> `image.group_whitelist` 与 `qq.group_whitelist` **完全解耦**：前者控制"哪些群的图片要 OCR"，后者控制"哪些群的文本要做作业识别"。`image.group_whitelist` 留空即整条图片管道不启用（`ImageOCR` 不会被创建，不消耗任何算力）。

> `.config/settings.py` 仅含默认值、可安全入库；而 `calendar.conf` / `config.yaml` 含密码与 token，已在 `.gitignore` 排除，请使用对应的 `.example` 模板。

---

## 三、主系统数据流

```
用户文本
  │
  ▼
api/schedule.py  →  POST /api/chat  {text}
  │
  ▼
core/llm_agent.py  →  PlanmeAgent.process_query()
  │  • 注入当前本地时间到 system prompt
  │  • 调用 Ollama chat（带 tools=create_item）
  │  • 最多重试 3 次；Schema 校验失败会把错误喂回模型
  ▼
_parse_response()
  ├─ tool_call  →  CalendarItemSchema 校验  ──► core/calendar_sync.py
  └─ text       →  直接自然语言回复
  │
  ▼
iCloudCalendarManager.create_item(item)
  ├─ item_type == "Event"  →  路由到「planme」日历（默认），写 Event（含 duration）
  └─ item_type == "Todo"   →  路由到「提醒」日历（默认），写 Todo（due = 截止时间）
  │
  ▼
iCloud CalDAV  (caldav 库)
```

**双日历路由规则**（`calendar_sync._get_calendar_by_type`）：
- `Event` 默认匹配名为 `planme` 的日历；`Todo` 默认匹配名为 `提醒` 的日历；
- 未匹配到时降级为第一个可用日历，并打印告警。

**时间约定**（`CalendarItemSchema.start_time`）：
- 格式固定 `YYYY-MM-DDTHH:MM:SS`，**不携带时区后缀**；
- 时区由后端统一按 `settings.TIMEZONE`（默认 `Asia/Shanghai`）补充；
- 仅给日期时：Event 默认 `10:00:00`，Todo 默认 `20:00:00`。

---

## 四、QQ 作业扫描器数据流

```
QQ 群消息
  │  NapCat (OneBot 11) 正向 WS  ws://127.0.0.1:3001
  ▼
core/homework/onebot_client.py  →  OneBotClient（断线自动重连）
  │  on_event() 按 message_type 分发；group 消息同时进入「文本管道」与「图片管道」
  ▼
HomeworkScanner._handle_group(event)        # 文本管道，见 core/homework/scanner.py
  ├─ 群白名单过滤（qq.group_whitelist）
  ├─ 发送者身份过滤（teacher_roles / teacher_user_ids）
  ├─ CQ 码剥离（strip_cq）
  ├─ message_store.save()  →  SQLite 增量去重（跨重启）
  ├─ detector.prefilter()  →  关键词预过滤（命中才调模型）
  ▼
core/homework/detector.py  →  HomeworkDetector.detect()
  ├─ 动态注入当前真实日期到 system prompt（避免相对日期年份错误）
  ├─ Ollama 结构化抽取 HomeworkExtraction
  ├─ 漏报兜底：关键词命中 + 已抽到期末时间 → 仍判为作业
  ▼
detector.decide_action()
  ├─ confidence > auto_confidence (0.9) → ACTION_AUTO  自动加入
  ├─ min(0.6) ≤ confidence ≤ auto       → ACTION_ASK   私聊确认
  └─ 否则                                → ACTION_DROP  静默丢弃
  ▼
  ├─ 落库 homework_items（status=pending / auto）    ★ 权威状态起点
  ▼
core/homework/notifier.py  →  Notifier
  ├─ AUTO：直接 SchedulerBridge.add_reminder() → POST /api/chat → 主系统写 iCloud
  │        └─ 回写 status=auto
  └─ ASK ：向主号私聊确认（cid = hw{message_id}，稳定可复现）
           主号回复：
             y/确认     → 加入   → 回写 status=confirmed
             n/取消     → 忽略   → 回写 status=ignored
             改 <时间>  → 改期后加入（同时更新 deadline）
             超时       → 自动忽略 → 回写 status=timeout
```

**关键设计点**
- `message_store` 以 `message_id` 为主键 `INSERT OR IGNORE` 天然去重，避免重复询问 / 重复写入。
- **cid 稳定化**：确认单号由早期的自增序号改为 `hw{message_id}`，因此重启后 db 里的记录仍能和私聊里的 `#编号` 对上，`rehydrate_from_db()` 可把 `status=pending` 的记录重新装回 `notifier.pending`。
- `OneBotClient` 用 `echo` 关联 API 请求 / 响应，断线后 `run_forever` 自动重连。
- 扫描器与主系统同进程运行，Web 既能读内存（pending / feed）也能读 db（items / notes）；扫描器崩溃被 `try/except` 隔离，不影响 FastAPI 主系统。
- **统一装配**：新增后台服务只需在 `app/services.py` 挂一个实例并在 `app/factory.py` 的 `lifespan` 中启停；新增 HTTP 接口只需在 `api/__init__.py` 的 `register_routers` 多 `include` 一个 router。

---

## 五、群图片 OCR 数据流

用途：把讲座 / 通知 / 海报类图片集合转成可检索、可阅读的 Markdown 存档。**与作业管道并行、互不干扰**。

```
QQ 群消息（含图片）
  │  NapCat (OneBot 11) 正向 WS
  ▼
HomeworkScanner._handle_images(event)
  ├─ 群白名单过滤（image.group_whitelist）   ← 独立白名单，留空则整条管道不启用
  ├─ 不做发送者身份过滤（群内任何人发的图都算通知）
  ├─ parse_cq_images()  →  从 CQ 码 [CQ:image,file=...,url=...] 解析出图片列表
  ▼
每张图片一个 asyncio 任务（受 max_concurrency 信号量限制）
  ▼
OneBotClient.fetch_image_path(file, url)
  ├─ 优先：OneBot `get_image` 动作 → 拿 NapCat 本地缓存的文件路径（零额外下载）
  └─ 兜底：HTTP GET url → 落到临时文件
  ▼
core/homework/ocr.py  →  ImageOCR.ocr(path)
  ├─ throttle_seconds 节流，防刷屏 / 风控
  ├─ ollama.Client.chat(
  │      model="qwen2.5vl:7b",
  │      messages=[{"role": "user", "content": <提示词>, "images": [path]}])
  │    ★ 视觉模型的用法就是「chat 里传模型名 + images 传图片路径」
  ▼
落库 lecture_notes
  ├─ 去重键 (message_id, image_seq)：一条消息多图各自成行，重复上报不会重复写
  ├─ OCR 成功 → status=active；异常 → status=error（保留记录，便于后续重跑）
  ▼
GET /api/lecture/notes  →  Web「🖼️ 讲座/通知」Tab 用 st.markdown 渲染
```

**设计权衡**
- **为什么优先 `get_image` 而不是直接下 url**：NapCat 已把图片缓存在本地，取路径比走 HTTP 更快、也不受图片 url 鉴权 / 过期影响；HTTP 只作兜底。
- **为什么每张图独立建 task**：OCR 是秒级操作，同步做会阻塞 WS 事件循环，导致后续群消息延迟；用信号量把并发压到 `max_concurrency`（默认 2）避免显存打满。
- **为什么失败也落库**：`status=error` 的记录保留了 `message_id` 与 `image_url`，将来可以做"重跑 OCR"而不必回溯聊天记录。

---

## 六、SQLite 数据表

默认库文件 `qq_homework.db`（路径由 `storage.db_path` 配置，相对路径按项目根解析；`resolve_db_path()` 是 API 侧共用的解析入口）。统一开启 **WAL**，使扫描器持续写入的同时 API 进程可并发读。

| 表 | 主键 / 去重键 | 关键字段 | 说明 |
| --- | --- | --- | --- |
| `messages` | `message_id` | `group_id` `user_id` `role` `content` `time` | 群消息原文，跨重启增量去重 |
| `group_progress` | `group_id` | `last_time` | 各群已处理时间水位 |
| `homework_items` | `message_id`（UNIQUE） | `cid` `subject` `deadline` `description` `confidence` `status` `raw_content` `created_at` `decided_at` | **作业识别结果 + 决策状态**（Web 权威列表） |
| `lecture_notes` | `(message_id, image_seq)` | `group_id` `group_name` `user_id` `image_url` `ocr_md` `status` `created_at` `ocr_at` | **图片 OCR 的 Markdown 存档** |

**状态取值**

| 表 | status | 含义 |
| --- | --- | --- |
| `homework_items` | `pending` | 已识别，等待主号私聊确认（重启后会被 rehydrate） |
| | `confirmed` | 主号回复 `y`，已提交主系统写入 iCloud |
| | `auto` | 置信度高于 `auto_confidence`，未经确认直接加入 |
| | `ignored` | 主号回复 `n` |
| | `timeout` | 超过 `confirm_timeout_seconds` 未回复，自动忽略 |
| `lecture_notes` | `active` | OCR 成功，`ocr_md` 有内容 |
| | `error` | OCR 异常，记录保留以便重跑 |

> ⚠️ `qq_homework.db` 含群聊原文与 OCR 文本，已被 `.gitignore`（`*.db`）排除，不要提交。

---

## 七、常驻调度器

`planme_guardian.py` 是单进程守护（替代早期的多 bat 启动方案）：
- 按设定时间（如每天 10:00 / 16:00 / 22:00）自动拉起 Ollama + NapCat + 扫描器；
- 运行一段时间后用 `taskkill /T` 清理子进程；
- 通过 `.guardian_stop` 标志实现优雅停止。

`guardian.bat` 为纯 ASCII 启动器（`start/stop/status/test/install/uninstall`），避免 Windows GBK 编码问题。
