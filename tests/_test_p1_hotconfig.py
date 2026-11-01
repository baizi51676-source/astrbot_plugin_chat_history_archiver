"""P1-2（配置热生效）/ P1-3（立即归档语义）/ P1-4（前端 hint）回归测试。

P1-2 核心：
- _apply_runtime_config 能把配置改动重新解析到运行态（不必重载）
- 返回「实际变化的字段」，页面据此反馈
- interval/whitelist/batch/clean_days 等属性直接影响后续行为
- auto_export 开关切换会真的启停后台任务
- export_dir 变更标记 needs_reload（涉及数据迁移，确实需要重载）

P1-3：
- 按钮走 apply_rules=False（不受白名单、不限当天）
- 归档后登记 protected
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
event_mod.AstrMessageEvent = type("E", (), {})

web_mod = types.ModuleType("astrbot.api.web")
PAYLOAD = {}          # 模块级可变容器：main.py 在导入时就绑定了 request 对象，
class _Req:           # 后续替换 web_mod.request 不会影响它，因此用共享容器传值
    query = {}
    async def json(self, default=None):
        return PAYLOAD if PAYLOAD else (default or {})
web_mod.request = _Req()
web_mod.json_response = lambda d: {"json": d}
web_mod.error_response = lambda m, status_code=400: {"error": m, "code": status_code}
web_mod.file_response = None

class AstrBotConfig(dict):
    save_count = 0
    def save_config(self): AstrBotConfig.save_count += 1
    async def save_config_async(self): AstrBotConfig.save_count += 1
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

def make(**override):
    base = {
        "export_dir": "", "auto_export": False, "interval_seconds": 120,
        "count_per_batch": 50, "auto_export_friends": False, "admin_only": True,
        "whitelist": [], "auto_clean": False, "clean_days": 14, "archive_bots": [],
        "backend": "auto", "startup_verify": False, "verify_days": 3, "aliases": [],
        "llm_summary_enabled": False, "llm_summary_time": "23:30",
        "llm_briefing_enabled": False,
    }
    base.update(override)
    return m.ChatHistoryArchiver(star_mod.Context(), AstrBotConfig(base))

print("[P1-2] _apply_runtime_config 直接改运行态")
inst = make()
before_interval = inst.interval
# 模拟页面改了配置（config 已是新值），再重算
inst.config["interval_seconds"] = 300
inst.config["whitelist"] = ["111", "222"]
inst.config["count_per_batch"] = 200
inst.config["clean_days"] = 90
changed = inst._apply_runtime_config()
check("interval 已更新到 300", inst.interval == 300, inst.interval)
check("interval 出现在变更列表", "interval" in changed, list(changed))
check("变更记录含新旧值",
      changed.get("interval") == (before_interval, 300), changed.get("interval"))
check("whitelist 已更新", inst.whitelist == ["111", "222"], inst.whitelist)
check("batch 已更新到 200", inst.batch == 200, inst.batch)
check("clean_days 已更新到 90", inst.clean_days == 90, inst.clean_days)

print()
print("[P1-2] 现场问题复现：文件 60 天 → 运行态也 60 天")
inst2 = make(clean_days=14, verify_days=3)
inst2.config["clean_days"] = 90
inst2.config["verify_days"] = 60
inst2._apply_runtime_config()
check("clean_days 90 已生效", inst2.clean_days == 90, inst2.clean_days)
check("verify_days 60 已生效（不再被截成30）",
      inst2.verify_days == 60, inst2.verify_days)

print()
print("[P1-2] 边界与不变项")
inst3 = make()
inst3.config["interval_seconds"] = 10          # 低于最小值
inst3.config["count_per_batch"] = 999          # 高于最大值
inst3.config["verify_days"] = 999              # 超上限
inst3._apply_runtime_config()
check("interval 夹到最小 30", inst3.interval == 30, inst3.interval)
check("batch 夹到 200", inst3.batch == 200, inst3.batch)
check("verify_days 夹到 180", inst3.verify_days == 180, inst3.verify_days)
check("超范围有告警",
      any(s == "W" and "verify_days" in msg for s, msg in logs), logs[-3:])

print()
print("[P1-2] _api_config_save 端到端：保存即生效")
inst4 = make()
PAYLOAD.clear()
PAYLOAD["patch"] = {"interval_seconds": 300,
                    "whitelist": ["111"],
                    "admin_only": False}
r = asyncio.run(inst4._api_config_save())
d = r["json"]
check("保存成功", d.get("ok") is True, d)
check("saved 含三项", set(d.get("saved", [])) ==
      {"interval_seconds", "whitelist", "admin_only"}, d.get("saved"))
check("applied 报告热生效项",
      "interval" in d.get("applied", []), d.get("applied"))
check("运行态 interval=300", inst4.interval == 300, inst4.interval)
check("运行态 whitelist 生效", inst4.whitelist == ["111"], inst4.whitelist)
check("运行态 admin_only 生效", inst4.admin_only is False, inst4.admin_only)
check("有热生效日志",
      any(s == "I" and "热生效" in msg for s, msg in logs), logs[-3:])
check("配置已落盘", AstrBotConfig.save_count > 0, AstrBotConfig.save_count)

print()
print("[P1-2] export_dir 变更标记 needs_reload")
inst5 = make()
inst5.config["export_dir"] = "D:/other/dir"
PAYLOAD.clear()
PAYLOAD["patch"] = {"export_dir": "D:/other/dir"}
r5 = asyncio.run(inst5._api_config_save())["json"]
check("标记 needs_reload", "needs_reload" in r5, r5)
check("提示需重载", "重载" in r5.get("message", ""), r5.get("message"))
check("export_dir 未被就地改写（需重载才迁移）",
      inst5.export_dir != Path("D:/other/dir"), inst5.export_dir)

print()
print("[P1-2] auto_export 开关真的启停任务")
async def toggle_test():
    inst6 = make(auto_export=False)
    check("初始无 _task", inst6._task is None)
    inst6.config["auto_export"] = True
    inst6._apply_runtime_config()
    await inst6._sync_background_tasks()
    started = inst6._task is not None
    # 立刻停掉，避免测试里真的跑循环
    if inst6._task:
        inst6._task.cancel()
        try:
            await inst6._task
        except asyncio.CancelledError:
            pass
        inst6._task = None
    return started

started = asyncio.run(toggle_test())
check("打开 auto_export 会启动 _task", started)

async def toggle_off():
    inst7 = make(auto_export=True)
    loop = asyncio.get_running_loop()
    async def forever():
        await asyncio.sleep(3600)
    inst7._task = loop.create_task(forever())
    inst7.config["auto_export"] = False
    inst7._apply_runtime_config()
    await inst7._sync_background_tasks()
    return inst7._task is None

check("关闭 auto_export 会停止 _task", asyncio.run(toggle_off()))

print()
print("[P1-3] 立即归档不受白名单限制 + 登记 protected")
inst8 = make(whitelist=["999"])   # 白名单只有 999，实际群是 111
calls = []
async def fake_get_clients():
    class C:
        async def call_action(self, action, **kw):
            if action == "get_group_list":
                return [{"group_id": "111", "group_name": "A"}]
            if action == "get_friend_list":
                return []
            return []
    return [("pid", "10001", C())]

async def fake_export(chat, tid, **kw):
    calls.append({"chat": chat, "tid": tid, "kw": kw})
    # 造一条归档文件，让 _scan_archived_targets 能扫到
    fp = inst8.export_dir / f"napcat_{tid}_2026-11-01.jsonl"
    fp.write_text(json.dumps({"seq": 1, "t": "2026-11-01 10:00:00",
                              "user_id": "u", "content": "hi"},
                             ensure_ascii=False) + "\n", encoding="utf-8")
    return 1

inst8._get_clients = fake_get_clients
inst8._export_target = fake_export
inst8._register_name = lambda *a, **k: None

async def run_archive():
    PAYLOAD.clear()
    web_mod.request.query = {}
    r = await inst8._api_archive()
    # 等待内部任务跑完
    if inst8._manual_task:
        await inst8._manual_task
    return r["json"]

res = asyncio.run(run_archive())
check("返回 started", res.get("started") is True, res)
check("白名单外的群 111 也被归档", any(c["tid"] == "111" for c in calls), calls)
check("按钮走 apply_rules=False（不限当天）",
      all(c["kw"].get("today_only") is False for c in calls), calls)
check("目标已登记 protected",
      "111" in inst8._state.get("protected", {}).get("group", []),
      inst8._state.get("protected"))

print()
print("[P1-4] 前端 export_dir hint 已更新")
app_js = (PLUGIN_DIR / "pages" / "console" / "app.js").read_text(encoding="utf-8")
check("不再提示旧路径 data/workspaces", "data/workspaces/napcat_exports" not in app_js)
check("提示新数据目录", "plugin_data/astrbot_plugin_chat_history_archiver" in app_js)
check("提示需重载", "重载" in app_js)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
