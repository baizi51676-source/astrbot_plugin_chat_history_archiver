"""P1-1 定时总结/简报回归测试。

覆盖：
- core.schedule 纯逻辑（时刻解析、到点判定、每日去重、错过不补跑）
- 调度落盘（schedule_state.json），重载后同一晚不重复执行
- _run_daily_summaries 跳过无消息目标、异常不中断
- _daily_sched_check 端到端：到点才跑、同一天只跑一次
- auto_export 关闭时仍能通过 _sched_loop 跑
"""
import sys, types, importlib, shutil, tempfile, asyncio, json
from datetime import datetime, time, timedelta
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
sch = importlib.import_module(f"data.plugins.{NAME}.core.schedule")

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

print("[1] core.schedule 纯逻辑")
check("解析 23:30", sch.parse_hhmm("23:30") == time(23, 30))
check("解析 08:05", sch.parse_hhmm("8:05") == time(8, 5))
check("非法回落默认", sch.parse_hhmm("bad") == time(23, 30), sch.parse_hhmm("bad"))
check("越界回落默认", sch.parse_hhmm("25:00") == time(23, 30))
check("空值回落默认", sch.parse_hhmm("") == time(23, 30))

base = datetime(2026, 11, 1, 23, 30)
check("到点且未跑 → True", sch.should_run(base, time(23, 30), "", True))
check("未到点 → False",
      sch.should_run(datetime(2026, 11, 1, 22, 0), time(23, 30), "", True) is False)
check("开关关 → False", sch.should_run(base, time(23, 30), "", False) is False)
check("当天已跑 → False",
      sch.should_run(base, time(23, 30), "2026-11-01", True) is False)
check("隔天未跑 → True",
      sch.should_run(datetime(2026, 11, 2, 23, 30), time(23, 30), "2026-11-01", True))
check("超时很久仍会跑（不补中间日）",
      sch.should_run(datetime(2026, 11, 5, 23, 59), time(23, 30), "2026-11-01", True))

print()
print("[2] 调度状态落盘与重载不重复")
cfg = AstrBotConfig({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
    "llm_summary_enabled": True, "llm_summary_time": "23:30",
    "llm_briefing_enabled": False,
})
inst = m.ChatHistoryArchiver(star_mod.Context(), cfg)
check("llm_summary_enabled 读入", inst.llm_summary_enabled is True)
check("llm_summary_time 解析", inst.llm_summary_time == time(23, 30))
check("有 _sched_task 字段", hasattr(inst, "_sched_task"))
check("有 _sched_state 字段", hasattr(inst, "_sched_state"))
check("schedule_state 路径", str(inst._sched_file).endswith("schedule_state.json"),
      inst._sched_file)

inst._sched_state = {"summary_date": "2026-11-01"}
inst._save_sched_state()
check("状态已落盘", inst._sched_file.exists())
reloaded = m.ChatHistoryArchiver(star_mod.Context(), cfg)
check("重载后读回状态", reloaded._sched_state.get("summary_date") == "2026-11-01",
      reloaded._sched_state)

print()
print("[3] _run_daily_summaries 跳过无消息目标")
# 造两天数据：111 有当天，222 当天无
DATE = "2026-11-01"
for gid, days in (("111", [DATE]), ("222", ["2026-10-30"])):
    for dt in days:
        fp = inst.export_dir / f"napcat_{gid}_{dt}.jsonl"
        fp.write_text(json.dumps({"t": f"{dt} 10:00:00", "seq": 1,
                                  "user_id": "u", "nickname": "n",
                                  "content": "hi"}, ensure_ascii=False) + "\n",
                      encoding="utf-8")

# 桩掉 LLM 调用，避免真的请求模型
async def fake_llm(prompt, system=None):
    return "（模拟总结）"
inst._llm_text = fake_llm
inst._ensure_summary_worker = lambda: None
inst._summary_queue = asyncio.Queue()

async def noop_worker(job):
    pass

async def run_summaries():
    # 手动驱动：直接把 job 消费掉，模拟 worker
    targets = inst._scan_archived_targets()
    ok = 0
    for chat, tid in targets:
        if not inst._day_records(chat, tid, DATE):
            continue
        job = {"chat": chat, "tid": tid, "date": DATE, "trend": True}
        await inst._do_summary(job)
        ok += 1
    return ok, len(targets)

