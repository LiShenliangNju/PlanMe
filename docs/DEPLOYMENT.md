# 🚀 PlanMe 部署与排障

本文档聚焦**真实跑起来**所需的步骤与常见问题，尤其是可选的 **QQ 作业扫描器**、**群图片 OCR 存档** 与 **NapCat** 对接。

三档部署，按需选择：

| 档位 | 需要的东西 | 对应章节 |
| --- | --- | --- |
| 只管日程 | Ollama 文本模型 + iCloud | 第一章 |
| ＋作业扫描 | ＋ NapCat（小号登录） | 第二章 |
| ＋图片 OCR | ＋ `qwen2.5vl:7b` 视觉模型 | 第三章 |

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
- `qq.teacher_roles`：视为「老师」的群角色（`owner` / `admin`）——**注意键名是 `teacher_roles`**
- `scheduler.endpoint`：主系统地址（默认 `http://127.0.0.1:8000/api/chat`）
- `image.group_whitelist`：图片 OCR 白名单，**默认留空即不启用**（见第三章）

### 2.3 启动顺序
1. **NapCat**（独立程序，小号登录、仅绑 127.0.0.1、强 token）
2. **Ollama**（`ollama pull` + `ollama serve`）
3. **主系统 + 扫描器**（`python main.py` → 8000；扫描器随主程序**单一入口**自动作为同进程后台任务启动，连接 NapCat 3001）

> 扫描器默认开启，可用环境变量 `ENABLE_HOMEWORK=false` 临时关闭。

### 2.4 验证
- 在白名单群、用老师身份发一条带「作业 / 截止」关键词的消息；
- 高置信度 → 主号收到「已自动加入日程」私聊，iCloud 提醒事项出现新待办；
- 中置信度 → 主号收到确认私聊，回复 `y` / `n` / `改 <时间>` 验证状态机；
- 打开 Web 界面（streamlit）的「🤖 QQ作业」页，可见**落库的作业列表（带状态徽标）** 与 qqbot 实时推送流；
- 重启主系统后作业列表**仍在**（读的是 db），且 `pending` 项会自动恢复到待确认队列——这是验证落库是否生效的最快方法；
- 也可直接看接口：`curl http://localhost:8000/api/homework/items?limit=5`。

---

## 三、启用群图片 OCR 存档

把讲座 / 通知 / 海报类图片自动转成 Markdown 存档。**独立白名单、默认关闭**，与作业扫描互不影响。

### 3.1 拉取视觉模型

```bash
ollama pull qwen2.5vl:7b      # 约数 GB；显存紧张可换 qwen2.5vl:3b
ollama list                   # 确认模型已存在
```

> 换模型后记得同步改 `config.yaml` 里的 `image.model`，两处必须一致。

### 3.2 配置白名单

编辑 `.config/hmwk_scnr/config.yaml`：

```yaml
image:
  group_whitelist: [123456789]   # 填入讲座 / 通知群的群号；留空 [] = 关闭
  model: "qwen2.5vl:7b"
  throttle_seconds: 3            # 两次 OCR 最小间隔，防刷屏
  max_concurrency: 2             # 并发 OCR 上限，显存紧张调小为 1
```

**与作业扫描的区别（容易混淆）**：

| | 作业管道 | 图片管道 |
| --- | --- | --- |
| 配置项 | `qq.group_whitelist` | `image.group_whitelist` |
| 消息类型 | 文本 | 图片 |
| 发送者限制 | **仅老师**（`teacher_roles` / `teacher_user_ids`） | **不限**，群内任何人 |
| 触发条件 | 命中关键词预过滤 | 消息里含图片即触发 |
| 落库表 | `homework_items` | `lecture_notes` |

### 3.3 验证

1. 重启主系统（`python main.py`）；
2. 在图片白名单群里发一张**带文字的图**（讲座海报、通知截图都行）；
3. 观察日志出现 OCR 相关记录（首次调用视觉模型会较慢，需加载模型权重）；
4. 打开 Web 的「🖼️ 讲座/通知」页，应看到渲染后的 Markdown 与原图链接；
5. 或直接查接口：`curl http://localhost:8000/api/lecture/notes?limit=3`。

