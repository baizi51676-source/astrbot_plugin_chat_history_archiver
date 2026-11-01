import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
try:
    from astrbot.api.web import (
        error_response,
        json_response,
        request,
    )
    try:
        # file_response 仅在较新的 AstrBot 中提供：单独降级，避免因缺它导致整个页面 API 不可用
        from astrbot.api.web import file_response
    except Exception:  # pragma: no cover - 旧版 AstrBot 无 file_response
        file_response = None
    _WEB_AVAILABLE = True
except Exception:  # pragma: no cover - 旧版 AstrBot 无插件页面 API
    _WEB_AVAILABLE = False
    file_response = None

# 纯逻辑子模块（P3-1 拆分）。
# 注意：插件类与本文件内的 @filter.llm_tool 方法必须留在 main.py——
# AstrBot 以 cls.__module__ 作为 star_map 键、按 "<插件模块路径>." 前缀
# 归属/清理 LLM 工具，搬到子模块会导致插件无法加载或工具丢失归属。
from .core import aggregate as _aggregate
from .core import aliases as _aliases
from .core import atomic as _atomic
from .core import backend as _backend_mod
from .core import paths as _paths
from .core import pager as _pager
from .core import schedule as _schedule
from .core import store as _store
from .core import targets as _targets
from .core import text as _text
from .core import verify as _verify
from .core.paths import LEGACY_EXPORT_DIR
from .core.text import SEG_PLACEHOLDER as _SEG_PLACEHOLDER

PLUGIN_NAME = "astrbot_plugin_chat_history_archiver"


def _read_plugin_version(default: str = "2.4.0") -> str:
    """P3-2: 版本号的单一来源 = metadata.yaml（AstrBot 也用这个文件）。

    此前 `PLUGIN_VERSION` 与 `metadata.yaml` 需手工双份同步，极易漂移
    （页面 footer 读常量、市场/WebUI 读 yaml，两边不一致就会出现
    「页面显示 v2.3.3、插件列表显示 2.4.0」）。这里启动时读一次，
    读不到才退回内置默认值。
    """
    from pathlib import Path

    try:
        meta = Path(__file__).resolve().parent / "metadata.yaml"
        for line in meta.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("version:"):
                v = s.split(":", 1)[1].strip().strip("'\"")
                if v:
                    return v
    except Exception:
        pass
    return default


PLUGIN_VERSION = _read_plugin_version()

#: P1-6：启动检查天数上限（v2.4.0 由 30 提到 180）
VERIFY_DAYS_MAX = 180


def _count_lines(fp: Path) -> int:
    """统计行数（P2-1：供 to_thread 调用；按字节数换行，远快于逐行迭代）。"""
    return _store._count_newlines(fp)


def _read_text_file(fp: Path) -> str:
    """读取文本文件（P2-1：供 asyncio.to_thread 调用）。"""
    return fp.read_text(encoding="utf-8")


class _EmptyData(RuntimeError):
    """空数据状态（不是故障）：应返回 200 + 友好文案，而非 500。

    P2-11：现场「还没有每日总结可汇总」曾被当作 ERRO 级故障打日志并返回 500，
    前端只能显示「简报失败」，用户误以为插件出错。
    """


def _plugin_data_root() -> Path:
    """返回 AstrBot 的 data/plugin_data 目录（委托 core.paths）。"""
    return _paths.plugin_data_root()


def _plugin_dir_name() -> str:
    return _paths.plugin_dir_name(__file__)


def _default_export_dir() -> Path:
    return _paths.default_export_dir(__file__)


def _legacy_export_dir_path() -> Path:
    return _paths.legacy_export_dir_path()


def resolve_export_dir(raw):
    """解析 export_dir 配置（委托 core.paths.resolve_export_dir）。

    规则（v2.3.1，满足插件市场「数据持久化位置」规范）：
    - 留空或仍是旧默认值 → 用新默认 data/plugin_data/<插件名>（并返回旧目录用于迁移）
    - 相对路径且落在 data/workspaces/** → 视为旧数据位置，改用新默认并自动迁移
    - 其它相对路径 → 限制在 data/plugin_data/<插件名>/ 下
    - 绝对路径 → 原样使用（在 plugin_data 之外会告警）

    返回 (最终目录, 需要迁移的旧目录或 None)
    """
    return _paths.resolve_export_dir(raw, __file__)


def migrate_dir(src, dst) -> str:
    """把旧数据目录内容逐项搬到新目录（委托 core.paths.migrate_dir）。"""
    return _paths.migrate_dir(src, dst)



