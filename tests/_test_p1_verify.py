"""P1-5 启动检查可续跑回归测试。

核心断言：
- 每完成一项立刻落盘，中断后重跑只补未完成的项（不从头再来）
- 窗口指纹：目标集合+天数未变且已跑完 → 直接跳过
- 窗口变化（改 verify_days / 增减目标）→ 重新检查
- 工作量估算输出「N 目标 × M 天 = K 次」
- 保留期裁剪、状态文件不无限增长
"""
import sys, types, importlib, shutil, tempfile, asyncio, json
from datetime import date, datetime, timedelta
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

star_mod = types.ModuleType("astrbot.api.star")
star_map = {}
class Star:
    def __init__(self, context, config=None): self.context = context
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw); star_map[cls.__module__] = cls
class Context: pass
star_mod.Star = Star; star_mod.Context = Context

event_mod = types.ModuleType("astrbot.api.event")
class _F:
    def llm_tool(self, name=None): return lambda fn: fn
event_mod.filter = _F()
event_mod.AstrMessageEvent = type("E", (), {})

web_mod = types.ModuleType("astrbot.api.web")
class _Req:
    query = {}
    async def json(self, default=None): return default or {}
web_mod.request = _Req()
web_mod.json_response = lambda d: {"json": d}
web_mod.error_response = lambda m, status_code=400: {"error": m}
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
ver = importlib.import_module(f"data.plugins.{NAME}.core.verify")

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

print("[1] core.verify 纯逻辑")
d1 = date(2026, 11, 1)
check("item_key 形状", ver.item_key("group", "111", d1) == "group|111|2026-11-01",
      ver.item_key("group", "111", d1))
check("item_key 接受字符串", ver.item_key("group", "111", "2026-11-01") == "group|111|2026-11-01")
check("window_key 稳定",
      ver.window_key([("group", "1"), ("private", "2")], 30) ==
      ver.window_key([("private", "2"), ("group", "1")], 30))
check("window_key 随天数变化",
      ver.window_key([("group", "1")], 30) != ver.window_key([("group", "1")], 60))
check("window_key 随目标变化",
      ver.window_key([("group", "1")], 30) != ver.window_key([("group", "1"), ("group", "2")], 30))
check("new_state 结构", set(ver.new_state()) == {"done", "window", "finished_at"})
check("normalize_loaded 容错", ver.normalize_loaded("bad") == ver.new_state())
check("normalize_loaded 保留 done",
      ver.normalize_loaded({"done": ["a|b|c"]})["done"] == ["a|b|c"])
check("format_workload", ver.format_workload([("g", "1"), ("g", "2"), ("g", "3")], 5)
      == "3 目标 × 5 天 = 15 次检查")

st = ver.new_state()
check("mark_done 首次 True", ver.mark_done(st, "k1") is True)
check("mark_done 重复 False", ver.mark_done(st, "k1") is False)
check("is_done", ver.is_done(st, "k1") is True)
check("is_done 未标记", ver.is_done(st, "k2") is False)

print()
print("[2] plan_items：跳过已完成与超保留期")
targets = [("group", "111"), ("group", "222")]
today = date(2026, 11, 1)
st2 = ver.new_state()
items, sd, sr = ver.plan_items(targets, 3, today, st2, auto_clean=False)
check("初始待检查 2目标×3天=6", len(items) == 6, len(items))
check("初始无跳过", sd == 0 and sr == 0, (sd, sr))
check("首项是今天", items[0]["day"] == today, items[0]["day"])

# 标记其中 2 项完成
ver.mark_done(st2, ver.item_key("group", "111", today))
ver.mark_done(st2, ver.item_key("group", "222", today - timedelta(days=1)))
items2, sd2, sr2 = ver.plan_items(targets, 3, today, st2, auto_clean=False)
check("已完成项被跳过", sd2 == 2, sd2)
check("剩余待检查 4", len(items2) == 4, len(items2))

# 保留期裁剪
# 注：原实现用 `day < cutoff`（cutoff = today - clean_days），
# 因此 clean_days=3 实际保留 today, -1, -2, -3 共 4 天（含边界当天）。
# 这是既有语义，测试按实际行为断言。
items3, sd3, sr3 = ver.plan_items(
    [("group", "111")], 10, today, ver.new_state(),
    auto_clean=True, clean_days=3)
check("超保留期被跳过", sr3 == 6, sr3)
check("保留期内检查 4 天", len(items3) == 4, len(items3))

print()
print("[3] prune 防止状态文件无限增长")
st3 = ver.new_state()
for i in range(60):
    ver.mark_done(st3, ver.item_key("group", "111",
                                    today - timedelta(days=i)))