> 若返回 `status: error`，说明图片抓到了但 OCR 失败——记录仍保留（含 `image_url`），排掉原因后可重跑。

---

## 四、常驻调度器（Windows）

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

## 五、常见问题（排障）

### 5.1 主系统 / 作业扫描

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `未能获取到任何 iCloud 日历` | CalDAV 凭据错误或应用专用密码无效 | 复核 `calendar.conf`；确认 iCloud 已开启 CalDAV |
| 作业被静默漏报 | 模型把明显作业判成 `is_homework=False` | detector 已有「关键词命中 + 抽到期末时间」兜底；可下调 `auto_confidence` 或调高 `min_confidence` 观察 |
| 写入过期提醒（年份错） | 模型把「8月22日」解析成旧年份 | detector 已注入当前真实日期；如仍发生，检查系统时间 |
| 老师身份过滤不生效 | 配置里键名写成了 `leader_roles` | 正确键名是 **`teacher_roles`**（scanner 只读这个） |
| 扫描器启动即报 `SyntaxError` | `.ps1/.bat` 中文编码乱码或 except 缺变量名 | 确保所有启动脚本为纯 ASCII 或 UTF-8 BOM |
| OneBot 连接失败 | NapCat 未启动 / token 不匹配 / 非 127.0.0.1 | 核对 `onebot_ws_url` 与 `access_token`；确认 NapCat 正向 WS 已开 |
| `/api/chat` 返回 500 | Ollama 未启动或模型未拉取 | `curl` 健康检查 + `ollama list` 确认模型存在 |
| Web 作业列表为空但私聊收到了 | db 路径不一致（扫描器写 A、API 读 B） | 两侧都走 `resolve_db_path()`；确认 `storage.db_path` 只有一处配置 |
| 重启后待确认项没了 | 期望行为是自动恢复 | 检查 `homework_items` 里是否有 `status=pending` 的记录；rehydrate 只恢复 pending |

### 5.2 图片 OCR

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 发图后完全无反应 | `image.group_whitelist` 为空或群号不在其中 | 填入群号后**重启主系统**（配置在启动时读取） |
| `model not found` / OCR 全失败 | 视觉模型未拉取，或 `image.model` 与实际模型名不一致 | `ollama pull qwen2.5vl:7b` 后 `ollama list` 核对名称 |
| 首张图 OCR 极慢 | 视觉模型首次加载权重 | 正常现象，后续会快；可提前 `ollama run qwen2.5vl:7b` 预热 |
| 显存不足 / Ollama 崩溃 | 并发 OCR 太多，或 7B 视觉模型太大 | 把 `max_concurrency` 调为 `1`；或换 `qwen2.5vl:3b` |
| 记录 `status=error`、`ocr_md` 为空 | 图片抓取成功但模型调用异常 | 看日志里的 OCR 异常栈；记录已保留 `image_url`，修好后可重跑 |
| 图片抓取失败（日志「图片获取失败」） | NapCat 缓存已清 且 图片 url 过期 | 确认 NapCat 在线；及时处理，QQ 图片 url 有有效期 |
| 群消息延迟变高 | OCR 占满并发 | 调低 `max_concurrency`、调高 `throttle_seconds` |

---

## 六、安全清单（部署前自查）

- [ ] iCloud 使用的是**应用专用密码**，非 Apple ID 主密码
- [ ] NapCat 仅监听 `127.0.0.1`，`access_token` 为强随机串
- [ ] `.env` / `.config/caldav/calendar.conf` / `.config/hmwk_scnr/config.yaml` **未**被提交进版本库（已由 `.gitignore` 排除）
- [ ] `qq_homework.db` 未被提交（含群聊原文与 OCR 文本，已由 `*.db` 规则排除）
- [ ] 图片白名单只填**确实需要**存档的群，避免把私密图片 OCR 落库
- [ ] 机器人小号仅用于读群 + 私聊确认，权限最小化
