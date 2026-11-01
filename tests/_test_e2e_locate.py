"""端到端定位链路模拟：复刻前端 gotoLocated/highlightAt 的算法，
用后端真实返回数据验证「点击引用 → 精确高亮目标」在分页场景下成立。

前端逻辑（app.js）：
  state.chat   = 从末尾向前的窗口（loadMessages(reset) 取尾部 pageSize 条）
  state.offset = 已向前加载的条数
  gotoLocated(loc):
      wantOffset = loc.offset_from_end
      while hasMore and state.offset <= wantOffset: loadOlder()
      highlightAt(wantOffset)  → domIdx = len(state.chat)-1-wantOffset
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

DATE = "2026-08-23"
TOTAL = 300          # 单日 300 条，远超页大小，覆盖「深分页」场景
PAGE = inst.config.get("ui_messages_page_size", 50) or 50

rows = []
for i in range(TOTAL):
    rec = {"t": f"{DATE} 10:{i // 60:02d}:{i % 60:02d}", "chat": "group",
           "group_id": "111", "user_id": f"u{i % 3}", "nickname": f"用户{i % 3}",
           "seq": 5000 + i, "content": f"消息{i}"}
    rows.append(rec)
# 第 299 条（最后一条）引用第 0 条（最早一条）——最深定位
rows[-1]["reply"] = {"id": "5000", "message_id": "5000", "seq": "5000",
                     "qq": "u0", "nickname": "用户0",
                     "time": f"{DATE} 10:00:00", "text": "消息0"}
fp = inst.export_dir / f"napcat_111_{DATE}.jsonl"
fp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
              encoding="utf-8")

# ---------- 复刻前端状态机 ----------
class Frontend:
    def __init__(self):
        self.chat = []       # 从末尾向前的窗口
        self.offset = 0      # 已向前加载
        self.total = 0
        self.hasMore = False
        self.page_size = PAGE

    def load(self, reset=False):
        if reset:
            self.offset = 0
            self.chat = []
        web_mod.request.query = {"chat": "group", "target_id": "111",
                                 "start": DATE, "end": DATE,
                                 "limit": self.page_size,
                                 "offset": self.offset}
        d = asyncio.run(inst._api_messages())["json"]
        self.total = d["total"]
        self.hasMore = d["has_more"]
        items = d["items"]
        # 前端：state.chat = reset ? items : items.concat(state.chat)
        self.chat = items if reset else (items + self.chat)

    def load_older(self):
        prev = self.offset
        self.offset += self.page_size
        self.load(False)
        return self.offset != prev and True

    def locate(self, key, date_hint=None):
        web_mod.request.query = {"chat": "group", "target_id": "111", "id": key}
        r = asyncio.run(inst._api_locate())
        return r.get("json") or None

    def goto_located(self, loc):
        """复刻 gotoLocated()"""
        want = int(loc.get("offset_from_end") or 0)
        # 切日期（此处同一天，跳过）
        if self.offset > want or not self.chat:
            self.offset = 0
            self.chat = []
            self.load(True)
        guard = 0
        # 与 app.js 一致：用「已加载条数 <= wantOffset」判断是否还需加载
        while self.hasMore and len(self.chat) <= want and guard < 50:
            guard += 1
            before = self.offset
            self.load_older()
            if self.offset == before:
                break
        return self.highlight_index(want)

    def highlight_index(self, offset_from_end):
        """复刻 highlightAt()：返回实际命中的消息对象"""
        n = len(self.chat)
        idx = max(0, n - 1 - int(offset_from_end))
        return self.chat[idx] if 0 <= idx < n else None

print("[1] 初始窗口（尾部 50 条）")
fe = Frontend()
fe.load(True)
check("窗口 50 条", len(fe.chat) == 50, len(fe.chat))
check("窗口末条 = 消息299", fe.chat[-1]["text"] == "消息299", fe.chat[-1]["text"])
check("窗口首条 = 消息250", fe.chat[0]["text"] == "消息250", fe.chat[0]["text"])
check("hasMore=True", fe.hasMore)

print()
print("[2] 点击最后一条的引用（指向最早 消息0）——最深定位")
last = fe.chat[-1]
rep_key = last["reply"]["id"]
check("最后一条带 reply", last.get("reply") is not None, last.get("reply"))
check("reply key = 5000", rep_key == "5000", rep_key)

loc = fe.locate(rep_key)
check("locate 成功", loc is not None, loc)
check("locate 的 offset_from_end=299", loc["offset_from_end"] == 299,
      loc.get("offset_from_end"))

hit = fe.goto_located(loc)
check("定位命中对象存在", hit is not None, hit)
check("命中的是 消息0", hit["text"] == "消息0", hit.get("text") if hit else None)
check("命中 seq=5000", hit["seq"] == 5000, hit.get("seq") if hit else None)
check("窗口已加载全部 300 条", len(fe.chat) == 300, len(fe.chat))
check("窗口首条 = 消息0", fe.chat[0]["text"] == "消息0", fe.chat[0]["text"])

print()
print("[3] 中点定位（消息150，offset_from_end=149）")
fe2 = Frontend()
fe2.load(True)
loc2 = fe2.locate("5150")
check("locate 中点", loc2 is not None and loc2["offset_from_end"] == 149,
      loc2)
hit2 = fe2.goto_located(loc2)
check("命中 消息150", hit2 is not None and hit2["text"] == "消息150",
      hit2.get("text") if hit2 else None)
check("窗口深度足够（>=150 条）", len(fe2.chat) >= 150, len(fe2.chat))

print()
print("[4] 已在窗口内的定位（消息299，无需加载）")
fe3 = Frontend()
fe3.load(True)
loc3 = fe3.locate("5299")
check("offset_from_end=0", loc3["offset_from_end"] == 0, loc3)
hit3 = fe3.goto_located(loc3)
check("命中 消息299", hit3 is not None and hit3["text"] == "消息299",
      hit3.get("text") if hit3 else None)
check("未额外加载（仍 50 条）", len(fe3.chat) == 50, len(fe3.chat))

print()
print("[5] 对照：旧实现（把 seq 当索引）为何失败")
seq_val = 5150
check("seq=5150 远超 300 条", seq_val >= TOTAL, f"seq={seq_val} total={TOTAL}")
check("旧 formula nodes[total-1-seq] 会越界/取错",
      seq_val > TOTAL - 1, "旧实现在 >页大小 时必然错位")

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
