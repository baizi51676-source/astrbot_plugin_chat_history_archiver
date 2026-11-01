"""真实导入测试：用仿真 AstrBot 包，验证 main.py 能被 __import__ 加载，
插件类能实例化，LLM 工具能注册，Web API 能注册。

这比 AST 静态比对更严格——它能抓到相对导入、循环引用、运行期名字错误。
"""
import sys
import types
import asyncio
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "astrbot_plugin_chat_history_archiver"

# ---------- 构造 astrbot 仿真包 ----------
def build_fake_astrbot():
    # astrbot.api.logger
    import logging
    api = types.ModuleType("astrbot.api")
    class _Logger:
        def __getattr__(self, name):
            return lambda *a, **k: None
    api.logger = _Logger()

    # astrbot.api.star
    star_mod = types.ModuleType("astrbot.api.star")
    star_map = {}
    star_registry = []

    class Star:
        def __init__(self, context, config=None):
            self.context = context
        def __init_subclass__(cls, **kw):
            super().__init_subclass__(**kw)
            if not star_map.get(cls.__module__):
                star_map[cls.__module__] = cls
                star_registry.append(cls)
            else:
                star_map[cls.__module__] = cls
        async def initialize(self): pass
        async def terminate(self): pass

    class Context:
        registered_web_apis = []
        def __init__(self):
            self.platform_manager = types.SimpleNamespace(platform_insts=[])
        def register_web_api(self, route, handler, methods, desc):
            Context.registered_web_apis.append((route, handler, methods, desc))

    star_mod.Star = Star
    star_mod.Context = Context
    star_mod.star_map = star_map
    star_mod.star_registry = star_registry

    # astrbot.api.event : filter + AstrMessageEvent
    event_mod = types.ModuleType("astrbot.api.event")
    llm_tools = []
    class _Filter:
        def llm_tool(self, name=None):
            def deco(fn):
                llm_tools.append((name or fn.__name__, fn, fn.__module__))
                return fn
            return deco
    class AstrMessageEvent:
        def is_admin(self): return True
        def get_message_obj(self): return types.SimpleNamespace(self_id="123456")
    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = AstrMessageEvent

    # astrbot.api.web
    web_mod = types.ModuleType("astrbot.api.web")
    class _Req:
        query = {}
        async def json(self, default=None): return default or {}
    web_mod.request = _Req()
    web_mod.json_response = lambda data: {"json": data}
    web_mod.error_response = lambda msg, status_code=400: {"error": msg, "code": status_code}
    web_mod.file_response = lambda *a, **k: {"file": a}

    # astrbot.api 挂载
    api.star = star_mod
    api.event = event_mod
    api.web = web_mod

    # AstrBotConfig 类型
    class AstrBotConfig(dict):
        def save_config(self): pass
        async def save_config_async(self): pass
    api.AstrBotConfig = AstrBotConfig

    class _GetAstrBotPluginDataPath:
        pass

    # astrbot.core.utils.astrbot_path
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    apath = types.ModuleType("astrbot.core.utils.astrbot_path")
    tmp_root = Path(tempfile.mkdtemp())
    apath.get_astrbot_plugin_data_path = lambda: str(tmp_root / "plugin_data")
    apath.get_astrbot_data_path = lambda: str(tmp_root)
    utils.astrbot_path = apath
    core.utils = utils

    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    astrbot.core = core

    mods = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star_mod,
        "astrbot.api.event": event_mod,
        "astrbot.api.web": web_mod,
        "astrbot.core": core,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": apath,
    }
    sys.modules.update(mods)
    return star_mod, llm_tools, AstrBotConfig, tmp_root

star_mod, llm_tools, AstrBotConfig, tmp_root = build_fake_astrbot()

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond:
        fails.append(name)

# ---------- 模拟 AstrBot 的加载方式 ----------
print("[1] 以 AstrBot 的方式导入插件")
# AstrBot 实际用 __import__("data.plugins.<目录名>.main", fromlist=["main"])，
# 即插件是 data.plugins.<name> 命名空间包下的 main 子模块——相对导入依赖这一点。
import importlib
import shutil