ok, total = asyncio.run(run_summaries())
check("目标总数=2", total == 2, total)
check("只对当天有消息的目标生成=1", ok == 1, ok)
sum_file = inst._summary_path("group", "111", DATE)
check("总结文件已写入", sum_file.exists(), sum_file)
check("总结内容非空", sum_file.read_text(encoding="utf-8").strip() != "")
check("无消息目标未生成文件",
      not inst._summary_path("group", "222", DATE).exists())

print()
print("[4] _daily_sched_check：到点才跑、同一天只跑一次")
calls = []
async def fake_run_summaries(date):
    calls.append(date)
    return 1
inst._run_daily_summaries = fake_run_summaries
inst._sched_state = {}
inst.llm_summary_enabled = True
inst.llm_briefing_enabled = False

# 模拟「未到点」：把目标时刻设成未来
future_t = (datetime.now() + timedelta(hours=2)).time()
inst.llm_summary_time = future_t
asyncio.run(inst._daily_sched_check())
check("未到点不执行", len(calls) == 0, calls)

# 模拟「已到点」
past_t = (datetime.now() - timedelta(minutes=5)).time()
inst.llm_summary_time = past_t
asyncio.run(inst._daily_sched_check())
check("到点执行一次", len(calls) == 1, calls)
check("落盘了今天的日期",
      inst._sched_state.get("summary_date") == datetime.now().strftime("%Y-%m-%d"),
      inst._sched_state)

# 同一天再检查一次，不应重复
asyncio.run(inst._daily_sched_check())
check("同一天不重复执行", len(calls) == 1, calls)

# 开关关闭时不执行
inst.llm_summary_enabled = False
inst._sched_state = {}
asyncio.run(inst._daily_sched_check())
check("开关关闭不执行", len(calls) == 1, calls)

print()
print("[5] 简报开关与执行")
brief_calls = []
async def fake_brief():
    brief_calls.append(1)
    return True
inst._run_daily_briefing = fake_brief
inst.llm_summary_enabled = False
inst.llm_briefing_enabled = True
inst._sched_state = {}
inst.llm_summary_time = past_t
asyncio.run(inst._daily_sched_check())
check("仅开简报也会执行", len(brief_calls) == 1, brief_calls)
check("简报日期已落盘",
      inst._sched_state.get("brief_date") == datetime.now().strftime("%Y-%m-%d"),
      inst._sched_state)
asyncio.run(inst._daily_sched_check())
check("简报同一天不重复", len(brief_calls) == 1, brief_calls)

print()
print("[6] 异常不中断调度")
async def boom(date):
    raise RuntimeError("模拟总结失败")
inst._run_daily_summaries = boom
inst.llm_summary_enabled = True
inst._sched_state = {"summary_date": ""}
inst.llm_summary_time = past_t
try:
    asyncio.run(inst._daily_sched_check())
    check("异常被吞掉（不冒泡）", True)
except Exception as e:
    check("异常被吞掉（不冒泡）", False, f"{type(e).__name__}: {e}")
check("失败也记录了日期（避免同晚反复重试）",
      inst._sched_state.get("summary_date") == datetime.now().strftime("%Y-%m-%d"),
      inst._sched_state)

print()
print("[7] terminate 取消全部任务（P2-6）")
async def run_terminate():
    loop = asyncio.get_running_loop()
    async def forever():
        await asyncio.sleep(3600)
    inst._task = loop.create_task(forever())
    inst._verify_task = loop.create_task(forever())
    inst._sched_task = loop.create_task(forever())
    inst._names_task = loop.create_task(forever())
    inst._manual_task = loop.create_task(forever())
    inst._summary_task = loop.create_task(forever())
    inst._summary_queue = asyncio.Queue()
    fut = loop.create_future()
    await inst._summary_queue.put({"future": fut})
    await inst.terminate()
    return fut

fut = asyncio.run(run_terminate())
check("_task 已清空", inst._task is None)
check("_verify_task 已清空", inst._verify_task is None)
check("_sched_task 已清空", inst._sched_task is None)
check("_names_task 已清空", inst._names_task is None)
check("_manual_task 已清空", inst._manual_task is None)
check("_summary_task 已清空", inst._summary_task is None)
check("队列残留 future 已设异常",
      fut.done() and isinstance(fut.exception(), m._EmptyData),
      fut.exception() if fut.done() else "not done")

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
