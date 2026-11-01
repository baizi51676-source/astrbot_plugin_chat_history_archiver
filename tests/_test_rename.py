"""验证类改名与旧名别名不会破坏 star_map 注册。"""
import sys, types, importlib, shutil, tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
NAME = "astrbot_plugin_chat_history_archiver"

api = types.ModuleType("astrbot.api")
api.logger = types.SimpleNamespace(__getattr__=lambda n: (lambda *a, **k: None))

star_mod = types.ModuleType("astrbot.api.star")
star_map = {}

class Star:
    def __init__(self, context, config=None):
        self.context = context
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        star_map[cls.__module__] = cls

class Context:
    pass

star_mod.Star = Star
star_mod.Context = Context

event_mod = types.ModuleType("astrbot.api.event")
class _F:
    def llm_tool(self, name=None):
        return lambda fn: fn
event_mod.filter = _F()
event_mod.AstrMessageEvent = type("E", (), {})

web_mod = types.ModuleType("astrbot.api.web")
web_mod.request = types.SimpleNamespace(query={})
web_mod.json_response = lambda d: d
web_mod.error_response = lambda m, status_code=400: {"e": m}
web_mod.file_response = None

class AstrBotConfig(dict):
    def save_config(self): pass
    async def save_config_async(self): pass

api.AstrBotConfig = AstrBotConfig
api.star = star_mod
api.event = event_mod
api.web = web_mod

core = types.ModuleType("astrbot.core")
utils = types.ModuleType("astrbot.core.utils")
ap = types.ModuleType("astrbot.core.utils.astrbot_path")
root = Path(tempfile.mkdtemp())
ap.get_astrbot_plugin_data_path = lambda: str(root / "plugin_data")
ap.get_astrbot_data_path = lambda: str(root)
utils.astrbot_path = ap
core.utils = utils
astrbot = types.ModuleType("astrbot")
astrbot.api = api
astrbot.core = core

sys.modules.update({
    "astrbot": astrbot, "astrbot.api": api, "astrbot.api.star": star_mod,
    "astrbot.api.event": event_mod, "astrbot.api.web": web_mod,
    "astrbot.core": core, "astrbot.core.utils": utils,
    "astrbot.core.utils.astrbot_path": ap,
})

pkg = Path(tempfile.mkdtemp())
pd = pkg / "data" / "plugins" / NAME
pd.mkdir(parents=True)
for it in PLUGIN_DIR.iterdir():
    if it.is_dir() and it.name in ("core", "pages"):
        shutil.copytree(it, pd / it.name)
    elif it.is_file() and it.suffix in (".py", ".json", ".yaml"):
        shutil.copy2(it, pd / it.name)
sys.path.insert(0, str(pkg))

m = importlib.import_module(f"data.plugins.{NAME}.main")
expected = f"data.plugins.{NAME}.main"

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond:
        fails.append(name)

check("star_map 只有 1 个条目", len(star_map) == 1, list(star_map.keys()))
check("键是插件 main 模块", expected in star_map, list(star_map.keys()))
check("映射到 ChatHistoryArchiver", star_map[expected].__name__ == "ChatHistoryArchiver",
      star_map[expected].__name__)
check("旧名别名仍可用", hasattr(m, "NapcatHistoryExporter"))
check("旧名与新名同一对象", m.NapcatHistoryExporter is m.ChatHistoryArchiver)
check("类 __module__ 正确", m.ChatHistoryArchiver.__module__ == expected,
      m.ChatHistoryArchiver.__module__)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
