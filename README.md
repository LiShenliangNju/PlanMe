# 📅 PlanMe · 智能日程管家

> 基于 **Ollama 本地大模型** + **iCloud CalDAV** 的智能日程 / 待办管理服务，并内置 **QQ 群作业扫描器**：自动识别老师在群里布置的作业，经你确认后写入 iCloud 提醒事项。

---

## 一、项目简介

PlanMe 是一个本地优先（local-first）的日程自动化工具，核心思路是：

- 用**自然语言**告诉它任何日程或待办，本地 Ollama 模型负责理解意图、抽取结构化字段，并通过 **Tool Calling** 直接写入 iCloud 日历 / 提醒事项；
- 提供 **Streamlit** 可视化界面，既能聊天式创建，也能手动精准添加；
- 附带一个**独立的 QQ 作业扫描进程**：监听 NapCat（OneBot 11）转发的 QQ 群消息，用关键词预过滤 + 大模型抽取判断「是不是作业」，高置信度自动入日历，低置信度私聊问你确认。

所有 AI 推理都在你本机完成，**数据不出本机**（仅 iCloud 同步需要你的 Apple 账户凭据）。

---

## 二、✨ 功能特性

| 模块 | 能力 |
| --- | --- |
| **智能对话（主系统）** | 自然语言建日程 / 待办；Ollama `qwen2.5` 系列模型；基于 Pydantic Schema 的强校验与自动重试 |
| **双日历路由** | `Event` → 写入 iCloud「日程」日历；`Todo` → 写入 iCloud「提醒」日历（可按类型自动匹配） |
| **Web 界面** | Streamlit 双页签：AI 智能对话 + 手动快捷添加，实时展示系统健康状态 |
| **HTTP API** | `POST /api/chat`（自然语言）、`POST /api/manual-item`（绕过 AI）、`GET /api/health`（健康检查） |
| **QQ 作业扫描器** | OneBot 11（NapCat）接入；关键词预过滤 + 大模型结构化抽取；**漏报兜底**避免真作业被静默丢弃 |
| **私聊确认状态机** | 识别到作业后向主号私聊，支持 `y / n / 改 <时间>` 指令；超时自动忽略 |
| **常驻调度器** | `planme_guardian.py` 单进程守护，按设定时间自动拉起 Ollama + NapCat + 扫描器并定时清理 |

---

## 三、🧱 系统架构

系统由 **主系统（FastAPI）** 与 **QQ 作业扫描器（独立进程）** 两部分组成：

```
                    ┌─────────────── 用户 / QQ 群 ───────────────┐
                    │                                            │
  自然语言 ──►  Streamlit Web UI ──►  FastAPI 主系统 (8000)
                    │                       │
                    │                  Ollama 本地模型
                    │                       │  Tool Calling
                    │                       ▼
                    │               iCloudCalendarManager
                    │                       │
                    │                  iCloud CalDAV
                    │
  QQ 群消息 ──►  NapCat (OneBot WS) ──►  扫描器进程 (core/homework)
                                            │ Ollama 抽取
                                            │ 私聊确认
                                            └──► POST /api/chat ──► 主系统
```

扫描器与主系统通过 HTTP（`/api/chat`）解耦协作，扫描器本身**不做**日历写入，只把识别出的作业转成自然语言交给主系统处理。

---

## 四、📦 技术栈

- **后端**：FastAPI + Uvicorn，Pydantic / pydantic-settings
- **本地大模型**：Ollama（`qwen2.5:7b-instruct-q4_K_M` 等）
- **日历同步**：`caldav`（iCloud CalDAV 协议）
- **前端**：Streamlit
- **QQ 接入**：OneBot 11（NapCat），`websockets` 客户端
- **存储**：SQLite（`aiosqlite`，作业消息去重）
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
ollama pull qwen2.5:7b-instruct-q4_K_M
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

1. 按 `docs/DEPLOYMENT.md` 配置并启动 NapCat（小号登录、正向 WS 监听 `127.0.0.1:3001`）；
2. 填写 `.config/hmwk_scnr/config.yaml`；
3. 启动扫描器：

