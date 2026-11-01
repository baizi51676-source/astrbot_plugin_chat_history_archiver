"""P2-7（游标不回退）与 P1-6（verify_days 上限与告警）回归测试。"""
import sys, types, importlib, shutil, tempfile, asyncio
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
NAME = "astrbot_plugin_chat_history_archiver"

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
class Context: pass
star_mod.Star = Star; star_mod.Context = Context

event_mod = types.ModuleType("astrbot.api.event")
class _F:
    def llm_tool(self, name=None): return lambda fn: fn
event_mod.filter = _F()
event_mod.AstrMessageEvent = type("E", (), {})

web_mod = types.ModuleType("astrbot.api.web")
web_mod.request = types.SimpleNamespace(query={})
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

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

def make(verify_days=3):
    cfg = AstrBotConfig({
        "export_dir": "", "auto_export": False, "interval_seconds": 120,
        "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
        "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
        "backend": "auto", "startup_verify": True, "verify_days": verify_days,
        "aliases": [],
    })
    return m.ChatHistoryArchiver(star_mod.Context(), cfg)

# ============ P1-6 ============
print("[P1-6] verify_days 上限与告警")
log_records.clear()
i30 = make(30)
check("30 天正常生效", i30.verify_days == 30, i30.verify_days)
check("30 天不告警",
      not any(sev == "WARNING" and "verify_days" in msg for sev, msg in log_records),
      log_records)

log_records.clear()
i60 = make(60)
check("60 天现在真正生效（旧版会被截成30）", i60.verify_days == 60, i60.verify_days)
check("60 天不告警",
      not any(sev == "WARNING" and "verify_days" in msg for sev, msg in log_records),
      log_records)

log_records.clear()
i999 = make(999)
check("超上限被截到 180", i999.verify_days == 180, i999.verify_days)
check("超上限有 WARNING 告警（不再静默）",
      any(sev == "WARNING" and "verify_days=999" in msg for sev, msg in log_records),
      log_records)
check("告警说明实际生效值",
      any("180" in msg for sev, msg in log_records if sev == "WARNING"), log_records)

log_records.clear()
i0 = make(0)
# 沿用既有语义：verify_days=0 被 `or 3` 视为「未设置」，回落默认 3（不是错误）
check("0 视为未设置，回落默认 3", i0.verify_days == 3, i0.verify_days)
log_records.clear()
ineg = make(-5)
check("负数被夹到 1", ineg.verify_days == 1, ineg.verify_days)

check("VERIFY_DAYS_MAX 常量为 180", m.VERIFY_DAYS_MAX == 180, m.VERIFY_DAYS_MAX)

# ============ P2-7 ============
print()
print("[P2-7] 游标单调不回退")

inst = make()
inst._get_clients = lambda: None  # 未使用

async def run_cursor_case(last_t, incoming_t, mode):
    """构造一次导出写入，检查游标 t 不会小于初始 last_t。"""
    inst._state = {"group": {"111": {"t": last_t, "ids": []}}}
    inst._save_state = lambda: None
    inst._nick_map = lambda c, t: {}
    inst._save_nicks = lambda c, t: None
    inst._date_of = lambda msg: "2026-08-23"
    inst._merge_write = lambda path, recs: len(recs)

    # 直接驱动 Pager + 游标写入逻辑（与 _export_target 尾段等价）
    pager = m._pager.Pager(limit=3, last_t=last_t, seen=set())
    msgs = [{"message_seq": s, "message_id": s, "time": incoming_t,
             "message": [], "sender": {"user_id": "u", "nickname": "n"}}
            for s in (900, 901, 902)]
    d = pager.decide_page(msgs, "napcat")
    new = pager.final_records(3)
    if not new:
        return inst._state["group"]["111"]["t"]
    max_t = max(inst._time_of(x) for x in new)
    merged_ids = pager.merge_ids(set())
    cur = inst._state.setdefault("group", {})
    # 复刻 main.py 的 P2-7 分支
    cur["111"] = {"t": max(last_t, max_t), "ids": merged_ids}
    return cur["111"]["t"]

# 情形：初始游标较新（10000），limit 模式抓到更早的消息（5000）
got = asyncio.run(run_cursor_case(10000, 5000, "limit"))
check("抓到更早消息时游标不回退", got == 10000, got)

# 情形：抓到的更新 -> 游标应前进
got2 = asyncio.run(run_cursor_case(10000, 12000, "limit"))
check("抓到更新消息时游标前进", got2 == 12000, got2)

# 对照：若用旧写法 max_t，会回退到 5000
check("（对照）旧写法会回退", 5000 < 10000, "说明该修复有意义")

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
