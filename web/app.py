import streamlit as st
import requests
from datetime import datetime

# 设置页面元信息
st.set_page_config(
    page_title="Planme - 智能日程管家",
    page_icon="📅",
    layout="wide"
)

API_BASE_URL = "http://localhost:8000/api"

# 应用标题与头部
st.title("📅 Planme 智能日程管家")
st.caption("基于 Ollama 本地模型 & iCloud CalDAV 协议的日程同步 Agent")

# 侧边栏：状态显示与快捷提示
with st.sidebar:
    st.header("⚙️ 系统状态")
    try:
        health_res = requests.get(f"{API_BASE_URL}/health", timeout=2).json()
        st.success(f"后端服务: 正常运行")
        st.info(f"AI 模型: `{health_res.get('agent_model')}`")
        st.info(f"系统时区: `{health_res.get('timezone')}`")
    except Exception:
        st.error("后端服务未连接，请先启动 main.py")

    st.divider()
    st.header("💡 试一试这样说")
    st.code("帮我约明天下午5点和老李头在图书馆打游戏，大概2小时", language=None)
    st.code("提醒我下周五18:00前提交行策期末报告", language=None)
    st.code("今晚8点和李总在腾讯会议线上开会，链接是 https://meeting.tencent.com/ilovesleep", language=None)

# 选项卡划分为：AI 智能对话、手动精准添加、QQ作业、讲座/通知
tab1, tab2, tab3, tab4 = st.tabs(["💬 AI 智能对话", "📝 手动快捷添加", "🤖 QQ作业", "🖼️ 讲座/通知"])

# ---------------------------------------------------------
# Tab 1: AI 智能对话
# ---------------------------------------------------------
with tab1:
    # 初始化会话历史
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "assistant", "content": "你好！我是 Planme。你可以用自然语言告诉我任何日程或待办，我会为你同步到 iCloud日历。"}
        ]

    # 渲染历史对话
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if "card_data" in msg:
                data :dict = msg["card_data"]
                st.info(
                    f"**[{'📅 日程 Event' if data['item_type']=='Event' else '📌 待办 Todo'}] {data['summary']}**\n\n"
                    f"- 🕒 时间: `{data['start_time']}`\n"
                    f"- 📍 地点/链接: {data.get('location') or data.get('url') or '无'}\n"
                    f"- 📝 备注: {data.get('description') or '无'}"
                )

    # 聊天输入框
    if prompt := st.chat_input("输入你的日程或需求..."):
        # 记录用户输入
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # 请求 API 交互
        with st.chat_message("assistant"):
            with st.spinner("Ollama 正在思考"):
                try:
                    response = requests.post(
                        f"{API_BASE_URL}/chat",
                        json={"text": prompt},
                        timeout=125
                    )

                    if response.status_code != 200:
                        error_msg = response.json().get("detail", "未知错误")
                        st.error(f"⚠️ 后端返回错误: {error_msg}")
                    else:
                        res_data = response.json()
                        if res_data.get("status") == "chat":
                            reply_text = res_data.get("message")
                            st.markdown(reply_text)
                            st.session_state.messages.append({"role": "assistant", "content": reply_text})

                        elif res_data.get("status") == "success":
                            reply_text = res_data.get("message")
                            card_data = res_data.get("data")
                            st.markdown(reply_text)
                            st.info(
                                f"**[{'📅 日程 Event' if card_data['item_type']=='Event' else '📌 待办 Todo'}] {card_data['summary']}**\n\n"
                                f"- 🕒 时间: `{card_data['start_time']}`\n"
                                f"- 📍 地点/链接: {card_data.get('location') or card_data.get('url') or '无'}\n"
                                f"- 📝 备注: {card_data.get('description') or '无'}"
                            )
                            st.session_state.messages.append({
                                "role": "assistant",
                                "content": reply_text,
                                "card_data": card_data
                            })

                except Exception as e:
                    st.error(f"网络通信异常: {e}")

# ---------------------------------------------------------
# Tab 2: 手动快捷添加
# ---------------------------------------------------------
with tab2:
    st.subheader("手动提交事件或待办")
    with st.form("manual_event_form"):
        col1, col2 = st.columns(2)
        with col1:
            item_type = st.selectbox("事件类型", ["Event", "Todo"], format_func=lambda x: "📅 会议/日程 (Event)" if x=="Event" else "📌 提醒/待办 (Todo)")
            summary = st.text_input("标题/主题", placeholder="例如：产品需求评审会议")
            start_date = st.date_input("开始日期", datetime.now())
            start_time = st.time_input("开始时间", datetime.now().time())
        
        with col2:
            target_cal = st.text_input("目标日历 (留空按类型自动匹配)", placeholder="默认：planme / 提醒")
            duration = st.number_input("持续时间 (分钟，仅 Event 生效)", min_value=15, value=60, step=15)
            location = st.text_input("地点 / 线上会议链接")
            url = st.text_input("关联网页 URL")

        description = st.text_area("详细备注信息")

        submitted = st.form_submit_button("🚀 直接提交至 iCloud")

        if submitted:
            if not summary:
                st.warning("标题不能为空！")
            else:
                combined_dt = datetime.combine(start_date, start_time).strftime("%Y-%m-%dT%H:%M:%S")
                payload = {
                    "item_type": item_type,
                    "summary": summary,
                    "start_time": combined_dt,
                    "duration_minutes": duration if item_type == "event" else None,
                    "target_calendar": target_cal if target_cal.strip() else None,
                    "location": location if location.strip() else None,
                    "url": url if url.strip() else None,
                    "description": description if description.strip() else None
                }

                try:
                    res = requests.post(f"{API_BASE_URL}/manual-item", json=payload).json()
                    if res.get("status") == "success":
                        st.success(res.get("message"))
                    else:
                        st.error(f"提交失败: {res}")
                except Exception as e:
                    st.error(f"无法连接到后端服务器: {e}")

