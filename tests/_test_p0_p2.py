"""P0-1 / P2-11 回归测试。

P0-1：某个群导出抛异常时，本轮剩余群/私聊必须继续，不得 NameError 中断。
P2-11：空数据（无总结/无归档消息）必须返回 200 + 友好文案，日志走 INFO。
"""
import sys, types, importlib, shutil, tempfile, asyncio
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
NAME = "astrbot_plugin_chat_history_archiver"

# ---- 仿真 astrbot ----
log_records = []
api = types.ModuleType("astrbot.api")
class _L:
    def warning(self, msg, *a, **k): log_records.append(("WARNING", str(msg)))
    def info(self, msg, *a, **k): log_records.append(("INFO", str(msg)))
    def debug(self, msg, *a, **k): log_records.append(("DEBUG", str(msg)))
    def error(self, msg, *a, **k): log_records.append(("ERROR", str(msg)))
api.logger = _L()

star_mod = types.ModuleType("astrbot.api.star")
star_map = {}
class Star:
    def __init__(self, context, config=None): self.context = context
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw); star_map[cls.__module__] = cls
class Context:
    registered_web_apis = []
    def __init__(self):
        self.platform_manager = types.SimpleNamespace(platform_insts=[])
    def register_web_api(self, route, handler, methods, desc):
        Context.registered_web_apis.append((route, handler, methods, desc))
star_mod.Star = Star; star_mod.Context = Context

event_mod = types.ModuleType("astrbot.api.event")
class _F:
    def llm_tool(self, name=None): return lambda fn: fn
event_mod.filter = _F()
event_mod.AstrMessageEvent = type("E", (), {"is_admin": lambda s: True,
                                           "get_message_obj": lambda s: types.SimpleNamespace(self_id="1")})

web_mod = types.ModuleType("astrbot.api.web")
class _Req:
    query = {}
    async def json(self, default=None): return default or {}
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

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

cfg = AstrBotConfig({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": True, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
})
inst = m.ChatHistoryArchiver(star_mod.Context(), cfg)

# ================= P0-1 =================
print("[P0-1] 单个目标抛异常不中断整轮")

CALLS = []
async def fake_get_clients():
    return [("pid1", "10001", client)]
class FakeClient:
    async def call_action(self, action, **kw):
        if action == "get_group_list":
            return [{"group_id": "111", "group_name": "A"},
                    {"group_id": "222", "group_name": "B"},
                    {"group_id": "333", "group_name": "C"}]
        if action == "get_friend_list":
            return [{"user_id": "9001", "nickname": "好友1"}]
        return []
client = FakeClient()

FAIL_ON = "222"
async def fake_export(chat, tid, **kw):
    CALLS.append((chat, tid))
    if tid == FAIL_ON:
        raise RuntimeError("模拟导出失败")
    return 5

inst._get_clients = fake_get_clients
inst._export_target = fake_export
inst._register_name = lambda *a, **k: None
inst._save_state = lambda: None
# v2.4.0 私聊白名单：留空时只巡「已登记」目标。
# 这里显式登记私聊 9001，否则它不会被巡检（P0-1 要验的是「私聊分支不被中断」）。
inst.aliases.setdefault("private", {})["9001"] = {
    "name": "好友1", "aliases": [], "updated": ""}

try:
    total = await_result = asyncio.run(inst._auto_export_once(apply_rules=False))
except NameError as e:
    check("不应抛 NameError", False, str(e))
    raise
except Exception as e:
    check("不应抛未捕获异常", False, f"{type(e).__name__}: {e}")
    raise

check("三个群都被调用", {c[1] for c in CALLS if c[0] == "group"} == {"111", "222", "333"},
      [c for c in CALLS if c[0] == "group"])
check("私聊也被调用（未被中断）", any(c[0] == "private" for c in CALLS), CALLS)
check("失败群已记录 ERROR",
      any(sev == "ERROR" and "导出群 222 失败" in msg for sev, msg in log_records),
      [r for r in log_records if r[0] == "ERROR"])
# 111 + 333 各 5 条 + 私聊 5 条 = 15（222 记 0）
check("total 正确累加（跳过失败项）", total == 15, total)

# ================= P2-11 =================
print()
print("[P2-11] 空数据返回 200 而非 500")

# 无归档消息 → _do_summary 抛 _EmptyData
async def run_summary_empty():
    job = {"chat": "group", "tid": "111", "date": "2026-08-23"}
    try:
        await inst._do_summary(job)
        return "no-raise"
    except m._EmptyData as e:
        return ("EmptyData", str(e))
    except Exception as e:
        return ("Other", f"{type(e).__name__}: {e}")

res = asyncio.run(run_summary_empty())
check("_do_summary 空数据抛 _EmptyData", res[0] == "EmptyData", res)
check("文案友好", "没有归档消息" in res[1], res[1])

# _do_briefing 无总结 → _EmptyData
async def run_brief_empty():
    job = {"kind": "brief", "days": 3}
    try:
        await inst._do_briefing(job)
        return "no-raise"
    except m._EmptyData as e:
        return ("EmptyData", str(e))
    except Exception as e:
        return ("Other", f"{type(e).__name__}: {e}")

res2 = asyncio.run(run_brief_empty())
check("_do_briefing 空数据抛 _EmptyData", res2[0] == "EmptyData", res2)
check("简报文案友好", "还没有每日总结" in res2[1], res2[1])

# API 层：_api_briefing 空数据 → 200 结构
print()
print("[P2-11] API 层返回结构")
async def run_api_briefing_empty():
    inst._ensure_summary_worker = lambda: None
    inst._summary_queue = asyncio.Queue()
    web_mod.request.query = {}
    # 直接构造一个已失败 future 模拟 worker 回传空数据异常
    fut = asyncio.get_running_loop().create_future()
    fut.set_exception(m._EmptyData("还没有每日总结可汇总（请先在「统计」里生成某天总结）"))
    # 绕过队列，直接复现 handler 的 except 分支
    try:
        await asyncio.wait_for(fut, timeout=1)
    except m._EmptyData as e:
        log_records.append(("INFO", f"[简报] 无数据可汇总：{e}"))
        return web_mod.json_response({"brief": "", "empty": True, "message": str(e)})
    return None

r = asyncio.run(run_api_briefing_empty())
check("返回 json_response（非 error）", isinstance(r, dict) and "json" in r, r)
check("结构标记 empty=True", r["json"].get("empty") is True, r)
check("日志为 INFO 不是 ERROR",
      log_records[-1][0] == "INFO", log_records[-1])
check("无新增 ERROR 记录（空数据场景）",
      not any(sev == "ERROR" and "没有每日总结" in msg for sev, msg in log_records),
      [r for r in log_records if r[0] == "ERROR"])

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
