"""P2-4 / P2-5 回归测试。

P2-4：migrate_dir 遇到同名目录时递归合并（不再整项跳过，避免半套数据）。
P2-5：_bot_qq 缓存键在平台实例 id 缺失时不会串号。
"""
import sys, types, importlib, shutil, tempfile, asyncio
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
paths = importlib.import_module(f"data.plugins.{NAME}.core.paths")

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

print("[P2-4] migrate_dir 递归合并同名子目录")
work = Path(tempfile.mkdtemp())
src = work / "old"
dst = work / "new"
(src / "avatars").mkdir(parents=True)
(dst / "avatars").mkdir(parents=True)
(src / "avatars" / "1.png").write_bytes(b"a")
(src / "avatars" / "2.png").write_bytes(b"b")
(dst / "avatars" / "3.png").write_bytes(b"c")
(src / "summaries").mkdir()
(src / "summaries" / "x.md").write_text("t", encoding="utf-8")
(src / "state.json").write_text("{}", encoding="utf-8")

msg = paths.migrate_dir(src, dst)
new_av = sorted(p.name for p in (dst / "avatars").iterdir())
check("avatars 已合并（3 个）", len(new_av) == 3, new_av)
check("旧 avatars 文件已迁入", {"1.png", "2.png"} <= set(new_av), new_av)
check("目标原有文件保留", "3.png" in new_av, new_av)
check("summaries 已迁入", (dst / "summaries" / "x.md").exists())
check("顶层 state.json 已迁入", (dst / "state.json").exists())
check("旧目录已清理", not src.exists(), src)
check("返回文案含移动项数", "移动" in msg, msg)

# 同名文件冲突：应保留目标内容、不覆盖
work2 = Path(tempfile.mkdtemp())
s2, d2 = work2 / "s", work2 / "d"
s2.mkdir(); d2.mkdir()
(s2 / "a.json").write_text("OLD", encoding="utf-8")
(d2 / "a.json").write_text("NEW", encoding="utf-8")
msg2 = paths.migrate_dir(s2, d2)
check("同名文件不被覆盖", (d2 / "a.json").read_text(encoding="utf-8") == "NEW")
check("冲突项计入跳过", "跳过" in msg2, msg2)

# 空目录迁移返回空串
work3 = Path(tempfile.mkdtemp())
s3, d3 = work3 / "e1", work3 / "e2"
s3.mkdir(); d3.mkdir()
check("空目录返回空串", paths.migrate_dir(s3, d3) == "")
check("src=None 返回空串", paths.migrate_dir(None, d3) == "")
check("src 不存在返回空串", paths.migrate_dir(work3 / "nope", d3) == "")

print()
print("[P2-5] _bot_qq 缓存键不串号")
cfg = AstrBotConfig({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
})
inst = m.ChatHistoryArchiver(star_mod.Context(), cfg)

class FakeClient:
    def __init__(self, qq): self.qq = qq
    async def call_action(self, action, **kw):
        if action == "get_login_info":
            return {"user_id": self.qq}
        return []

async def run_cache_key_test():
    """两个实例 id 都为空 → 必须用不同缓存键，QQ 不能互相覆盖。"""
    c1, c2 = FakeClient("11111"), FakeClient("22222")
    inst._qq_cache = {}
    inst._qq_cache_ts = 0
    q1 = await inst._bot_qq(c1, "", cache_key="idx:0")
    q2 = await inst._bot_qq(c2, "", cache_key="idx:1")
    return q1, q2, dict(inst._qq_cache)

q1, q2, cache = asyncio.run(run_cache_key_test())
check("两实例 QQ 各自正确", (q1, q2) == ("11111", "22222"), (q1, q2))
check("缓存有两个独立键", len(cache) == 2, cache)
check("idx:0 -> 11111", cache.get("idx:0") == "11111", cache)
check("idx:1 -> 22222", cache.get("idx:1") == "22222", cache)

# 对照组：若仍用空串 pid 作为唯一键，会串号
async def run_old_behavior():
    c1, c2 = FakeClient("11111"), FakeClient("22222")
    inst._qq_cache = {}
    inst._qq_cache_ts = 0
    a = await inst._bot_qq(c1, "", cache_key="")
    b = await inst._bot_qq(c2, "", cache_key="")
    return a, b, dict(inst._qq_cache)

a, b, cache_old = asyncio.run(run_old_behavior())
check("（对照）空键会串号：第二个覆盖第一个并命中缓存",
      a == "11111" and b == "11111", (a, b))
check("（对照）缓存只剩 1 个键", len(cache_old) == 1, cache_old)

print()
print("[P2-5] _get_clients 在 pid 缺失时给出不同键")
class Meta:
    def __init__(self, pid): self.id = pid
class Platform:
    def __init__(self, pid, qq):
        self._meta = Meta(pid); self._client = FakeClient(qq)
    def meta(self): return self._meta
    def get_client(self): return self._client

async def run_get_clients():
    inst2 = m.ChatHistoryArchiver(star_mod.Context(), cfg)
    inst2._aiocqhttp_platforms = lambda: [Platform("", "11111"),
                                          Platform("", "22222"),
                                          Platform("pid3", "33333")]
    inst2._qq_cache = {}
    inst2._qq_cache_ts = 0
    return await inst2._get_clients()

clients = asyncio.run(run_get_clients())
qqs = [c[1] for c in clients]
check("三个实例都被识别", len(clients) == 3, len(clients))
check("QQ 各不相同（无串号）", qqs == ["11111", "22222", "33333"], qqs)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
