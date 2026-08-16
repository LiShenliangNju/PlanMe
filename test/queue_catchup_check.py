"""离线自检：db 迁移 / 断点推进 / OCR 队列状态机 / 消息归一化。

不连 NapCat、不调 Ollama，纯本地跑，用于改动后快速验证核心不变量：
  1. 老库（缺 local_path、last_message_id）能被幂等迁移补列，且不丢数据；
  2. mark_progress 单调递增 —— 补抓重放老消息不会把断点拽回去；
  3. lecture_notes 的 pending → active / error 状态机与「重启续跑」查询正确；
  4. normalize_message 能把 segment 数组还原成 CQ 串，url 的 &amp; 被正确反转义。

用法：python -m test.queue_catchup_check
"""

import asyncio
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".config"))

from core.homework.message_store import MessageStore  # noqa: E402
from core.homework.scanner import normalize_message, parse_cq_images  # noqa: E402
from schemas.homework_schema import GroupMessage, LectureNote, Sender  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' -> ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def make_legacy_db(path: str) -> None:
    """造一个「老版本」库：group_progress 缺 last_message_id，lecture_notes 缺 local_path。"""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE messages (message_id INTEGER PRIMARY KEY, group_id INTEGER,"
              " user_id INTEGER, role TEXT, content TEXT, time INTEGER)")
    c.execute("CREATE TABLE group_progress (group_id INTEGER PRIMARY KEY, last_time INTEGER)")
    c.execute("CREATE TABLE lecture_notes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " message_id INTEGER NOT NULL, image_seq INTEGER NOT NULL DEFAULT 0,"
              " group_id INTEGER, group_name TEXT, user_id INTEGER, image_url TEXT,"
              " ocr_md TEXT, status TEXT, created_at INTEGER, ocr_at INTEGER,"
              " UNIQUE(message_id, image_seq))")
    c.execute("INSERT INTO group_progress VALUES (1055992109, 1000)")
    c.execute("INSERT INTO lecture_notes (message_id, image_seq, group_id, ocr_md, status,"
              " created_at) VALUES (777, 0, 1055992109, '# 老数据', 'active', 900)")
    c.commit()
    c.close()


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="planme_check_"))
    db_path = str(tmp / "legacy.db")
    make_legacy_db(db_path)

    store = MessageStore(db_path)
    await store.init()  # 触发迁移

    # ---- 1. 迁移 ----
    raw = sqlite3.connect(db_path)
    gp_cols = {r[1] for r in raw.execute("PRAGMA table_info(group_progress)")}
    ln_cols = {r[1] for r in raw.execute("PRAGMA table_info(lecture_notes)")}
    check("group_progress 补出 last_message_id/updated_at",
          {"last_message_id", "updated_at"} <= gp_cols, str(sorted(gp_cols)))
    check("lecture_notes 补出 local_path/attempts/error",
          {"local_path", "attempts", "error"} <= ln_cols, str(sorted(ln_cols)))
    old = raw.execute("SELECT ocr_md, status FROM lecture_notes WHERE message_id=777").fetchone()
    check("迁移不丢老数据", old == ("# 老数据", "active"), str(old))
    check("老断点保留", raw.execute(
        "SELECT last_time FROM group_progress WHERE group_id=1055992109").fetchone()[0] == 1000)
    raw.close()

    # 幂等：再 init 一次不应报错、不应重复加列
    await store.close()
    store = MessageStore(db_path)
    await store.init()
    check("迁移幂等（重复 init 不炸）", True)

    # ---- 2. 断点单调递增 ----
    await store.mark_progress(1055992109, 5000, 2000)
    p = await store.get_progress(1055992109)
    check("断点前进到新消息", p["last_time"] == 2000 and p["last_message_id"] == 5000, str(p))
    await store.mark_progress(1055992109, 3000, 1500)  # 补抓重放老消息
    p = await store.get_progress(1055992109)
    check("补抓重放老消息不回退断点",
          p["last_time"] == 2000 and p["last_message_id"] == 5000, str(p))
    await store.mark_progress(2222, 10, 111)
    check("新群自动建断点", len(await store.all_progress()) == 2)

    # ---- 3. 消息去重（补抓重放安全）----
    msg = GroupMessage(message_id=8801, group_id=1055992109,
                       sender=Sender(user_id=1, role="owner"), content="交作业", time=2100)
    check("首次落库返回 True", await store.save(msg) is True)
    check("重放同一条消息返回 False（跨重启去重）", await store.save(msg) is False)

    # ---- 4. OCR 队列状态机 ----
    note = LectureNote(message_id=9001, image_seq=0, group_id=1055992109,
                       group_name="讲座群", image_url="http://x/y.png",
                       local_path=str(tmp / "a.png"), status="pending", created_at=3000)
    check("图片以 pending 入库", await store.save_lecture_note(note) is True)
    check("同图重复入队被拒（UNIQUE）", await store.save_lecture_note(note) is False)
    pend = await store.pending_lecture_notes()
    check("pending 能被捞出（重启续跑）",
          len(pend) == 1 and pend[0]["message_id"] == 9001, f"{len(pend)} 条")
    check("pending 计数", await store.count_pending_lecture_notes() == 1)

    await store.mark_lecture_ocr_done(9001, 0, "# 讲座标题\n- 内容")
    rows = await store.recent_lecture_notes(10)
    done = [r for r in rows if r["message_id"] == 9001][0]
    check("OCR 完成后 status=active 且回填 md",
          done["status"] == "active" and done["ocr_md"].startswith("# 讲座标题")
          and done["attempts"] == 1 and done["ocr_at"] > 0, str(done["status"]))
    check("完成后不再出现在 pending 队列",
          await store.count_pending_lecture_notes() == 0)

    # 失败重试：未到上限留在 pending，到上限置 error
    n2 = LectureNote(message_id=9002, image_seq=1, group_id=1055992109, status="pending",
                     created_at=3100)
    await store.save_lecture_note(n2)
    await store.mark_lecture_ocr_failed(9002, 1, "OCR 返回空内容", give_up=False)
    check("失败未到上限仍留在 pending（会被重试）",
          await store.count_pending_lecture_notes() == 1)
    await store.mark_lecture_ocr_failed(9002, 1, "OCR 返回空内容", give_up=True)
    rows = await store.recent_lecture_notes(10)
    bad = [r for r in rows if r["message_id"] == 9002][0]
    check("到上限置 error 且记录原因与次数",
          bad["status"] == "error" and bad["attempts"] == 2 and "空内容" in (bad["error"] or ""),
          f"{bad['status']}/{bad['attempts']}")
    check("error 不再占用 pending 队列",
          await store.count_pending_lecture_notes() == 0)

    await store.update_lecture_local_path(9002, 1, "D:/new/path.png")
    rows = await store.recent_lecture_notes(10)
    check("local_path 可更新（缓存被清后重下）",
          [r for r in rows if r["message_id"] == 9002][0]["local_path"] == "D:/new/path.png")

    await store.close()

    # ---- 5. 消息归一化 + CQ 反转义 ----
    seg = [
        {"type": "text", "data": {"text": "看海报"}},
        {"type": "image", "data": {"file": "abc.png",
                                   "url": "https://gchat.qq.com/x?a=1&b=2&rkey=zz"}},
    ]
    cq = normalize_message(seg)
    check("segment 数组归一化为 CQ 串", "[CQ:image," in cq and "看海报" in cq, cq[:80])
    imgs = parse_cq_images(cq)
    check("能从归一化结果解析出图片", len(imgs) == 1, str(imgs))
    check("url 的 & 被正确还原（原实现会 404）",
          imgs[0]["url"] == "https://gchat.qq.com/x?a=1&b=2&rkey=zz", str(imgs[0]["url"]))
    check("CQ 字符串原样透传", normalize_message("hi [CQ:at,qq=1]") == "hi [CQ:at,qq=1]")
    check("非字符串非数组安全返回空", normalize_message(None) == "")

    # ---- 6. 补抓：分页 / 断点过滤 / 正序重放 / 去重 / 图片留痕 ----
    await _check_catchup(tmp)

    # ---- 7. 共享 GPU 锁：文本检测 / 图片 OCR 互不并发 ----
    await _check_gpu_lock()

    print()
    if FAILED:
        print(f"{len(FAILED)} 项未通过：{FAILED}")
        sys.exit(1)
    print("全部通过。")


