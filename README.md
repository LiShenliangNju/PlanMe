# 📅 PlanMe · 智能日程管家

> 基于 **Ollama 本地大模型** + **iCloud CalDAV** 的智能日程 / 待办管理服务，并内置 **QQ 群作业扫描器** 与 **群图片 OCR 存档**：自动识别老师在群里布置的作业（经你确认后写入 iCloud 提醒事项），并把讲座 / 通知类图片用视觉模型转成 Markdown 归档。

---

## 一、项目简介

PlanMe 是一个本地优先（local-first）的日程自动化工具，核心思路是：

- 用**自然语言**告诉它任何日程或待办，本地 Ollama 模型负责理解意图、抽取结构化字段，并通过 **Tool Calling** 直接写入 iCloud 日历 / 提醒事项；
- 提供 **Streamlit** 可视化界面，既能聊天式创建，也能手动精准添加；
- 附带一个 **QQ 作业扫描器**（主程序同进程后台服务）：监听 NapCat（OneBot 11）转发的 QQ 群消息，用关键词预过滤 + 大模型抽取判断「是不是作业」，高置信度自动入日历，低置信度私聊问你确认，识别结果与决策状态**全程落库**；
- 附带 **群图片 OCR 存档**：对**独立白名单群**里的图片调用本地视觉模型 `qwen2.5vl`，转成 Markdown 存入 SQLite，在 Web 端直接渲染阅读（适合讲座、通知、海报类图片集合）。

所有 AI 推理都在你本机完成，**数据不出本机**（仅 iCloud 同步需要你的 Apple 账户凭据）。

---

## 二、✨ 功能特性

| 模块 | 能力 |
| --- | --- |
| **智能对话（主系统）** | 自然语言建日程 / 待办；Ollama `qwen2.5` 系列模型；基于 Pydantic Schema 的强校验与自动重试 |
| **双日历路由** | `Event` → 写入 iCloud「日程」日历；`Todo` → 写入 iCloud「提醒」日历（可按类型自动匹配） |
| **Web 界面** | Streamlit 双页签：AI 智能对话 + 手动快捷添加，实时展示系统健康状态 |
| **HTTP API** | `POST /api/chat`（自然语言）、`POST /api/manual-item`（绕过 AI）、`GET /api/health`（健康检查） |
| **QQ 作业扫描器** | 作为主程序的**同进程后台服务**运行（单一入口统一启停）；OneBot 11（NapCat）接入；关键词预过滤 + 大模型结构化抽取；**漏报兜底**避免真作业被静默丢弃 |
| **私聊确认状态机** | 识别到作业后向主号私聊，支持 `y / n / 改 <时间>` 指令；超时自动忽略 |
| **作业结果落库** | 识别结果与决策状态写入 `homework_items` 表（`pending / confirmed / auto / ignored / timeout`）；**重启不丢、可历史回看**，待确认项在重启后自动恢复 |
| **群图片 OCR 存档** | **独立的图片白名单群**：群内图片经 OneBot 抓取后调用本地视觉模型 `qwen2.5vl` 转 Markdown，存入 `lecture_notes` 表（按 `message_id + 图片序号` 去重、带节流与并发上限） |
| **Web 多页展示** | Streamlit 四页签：AI 对话 / 手动添加 / 🤖 QQ作业（**直读 db 权威列表** + 实时推送流）/ 🖼️ 讲座通知（渲染 OCR Markdown） |
| **常驻调度器** | `planme_guardian.py` 单进程守护，按设定时间自动拉起 Ollama + NapCat + 主系统（扫描器随主系统一并启动）并定时清理 |

---

## 三、🧱 系统架构

系统以 **单一入口 `main.py`** 启动 FastAPI 主系统，并在同一进程内按配置拉起可插拔的后台服务（如 homework 扫描器）。所有路由统一注册到 `api`，所有服务由 `app` 编排启停：