```bash
python -m core.homework
```

识别到作业后，主号会收到私聊确认；回复 `y` 加入、`n` 忽略、`改 <时间>` 改期。

### 5.8 常驻调度器（可选，Windows）

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
├── main.py                      # FastAPI 入口，启动 8000 端口
├── planme_guardian.py           # 常驻调度器（守护进程）
├── guardian.bat                # Windows 启动器（纯 ASCII，GBK 安全）
├── requirements.txt            # 统一依赖清单
├── .env.example                # 环境变量模板（真实 .env 请勿提交）
├── .gitignore
├── README.md / CONTRIBUTING.md / LICENSE
├── docs/
│   ├── ARCHITECTURE.md         # 详细架构与数据流说明
│   └── DEPLOYMENT.md           # 部署、NapCat 对接、排障
├── api/
│   └── routes.py               # FastAPI 路由：/api/chat、/api/manual-item、/api/health
├── core/
│   ├── __init__.py
│   ├── llm_agent.py            # PlanmeAgent：Ollama 对话 + Tool Calling
│   ├── calendar_sync.py        # iCloudCalendarManager：CalDAV 写入（Event/Todo 路由）
│   └── homework/               # QQ 群作业扫描器（独立进程，python -m core.homework）
│       ├── __main__.py         # 入口：加载配置、连接 NapCat、路由消息
│       ├── detector.py         # 关键词预过滤 + Ollama 结构化抽取（含漏报兜底）
│       ├── onebot_client.py    # OneBot 11（NapCat）WebSocket 客户端
│       ├── message_store.py    # SQLite 增量去重存储
│       ├── scheduler_bridge.py # 把作业 POST 给主系统 /api/chat
│       └── notifier.py         # 私聊确认状态机（y / n / 改）
├── schemas/
│   ├── schedule_schema.py      # CalendarItemSchema（日程 / 待办结构化规范）
│   └── homework_schema.py      # 作业相关 Schema（Sender / GroupMessage / HomeworkExtraction / ReminderPayload）
├── web/
│   └── app.py                  # Streamlit 前端界面
├── test/
│   ├── icloud_server.py        # iCloud / CalDAV 连接测试
│   ├── ollama_server.py        # Ollama 连接测试
│   └── tool_call.py            # Tool Calling 调用示例
└── .config/                    # 本地配置（不进版本库，见 *.example）
    ├── settings.py             # 全局设置（可提交，仅默认值）
    ├── caldav/calendar.conf    # ⚠️ 含 iCloud 密码，gitignore
    └── hmwk_scnr/config.yaml   # ⚠️ 含 token / QQ 号，gitignore
```

---

## 七、🔧 常用命令

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
```

---

## 八、🔐 安全说明

- 本项目**不收集、不上传**你的任何日程或聊天内容；所有模型推理在本地 Ollama 完成。
- iCloud / NapCat 凭据仅存于你本机的 `.config/*.conf` 与 `.env`，已在 `.gitignore` 中排除。
- QQ 作业扫描器仅建议在本机 `127.0.0.1` 运行，避免 token 与账号暴露。

---

## 九、🤝 贡献指南

欢迎 Issue 与 PR！请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，了解分支规范、提交信息与代码风格要求。

---

## 十、📄 许可证

本项目基于 **MIT 许可证** 开源，详见 [LICENSE](LICENSE)。

---

## English Abstract

**PlanMe** is a local-first smart schedule manager built on **Ollama** (local LLM) and **iCloud CalDAV**.
It turns natural-language requests into iCloud calendar events / reminders via Tool Calling, ships a Streamlit UI,
and includes an optional **QQ homework scanner** (OneBot 11 / NapCat) that detects assignments in group chats,
asks you for confirmation over private message, then writes them to your iCloud reminders.
All inference runs locally; only iCloud sync needs your Apple credentials.
MIT licensed. See `docs/` for architecture and deployment details.
