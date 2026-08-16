# 🚀 PlanMe 部署与排障

本文档聚焦**真实跑起来**所需的步骤与常见问题，尤其是可选的 **QQ 作业扫描器** 与 **NapCat** 对接。

---

## 一、最小化部署（仅主系统 + Web）

适合只想用自然语言管理 iCloud 日程的场景。

1. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

2. **配置环境变量**
   ```bash
   cp .env.example .env
   # 编辑 .env：OLLAMA_HOST / OLLAMA_MODEL / TIMEZONE
   ```

3. **配置 iCloud CalDAV**
   ```bash
   cp .config/caldav/calendar.conf.example .config/caldav/calendar.conf
   ```
   - `caldav_username`：你的 Apple ID（邮箱）
   - `caldav_password`：**App 专用密码**（appleid.apple.com → 安全 → 生成），不要用主密码
   - 在 iCloud 中确保已存在名为 `planme`（日程）与 `提醒`（待办）的日历；否则会自动降级到第一个可用日历

4. **启动 Ollama**
   ```bash
   ollama pull qwen2.5:7b-instruct-q4_K_M
   ollama serve
   ```

5. **启动主系统**
   ```bash
   python main.py
   curl http://localhost:8000/api/health
   ```

6. **（可选）启动 Web 界面**
   ```bash
   streamlit run web/app.py
   ```

---

## 二、启用 QQ 作业扫描器

> 前置：一台 Windows 机器 + 一个已登录的 NapCat（OneBot 11 实现）小号。

### 2.1 安装并启动 NapCat
- 推荐从官方 Release 下载 **Windows 一键包**（内置 QQ + NapCat），或确保本机 QQ NT 版本 ≥ 40768。
- 扫码登录**机器人小号**。
- 在 NapCat WebUI 中：
  - 网络类型选择 **`Websocket服务器`**（= OneBot 正向 WS，NapCat 监听端口、本服务来连）；
  - host 设为 `127.0.0.1`，port 设为 `3001`；
  - 配置一个**强随机** `access_token`（与下方 `config.yaml` 的 `access_token` 必须完全一致）。
- ⚠️ `WebUi Token`（进管理后台用）≠ WS `access_token`，二者不要混淆；且 WS 必须只绑 `127.0.0.1`，切勿暴露公网。

### 2.2 配置扫描器
```bash
cp .config/hmwk_scnr/config.yaml.example .config/hmwk_scnr/config.yaml
```
逐项填写：
- `qq.onebot_ws_url`：`ws://127.0.0.1:3001`
- `qq.access_token`：与 NapCat 中配置的一致
- `qq.bot_user_id` / `qq.owner_user_id`：机器人小号 / 你的主号 QQ
- `qq.group_whitelist` / `teacher_user_ids`：按需收窄监听范围
- `scheduler.endpoint`：主系统地址（默认 `http://127.0.0.1:8000/api/chat`）

### 2.3 启动顺序
1. **NapCat**（独立程序，小号登录、仅绑 127.0.0.1、强 token）
2. **Ollama**（`ollama pull` + `ollama serve`）
3. **主系统 + 扫描器**（`python main.py` → 8000；扫描器随主程序**单一入口**自动作为同进程后台任务启动，连接 NapCat 3001）

> 扫描器默认开启，可用环境变量 `ENABLE_HOMEWORK=false` 临时关闭。

### 2.4 验证
- 在白名单群、用老师身份发一条带「作业 / 截止」关键词的消息；
- 高置信度 → 主号收到「已自动加入日程」私聊，iCloud 提醒事项出现新待办；
- 中置信度 → 主号收到确认私聊，回复 `y` / `n` / `改 <时间>` 验证状态机；
- 打开 Web 界面（streamlit）的「🤖 QQ作业」页，可见 qqbot 推送与待确认 / 已添加的建议日程。

---

## 三、常驻调度器（Windows）

```bat
guardian.bat start      # 启动（常驻，无窗口），日志见 guardian.log
guardian.bat stop       # 优雅停止
guardian.bat status     # 是否运行 / 下次触发时间
guardian.bat test       # 自检路径与端口，不启动任何服务
guardian.bat install    # 登录时自动启动（任务计划程序）
guardian.bat uninstall  # 移除自启任务
```
> `guardian.bat` 为纯 ASCII 文件，避免 Windows GBK/CP936 下中文注释导致脚本报错。

---

## 四、常见问题（排障）

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `未能获取到任何 iCloud 日历` | CalDAV 凭据错误或应用专用密码无效 | 复核 `calendar.conf`；确认 iCloud 已开启 CalDAV |
| 作业被静默漏报 | 模型把明显作业判成 `is_homework=False` | detector 已有「关键词命中 + 抽到期末时间」兜底；可下调 `auto_confidence` 或调高 `min_confidence` 观察 |
| 写入过期提醒（年份错） | 模型把「8月22日」解析成旧年份 | detector 已注入当前真实日期；如仍发生，检查系统时间 |
| 扫描器启动即报 `SyntaxError` | `.ps1/.bat` 中文编码乱码或 except 缺变量名 | 确保所有启动脚本为纯 ASCII 或 UTF-8 BOM |
| OneBot 连接失败 | NapCat 未启动 / token 不匹配 / 非 127.0.0.1 | 核对 `onebot_ws_url` 与 `access_token`；确认 NapCat 正向 WS 已开 |
| `/api/chat` 返回 500 | Ollama 未启动或模型未拉取 | `curl` 健康检查 + `ollama list` 确认模型存在 |

---

## 五、安全清单（部署前自查）

- [ ] iCloud 使用的是**应用专用密码**，非 Apple ID 主密码
- [ ] NapCat 仅监听 `127.0.0.1`，`access_token` 为强随机串
- [ ] `.env` / `.config/caldav/calendar.conf` / `.config/hmwk_scnr/config.yaml` **未**被提交进版本库（已由 `.gitignore` 排除）
- [ ] 机器人小号仅用于读群 + 私聊确认，权限最小化