class ChatHistoryArchiver(Star):
    """NapCat / SnowLuma 历史聊天记录导出插件（OneBot v11 / aiocqhttp 适配）。

    通过扩展 API（get_group_msg_history / get_friend_msg_history）
    将历史聊天记录导出为 JSONL 文件（每行一条消息），供归档检索与离线分析；
    v1.4.0 起不再与外部插件联动搜索（查看/搜索已内置为 LLM 工具）。
    特性：
    - 自动归档开关（auto_export）：开启后定时循环增量导出（默认 120s 一次）
    - v1.5.0：多 bot（多 aiocqhttp 实例）独立归档，archive_bots 可选 bot；
      后端 NapCat/SnowLuma 自动探测（backend: auto）
    - 图片、表情、语音等媒体不导出，使用 [图片]/[表情]/[语音] 等占位符
    - 按天分文件：napcat_<群号>_YYYY-MM-DD.jsonl（私聊 napcat_private_<QQ>_*.jsonl）
    - 增量导出：记录每个目标的最新 message_seq，只拉新消息，不重复写入

    P3-1（v2.4.0）：纯逻辑已下沉到 ``core/``（paths/text/store/backend/
    aliases/aggregate/pager）。本类保留在 ``main.py`` 是硬性要求——
    AstrBot 以 ``cls.__module__`` 为 ``star_map`` 键、按插件模块路径前缀
    归属/清理 LLM 工具，类搬走会导致插件无法加载。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # v2.3.1：插件数据统一放 data/plugin_data/<插件名>（插件市场合规要求）
        self.export_dir, _legacy_dir = resolve_export_dir(
            config.get("export_dir", ""))
        self.export_dir.mkdir(parents=True, exist_ok=True)
        # 旧目录（data/workspaces/napcat_exports）存在时自动迁移
        self._migrate_msg = migrate_dir(_legacy_dir, self.export_dir)
        self._split_merged_files()  # v1.2.0: v1.1.0 单文件自动拆回按天文件
        self.state_file = self.export_dir / "state.json"
        self._state: dict = self._load_state()
        self._client = None
        self._task: asyncio.Task | None = None
        self._last_clean: datetime | None = None  # v1.3.1: 上次自动清理时间（每12h一次）
        self._qq_cache: dict = {}             # 平台实例 id -> 登录 QQ号（get_login_info）
        self._qq_cache_ts: float = 0.0
        self._backend: str | None = None      # 后端探测结果缓存: "napcat" / "snowluma"
        self._verify_task: asyncio.Task | None = None
        self._file_lock = asyncio.Lock()      # 写入段串行化（并发任务防护）
        # P1-5: 启动检查进度落盘（可续跑）
        self._verify_file = self.export_dir / "verify_state.json"
        self._verify_state = self._load_verify_state()
        self._sched_file = self.export_dir / "schedule_state.json"
        self._sched_state = self._load_sched_state()
        self._sched_task: asyncio.Task | None = None
        # P2-1: 行数索引持久化（重载后不必重新逐行统计 57k 行）
        self._row_cache_file = self.export_dir / "row_index.json"
        self._row_cache: dict = self._load_row_cache()
        self._row_cache_dirty = False
        self._warm_task: asyncio.Task | None = None
        # v2.2.0: 对话别名注册系统
        self.aliases_file = self.export_dir / "aliases.json"
        self.aliases = self._load_aliases()
        # P1-2: 所有「可热更新」的配置项集中在这里解析。
        # 页面保存配置后调用 _apply_runtime_config() 重新计算，
        # 不再需要「重载插件」才生效（现场「文件 60 天 / 运行 14 天」即此因）。
        self._apply_runtime_config(initial=True)

    def _apply_runtime_config(self, initial: bool = False) -> dict:
        """把 self.config 里的可热更新项重新解析到运行态属性。

        Returns:
            实际发生变化的字段 {字段: (旧值, 新值)}，供日志与页面提示。

        注意：`export_dir` 不在此列——它涉及数据目录迁移与 state_file/
        aliases_file 等路径绑定，改它必须重载插件（见 _api_config_save 的提示）。
        """
        cfg = self.config
        before = {}
        def _mark(name):
            before[name] = getattr(self, name, None)

        for name in ("auto_export", "interval", "batch", "auto_friends",
                     "admin_only", "whitelist", "private_whitelist",
                     "auto_clean", "clean_days",
                     "archive_bots", "backend_cfg", "startup_verify",
                     "verify_days", "llm_summary_enabled", "llm_summary_time",
                     "llm_briefing_enabled"):
            _mark(name)

        self.auto_export = bool(cfg.get("auto_export", True))
        self.interval = max(30, int(cfg.get("interval_seconds", 120)))
        self.batch = max(1, min(int(cfg.get("count_per_batch", 50)), 200))
        self.auto_friends = bool(cfg.get("auto_export_friends", False))
        self.admin_only = bool(cfg.get("admin_only", True))
        # v1.3.0: 自动归档白名单 / 历史自动清理（仅循环导出模式生效）
        self.whitelist = [str(x).strip() for x in (cfg.get("whitelist") or [])
                          if str(x).strip()]
        # v2.4.0: 私聊归档白名单（留空=只巡已登记目标，避免僵尸账号空调用）
        self.private_whitelist = [
            str(x).strip() for x in (cfg.get("private_whitelist") or [])
            if str(x).strip()]
        self.auto_clean = bool(cfg.get("auto_clean", True))
        self.clean_days = max(1, int(cfg.get("clean_days", 14)))
        # v1.5.0: 多 bot 归档范围
        self.archive_bots = [str(x).strip() for x in (cfg.get("archive_bots") or [])
                             if str(x).strip()]
        self.backend_cfg = str(cfg.get("backend", "auto") or "auto").strip().lower()
        # v2.1.0: 启动归档检查
        self.startup_verify = bool(cfg.get("startup_verify", True))
        # P1-6: 上限 180，超范围告警（不再静默截断）
        _verify_days_raw = int(cfg.get("verify_days", 3) or 3)
        self.verify_days = max(1, min(_verify_days_raw, VERIFY_DAYS_MAX))
        if _verify_days_raw != self.verify_days:
            logger.warning(
                f"[{PLUGIN_NAME}] verify_days={_verify_days_raw} 超出允许范围，"
                f"实际生效 {self.verify_days}（允许 1–{VERIFY_DAYS_MAX}）")
        # v2.4.0 (P1-1): 每日定时总结/简报
        self.llm_summary_enabled = bool(cfg.get("llm_summary_enabled", False))
        self.llm_summary_time = _schedule.parse_hhmm(
            cfg.get("llm_summary_time", "23:30"))
        self.llm_briefing_enabled = bool(cfg.get("llm_briefing_enabled", False))
        # v2.2.0: 别名配置项缓存
        self._alias_cfg = [str(x).strip() for x in (cfg.get("aliases") or [])
                           if str(x).strip()]

        changed = {}
        for name, old in before.items():
            new = getattr(self, name, None)
            if old != new:
                changed[name] = (old, new)
        return changed

    async def _sync_background_tasks(self) -> None:
        """P1-2: 按当前运行态开关同步后台任务（配置热生效的补充）。

        `interval` / `whitelist` / `batch` 等属性每轮都被读取，改完即生效；
        但 `auto_export` 决定「_auto_loop 要不要跑」，所以开关切换时必须
        真的启动/停止任务，否则页面显示已保存、行为却不变。
        """
        # 自动归档主循环
        if self.auto_export and self._task is None:
            self._task = asyncio.create_task(self._auto_loop())
            logger.info(f"[页面] 自动归档已启用（间隔 {self.interval}s）")
        elif not self.auto_export and self._task is not None:
            task = self._task
            self._task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 停止自动归档时异常（可忽略）：{e}")
            logger.info("[页面] 自动归档已停用（仅在被 LLM 工具触发时归档）")

        # 定时总结独立轮询：仅当 auto_export 关闭时由它负责
        need_sched_loop = ((self.llm_summary_enabled or self.llm_briefing_enabled)
                           and not self.auto_export)
        if need_sched_loop and self._sched_task is None:
            self._sched_task = asyncio.create_task(self._sched_loop())
            logger.info("[页面] 定时总结轮询已启动")
        elif not need_sched_loop and self._sched_task is not None:
            task = self._sched_task
            self._sched_task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 停止总结轮询时异常（可忽略）：{e}")

    # ---------------------------------------------------------------
    # 内部工具
    # ---------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {"group": {}, "private": {}}

    def _save_state(self) -> None:
        try:
            self.state_file.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logger.error(f"保存导出状态失败: {e}")

    # ---------------- v2.4.0 (P1-1)：定时总结调度状态 ----------------

    def _load_row_cache(self) -> dict:
        """P2-1: 行数索引持久化（重载插件后不必重新逐行统计）。

        内存缓存在重载后失效，而 57k 行首轮统计约 1.8s——这就是「重载后
        第一次打开总览要等很久」的原因。这里把 (mtime, size) → rows 落盘，
        重载后直接复用；文件变动时按 mtime/size 自动失效重算。
        """
        try:
            data = json.loads(self._row_cache_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            out = {}
            for k, v in data.items():
                if isinstance(v, list) and len(v) == 3:
                    out[str(k)] = (v[0], v[1], int(v[2]))
            return out
        except Exception:
            return {}

    def _save_row_cache(self) -> None:
        """把行数索引原子落盘（失败不影响主流程）。"""
        cache = getattr(self, "_row_cache", None)
        if not isinstance(cache, dict) or not cache:
            return
        try:
            _atomic.atomic_write_text(
                self._row_cache_file,
                json.dumps({k: list(v) for k, v in cache.items()},
                           ensure_ascii=False))
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 保存行数索引失败（可忽略）：{e}")

    async def _warm_row_index(self) -> None:
        """P2-1: 后台预热行数索引（全新安装时首次打开总览不再等 3.5s）。

        只在索引为空（全新安装 / 索引文件丢失）时才跑，避免每次启动都
        白白扫一遍 57k 行。在线程池里执行，不阻塞事件循环。
        """
        try:
            if self._row_cache:
                logger.debug("[页面] 行数索引已存在，跳过预热")
                return
            files = sorted(self.export_dir.glob("napcat_*.jsonl"))
            if not files:
                return
            logger.info(f"[页面] 后台预热行数索引（{len(files)} 个文件）…")
            await asyncio.to_thread(self._overview_data)
            await asyncio.to_thread(self._save_row_cache)
            self._row_cache_dirty = False
            logger.info("[页面] 行数索引预热完成，首次打开总览不再需要全量统计")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[页面] 行数索引预热失败（可忽略）：{e}")

    def _load_sched_state(self) -> dict:
        """读取调度状态（记录上次执行日期，重载后不重复跑同一晚）。"""
        try:
            data = json.loads(self._sched_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_sched_state(self) -> None:
        try:
            self._sched_file.write_text(
                json.dumps(self._sched_state, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logger.error(f"[总结] 保存调度状态失败: {e}")

    # ---------------- v2.4.0 (P1-5)：启动检查续跑状态 ----------------

    def _load_verify_state(self) -> dict:
        """读取启动检查进度（已完成项 + 窗口指纹）。"""
        try:
            data = json.loads(self._verify_file.read_text(encoding="utf-8"))
            return _verify.normalize_loaded(data)
        except Exception:
            return _verify.new_state()

    def _save_verify_state(self) -> None:
        try:
            self._verify_file.write_text(
                json.dumps(self._verify_state, ensure_ascii=False),
                encoding="utf-8")
        except Exception as e:
            logger.error(f"[归档检查] 保存检查进度失败: {e}")

    async def _run_daily_summaries(self, date: str) -> int:
        """对所有已归档目标生成指定日期的总结。返回成功条数。

        走与页面相同的队列（串行），避免并发打爆模型额度。
        """
        targets = self._scan_archived_targets()
        if not targets:
            logger.info("[总结] 暂无已归档目标，跳过定时总结")
            return 0
        self._ensure_summary_worker()
        ok = 0
        for chat, tid in targets:
            # 当天没有消息的目标直接跳过（不产生空总结，也不打 ERROR）
            if not self._day_records(chat, tid, date):
                continue
            fut = asyncio.get_running_loop().create_future()
            job = {"chat": chat, "tid": tid, "date": date,
                   "trend": True, "future": fut}
            await self._summary_queue.put(job)
            try:
                await asyncio.wait_for(fut, timeout=300)
                ok += 1
            except _EmptyData:
                pass
            except Exception as e:
                logger.error(f"[总结] {chat}:{tid} {date} 定时总结失败: {e}")
        logger.info(f"[总结] 定时总结完成：{date}，成功 {ok}/{len(targets)} 个目标")
        return ok

    async def _run_daily_briefing(self) -> bool:
        """生成一次简报（汇总最近的每日总结）。"""
        self._ensure_summary_worker()
        fut = asyncio.get_running_loop().create_future()
        job = {"kind": "brief", "days": _schedule.briefing_days(
            datetime.now(), self.config.get("llm_summary_trend_days", 3)),
            "future": fut}
        await self._summary_queue.put(job)
        try:
            await asyncio.wait_for(fut, timeout=360)
            return True
        except _EmptyData as e:
            logger.info(f"[简报] 定时简报跳过：{e}")
            return False
        except Exception as e:
            logger.error(f"[简报] 定时简报失败: {e}")
            return False

    async def _sched_loop(self) -> None:
        """v2.4.0 (P1-1): 独立的定时总结轮询。

        仅当 auto_export 关闭时才启用（auto_export 开时由 _auto_loop 顺带调用，
        避免两处同时触发导致重复执行）。
        """
        while True:
            try:
                await self._daily_sched_check()
            except Exception as e:
                logger.error(f"[总结] 定时调度异常: {e}")
            await asyncio.sleep(self.interval)

    async def _daily_sched_check(self) -> None:
        """调度轮询：每轮检查是否到了总结/简报的时刻（每天各最多一次）。"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if _schedule.should_run(now, self.llm_summary_time,
                                self._sched_state.get("summary_date", ""),
                                self.llm_summary_enabled):
            date = _schedule.summary_date(now)
            logger.info(f"[总结] 到达定时点 {self.llm_summary_time.strftime('%H:%M')}，"
                        f"开始生成 {date} 的每日总结")
            # 先落盘日期再执行：即便中途失败也不会在同一晚反复重试烧额度
            self._sched_state["summary_date"] = today
            self._save_sched_state()
            try:
                await self._run_daily_summaries(date)
            except Exception as e:
                logger.error(f"[总结] 定时总结异常: {e}")

        # 简报：在总结之后跑（同一天只跑一次），确保能汇总到当日总结
        if _schedule.should_run(now, self.llm_summary_time,
                                self._sched_state.get("brief_date", ""),
                                self.llm_briefing_enabled):
            logger.info("[简报] 开始生成定时简报")
            self._sched_state["brief_date"] = today
            self._save_sched_state()
            try:
                await self._run_daily_briefing()
            except Exception as e:
                logger.error(f"[简报] 定时简报异常: {e}")

    # ---------------- 对话别名注册系统（v2.2.0） ----------------

    def _load_aliases(self) -> dict:
        """读取 aliases.json（不存在时返回空结构）。"""
        try:
            data = json.loads(self.aliases_file.read_text(encoding="utf-8"))
            return _aliases.normalize_loaded(data)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取别名文件失败（将按无别名处理）：{e}")
        return _aliases.empty_aliases()

    def _save_aliases(self) -> None:
        try:
            self.aliases_file.write_text(
                json.dumps(self.aliases, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logger.error(f"[别名] 保存 aliases.json 失败: {e}")

    def _alias_entry(self, chat: str, tid: str, create: bool = False):
        """获取（或创建）某目标的别名条目（委托 core.aliases）。"""
        return _aliases.get_entry(self.aliases, chat, tid, create)

    def _register_name(self, chat: str, tid: str, name: str) -> None:
        """记录/更新目标名称（群名或好友昵称）。"""
        if _aliases.register_name(self.aliases, chat, tid, name):
            self._save_aliases()

    def _aliases_of(self, chat: str, tid: str) -> list:
        return _aliases.aliases_of(self.aliases, chat, tid)

    def _target_label(self, chat: str, tid: str) -> str:
        """目标展示标签：「名称/别名 (群号或QQ号)」。"""
        return _aliases.target_label(self.aliases, chat, tid)

    def _alias_add(self, chat: str, tid: str, alias: str):
        """新增别名。返回错误信息；成功返回 None。"""
        err = _aliases.add(self.aliases, chat, tid, alias)
        if err is None:
            self._save_aliases()
        return err

    def _alias_remove(self, chat: str, tid: str, alias: str):
        """删除别名。返回错误信息；成功返回 None。"""
        err = _aliases.remove(self.aliases, chat, tid, alias)
        if err is None:
            self._save_aliases()
        return err

    def _alias_rename(self, chat: str, tid: str, old: str, new: str):
        """修改别名（把 old 改成 new）。返回错误信息；成功返回 None。"""
        err = _aliases.rename(self.aliases, chat, tid, old, new)
        if err is None:
            self._save_aliases()
        return err

    def _resolve_target(self, text: str, prefer_chat: str = ""):
        """把「群号 / QQ号 / 别名」解析为 (chat, tid)。
        返回 None = 未找到；返回 ("AMBIGUOUS", hits) = 别名歧义。"""
        return _aliases.resolve(self.aliases, text, prefer_chat)

    def _alias_hint(self, text: str) -> str:
        """别名歧义时的用户提示。"""
        return _aliases.hint(self.aliases, text)

    def _resolve_gid(self, raw: str, kind: str = "group"):
        """工具入口的目标解析：支持群号或别名。
        返回 (gid, None)；失败时 (None, 错误提示)。"""
        return _aliases.resolve_gid(self.aliases, raw, kind)

    def _sync_alias_config(self) -> None:
        """把配置项 aliases（形如「别名,群号」）合并进 aliases.json。"""
        added = 0
        for item in self._alias_cfg:
            alias, target = _aliases.parse_config_item(item)
            if not target or not target.isdigit():
                continue
            chat = "group"
            for c in _aliases.CHATS:
                if self._alias_entry(c, target):
                    chat = c
                    break
            self._alias_entry(chat, target, create=True)
            if alias and self._alias_add(chat, target, alias) is None:
                added += 1
        if added:
            logger.info(f"[别名] 已从配置同步 {added} 个别名")

    async def _flush_alias_config(self) -> None:
        """把别名回写配置项并保存（保持 WebUI 显示同步）。"""
        try:
            self.config["aliases"] = _aliases.to_config_lines(self.aliases)
        except Exception:
            return
        try:
            saver = getattr(self.config, "save_config_async", None)
            if callable(saver):
                await saver()
            else:
                self.config.save_config()
        except Exception as e:
            logger.error(f"[别名] 回写配置失败: {e}")

    async def _refresh_target_names(self, clients: list) -> int:
        """从后端刷新已归档目标的名称（群名/好友昵称）。"""
        if not clients:
            return 0
        groups: dict = {}
        friends: dict = {}
        for _pid, _qq, client in clients:
            try:
                for g in (await client.call_action("get_group_list") or []):
                    gid = str(g.get("group_id", ""))
                    if gid and gid not in groups:
                        groups[gid] = str(g.get("group_name") or "")
            except Exception:
                pass
            try:
                for f in (await client.call_action("get_friend_list") or []):
                    uid = str(f.get("user_id", ""))
                    if uid and uid not in friends:
                        friends[uid] = str(f.get("nickname")
                                           or f.get("remark") or "")
            except Exception:
                pass
        n = 0
        for chat, tid in self._scan_archived_targets():
            if chat == "group" and tid in groups:
                self._register_name("group", tid, groups[tid])
                n += 1
            elif chat == "private" and tid in friends:
                self._register_name("private", tid, friends[tid])
                n += 1
        if n:
            logger.info(f"[别名] 已刷新 {n} 个归档目标的名称")
        return n

    async def _refresh_names_safe(self) -> None:
        """启动时后台刷新目标名称（等待客户端就绪，异常不外抛）。"""
        try:
            clients: list = []
            for i in range(3):
                try:
                    clients = await self._get_clients()
                except Exception:
                    clients = []
                if clients:
                    break
                if i < 2:
                    await asyncio.sleep(10)
            if clients:
                await self._refresh_target_names(clients)
        except Exception as e:
            logger.error(f"[别名] 刷新目标名称失败: {e}")

    def _alias_list_text(self) -> str:
        targets = self._scan_archived_targets()
        if not targets:
            return "📭 暂无已归档目标（归档过一次后会自动登记名称）。"
        lines = ["📇 归档目标与别名："]
        for chat, tid in targets:
            als = self._aliases_of(chat, tid)
            lines.append(f"• {self._target_label(chat, tid)}｜别名："
                         f"{'、'.join(als) if als else '（无）'}")
        lines.append("提示：可以让 bot「给 XX 加个别名 YY」来管理别名。")
        return "\n".join(lines)

    def _archived_targets_text(self) -> str:
        targets = self._scan_archived_targets()
        if not targets:
            return "📭 暂无已归档目标。"
        lines = ["📦 归档目标清单："]
        total = 0
        for chat, tid in targets:
            prefix = "private_" if chat == "private" else ""
            files = sorted(self.export_dir.glob(f"napcat_{prefix}{tid}_*.jsonl"))
            days = len(files)
            rows = 0
            size = 0
            for fp in files:
                try:
                    size += fp.stat().st_size
                    rows += _store._count_newlines(fp)  # P2-1：字节计数
                except Exception:
                    continue
            total += rows
            als = self._aliases_of(chat, tid)
            lines.append(
                f"• {self._target_label(chat, tid)}｜别名："
                f"{'、'.join(als) if als else '无'}｜{days} 天｜"
                f"{rows} 条｜{size / 1024 / 1024:.1f} MB")
        lines.append(f"\n合计：{len(targets)} 个目标，{total} 条消息。")
        return "\n".join(lines)

    def _scan_archived_targets(self) -> list:
        """v2.1.0: 扫描导出目录，收集已有归档记录的目标（委托 core.store）。"""
        return _store.scan_archived_targets(self.export_dir)

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        if not self.admin_only:
            return True
        return event.is_admin()

    async def _get_client(self):
        """获取 aiocqhttp 平台的 CQHttp 客户端（用于调用历史消息扩展 API）。

        v1.5.0 起遍历全部 aiocqhttp 平台实例（旧版 get_platform 在多个实例时
        只会返回第一个，已弃用）。单 bot 行为与旧版一致；多 bot 时返回第一个
        “启用归档”的实例（事件链路请用 _client_for_event 按 self_id 精确路由）。
        """
        if self._client is not None:
            return self._client
        clients = await self._get_clients()
        if not clients:
            logger.warning(
                "未找到可用的 aiocqhttp 平台实例，无法进行定时导出。"
                "请确认 AstrBot 已启用 aiocqhttp 适配器连接 NapCat/SnowLuma。")
            return None
        self._client = clients[0][2]
        logger.info("已获取 aiocqhttp 客户端（CQHttp），可用于调用历史消息 API")
        return self._client

    def _aiocqhttp_platforms(self) -> list:
        """枚举全部 aiocqhttp 平台实例（v4 的 get_platform 已弃用且多实例时
        只返回第一个匹配，因此直接读取 platform_manager.platform_insts）。"""
        try:
            mgr = getattr(self.context, "platform_manager", None)
            insts = list(getattr(mgr, "platform_insts", None) or [])
        except Exception:
            insts = []
        if not insts:
            # 拿不到 platform_manager 的旧版 AstrBot：回退 get_platform
            try:
                p = self.context.get_platform("aiocqhttp")
            except Exception:
                p = None
            return [p] if p is not None else []
        out = []
        for p in insts:
            try:
                if p.meta().name != "aiocqhttp":
                    continue
            except Exception:
                continue
            out.append(p)
        return out

    async def _bot_qq(self, client, pid: str, cache_key: str = "") -> str:
        """获取某平台实例的登录 QQ 号（self_id）；失败返回空串。结果缓存 600s。

        P2-5: 缓存键不能只用平台实例 id —— 若 `meta().id` 缺失，多个实例
        都会得到空串 ""，导致后写的 QQ 覆盖前一个，`archive_bots` 过滤与
        `self_id` 路由全部出错。调用方可传 `cache_key`（通常带实例身份）
        作为兜底；为空时退回 `pid`。
        """
        key = cache_key or pid
        now = time.time()
        if now - self._qq_cache_ts > 600:
            self._qq_cache = {}
            self._qq_cache_ts = now
        if key in self._qq_cache:
            return self._qq_cache[key]
        try:
            info = await client.call_action("get_login_info")
            qq = str((info or {}).get("user_id") or "")
        except Exception:
            qq = ""
        self._qq_cache[key] = qq
        return qq

    def _bot_enabled(self, pid: str, qq: str) -> bool:
        """archive_bots 过滤：留空 = 全部实例都归档；
        否则按平台实例 id（WebUI 平台配置的 id）或登录 QQ 号匹配。"""
        if not self.archive_bots:
            return True
        return pid in self.archive_bots or (bool(qq) and qq in self.archive_bots)

    async def _get_clients(self) -> list:
        """返回 [(platform_id, qq, client)]：所有“启用归档”的 aiocqhttp 实例。"""
        out = []
        for idx, p in enumerate(self._aiocqhttp_platforms()):
            try:
                pid = str(p.meta().id or "")
                client = p.get_client()
            except Exception:
                continue
            if client is None:
                continue
            # P2-5: pid 缺失时用「该实例在列表中的位置」兜底，
            # 避免多个实例共用空串键而串号
            cache_key = pid if pid else f"idx:{idx}"
            qq = await self._bot_qq(client, pid, cache_key=cache_key)
            if not self._bot_enabled(pid, qq):
                logger.info(f"[NapCatExporter] bot 未启用归档"
                            f"（archive_bots 过滤）: 实例id={pid or cache_key} "
                            f"qq={qq or '未知'}")
                continue
            out.append((pid, qq, client))
        return out

    async def _client_for_event(self, event: AstrMessageEvent):
        """按事件所属 bot（self_id=QQ 号）路由到对应实例。

        返回 (client, platform_id, qq)；事件来自未被 archive_bots 启用的
        bot、或找不到对应实例时返回 (None, "", "")。
        """
        clients = await self._get_clients()
        if not clients:
            return (None, "", "")
        sid = None
        try:
            mobj = event.get_message_obj()
            sid = getattr(mobj, "self_id", None)
        except Exception:
            pass
        if sid:
            sid = str(sid)
            for pid, qq, client in clients:
                if qq and qq == sid:
                    return (client, pid, qq)
            # 事件来自未启用归档的 bot
            return (None, "", "")
        # 旧适配器事件无 self_id：单实例返回唯一；多实例返回第一个
        return (clients[0][2], clients[0][0], clients[0][1])

    def _backend_now(self) -> str:
        """当前生效的后端。显式配置（backend: napcat/snowluma）优先；
        auto 在未探测完成前按 napcat 参数发出首页请求（两后端均会返回最新一页），
        收到数据后由 _probe_backend 修正缓存。"""
        if self.backend_cfg in ("napcat", "snowluma"):
            return self.backend_cfg
        return self._backend or "napcat"

    @staticmethod
    def _probe_backend(msgs: list) -> str | None:
        """auto 后端探测（委托 core.backend.probe_backend）：
        NapCat : 消息带 real_seq 字段，且 message_seq == message_id；
        SnowLuma: 无 real_seq，message_id 为 int32 哈希（≠单调会话号 message_seq）。"""
        return _backend_mod.probe_backend(msgs)

    def _segments_to_text(self, segments, nickmap=None) -> str:
        """消息段 → 文本（委托 core.text.segments_to_text）。

        v2.3.0：若提供昵称映射（QQ号 → 昵称），@ 消息会渲染为 @昵称，
        否则保留 [At:QQ号] 占位（页面端会用同一映射再做兜底替换）。
        """
        return _text.segments_to_text(segments, nickmap)

    def _fmt_record(self, msg: dict, chat: str, target_id: str, nickmap=None) -> dict:
        """OneBot 消息 → JSONL 行（委托 core.text.fmt_record）。"""
        return _text.fmt_record(msg, chat, target_id, nickmap)

    @staticmethod
    def _reply_of(msg: dict) -> dict | None:
        """抽取引用（reply）消息信息（委托 core.text.reply_of）。"""
        return _text.reply_of(msg)

    def _date_of(self, msg: dict) -> str:
        return _text.date_of(msg)

    def _target_path(self, chat: str, target_id: str, date: str) -> Path:
        # 按天分文件：napcat_<群号>_YYYY-MM-DD.jsonl（私聊 napcat_private_<QQ>_*）
        return _store.target_path(self.export_dir, chat, target_id, date)

    def _split_merged_files(self) -> None:
        """v1.2.0 迁移：单文件拆回按天文件（委托 core.store.split_merged_files）。"""
        _store.split_merged_files(self.export_dir)

    def _merge_write(self, path: Path, records: list) -> int:
        """v1.3.3 合并写入（委托 core.store.merge_write）。"""
        return _store.merge_write(path, records)

    async def _fetch(self, action: str, key: str, target_id: str,
                     start: int, count: int, client=None) -> list:
        """调用后端扩展 API（get_group_msg_history / get_friend_msg_history）
        拉取一页历史消息。

        v1.5.0 参数差异：
          NapCat  : group_id/user_id/message_seq 均为字符串，message_seq=0 取最新；
          SnowLuma: group_id/user_id 传数值，锚点参数名为 message_id
                    （int32 哈希，0=最新；未知参数会被静默忽略）。
        start 为内部锚点：napcat = 序号（real_seq/message_seq），
        snowluma = message_id 哈希。auto 模式下首页尚未探测完成前按 napcat
        参数发出（两后端均会返回最新一页，无数据丢失），收到数据后由
        _probe_backend 完成探测并缓存。

        aiocqhttp 的 call_action 已自动解包，resp 可能是：
          a) {"messages": [...]}                    —— 后端实际返回（解包后）
          b) {"data": {"messages": [...]}}          —— 标准 OneBot 包装
          c) {"data": [...]}                        —— data 直接是列表
          d) {"status": "failed", "retcode": 1400}  —— 调用失败
        """
        if client is None:
            client = await self._get_client()
        if client is None:
            return []
        backend = self._backend_now()
        try:
            if backend == "snowluma":
                tid = target_id
                if str(tid).isdigit():
                    tid = int(tid)
                resp = await client.call_action(
                    action, **{key: tid, "message_id": int(start),
                               "count": count})
            else:
                resp = await client.call_action(
                    action, **{key: str(target_id), "message_seq": str(start),
                               "count": count})
        except Exception as e:
            # v1.3.2: NapCat 翻页锚点消息不存在（retcode=1200，如 message_seq 传了
            # 不存在的 id）属预期行为 → 降级为 warning，停止翻页（已获取的消息保留）
            msg = str(e)
            if "不存在" in msg or getattr(e, "retcode", None) == 1200:
                logger.warning(f"{action}({target_id}) 翻页停止: {msg}")
            else:
                logger.error(f"调用 {action}({target_id}) 失败: {e}")
            return []
        if not isinstance(resp, dict):
            logger.warning(f"{action}({target_id}) 返回异常: {resp!r}")
            return []
        # 失败检测
        retcode = resp.get("retcode")
        if resp.get("status") == "failed" or (retcode is not None and retcode != 0):
            logger.warning(
                f"{action}({target_id}) 调用失败: retcode={retcode} "
                f"message={resp.get('message')!r} wording={resp.get('wording')!r}")
            return []
        # 解析消息列表（兼容三种结构）
        msgs = None
        if "messages" in resp:
            msgs = resp.get("messages")
        else:
            data = resp.get("data")
            if isinstance(data, dict):
                msgs = data.get("messages")
            elif isinstance(data, list):
                msgs = data
        if msgs is None:
            logger.warning(f"{action}({target_id}) 返回格式无法解析: {str(resp)[:200]}")
            return []
        if not msgs:
            logger.warning(
                f"{action}({target_id}) 返回空消息列表"
                f"（NapCat/SnowLuma 本地可能无该会话的消息记录/AIO 缓存）")
            return []
        # v1.5.0: auto 后端探测（首次拿到有效数据时）
        if self._backend is None and self.backend_cfg == "auto":
            self._backend = self._probe_backend(msgs)
            if self._backend:
                logger.info(f"[NapCatExporter] 后端自动探测: {self._backend}"
                            f"（可用配置项 backend 覆盖）")
        return msgs

    @staticmethod
    def _seq_of(msg: dict) -> int:
        return _backend_mod.seq_of(msg)

    @staticmethod
    def _anchor_of(msg: dict) -> int | None:
        """v1.3.4: 翻页定位锚点（委托 core.backend.anchor_of）——优先 real_seq
        （会话内序号，单调递增，NapCat 内部用其定位），否则回退 message_id。"""
        return _backend_mod.anchor_of(msg)

    @staticmethod
    def _mid_of(msg: dict) -> str:
        """消息唯一 ID（用于去重）。"""
        return _backend_mod.mid_of(msg)

    @staticmethod
    def _time_of(msg: dict) -> int:
        return _backend_mod.time_of(msg)

    async def _export_target(self, chat: str, target_id: str,
                             limit: int = 0, today_only: bool = False,
                             start_ts: int = 0, end_ts: int = 0,
                             client=None, bot_tag: str = "",
                             ignore_seen: bool = False) -> int:
        """导出单个目标。
        limit > 0：按需导出最近 limit 条（去重追加，同时更新游标）；
        limit = 0：增量导出（以 time 时间戳为边界 + message_id 去重）。
        today_only=True（自动归档）：只归档当天消息，不导历史。
        start_ts/end_ts > 0：回溯导出 [start_ts, end_ts] 时间段消息
        （手动归档，不推进游标，不影响后续增量）。
        client：指定使用的后端客户端（多 bot 场景按 bot 路由）；None 时自动获取。
        bot_tag：日志标识（如 bot[QQ 号]），用于多 bot 场景区分来源。
        ignore_seen（v2.1.0）：回溯时忽略 state 的已归档 id 过滤——
            补全缺口（文件被删/截断）场景下，去重以文件内容为权威。
        返回本次新增写入的条数（v2.1.0：与既有文件合并后的实际新增数）。
        注意：NapCat 返回的 message_seq = message_id（全局消息 ID），
        并非单调递增，不能用作增量游标；因此使用 time 做边界，
        并用已写 message_id 集合做去重（避免同秒消息重复/遗漏）。
        """
        if client is None:
            client = await self._get_client()
            if client is None:
                logger.warning("未获取到 aiocqhttp 客户端，跳过导出")
                return 0
        tag = f" [{bot_tag}]" if bot_tag else ""
        action = "get_group_msg_history" if chat == "group" \
            else "get_friend_msg_history"
        key = "group_id" if chat == "group" else "user_id"
        # 游标结构: {"t": 最后导出消息的time, "ids": [已写message_id, 最多5000]}
        st = self._state.get(chat, {}).get(target_id)
        if isinstance(st, dict):
            last_t = int(st.get("t") or 0)
            seen = set(st.get("ids") or [])
        else:
            if st:  # 旧格式 int 游标（message_id 不单调，不可用）
                logger.info(f"[NapCatExporter]{tag} {target_id} 检测到旧格式游标 "
                            f"{st!r}（message_id 不单调，已重置为 0，将全量补导）")
            last_t = 0
            seen = set()
        # 循环导出模式：只归档当天（本地时区 0 点起）
        today_start = 0
        if today_only:
            today_start = int(datetime.combine(
                datetime.now().date(), datetime.min.time()).timestamp())
        # NapCat 扩展 API：message_seq=0 表示从最新消息开始，只能往回翻页
        start = 0
        pager = _pager.Pager(
            limit=limit, today_only=today_only, start_ts=start_ts,
            end_ts=end_ts, last_t=last_t, seen=seen,
            ignore_seen=ignore_seen, today_start=today_start)
        while True:
            msgs = await self._fetch(action, key, target_id, start,
                                     self.batch, client=client)
            decision = pager.decide_page(msgs, self._backend_now())
            if decision.reason == "超过 600 页":
                logger.warning(f"[NapCatExporter] {target_id} 翻页超过 600 页，强制停止")
            if decision.stop:
                break
            if decision.next_anchor is not None:
                start = decision.next_anchor
        fetched = pager.fetched
        if not fetched:
            return 0
        # v2.1.0: 写入段串行化（启动归档检查 / 手动 / 循环任务并发防护）
        async with self._file_lock:
            # 按 time 排序
            new = pager.final_records(limit)
            # 按天分文件写入（v1.3.3: 统一合并-去重-排序-重写，文件自愈）
            written = 0
            by_date: dict = {}
            for m in new:
                by_date.setdefault(self._date_of(m), []).append(m)
            # v2.3.0：用昵称映射把 @ 渲染为 @昵称，并增量维护映射
            nickmap = self._nick_map(chat, target_id)
            nick_dirty = False
            for m in new:
                snd = m.get("sender") or {}
                uid = str(snd.get("user_id", "") or "")
                nm = str(snd.get("card") or snd.get("nickname") or "").strip()
                if uid and nm and nickmap.get(uid) != nm:
                    nickmap[uid] = nm
                    nick_dirty = True
            for date, msgs in by_date.items():
                path = self._target_path(chat, target_id, date)
                records = [self._fmt_record(m, chat, target_id, nickmap) for m in msgs]
                written += self._merge_write(path, records)  # v2.1.0: 实际新增数
            if nick_dirty:
                self._save_nicks(chat, target_id)
            # 更新游标：time 边界 + 最近 5000 个已写 message_id
            max_t = max(self._time_of(m) for m in new)
            merged_ids = pager.merge_ids(seen)
            cur = self._state.setdefault(chat, {})
            if today_only:
                # v1.3.2: 当天模式游标不推进（固定为今天 0 点 -1 秒），
                # 每轮都尝试拉取当天全部消息，配合 message_id 去重：
                # 翻页失败时也不漏"最新"消息，翻页可用时能补全当天更早的
                cur[target_id] = {"t": today_start - 1, "ids": merged_ids}
            elif start_ts > 0:
                # v1.4.0: 回溯时间段归档不推进游标（不影响后续增量），仅更新去重 ids
                cur[target_id] = {"t": last_t, "ids": merged_ids}
            else:
                # P2-7: limit 模式下可能抓到「更早但从未见过」的消息，
                # 直接写 max_t 会让游标回退，下一轮重复拉取（浪费 API 配额）。
                # 游标必须单调不减。
                cur[target_id] = {"t": max(last_t, max_t), "ids": merged_ids}
            self._save_state()
            return written

    async def _auto_export_once(self, apply_rules: bool = True) -> int:
        """定时一轮：遍历所有“启用归档”的 aiocqhttp 实例（多 bot），
        各自导出其群/私聊增量消息。
        apply_rules=True（循环导出模式）：应用群白名单、只归档当天消息；
        apply_rules=False（用户/LLM 手动触发全量增量）：不过滤，可导历史。

        v1.5.0 多 bot：archive_bots 留空=全部实例；同一轮中不同 bot 出现
        的相同群/好友只导出一次（同群消息对所有 bot 一致，避免重复拉取）。
        """
        clients = await self._get_clients()
        if not clients:
            logger.warning("未获取到可用的 aiocqhttp 客户端，本轮跳过")
            return 0
        total = 0
        seen_targets: set = set()  # 本轮已导出目标（跨 bot 去重）
        for pid, qq, client in clients:
            bot_tag = f"bot[{qq or pid or '?'}]"
            # ---------------- 群 ----------------
            try:
                groups = await client.call_action("get_group_list")
            except Exception as e:
                logger.error(f"[{bot_tag}] 获取群列表失败: {e}")
                groups = []
            groups = groups or []
            if apply_rules and self.whitelist:
                before = len(groups)
                groups = [g for g in groups
                          if str(g.get("group_id", "")) in self.whitelist]
                if len(groups) != before:
                    logger.info(f"[{bot_tag}] 白名单过滤：{before} 个群 → "
                                f"{len(groups)} 个（仅白名单内导出）")
                if not groups:
                    logger.info(f"[{bot_tag}] 白名单内没有可导出的群，跳过")
                    continue
            logger.info(f"定时导出 [{bot_tag}]：共 {len(groups)} 个群")
            for g in groups:
                gid = str(g.get("group_id", ""))
                if not gid:
                    continue
                self._register_name("group", gid, g.get("group_name") or "")
                if gid in seen_targets:
                    continue  # 同一轮其他 bot 已导出（同群消息一致）
                seen_targets.add(gid)
                n = 0  # P0-1: 前置初始化，避免导出抛异常时 total += n 引发 NameError
                       # 中断整轮归档（该异常会让本轮剩余目标与私聊全部跳过）
                try:
                    n = await self._export_target(
                        "group", gid, today_only=apply_rules,
                        client=client, bot_tag=bot_tag)
                    if n:
                        logger.info(f"[{bot_tag}] 群 {gid} 新增 {n} 条")
                except Exception as e:
                    logger.error(f"[{bot_tag}] 导出群 {gid} 失败: {e}")
                total += n
            # ---------------- 私聊 ----------------
            if self.auto_friends:
                try:
                    friends = await client.call_action("get_friend_list")
                except Exception as e:
                    logger.error(f"[{bot_tag}] 获取好友列表失败: {e}")
                    friends = []
                # v2.4.0: 按「私聊归档白名单」筛选，不再无差别遍历全部好友
                # （僵尸账号每轮空调用既浪费配额又刷 WARN）
                picked = self._pick_private_targets(friends)
                if picked:
                    logger.info(f"[{bot_tag}] 私聊巡检 {len(picked)} 个目标"
                                + ("（白名单模式）" if self.private_whitelist
                                   else "（仅已登记目标）"))
                for uid, nick in picked:
                    self._register_name("private", uid, nick)
                    if uid in seen_targets:
                        continue
                    seen_targets.add(uid)
                    n = 0  # P0-1: 同群分支，前置初始化避免 NameError 中断本轮
                    try:
                        n = await self._export_target(
                            "private", uid, today_only=apply_rules,
                            client=client, bot_tag=bot_tag)
                        if n:
                            logger.info(f"[{bot_tag}] 私聊 {uid} 新增 {n} 条")
                    except Exception as e:
                        logger.error(f"[{bot_tag}] 导出私聊 {uid} 失败: {e}")
                    total += n
        logger.info(f"定时导出完成，本轮新增 {total} 条（目录: {self.export_dir.resolve()}）")
        return total

    def _pick_private_targets(self, friends: list) -> list:
        """决定本轮要巡检哪些私聊目标（v2.4.0 私聊白名单）。

        规则：
        - `private_whitelist` 非空 → **严格**只用名单内的 QQ（即便后端好友列表里没有）
        - `private_whitelist` 留空 → 只巡视「已登记」的目标：有归档文件、有游标、
          或有别名条目。这样已清理 / 已失效的僵尸账号不会被每轮反复空调用。

        为什么不再遍历全部好友：SnowLuma/NapCat 的 `get_friend_list` 会返回
        大量无聊天记录的账号，逐个 `get_friend_msg_history` 只会空返回并刷 WARN，
        白白消耗 API 配额（现网 `2490530663` 即此情况）。
        """
        names = {}
        for f in friends or []:
            uid = str((f or {}).get("user_id", "") or "")
            if uid:
                names[uid] = (f or {}).get("nickname") or ""

        ids = set()
        if self.private_whitelist:
            for raw in self.private_whitelist:
                s = str(raw).strip()
                if s:
                    ids.add(s)
        else:
            # 已登记：别名条目
            for tid in (self.aliases.get("private") or {}):
                ids.add(str(tid))
            # 已登记：游标
            for tid in (self._state.get("private") or {}):
                ids.add(str(tid))
            # 已登记：有归档文件
            for chat, tid in self._scan_archived_targets():
                if chat == "private":
                    ids.add(str(tid))
        # 名单里有、但后端没返回昵称的，仍要巡检（好友列表可能不含它）
        return [(uid, names.get(uid, "")) for uid in sorted(ids)]

    def _cleanup_old_files(self) -> None:
        """v1.3.0: 自动清理过期历史文件（委托 core.store.cleanup_old_files）。

        删除文件名日期早于（今天 - clean_days）的 napcat_*_YYYY-MM-DD.jsonl；
        手动归档（protected）过的目标文件不清理。
        """
        _store.cleanup_old_files(self.export_dir, self._state, self.clean_days,
                                 self.auto_clean)

    async def _startup_verify_once(self) -> int:
        """v2.1.0: 启动归档检查——对已归档目标逐天回溯补全最近 verify_days 天缺口。

        对导出目录中已有归档的目标（群/私聊），逐天拉取最近 N 天消息并与现有
        文件幂等合并（按 id 去重）：
          - 整天空洞（文件缺失但该天有消息）→ 生成补全；
          - 记录不全（文件存在但缺少部分消息）→ 补齐差值。
        超出自动清理保留期的天跳过；后端（SL/NapCat）本地无记录的消息无法补。

        P1-5（v2.4.0）：
        - **可续跑**：每个 (目标, 日期) 完成后立即落盘 verify_state.json，
          重载/中断后从断点继续，不再从头重跑数小时。
        - **工作量可见**：日志输出「N 目标 × M 天 = K 次检查」，并报告进度。
        - **窗口指纹**：目标集合与天数未变且上次已跑完 → 直接跳过，避免
          每次「保存配置 / 重载插件」都触发一次长任务。
        - **低优先级**：每项之间让出事件循环（0.3s），并可被 cancel 中断。
        """
        if not self.startup_verify:
            return 0
        targets = self._scan_archived_targets()
        if not targets:
            logger.info("[归档检查] 导出目录暂无已归档目标，跳过检查")
            return 0

        today = datetime.now().date()
        wk = _verify.window_key(targets, self.verify_days)
        # 窗口未变且上次已完整跑完 → 本次直接跳过
        if (self._verify_state.get("window") == wk
                and self._verify_state.get("finished_at")):
            logger.info(f"[归档检查] 本轮窗口与上次相同（{_verify.format_workload(targets, self.verify_days)}），"
                        f"上次已完成于 {self._verify_state['finished_at']}，跳过重复检查")
            return 0

        # 等平台连接就绪（最多 3 次尝试，间隔 10s）
        clients = []
        for i in range(3):
            try:
                clients = await self._get_clients()
            except Exception:
                clients = []
            if clients:
                break
            if i < 2:
                await asyncio.sleep(10)
        if not clients:
            logger.warning("[归档检查] 未获取到可用的 aiocqhttp 客户端，跳过本次检查")
            return 0

        # 群 → 首选客户端映射（多 bot 场景：优先用能看到该群的实例拉取）
        gid2client: dict = {}
        for pid, qq, client in clients:
            try:
                groups = await client.call_action("get_group_list") or []
            except Exception:
                groups = []
            for g in groups:
                gid = str(g.get("group_id", ""))
                if gid and gid not in gid2client:
                    gid2client[gid] = (pid, qq, client)

        # 规划本轮要检查的项（跳过已完成 / 超出保留期）
        items, skipped_done, skipped_retention = _verify.plan_items(
            targets, self.verify_days, today, self._verify_state,
            auto_clean=self.auto_clean, clean_days=self.clean_days)
        est = _verify.format_workload(targets, self.verify_days)
        logger.info(f"[归档检查] 启动检查开始：{est}"
                    f"（已完成 {skipped_done} 项已跳过"
                    + (f"，{skipped_retention} 项超出保留期" if skipped_retention else "")
                    + f"，本次待检查 {len(items)} 项）")
        if not items:
            logger.info("[归档检查] 所有项均已完成，本次无需检查")
            self._verify_state["window"] = wk
            self._verify_state["finished_at"] = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S")
            self._save_verify_state()
            return 0

        total = 0
        t0 = time.time()
        ok = 0
        for idx, it in enumerate(items, 1):
            day = it["day"]
            chat, tid = it["chat"], it["tid"]
            label = f"群 {tid}" if chat == "group" else f"私聊 {tid}"
            if chat == "group" and tid in gid2client:
                pid, qq, client = gid2client[tid]
            else:
                pid, qq, client = clients[0]
            bot_tag = f"bot[{qq or pid or '?'}]"
            start_ts = int(datetime.combine(day, datetime.min.time()).timestamp())
            end_ts = int(datetime.combine(day, datetime.max.time()).timestamp())
            if day == today:
                end_ts = int(time.time())
            try:
                n = await self._export_target(
                    chat, tid, start_ts=start_ts, end_ts=end_ts,
                    client=client, bot_tag=bot_tag, ignore_seen=True)
            except asyncio.CancelledError:
                # 被重载/停用中断：已完成的项已落盘，下次可续跑
                logger.info(f"[归档检查] 被中断（已完成 {ok}/{len(items)} 项，"
                            f"下次可从断点继续）")
                raise
            except Exception as e:
                logger.error(f"[归档检查] {label} {day} 检查失败: {e}")
                n = 0
            if n:
                logger.info(f"[归档检查] {label} {day} 补齐 {n} 条")
            total += n
            ok += 1
            # 每完成一项立刻落盘：续跑的最小粒度，代价是可接受的写盘量
            _verify.mark_done(self._verify_state, it["key"])
            self._save_verify_state()
            if idx % 20 == 0 or idx == len(items):
                logger.info(f"[归档检查] 进度 {idx}/{len(items)}"
                            f"（{idx * 100 // max(1, len(items))}%），累计补齐 {total} 条")
            await asyncio.sleep(0.3)
        # 全部跑完：裁掉窗口外的历史键，写入完成标记与窗口指纹
        _verify.prune(self._verify_state, targets, self.verify_days, today)
        self._verify_state["window"] = wk
        self._verify_state["finished_at"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S")
        self._save_verify_state()
        logger.info(f"[归档检查] 完成：{ok} 项检查，共补齐 {total} 条，"
                    f"耗时 {time.time() - t0:.0f}s")
        return total

    async def _verify_safe(self) -> None:
        """v2.1.0: 启动检查的安全包装（异常不外抛，不影响主流程）。"""
        try:
            await self._startup_verify_once()
        except Exception as e:
            logger.error(f"[归档检查] 启动检查异常: {e}")

    async def _auto_loop(self) -> None:
        logger.info(f"定时导出循环已启动，间隔 {self.interval}s，"
                    f"导出目录: {self.export_dir.resolve()}"
                    + (f"，白名单: {self.whitelist}" if self.whitelist else "")
                    + (f"，自动清理: {self.clean_days}天前"
                       if self.auto_clean else "，自动清理: 关闭"))
        # v2.1.0: 启动归档检查（一次性；与定时循环同一任务，天然串行）
        if self.startup_verify:
            await self._verify_safe()
        while True:
            try:
                await self._auto_export_once()
            except Exception as e:
                logger.error(f"定时导出异常: {e}")
            # v1.3.1: 自动清理每 12 小时执行一次（非每轮）
            if self._last_clean is None or \
                    datetime.now() - self._last_clean >= timedelta(hours=12):
                try:
                    self._cleanup_old_files()
                    self._last_clean = datetime.now()
                except Exception as e:
                    logger.error(f"自动清理异常: {e}")
            # v2.4.0 (P1-1): 每日定时总结/简报（每天各最多一次）
            try:
                await self._daily_sched_check()
            except Exception as e:
                logger.error(f"[总结] 定时调度异常: {e}")
            await asyncio.sleep(self.interval)

    # ---------------------------------------------------------------
    # 生命周期
    # ---------------------------------------------------------------

    async def initialize(self) -> None:
        # v2.2.0: 别名系统初始化（加载 + 同步配置 + 后台刷新名称）
        try:
            self.aliases = self._load_aliases()
            self._sync_alias_config()
            # 持有任务引用，避免被 GC 提前回收（RUF006）
            self._names_task = asyncio.create_task(self._refresh_names_safe())
        except Exception as e:
            logger.error(f"[别名] 初始化异常: {e}")
        # v2.3.0: 注册插件页面（控制台）API
        try:
            self._register_web_apis()
        except Exception as e:
            logger.error(f"[页面] 注册 Web API 失败: {e}")
        # P2-1: 后台预热行数索引（全新安装时首次打开总览不必等全量统计）
        try:
            self._warm_task = asyncio.create_task(self._warm_row_index())
        except Exception as e:
            logger.error(f"[页面] 启动索引预热失败: {e}")
        if self.auto_export:
            self._task = asyncio.create_task(self._auto_loop())
            logger.info("NapCat 历史导出器已启动自动归档（每 %ss 增量导出一次，"
                        "导出目录: %s）", self.interval, self.export_dir.resolve())
        else:
            logger.info("NapCat 历史导出器自动归档已关闭，"
                        "仅在被 LLM 工具触发时归档（导出目录: %s）",
                        self.export_dir.resolve())
            # v2.1.0: 自动归档关闭时，启动检查仍按配置执行（独立任务）
            if self.startup_verify:
                self._verify_task = asyncio.create_task(self._verify_safe())
            # v2.4.0 (P1-1): 自动归档关闭时，定时总结/简报仍需运行
            if self.llm_summary_enabled or self.llm_briefing_enabled:
                self._sched_task = asyncio.create_task(self._sched_loop())
        if self.llm_summary_enabled or self.llm_briefing_enabled:
            logger.info("[总结] 定时任务已启用：每日 %s 生成总结%s",
                        self.llm_summary_time.strftime("%H:%M"),
                        "（含简报）" if self.llm_briefing_enabled else "")

    async def terminate(self) -> None:
        # P2-6: 逐一取消全部后台任务（此前只取消 _task/_verify_task，
        # _names_task / _manual_task / _summary_task 会残留到新实例）。
        for attr in ("_task", "_verify_task", "_sched_task",
                     "_names_task", "_manual_task", "_summary_task",
                     "_warm_task"):
            task = getattr(self, attr, None)
            if not task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 取消 {attr} 时异常（可忽略）：{e}")
            setattr(self, attr, None)
        # P2-6: 队列里未消费的 job，其 future 若不处理会永久挂起
        q = getattr(self, "_summary_queue", None)
        if q is not None:
            while not q.empty():
                try:
                    job = q.get_nowait()
                except Exception:
                    break
                fut = (job or {}).get("future")
                if fut is not None and not fut.done():
                    fut.set_exception(_EmptyData("插件正在停止，任务已取消"))
                try:
                    q.task_done()
                except Exception:
                    pass

    # ---------------------------------------------------------------
    # LLM 工具
    # ---------------------------------------------------------------

    @filter.llm_tool("export_group_history")
    async def export_group_history(self, event: AstrMessageEvent,
                                   group_id: str, count: int = 200):
        '''
        按需导出指定 QQ 群的历史聊天记录为 JSONL 文件（图片/表情等用占位符）。
        适合需要把某群聊天记录保存成文件、供后续搜索/分析的场景。
        与定时模式共用同一套增量游标，重复导出不会产生大量重复数据。
        v1.5.0：多 bot 时按触发本消息的 bot（self_id）路由到对应实例归档，
        目标群必须属于该 bot（或用 get_export_status 查看归档了哪些 bot）。
        Args:
          group_id(string): 目标群（群号或别名，必填）
          count(number): 导出的消息条数上限（默认 200，最大 5000）
        返回: 导出摘要（新增条数、文件路径）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        gid, _err = self._resolve_gid(group_id, "group")
        if _err:
            return _err
        count = max(1, min(int(count), 5000))
        client, pid, qq = await self._client_for_event(event)
        if client is None:
            return ("❌ 未能定位触发此消息的 bot（未启用归档或未找到对应"
                    " aiocqhttp 实例），请检查 archive_bots 配置。")
        bot_tag = f"bot[{qq or pid or '?'}]"
        written = await self._export_target(
            "group", gid, limit=count, client=client, bot_tag=bot_tag)
        if written > 0:
            # v1.3.0: 手动归档过的目标加入保护名单，自动清理不删除其文件
            self._state.setdefault("protected", {}).setdefault("group", [])
            if gid not in self._state["protected"]["group"]:
                self._state["protected"]["group"].append(gid)
                self._save_state()
                logger.info(f"[NapCatExporter] 群 {gid} 已加入手动归档保护名单")
        path = self.export_dir
        return (f"✅ 群 {gid} 导出完成：新增 {written} 条"
                f"（{bot_tag}，导出目录: {path}）")
    @filter.llm_tool("export_private_history")
    async def export_private_history(self, event: AstrMessageEvent,
                                     user_id: str, count: int = 200):
        '''
        按需导出指定 QQ 好友的私聊历史记录为 JSONL 文件（图片/表情用占位符）。
        注意：后端（NapCat/SnowLuma）需保存有与该好友的聊天记录。
        v1.5.0：多 bot 时按触发本消息的 bot（self_id）路由到对应实例归档，
        目标好友必须属于该 bot。
        Args:
          user_id(string): 目标好友（QQ 号或别名，必填）
          count(number): 导出的消息条数上限（默认 200，最大 5000）
        返回: 导出摘要（新增条数、文件路径）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        uid, _err = self._resolve_gid(user_id, "private")
        if _err:
            return _err
        count = max(1, min(int(count), 5000))
        client, pid, qq = await self._client_for_event(event)
        if client is None:
            return ("❌ 未能定位触发此消息的 bot（未启用归档或未找到对应"
                    " aiocqhttp 实例），请检查 archive_bots 配置。")
        bot_tag = f"bot[{qq or pid or '?'}]"
        written = await self._export_target(
            "private", uid, limit=count, client=client, bot_tag=bot_tag)
        if written > 0:
            # v1.3.0: 手动归档过的目标加入保护名单，自动清理不删除其文件
            self._state.setdefault("protected", {}).setdefault("private", [])
            if uid not in self._state["protected"]["private"]:
                self._state["protected"]["private"].append(uid)
                self._save_state()
                logger.info(f"[NapCatExporter] 私聊 {uid} 已加入手动归档保护名单")
        return (f"✅ 与 {uid} 的私聊导出完成：新增 {written} 条"
                f"（{bot_tag}，导出目录: {self.export_dir}）")

    @filter.llm_tool("export_all_incremental")
    async def export_all_incremental(self, event: AstrMessageEvent,
                                     group_id: str = "",
                                     start_date: str = "",
                                     end_date: str = ""):
        '''
        立即归档（手动触发，不受自动归档开关/白名单/当天限制）：
        默认增量归档全部群；可指定群号与时间段回溯归档历史消息。
        v1.5.0：多 bot 时归档范围 = 触发本消息的 bot（self_id）所属实例
        的群；留空 group_id 时归档该 bot 的全部群。

        Args:
          group_id(string): 目标群（群号或别名，可选；留空=该 bot 全部群）
          start_date(string): 开始日期 YYYY-MM-DD（可选，留空=不限起点）
          end_date(string): 结束日期 YYYY-MM-DD（可选，留空=不限终点）
        返回: 归档摘要（新增条数、文件路径）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        client, pid, qq = await self._client_for_event(event)
        if client is None:
            return ("❌ 未能定位触发此消息的 bot（未启用归档或未找到对应"
                    " aiocqhttp 实例），请检查 archive_bots 配置。")
        bot_tag = f"bot[{qq or pid or '?'}]"
        start_ts = 0
        end_ts = 0
        try:
            if start_date:
                start_ts = int(datetime.strptime(
                    start_date.strip(), "%Y-%m-%d").timestamp())
            if end_date:
                end_ts = int(datetime.strptime(
                    end_date.strip() + " 23:59:59",
                    "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            return ("❌ 日期格式错误：应为 YYYY-MM-DD，例如 2026-08-20。")
        if start_ts and end_ts and start_ts > end_ts:
            return "❌ 开始日期不能晚于结束日期。"
        # 指定群
        if group_id:
            gid, _err = self._resolve_gid(group_id, "group")
            if _err:
                return _err
            written = await self._export_target(
                "group", gid, start_ts=start_ts, end_ts=end_ts,
                client=client, bot_tag=bot_tag, ignore_seen=True)
            return (f"✅ 群 {gid} 归档完成：新增 {written} 条"
                    f"（{bot_tag}，导出目录: {self.export_dir}）")
        # 该 bot 的全部群
        try:
            groups = await client.call_action("get_group_list")
        except Exception as e:
            logger.error(f"[{bot_tag}] 获取群列表失败: {e}")
            groups = []
        total = 0
        for g in groups or []:
            gid = str(g.get("group_id", ""))
            if not gid:
                continue
            try:
                n = await self._export_target(
                    "group", gid, start_ts=start_ts, end_ts=end_ts,
                    client=client, bot_tag=bot_tag, ignore_seen=True)
                total += n
            except Exception as e:
                logger.error(f"[{bot_tag}] 归档群 {gid} 失败: {e}")
        return (f"✅ 全部群归档完成，新增 {total} 条"
                f"（{bot_tag}，导出目录: {self.export_dir}）")

    @filter.llm_tool("get_export_status")
    async def get_export_status(self, event: AstrMessageEvent):
        '''
        查看导出器状态：模式、后端、归档的 bot、导出目录、各群/私聊的最新导出游标与文件数。
        返回: 状态摘要
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        exp = self.export_dir.resolve()
        files = sorted(p.name for p in exp.glob("*.jsonl"))
        lines = [
            f"自动归档: {'开启' if self.auto_export else '关闭'}"
            f"（间隔 {self.interval}s）",
            f"后端: {self._backend_now()}（配置 backend={self.backend_cfg}，"
            f"auto 探测结果: {self._backend or '尚未探测'}）",
            f"归档 bot 配置: {self.archive_bots if self.archive_bots else '(全部 aiocqhttp 实例)'}",
            f"导出目录(配置值): {self.export_dir!r}",
            *([f"数据目录迁移: {self._migrate_msg}"]
              if getattr(self, "_migrate_msg", "") else []),
            f"导出目录(绝对路径): {exp}",
            f"state.json 路径: {self.state_file.resolve()}",
            f"JSONL 文件数: {len(files)}",
        ]
        if files:
            lines.append("JSONL 文件列表:\n  " + "\n  ".join(files[:20]))
        # 列出导出目录内的所有内容（含 state.json）
        try:
            items = sorted(p.name for p in exp.iterdir())
            lines.append(f"导出目录内容({len(items)}项): "
                         + (", ".join(items[:30]) if items else "(空)"))
        except Exception as e:
            lines.append(f"读取导出目录失败: {e}")
        # state.json 实际内容摘要
        try:
            raw = self.state_file.read_text(encoding="utf-8")
            lines.append(f"state.json 内容: {raw[:400]}")
        except Exception as e:
            lines.append(f"读取 state.json 失败: {e}")
        for chat in ("group", "private"):
            st = self._state.get(chat, {})
            if st:
                def _fmt(v):
                    if isinstance(v, dict):
                        return f"t={v.get('t')}(ids={len(v.get('ids') or [])})"
                    return f"旧格式seq={v}"
                lines.append(f"{chat}: " + ", ".join(
                    f"{k}@{_fmt(v)}" for k, v in list(st.items())[:10]))
        return "\n".join(lines)

    # ---------------------------------------------------------------
    # 归档读取（v1.4.0 整合自特殊版：查看/搜索/群列表）
    # ---------------------------------------------------------------

    def _read_group_files(self, group_id: str) -> list:
        """返回某群全部归档文件路径（按文件名倒序 = 新 → 旧）。"""
        gid = group_id.strip()
        files = [p for p in self.export_dir.iterdir()
                 if p.is_file()
                 and (p.name.startswith(f"napcat_{gid}_")
                      or p.name.startswith(f"napcat_{gid}."))]
        files.sort(key=lambda p: p.name, reverse=True)
        return files

    def _parse_archived_line(self, raw: str) -> dict | None:
        """解析 JSONL 归档行（单行，保留轻量映射）。"""
        raw = raw.strip()
        if not raw.startswith("{"):
            return None
        try:
            d = json.loads(raw)
        except Exception:
            return None
        return {
            "t": d.get("t", ""),
            "user_id": str(d.get("user_id", "") or ""),
            "nickname": d.get("nickname", ""),
            "content": d.get("content", ""),
        }

    @filter.llm_tool("get_group_message_history")
    async def get_group_message_history(self, event: AstrMessageEvent,
                                        group_id: str, count: int = 20):
        '''
        读取指定群已归档的聊天记录（跨天文件合并，按时间正序返回最近 N 条）。
        适用于查看本插件导出的历史消息，无需依赖 LLM 推理。

        Args:
          group_id(string): 目标群（群号或别名，必填）
          count(number): 返回条数上限（默认 20，最大 200）

        返回: 最近 N 条消息文本（时间正序）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        gid, _err = self._resolve_gid(group_id, "group")
        if _err:
            return _err
        count = max(1, min(int(count), 200))
        files = self._read_group_files(gid)
        if not files:
            return f"📭 群 {gid} 暂无归档记录（目录: {self.export_dir}）"
        all_msgs: list[dict] = []
        for fpath in files:
            try:
                raw_lines = fpath.read_text(encoding="utf-8",
                                            errors="replace").splitlines()
            except Exception as e:
                logger.error(f"读取归档文件失败 {fpath}: {e}")
                continue
            for raw in raw_lines:
                info = self._parse_archived_line(raw)
                if info:
                    all_msgs.append(info)
        all_msgs.sort(key=lambda m: m["t"])
        recent = all_msgs[-count:]
        lines = [f"[{m['t']}] {m['nickname']}: {m['content']}"
                 for m in recent]
        return (f"📋 群 {gid} 最近 {len(recent)} 条消息:\n"
                + "\n".join(lines))

    @filter.llm_tool("search_archived_messages")
    async def search_archived_messages(self, event: AstrMessageEvent,
                                       group_id: str, keyword: str = "",
                                       date: str = "",
                                       user_id: str = "",
                                       nickname: str = "",
                                       count: int = 20):
        '''
        在指定群已归档记录中搜索消息（纯程序过滤，不依赖 LLM 推理）。
        条件可任意组合，全部满足才命中。

        Args:
          group_id(string): 目标群（群号或别名，必填）
          keyword(string): 消息内容关键词（子串匹配，不区分大小写，可选）
          date(string): 日期过滤 YYYY-MM-DD（可选）
          user_id(string): QQ 号精确匹配（可选）
          nickname(string): 昵称包含该字符串（可选）
          count(number): 返回条数上限（默认 20，最大 200）

        返回: 命中的消息文本（时间正序）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        gid, _err = self._resolve_gid(group_id, "group")
        if _err:
            return _err
        count = max(1, min(int(count), 200))
        kw = keyword.strip() if keyword else None
        dt = date.strip() if date else None
        uid = user_id.strip() if user_id else None
        nick = nickname.strip() if nickname else None
        files = self._read_group_files(gid)
        if not files:
            return f"📭 群 {gid} 暂无归档记录（目录: {self.export_dir}）"
        hits: list[dict] = []
        for fpath in files:
            try:
                raw_lines = fpath.read_text(encoding="utf-8",
                                            errors="replace").splitlines()
            except Exception as e:
                logger.error(f"读取归档文件失败 {fpath}: {e}")
                continue
            for raw in raw_lines:
                info = self._parse_archived_line(raw)
                if not info:
                    continue
                if dt and not str(info["t"]).startswith(dt):
                    continue
                if kw and kw.lower() not in info["content"].lower():
                    continue
                if uid and info["user_id"] != uid:
                    continue
                if nick and nick.lower() not in info["nickname"].lower():
                    continue
                hits.append(info)
        hits.sort(key=lambda m: m["t"])
        result = hits[-count:]
        lines = [f"[{m['t']}] {m['nickname']}({m['user_id']}): {m['content']}"
                 for m in result]
        return (f"🔍 群 {gid} 搜索到 {len(hits)} 条"
                f"（显示最近 {len(result)} 条）:\n" + "\n".join(lines))

    @filter.llm_tool("list_archived_groups")
    async def list_archived_groups(self, event: AstrMessageEvent):
        '''
        列出所有已归档的群聊/私聊：名称、群号/QQ号、别名、归档天数与消息条数。
        （v2.2.0 起包含别名与统计信息）

        返回: 归档目标清单
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        return self._archived_targets_text()

    @filter.llm_tool("alias_manage")
    async def alias_manage(self, event: AstrMessageEvent,
                           action: str = "list",
                           target: str = "", alias: str = "",
                           new_alias: str = ""):
        '''
        管理归档目标的别名：别名可替代群号/QQ号用于归档、查询等指令。

        Args:
          action(string): 操作：add=新增别名，remove=删除别名，rename=修改别名，list=查看全部（默认 list）
          target(string): 目标：群号 / QQ号 / 已有别名（除 list 外必填）
          alias(string): 别名（add/remove 必填；rename 时填「旧别名」）
          new_alias(string): 新别名（仅 rename 必填）

        返回: 操作结果或别名列表
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        act = (action or "list").strip().lower()
        if act in ("", "list", "ls", "查看"):
            return self._alias_list_text()
        if act not in ("add", "remove", "del", "delete", "rename", "modify", "新增", "删除", "修改", "重命名"):
            return f"❌ 未知操作「{action}」，可用：add / remove / rename / list"
        if not target.strip():
            return "❌ 请提供目标（群号 / QQ号 / 已有别名）"
        if not alias.strip():
            return "❌ 请提供别名"
        r = self._resolve_target(target)
        if r is None:
            return (f"❌ 未找到目标「{target.strip()}」：尚未归档或别名不存在"
                    f"（可先用群号/QQ号归档一次）")
        if isinstance(r, tuple) and len(r) == 2 and r[0] == "AMBIGUOUS":
            return self._alias_hint(target)
        chat, tid = r
        if act in ("rename", "modify", "修改", "重命名") and not (new_alias or "").strip():
            return "❌ 修改别名请提供新别名（new_alias 参数）"
        if act in ("add", "新增"):
            err = self._alias_add(chat, tid, alias)
        elif act in ("rename", "modify", "修改", "重命名"):
            err = self._alias_rename(chat, tid, alias, new_alias)
        else:
            err = self._alias_remove(chat, tid, alias)
        if err:
            return f"❌ {err}"
        await self._flush_alias_config()
        cur = self._aliases_of(chat, tid)
        if act in ("rename", "modify", "修改", "重命名"):
            verb = "修改（%s → %s）" % (alias.strip(), (new_alias or "").strip())
        elif act in ("add", "新增"):
            verb = "新增「%s」" % alias.strip()
        else:
            verb = "删除「%s」" % alias.strip()
        return (f"✅ 已{verb} → {self._target_label(chat, tid)}\n"
                f"当前别名：{'、'.join(cur) if cur else '（无）'}")

    @filter.llm_tool("remove_archived_target")
    async def remove_archived_target(self, event: AstrMessageEvent,
                                     target: str, chat: str = "",
                                     dry_run: str = "false"):
        '''
        移除一个归档目标（P2-10）：删除其全部归档数据、昵称映射、头像与每日总结，
        并从 state.json / aliases.json / 配置项 aliases 中移除该目标，此后不再巡检。
        适合清理错误账号残留、已解散的群等“僵尸目标”（否则每轮都会空调用后端）。

        Args:
          target(string): 目标：群号 / QQ号 / 已有别名（必填）
          chat(string): 目标类型：group=群聊 / private=私聊；留空=自动判断
          dry_run(string): 填 true 时只列出将删除的文件，不真正删除（默认 false）

        Returns: 操作结果摘要
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用此工具。"
        if not (target or "").strip():
            return "❌ 请提供目标（群号 / QQ号 / 已有别名）"
        kind = (chat or "").strip().lower()
        if kind in ("group", "private"):
            # 显式指定类型：不再依赖别名/文件推断，避免「移除后别名已清、
            # 再按纯数字解析会默认落到 group」的错向问题
            tid = (target or "").strip()
            if not self._safe_id(tid):
                return "❌ 指定 chat 时 target 必须是纯数字群号/QQ号"
            chat_type, real_tid = kind, tid
        else:
            r = self._resolve_target(target)
            if r is None:
                return (f"❌ 未找到目标「{target.strip()}」：尚未归档或别名不存在")
            if isinstance(r, tuple) and len(r) == 2 and r[0] == "AMBIGUOUS":
                return self._alias_hint(target)
            chat_type, real_tid = r
        return await self._remove_target(
            chat_type, real_tid,
            dry_run=(str(dry_run).lower() in ("1", "true", "yes", "on")))

    def _existing_target_dirs(self, tid: str) -> list:
        """按磁盘上的实际文件判断该 tid 属于哪种类型（可能两种都有）。"""
        out = []
        for c in _aliases.CHATS:
            if _targets.related_files(self.export_dir, c, tid) \
                    or self._alias_entry(c, tid) \
                    or self._state.get(c, {}).get(str(tid)):
                out.append(c)
        return out

    async def _remove_target(self, chat: str, tid: str, dry_run: bool = False,
                             both: bool = True):
        """P2-10: 移除归档目标的完整实现（工具与控制台 API 共用）。

        both=True 时，若该 tid 在同名另一类型下也有残留文件，一并清理
        （避免「移除了 private 却留下 group 残骸」）。
        """
        targets_to_clean = [chat]
        if both:
            extra = [c for c in self._existing_target_dirs(tid) if c != chat]
            targets_to_clean += extra
        all_files = []
        for c in targets_to_clean:
            all_files += _targets.related_files(self.export_dir, c, tid)
        files = sorted(set(all_files))
        label = self._target_label(chat, tid)
        summary = _targets.describe(chat, tid, files)

        if dry_run:
            lines = [f"🔍 预览（未删除）：{summary}", f"目标：{label}"]
            if files:
                lines.append("将删除的文件：")
                lines += [f"  - {p.name}" for p in files[:50]]
                if len(files) > 50:
                    lines.append(f"  …（共 {len(files)} 个）")
            else:
                lines.append("（该目标没有关联文件）")
            return "\n".join(lines)

        # 1) 删文件
        removed = 0
        failed = []
        for p in files:
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except Exception as e:
                failed.append(f"{p.name}: {e}")

        # 2) 清理 state（游标 + protected）—— 覆盖所有待清理类型
        for c in targets_to_clean:
            _targets.strip_from_state(self._state, c, tid)
        self._save_state()

        # 3) 清理 aliases.json
        for c in targets_to_clean:
            _targets.strip_from_aliases(self.aliases, c, tid)
        self._save_aliases()

        # 4) 清理配置项 aliases（否则 _sync_alias_config 会把它复活）
        try:
            before_cfg = list(self.config.get("aliases") or [])
            cfg_lines = _targets.strip_from_config_lines(before_cfg, tid)
            if cfg_lines != before_cfg:
                self.config["aliases"] = cfg_lines
                saver = getattr(self.config, "save_config_async", None)
                if callable(saver):
                    await saver()
                else:
                    self.config.save_config()
        except Exception as e:
            logger.error(f"[页面] 清理配置项 aliases 失败: {e}")

        # 5) 清理内存缓存，避免旧值残留
        cache = getattr(self, "_nick_cache", None)
        if isinstance(cache, dict):
            for c in targets_to_clean:
                cache.pop(f"{c}:{tid}", None)
        row_cache = getattr(self, "_row_cache", None)
        if isinstance(row_cache, dict):
            for k in [k for k in row_cache if f"_{tid}_" in str(k)]:
                row_cache.pop(k, None)

        logger.info(f"[归档] 已移除目标 {label}：删除 {removed} 个文件"
                    + (f"，{len(failed)} 个失败" if failed else ""))
        out = [f"✅ 已移除目标：{label}", f"删除文件 {removed} 个"
               + (f"（{len(failed)} 个失败）" if failed else "")]
        if failed:
            out += [f"  - {x}" for x in failed[:10]]
        out.append("已同步清理 state.json / aliases.json / 配置项，此后不再巡检该目标。")
        return "\n".join(out)

    # ---------------- v2.3.0: 插件页面（控制台）API ----------------

    _PAGE_CONFIG_FIELDS = [
        ("backend", "str"),
        ("export_dir", "str"),
        ("auto_export", "bool"),
        ("interval_seconds", "int"),
        ("startup_verify", "bool"),
        ("verify_days", "int"),
        ("whitelist", "list"),
        ("private_whitelist", "list"),
        ("auto_export_friends", "bool"),
        ("archive_bots", "list"),
        ("aliases", "list"),
        ("count_per_batch", "int"),
        ("admin_only", "bool"),
        ("auto_clean", "bool"),
        ("clean_days", "int"),
        ("ui_default_tab", "str"),
        ("ui_messages_page_size", "int"),
        ("ui_avatar_cache", "bool"),
        ("llm_summary_enabled", "bool"),
        ("llm_summary_provider", "str"),
        ("llm_summary_time", "str"),
        ("llm_summary_trend_days", "int"),
        ("llm_briefing_enabled", "bool"),
        ("llm_summary_max_chars", "int"),
        ("llm_briefing_max_chars", "int"),
    ]

    def _register_web_apis(self) -> None:
        """向 AstrBot 注册控制台页面后端 API（旧版本自动跳过）。"""
        if not _WEB_AVAILABLE:
            logger.warning("[页面] 当前 AstrBot 版本缺少插件页面 API，控制台不可用。")
            return
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning("[页面] context.register_web_api 不可用，控制台不可用。")
            return
        routes = [
            ("console/state", "GET", self._api_state, "总览统计"),
            ("console/targets", "GET", self._api_targets, "归档目标列表"),
            ("console/messages", "GET", self._api_messages, "读取归档消息"),
            ("console/dates", "GET", self._api_dates, "归档日期列表"),
            ("console/stats", "GET", self._api_stats, "数据统计"),
            ("console/avatar", "GET", self._api_avatar, "发言人头像"),
            ("console/avatar_file", "GET", self._api_avatar_file, "头像文件"),
            ("console/search", "GET", self._api_search, "消息搜索"),
            ("console/members", "GET", self._api_members, "成员昵称映射"),
            ("console/locate", "GET", self._api_locate, "消息定位（引用跳转）"),
            ("console/archive", "POST", self._api_archive, "手动触发归档"),
            ("console/summary", "POST", self._api_summary, "生成每日总结"),
            ("console/summary_get", "GET", self._api_summary_get, "读取已有总结"),
            ("console/briefing", "GET", self._api_briefing, "生成/读取简报"),
            ("console/summary_config", "POST", self._api_summary_config, "保存总结配置"),
            ("console/config", "GET", self._api_config_get, "读取插件配置"),
            ("console/config", "POST", self._api_config_save, "保存插件配置"),
            # P2-10: 移除归档目标（清理僵尸目标）
            ("console/remove_target", "POST", self._api_remove_target,
             "移除归档目标"),
            # 阶段 1 已实测通过的兼容端点
            ("state", "GET", self._api_state, "总览统计（兼容）"),
            ("targets", "GET", self._api_targets, "归档目标列表（兼容）"),
            ("messages", "GET", self._api_messages, "读取归档消息（兼容）"),
            ("config", "GET", self._api_config_get, "读取配置（兼容）"),
            ("config", "POST", self._api_config_save, "保存配置（兼容）"),
        ]
        for route, method, handler, desc in routes:
            try:
                register(f"/{PLUGIN_NAME}/{route}", handler, [method], desc)
            except Exception as e:
                logger.error(f"[页面] 注册 API {route} 失败: {e}")

    def _target_files(self, chat: str, tid: str) -> list:
        return _store.target_files(self.export_dir, chat, tid)

    def _count_rows_cached(self, files) -> int:
        """统计 JSONL 行数（按 mtime/size 缓存，并持久化到 row_index.json）。

        P2-1：内存缓存在重载后失效，57k 行首轮统计约 1.8s；把索引落盘后
        重载直接复用。文件被改动时 mtime/size 变化会自动重算。
        """
        cache = getattr(self, "_row_cache", None)
        if cache is None:
            cache = {}
            self._row_cache = cache
        before = len(cache)
        total = _store.count_rows_cached(files, cache)
        if len(cache) != before:
            # 有新条目：标记待落盘（不每次都写，避免频繁 I/O）
            self._row_cache_dirty = True
        return total

    def _overview_data(self) -> dict:
        targets = self._scan_archived_targets()
        files = sorted(self.export_dir.glob("napcat_*.jsonl"))
        days = set()
        total_size = 0
        for fp in files:
            try:
                total_size += fp.stat().st_size
            except Exception:
                pass
            stem = fp.name[:-6]
            parts = stem.split("_")
            if len(parts) >= 3 and len(parts[-1]) == 10:
                days.add(parts[-1])
        items = []
        for chat, tid in targets:
            tfiles = self._target_files(chat, tid)
            size = 0
            for fp in tfiles:
                try:
                    size += fp.stat().st_size
                except Exception:
                    pass
            ent = self._alias_entry(chat, tid) or {}
            items.append({
                "chat": chat,
                "target": tid,
                "label": self._target_label(chat, tid),
                "name": str(ent.get("name") or ""),
                "aliases": self._aliases_of(chat, tid),
                "days": len(tfiles),
                "rows": self._count_rows_cached(tfiles),
                "size": size,
                "last_date": (tfiles[-1].name.split("_")[-1][:-6] if tfiles else ""),
            })
        items.sort(key=lambda x: (-x["rows"], x["target"]))
        return {
            "version": PLUGIN_VERSION,
            "export_dir": str(self.export_dir),
            "backend": self.config.get("backend", "auto"),
            "summary": {
                "targets": len(targets),
                "groups": sum(1 for c, _t in targets if c == "group"),
                "privates": sum(1 for c, _t in targets if c == "private"),
                "days": len(days),
                "files": len(files),
                "rows": self._count_rows_cached(files),
                "size": total_size,
                "last_date": (max(days) if days else ""),
            },
            "targets": items,
        }

    async def _api_state(self):
        try:
            # P2-1: 总览要扫全部目标 + 统计所有文件行数，是最重的一个
            data = await asyncio.to_thread(self._overview_data)
            # 行数索引有更新时落盘，供下次重载后直接复用
            if getattr(self, "_row_cache_dirty", False):
                await asyncio.to_thread(self._save_row_cache)
                self._row_cache_dirty = False
            return json_response(data)
        except Exception as e:
            logger.error(f"[页面] 总览数据失败: {e}")
            return error_response(f"总览数据失败: {e}", status_code=500)

    async def _api_targets(self):
        try:
            data = await asyncio.to_thread(self._overview_data)
            return json_response({"targets": data["targets"]})
        except Exception as e:
            return error_response(f"目标列表失败: {e}", status_code=500)

    async def _api_messages(self):
        q = request.query
        date = (q.get("date") or "").strip()
        if date and not self._safe_date(date):
            return error_response("date 非法（应为 YYYY-MM-DD）", status_code=400)
        # P3-4: start/end 的校验必须在「无归档文件」早退之前完成，
        # 否则不存在的目标 + 非法日期会静默返回空结果而不是报错。
        rng_start = (q.get("start") or "").strip()
        rng_end = (q.get("end") or "").strip()
        if rng_start and not self._safe_date(rng_start):
            return error_response("start 非法（应为 YYYY-MM-DD）", status_code=400)
        if rng_end and not self._safe_date(rng_end):
            return error_response("end 非法（应为 YYYY-MM-DD）", status_code=400)
        try:
            limit = int(q.get("limit") or 200)
        except Exception:
            limit = 200
        limit = max(1, min(limit, 1000))
        try:
            offset = int(q.get("offset") or 0)
        except Exception:
            offset = 0
        r, terr = self._resolve_chat_target(q)
        if terr:
            return error_response(terr, status_code=400)
        chat, tid = r
        files = self._target_files(chat, tid)
        dates = [fp.name.split("_")[-1][:-6] for fp in files]
        info = {"chat": chat, "target": tid, "label": self._target_label(chat, tid)}
        if not files:
            # P0-2：与正常分支保持同一契约（统一用 items，不再返回 messages）
            return json_response({"target": info, "dates": [], "items": [],
                                  "total": 0, "offset": 0, "has_more": False})
        # v2.3.2：支持日期区间（start/end），无区间时沿用单个 date
        if not (rng_start or rng_end):
            rng_start = rng_end = date or dates[-1]
        elif rng_start and rng_end and rng_start > rng_end:
            rng_start, rng_end = rng_end, rng_start
        sel_files = []
        for fp in files:
            d = fp.name.split("_")[-1][:-6]
            if (not rng_start or d >= rng_start) and (not rng_end or d <= rng_end):
                sel_files.append(fp)
        # P2-1: 整日文件读取是重活（现网单文件可达上千行），
        # 放进线程池，避免阻塞 AstrBot 事件循环（机器人回复延迟/心跳抖动）
        try:
            records = await asyncio.to_thread(_aggregate.read_messages, sel_files)
        except Exception as e:
            return error_response(f"读取失败: {e}", status_code=500)
        total = len(records)
        end_i = max(0, total - offset)
        start_i = max(0, end_i - limit)
        page = records[start_i:end_i]
        items = []
        for i, rec in enumerate(page):
            items.append({
                "seq": rec.get("seq"),
                "date": rec.get("_date") or "",
                "time": rec.get("t", ""),
                "sender_id": str(rec.get("user_id") or ""),
                "sender_name": rec.get("nickname") or "",
                "text": rec.get("content") or "",
                "type": rec.get("chat") or chat,
                # P0-2: v2.3.0 宣称「引用条可点击跳转」，但此前 items 没带 reply，
                # 前端 bubble() 依赖 m.reply 的整条链路是死的（归档文件里有该字段）。
                "reply": rec.get("reply"),
                # P0-4: 该条在「当次选中范围」内距末尾的偏移（0 = 最后一条）。
                # 前端据此判断要 loadOlder 多少次才能让目标进入已加载窗口，
                # 避免旧实现用 nodes[total-1-index] 在分页场景下必然错位。
                "offset_from_end": len(records) - (start_i + i) - 1,
            })
        return json_response({
            "target": info,
            "dates": dates,
            "date": (rng_start if rng_start == rng_end else ""),
            "start": rng_start,
            "end": rng_end,
            "selected_dates": [fp.name.split("_")[-1][:-6] for fp in sel_files],
            "total": total,
            "offset": offset,
            "has_more": start_i > 0,
            # P0-2: 不再回传原始 page（前端从未使用，响应体白白翻倍）
            "items": items,
        })

    async def _api_config_get(self):
        cfg = {}
        for key, _kind in self._PAGE_CONFIG_FIELDS:
            try:
                cfg[key] = self.config.get(key)
            except Exception:
                cfg[key] = None
        return json_response({
            "config": cfg,
            "meta": [{"key": k, "type": t} for k, t in self._PAGE_CONFIG_FIELDS],
        })

    async def _api_config_save(self):
        payload = await request.json(default={})
        patch = payload.get("patch") if isinstance(payload, dict) else None
        if not isinstance(patch, dict) or not patch:
            return error_response("patch 无效", status_code=400)
        kinds = dict(self._PAGE_CONFIG_FIELDS)
        cleaned = {}
        for key, val in patch.items():
            kind = kinds.get(key)
            if kind is None:
                continue
            if kind == "bool":
                cleaned[key] = (val if isinstance(val, bool)
                                else str(val).lower() in ("1", "true", "yes", "on"))
            elif kind == "int":
                try:
                    cleaned[key] = int(val)
                except Exception:
                    return error_response(f"{key} 需要整数", status_code=400)
            elif kind == "list":
                if isinstance(val, str):
                    items = [x.strip() for x in val.replace("，", ",").replace(",", "\n").split("\n")]
                    cleaned[key] = [x for x in items if x]
                elif isinstance(val, list):
                    cleaned[key] = [str(x).strip() for x in val if str(x).strip()]
                else:
                    return error_response(f"{key} 需要列表", status_code=400)
            else:
                cleaned[key] = "" if val is None else str(val)
        if not cleaned:
            return error_response("没有可保存的配置项", status_code=400)
        try:
            self.config.update(cleaned)
        except Exception:
            for k, v in cleaned.items():
                self.config[k] = v
        try:
            saver = getattr(self.config, "save_config_async", None)
            if callable(saver):
                await saver()
            else:
                self.config.save_config()
        except Exception as e:
            logger.error(f"[页面] 保存配置失败: {e}")
            return error_response(f"保存失败: {e}", status_code=500)
        try:
            self._alias_cfg = [str(x).strip() for x in (self.config.get("aliases") or []) if str(x).strip()]
            self._sync_alias_config()
        except Exception as e:
            logger.error(f"[页面] 同步别名配置失败: {e}")
        # P1-2: 把可热更新项立刻回写到运行态，无需重载插件。
        # 现场曾出现「配置文件改成 60 天，运行态仍是 14 天」——正是缺了这一步。
        try:
            changed = self._apply_runtime_config()
        except Exception as e:
            logger.error(f"[页面] 应用配置到运行态失败: {e}")
            changed = {}
        if changed:
            detail = "，".join(f"{k}: {old!r}→{new!r}"
                              for k, (old, new) in changed.items())
            logger.info(f"[页面] 配置已热生效：{detail}")
        # 开关类改动需要真的启停后台任务（否则只改了属性、行为不变）
        try:
            await self._sync_background_tasks()
        except Exception as e:
            logger.error(f"[页面] 同步后台任务失败: {e}")
        # 导出目录变更需要重载（涉及数据迁移与路径绑定）
        needs_reload = "export_dir" in cleaned and \
            cleaned.get("export_dir") != str(self.export_dir)
        resp = {"ok": True, "saved": sorted(cleaned.keys()),
                "applied": sorted(changed.keys())}
        if needs_reload:
            resp["needs_reload"] = ["export_dir"]
            resp["message"] = ("导出目录的改动需要重载插件后才会生效"
                               "（将触发旧目录自动迁移）")
        return json_response(resp)

    # ---------------- 阶段 2：日期 / 统计 / 头像 ----------------

    @staticmethod
    def _safe_id(v) -> bool:
        return _backend_mod.safe_id(v)

    @staticmethod
    def _safe_date(v) -> bool:
        return _backend_mod.safe_date(v)

    def _resolve_chat_target(self, q):
        """从 query 解析 (chat, tid)：支持 chat+target_id，或 target(群号/QQ/别名)。
        返回 ((chat, tid), None) 或 (None, 错误信息)。"""
        chat_q = (q.get("chat") or "").strip().lower()
        tid_q = (q.get("target_id") or "").strip()
        raw = (q.get("target") or "").strip()
        if chat_q in ("group", "private") and tid_q:
            if not self._safe_id(tid_q):
                return None, "target_id 非法（应为纯数字）"
            return (chat_q, str(tid_q)), None
        if raw:
            r = self._resolve_target(raw)
            if r is None:
                return None, f"未找到目标「{raw}」"
            if isinstance(r, tuple) and len(r) == 2 and r[0] == "AMBIGUOUS":
                return None, self._alias_hint(raw)
            return r, None
        return None, "缺少 target 或 chat+target_id 参数"

    async def _api_dates(self):
        q = request.query
        r, err = self._resolve_chat_target(q)
        if err:
            return error_response(err, status_code=400)
        chat, tid = r
        files = self._target_files(chat, tid)
        # P2-1: 需要逐文件统计行数（缓存未命中时会读全文件），放线程池
        out = await asyncio.to_thread(self._dates_sync, files)
        return json_response({
            "target": {"chat": chat, "target": tid, "label": self._target_label(chat, tid)},
            "dates": out,
        })

    def _dates_sync(self, files) -> list:
        """日期列表的同步实现（供 to_thread 调用）。"""
        out = []
        for fp in files:
            date = fp.name.split("_")[-1][:-6]
            try:
                size = fp.stat().st_size
            except Exception:
                size = 0
            out.append({"date": date,
                        "count": self._count_rows_cached([fp]),
                        "size": size})
        return out

    async def _api_stats(self):
        q = request.query
        r, err = self._resolve_chat_target(q)
        if err:
            return error_response(err, status_code=400)
        chat, tid = r
        start = (q.get("start") or "").strip()
        end = (q.get("end") or "").strip()
        if start and not self._safe_date(start):
            return error_response("start 非法（应为 YYYY-MM-DD）", status_code=400)
        if end and not self._safe_date(end):
            return error_response("end 非法（应为 YYYY-MM-DD）", status_code=400)
        try:
            days = int(q.get("days") or 30)
        except Exception:
            days = 30
        recent = _aggregate.pick_recent_files(
            self._target_files(chat, tid), start, end, days)
        # P2-1: 统计需要解析每个文件的每一行，是 O(总行数) 的重活
        agg = await asyncio.to_thread(_aggregate.stats, recent)
        return json_response({
            "target": {"chat": chat, "target": tid, "label": self._target_label(chat, tid)},
            "days": len(recent),
            "start": (_aggregate.file_date(recent[0]) if recent else start),
            "end": (_aggregate.file_date(recent[-1]) if recent else end),
            "total": agg["total"],
            "daily": agg["daily"],
            "top_senders": agg["top_senders"],
            "hourly": agg["hourly"],
        })

    @staticmethod
    def _download_avatar(uid: str):
        """同步下载 QQ 头像（在线程中执行）；失败返回 None。"""
        import urllib.request
        url = f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=100"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                if getattr(resp, "status", 200) != 200:
                    return None
                data = resp.read(300 * 1024 + 1)
            if not data or len(data) > 300 * 1024:
                return None
            return data
        except Exception:
            return None

    def _avatar_path(self, uid: str):
        return self.export_dir / "avatars" / f"{uid}.png"

    async def _load_avatar_bytes(self, uid: str, use_cache: bool):
        """优先读缓存，否则下载并写缓存；返回 bytes 或 None。"""
        fp = self._avatar_path(uid)
        if use_cache:
            try:
                if fp.exists() and (time.time() - fp.stat().st_mtime) < 7 * 86400:
                    return fp.read_bytes()
            except Exception:
                pass
        data = await asyncio.to_thread(self._download_avatar, uid)
        if data and use_cache:
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(data)
            except Exception:
                pass
        return data

    async def _api_avatar(self):
        """头像（JSON + base64 data URL）：受限 iframe 内 <img> 可直接使用。"""
        q = request.query
        uid = (q.get("user_id") or "").strip()
        if not self._safe_id(uid):
            return error_response("user_id 非法", status_code=400)
        use_cache = bool(self.config.get("ui_avatar_cache", True))
        data = await self._load_avatar_bytes(uid, use_cache)
        if not data:
            return json_response({"user_id": uid, "data_url": "", "cached": False})
        import base64
        return json_response({
            "user_id": uid,
            "data_url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
            "cached": use_cache,
        })

    async def _api_avatar_file(self):
        """头像文件（file_response）：供直接下载或另存使用。"""
        if file_response is None:
            return error_response("当前 AstrBot 版本不支持文件下载接口", status_code=501)
        q = request.query
        uid = (q.get("user_id") or "").strip()
        if not self._safe_id(uid):
            return error_response("user_id 非法", status_code=400)
        use_cache = bool(self.config.get("ui_avatar_cache", True))
        data = await self._load_avatar_bytes(uid, use_cache)
        if not data:
            return error_response("头像获取失败", status_code=404)
        fp = self._avatar_path(uid)
        if not fp.exists():
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(data)
            except Exception as e:
                # 写盘失败不影响本次响应：file_response 仍可直接读已有缓存
                logger.debug(f"[{PLUGIN_NAME}] 写入头像缓存失败：{e}")
        return file_response(str(fp), filename=f"{uid}.png", content_type="image/png")

    # ---------------- v2.3.0：昵称映射（@ 替名 / 引用预览） ----------------

    def _nick_file(self, chat: str, tid: str):
        prefix = "private_" if chat == "private" else ""
        return self.export_dir / f"names_{prefix}{tid}.json"

    def _load_nicks(self, chat: str, tid: str) -> dict:
        cache = getattr(self, "_nick_cache", None)
        if cache is None:
            cache = {}
            self._nick_cache = cache
        key = f"{chat}:{tid}"
        if key in cache and isinstance(cache[key], dict):
            return cache[key]
        data = {}
        try:
            p = self._nick_file(chat, tid)
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {str(k): str(v) for k, v in raw.items() if str(v)}
        except Exception:
            data = {}
        cache[key] = data
        return data

    def _nick_map(self, chat: str, tid: str) -> dict:
        """返回可直接修改的昵称映射（首次从磁盘加载）。"""
        return self._load_nicks(chat, tid)

    def _save_nicks(self, chat: str, tid: str) -> None:
        try:
            m = self._nick_map(chat, tid)
            if len(m) > 20000:
                m = dict(list(m.items())[-20000:])
                getattr(self, "_nick_cache", {})[f"{chat}:{tid}"] = m
            self._nick_file(chat, tid).write_text(
                json.dumps(m, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"[昵称] 保存映射失败: {e}")

    async def _refresh_members(self, chat: str, tid: str, client) -> int:
        """尽力拉取群成员/好友列表，把昵称合并进映射（供 @ 与引用预览用）。"""
        try:
            if chat == "group":
                items = await client.call_action("get_group_member_list", group_id=int(tid))
            else:
                items = await client.call_action("get_friend_list")
        except Exception as e:
            logger.warning(f"[昵称] 拉取成员列表失败: {e}")
            return 0
        m = self._nick_map(chat, tid)
        n = 0
        for it in items or []:
            if not isinstance(it, dict):
                continue
            uid = str(it.get("user_id", "") or "")
            nm = str(it.get("card") or it.get("nickname") or "").strip()
            if uid and nm and m.get(uid) != nm:
                m[uid] = nm
                n += 1
        if n:
            self._save_nicks(chat, tid)
            logger.info(f"[昵称] 已更新 {n} 个昵称（{chat}:{tid}）")
        return n

    # ---------------- v2.3.0：搜索 / 成员 / 引用定位 / 手动归档 / LLM 总结 ----------------

    async def _client_has_group(self, client, tid: str) -> bool:
        try:
            groups = await client.call_action("get_group_list") or []
        except Exception:
            return False
        for g in groups:
            if str(g.get("group_id", "")) == str(tid):
                return True
        return False

    async def _api_search(self):
        q = request.query
        kw = (q.get("q") or "").strip()
        user = (q.get("user") or "").strip()
        start = (q.get("start") or "").strip()
        end = (q.get("end") or "").strip()
        try:
            limit = int(q.get("limit") or 200)
        except Exception:
            limit = 200
        limit = max(1, min(limit, 1000))
        r, err = self._resolve_chat_target(q)
        if err:
            return error_response(err, status_code=400)
        chat, tid = r
        # P2-1: 搜索会扫最多 limit*5 条消息，放线程池
        result = await asyncio.to_thread(
            _aggregate.search, self._target_files(chat, tid), kw, user,
            limit, start, end)
        return json_response(result)

    async def _api_members(self):
        q = request.query
        r, err = self._resolve_chat_target(q)
        if err:
            return error_response(err, status_code=400)
        chat, tid = r
        names = dict(self._nick_map(chat, tid))
        if (q.get("refresh") or "") in ("1", "true", "yes"):
            try:
                clients = await self._get_clients()
                for _pid, _qq, client in clients:
                    if chat == "group" and not await self._client_has_group(client, tid):
                        continue
                    await self._refresh_members(chat, tid, client)
                    names = dict(self._nick_map(chat, tid))
                    break
            except Exception as e:
                logger.warning(f"[页面] 刷新成员失败: {e}")
        return json_response({"names": names})

    async def _api_locate(self):
        q = request.query
        r, err = self._resolve_chat_target(q)
        if err:
            return error_response(err, status_code=400)
        chat, tid = r
        want = str(q.get("seq") or q.get("id") or "").strip()
        if not want:
            return error_response("缺少 seq/id 参数", status_code=400)
        # P0-3/P0-4: 定位必须返回「文件内 0 基行号」与「距末尾偏移」。
        # 前端不能用 seq 当行索引（seq 是 message_seq，与行号无关）。
        files = self._target_files(chat, tid)
        # P2-1: 定位要从最新文件往回逐个扫，放线程池
        hit = await asyncio.to_thread(_aggregate.locate, files, want)
        if not hit:
            return error_response("未找到该消息", status_code=404)
        target_date, index = hit
        # 计算该消息在其所属文件里距末尾的偏移
        fp = next((f for f in files
                   if _aggregate.file_date(f) == target_date), None)
        row_count = 0
        if fp is not None:
            row_count = await asyncio.to_thread(_count_lines, fp)
        offset_from_end = max(0, row_count - index - 1)
        return json_response({
            "date": target_date,
            "index": index,
            "total": row_count,
            "offset_from_end": offset_from_end,
        })

    async def _api_archive(self):
        """手动触发一次增量归档（后台执行，不阻塞页面）。

        P1-3：按钮语义应与字面「立即归档」一致——
        - 不受 `whitelist` 限制（白名单只管定时循环，与 LLM 工具口径一致）
        - `apply_rules=False`：不限于当天，可补齐历史
        - 归档成功后把目标登记进 `protected`，避免被自动清理删掉
        """
        if getattr(self, "_manual_archiving", False):
            return json_response({"ok": True, "busy": True})
        self._manual_archiving = True

        async def run():
            try:
                n = await self._auto_export_once(apply_rules=False)
                # 按钮归档 = 用户主动行为，登记保护名单（与 LLM 工具一致）
                if n:
                    prot = self._state.setdefault("protected", {})
                    gt = prot.setdefault("group", [])
                    for chat, tid in self._scan_archived_targets():
                        bucket = gt if chat == "group" else \
                            prot.setdefault("private", [])
                        if tid not in bucket:
                            bucket.append(tid)
                    self._save_state()
                logger.info(f"[页面] 手动触发归档完成，新增 {n} 条")
            except Exception as e:
                logger.error(f"[页面] 手动触发归档失败: {e}")
            finally:
                self._manual_archiving = False

        # 持有任务引用，避免被 GC 提前回收（RUF006）
        self._manual_task = asyncio.create_task(run())
        return json_response({"ok": True, "started": True})

    def _ensure_summary_worker(self):
        if getattr(self, "_summary_queue", None) is None:
            self._summary_queue = asyncio.Queue()
        task = getattr(self, "_summary_task", None)
        if task is None or task.done():
            self._summary_task = asyncio.create_task(self._summary_worker())

    async def _summary_worker(self):
        """总结队列：串行执行，一个完成再下一个。"""
        q = self._summary_queue
        while True:
            job = await q.get()
            fut = job.get("future")
            try:
                if job.get("kind") == "brief":
                    await self._do_briefing(job)
                else:
                    await self._do_summary(job)
            except _EmptyData as e:
                # P2-11：空数据是正常状态，不算故障
                logger.info(f"[总结] 无数据可处理: {e}")
                if fut is not None and not fut.done():
                    fut.set_exception(e)
            except Exception as e:
                logger.error(f"[总结] 任务失败: {e}")
                if fut is not None and not fut.done():
                    fut.set_exception(e)
            finally:
                if fut is not None and not fut.done():
                    fut.set_result(job.get("result"))
                q.task_done()

    def _summary_provider(self):
        pid = str(self.config.get("llm_summary_provider") or "").strip()
        if not pid:
            return None
        try:
            return self.context.get_provider_by_id(pid)
        except Exception:
            return None

    async def _llm_text(self, prompt: str, system_prompt: str = "") -> str:
        prov = self._summary_provider()
        if prov is None:
            prov = await self.context.get_using_provider_async()
        if prov is None:
            raise RuntimeError("没有可用的模型（请先在 AstrBot 里配置模型）")
        resp = await prov.text_chat(prompt=prompt, system_prompt=system_prompt or None)
        return (getattr(resp, "completion_text", "") or "").strip()

    def _summary_dir(self, date: str):
        return self.export_dir / "summaries" / date

    def _summary_path(self, chat: str, tid: str, date: str):
        prefix = "private_" if chat == "private" else ""
        return self._summary_dir(date) / f"{prefix}{tid}.md"

    def _day_records(self, chat: str, tid: str, date: str) -> list:
        fp = self._target_path(chat, tid, date)
        out = []
        if not fp.exists():
            return out
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取归档文件失败：{fp}：{e}")
        return out

    @staticmethod
    def _records_to_text(records, max_chars: int = 12000) -> str:
        lines = []
        for r in records:
            lines.append(f"[{str(r.get('t',''))[-8:]}] {r.get('nickname','')}: {r.get('content','')}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（过长已截断）"
        return text

    async def _do_summary(self, job):
        chat, tid, date = job["chat"], job["tid"], job["date"]
        records = self._day_records(chat, tid, date)
        if not records:
            raise _EmptyData("该日期没有归档消息")
        label = self._target_label(chat, tid)
        try:
            trend_days = int(self.config.get("llm_summary_trend_days", 3) or 3)
        except Exception:
            trend_days = 3
        trend_days = max(1, min(trend_days, 30))
        history = ""
        if job.get("trend"):
            parts = []
            files = self._target_files(chat, tid)
            for fp in files[:-1][-trend_days:]:
                d = fp.name.split("_")[-1][:-6]
                p = self._summary_path(chat, tid, d)
                if p.exists():
                    try:
                        parts.append(p.read_text(encoding="utf-8"))
                    except Exception as e:
                        logger.warning(f"[{PLUGIN_NAME}] 读取历史总结失败：{p}：{e}")
            if parts:
                history = "\n\n【历史总结】\n" + "\n---\n".join(parts)[-4000:]
        try:
            max_chars = int(self.config.get("llm_summary_max_chars", 1000) or 1000)
        except Exception:
            max_chars = 1000
        max_chars = max(100, min(max_chars, 5000))
        prompt = (f"以下是「{label}」在 {date} 的聊天记录：\n\n"
                  + self._records_to_text(records) + history
                  + "\n\n请用简洁中文总结：1) 主要话题 2) 活跃成员与互动 3) 值得记录的事件或决定"
                  + (" 4) 与最近几天的趋势变化" if history else "")
                  + f"\n要求：客观叙述，控制在 {max_chars} 字以内，不要逐条复述消息。")
        text = await asyncio.wait_for(
            self._llm_text(prompt, "你是聊天记录整理助手。"), timeout=180)
        p = self._summary_path(chat, tid, date)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        except Exception as e:
            logger.error(f"[总结] 保存失败: {e}")
        job["result"] = {"summary": text, "date": date,
                        "target": {"chat": chat, "target": tid, "label": label},
                        "saved": str(p)}

    async def _do_briefing(self, job):
        try:
            days = int(job.get("days") or 3)
        except Exception:
            days = 3
        days = max(1, min(days, 30))
        dates = []
        for fn in sorted(self.export_dir.glob("napcat_*.jsonl")):
            d = fn.name.split("_")[-1][:-6]
            if len(d) == 10:
                dates.append(d)
        dates = sorted(set(dates))[-days:]
        parts = []
        for d in dates:
            folder = self.export_dir / "summaries" / d
            if not folder.is_dir():
                continue
            for p in sorted(folder.glob("*.md")):
                if p.name == "brief.md":
                    continue
                try:
                    parts.append(f"【{d}｜{p.stem}】" + p.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"[{PLUGIN_NAME}] 读取每日总结失败：{p}：{e}")
        if not parts:
            raise _EmptyData("还没有每日总结可汇总（请先在「统计」里生成某天总结）")
        try:
            max_chars = int(self.config.get("llm_briefing_max_chars", 2000) or 2000)
        except Exception:
            max_chars = 2000
        max_chars = max(200, min(max_chars, 8000))
        prompt = ("以下是各群/私聊最近的每日总结，请汇总成一份「昨日简报」：\n\n"
                  + "\n\n".join(parts)[-8000:]
                  + f"\n\n要求：中文、客观，{max_chars} 字以内，包含总体活跃度、主要话题、值得关注的事。")
        text = await asyncio.wait_for(
            self._llm_text(prompt, "你是社群简报编辑。"), timeout=240)
        p = self.export_dir / "summaries" / (dates[-1] if dates else "brief") / "brief.md"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        except Exception:
            pass
        job["result"] = {"brief": text, "days": dates, "saved": str(p)}

    async def _api_summary(self):
        payload = await request.json(default={})
        body = payload if isinstance(payload, dict) else {}
        chat = str(body.get("chat") or "").strip().lower()
        tid = str(body.get("target_id") or "").strip()
        date = str(body.get("date") or "").strip()
        raw = str(body.get("target") or "").strip()
        trend = bool(body.get("trend"))
        if chat in ("group", "private") and self._safe_id(tid):
            target = (chat, tid)
        elif raw:
            target, _err = self._resolve_chat_target({"target": raw})
        else:
            target = None
        if not target:
            return error_response("缺少目标参数（chat+target_id）", status_code=400)
        chat, tid = target
        if not date or not self._safe_date(date):
            return error_response("date 非法（应为 YYYY-MM-DD）", status_code=400)
        cached = self._summary_path(chat, tid, date)
        if cached.exists() and not body.get("force"):
            try:
                # P2-1: 总结文件可能上万字符，读盘放线程池
                text = await asyncio.to_thread(_read_text_file, cached)
                return json_response({"summary": text,
                                      "date": date, "cached": True})
            except Exception:
                pass
        self._ensure_summary_worker()
        fut = asyncio.get_running_loop().create_future()
        job = {"chat": chat, "tid": tid, "date": date, "trend": trend, "future": fut}
        await self._summary_queue.put(job)
        try:
            await asyncio.wait_for(fut, timeout=300)
        except _EmptyData as e:
            # P2-11：空数据是正常状态，返回 200 + 友好文案，日志用 INFO
            logger.info(f"[总结] 无数据可总结：{e}")
            return json_response({"summary": "", "date": date,
                                  "empty": True, "message": str(e)})
        except Exception as e:
            return error_response(f"总结失败或超时: {e}", status_code=500)
        res = job.get("result") or {}
        if not res.get("summary"):
            return error_response("总结失败", status_code=500)
        res["cached"] = False
        return json_response(res)

    async def _api_summary_get(self):
        q = request.query
        r, err = self._resolve_chat_target(q)
        if err:
            return error_response(err, status_code=400)
        chat, tid = r
        items = []
        # P2-1: 可能有几十个总结文件，整体放线程池
        items = await asyncio.to_thread(self._summary_items_sync, chat, tid)
        return json_response({"items": items})

    def _summary_items_sync(self, chat: str, tid: str) -> list:
        """读取某目标全部已有总结（供 to_thread 调用）。"""
        items = []
        for fp in self._target_files(chat, tid):
            d = fp.name.split("_")[-1][:-6]
            p = self._summary_path(chat, tid, d)
            if p.exists():
                try:
                    items.append({"date": d,
                                  "summary": p.read_text(encoding="utf-8")})
                except Exception:
                    pass
        return items

    async def _api_briefing(self):
        q = request.query
        try:
            days = int(q.get("days") or 3)
        except Exception:
            days = 3
        days = max(1, min(days, 30))
        root = self.export_dir / "summaries"
        if (q.get("get") or "") in ("1", "true", "yes") and root.is_dir():
            files = sorted(root.glob("*/brief.md"))
            if files:
                try:
                    # P2-1: 简报文本通常较长，读盘放线程池
                    text = await asyncio.to_thread(_read_text_file, files[-1])
                    return json_response({"brief": text,
                                          "date": files[-1].parent.name,
                                          "cached": True})
                except Exception:
                    pass
        self._ensure_summary_worker()
        fut = asyncio.get_running_loop().create_future()
        job = {"kind": "brief", "days": days, "future": fut}
        await self._summary_queue.put(job)
        try:
            await asyncio.wait_for(fut, timeout=360)
        except _EmptyData as e:
            # P2-11：空数据是正常状态，返回 200 + 友好文案，日志用 INFO
            logger.info(f"[简报] 无数据可汇总：{e}")
            return json_response({"brief": "", "empty": True, "message": str(e)})
        except Exception as e:
            return error_response(f"简报生成失败或超时: {e}", status_code=500)
        res = job.get("result") or {}
        if not res.get("brief"):
            return error_response("简报生成失败", status_code=500)
        res["cached"] = False
        return json_response(res)

    async def _api_summary_config(self):
        return await self._api_config_save()

    async def _api_remove_target(self):
        """P2-10: 移除归档目标（控制台入口）。

        body = {chat, target_id, dry_run}；dry_run 为真时只预览不删除。
        """
        payload = await request.json(default={})
        body = payload if isinstance(payload, dict) else {}
        q = request.query
        # 兼容 query 参数（GET 预览场景）
        if not body:
            body = {k: q.get(k) for k in ("chat", "target_id", "dry_run")
                    if q.get(k)}
        chat = str(body.get("chat") or "").strip().lower()
        tid = str(body.get("target_id") or "").strip()
        dry = str(body.get("dry_run") or "").lower() in ("1", "true", "yes", "on")
        if chat not in ("group", "private") or not self._safe_id(tid):
            return error_response("需要 chat(group/private) 与合法的 target_id",
                                  status_code=400)
        try:
            msg = await self._remove_target(chat, tid, dry_run=dry)
        except Exception as e:
            logger.error(f"[页面] 移除目标失败: {e}")
            return error_response(f"移除失败: {e}", status_code=500)
        return json_response({"ok": True, "dry_run": dry, "message": msg})
# ===== P3-1（v2.4.0）：类名现代化，保留旧名别名 =====
# 旧名 NapcatHistoryExporter 在 v2.0.0 改名后一直沿用，与项目「chat_history_archiver」
# 已不符。这里保留同名引用，避免仍在引用旧名的第三方代码/旧文档失效；
# 注意：别名不再触发 Star.__init_subclass__（同一个类对象），因此不会产生
# 额外的 star_map 条目，不影响插件加载。
NapcatHistoryExporter = ChatHistoryArchiver
