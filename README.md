# 📅 PlanMe · 智能日程管家

> 基于 **Ollama 本地大模型** + **iCloud CalDAV** 的智能日程 / 待办管理服务，并内置 **QQ 群作业扫描器** 与 **群图片 OCR 存档**：自动识别老师在群里布置的作业（经你确认后写入 iCloud 提醒事项），并把讲座 / 通知类图片用视觉模型转成 Markdown 归档。

---

## 一、项目简介

PlanMe 是一个本地优先（local-first）的日程自动化工具，核心思路是：

- 用**自然语言**告诉它任何日程或待办，本地 Ollama 模型负责理解意图、抽取结构化字段，并通过 **Tool Calling** 直接写入 iCloud 日历 / 提醒事项；
- 自带 **Web 前端**（内置 SPA，随主系统挂载于 `/ui`，启动后自动打开），既能聊天式创建，也能手动精准添加；
- 附带一个 **QQ 作业扫描器**（主程序同进程后台服务）：监听 NapCat（OneBot 11）转发的 QQ 群消息，用关键词预过滤 + 大模型抽取判断「是不是作业」，高置信度自动入日历，低置信度私聊问你确认，识别结果与决策状态**全程落库**；
- 附带 **群图片 OCR 存档**：对**独立白名单群**里的图片调用本地视觉模型 `qwen2.5vl`，转成 Markdown 存入 SQLite，在 Web 端直接渲染阅读（适合讲座、通知、海报类图片集合）。

所有 AI 推理都在你本机完成，**数据不出本机**（仅 iCloud 同步需要你的 Apple 账户凭据）。

---

## 二、✨ 功能特性

| 模块 | 能力 |
| --- | --- |
| **智能对话（主系统）** | 自然语言建日程 / 待办；Ollama `qwen2.5` 系列模型；基于 Pydantic Schema 的强校验与自动重试 |
| **双日历路由** | `Event` → 写入 iCloud「日程」日历；`Todo` → 写入 iCloud「提醒」日历（可按类型自动匹配） |
| **Web 界面** | 内置 SPA（`web/frontend/`，挂载于 `/ui`）：仪表盘 / 智能对话 / 手动添加 / QQ作业 / 讲座通知 / 连接状态 / 配置白名单 七页签，`main.py` 启动后自动在浏览器打开 |
| **HTTP API** | `POST /api/chat`（自然语言，支持多轮 `history` 记忆）、`POST /api/manual-item`（绕过 AI）、`GET /api/health`（健康检查）、`GET/POST /api/config/whitelist`（白名单读写） |
| **多轮对话记忆** | 前端把渲染过的对话历史随 `/api/chat` 回传，后端清洗（去脏结构、截断、最多保留最近 10 轮）后回灌给 Ollama，同一会话上下文连贯 |
| **白名单在线编辑** | Web「配置/白名单」页可直接增删作业群 / 图片 OCR 群；后端对 `config.yaml` **定点改写保注释**（不整文件重写、不丢 YAML 注释），保存后重启扫描器生效 |
| **QQ 作业扫描器** | 作为主程序的**同进程后台服务**运行（单一入口统一启停）；OneBot 11（NapCat）接入；关键词预过滤 + 大模型结构化抽取；**漏报兜底**避免真作业被静默丢弃 |
| **私聊确认状态机** | 识别到作业后向主号私聊，支持 `y / n / 改 <时间>` 指令；超时自动忽略 |
| **作业结果落库** | 识别结果与决策状态写入 `homework_items` 表（`pending / confirmed / auto / ignored / timeout`）；**重启不丢、可历史回看**，待确认项在重启后自动恢复 |
| **群图片 OCR 存档** | **独立的图片白名单群**：群内图片经 OneBot 抓取后落盘，先写库 `status=pending` 再交给后台 OCR 队列（worker 串行慢慢解析），存入 `lecture_notes` 表（按 `message_id + 图片序号` 去重）；**进程被杀 / OCR 失败 / 重启都不丢图**，pending 项重启后自动续跑 |
| **历史补抓（catchup）** | NapCat 是纯 push 模式，进程离线时段不重放；启动时按 `group_progress` 断点（`last_time` + `last_message_id`）调用 OneBot `get_group_msg_history` 把空窗期补回，正序重放复用同一条 `_on_event` 链路（断点 `MAX()` 单调递增，二次补抓不重复入队） |
| **GPU 单卡推理互斥** | 文本识别（`qwen2.5:7b`）、图片 OCR（`qwen2.5vl:7b`）、主系统对话（`qwen2.5:7b`）三路共用一把全局锁 `core/ollama_gpu.inference_lock`，同一时刻只有一次 Ollama 推理在跑 —— 单张 8GB 卡不再被两模型并发争抢、反复 swap |
| **Web 多页展示** | 内置 SPA 七页签：仪表盘 / AI 对话（**多轮记忆**）/ 手动添加 / 🤖 QQ作业（**直读 db 权威列表** + 实时推送流）/ 🖼️ 讲座通知（渲染 OCR Markdown）/ 连接状态 / ⚙️ 配置·白名单（**在线编辑，定点改写保注释**） |

