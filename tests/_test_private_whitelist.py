"""私聊归档白名单（v2.4.0）回归测试。

背景（现网实测）：移除归档目标 2490530663 后，文件已删干净，
但它仍在 SnowLuma 好友列表里，定时轮每轮 `get_friend_msg_history` 都空返回并打 WARN。
根因是私聊分支无差别遍历全部好友。

规则：
- private_whitelist 非空 → 严格只用名单内的 QQ
- 留空 → 只巡检「已登记」目标：有归档文件 / 有游标 / 有别名
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
    def register_web_api(self, *a, **k): pass
sm.Star = Star; sm.Context = Context
em = types.ModuleType("astrbot.api.event")
class F:
    def llm_tool(self, name=None): return lambda fn: fn
em.filter = F(); em.AstrMessageEvent = type("E", (), {
    "is_admin": lambda s: True,
    "get_message_obj": lambda s: types.SimpleNamespace(self_id="1")})
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

def make(**over):
    base = {
        "export_dir": "", "auto_export": False, "interval_seconds": 120,
        "count_per_batch": 50, "auto_export_friends": True, "admin_only": True,
        "whitelist": [], "private_whitelist": [], "auto_clean": False,
        "clean_days": 14, "archive_bots": [], "backend": "auto",
        "startup_verify": False, "verify_days": 3, "aliases": [],
    }
    base.update(over)
    return m.ChatHistoryArchiver(sm.Context(), Cfg(base))

# 后端返回一堆好友（多数是僵尸）
FRIENDS = [
    {"user_id": "2490530663", "nickname": "Skylar"},     # 僵尸：无文件/无游标/无别名
    {"user_id": "111", "nickname": "好友A"},             # 有归档文件
    {"user_id": "3977113961", "nickname": "好友B"},      # 有别名
    {"user_id": "222", "nickname": "好友C"},             # 有游标
    {"user_id": "999", "nickname": "好友D"},             # 完全未登记
]

print("[1] 留空 → 只巡「已登记」目标")
inst = make()
# 造登记证据
(inst.export_dir / "napcat_private_111_2026-11-01.jsonl").write_text(
    '{"seq":1}\n', encoding="utf-8")
inst.aliases.setdefault("private", {})["3977113961"] = {
    "name": "好友B", "aliases": [], "updated": ""}
inst._state.setdefault("private", {})["222"] = {"t": 1, "ids": []}

picked = inst._pick_private_targets(FRIENDS)
ids = [u for u, _ in picked]
check("包含有归档文件的 111", "111" in ids, ids)
check("包含有别名的 3977113961", "3977113961" in ids, ids)
check("包含有游标的 222", "222" in ids, ids)
check("排除僵尸 2490530663", "2490530663" not in ids, ids)
check("排除未登记的 999", "999" not in ids, ids)
check("恰好 3 个", len(ids) == 3, ids)
check("带回昵称", dict(picked).get("111") == "好友A", picked)

print()
print("[2] 填写白名单 → 严格只用名单")
inst2 = make(private_whitelist=["2490530663", "999"])
picked2 = inst2._pick_private_targets(FRIENDS)
ids2 = [u for u, _ in picked2]
check("只用名单内", set(ids2) == {"2490530663", "999"}, ids2)
check("名单内有昵称的带回昵称",
      dict(picked2).get("2490530663") == "Skylar", picked2)
check("名单外的不巡检", "111" not in ids2, ids2)
check("数量 2", len(ids2) == 2, ids2)

print()
print("[3] 白名单里含后端未返回的 QQ（仍要巡检）")
inst3 = make(private_whitelist=["888888"])
picked3 = inst3._pick_private_targets([{"user_id": "111", "nickname": "A"}])
check("不在好友列表也巡检", [u for u, _ in picked3] == ["888888"], picked3)
check("昵称为空不报错", dict(picked3)["888888"] == "")

print()
print("[4] 后端好友列表为空/None 不崩")
inst4 = make(private_whitelist=["1"])
check("friends=None 不崩", inst4._pick_private_targets(None) == [("1", "")])
# 注意：所有实例共用同一数据目录，前面已造过 111 的归档文件，
# 因此「无登记」这个前提在一个全新临时目录里验证。
fresh_root = Path(tempfile.mkdtemp())
ap.get_astrbot_plugin_data_path = lambda: str(fresh_root / "plugin_data")
inst5 = make()
check("friends=[] 且无登记 → 空", inst5._pick_private_targets([]) == [],
      inst5._pick_private_targets([]))

print()
print("[5] 端到端：定时轮不再调用僵尸账号")
calls = []
class FakeClient:
    async def call_action(self, action, **kw):
        if action == "get_group_list":
            return []
        if action == "get_friend_list":
            return FRIENDS
        return []

inst6 = make()
(inst6.export_dir / "napcat_private_111_2026-11-01.jsonl").write_text(
    '{"seq":1}\n', encoding="utf-8")

async def fake_export(chat, tid, **kw):
    calls.append(tid)
    return 0

async def fake_get_clients():
    return [("pid", "10001", FakeClient())]

inst6._get_clients = fake_get_clients
inst6._export_target = fake_export
inst6._register_name = lambda *a, **k: None
logs.clear()
total = asyncio.run(inst6._auto_export_once(apply_rules=False))
check("只调用了已登记的 111", calls == ["111"], calls)
check("未调用僵尸 2490530663", "2490530663" not in calls, calls)
check("未调用未登记 999", "999" not in calls, calls)
check("无空返回 WARN（不再刷日志）",
      not any(s == "W" and "返回空消息列表" in msg for s, msg in logs), logs[:3])
check("日志说明巡检模式",
      any(s == "I" and "仅已登记目标" in msg for s, msg in logs), logs[:4])

print()
print("[6] 白名单模式下的端到端")
calls.clear()
inst7 = make(private_whitelist=["2490530663"])
async def fake_get_clients7():
    return [("pid", "10001", FakeClient())]
inst7._get_clients = fake_get_clients7
inst7._export_target = fake_export
inst7._register_name = lambda *a, **k: None
logs.clear()
asyncio.run(inst7._auto_export_once(apply_rules=False))
check("只调用白名单内", calls == ["2490530663"], calls)
check("日志标注白名单模式",
      any(s == "I" and "白名单模式" in msg for s, msg in logs), logs[:4])

print()
print("[7] 配置项与页面字段")
check("schema 含 private_whitelist",
      "private_whitelist" in json.loads(
          (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")))
check("_PAGE_CONFIG_FIELDS 含 private_whitelist",
      any(k == "private_whitelist" for k, _ in inst._PAGE_CONFIG_FIELDS),
      [k for k, _ in inst._PAGE_CONFIG_FIELDS])
check("前端 app.js 含 private_whitelist",
      "private_whitelist" in
      (PLUGIN_DIR / "pages" / "console" / "app.js").read_text(encoding="utf-8"))

print()
print("[8] 热生效：改白名单立即影响巡检")
inst8 = make()
_c = inst8._pick_private_targets(FRIENDS)
check("初始只巡已登记", len(_c) >= 1)
inst8.config["private_whitelist"] = ["2490530663"]
inst8._apply_runtime_config()
check("改为白名单后严格限定",
      [u for u, _ in inst8._pick_private_targets(FRIENDS)] == ["2490530663"],
      inst8.private_whitelist)
inst8.config["private_whitelist"] = []
inst8._apply_runtime_config()
check("清空后回到已登记模式", inst8.private_whitelist == [])

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
