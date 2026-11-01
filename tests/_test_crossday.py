"""跨天范围下的定位语义测试。

_api_messages 的 offset_from_end 是相对「当次选中范围的全部记录」，
而 _api_locate 只针对命中的那一天文件。两者在多天范围下不一致，
前端 gotoLocated 会先切到目标当天，所以必须验证：定位后能精确命中。
"""
import sys, types, importlib, shutil, tempfile, asyncio, json
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

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

cfg = AstrBotConfig({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
})
inst = m.ChatHistoryArchiver(star_mod.Context(), cfg)

# 三天，各 40 条
DATES = ["2026-08-21", "2026-08-22", "2026-08-23"]
PER_DAY = 40
seq = 7000
for di, dt in enumerate(DATES):
    rows = []
    for i in range(PER_DAY):
        rows.append({"t": f"{dt} 10:{i // 60:02d}:{i % 60:02d}", "chat": "group",
                     "group_id": "111", "user_id": "u0", "nickname": "用户0",
                     "seq": seq, "content": f"{dt}-消息{i}"})
        seq += 1
    fp = inst.export_dir / f"napcat_111_{dt}.jsonl"
    fp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                  encoding="utf-8")

def call(fn, q):
    web_mod.request.query = q
    return asyncio.run(fn())

print("[1] 多天范围的 messages：offset_from_end 相对整个范围")
r = call(inst._api_messages, {"chat": "group", "target_id": "111",
                              "start": DATES[0], "end": DATES[-1], "limit": 30})
d = r["json"]
check("total = 120", d["total"] == 120, d["total"])
check("返回 30 条", len(d["items"]) == 30, len(d["items"]))
check("末条 offset_from_end=0", d["items"][-1]["offset_from_end"] == 0,
      d["items"][-1]["offset_from_end"])
check("末条来自最后一天", d["items"][-1]["text"].startswith(DATES[-1]),
      d["items"][-1]["text"])
check("首条 offset_from_end=29", d["items"][0]["offset_from_end"] == 29,
      d["items"][0]["offset_from_end"])

print()
print("[2] locate 的 offset_from_end 只针对当天文件")
# 定位第一天第 5 条
target_seq = 7000 + 5
loc = call(inst._api_locate, {"chat": "group", "target_id": "111",
                              "seq": str(target_seq)})["json"]
check("locate date = 第一天", loc["date"] == DATES[0], loc.get("date"))
check("index = 5", loc["index"] == 5, loc.get("index"))
check("total = 40（单日）", loc["total"] == PER_DAY, loc.get("total"))
check("offset_from_end = 34", loc["offset_from_end"] == 34,
      loc.get("offset_from_end"))

print()
print("[3] 前端流程：先切到当天，再按 offset_from_end 加载并命中")
class FE:
    def __init__(self, page=30):
        self.chat = []; self.offset = 0; self.hasMore = False
        self.page = page; self.range = ("", "")
    def load(self, reset=False, day=None):
        if reset:
            self.offset = 0; self.chat = []
        if day:
            self.range = (day, day)
        q = {"chat": "group", "target_id": "111", "limit": self.page,
             "offset": self.offset,
             "start": self.range[0], "end": self.range[1]}
        d = call(inst._api_messages, q)["json"]
        self.hasMore = d["has_more"]
        items = d["items"]
        self.chat = items if reset else (items + self.chat)
    def older(self):
        before = self.offset
        self.offset += self.page
        self.load(False)
        return self.offset != before
    def goto(self, loc):
        want = int(loc["offset_from_end"])
        if self.range != (loc["date"], loc["date"]) or not self.chat:
            self.load(True, loc["date"])
        g = 0
        while self.hasMore and len(self.chat) <= want and g < 50:
            g += 1
            if not self.older(): break
        n = len(self.chat)
        idx = n - 1 - want
        return self.chat[idx] if 0 <= idx < n else None

fe = FE()
fe.load(True, DATES[-1])   # 先看最后一天
hit = fe.goto(loc)
check("切换日期后命中", hit is not None, hit)
check("命中的是第一天第5条", hit and hit["text"] == f"{DATES[0]}-消息5",
      hit.get("text") if hit else None)
check("命中 seq 正确", hit and hit["seq"] == target_seq,
      hit.get("seq") if hit else None)
check("窗口是当天 40 条", len(fe.chat) == PER_DAY, len(fe.chat))
check("范围已切到第一天", fe.range == (DATES[0], DATES[0]), fe.range)

print()
print("[4] 命中最后一天的末条（跨天边界）")
last_seq = 7000 + PER_DAY * 3 - 1
loc2 = call(inst._api_locate, {"chat": "group", "target_id": "111",
                               "seq": str(last_seq)})["json"]
check("date=最后一天", loc2["date"] == DATES[-1], loc2.get("date"))
check("index=39", loc2["index"] == PER_DAY - 1, loc2.get("index"))
check("offset_from_end=0", loc2["offset_from_end"] == 0, loc2.get("offset_from_end"))
fe2 = FE(); fe2.load(True, DATES[0])
hit2 = fe2.goto(loc2)
check("命中最后一天末条", hit2 and hit2["text"] == f"{DATES[-1]}-消息39",
      hit2.get("text") if hit2 else None)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
