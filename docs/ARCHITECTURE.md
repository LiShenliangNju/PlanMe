# 🧱 PlanMe 架构与数据流

本文档补充 `README.md` 中的架构概览，说明各模块职责、关键数据流与配置加载方式。

---

## 一、总体划分

PlanMe 由两个**解耦**的部分组成：

| 部件 | 进程 | 入口 | 职责 |
| --- | --- | --- | --- |
| **主系统** | FastAPI (`uvicorn`, 8000) | `main.py` | 理解自然语言、调用 Ollama、写入 iCloud |
| **QQ 作业扫描器** | 独立进程 | `python -m core.homework` | 监听 QQ 群消息、识别作业、私聊确认、转发主系统 |

两者通过 HTTP（`POST /api/chat`）协作，**扫描器不做日历写入**，只把作业转成自然语言交给主系统处理。这样主系统是唯一写入 iCloud 的入口，便于审计与复用。

---

## 二、配置加载链路

```
main.py / core/homework/__main__.py
        │  sys.path.insert(0, "<ROOT>/.config")
        ▼
.config/settings.py  ──►  Settings(BaseSettings)
        │  • 默认值：OLLAMA_HOST / OLLAMA_MODEL / TIMEZONE ...
        │  • env_file = ".env"（可被 .env 覆盖）
        │  • 把 CALDAV_CONFIG_FILE / HMWK_SCRN_CONFIG_FILE 注入 os.environ
        ▼
caldav.get_calendars() 读取 .config/caldav/calendar.conf
core/homework/__main__.py 读取 .config/hmwk_scnr/config.yaml
```

> `.config/settings.py` 仅含默认值、可安全入库；而 `calendar.conf` / `config.yaml` 含密码与 token，已在 `.gitignore` 排除，请使用对应的 `.example` 模板。

---

## 三、主系统数据流

```
用户文本
  │
  ▼
api/routes.py  →  POST /api/chat  {text}
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
  │  on_event() 按 message_type 分发
  ▼
__main__.handle_group(event)
  ├─ 群白名单过滤（group_whitelist）
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
core/homework/notifier.py  →  Notifier
  ├─ AUTO：直接 SchedulerBridge.add_reminder() → POST /api/chat → 主系统写 iCloud
  └─ ASK ：向主号私聊确认
           主号回复：
             y/确认     → 加入
             n/取消     → 忽略
             改 <时间>  → 改期后加入
             超时       → 自动忽略
```

**关键设计点**
- `message_store` 以 `message_id` 为主键 `INSERT OR IGNORE` 天然去重，避免重复询问 / 重复写入。
- `OneBotClient` 用 `echo` 关联 API 请求 / 响应，断线后 `run_forever` 自动重连。
- 扫描器与主系统彻底解耦：即使扫描器崩溃，也不影响主系统；反之同理。

---

## 五、常驻调度器

`planme_guardian.py` 是单进程守护（替代早期的多 bat 启动方案）：
- 按设定时间（如每天 10:00 / 16:00 / 22:00）自动拉起 Ollama + NapCat + 扫描器；
- 运行一段时间后用 `taskkill /T` 清理子进程；
- 通过 `.guardian_stop` 标志实现优雅停止。

`guardian.bat` 为纯 ASCII 启动器（`start/stop/status/test/install/uninstall`），避免 Windows GBK 编码问题。