class FakeClient:
    """假 OneBot：按 message_seq 分页返回历史消息，记录被调用的锚点。"""

    def __init__(self, msgs: list[dict]) -> None:
        self.msgs = sorted(msgs, key=lambda m: m["time"])  # 全量，正序
        self.anchors: list = []
        self.page_calls = 0

    async def get_group_msg_history(self, group_id, message_seq=None, count=20):
        self.page_calls += 1
        self.anchors.append(message_seq)
        pool = self.msgs
        if message_seq:  # 取该消息之前的更早消息
            pool = [m for m in self.msgs if m["message_id"] < message_seq]
        return pool[-count:]  # 最近 count 条，正序

    async def get_group_info(self, group_id):
        return {"group_name": "讲座群"}

    async def get_group_list(self):
        return [{"group_id": 1055992109}]

    async def fetch_image_path(self, file_id, url):
        return None  # 模拟「图片拿不到」，验证仍会留痕

    def stop(self):
        pass


class FakeOllama:
    """假 Ollama：统计同一时刻并发的 chat 调用数，验证共享锁的互斥效果。"""

    def __init__(self) -> None:
        self.peak = 0
        self._cur = 0
        self._lk = threading.Lock()

    def chat(self, *args, **kwargs):
        with self._lk:
            self._cur += 1
            if self._cur > self.peak:
                self.peak = self._cur
        time.sleep(0.05)  # 制造重叠窗口
        with self._lk:
            self._cur -= 1
        # 同时兼容 detector(JSON 解析) 与 ocr(纯文本返回)
        return {"message": {"content": '{"is_homework": false, "confidence": 0.0}'}}


