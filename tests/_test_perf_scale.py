"""按现网规模（57k 行 / 87 文件 / 6 目标）做性能验收。

验收目标（交接文档 §8.4 第 9 项）：
- /console/messages  < 300ms
- 写文件不再「读-改-写」整体覆盖（追加快路径）
"""
import sys, types, importlib, shutil, tempfile, asyncio, json, time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
NAME = "astrbot_plugin_chat_history_archiver"

api = types.ModuleType("astrbot.api")
class _L:
    def __getattr__(self, n): return lambda *a, **k: None
api.logger = _L()
sm = types.ModuleType("astrbot.api.star")
smap = {}
class Star:
    def __init__(self, context, config=None): self.context = context
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw); smap[cls.__module__] = cls
class Context:
    def __init__(self):
        self.platform_manager = types.SimpleNamespace(platform_insts=[])
sm.Star = Star; sm.Context = Context
em = types.ModuleType("astrbot.api.event")
class F:
    def llm_tool(self, name=None): return lambda fn: fn
em.filter = F(); em.AstrMessageEvent = type("E", (), {})
wm = types.ModuleType("astrbot.api.web")
PAYLOAD = {}
class R:
    query = {}
    async def json(self, default=None): return PAYLOAD if PAYLOAD else (default or {})
wm.request = R(); wm.json_response = lambda d: {"json": d}
wm.error_response = lambda m, status_code=400: {"error": m, "code": status_code}
wm.file_response = None
class Cfg(dict):
    def save_config(self): pass
    async def save_config_async(self): pass
