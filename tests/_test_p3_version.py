"""P3-2 版本号单一来源测试。

核心断言：
- PLUGIN_VERSION 来自 metadata.yaml，而不是硬编码常量
- 改 metadata 的 version → 常量跟着变（同一份源码，无需改 py）
- metadata 缺失/损坏 → 退回默认值，不崩溃
"""
import sys, types, importlib, shutil, tempfile, re
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
NAME = "astrbot_plugin_chat_history_archiver"

def build_env():
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
    class Context: pass
    sm.Star = Star; sm.Context = Context
    em = types.ModuleType("astrbot.api.event")
    class F:
        def llm_tool(self, name=None): return lambda fn: fn
    em.filter = F(); em.AstrMessageEvent = type("E", (), {})
    wm = types.ModuleType("astrbot.api.web")
    class R:
        query = {}
        async def json(self, default=None): return default or {}
    wm.request = R(); wm.json_response = lambda d: {"json": d}
    wm.error_response = lambda m, status_code=400: {"error": m}
    wm.file_response = None
    class Cfg(dict):
        def save_config(self): pass
        async def save_config_async(self): pass
    api.AstrBotConfig = Cfg
    api.star = sm; api.event = em; api.web = wm
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
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
    return smap

def make_plugin_copy(version_override=None, drop_metadata=False):
    pkg = Path(tempfile.mkdtemp())
    pd = pkg / "data" / "plugins" / NAME
    pd.mkdir(parents=True)
    for it in PLUGIN_DIR.iterdir():
        if it.is_dir() and it.name in ("core", "pages"):
            shutil.copytree(it, pd / it.name)
        elif it.is_file() and it.suffix in (".py", ".json", ".yaml"):
            shutil.copy2(it, pd / it.name)
    meta = pd / "metadata.yaml"
    if drop_metadata:
        meta.unlink()
    elif version_override is not None:
        text = meta.read_text(encoding="utf-8")
        text = re.sub(r"^version:\s*[0-9.]+", f"version: {version_override}",
                      text, count=1, flags=re.M)
        meta.write_text(text, encoding="utf-8")
    return pkg, pd

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

def load(pkg):
    sys.path.insert(0, str(pkg))
    mod_name = f"data.plugins.{NAME}.main"
    for k in list(sys.modules):
        if k.startswith(f"data.plugins.{NAME}"):
            del sys.modules[k]
    sys.path.remove(str(pkg))
    sys.path.insert(0, str(pkg))
    return importlib.import_module(mod_name)

print("[1] 正常：版本来自 metadata.yaml")
smap = build_env()
pkg, pd = make_plugin_copy()
m = load(pkg)
meta_ver = re.search(r"^version:\s*([0-9.]+)",
                     (pd / "metadata.yaml").read_text(encoding="utf-8"),
                     re.M).group(1)
check("PLUGIN_VERSION 等于 metadata 版本", m.PLUGIN_VERSION == meta_ver,
      f"{m.PLUGIN_VERSION} vs {meta_ver}")
check("有读取函数", hasattr(m, "_read_plugin_version"))

print()
print("[2] 改 metadata → 常量自动跟着变（同一份 main.py）")
pkg2, pd2 = make_plugin_copy(version_override="9.9.9")
m2 = load(pkg2)
check("版本变为 9.9.9", m2.PLUGIN_VERSION == "9.9.9", m2.PLUGIN_VERSION)
check("main.py 源码未改", (pd2 / "main.py").read_text(encoding="utf-8")
      .count('PLUGIN_VERSION = "2.4.0"') == 0)

print()
print("[3] metadata 缺失 → 退回默认值不崩溃")
pkg3, pd3 = make_plugin_copy(drop_metadata=True)
try:
    m3 = load(pkg3)
    check("缺失时仍能加载", True)
    check("退回默认 2.4.0", m3.PLUGIN_VERSION == "2.4.0", m3.PLUGIN_VERSION)
except Exception as e:
    check("缺失时仍能加载", False, f"{type(e).__name__}: {e}")

print()
print("[4] metadata 损坏（version 行异常）→ 不崩溃")
pkg4, pd4 = make_plugin_copy()
meta4 = pd4 / "metadata.yaml"
text4 = meta4.read_text(encoding="utf-8")
text4 = re.sub(r"^version:\s*[0-9.]+", "version: bad@@", text4, count=1, flags=re.M)
meta4.write_text(text4, encoding="utf-8")
try:
    m4 = load(pkg4)
    check("损坏值被接受（不会崩）", True)
    check("拿到的是原文值", m4.PLUGIN_VERSION == "bad@@", m4.PLUGIN_VERSION)
except Exception as e:
    check("损坏值被接受（不会崩）", False, f"{type(e).__name__}: {e}")

print()
print("[5] 页面/API 用的是同一个常量")
pkg5, pd5 = make_plugin_copy()
m5 = load(pkg5)
import asyncio
cfg = m5.AstrBotConfig({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
})
sys.modules[f"data.plugins.{NAME}.main"].Context = sys.modules["astrbot.api.star"].Context
inst = m5.ChatHistoryArchiver(sys.modules["astrbot.api.star"].Context(), cfg)
overview = inst._overview_data()
check("总览 API 版本与常量一致", overview["version"] == m5.PLUGIN_VERSION,
      (overview["version"], m5.PLUGIN_VERSION))

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
