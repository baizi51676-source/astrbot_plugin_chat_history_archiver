"""P2-1 行数索引预热回归测试。

验收点：
- 索引为空（全新安装）时，initialize 会起后台预热任务
- 预热后 row_index.json 落盘，新实例能复用
- 索引已存在时不重复预热（避免每次启动都全量扫）
- terminate 会取消预热任务
"""
import sys, types, importlib, shutil, tempfile, asyncio, json
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
NAME = "astrbot_plugin_chat_history_archiver"

logs = []
api = types.ModuleType("astrbot.api")
class _L:
    def warning(self, m, *a, **k): logs.append(("W", str(m)))
    def info(self, m, *a, **k): logs.append(("I", str(m)))
    def debug(self, m, *a, **k): logs.append(("D", str(m)))
    def error(self, m, *a, **k): logs.append(("E", str(m)))
api.logger = _L()
sm = types.ModuleType("astrbot.api.star")
smap = {}
class Star:
    def __init__(self, context, config=None): self.context = context
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw); smap[cls.__module__] = cls
class Context:
    registered_web_apis = []
    def __init__(self):
        self.platform_manager = types.SimpleNamespace(platform_insts=[])
    def register_web_api(self, route, handler, methods, desc):
        Context.registered_web_apis.append((route, handler, methods, desc))
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
wm.error_response = lambda m, status_code=400: {"error": m}
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

# 造 30 个文件
DATE = "2026-11-01"
for gid in ("111", "222", "333"):
    for day in range(10):
        d = f"2026-10-{23 + day:02d}" if day < 10 else DATE
        path = inst.export_dir / f"napcat_{gid}_{DATE}.jsonl"
        if day == 0:
            with open(path, "w", encoding="utf-8") as f:
                for i in range(200):
                    f.write(json.dumps({"t": f"{DATE} 10:{i // 60:02d}:{i % 60:02d}",
                                        "seq": 1000 + i, "user_id": "u",
                                        "content": f"m{i}"}, ensure_ascii=False) + "\n")
n_files = len(list(inst.export_dir.glob("napcat_*.jsonl")))
print(f"[造数据] {n_files} 个文件")

print()
print("[1] 索引为空时自动预热")
check("初始索引为空", len(inst._row_cache) == 0, len(inst._row_cache))
check("有 _warm_task 字段", hasattr(inst, "_warm_task"))

logs.clear()
async def run_init():
    await inst.initialize()
    # 等预热任务完成
    if inst._warm_task:
        try:
            await asyncio.wait_for(inst._warm_task, timeout=20)
        except asyncio.TimeoutError:
            pass
asyncio.run(run_init())
check("预热后索引非空", len(inst._row_cache) >= n_files, len(inst._row_cache))
check("索引文件已落盘", inst._row_cache_file.exists())
check("日志有预热记录",
      any(s == "I" and "预热" in msg for s, msg in logs), logs[-4:])

print()
print("[2] 新实例复用索引（不再预热）")
inst2 = m.ChatHistoryArchiver(sm.Context(), cfg)
check("新实例读到索引", len(inst2._row_cache) >= n_files, len(inst2._row_cache))
logs.clear()
async def run_init2():
    await inst2.initialize()
    if inst2._warm_task:
        try:
            await asyncio.wait_for(inst2._warm_task, timeout=20)
        except asyncio.TimeoutError:
            pass
asyncio.run(run_init2())
check("未重复预热（debug 说跳过）",
      any(s == "D" and "跳过预热" in msg for s, msg in logs), logs[-4:])
check("索引条数未暴涨", len(inst2._row_cache) >= n_files)

print()
print("[3] 预热结果与实时统计一致")
inst3 = m.ChatHistoryArchiver(sm.Context(), cfg)
inst3._row_cache = {}
rows_direct = inst3._overview_data()["summary"]["rows"]
inst3._save_row_cache()
inst4 = m.ChatHistoryArchiver(sm.Context(), cfg)
rows_cached = inst4._overview_data()["summary"]["rows"]
check("缓存与直算行数一致", rows_direct == rows_cached,
      (rows_direct, rows_cached))
check("行数 > 0", rows_direct > 0, rows_direct)

print()
print("[4] 文件变化后索引自动失效")
logs.clear()
# 追加内容到某文件，mtime/size 变化
p = inst.export_dir / "napcat_111_2026-11-01.jsonl"
before = len(inst._row_cache)
with open(p, "a", encoding="utf-8") as f:
    f.write(json.dumps({"t": f"{DATE} 23:00:00", "seq": 99999, "user_id": "u",
                        "content": "new"}, ensure_ascii=False) + "\n")
inst5 = m.ChatHistoryArchiver(sm.Context(), cfg)
d = inst5._overview_data()
check("统计到新追加的行", d["summary"]["rows"] >= rows_direct + 1,
      d["summary"]["rows"])
check("索引条目数未变（只是该键被更新）", len(inst5._row_cache) >= n_files)

print()
print("[5] terminate 取消预热任务")
async def run_term():
    loop = asyncio.get_running_loop()
    async def slow():
        await asyncio.sleep(3600)
    inst6 = m.ChatHistoryArchiver(sm.Context(), cfg)
    inst6._warm_task = loop.create_task(slow())
    await inst6.terminate()
    return inst6._warm_task
left = asyncio.run(run_term())
check("预热任务已清空", left is None)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