```
[ 输入层 ]
  用户自然语言 ──► Streamlit Web UI (4 页签)
  QQ 群消息 ─────► NapCat (OneBot 11 正向 WS, 127.0.0.1:3001)

        │                                    │
        ▼                                    ▼
[ 主系统 main.py ]  FastAPI :8000       [ homework 扫描器 ]
  路由集中注册于 api/__init__             主程序同进程后台任务
        │                                    │
        │                          ┌─────────┴─────────┐
        │                          ▼                   ▼
        │                  文本管道                图片管道
        │                  关键词预过滤            CQ 码解析
        │                  Ollama 抽取             OneBot 取图
        │                  置信度决策              qwen2.5vl OCR
        │                          │                   │
        │        POST /api/chat ◄──┤ 私聊确认           │
        ▼                          ▼                   ▼
[ 输出层 ]                    homework_items      lecture_notes
  Ollama Tool Calling              └────────┬──────────┘
        ▼                                   ▼
  iCloudCalendarManager            SQLite (WAL, qq_homework.db)
        ▼                                   ▲
  iCloud CalDAV                             │ Web 直读（重启不丢）
                                            │
  Streamlit ◄── /api/homework/items ────────┘
            ◄── /api/lecture/notes  ────────┘
            ◄── /api/homework/{pending,feed}（内存实时流）
```

**两条独立管道，两套独立白名单**：

| 管道 | 白名单 | 触发条件 | 产出 |
| --- | --- | --- | --- |
| 作业识别 | `qq.group_whitelist` | 文本消息 + **限老师身份** + 关键词命中 | `homework_items` 表 + iCloud 提醒 |
| 图片 OCR | `image.group_whitelist` | 群内**任意人**发的图片 | `lecture_notes` 表（Markdown） |

扫描器与主系统**同进程**运行，但**不做**日历写入——只把识别出的作业转成自然语言交给主系统处理，主系统是唯一写 iCloud 的入口。Web 端的「权威列表」统一**直读 SQLite**（重启不丢），内存 `feed` 只承担「实时推送流水」。

---

## 四、📦 技术栈

- **后端**：FastAPI + Uvicorn，Pydantic / pydantic-settings
- **本地大模型**：Ollama
  - 文本理解 / 作业抽取：`qwen2.5:7b-instruct-q4_K_M`
  - 图片 OCR（视觉）：`qwen2.5vl:7b`（通过 `chat(model=..., messages=[{..., "images": [path]}])` 调用）
- **日历同步**：`caldav`（iCloud CalDAV 协议）
- **前端**：Streamlit
- **QQ 接入**：OneBot 11（NapCat），`websockets` 客户端
- **存储**：SQLite（`aiosqlite`，WAL 模式）——消息去重、作业状态、OCR 存档
- **守护进程**：纯 Python + Windows `taskkill`（调度 / 清理）

---

## 五、🚀 安装与部署

### 5.1 前置依赖

- Python ≥ 3.10
- 已安装并运行 **Ollama**，且已拉取模型（见 5.4）
- 一个 iCloud 账户（用于 CalDAV 同步）
- （可选，启用 QQ 作业扫描器）一台已登录 **NapCat** 小号的 Windows 机器

### 5.2 获取代码与安装依赖

```bash
git clone https://github.com/<你的用户名>/PlanMe.git
cd PlanMe
pip install -r requirements.txt
```

### 5.3 配置

项目有三处本地配置，**均含敏感信息，已通过 `.gitignore` 排除，不会进入版本库**。请复制模板后填写：

```bash
# 1) 环境变量
cp .env.example .env

# 2) iCloud CalDAV 凭据（填入你的 Apple ID 与应用专用密码）
cp .config/caldav/calendar.conf.example .config/caldav/calendar.conf

# 3) （可选）QQ 作业扫描器
cp .config/hmwk_scnr/config.yaml.example .config/hmwk_scnr/config.yaml
```

> ⚠️ iCloud 密码请使用 **App 专用密码**（appleid.apple.com → 安全 → 生成），不要用 Apple ID 主密码。
> ⚠️ NapCat 必须仅监听 `127.0.0.1`，`access_token` 务必是强随机串，切勿暴露到公网。

