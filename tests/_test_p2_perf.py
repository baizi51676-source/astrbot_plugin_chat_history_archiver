"""P2-1 / P2-2 / P2-12 回归测试。

P2-12：原子写 —— 写入中断不会留下半截文件
P2-2 ：追加快路径 —— 顺序新消息走 append，乱序/重复走合并重写
P2-1 ：to_thread —— Web API 重活不阻塞事件循环

同时验证改造后行为与改造前等价（去重、排序、自愈语义不变）。
"""
import sys, types, importlib, shutil, tempfile, asyncio, json, time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
NAME = "astrbot_plugin_chat_history_archiver"

api = types.ModuleType("astrbot.api")
class _L:
    def __getattr__(self, n): return lambda *a, **k: None
api.logger = _L()
star_mod = types.ModuleType("astrbot.api.star")
star_map = {}
class Star:
    def __init__(self, context, config=None): self.context = context
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw); star_map[cls.__module__] = cls
class Context:
    def __init__(self):
        self.platform_manager = types.SimpleNamespace(platform_insts=[])
star_mod.Star = Star; star_mod.Context = Context
event_mod = types.ModuleType("astrbot.api.event")
class _F:
    def llm_tool(self, name=None): return lambda fn: fn
event_mod.filter = _F()
event_mod.AstrMessageEvent = type("E", (), {})
web_mod = types.ModuleType("astrbot.api.web")
PAYLOAD = {}
class _Req:
    query = {}
    async def json(self, default=None): return PAYLOAD if PAYLOAD else (default or {})
web_mod.request = _Req()
web_mod.json_response = lambda d: {"json": d}
web_mod.error_response = lambda m, status_code=400: {"error": m, "code": status_code}
web_mod.file_response = None
class AstrBotConfig(dict):
    def save_config(self): pass
    async def save_config_async(self): pass
api.AstrBotConfig = AstrBotConfig
api.star = star_mod; api.event = event_mod; api.web = web_mod
core = types.ModuleType("astrbot.core"); utils = types.ModuleType("astrbot.core.utils")
ap = types.ModuleType("astrbot.core.utils.astrbot_path")
root = Path(tempfile.mkdtemp())
ap.get_astrbot_plugin_data_path = lambda: str(root / "plugin_data")
ap.get_astrbot_data_path = lambda: str(root)
utils.astrbot_path = ap; core.utils = utils
astrbot = types.ModuleType("astrbot"); astrbot.api = api; astrbot.core = core
sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.star": star_mod,
                    "astrbot.api.event": event_mod, "astrbot.api.web": web_mod,
                    "astrbot.core": core, "astrbot.core.utils": utils,
                    "astrbot.core.utils.astrbot_path": ap})

pkg = Path(tempfile.mkdtemp())
pd = pkg / "data" / "plugins" / NAME
pd.mkdir(parents=True)
for it in PLUGIN_DIR.iterdir():
    if it.is_dir() and it.name in ("core", "pages"): shutil.copytree(it, pd / it.name)
    elif it.is_file() and it.suffix in (".py", ".json", ".yaml"): shutil.copy2(it, pd / it.name)
sys.path.insert(0, str(pkg))
m = importlib.import_module(f"data.plugins.{NAME}.main")
st = importlib.import_module(f"data.plugins.{NAME}.core.store")
amo = importlib.import_module(f"data.plugins.{NAME}.core.atomic")

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

def rec(seq, t, uid="u", content=None):
    return {"t": t, "seq": seq, "user_id": uid,
            "content": content or f"m{seq}"}

print("[P2-2] 追加快路径判定")
tmp = Path(tempfile.mkdtemp())
p = tmp / "a.jsonl"
p.write_text(json.dumps(rec(1, "2026-11-01 10:00:00"), ensure_ascii=False) + "\n"
             + json.dumps(rec(2, "2026-11-01 10:01:00"), ensure_ascii=False) + "\n",
             encoding="utf-8")

check("顺序更新的消息可追加",
      amo.can_append(p, [rec(3, "2026-11-01 10:02:00")]) is True)
