"""P3-3 / P3-4 回归测试。

P3-3：metadata.yaml 的冗余 config 块已删除，_conf_schema.json 仍是唯一权威。
P3-4：_safe_date 真正校验日期合法性（不再只查形状）。
"""
import json
import sys
import types
import importlib
import shutil
import tempfile
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
be = importlib.import_module(f"data.plugins.{NAME}.core.backend")

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

print("[P3-3] metadata.yaml 不再有冗余 config 块")
meta_text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
check("无 config: 块", "\nconfig:" not in meta_text, )
check("保留 name", "name: " + NAME in meta_text)
check("保留 version", "version: 2.4.0" in meta_text)
check("保留 pages", "pages:" in meta_text and "name: console" in meta_text)
check("保留 support_platforms", "support_platforms:" in meta_text)
check("不再写旧导出目录",
      "data/workspaces/napcat_exports" not in meta_text)

# schema 是唯一权威（v2.4.0 新增 private_whitelist 后为 25 项）
schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
check("schema 25 项", len(schema) == 25, len(schema))
for must in ("backend", "export_dir", "auto_export", "interval_seconds",
             "verify_days", "whitelist", "private_whitelist",
             "auto_clean", "clean_days",
             "ui_default_tab", "ui_messages_page_size", "ui_avatar_cache",
             "llm_summary_enabled", "llm_summary_time", "llm_briefing_enabled",
             "llm_summary_provider", "llm_summary_trend_days",
             "llm_summary_max_chars", "llm_briefing_max_chars"):
    check(f"schema 含 {must}", must in schema, list(schema))

check("metadata 精简为 14 行",
      len(meta_text.strip().splitlines()) == 14,
      len(meta_text.strip().splitlines()))

print()
print("[P3-4] _safe_date 真正校验日期")
cases_ok = ["2026-08-23", "2026-12-31", "2028-02-29", "2024-02-29", "2000-01-01"]
cases_bad = ["2026-99-99", "2026-02-31", "2026-13-01", "2026-00-10",
             "2026-04-31", "2026-02-29", "2026-8-23", "", "2026/08/23",
             "2026-08-23x"]
# 注：'2026-08-23 '（尾随空格）会被判为合法——safe_date 内部先 strip()，
# 这是既有且合理的行为（用户从表格复制常带空格），故不列入拒绝用例。
for v in cases_ok:
    check(f"接受 {v}", be.safe_date(v) is True)
check("接受带尾随空格（内部 strip）", be.safe_date("2026-08-23 ") is True)
for v in cases_bad:
    check(f"拒绝 {v!r}", be.safe_date(v) is False, f"got {be.safe_date(v)}")

# 边界：闰年规则
check("2026 非闰年 2/29 拒绝", be.safe_date("2026-02-29") is False)
check("2028 闰年 2/29 接受", be.safe_date("2028-02-29") is True)
check("2100 非闰年拒绝", be.safe_date("2100-02-29") is False)

print()
print("[P3-4] 实例方法委托一致")
cfg = AstrBotConfig({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
})
inst = m.ChatHistoryArchiver(star_mod.Context(), cfg)
for v, want in (("2026-08-23", True), ("2026-99-99", False),
                ("2026-02-31", False), ("2026-8-23", False)):
    check(f"inst._safe_date({v!r}) == {want}",
          inst._safe_date(v) is want, inst._safe_date(v))

print()
print("[P3-4] API 会拒绝非法日期（而不是静默返回空）")
import asyncio
web_mod.request.query = {"chat": "group", "target_id": "111",
                         "start": "2026-99-99", "end": "2026-99-99"}
r = asyncio.run(inst._api_messages())
check("messages 拒绝非法 start", "error" in r, r)
web_mod.request.query = {"chat": "group", "target_id": "111",
                         "start": "2026-02-31", "end": "2026-02-31"}
r2 = asyncio.run(inst._api_messages())
check("messages 拒绝 2026-02-31", "error" in r2, r2)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