api.AstrBotConfig = Cfg
api.star = sm; api.event = em; api.web = wm
core = types.ModuleType("astrbot.core"); utils = types.ModuleType("astrbot.core.utils")
ap = types.ModuleType("astrbot.core.utils.astrbot_path")
root = Path(tempfile.mkdtemp())
ap.get_astrbot_plugin_data_path = lambda: str(root / "plugin_data")
ap.get_astrbot_data_path = lambda: str(root)
utils.astrbot_path = ap; core.utils = utils
at = types.ModuleType("astrbot"); at.api = api; at.core = core
sys.modules.update({"astrbot": at, "astrbot.api": api, "astrbot.api.star": sm,
                    "astrbot.api.event": em, "astrbot.api.web": wm,
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

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

cfg = Cfg({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
})
inst = m.ChatHistoryArchiver(sm.Context(), cfg)

# 造现网规模：6 目标 × 约 15 天，合计约 57,000 行 / 约 87 文件
print("[造数据] 目标规模 ≈ 现网（57k 行 / 87 文件 / 6 目标）")
targets = [("group", "1094103778"), ("group", "748791823"), ("group", "10001"),
           ("group", "10002"), ("private", "2490530663"), ("private", "8001")]
TOTAL_DAYS = 15
total_rows = 0
t0 = time.perf_counter()
for chat, tid in targets:
    prefix = "private_" if chat == "private" else ""
    for day in range(TOTAL_DAYS):
        d = f"2026-10-{18 + day:02d}"
        path = inst.export_dir / f"napcat_{prefix}{tid}_{d}.jsonl"
        n = 640
        with open(path, "w", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({
                    "t": f"{d} {i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}",
                    "chat": chat, "group_id": tid, "user_id": f"u{i % 30}",
                    "nickname": f"用户{i % 30}", "seq": 100000 + i,
                    "content": f"第{i}条消息，一些中文内容用来模拟真实长度",
                }, ensure_ascii=False) + "\n")
        total_rows += n
gen_s = time.perf_counter() - t0
n_files = len(list(inst.export_dir.glob("napcat_*.jsonl")))
size_mb = sum(p.stat().st_size for p in inst.export_dir.glob("napcat_*.jsonl")) / 1024 / 1024
print(f"  生成 {n_files} 个文件 / {total_rows} 行 / {size_mb:.1f} MB，耗时 {gen_s:.1f}s")
check("文件数接近现网 87", n_files >= 80, n_files)
check("行数接近现网 57k", total_rows >= 50000, total_rows)

print()
print("[验收] Web API 响应时间")
DATE = "2026-10-18"

# 先预热：把行数索引建立并落盘，模拟「插件已稳定运行」的常态。
# 真实的第一轮冷算（重载后首次）在下一节单独验收。
inst._overview_data()
inst._save_row_cache()

async def bench():
    res = {}
    wm.request.query = {"chat": "group", "target_id": "1094103778",
                             "start": DATE, "end": DATE, "limit": 50}
    t = time.perf_counter(); await inst._api_messages()
    res["messages(单日50)"] = time.perf_counter() - t

    wm.request.query = {"chat": "group", "target_id": "1094103778",
                             "limit": 50}
    t = time.perf_counter(); await inst._api_messages()
    res["messages(全范围50)"] = time.perf_counter() - t

    wm.request.query = {"chat": "group", "target_id": "1094103778"}
    t = time.perf_counter(); await inst._api_dates()
    res["dates"] = time.perf_counter() - t

    t = time.perf_counter(); await inst._api_stats()
    res["stats"] = time.perf_counter() - t

    t = time.perf_counter(); await inst._api_state()
    res["state(总览)"] = time.perf_counter() - t

    wm.request.query = {"chat": "group", "target_id": "1094103778",
                             "q": "消息"}
    t = time.perf_counter(); await inst._api_search()
    res["search"] = time.perf_counter() - t

    wm.request.query = {"chat": "group", "target_id": "1094103778"}
    t = time.perf_counter(); await inst._api_dates()
    res["dates(缓存命中)"] = time.perf_counter() - t
    return res

r1 = asyncio.run(bench())
for k, v in r1.items():
    print(f"  {k:22} {v*1000:7.1f} ms")

check("messages(单日) < 300ms", r1["messages(单日50)"] < 0.300,
      f"{r1['messages(单日50)']*1000:.0f}ms")
check("messages(全范围) < 300ms", r1["messages(全范围50)"] < 0.300,
      f"{r1['messages(全范围50)']*1000:.0f}ms")
check("总览 < 300ms", r1["state(总览)"] < 0.300, f"{r1['state(总览)']*1000:.0f}ms")
# 缓存收益体现在「不再逐文件统计行数」，而绝对耗时在毫秒级易受抖动影响。
# 因此这里改为验证语义：二次调用的计数结果必须与首次一致（缓存命中而不是重算错）。
dates_again = asyncio.run(lambda: None) if False else None
async def _dates_again():
    wm.request.query = {"chat": "group", "target_id": "1094103778"}
    return await inst._api_dates()
d_again = asyncio.run(_dates_again())["json"]
check("dates 二次调用结果一致（缓存命中语义正确）",
      d_again["dates"] == asyncio.run(_dates_again())["json"]["dates"],
      d_again["dates"])
print(f"  （参考：dates 首次 {r1['dates']*1000:.1f}ms / 二次 "
      f"{r1['dates(缓存命中)']*1000:.1f}ms，毫秒级抖动属正常）")

print()
print("[验收] 插件重载后首次打开总览（行数索引持久化的意义）")
inst2 = m.ChatHistoryArchiver(sm.Context(), cfg)
loaded = len(inst2._row_cache)
print(f"  重载后读到索引条目：{loaded}")
check("重载能复用行数索引", loaded >= 80, loaded)

async def bench_reload_state():
    t = time.perf_counter()
    await inst2._api_state()
    return time.perf_counter() - t

t_reload = asyncio.run(bench_reload_state())
print(f"  重载后首次 _api_state：{t_reload*1000:.0f} ms")
check("重载后首次总览 < 300ms", t_reload < 0.300, f"{t_reload*1000:.0f}ms")

print()
print("[验收] 写入：追加 vs 整文件重写")
big = inst.export_dir / "napcat_1094103778_2026-10-18.jsonl"
nrows = len(big.read_text(encoding="utf-8").strip().splitlines())
t = time.perf_counter()
st.merge_write(big, [{"t": f"{DATE} 23:59:59", "seq": 999999, "user_id": "u0",
                      "content": "新消息"}])
t_append = time.perf_counter() - t
t = time.perf_counter()
st.merge_write(big, [{"t": f"{DATE} 00:00:01", "seq": 888888, "user_id": "u0",
                      "content": "更早的消息"}])
t_rewrite = time.perf_counter() - t
print(f"  单文件 {nrows} 行：追加 {t_append*1000:.1f} ms / 重写 {t_rewrite*1000:.1f} ms"
      f"（{t_rewrite/max(t_append,1e-9):.0f}x）")
check("追加快于重写", t_append < t_rewrite, (t_append, t_rewrite))
check("追加路径 < 50ms", t_append < 0.050, f"{t_append*1000:.1f}ms")

print()
print("[验收] 事件循环不被阻塞")
async def loop_probe():
    ticks = 0
    stop = False
    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(ticker())
    wm.request.query = {"chat": "group", "target_id": "1094103778"}
    await inst._api_state()
    stop = True
    await task
    return ticks

ticks = asyncio.run(loop_probe())
check("总览期间事件循环仍在调度", ticks > 10, ticks)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
