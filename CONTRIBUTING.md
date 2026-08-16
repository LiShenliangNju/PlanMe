# 🤝 贡献指南（CONTRIBUTING）

感谢你考虑为 **PlanMe** 做出贡献！本文档约定了提交代码、Issue 与 Pull Request 的基本规范。

---

## 一、参与方式

- **Bug 反馈 / 功能建议**：请在仓库的 Issues 中提交，尽量附上复现步骤、环境信息（OS、Python、Ollama 版本）与相关日志。
- **代码贡献**：Fork 后提交 Pull Request 到 `main` 分支。

---

## 二、开发环境准备

```bash
# 1) 先到 https://github.com/LiShenliangNju/PlanMe 点击 Fork，把仓库 fork 到你的账号
# 2) 克隆你自己的 fork（把 <你的用户名> 换成你的 GitHub 用户名）
git clone https://github.com/<你的用户名>/PlanMe.git
cd PlanMe
# 3) 添加上游仓库，方便后续把主干改动同步到你的 fork
git remote add upstream https://github.com/LiShenliangNju/PlanMe.git

# 创建并激活虚拟环境（任选其一）
python -m venv .venv && source .venv/bin/activate   # Linux / macOS
# .venv\Scripts\activate                              # Windows (PowerShell)

pip install -r requirements.txt

# 复制配置模板（真实配置不入库，按需填写）
cp .env.example .env
cp .config/caldav/calendar.conf.example .config/caldav/calendar.conf
```

本地联调需要启动 Ollama 并拉取模型（见 `README.md` 第 5 节）。

---

## 三、分支与提交规范

- 默认分支为 `main`，请勿直接向 `main` 推送大型变更，请走 PR。
- 分支命名建议：
  - 新功能：`feat/简短描述`
  - 修复：`fix/简短描述`
  - 文档：`docs/简短描述`
- **提交信息（Commit Message）** 推荐中文 + 语义化前缀：
  - `feat:` 新功能
  - `fix:` 修复缺陷
  - `docs:` 文档变更
  - `refactor:` 重构（无功能变化）
  - `test:` 测试相关
  - `chore:` 构建 / 依赖等杂项
- 示例：`fix: detector 漏报时兜底判为作业，避免真作业被静默丢弃`

---

## 四、代码风格

- 语言：Python 3.10+，遵循 **PEP 8**。
- 注释与文档字符串优先使用**中文**（与项目保持一致）。
- 类型注解：公共函数 / 方法建议标注参数与返回类型。
- 涉及结构化数据抽取时，优先复用 `schemas/` 下的 Pydantic 模型，便于校验与复用。
- 涉及外部凭据（iCloud / NapCat / QQ）的代码，**严禁**将真实密码、token 写入源码或日志打印。

---

## 五、Pull Request 要求

1. PR 描述请说明：改动动机、主要变更、测试方式。
2. 确保本地可正常启动主系统（`python main.py`）。homework 扫描器等后台服务已由主程序单一入口统一拉起（受 `ENABLE_HOMEWORK` 控制），无需单独启动。
3. 如改动 API 契约（`api/schedule.py`、`api/homework.py`、`api/lecture.py`、`api/napcat.py`、`schemas/`），请在 PR 中说明前后兼容性影响。
4. 新增依赖请同步更新 `requirements.txt` 并注明用途。
5. 如改动 SQLite 表结构（`core/homework/message_store.py`），请使用 `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE` 保持对已有库文件的向前兼容，并在 PR 中说明字段含义与状态取值。
6. 如新增配置项，**必须同步更新 `.config/hmwk_scnr/config.yaml.example` 或 `.env.example`**——真实配置文件不入库，模板是使用者唯一的参考。

---

## 六、安全与隐私

- 任何涉及用户凭据的改动都须维持「配置与代码分离、敏感文件不入库」的原则（参见 `.gitignore`）。
- 若发现安全漏洞，请**不要**公开 Issue，改为私信维护者或在仓库 Security 中报告。

---

再次感谢你的参与 🐾