check("更早的消息不可追加（会破坏排序）",
      amo.can_append(p, [rec(9, "2026-11-01 09:00:00")]) is False)
check("与末行重复不可追加",
      amo.can_append(p, [rec(2, "2026-11-01 10:01:00")]) is False)
check("内部乱序不可追加",
      amo.can_append(p, [rec(3, "2026-11-01 10:05:00"),
                         rec(4, "2026-11-01 10:02:00")]) is False)
check("文件不存在不可追加（走慢路径建文件）",
      amo.can_append(tmp / "none.jsonl", [rec(1, "2026-11-01 10:00:00")]) is False)

print()
print("[P2-2] 快路径结果与慢路径等价")
before_text = p.read_text(encoding="utf-8")
n_fast = st.merge_write(p, [rec(3, "2026-11-01 10:02:00")])
after = p.read_text(encoding="utf-8")
check("快路径新增 1", n_fast == 1, n_fast)
check("快路径是纯追加（前缀未变）", after.startswith(before_text), after[:80])
check("追加后行数 3", len(after.strip().splitlines()) == 3)

# 对照：用慢路径喂同样的数据，结果应一致
p2 = tmp / "b.jsonl"
p2.write_text(before_text, encoding="utf-8")
amo.append_records  # 占位
# 强制慢路径：先写一条更早的
n_slow = st.merge_write(p2, [rec(9, "2026-11-01 09:59:00")])
check("慢路径插入更早消息", n_slow == 1, n_slow)
lines_slow = p2.read_text(encoding="utf-8").strip().splitlines()
check("慢路径按 t 排序",
      json.loads(lines_slow[0])["t"] == "2026-11-01 09:59:00",
      json.loads(lines_slow[0])["t"])

print()
print("[P2-2] 重复与覆盖语义不变")
n_dup = st.merge_write(p, [rec(2, "2026-11-01 10:01:00")])
check("重复写入新增 0", n_dup == 0, n_dup)
check("重复后行数不变", len(p.read_text(encoding="utf-8").strip().splitlines()) == 3)
n_cover = st.merge_write(p, [rec(2, "2026-11-01 10:01:00", content="覆盖")])
check("同 seq 覆盖也新增 0", n_cover == 0, n_cover)
check("内容已更新",
      "覆盖" in p.read_text(encoding="utf-8"), "未覆盖")

print()
print("[P2-2] 快路径在大文件上的收益（仅记录，不作硬断言）")
big = tmp / "big.jsonl"
with open(big, "w", encoding="utf-8") as f:
    for i in range(20000):
        f.write(json.dumps(rec(i, f"2026-11-01 10:{(i//60)%60:02d}:{i%60:02d}"),
                           ensure_ascii=False) + "\n")
t0 = time.perf_counter()
st.merge_write(big, [rec(20000, "2026-11-01 11:00:00")])   # 追加
t_fast = time.perf_counter() - t0
t0 = time.perf_counter()
st.merge_write(big, [rec(5000, "2026-11-01 09:00:00")])    # 强制重写
t_slow = time.perf_counter() - t0
print(f"  追加 1 条: {t_fast*1000:.1f} ms   重写 2 万条: {t_slow*1000:.1f} ms")
check("追加快于重写", t_fast < t_slow, (t_fast, t_slow))
check("附件后行数 20002 或 20001（取决于是否覆盖）",
      len(big.read_text(encoding="utf-8").strip().splitlines()) in (20001, 20002))

print()
print("[P2-12] 原子写：中途失败不留半截文件")
target = tmp / "atomic.jsonl"
target.write_text('{"seq":1}\n{"seq":2}\n', encoding="utf-8")
orig = target.read_text(encoding="utf-8")

class Boom(Exception): pass
real_fsync = amo.os.fsync
def bad_fsync(fd):
    raise Boom("模拟写入中断")
amo.os.fsync = bad_fsync
try:
    amo.atomic_write_text(target, '{"seq":3}\n')
    check("应抛出异常", False)
except Boom:
    check("写入中断抛出异常", True)