### 5.4 启动 Ollama

```bash
# 文本模型（必需）：自然语言理解 + 作业抽取
ollama pull qwen2.5:7b-instruct-q4_K_M

# 视觉模型（可选，仅启用群图片 OCR 时需要）
ollama pull qwen2.5vl:7b

ollama serve        # 默认监听 http://localhost:11434
```

### 5.5 启动主系统

```bash
python main.py
# 或：uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

健康检查：`curl http://localhost:8000/api/health`

### 5.6 启动 Web 界面（可选）

```bash
streamlit run web/app.py
```

打开浏览器访问 Streamlit 提示的地址，即可用自然语言添加日程，或手动精准填写。

### 5.7 启用 QQ 作业扫描器（可选）

QQ 作业扫描器已并入主程序**单一入口**：启动 `main.py` 时会按 `ENABLE_HOMEWORK`（默认 `true`，可在 `.env` 覆盖）自动作为同进程后台任务拉起，无需再单独运行。

1. 按 `docs/DEPLOYMENT.md` 配置并启动 NapCat（小号登录、正向 WS 监听 `127.0.0.1:3001`）；
2. 填写 `.config/hmwk_scnr/config.yaml`；
3. 正常启动主系统即可（`python main.py`），扫描器随之运行：

```bash
python main.py
# 可选：临时关闭扫描器
ENABLE_HOMEWORK=false python main.py
```

识别到作业后，主号会收到私聊确认；回复 `y` 加入、`n` 忽略、`改 <时间>` 改期。识别结果与状态会写入 `homework_items` 表，Web 界面的「🤖 QQ作业」页**直接读 db** 展示（重启不丢），旁边同时显示内存里的实时推送流。

### 5.8 启用群图片 OCR 存档（可选）

用于把讲座 / 通知 / 海报类图片自动转成可检索的 Markdown。**它有自己独立的白名单，默认关闭**。

1. 拉取视觉模型：`ollama pull qwen2.5vl:7b`；
2. 在 `.config/hmwk_scnr/config.yaml` 的 `image:` 段填入群号：

```yaml
image:
  group_whitelist: [123456789]   # 留空 [] = 关闭图片抓取
  model: "qwen2.5vl:7b"
  throttle_seconds: 3            # 两次 OCR 最小间隔
  max_concurrency: 2             # 并发 OCR 上限（显存紧张调小）
```

3. 重启主系统即可。白名单群里**任何人**发的图片都会被抓取 → OCR → 存入 `lecture_notes` 表，在 Web 的「🖼️ 讲座/通知」页按时间倒序渲染。

> 与作业扫描的区别：作业管道只认**老师身份的文本消息**；图片管道不限身份，只看群号是否在 `image.group_whitelist` 里。两者互不影响。

### 5.9 常驻调度器（可选，Windows）

`planme_guardian.py` 提供单进程守护，按设定时间自动拉起相关服务并清理：

```bat
guardian.bat start      # 启动（常驻，无窗口）
guardian.bat stop       # 优雅停止（写停止标志）
guardian.bat status     # 查看是否运行 / 下次触发时间
guardian.bat test       # 自检路径与端口，不启动任何服务
```

---

## 六、📁 目录结构