---

## 三、🧱 系统架构

系统以 **单一入口 `main.py`** 启动 FastAPI 主系统，并在同一进程内按配置拉起可插拔的后台服务（如 homework 扫描器）。所有路由统一注册到 `api`，所有服务由 `app` 编排启停：

```
[ 输入层 ]
  用户自然语言 ──► 内置 SPA (/ui，随主系统挂载、启动自动打开)
  QQ 群消息 ─────► NapCat (OneBot 11 正向 WS, 127.0.0.1:3001)

        │                                    │
        ▼                                    ▼
[ 主系统 main.py ]  FastAPI :8000       [ homework 扫描器 ]
  路由集中注册于 api/__init__             主程序同进程后台任务
        │                                    │
        │          ┌───────────────┴───────────────┐
        │          ▼                               ▼
        │   文本管道                          图片管道
        │   关键词预过滤                    CQ 码解析 → 落盘
        │   Ollama 抽取                     写库 status=pending
        │   置信度决策                     入 OCR 队列 → worker
        │        │                          qwen2.5vl OCR
        │        │                               │
        │        │  POST /api/chat ◄──┐ 私聊确认 │
        ▼        ▼                    ▼         ▼
[ 输出层 ]   homework_items        lecture_notes
                                          (pending/active/error)

  WS 连上后(on_ready) ──► 历史补抓 get_group_msg_history
                          按 group_progress 断点补回空窗期，正序重放

  ┌─────────────────────────────────────────────────────┐
  │ 全局 GPU 推理锁 inference_lock（core/ollama_gpu.py）   │
  │ 文本检测 / 图片 OCR / 主系统 /api/chat 共用一把锁      │
  │ ⇒ 同一时刻仅一次 Ollama 推理，单卡不再并发 swap        │
  └─────────────────────────────────────────────────────┘
  Ollama Tool Calling              └────────┬──────────┘
        ▼                                   ▼
  iCloudCalendarManager            SQLite (WAL, qq_homework.db)
        ▼                                   ▲
  iCloud CalDAV                             │ Web 直读（重启不丢）
                                            │
  SPA (/ui) ◄── /api/homework/items ────────┘
           ◄── /api/lecture/notes  ────────┘
           ◄── /api/homework/{pending,feed}（内存实时流）
           ◄── /api/config/whitelist（白名单读写）
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
  - 调用调优：`keep_alive="30m"`（模型常驻显存，根治反复冷启动）、`num_ctx=8192`（压 KV cache 防 CPU offload）、`num_predict=2048`、`Client(timeout=300)`；启动时 `warmup()` 预热
  - **GPU 单卡互斥**：`core/ollama_gpu.inference_lock` 全局 `asyncio.Lock()`，三路推理共用，保证同一时刻仅一次调用（单卡只装得下一个模型）
- **日历同步**：`caldav`（iCloud CalDAV 协议）
- **前端**：内置 SPA（原生 HTML / CSS / JS，`web/frontend/`，经 FastAPI `StaticFiles` 挂载于 `/ui`，与后端同源、直接 fetch `/api/*`）
- **QQ 接入**：OneBot 11（NapCat），`websockets` 客户端
- **存储**：SQLite（`aiosqlite`，WAL 模式）——消息去重、作业状态、OCR 存档

---

## 五、🚀 安装与部署

### 5.1 前置依赖

- Python ≥ 3.10
- 已安装并运行 **Ollama**，且已拉取模型（见 5.4）
- 一个 iCloud 账户（用于 CalDAV 同步）
- （可选，启用 QQ 作业扫描器）一台已登录 **NapCat** 小号的 Windows 机器

### 5.2 获取代码与安装依赖

```bash
git clone https://github.com/LiShenliangNju/PlanMe.git
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

### 5.5 启动主系统（前端 UI 会自动打开）

```bash
# 推荐：用内置入口（main.py）
#   - 启动 FastAPI 主系统（uvicorn，端口 8000，默认 --reload 热重载）
#   - 前端 SPA 已随主系统挂载在 /ui，服务就绪后自动在浏览器打开 http://localhost:8000/ui/
python main.py

# 只想跑 API、不自动打开浏览器：关闭自动打开
ENABLE_WEB=false python main.py

# 或纯 uvicorn 启动（不带 --reload，避免旧进程残留，见下方注意 / docs/DEPLOYMENT.md 第四章）
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

> ⚠️ **改完代码后务必彻底停掉旧 uvicorn 再重启**：`main.py` 默认 `--reload`，会起「父进程 + 子进程」；若旧子进程没被杀干净，它可能仍霸占 8000 端口跑旧代码，表现为接口 404、前端列表显示 `(db, 0)`（数据其实已经在库里）。排障方式见 `docs/DEPLOYMENT.md` 第四章。

健康检查：`curl http://localhost:8000/api/health`

### 5.6 Web 界面（随主系统自动打开）

`python main.py` 启动时会**自动打开前端 SPA**（`http://localhost:8000/ui/`），无需再单独执行命令。该 SPA 由 FastAPI 直接挂载（`app/factory.py` 中的 `StaticFiles`，指向 `web/frontend/`），与后端**同源**，无需跨域配置。若只想独立调试前端静态页，直接访问 `/ui/` 即可；前端页面本身不含后端逻辑，所有数据都通过 `fetch('/api/*')` 获取。

打开浏览器即可使用自然语言添加日程、手动精准填写、查看作业与讲座列表、在线编辑白名单。

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
  max_concurrency: 1             # OCR worker 数（= 并发上限；显存紧张保持 1）
  throttle_seconds: 0            # 0 = 不额外空等，串行完全由 max_concurrency 保证
  keep_alive: "30m"              # 模型常驻显存，避免每张图重新加载权重（治冷启动卡顿）
  num_ctx: 8192                  # 上下文长度，过大易触发 CPU offload 拖慢速度
  num_predict: 2048              # 单张图最大输出 token
  request_timeout: 300           # 单次 OCR 超时（秒）
  warmup: true                   # 启动时预热模型，第一张图不等冷启动
  retry_max: 2                   # 单图最大重试次数，超过标记 error
  image_dir: "data/lecture_images"  # 图片落盘目录（重启续跑依赖它）
```

3. 重启主系统即可。白名单群里**任何人**发的图片都会被抓取 → **落盘 + 写库（status=pending）→ 后台 worker 排队 OCR** → 存入 `lecture_notes` 表，在 Web 的「🖼️ 讲座/通知」页按时间倒序渲染。此「先落库后 OCR」模式保证：**进程被杀、OCR 中途失败、重启都不丢图**，pending 项会在下次启动时自动续跑。

> 与作业扫描的区别：作业管道只认**老师身份的文本消息**；图片管道不限身份，只看群号是否在 `image.group_whitelist` 里。两者互不影响。
>
> ⚠️ **GPU 共享锁**：文本识别、图片 OCR、主系统对话三路共用一把全局推理锁（`core/ollama_gpu.py`），同一时刻只有一次 Ollama 推理在跑。若你的 GPU 单卡装不下两个 7B 模型同跑，这是必要的互斥保护；彻底消除模型切换代价可把文本识别也改用 `qwen2.5vl:7b`（单模型方案，见下文 5.10）。

### 5.9 验证

项目在 `test/` 下提供若干可独立运行的验证脚本（`python test/xxx.py`）：

| 脚本 | 验证内容 | 前置 |
| --- | --- | --- |
| `test/ollama_server.py` | Ollama 服务连通性与 `generate` 调用 | 已启动 Ollama |
| `test/icloud_server.py` | iCloud / CalDAV 连接与 Event/Todo 创建 | 已配置 `.config/caldav/calendar.conf` |
| `test/tool_call.py` | Ollama Tool Calling 示例 | 已启动 Ollama |
| `test/qwenvl_ocr.py` | **qwen2.5vl 视觉模型 OCR 端到端**（图片→Markdown） | 已 `ollama pull qwen2.5vl:7b`；可选 Pillow |
| `test/queue_catchup_check.py` | **离线自检**：幂等迁移补列、断点单调递增、消息去重、OCR 队列状态机、消息归一化、CQ 反转义、补抓分页/断点过滤/正序重放/去重、图片留痕、GPU 共享锁互斥（共 28 项） | 无（纯逻辑，不依赖 Ollama） |

视觉 OCR 测试用法：

```bash
# 无参：自动用 Pillow 生成一张带文字的测试图
python test/qwenvl_ocr.py
# 或对指定图片做 OCR
python test/qwenvl_ocr.py "D:/xxx/讲座通知.png"
# 模型 / 地址可环境变量覆盖
MODEL=qwen2.5vl:7b OLLAMA_HOST=http://localhost:11434 python test/qwenvl_ocr.py
```

> 该测试会先探活 Ollama 并确认 `qwen2.5vl:7b` 已拉取，再调用 `core/homework/ocr.ImageOCR` 打印 Markdown 结果；输出非空即视为通过。

### 5.10 历史补抓（catchup）与离线窗口

NapCat 是**纯 push 模式**：只在 WebSocket 连接期间把新群消息推过来，进程没跑的时段**不会重放**，那些消息会永久丢失。为此扫描器在 **WS 连上后（`on_ready`）** 按各群断点补抓：

- 断点存于 `group_progress` 表：`last_time`（处理到的时间戳）+ `last_message_id`（消息锚点），写入用 `MAX()` 保证**单调递增**，补抓重放老消息不会把锚点拽回去。
- 启动时调用 OneBot `get_group_msg_history` 分页拉取断点之后的历史，按时间自排序、断点过滤、**正序重放**复用同一条 `_on_event` 链路（`_history_to_event`），去重完全交给主键 / `UNIQUE(message_id, image_seq)`。
- 补抓重放会触发 notifier 私聊确认 —— 一次补出多条作业就会连发多条私聊，属预期行为。

在 `.config/hmwk_scnr/config.yaml` 的 `catchup:` 段调整：

```yaml
catchup:
  enabled: true              # 关闭则启动时不再补抓
  page_size: 50              # 每次 get_group_msg_history 拉取的条数
  max_pages: 5               # 单群最多翻页数（page_size × max_pages = 250 条上限）
  max_messages_per_group: 200
  max_age_hours: 72          # 只补最近 72 小时内的消息
  min_interval_seconds: 300  # 重连风暴防护：两次补抓最小间隔
  include_all_groups: false  # false=仅白名单+有断点群；true=所有已加入群（历史量大慎用）
```

> 局限：单群单次补抓上限约 250 条（`page_size × max_pages`）。若某群在离线窗口内消息量超过该上限，最老的消息仍可能漏抓；要消除可调大 `max_pages` 或改游标抓取。

---
## 六、📁 目录结构

```
PlanMe/
├── main.py                      # 唯一启动入口：create_app() + 后台服务编排 + uvicorn + 自动打开前端 /ui
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
│   ├── schedule.py             # /api/chat（自然语言，含多轮 history）、/api/manual-item、/api/health
│   ├── homework.py             # /api/homework/*：status/pending/feed（内存）+ items（直读 db）
│   ├── lecture.py              # ★ /api/lecture/notes：直读 lecture_notes 表（OCR Markdown）
│   ├── napcat.py               # /api/napcat/* 连接状态与推送流
│   └── config.py               # ★ /api/config/whitelist：白名单读取 + 定点改写保注释写回
├── core/
│   ├── __init__.py
│   ├── ollama_gpu.py           # ★ 单卡 GPU 全局推理锁 inference_lock（三路 Ollama 调用共用）
│   ├── llm_agent.py            # PlanmeAgent：Ollama 对话 + Tool Calling（async，调用前 await 共享锁）
│   ├── calendar_sync.py        # iCloudCalendarManager：CalDAV 写入（Event/Todo 路由）
│   ├── homework/               # QQ 群作业扫描器 + 图片 OCR（主程序同进程后台服务）
│       ├── __init__.py         # 导出 HomeworkScanner
│       ├── __main__.py         # `python -m core.homework` 独立运行入口（扫描器 standalone 启动）
│       ├── scanner.py          # HomeworkScanner 服务类：双管道路由 + 落库 OCR 队列 + 历史补抓
│       ├── detector.py         # 关键词预过滤 + Ollama 结构化抽取（含漏报兜底，调用前 await 共享锁）
│       ├── ocr.py              # ★ ImageOCR：qwen2.5vl 视觉模型转 Markdown；keep_alive/num_ctx/warmup + 队列消费（await 共享锁）
│       ├── onebot_client.py    # OneBot 11（NapCat）WS 客户端：get_image / 图片下载兜底 / get_group_msg_history / on_ready 钩子
│       ├── message_store.py    # SQLite 存储层（4 张表 + WAL + 幂等迁移 + 去重 + OCR 队列状态机 + 补抓锚点）
│       ├── scheduler_bridge.py # 把作业 POST 给主系统 /api/chat
│       └── notifier.py         # 私聊确认状态机（y / n / 改），写状态到 db，并向 feed 发布事件
│   └── napcat/                 # ★ NapCat 集成层
│       ├── __init__.py
│       └── feed.py             # FeedBus 事件总线：qqbot 推送 + 建议日程，供 API/Web 消费
├── schemas/
│   ├── schedule_schema.py      # CalendarItemSchema（日程 / 待办结构化规范）
│   └── homework_schema.py      # Sender / GroupMessage / HomeworkExtraction / ReminderPayload
│                               #   + HomeworkItem（落库的作业与状态）+ LectureNote（OCR 存档，含 local_path/attempts/error）
│                               #   + GroupProgress（补抓锚点：last_time + last_message_id）
├── web/
│   ├── frontend/               # ★ 内置 SPA 前端（挂载于 /ui，启动自动打开）
│   │   ├── index.html          # 页面骨架：七页签结构 + 侧边栏 + 顶栏
│   │   ├── styles.css          # 视觉样式（严格还原设计稿：深色侧栏/米白底/Action Blue/发丝线）
│   │   └── app.js              # 路由切换 / 状态管理 / fetch 对接 REST / 多轮对话 / 白名单编辑
│   └── (app.py 已随 Streamlit 过渡方案移除)
├── test/
│   ├── icloud_server.py        # iCloud / CalDAV 连接测试
│   ├── ollama_server.py        # Ollama 连接测试
│   ├── tool_call.py            # Tool Calling 调用示例
│   └── qwenvl_ocr.py           # qwen2.5vl 视觉模型 OCR 端到端测试
│   └── queue_catchup_check.py  # ★ 离线自检 28 项：迁移/断点/去重/队列状态机/补抓/GPU 锁
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
| `group_progress` | 各群已处理到的时间水位（**补抓锚点**：`last_time` + `last_message_id`，写入取 `MAX()` 保证单调递增） | `group_id`（主键） |
| `homework_items` | **作业识别结果 + 决策状态**（Web 权威列表） | `message_id`（UNIQUE） |
| `lecture_notes` | **图片 OCR 队列与 Markdown 存档**（先落库 `pending` 再排队 OCR，含 `local_path` / `attempts` / `error`） | `(message_id, image_seq)` |

`homework_items.status` 取值：`pending`（待你私聊确认）、`confirmed`（你确认后已入日历）、`auto`（高置信度自动入）、`ignored`（你回复 n）、`timeout`（超时自动忽略）。
`lecture_notes.status` 取值：`pending`（已落库、等待 OCR）、`active`（OCR 成功）、`error`（OCR 失败，`retry_max` 用尽后标记，可后续重跑）。

> WAL 模式让 API 进程并发读、扫描器持续写互不阻塞，因此 Web 可以随时直读 db。

### 7.2 HTTP 接口

| 方法 | 路径 | 说明 | 数据来源 |
| --- | --- | --- | --- |
| `POST` | `/api/chat` | 自然语言创建日程 / 待办（请求体可带 `history` 多轮记忆） | Ollama + CalDAV |
| `POST` | `/api/manual-item` | 结构化创建（绕过 AI） | CalDAV |
| `GET` | `/api/health` | 健康检查 | — |
| `GET` | `/api/homework/status` | 扫描器是否启用 / 运行中 | 内存 |
| `GET` | `/api/homework/pending` | 当前等待私聊确认的作业 | 内存（notifier） |
| `GET` | `/api/homework/feed` | 最近的推送 / 建议日程事件 | 内存（feed） |
| `GET` | `/api/homework/items?limit=200` | **作业权威列表（含状态、可历史回看）** | db `homework_items` |
| `GET` | `/api/lecture/notes?limit=100` | **讲座 / 通知 OCR 存档（Markdown）** | db `lecture_notes` |
| `GET/POST` | `/api/config/whitelist` | **读取 / 更新作业群与图片 OCR 群白名单（定点改写保注释）** | `.config/hmwk_scnr/config.yaml` |
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
It turns natural-language requests into iCloud calendar events / reminders via Tool Calling, and ships a built-in SPA
frontend (`web/frontend/`, served at `/ui` by FastAPI, auto-opened on `python main.py`) with multi-turn chat memory
and an online whitelist editor (`GET/POST /api/config/whitelist`, point-edit preserving YAML comments).

Two optional QQ pipelines run as in-process background services via **OneBot 11 / NapCat**, each with its **own group whitelist**:

1. **Homework scanner** — detects assignments in whitelisted group chats (keyword prefilter + LLM extraction),
   asks for confirmation over private message (`y` / `n` / reschedule), and writes them to iCloud reminders.
   Every extraction and decision is persisted to SQLite (`homework_items`), so the web list survives restarts.
2. **Image OCR archive** — for a separate image whitelist, group images are fetched via OneBot, persisted to disk,
   written to `lecture_notes` as `pending`, then OCR'd by the local vision model **`qwen2.5vl`** in a background
   queue (so a kill / restart never loses an image; pending items resume automatically). Useful for lecture /
   announcement posters.

Because NapCat is push-only, a **catchup** step replays each group's history since its `group_progress` checkpoint
on WebSocket connect, so messages during offline windows are not lost. All three Ollama callers (text detection,
image OCR, main-chat) share one global GPU inference lock (`core/ollama_gpu.py`), so only one inference runs at a
time on a single 8 GB card.

All inference runs locally; only iCloud sync needs your Apple credentials.
MIT licensed. See `docs/` for architecture and deployment details.
