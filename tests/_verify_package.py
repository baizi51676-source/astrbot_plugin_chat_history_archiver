"""发布包加载验证：把 zip 解压成 AstrBot 的插件目录结构，用真实加载方式导入。

这是打包前的最后一道闸——验证产物本身（而不是源码目录）能正常工作。
"""
import sys, types, zipfile, tempfile, shutil, importlib
from pathlib import Path

ZIP = Path(r"D:\astrbot\astrbot_plugin_chat_history_archiver\dist"
           r"\astrbot_plugin_chat_history_archiver-2.4.0.zip")
NAME = "astrbot_plugin_chat_history_archiver"

root = Path(tempfile.mkdtemp())
dest = root / "data" / "plugins" / NAME
dest.parent.mkdir(parents=True)
zipfile.ZipFile(ZIP).extractall(dest)

api = types.ModuleType("astrbot.api")
class _L:
    def __getattr__(self, n): return lambda *a, **k: None
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
    def register_web_api(self, route, handler, methods, desc):
        Context.registered_web_apis.append((route, handler, methods, desc))
sm.Star = Star; sm.Context = Context
em = types.ModuleType("astrbot.api.event")
tools = []
class F:
    def llm_tool(self, name=None):
        def deco(fn):
            tools.append((name or fn.__name__, fn, fn.__module__))
            return fn
        return deco
em.filter = F(); em.AstrMessageEvent = type("E", (), {
    "is_admin": lambda s: True,
    "get_message_obj": lambda s: types.SimpleNamespace(self_id="1")})
wm = types.ModuleType("astrbot.api.web")
class R:
    query = {}
    async def json(self, default=None): return default or {}
wm.request = R(); wm.json_response = lambda d: {"json": d}
wm.error_response = lambda m, status_code=400: {"error": m, "code": status_code}
wm.file_response = lambda *a, **k: {}
class Cfg(dict):
    def save_config(self): pass
    async def save_config_async(self): pass
api.AstrBotConfig = Cfg
api.star = sm; api.event = em; api.web = wm
core = types.ModuleType("astrbot.core"); utils = types.ModuleType("astrbot.core.utils")
ap = types.ModuleType("astrbot.core.utils.astrbot_path")
ap.get_astrbot_plugin_data_path = lambda: str(root / "plugin_data")
ap.get_astrbot_data_path = lambda: str(root)
utils.astrbot_path = ap; core.utils = utils
at = types.ModuleType("astrbot"); at.api = api; at.core = core
sys.modules.update({"astrbot": at, "astrbot.api": api, "astrbot.api.star": sm,
                    "astrbot.api.event": em, "astrbot.api.web": wm,
                    "astrbot.core": core, "astrbot.core.utils": utils,
                    "astrbot.core.utils.astrbot_path": ap})

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

print("[1] 解压后的目录结构")
check("main.py 在顶层", (dest / "main.py").exists())
check("metadata.yaml 在顶层", (dest / "metadata.yaml").exists())
check("_conf_schema.json 在顶层", (dest / "_conf_schema.json").exists())
n_core = len(list((dest / "core").glob("*.py")))
check("core/ 13 个 py", n_core == 13, n_core)
check("pages/console 4 个文件",
      all((dest / "pages" / "console" / f).exists()
          for f in ("app.js", "style.css", "index.html", "bg.jpg")))
check("无 tests 目录", not (dest / "tests").exists())
check("无 .git", not (dest / ".git").exists())

print()
print("[2] 以 AstrBot 方式导入")
sys.path.insert(0, str(root))
EXPECTED = f"data.plugins.{NAME}.main"
try:
    mod = importlib.import_module(EXPECTED)
    check("导入成功", True)
except Exception as e:
    check("导入成功", False, f"{type(e).__name__}: {e}")
    raise

check("star_map 已登记", EXPECTED in smap, list(smap.keys()))
check("类名 ChatHistoryArchiver",
      smap[EXPECTED].__name__ == "ChatHistoryArchiver",
      smap[EXPECTED].__name__)
check("旧名别名可用", hasattr(mod, "NapcatHistoryExporter"))
check("版本读到 2.4.0", mod.PLUGIN_VERSION == "2.4.0", mod.PLUGIN_VERSION)
check("VERIFY_DAYS_MAX=180", mod.VERIFY_DAYS_MAX == 180)

print()
print("[3] LLM 工具（应为 9 个）")
check("工具数量 9", len(tools) == 9, len(tools))
names = sorted(t[0] for t in tools)
expect = {"export_group_history", "export_private_history",
          "export_all_incremental", "get_export_status",
          "get_group_message_history", "search_archived_messages",
          "list_archived_groups", "alias_manage", "remove_archived_target"}
check("工具名齐全", set(names) == expect, names)
check("工具均归属插件 main", all(t[2] == EXPECTED for t in tools))

print()
print("[4] 实例化与数据目录")
cfg = Cfg({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
})
inst = mod.ChatHistoryArchiver(sm.Context(), cfg)
check("实例化成功", True)
check("数据目录在 plugin_data",
      str(inst.export_dir).replace("\\", "/").endswith(f"plugin_data/{NAME}"),
      inst.export_dir)
check("data 目录已创建", inst.export_dir.exists())

print()
print("[5] Web API 注册")
inst._register_web_apis()
routes = [r[0] for r in Context.registered_web_apis]
check("路由已注册", len(routes) > 0, len(routes))
check("含 console/state", any(r.endswith("console/state") for r in routes))
check("含 console/remove_target",
      any(r.endswith("console/remove_target") for r in routes))
print(f"  （共注册 {len(routes)} 条路由）")

print()
print("[6] 核心模块可用")
import asyncio, json
p = inst.export_dir / "napcat_111_2026-11-01.jsonl"
n = inst._merge_write(p, [{"t": "2026-11-01 10:00:00", "seq": 1,
                           "user_id": "u", "content": "hi"}])
check("merge_write 写入 1", n == 1, n)
check("文件已创建", p.exists())
check("锁文件已生成", (Path(str(p) + ".lock")).exists(),
      str(p) + ".lock")
from importlib import import_module
amo = import_module(f"data.plugins.{NAME}.core.atomic")
flk = import_module(f"data.plugins.{NAME}.core.filelock")
ver = import_module(f"data.plugins.{NAME}.core.verify")
tgt = import_module(f"data.plugins.{NAME}.core.targets")
sched = import_module(f"data.plugins.{NAME}.core.schedule")
check("core.atomic 可用", hasattr(amo, "atomic_write_text"))
check("core.filelock 可用", hasattr(flk, "locked"))
check("core.verify 可用", hasattr(ver, "plan_items"))
check("core.targets 可用", hasattr(tgt, "related_files"))
check("core.schedule 可用", hasattr(sched, "should_run"))

print()
print("FAILS:", fails if fails else "none")
shutil.rmtree(root, ignore_errors=True)
sys.exit(1 if fails else 0)