```
PlanMe/
├── main.py                      # 唯一启动入口：create_app() + 后台服务编排 + uvicorn
├── planme_guardian.py           # 常驻调度器（守护进程，仅拉起 main.py 单一入口）
├── guardian.bat                # Windows 启动器（纯 ASCII，GBK 安全）
├── requirements.txt            # 统一依赖清单
├── .env.example                # 环境变量模板（真实 .env 请勿提交）
├── .gitignore
├── README.md / CONTRIBUTING.md / LICENSE
├── docs/
│   ├── ARCHITECTURE.md         # 详细架构与数据流说明
│   └── DEPLOYMENT.md           # 部署、NapCat 对接、排障
├── app/
│   ├── __init__.py             # 集中把 .config 注入 sys.path
│   ├── factory.py              # create_app()：CORS + 集中注册路由 + 后台服务启停
│   └── services.py             # ServiceManager：后台服务注册表（start/stop 扩展点）
├── api/
│   ├── __init__.py             # register_routers(app)：统一挂载各模块 router
│   ├── schedule.py             # 原计划 routes.py：/api/chat、/api/manual-item、/api/health
│   ├── homework.py             # /api/homework/*：status/pending/feed（内存）+ items（直读 db）
│   ├── lecture.py              # ★ /api/lecture/notes：直读 lecture_notes 表（OCR Markdown）
│   └── napcat.py               # /api/napcat/* 连接状态与推送流
├── core/
│   ├── __init__.py
│   ├── llm_agent.py            # PlanmeAgent：Ollama 对话 + Tool Calling
│   ├── calendar_sync.py        # iCloudCalendarManager：CalDAV 写入（Event/Todo 路由）
│   ├── homework/               # QQ 群作业扫描器 + 图片 OCR（主程序同进程后台服务）
│       ├── __init__.py         # 导出 HomeworkScanner
│       ├── scanner.py          # HomeworkScanner 服务类（配置加载、连接 NapCat、文本/图片双管道路由）
│       ├── detector.py         # 关键词预过滤 + Ollama 结构化抽取（含漏报兜底）
│       ├── ocr.py              # ★ ImageOCR：qwen2.5vl 视觉模型把图片转 Markdown（带节流）
│       ├── onebot_client.py    # OneBot 11（NapCat）WS 客户端，含 get_image / 图片下载兜底
│       ├── message_store.py    # SQLite 存储层（4 张表 + WAL + 去重 + 状态更新）
│       ├── scheduler_bridge.py # 把作业 POST 给主系统 /api/chat
│       └── notifier.py         # 私聊确认状态机（y / n / 改），写状态到 db，并向 feed 发布事件
│   └── napcat/                 # ★ NapCat 集成层
│       ├── __init__.py
│       └── feed.py             # FeedBus 事件总线：qqbot 推送 + 建议日程，供 API/Web 消费
├── schemas/
│   ├── schedule_schema.py      # CalendarItemSchema（日程 / 待办结构化规范）
│   └── homework_schema.py      # Sender / GroupMessage / HomeworkExtraction / ReminderPayload
│                               #   + HomeworkItem（落库的作业与状态）+ LectureNote（OCR 存档）
├── web/
│   └── app.py                  # Streamlit 前端（4 页签：对话 / 手动 / 🤖QQ作业 / 🖼️讲座通知）
├── test/
│   ├── icloud_server.py        # iCloud / CalDAV 连接测试
│   ├── ollama_server.py        # Ollama 连接测试
│   └── tool_call.py            # Tool Calling 调用示例
└── .config/                    # 本地配置（不进版本库，见 *.example）
    ├── settings.py             # 全局设置（可提交，仅默认值；含 ENABLE_HOMEWORK 开关）
    ├── caldav/calendar.conf    # ⚠️ 含 iCloud 密码，gitignore
    └── hmwk_scnr/config.yaml   # ⚠️ 含 token / QQ 号，gitignore
```

---

## 七、🗄️ 数据存储与接口清单

### 7.1 SQLite 数据表（默认 `qq_homework.db`，WAL 模式）

| 表 | 作用 | 去重键 |
| --- | --- | --- |
| `messages` | 群消息原文，跨重启增量去重 | `message_id`（主键） |
| `group_progress` | 各群已处理到的时间水位 | `group_id`（主键） |
| `homework_items` | **作业识别结果 + 决策状态**（Web 权威列表） | `message_id`（UNIQUE） |
| `lecture_notes` | **图片 OCR 得到的 Markdown 存档** | `(message_id, image_seq)` |

