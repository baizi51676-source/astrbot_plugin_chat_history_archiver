"""P2-10 移除归档目标回归测试。

覆盖：
- core.targets 纯逻辑：关联文件枚举、state/aliases/配置项清理
- _remove_target：删文件、清 state、清 aliases、清配置项（防复活）
- dry_run 只预览不删除
- LLM 工具 remove_archived_target 与 console API 都能用
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
class _Ev:
    def is_admin(self): return True
    def get_message_obj(self): return types.SimpleNamespace(self_id="1")
event_mod.AstrMessageEvent = _Ev

web_mod = types.ModuleType("astrbot.api.web")
PAYLOAD = {}
class _Req:
    query = {}
    async def json(self, default=None):
        return PAYLOAD if PAYLOAD else (default or {})
web_mod.request = _Req()
web_mod.json_response = lambda d: {"json": d}
web_mod.error_response = lambda m, status_code=400: {"error": m, "code": status_code}
web_mod.file_response = lambda *a, **k: {}

class AstrBotConfig(dict):
    saved = 0
    def save_config(self): AstrBotConfig.saved += 1
    async def save_config_async(self): AstrBotConfig.saved += 1
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
tg = importlib.import_module(f"data.plugins.{NAME}.core.targets")

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

cfg = AstrBotConfig({
    "export_dir": "", "auto_export": False, "interval_seconds": 120,
    "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
    "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
    "backend": "auto", "startup_verify": False, "verify_days": 3,
    "aliases": ["僵尸号,2490530663", "正常群,111111"],
})
inst = m.ChatHistoryArchiver(star_mod.Context(), cfg)
TID = "2490530663"

# ---------- 造全套关联文件 ----------
def make_files():
    d = inst.export_dir
    for dt in ("2026-10-30", "2026-10-31"):
        (d / f"napcat_private_{TID}_{dt}.jsonl").write_text(
            json.dumps({"seq": 1, "t": f"{dt} 10:00:00", "user_id": "u",
                        "content": "x"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
    (d / f"napcat_private_{TID}.jsonl").write_text('{"seq":2}\n', encoding="utf-8")
    (d / f"names_private_{TID}.json").write_text('{"1":"a"}', encoding="utf-8")
    (d / "avatars").mkdir(exist_ok=True)
    (d / "avatars" / f"{TID}.png").write_bytes(b"PNG")
    for dt in ("2026-10-30", "2026-10-31"):
        folder = d / "summaries" / dt
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{TID}.md").write_text("总结", encoding="utf-8")
        (folder / "brief.md").write_text("简报", encoding="utf-8")
    # 另一个目标的文件，不应被删
    (d / "napcat_private_999999_2026-10-31.jsonl").write_text('{"seq":3}\n',
                                                              encoding="utf-8")

make_files()

print("[1] core.targets 枚举关联文件")
files = tg.related_files(inst.export_dir, "private", TID)
names = sorted(p.name for p in files)
check("找到按天归档 2 个", sum(1 for n in names if n.count("-") >= 2 and n.endswith(".jsonl")) >= 2, names)
check("找到旧版单文件", f"napcat_private_{TID}.jsonl" in names, names)
check("找到昵称映射", f"names_private_{TID}.json" in names, names)
check("找到头像", f"{TID}.png" in names, names)
check("找到每日总结 2 个", sum(1 for n in names if n.endswith(".md")) == 2, names)
check("不含别的目标的文件", "napcat_private_999999_2026-10-31.jsonl" not in names, names)
check("不含 brief.md", "brief.md" not in names, names)
check("总数 7", len(files) == 7, len(files))

print()
print("[2] state / aliases / 配置项清理（纯函数）")
state = {"private": {TID: {"t": 1, "ids": []}, "111111": {"t": 2, "ids": []}},
         "protected": {"private": [TID, "111111"], "group": ["222"]}}
tg.strip_from_state(state, "private", TID)
check("state 游标已移除", TID not in state["private"], state["private"])
check("其它目标游标保留", "111111" in state["private"])
check("protected 已移除", TID not in state["protected"]["private"],
      state["protected"]["private"])
check("protected 其它保留", "111111" in state["protected"]["private"])

aliases = {"private": {TID: {"name": "僵尸", "aliases": ["僵尸号"]}},
           "group": {"111111": {"name": "正常", "aliases": ["正常群"]}}}
tg.strip_from_aliases(aliases, "private", TID)
check("aliases 条目已移除", TID not in aliases["private"], aliases["private"])
check("其它别名保留", "111111" in aliases["group"])

out = tg.strip_from_config_lines(["僵尸号,2490530663", "正常群,111111"], TID)
check("配置项指向该目标的被移除", out == ["正常群,111111"], out)

print()
print("[3] dry_run 只预览")
PAYLOAD.clear()
res = asyncio.run(inst._remove_target("private", TID, dry_run=True))
check("预览含目标", TID in res, res[:80])
check("预览未真正删除",
      (inst.export_dir / f"names_private_{TID}.json").exists())
before_count = len(list(inst.export_dir.glob(f"napcat_private_{TID}_*.jsonl")))
check("文件仍在", before_count >= 2, before_count)

print()
print("[4] 真正移除")
AstrBotConfig.saved = 0
logs.clear()
res2 = asyncio.run(inst._remove_target("private", TID, dry_run=False))
check("返回成功", res2.startswith("✅"), res2[:60])
check("关联文件已删除",
      not (inst.export_dir / f"names_private_{TID}.json").exists())
check("归档文件已删除",
      len(list(inst.export_dir.glob(f"napcat_private_{TID}_*.jsonl"))) == 0)
check("旧版单文件已删除",
      not (inst.export_dir / f"napcat_private_{TID}.jsonl").exists())
check("头像已删除", not (inst.export_dir / "avatars" / f"{TID}.png").exists())
check("每日总结已删除",
      not any((inst.export_dir / "summaries" / dt / f"{TID}.md").exists()
              for dt in ("2026-10-30", "2026-10-31")))
check("别的目标文件未受影响",
      (inst.export_dir / "napcat_private_999999_2026-10-31.jsonl").exists())
check("brief.md 未受影响",
      (inst.export_dir / "summaries" / "2026-10-31" / "brief.md").exists())

check("state 已清理",
      TID not in inst._state.get("private", {}), inst._state.get("private"))
check("aliases 已清理",
      TID not in inst.aliases.get("private", {}), inst.aliases.get("private"))
check("配置项已清理（防复活）",
      TID not in (inst.config.get("aliases") or []),
      inst.config.get("aliases"))
check("配置已落盘", AstrBotConfig.saved > 0, AstrBotConfig.saved)
check("日志记录了移除",
      any(s == "I" and "已移除目标" in msg for s, msg in logs), logs[-3:])

print()
print("[5] 移除后不再出现在扫描结果")
targets = inst._scan_archived_targets()
check("扫描不到该目标", ("private", TID) not in targets, targets)

print()
print("[6] LLM 工具 remove_archived_target")
# 重新造文件以便再测工具。
# 注意：移除别名后，纯数字 tid 会被 resolve 默认判成 group，
# 因此工具支持（也应该用）显式 chat 参数指定类型。
make_files()
ev = _Ev()
out_tool = asyncio.run(inst.remove_archived_target(ev, TID, "private", "false"))
check("工具返回成功", str(out_tool).startswith("✅"), str(out_tool)[:60])
check("工具已删文件",
      not (inst.export_dir / f"names_private_{TID}.json").exists())

print()
print("[6b] 显式 chat 避免错向（移除后别名已清，纯数字会误判为 group）")
check("无别名时纯数字默认解析为 group",
      inst._resolve_target(TID) == ("group", TID),
      inst._resolve_target(TID))
make_files()
out2 = asyncio.run(inst.remove_archived_target(ev, TID, "private", "true"))
check("显式 chat=private 时预览的是 private",
      "私聊" in str(out2), str(out2)[:80])
check("预览未删文件",
      (inst.export_dir / f"names_private_{TID}.json").exists())

print()
print("[6c] both=True 兜底清理另一类型的残留")
d = inst.export_dir
(d / f"napcat_{TID}_2026-10-31.jsonl").write_text('{"seq":9}\n', encoding="utf-8")
make_files()
out3 = asyncio.run(inst._remove_target("private", TID, dry_run=False, both=True))
check("group 侧残留也被清", not (d / f"napcat_{TID}_2026-10-31.jsonl").exists())
check("private 侧也清", not (d / f"napcat_private_{TID}.jsonl").exists())

print()
print("[7] console API remove_target")
make_files()
PAYLOAD.clear()
PAYLOAD.update({"chat": "private", "target_id": TID, "dry_run": True})
r = asyncio.run(inst._api_remove_target())["json"]
check("API 预览 ok", r.get("ok") is True, r)
check("API 预览标记 dry_run", r.get("dry_run") is True, r)
check("预览不删文件",
      (inst.export_dir / f"names_private_{TID}.json").exists())

PAYLOAD.clear()
PAYLOAD.update({"chat": "private", "target_id": TID})
r2 = asyncio.run(inst._api_remove_target())["json"]
check("API 真正移除 ok", r2.get("ok") is True, r2)
check("API 删除了文件",
      not (inst.export_dir / f"names_private_{TID}.json").exists())

print()
print("[8] 参数校验")
PAYLOAD.clear()
PAYLOAD.update({"chat": "bad", "target_id": TID})
r3 = asyncio.run(inst._api_remove_target())
check("非法 chat 返回错误", "error" in r3, r3)
PAYLOAD.clear()
PAYLOAD.update({"chat": "group", "target_id": "abc"})
r4 = asyncio.run(inst._api_remove_target())
check("非法 target_id 返回错误", "error" in r4, r4)

print()
print("[9] Web API 已注册")
inst._register_web_apis()
routes = [x[0] for x in star_mod.Context.registered_web_apis]
check("注册了 console/remove_target",
      any(r.endswith("console/remove_target") for r in routes), routes[-5:])

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