# ---------------------------------------------------------
# Tab 3: QQ 作业（权威列表直接读 db 的 homework_items；下方保留 qqbot 推送流）
# ---------------------------------------------------------
with tab3:
    st.subheader("🤖 QQ 作业 · 落库列表 & qqbot 推送")
    if st.button("🔄 刷新", key="hw_refresh"):
        st.rerun()

    # 权威作业列表：直接读 db（重启不丢、可历史回看、按状态筛选）
    try:
        items = requests.get(f"{API_BASE_URL}/homework/items", timeout=5).json().get("items", [])
    except Exception:
        items = []
    st.markdown(f"**📋 作业列表（db，{len(items)}）**")
    if not items:
        st.caption("暂无作业记录")
    for it in items:
        status = it.get("status", "")
        subj = it.get("subject") or "未识别"
        dl = it.get("deadline") or "未识别"
        grp = it.get("group_name") or it.get("group_id")
        conf = it.get("confidence") or 0
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else str(conf)
        head = f"#{it.get('cid','')} {subj} · 截止 {dl} · 群 {grp} · 置信 {conf_s}"
        if status in ("confirmed", "auto"):
            st.success(f"✅ {'自动加入' if status == 'auto' else '已确认加入'} | {head}")
        elif status == "pending":
            st.warning(f"⏳ 待确认 | {head}")
        elif status == "ignored":
            st.caption(f"🚫 已忽略 | {head}")
        else:
            st.info(f"⚪ 已过滤(非作业/低置信) | {head}")

    st.divider()

    # qqbot 推送 / 建议日程流（内存 feed，保留作实时流水）
    try:
        pushes = requests.get(f"{API_BASE_URL}/napcat/pushes", timeout=3).json().get("pushes", [])
    except Exception:
        pushes = []
    st.markdown(f"**📨 qqbot 推送 / 建议日程（{len(pushes)}）**")
    if pushes:
        for ev in reversed(pushes):
            ts = datetime.fromtimestamp(ev.get("ts", 0)).strftime("%m-%d %H:%M")
            st.caption(f"`{ts}` · {ev.get('kind', '')}")
            st.write(ev.get("text", ""))
    else:
        st.caption("暂无推送记录")

# ---------------------------------------------------------
# Tab 4: 讲座/通知（白名单群图片 OCR 存档，直接读 db 的 lecture_notes）
# ---------------------------------------------------------
with tab4:
    st.subheader("🖼️ 讲座 / 通知 · 图片 OCR 存档")
    if st.button("🔄 刷新", key="lecture_refresh"):
        st.rerun()
    try:
        res = requests.get(f"{API_BASE_URL}/lecture/notes", timeout=5).json()
        notes = res.get("notes", [])
        pending = res.get("pending", 0)
    except Exception:
        notes, pending = [], 0
    st.markdown(f"**📚 笔记（db，{len(notes)}）**")
    if pending:
        st.info(f"⏳ 有 {pending} 张图片已存档、正在排队 OCR（本地模型串行处理，可稍后刷新）")
    if not notes:
        st.caption("白名单群里还没有抓到图片，或 image.group_whitelist 未配置")
    for n in notes:
        ts = datetime.fromtimestamp(n.get("created_at", 0)).strftime("%Y-%m-%d %H:%M")
        grp = n.get("group_name") or n.get("group_id")
        status = n.get("status")
        icon = {"active": "✅", "pending": "⏳", "error": "⚠️"}.get(status, "⚪")
        label = {"active": "", "pending": " · 排队 OCR 中", "error": " · OCR 失败"}.get(status, "")
        with st.expander(f"📄 {grp} · {ts} · {icon}{label}"):
            if n.get("image_url"):
                st.markdown(f"[🔗 原图链接]({n['image_url']})")
            if n.get("local_path"):
                st.caption(f"本地存档：`{n['local_path']}`")
            if status == "pending":
                st.caption("图片已落盘存档，OCR 排队中；进程重启后会自动继续。")
            elif status == "error":
                st.warning(f"OCR 失败（已尝试 {n.get('attempts', 0)} 次）：{n.get('error') or '未知原因'}")
            else:
                st.markdown(n.get("ocr_md") or "（OCR 未返回内容）")