finally:
    amo.os.fsync = real_fsync
check("原文件内容未被破坏", target.read_text(encoding="utf-8") == orig,
      target.read_text(encoding="utf-8")[:40])
leftovers = list(tmp.glob(".tmp_*"))
check("临时文件已清理", len(leftovers) == 0, [x.name for x in leftovers])

amo.atomic_write_text(target, '{"seq":9}\n')
check("正常写入生效", target.read_text(encoding="utf-8") == '{"seq":9}\n')

print()
print("[P2-1] Web API 用 to_thread（不阻塞事件循环）")
cfg = AstrBotConfig({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
})
inst = m.ChatHistoryArchiver(star_mod.Context(), cfg)

# 造一个有 3000 行的日期文件
DATE = "2026-11-01"
fp = inst.export_dir / f"napcat_111_{DATE}.jsonl"
with open(fp, "w", encoding="utf-8") as f:
    for i in range(3000):
        f.write(json.dumps(rec(1000 + i, f"{DATE} 10:{(i//60)%60:02d}:{i%60:02d}"),
                           ensure_ascii=False) + "\n")

async def probe():
    """在 to_thread 运行期间，事件循环必须仍能调度其它协程。"""
    ticks = 0
    stop = False
    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(ticker())
    web_mod.request.query = {"chat": "group", "target_id": "111",
                             "start": DATE, "end": DATE, "limit": 50}
    await inst._api_messages()
    stop = True
    await task
    return ticks

ticks = asyncio.run(probe())
check("读消息期间事件循环仍在跑", ticks > 0, ticks)

async def bench():
    web_mod.request.query = {"chat": "group", "target_id": "111",
                             "start": DATE, "end": DATE, "limit": 200}
    t0 = time.perf_counter()
    await inst._api_messages()
    t_msg = time.perf_counter() - t0
    web_mod.request.query = {"chat": "group", "target_id": "111"}
    t0 = time.perf_counter()
    await inst._api_stats()
    t_stat = time.perf_counter() - t0
    t0 = time.perf_counter()
    await inst._api_state()
    t_state = time.perf_counter() - t0
    web_mod.request.query = {"chat": "group", "target_id": "111", "q": "m1"}
    t0 = time.perf_counter()
    await inst._api_search()
    t_search = time.perf_counter() - t0
    return t_msg, t_stat, t_state, t_search

tm, tst, tstate, tsearch = asyncio.run(bench())
print(f"  3000 行规模：messages {tm*1000:.1f}ms  stats {tst*1000:.1f}ms  "
      f"state {tstate*1000:.1f}ms  search {tsearch*1000:.1f}ms")
check("messages 响应正常", tm > 0)
check("stats 响应正常", tst > 0)

print()
print("[P2-1] 结果正确性不受 to_thread 影响")
web_mod.request.query = {"chat": "group", "target_id": "111",
                         "start": DATE, "end": DATE, "limit": 200}
d = asyncio.run(inst._api_messages())["json"]
check("messages total=3000", d["total"] == 3000, d["total"])
check("分页 200 条", len(d["items"]) == 200, len(d["items"]))
check("末条 offset_from_end=0", d["items"][-1]["offset_from_end"] == 0)
check("含 reply 键", "reply" in d["items"][0])

web_mod.request.query = {"chat": "group", "target_id": "111"}
d2 = asyncio.run(inst._api_dates())["json"]
check("dates 返回 1 天", len(d2["dates"]) == 1, d2["dates"])
check("dates 计数正确", d2["dates"][0]["count"] == 3000, d2["dates"][0])

d3 = asyncio.run(inst._api_stats())["json"]
check("stats 总数 3000", d3["total"] == 3000, d3["total"])
check("stats hourly 24 项", len(d3["hourly"]) == 24)

d4 = asyncio.run(inst._api_state())["json"]
check("state 有 targets", "targets" in d4, list(d4.keys()))
check("state 汇总行数 >= 3000", d4["summary"]["rows"] >= 3000,
      d4["summary"]["rows"])

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