# 搭一个与线上同结构的临时包：<tmp>/data/plugins/<name>/...
pkg_root = Path(tempfile.mkdtemp())
plugins_dir = pkg_root / "data" / "plugins" / PLUGIN_NAME
plugins_dir.mkdir(parents=True)
for item in PLUGIN_DIR.iterdir():
    if item.is_dir() and item.name in ("core", "pages"):
        shutil.copytree(item, plugins_dir / item.name)
    elif item.is_file() and item.suffix in (".py", ".json", ".yaml"):
        shutil.copy2(item, plugins_dir / item.name)
# data/ 与 data/plugins/ 是命名空间包（无 __init__.py，与线上一致）

sys.path.insert(0, str(pkg_root))
try:
    mod = importlib.import_module(f"data.plugins.{PLUGIN_NAME}.main")
    check("以 data.plugins.<name>.main 导入成功", True)
except Exception as e:
    check("以 data.plugins.<name>.main 导入成功", False, f"{type(e).__name__}: {e}")
    raise

EXPECTED_MODULE = f"data.plugins.{PLUGIN_NAME}.main"
check("PLUGIN_NAME 常量", getattr(mod, "PLUGIN_NAME", "") == PLUGIN_NAME)
check("PLUGIN_VERSION 常量", getattr(mod, "PLUGIN_VERSION", "") == "2.4.0")
check("插件类存在", hasattr(mod, "NapcatHistoryExporter"))
check("插件类 __module__ 正确", mod.NapcatHistoryExporter.__module__ == EXPECTED_MODULE,
      mod.NapcatHistoryExporter.__module__)
check("已注册进 star_map", EXPECTED_MODULE in star_mod.star_map,
      list(star_mod.star_map))
check("core 子模块已作为插件子包加载",
      f"{EXPECTED_MODULE.rsplit('.', 1)[0]}.core.pager" in sys.modules,
      [m for m in sys.modules if ".core." in m])

print("[2] LLM 工具注册")
EXPECTED_TOOLS = {"export_group_history", "export_private_history",
                  "export_all_incremental", "get_export_status",
                  "get_group_message_history", "search_archived_messages",
                  "list_archived_groups", "alias_manage",
                  "remove_archived_target"}   # P2-10 新增
check(f"LLM 工具数量={len(EXPECTED_TOOLS)}", len(llm_tools) == len(EXPECTED_TOOLS),
      len(llm_tools))
tool_names = sorted(t[0] for t in llm_tools)
check("工具名齐全", set(tool_names) == EXPECTED_TOOLS, tool_names)
check("所有工具 __module__ 属插件 main",
      all(t[2] == EXPECTED_MODULE for t in llm_tools), [t[2] for t in llm_tools])

print("[3] 实例化（数据目录解析/迁移路径）")
config = AstrBotConfig({
    "export_dir": "",
    "auto_export": False,
    "interval_seconds": 120,
    "count_per_batch": 50,
    "auto_export_friends": False,
    "admin_only": True,
    "whitelist": [],
    "auto_clean": False,
    "clean_days": 14,
    "archive_bots": [],
    "backend": "auto",
    "startup_verify": False,
    "verify_days": 3,
    "aliases": [],
})
ctx = star_mod.Context()
try:
    inst = mod.NapcatHistoryExporter(ctx, config)
    check("实例化成功", True)
except Exception as e:
    check("实例化成功", False, f"{type(e).__name__}: {e}")
    raise

expected = str(tmp_root / "plugin_data" / PLUGIN_NAME)
check("export_dir 落在 plugin_data",
      str(inst.export_dir).replace("\\", "/").endswith(
          f"plugin_data/{PLUGIN_NAME}"), inst.export_dir)