async def _check_gpu_lock() -> None:
    """验证文本检测 / 图片 OCR / 主系统对话 共享同一把 GPU 锁，互不并发。"""
    from core.homework.detector import HomeworkDetector
    from core.homework.ocr import ImageOCR
    from core.llm_agent import inference_lock as agent_lock
    from core.ollama_gpu import inference_lock

    fake = FakeOllama()
    det = HomeworkDetector(host="x", model="m", temperature=0.0,
                           keyword_prefilter=[], throttle_seconds=0, min_confidence=0.6)
    det._client = fake
    ocr = ImageOCR(host="x", model="m", throttle_seconds=0)
    ocr._client = fake

    check("detector 与 ocr 默认共享同一把 GPU 锁",
          det._gpu_lock is ocr._gpu_lock is inference_lock)
    check("主系统 /api/chat 也接入同一把 GPU 锁", agent_lock is inference_lock)

    # 同时触发两条路，应被同一把锁串行化（同一时刻只有一次推理）
    await asyncio.gather(det.detect("作业 截止 本周五交"), ocr.ocr("dummy.png"))
    check("文本检测与图片OCR不并发（同一时刻仅一次推理）", fake.peak <= 1, f"peak={fake.peak}")


async def _check_catchup(tmp: Path) -> None:
    from core.homework.scanner import HomeworkScanner

    db_path = str(tmp / "catchup.db")
    store = MessageStore(db_path)
    await store.init()

    gid = 1055992109
    # 断点：已处理到 t=1000。1001~1004 是空窗期新消息，其中一条带图片。
    await store.mark_progress(gid, 500, 1000)
    history = [
        {"message_id": 100 + i, "time": 900 + i * 20,
         "sender": {"user_id": 42, "role": "member", "nickname": "同学"},
         "message": [{"type": "text", "data": {"text": f"消息{i}"}}]}
        for i in range(10)
    ]
    # 最后一条带图片（时间最新，必然在断点之后）
    history[-1]["message"] = [
        {"type": "text", "data": {"text": "海报"}},
        {"type": "image", "data": {"file": "p.png", "url": "http://x/p.png?a=1&b=2"}},
    ]

    sc = HomeworkScanner()
    sc.running = True
    sc._store = store
    sc._client = FakeClient(history)
    sc._group_whitelist = {gid}
    sc._image_whitelist = {gid}
    sc._ocr = object()          # 非 None 即启用图片路
    sc._ocr_queue = asyncio.Queue()
    sc._image_dir = tmp
    sc._catchup_page_size = 4    # 故意小，强制翻页
    sc._catchup_max_pages = 5
    sc._catchup_limit = 200
    sc._catchup_max_age_hours = 24 * 365 * 60  # 关掉时间下限，专测断点逻辑
    sc._teacher_roles = {"owner", "admin"}     # 消息发送者是 member，作业路自然跳过

    replayed = await sc._catchup_group(gid)
    expect = [m for m in history if m["time"] > 1000]
    check("补抓只重放断点之后的消息", replayed == len(expect), f"{replayed} vs {len(expect)}")
    check("补抓发生了翻页", sc._client.page_calls > 1, f"{sc._client.page_calls} 页")
    check("首页锚点为空、后续页带锚点",
          sc._client.anchors[0] is None and sc._client.anchors[1] is not None,
          str(sc._client.anchors))

    p = await store.get_progress(gid)
    newest = max(m["time"] for m in history)
    check("重放后断点推进到最新消息", p["last_time"] == newest, str(p))

    pend = await store.pending_lecture_notes()
    check("补抓到的图片也进了 OCR 队列（拿不到图仍留痕）",
          len(pend) == 1 and pend[0]["local_path"] == "", str(pend))
    check("图片任务已入内存队列", sc._ocr_queue.qsize() == 1)

    # 再补一次：断点已到最新，应当零重放（幂等，不会重复轰炸）
    sc._client.page_calls = 0
    again = await sc._catchup_group(gid)
    check("二次补抓零重放（幂等）", again == 0, f"重放 {again} 条")
    check("二次补抓不重复入队图片",
          await store.count_pending_lecture_notes() == 1)

    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