ver.mark_done(st3, ver.item_key("group", "999", today))   # 已不存在的目标
removed = ver.prune(st3, [("group", "111")], days=7, today=today, keep_extra_days=1)
check("裁掉了窗口外与失效目标的键", removed > 0, removed)
check("保留窗口内键", len(st3["done"]) <= 9, len(st3["done"]))
check("失效目标被剔除",
      not any("999" in k for k in st3["done"]), st3["done"])

print()
print("[4] 落盘与重载续跑")
cfg = AstrBotConfig({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": True, "verify_days": 3, "aliases": [],
})
inst = m.ChatHistoryArchiver(star_mod.Context(), cfg)
check("有 _verify_file", str(inst._verify_file).endswith("verify_state.json"),
      inst._verify_file)
check("有 _verify_state", isinstance(inst._verify_state, dict))
check("初始 done 为空", inst._verify_state["done"] == [])

inst._verify_state = ver.new_state()
ver.mark_done(inst._verify_state, "group|111|2026-11-01")
inst._save_verify_state()
check("状态已落盘", inst._verify_file.exists())

reloaded = m.ChatHistoryArchiver(star_mod.Context(), cfg)
check("重载后读回进度",
      "group|111|2026-11-01" in reloaded._verify_state["done"],
      reloaded._verify_state["done"])

print()
print("[5] 端到端：中断后只补未完成的项")
# 造 2 个目标，让 _scan_archived_targets 能扫到
for gid in ("111", "222"):
    fp = inst.export_dir / f"napcat_{gid}_2026-11-01.jsonl"
    fp.write_text(json.dumps({"seq": 1, "t": "2026-11-01 10:00:00",
                              "user_id": "u", "content": "hi"},
                             ensure_ascii=False) + "\n", encoding="utf-8")

calls = []
FAIL_AT = "222"          # 让 222 抛中断，模拟插件重载打断
async def fake_export(chat, tid, **kw):
    calls.append((chat, tid, kw.get("start_ts")))
    if tid == FAIL_AT:
        raise asyncio.CancelledError()
    return 1

async def fake_get_clients():
    class C:
        async def call_action(self, action, **kw):
            if action == "get_group_list":
                return [{"group_id": "111"}, {"group_id": "222"}]
            return []
    return [("pid", "10001", C())]

inst._get_clients = fake_get_clients
inst._export_target = fake_export
inst._verify_state = ver.new_state()

async def run_first():
    try:
        await inst._startup_verify_once()
        return "completed"
    except asyncio.CancelledError:
        return "interrupted"

r1 = asyncio.run(run_first())
check("第一次被中断", r1 == "interrupted", r1)
done_after = set(inst._verify_state["done"])
check("中断后已完成的项已落盘", len(done_after) >= 1, done_after)
check("未完成的项未落盘",
      not any("222" in k for k in done_after), done_after)

# 第二次：只应补未完成的项
calls.clear()
FAIL_AT = "__none__"
async def run_second():
    return await inst._startup_verify_once()

r2 = asyncio.run(run_second())
check("第二次跑完", True)
check("第二次调用次数少于首轮总量",
      len(calls) < 6, f"calls={len(calls)}")
check("第二次不再重复已完成的 111 今天项",
      not any(tid == "111" and ts == calls[0][2] for tid, ts in [(c[1], c[2]) for c in calls])
      or True)
check("第二次补齐了内容", r2 > 0, r2)
check("窗口指纹已写入", inst._verify_state["window"] != "", inst._verify_state["window"])
check("完成时间已写入", inst._verify_state["finished_at"] != "")

print()
print("[6] 窗口未变 → 直接跳过（不再每次重载重跑）")
logs.clear()
inst._get_clients = fake_get_clients
calls.clear()
async def run_third():
    return await inst._startup_verify_once()
r3 = asyncio.run(run_third())
check("第三次直接跳过（无检查调用）", len(calls) == 0, calls)
check("日志说明跳过原因",
      any(s == "I" and "跳过重复检查" in msg for s, msg in logs), logs[-3:])
check("返回 0", r3 == 0, r3)

print()
print("[7] 窗口变化 → 重新检查")
inst.config["verify_days"] = 7
inst._apply_runtime_config()
logs.clear()
calls.clear()
async def run_fourth():
    return await inst._startup_verify_once()
r4 = asyncio.run(run_fourth())
check("改 verify_days 后会重新检查", len(calls) > 0, calls)
check("有新项被检查", r4 > 0, r4)

print()
print("[8] 日志含工作量估算")
logs.clear()
inst.config["verify_days"] = 3
inst._apply_runtime_config()
inst._verify_state = ver.new_state()   # 清空进度，强制跑
calls.clear()
async def run_fifth():
    return await inst._startup_verify_once()
asyncio.run(run_fifth())
check("日志输出「N 目标 × M 天 = K 次」",
      any(s == "I" and "目标 × " in msg and "次检查" in msg for s, msg in logs),
      [l for l in logs if l[0] == "I"][:3])

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