check("export_dir 已创建", inst.export_dir.exists())
check("verify_days 截断在 1..30", 1 <= inst.verify_days <= 30, inst.verify_days)
check("interval 最小 30", inst.interval >= 30, inst.interval)
check("batch 在 1..200", 1 <= inst.batch <= 200, inst.batch)
check("aliases 结构正确", set(inst.aliases) == {"group", "private"}, inst.aliases)
check("有 _task 字段", hasattr(inst, "_task"))
check("有 _file_lock", hasattr(inst, "_file_lock"))

print("[4] 核心方法可调用（委托到 core）")
reverse = [
    ("_segments_to_text", ([{"type": "image", "data": {}}],)),
    ("_reply_of", ({"message": [{"type": "text", "data": {"text": "x"}}]},)),
    ("_date_of", ({"time": 1700000000},)),
    ("_probe_backend", ([{"real_seq": 1}],)),
    ("_safe_id", ("123456",)),
    ("_safe_date", ("2026-08-23",)),
    ("_target_files", ("group", "111")),
    ("_scan_archived_targets", ()),
]
for name, args in reverse:
    try:
        fn = getattr(inst, name)
        res = fn(*args)
        check(f"{name} 可调用", True, "")
    except Exception as e:
        check(f"{name} 可调用", False, f"{type(e).__name__}: {e}")

check("_segments_to_text 返回占位符",
      inst._segments_to_text([{"type": "image", "data": {}}]) == "[图片]")
check("_probe_backend real_seq->napcat",
      inst._probe_backend([{"real_seq": 1}]) == "napcat")
check("_safe_id 拒绝字母", inst._safe_id("12a") is False)
check("_safe_date 拒绝坏形状", inst._safe_date("2026-8-23") is False)

print("[5] 别名委托链路")
check("_alias_add 成功", inst._alias_add("group", "111", "测试群") is None)
check("_target_label 含别名", "测试群" in inst._target_label("group", "111"),
      inst._target_label("group", "111"))
check("_resolve_gid 别名", inst._resolve_gid("测试群", "group") == ("111", None),
      inst._resolve_gid("测试群", "group"))
check("_resolve_target 纯数字", inst._resolve_target("222") == ("group", "222"))
check("aliases.json 已写盘", inst.aliases_file.exists())

print("[6] Web API 注册")
inst._register_web_apis()
routes = [r[0] for r in star_mod.Context.registered_web_apis]
check("注册了路由", len(routes) > 0, len(routes))
check("含 console/state", f"/{PLUGIN_NAME}/console/state" in routes)
check("含 console/messages", f"/{PLUGIN_NAME}/console/messages" in routes)
check("含兼容 state", f"/{PLUGIN_NAME}/state" in routes)

print("[7] store 委托（真实写盘）")
p = inst._target_path("group", "111", "2026-08-23")
n = inst._merge_write(p, [{"seq": 1, "t": "2026-08-23 10:00:00",
                           "user_id": "u", "content": "hi"}])
check("_merge_write 写入1条", n == 1, n)
check("文件存在", p.exists())
n2 = inst._merge_write(p, [{"seq": 1, "t": "2026-08-23 10:00:00",
                            "user_id": "u", "content": "hi"}])
check("_merge_write 幂等", n2 == 0, n2)
check("_count_rows_cached", inst._count_rows_cached([p]) == 1,
      inst._count_rows_cached([p]))
files = inst._target_files("group", "111")
check("_target_files 找到文件", len(files) == 1, files)
check("_scan 扫到目标", ("group", "111") in inst._scan_archived_targets(),
      inst._scan_archived_targets())

print("[8] 聚合委托（真实读盘）")
try:
    agg = importlib.import_module(f"data.plugins.{PLUGIN_NAME}.core.aggregate")
    r = agg.search(inst._target_files("group", "111"), kw="hi")
    check("aggregate.search 命中", r["total"] == 1, r)
    st = agg.stats(inst._target_files("group", "111"))
    check("aggregate.stats 总条数", st["total"] == 1, st)
except Exception as e:
    check("aggregate 可调用", False, f"{type(e).__name__}: {e}")

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