`homework_items.status` 取值：`pending`（待你私聊确认）、`confirmed`（你确认后已入日历）、`auto`（高置信度自动入）、`ignored`（你回复 n）、`timeout`（超时自动忽略）。
`lecture_notes.status` 取值：`active`（OCR 成功）、`error`（OCR 失败，可后续重跑）。

> WAL 模式让 API 进程并发读、扫描器持续写互不阻塞，因此 Web 可以随时直读 db。

### 7.2 HTTP 接口

| 方法 | 路径 | 说明 | 数据来源 |
| --- | --- | --- | --- |
| `POST` | `/api/chat` | 自然语言创建日程 / 待办 | Ollama + CalDAV |
| `POST` | `/api/manual-item` | 结构化创建（绕过 AI） | CalDAV |
| `GET` | `/api/health` | 健康检查 | — |
| `GET` | `/api/homework/status` | 扫描器是否启用 / 运行中 | 内存 |
| `GET` | `/api/homework/pending` | 当前等待私聊确认的作业 | 内存（notifier） |
| `GET` | `/api/homework/feed` | 最近的推送 / 建议日程事件 | 内存（feed） |
| `GET` | `/api/homework/items?limit=200` | **作业权威列表（含状态、可历史回看）** | db `homework_items` |
| `GET` | `/api/lecture/notes?limit=100` | **讲座 / 通知 OCR 存档（Markdown）** | db `lecture_notes` |
| `GET` | `/api/napcat/*` | NapCat 连接状态与推送流 | 内存 |

---

## 八、🔧 常用命令

```bash
# 健康检查
curl http://localhost:8000/api/health

# 自然语言建日程
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"提醒我下周五18:00前提交行策期末报告"}'

# 手动建待办
curl -X POST http://localhost:8000/api/manual-item \
  -H "Content-Type: application/json" \
  -d '{"item_type":"Todo","summary":"交线性代数作业","start_time":"2026-08-20T20:00:00"}'

# 查看作业落库列表（含状态）
curl http://localhost:8000/api/homework/items?limit=20

# 查看讲座 / 通知 OCR 存档
curl http://localhost:8000/api/lecture/notes?limit=5
```

---

## 九、🔐 安全说明

- 本项目**不收集、不上传**你的任何日程或聊天内容；所有模型推理（含图片 OCR）均在本地 Ollama 完成。
- iCloud / NapCat 凭据仅存于你本机的 `.config/*.conf` 与 `.env`，已在 `.gitignore` 中排除。
- `qq_homework.db` 含群消息与 OCR 原文，已被 `.gitignore` 排除（`*.db`），不会误提交。
- QQ 作业扫描器仅建议在本机 `127.0.0.1` 运行，避免 token 与账号暴露。
- 图片抓取默认**关闭**（`image.group_whitelist` 为空），需你显式填入群号才会生效。

---

## 十、🤝 贡献指南

欢迎 Issue 与 PR！请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，了解分支规范、提交信息与代码风格要求。

---

## 十一、📄 许可证

本项目基于 **MIT 许可证** 开源，详见 [LICENSE](LICENSE)。

---

## English Abstract

**PlanMe** is a local-first smart schedule manager built on **Ollama** (local LLM) and **iCloud CalDAV**.
It turns natural-language requests into iCloud calendar events / reminders via Tool Calling, and ships a Streamlit UI.

Two optional QQ pipelines run as in-process background services via **OneBot 11 / NapCat**, each with its **own group whitelist**:

1. **Homework scanner** — detects assignments in whitelisted group chats (keyword prefilter + LLM extraction),
   asks for confirmation over private message (`y` / `n` / reschedule), and writes them to iCloud reminders.
   Every extraction and decision is persisted to SQLite (`homework_items`), so the web list survives restarts.
2. **Image OCR archive** — for a separate image whitelist, group images are fetched via OneBot and converted to
   Markdown by the local vision model **`qwen2.5vl`**, stored in `lecture_notes` and rendered in the web UI.
   Useful for lecture / announcement posters.

All inference runs locally; only iCloud sync needs your Apple credentials.
MIT licensed. See `docs/` for architecture and deployment details.
