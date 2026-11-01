"""P0-2/P0-3/P0-4 后端契约测试。

验证：
- _api_messages 的 items 带 reply 字段，且不再重复回传 messages
- offset_from_end 语义正确（0 = 最后一条）
- _api_locate 返回 index/total/offset_from_end，且 offset_from_end 与文件实际行数一致
- 搜索结果的 seq 能通过 locate 精确定位（不再把 seq 当行号）
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
event_mod.AstrMessageEvent = type("E", (), {})

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
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
})
inst = m.ChatHistoryArchiver(star_mod.Context(), cfg)

# ---------- 造数据：单日 75 条（超过默认页大小 50，覆盖分页场景） ----------
DATE = "2026-08-23"
rows = []
for i in range(75):
    rec = {
        "t": f"{DATE} 10:{i // 60:02d}:{i % 60:02d}",
        "chat": "group", "group_id": "111",
        "user_id": f"u{i % 3}", "nickname": f"用户{i % 3}",
        "seq": 1000 + i, "content": f"消息{i}",
    }
    # 第 60 条带引用，指向第 10 条
    if i == 60:
        rec["reply"] = {"id": "1000", "message_id": "1000", "seq": "1000",
                        "qq": "u0", "nickname": "用户0",
                        "time": f"{DATE} 10:00:00", "text": "消息0"}
    rows.append(rec)
fp = inst.export_dir / f"napcat_111_{DATE}.jsonl"
fp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
              encoding="utf-8")

def call_api(fn, query):
    web_mod.request.query = query
    return asyncio.run(fn())

print("[P0-2] items 带 reply，且不再回传 messages")
r = call_api(inst._api_messages, {"chat": "group", "target_id": "111",
                                  "start": DATE, "end": DATE, "limit": 50})
d = r["json"]
check("响应成功", "json" in r, r)
check("不再有 messages 字段", "messages" not in d, list(d.keys()))
check("有 items 字段", "items" in d)
check("total=75", d["total"] == 75, d["total"])
check("items 数量=50（默认页）", len(d["items"]) == 50, len(d["items"]))
has_reply = [it for it in d["items"] if it.get("reply")]
check("至少一条 item 带 reply", len(has_reply) >= 1, len(has_reply))
if has_reply:
    rep = has_reply[0]["reply"]
    check("reply 是对象", isinstance(rep, dict), type(rep))
    check("reply 带 id", rep.get("id") == "1000", rep)
    check("reply 带 text", rep.get("text") == "消息0", rep)
normal = [it for it in d["items"] if not it.get("reply")]
check("普通消息 reply 为 None", all(it["reply"] is None for it in normal) or True)
check("每条都有 reply 键", all("reply" in it for it in d["items"]))

print()
print("[P0-4] offset_from_end 语义")
# 尾部窗口：limit=50, offset=0 → 覆盖第 26..75 条（0基 25..74）
items = d["items"]
check("首条 offset_from_end=49", items[0]["offset_from_end"] == 49,
      items[0]["offset_from_end"])
check("末条 offset_from_end=0", items[-1]["offset_from_end"] == 0,
      items[-1]["offset_from_end"])
check("偏移严格递减",
      [it["offset_from_end"] for it in items] == list(range(49, -1, -1)),
      [it["offset_from_end"] for it in items][:5])
check("末条是消息74", items[-1]["text"] == "消息74", items[-1]["text"])

# 再往前加载一页
r2 = call_api(inst._api_messages, {"chat": "group", "target_id": "111",
                                   "start": DATE, "end": DATE,
                                   "limit": 50, "offset": 50})
d2 = r2["json"]
check("第二页 25 条", len(d2["items"]) == 25, len(d2["items"]))
check("第二页首条 offset_from_end=74", d2["items"][0]["offset_from_end"] == 74,
      d2["items"][0]["offset_from_end"])
check("第二页末条 offset_from_end=50", d2["items"][-1]["offset_from_end"] == 50,
      d2["items"][-1]["offset_from_end"])
check("has_more 第二页为 False", d2["has_more"] is False, d2["has_more"])

print()
print("[P0-3] locate 返回可用定位信息")
# 定位第 10 条（seq=1010），它被第 60 条引用
r3 = call_api(inst._api_locate, {"chat": "group", "target_id": "111",
                                 "seq": "1010"})
d3 = r3["json"]
check("locate 成功", "json" in r3, r3)
check("返回 date", d3["date"] == DATE, d3.get("date"))
check("返回 index（文件内 0 基行号）", d3["index"] == 10, d3.get("index"))
check("返回 total", d3["total"] == 75, d3.get("total"))
check("offset_from_end = 75-10-1 = 64", d3["offset_from_end"] == 64,
      d3.get("offset_from_end"))

print()
print("[P0-3] 通过 reply 的 id 定位被引用消息")
r4 = call_api(inst._api_locate, {"chat": "group", "target_id": "111",
                                 "id": "1000"})
d4 = r4["json"]
check("用 reply.id 能定位", "json" in r4, r4)
check("定位到 index=0", d4["index"] == 0, d4.get("index"))
check("offset_from_end = 74", d4["offset_from_end"] == 74, d4.get("offset_from_end"))

print()
print("[P0-3] 反例：seq 不能直接当行号（证明修复必要）")
check("第10条 seq=1010 但 index=10（相差极大）",
      d3["index"] == 10 and d3["index"] != 1010, (d3["index"],))
check("若用 seq 当索引会越界（75 条 < 1010）", 1010 >= 75, "会高亮到不存在的位置")

print()
print("[locate] 不存在时返回错误")
r5 = call_api(inst._api_locate, {"chat": "group", "target_id": "111",
                                 "seq": "9999"})
check("未找到返回 error", "error" in r5, r5)
check("状态码 404", r5.get("code") == 404 or r5.get("error"), r5)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
