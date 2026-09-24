import asyncio
import json
import os
import shutil
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
    _WEB_AVAILABLE = True
except Exception:  # pragma: no cover - 旧版 AstrBot 无插件页面 API
    _WEB_AVAILABLE = False

PLUGIN_NAME = "astrbot_plugin_chat_history_archiver"
PLUGIN_VERSION = "2.3.1"

# 消息段类型 → 占位符（不导出媒体文件）
_SEG_PLACEHOLDER = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "reply": "[引用消息]",
    "forward": "[合并转发]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
}

# ===== 数据目录（v2.3.1：插件数据统一放在 data/plugin_data/<插件名> 下）=====
LEGACY_EXPORT_DIR = "data/workspaces/napcat_exports"


def _plugin_data_root() -> Path:
    """返回 AstrBot 的 data/plugin_data 目录（优先官方 API，逐级降级）。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        return Path(get_astrbot_plugin_data_path())
    except Exception:
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return Path(get_astrbot_data_path()) / "plugin_data"
    except Exception:
        pass
    return Path("data") / "plugin_data"


def _plugin_dir_name() -> str:
    return Path(__file__).resolve().parent.name


def _default_export_dir() -> Path:
    return _plugin_data_root() / _plugin_dir_name()


def _legacy_export_dir_path() -> Path:
    # <root>/data/plugin_data -> <root>/data -> data/workspaces/napcat_exports
    return _plugin_data_root().parent / "workspaces" / "napcat_exports"


def resolve_export_dir(raw):
    """解析 export_dir 配置。

    规则（v2.3.1，满足插件市场「数据持久化位置」规范）：
    - 留空或仍是旧默认值 → 用新默认 data/plugin_data/<插件名>（并返回旧目录用于迁移）
    - 相对路径且落在 data/workspaces/** → 视为旧数据位置，改用新默认并自动迁移
    - 其它相对路径 → 限制在 data/plugin_data/<插件名>/ 下
    - 绝对路径 → 原样使用（在 plugin_data 之外会告警）

    返回 (最终目录, 需要迁移的旧目录或 None)
    """
    default_dir = _default_export_dir()
    raw = str(raw or "").strip().replace("\\", "/")
    if raw in ("", LEGACY_EXPORT_DIR, "./" + LEGACY_EXPORT_DIR,
               LEGACY_EXPORT_DIR + "/"):
        return default_dir, _legacy_export_dir_path()
    p = Path(raw).expanduser()
    if p.is_absolute():
        try:
            inside = str(p.resolve()).startswith(
                str(_plugin_data_root().resolve()))
        except Exception:
            inside = False
        if not inside:
            logger.warning(
                f"[{PLUGIN_NAME}] export_dir 位于 plugin_data 之外：{p}；"
                "该位置不受 AstrBot 备份/迁移覆盖，建议改为 "
                f"{_plugin_data_root() / _plugin_dir_name()}")
        return p, None
    norm = raw[2:] if raw.startswith("./") else raw
    root = _plugin_data_root().parent.parent  # AstrBot 根目录
    if norm.startswith("data/"):
        target = root / norm
        try:
            legacy_root = (root / "data" / "workspaces").resolve()
            if str(target.resolve()).startswith(str(legacy_root)):
                return default_dir, target
        except Exception:
            pass
        return target, None
    # 其它相对路径：约束到插件数据目录下
    return default_dir / norm.strip("/"), None


def migrate_dir(src, dst) -> str:
    """把旧数据目录内容逐项搬到新目录（已存在的项跳过），返回日志文案。"""
    if src is None:
        return ""
    try:
        src = src.resolve()
        dst = dst.resolve()
    except Exception:
        return ""
    if src == dst or not src.is_dir():
        return ""
    try:
        items = sorted(src.iterdir())
    except Exception:
        return ""
    if not items:
        return ""
    moved = 0
    skipped = 0
    for it in items:
        target = dst / it.name
        if target.exists():
            skipped += 1
            continue
        try:
            shutil.move(str(it), str(target))
            moved += 1
        except Exception as e:
            skipped += 1
            logger.warning(f"[{PLUGIN_NAME}] 迁移 {it} 失败：{e}")
    try:
        if not any(src.iterdir()):
            src.rmdir()
    except Exception:
        pass
    msg = (f"旧数据目录 {src} 已迁移到 {dst}"
           f"（移动 {moved} 项，跳过 {skipped} 项）")
    logger.info(f"[{PLUGIN_NAME}] {msg}")
    return msg



class NapcatHistoryExporter(Star):
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
        self.auto_export = bool(config.get("auto_export", True))
        self.interval = max(30, int(config.get("interval_seconds", 120)))
        self.batch = max(1, min(int(config.get("count_per_batch", 50)), 200))
        self.auto_friends = bool(config.get("auto_export_friends", False))
        self.admin_only = bool(config.get("admin_only", True))
        # v1.3.0: 自动归档白名单 / 历史自动清理（仅循环导出模式生效）
        self.whitelist = [str(x).strip() for x in (config.get("whitelist") or [])
                          if str(x).strip()]
        self.auto_clean = bool(config.get("auto_clean", True))
        self.clean_days = max(1, int(config.get("clean_days", 14)))
        self.state_file = self.export_dir / "state.json"
        self._state: dict = self._load_state()
        self._client = None
        self._task: asyncio.Task | None = None
        self._last_clean: datetime | None = None  # v1.3.1: 上次自动清理时间（每12h一次）
        # v1.5.0: 多 bot / SnowLuma
        self.archive_bots = [str(x).strip() for x in (config.get("archive_bots") or [])
                             if str(x).strip()]  # 留空=归档全部 aiocqhttp 实例
        self.backend_cfg = str(config.get("backend", "auto") or "auto").strip().lower()
        self._backend: str | None = None      # 后端探测结果缓存: "napcat" / "snowluma"
        self._qq_cache: dict = {}             # 平台实例 id -> 登录 QQ号（get_login_info）
        self._qq_cache_ts: float = 0.0
        # v2.1.0: 启动归档检查（自动补全缺失日期/不全记录）
        self.startup_verify = bool(config.get("startup_verify", True))
        self.verify_days = max(1, min(int(config.get("verify_days", 3) or 3), 30))
        self._verify_task: asyncio.Task | None = None
        self._file_lock = asyncio.Lock()      # 写入段串行化（并发任务防护）
        # v2.2.0: 对话别名注册系统
        self.aliases_file = self.export_dir / "aliases.json"
        self.aliases = self._load_aliases()
        self._alias_cfg = [str(x).strip() for x in (config.get("aliases") or [])
                           if str(x).strip()]

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

    # ---------------- 对话别名注册系统（v2.2.0） ----------------

    def _load_aliases(self) -> dict:
        """读取 aliases.json（不存在时返回空结构）。"""
        try:
            data = json.loads(self.aliases_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("group", {})
                data.setdefault("private", {})
                return data
        except Exception:
            pass
        return {"group": {}, "private": {}}

    def _save_aliases(self) -> None:
        try:
            self.aliases_file.write_text(
                json.dumps(self.aliases, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logger.error(f"[别名] 保存 aliases.json 失败: {e}")

    def _alias_entry(self, chat: str, tid: str, create: bool = False):
        """获取（或创建）某目标的别名条目。"""
        tid = str(tid)
        bucket = self.aliases.get(chat)
        if bucket is None:
            if not create:
                return None
            bucket = self.aliases.setdefault(chat, {})
        ent = bucket.get(tid)
        if ent is None and create:
            ent = {"name": "", "aliases": [], "updated": ""}
            bucket[tid] = ent
        return ent if isinstance(ent, dict) else None

    def _register_name(self, chat: str, tid: str, name: str) -> None:
        """记录/更新目标名称（群名或好友昵称）。"""
        name = (name or "").strip()
        if not name:
            return
        ent = self._alias_entry(chat, tid, create=True)
        if ent is None:
            return
        if ent.get("name") != name:
            ent["name"] = name
            ent["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_aliases()

    def _aliases_of(self, chat: str, tid: str) -> list:
        ent = self._alias_entry(chat, tid) or {}
        return [str(a) for a in (ent.get("aliases") or []) if str(a).strip()]

    def _target_label(self, chat: str, tid: str) -> str:
        """目标展示标签：「名称/别名 (群号或QQ号)」。"""
        ent = self._alias_entry(chat, tid) or {}
        name = str(ent.get("name") or "").strip()
        als = self._aliases_of(chat, tid)
        prefix = name or (als[0] if als else ("群聊" if chat == "group" else "私聊"))
        tag = f"群{tid}" if chat == "group" else f"QQ{tid}"
        return f"{prefix} ({tag})"

    def _alias_add(self, chat: str, tid: str, alias: str):
        """新增别名。返回错误信息；成功返回 None。"""
        alias = (alias or "").strip()
        if not alias:
            return "别名不能为空"
        if alias.isdigit():
            return "别名不能是纯数字（会与群号/QQ号冲突）"
        for c, bucket in self.aliases.items():
            for t, ent in (bucket or {}).items():
                if c == chat and str(t) == str(tid):
                    continue
                if not isinstance(ent, dict):
                    continue
                if alias in (ent.get("aliases") or []):
                    return f"别名「{alias}」已被 {self._target_label(c, t)} 使用"
        ent = self._alias_entry(chat, tid, create=True)
        if ent is None:
            return "创建别名条目失败"
        aliases = ent.setdefault("aliases", [])
        if alias not in aliases:
            aliases.append(alias)
        ent["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_aliases()
        return None

    def _alias_remove(self, chat: str, tid: str, alias: str):
        """删除别名。返回错误信息；成功返回 None。"""
        alias = (alias or "").strip()
        ent = self._alias_entry(chat, tid)
        if ent is None:
            return "该目标还没有登记（先归档一次或添加别名）"
        aliases = ent.get("aliases") or []
        if alias not in aliases:
            return (f"该目标没有别名「{alias}」"
                    f"（当前：{'、'.join(aliases) if aliases else '无'}）")
        aliases.remove(alias)
        ent["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_aliases()
        return None
    def _alias_rename(self, chat: str, tid: str, old: str, new: str):
        """修改别名（把 old 改成 new）。返回错误信息；成功返回 None。"""
        old = (old or "").strip()
        new = (new or "").strip()
        if not old or not new:
            return "修改别名需要提供「旧别名」和「新别名」"
        if old == new:
            return "新别名与旧别名相同，无需修改"
        if new.isdigit():
            return "别名不能是纯数字（会与群号/QQ号冲突）"
        ent = self._alias_entry(chat, tid)
        if ent is None:
            return "该目标还没有登记（先归档一次或添加别名）"
        aliases = ent.get("aliases") or []
        if old not in aliases:
            return (f"该目标没有别名「{old}」"
                    f"（当前：{'、'.join(aliases) if aliases else '无'}）")
        for c, bucket in self.aliases.items():
            for t, ent2 in (bucket or {}).items():
                if not isinstance(ent2, dict):
                    continue
                if c == chat and str(t) == str(tid):
                    continue
                if new in (ent2.get("aliases") or []):
                    return f"别名「{new}」已被 {self._target_label(c, t)} 使用"
        if new in aliases:
            return f"本目标已有别名「{new}」（可先删除「{old}」）"
        aliases[aliases.index(old)] = new
        ent["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_aliases()
        return None

    def _resolve_target(self, text: str, prefer_chat: str = ""):
        """把「群号 / QQ号 / 别名」解析为 (chat, tid)。
        返回 None = 未找到；返回 ("AMBIGUOUS", hits) = 别名歧义。"""
        s = (text or "").strip()
        if not s:
            return None
        if s.isdigit():
            if prefer_chat:
                return (prefer_chat, s)
            for c in ("group", "private"):
                if self._alias_entry(c, s):
                    return (c, s)
            return ("group", s)
        low = s.lower()
        hits: list = []
        for c, bucket in self.aliases.items():
            for t, ent in (bucket or {}).items():
                if not isinstance(ent, dict):
                    continue
                for a in (ent.get("aliases") or []):
                    if str(a).lower() == low:
                        hits.append((c, str(t)))
        if not hits:
            return None
        if len(hits) == 1:
            return hits[0]
        if prefer_chat:
            same = [h for h in hits if h[0] == prefer_chat]
            if len(same) == 1:
                return same[0]
        return ("AMBIGUOUS", hits)

    def _alias_hint(self, text: str) -> str:
        """别名歧义时的用户提示。"""
        r = self._resolve_target(text)
        if isinstance(r, tuple) and len(r) == 2 and r[0] == "AMBIGUOUS":
            opts = "；".join(self._target_label(c, t) for c, t in r[1])
            return (f"⚠️ 别名「{text.strip()}」对应多个目标：{opts}。"
                    f"请改用群号/QQ号指定。")
        return ""

    def _resolve_gid(self, raw: str, kind: str = "group"):
        """工具入口的目标解析：支持群号或别名。
        返回 (gid, None)；失败时 (None, 错误提示)。"""
        s = (raw or "").strip()
        if s.isdigit():
            return (s, None)
        r = self._resolve_target(s, prefer_chat=kind)
        if r is None:
            name = "群" if kind == "group" else "好友"
            return (None, f"❌ 未找到{name}「{s}」：尚未归档或别名不存在"
                          f"（可先用群号/QQ号归档一次）")
        if isinstance(r, tuple) and len(r) == 2 and r[0] == "AMBIGUOUS":
            return (None, self._alias_hint(s))
        if r[0] != kind:
            return (None, f"❌「{s}」对应的是"
                          f"{'私聊' if r[0] == 'private' else '群聊'}目标，"
                          f"请改用对应的"
                          f"{'QQ号' if r[0] == 'private' else '群号'}。")
        return (r[1], None)

    def _sync_alias_config(self) -> None:
        """把配置项 aliases（形如「别名,群号」）合并进 aliases.json。"""
        added = 0
        for item in self._alias_cfg:
            raw = str(item).strip()
            if not raw:
                continue
            alias, target = "", raw
            if "," in raw or "，" in raw:
                alias, _, target = raw.replace("，", ",").partition(",")
                alias, target = alias.strip(), target.strip()
            if not target.isdigit():
                continue
            chat = "group"
            for c in ("group", "private"):
                if self._alias_entry(c, target):
                    chat = c
                    break
            self._alias_entry(chat, target, create=True)
            if alias:
                if self._alias_add(chat, target, alias) is None:
                    added += 1
        if added:
            logger.info(f"[别名] 已从配置同步 {added} 个别名")

    async def _flush_alias_config(self) -> None:
        """把别名回写配置项并保存（保持 WebUI 显示同步）。"""
        lines = []
        for chat in ("group", "private"):
            for tid, ent in (self.aliases.get(chat) or {}).items():
                if not isinstance(ent, dict):
                    continue
                for a in (ent.get("aliases") or []):
                    lines.append(f"{a},{tid}")
        try:
            self.config["aliases"] = lines
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
        for pid, qq, client in clients:
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
                    with open(fp, encoding="utf-8",
                              errors="replace") as f:
                        rows += sum(1 for _ in f)
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
        """v2.1.0: 扫描导出目录，收集已有归档记录的目标 [(chat, target_id)]。"""
        import re as _re
        out: list = []
        seen: set = set()
        pat = _re.compile(r"^napcat_(private_)?(\d+)_\d{4}-\d{2}-\d{2}\.jsonl$")
        try:
            for f in self.export_dir.glob("napcat_*_????-??-??.jsonl"):
                m = pat.match(f.name)
                if not m:
                    continue
                chat = "private" if m.group(1) else "group"
                tid = m.group(2)
                key = (chat, tid)
                if key not in seen:
                    seen.add(key)
                    out.append(key)
        except Exception as e:
            logger.error(f"[归档检查] 扫描导出目录失败: {e}")
        return out

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

    async def _bot_qq(self, client, pid: str) -> str:
        """获取某平台实例的登录 QQ 号（self_id）；失败返回空串。结果缓存 600s。"""
        now = time.time()
        if now - self._qq_cache_ts > 600:
            self._qq_cache = {}
            self._qq_cache_ts = now
        if pid in self._qq_cache:
            return self._qq_cache[pid]
        try:
            info = await client.call_action("get_login_info")
            qq = str((info or {}).get("user_id") or "")
        except Exception:
            qq = ""
        self._qq_cache[pid] = qq
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
        for p in self._aiocqhttp_platforms():
            try:
                pid = str(p.meta().id or "")
                client = p.get_client()
            except Exception:
                continue
            if client is None:
                continue
            qq = await self._bot_qq(client, pid)
            if not self._bot_enabled(pid, qq):
                logger.info(f"[NapCatExporter] bot 未启用归档"
                            f"（archive_bots 过滤）: 实例id={pid or '?'} qq={qq or '未知'}")
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
        """auto 后端探测：
        NapCat : 消息带 real_seq 字段，且 message_seq == message_id；
        SnowLuma: 无 real_seq，message_id 为 int32 哈希（≠单调会话号 message_seq）。"""
        if not msgs:
            return None
        m0 = msgs[0]
        if not isinstance(m0, dict):
            return None
        if m0.get("real_seq") is not None:
            return "napcat"
        seq = m0.get("message_seq")
        mid = m0.get("message_id")
        if seq is not None and mid is not None and str(seq) != str(mid):
            return "snowluma"
        return "napcat"

    def _segments_to_text(self, segments, nickmap=None) -> str:
        """消息段 → 文本。图片/表情等替换为占位符，不导出媒体。

        v2.3.0：若提供昵称映射（QQ号 → 昵称），@ 消息会渲染为 @昵称，
        否则保留 [At:QQ号] 占位（页面端会用同一映射再做兜底替换）。
        """
        if isinstance(segments, str):
            return segments
        parts = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type", "")
            data = seg.get("data") or {}
            if t == "text":
                parts.append(data.get("text", ""))
            elif t == "at":
                qq = str(data.get("qq", "") or "")
                nm = (nickmap or {}).get(qq, "")
                if nm:
                    parts.append("@" + str(nm))
                else:
                    parts.append(f"[At:{qq}]")
            elif t == "file":
                parts.append(f"[文件:{data.get('name', '')}]")
            elif t in _SEG_PLACEHOLDER:
                parts.append(_SEG_PLACEHOLDER[t])
            else:
                parts.append(f"[{t}]")
        return "".join(parts)

    def _fmt_record(self, msg: dict, chat: str, target_id: str, nickmap=None) -> dict:
        """OneBot 消息 → JSONL 行。"""
        sender = msg.get("sender") or {}
        try:
            ts = int(msg.get("time") or 0)
            t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            t = ""
        return {
            "t": t,
            "chat": chat,
            "group_id": target_id,
            "user_id": str(sender.get("user_id", "")),
            "nickname": (sender.get("card") or sender.get("nickname") or "").strip(),
            "seq": msg.get("message_seq") or msg.get("message_id") or 0,
            "content": self._segments_to_text(msg.get("message", ""), nickmap),
            "reply": self._reply_of(msg),
        }

    @staticmethod
    def _reply_of(msg: dict) -> dict | None:
        """抽取引用（reply）消息信息，供页面“点击跳转”与引用条展示。"""
        segments = msg.get("message")
        if isinstance(segments, str):
            return None
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") != "reply":
                continue
            data = seg.get("data") or {}
            out = {}
            for key in ("id", "message_id", "seq", "text", "qq", "nickname", "time"):
                val = data.get(key)
                if val is None or val == "":
                    continue
                if key == "time":
                    try:
                        out["time"] = datetime.fromtimestamp(int(val)).strftime(
                            "%Y-%m-%d %H:%M:%S")
                        continue
                    except Exception:
                        pass
                out[key] = str(val)
            return out or None
        return None

    def _date_of(self, msg: dict) -> str:
        try:
            return datetime.fromtimestamp(int(msg.get("time") or 0)) \
                .strftime("%Y-%m-%d")
        except Exception:
            return "unknown"

    def _target_path(self, chat: str, target_id: str, date: str) -> Path:
        # 按天分文件：napcat_<群号>_YYYY-MM-DD.jsonl（私聊 napcat_private_<QQ>_*）
        if chat == "group":
            return self.export_dir / f"napcat_{target_id}_{date}.jsonl"
        return self.export_dir / f"napcat_private_{target_id}_{date}.jsonl"

    def _split_merged_files(self) -> None:
        """v1.2.0 迁移：若存在 v1.1.0 单文件 napcat_<群号>.jsonl，
        按行内 t 字段拆回按天文件 napcat_<群号>_YYYY-MM-DD.jsonl，
        随后删除单文件（避免与按天分文件模式冲突）。
        """
        try:
            if not self.export_dir.is_dir():
                return
            import re as _re
            for p in sorted(self.export_dir.glob("napcat_*.jsonl")):
                if p.name == "state.json":
                    continue
                m = _re.match(r"napcat_(private_)?(\d+)\.jsonl$", p.name)
                if not m:
                    continue  # 已是按天文件，跳过
                prefix = m.group(1) or ""
                gid = m.group(2)
                try:
                    lines = [ln for ln in
                             p.read_text(encoding="utf-8", errors="replace")
                             .splitlines() if ln.strip()]
                except Exception as e:
                    logger.error(f"[NapCatExporter] 拆分读取失败 {p.name}: {e}")
                    continue
                if not lines:
                    p.unlink(missing_ok=True)
                    continue
                # 按行内 t 字段的前 10 位（YYYY-MM-DD）分组
                by_date: dict = {}
                for ln in lines:
                    d = ""
                    try:
                        d = str(json.loads(ln).get("t", ""))[:10]
                    except Exception:
                        pass
                    by_date.setdefault(d or "unknown", []).append(ln)
                for date, day_lines in by_date.items():
                    target = self.export_dir / \
                        f"napcat_{prefix}{gid}_{date}.jsonl"
                    try:
                        with open(target, "a", encoding="utf-8") as f:
                            f.write("\n".join(day_lines) + "\n")
                        logger.info(f"[NapCatExporter] 拆分: {p.name} → "
                                    f"{target.name}（{len(day_lines)}行）")
                    except Exception as e:
                        logger.error(f"[NapCatExporter] 拆分写入失败 "
                                     f"{target.name}: {e}")
                p.unlink(missing_ok=True)
                logger.info(f"[NapCatExporter] 单文件已删除: {p.name}")
        except Exception as e:
            logger.error(f"[NapCatExporter] 单文件拆分迁移异常: {e}")

    def _merge_write(self, path: Path, records: list) -> int:
        """v1.3.3: 合并写入——读现有文件行 + 新记录，按 seq 去重、
        按 t 排序后整体重写。文件自愈：不依赖内存去重，永不重复、有序。
        v2.1.0: 返回实际新增条数（合并后新增，重复消息不计入）。"""
        merged: dict = {}
        if path.exists():
            for ln in path.read_text(encoding="utf-8",
                                     errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                    key = str(r.get("seq") or
                              (r.get("user_id", "") + r.get("t", "")))
                    merged[key] = r
                except Exception:
                    continue
        before = len(merged)  # v2.1.0: 合并前已有（去重）条数基线
        for r in records:
            key = str(r.get("seq") or
                      (r.get("user_id", "") + r.get("t", "")))
            merged[key] = r  # 新记录覆盖旧记录
        ordered = sorted(merged.values(),
                         key=lambda r: str(r.get("t", "")))
        with open(path, "w", encoding="utf-8") as f:
            for r in ordered:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return max(0, len(ordered) - before)

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
        return msg.get("message_seq") or msg.get("message_id") or 0

    @staticmethod
    def _anchor_of(msg: dict) -> int | None:
        """v1.3.4: 翻页定位锚点——优先 real_seq（会话内序号，单调递增，
        NapCat 内部用其定位），否则回退 message_id。"""
        rs = msg.get("real_seq")
        if rs is not None:
            try:
                return int(str(rs).strip())
            except Exception:
                pass
        return None

    @staticmethod
    def _mid_of(msg: dict) -> str:
        """消息唯一 ID（用于去重）。"""
        return str(msg.get("message_id") or msg.get("message_seq") or "")

    @staticmethod
    def _time_of(msg: dict) -> int:
        try:
            return int(msg.get("time") or 0)
        except Exception:
            return 0

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
        fetched: list = []
        guard = 0
        # v2.1.0: ignore_seen 用于回溯补全（启动检查/手动回溯）——文件才是
        # 唯一权威，若按 state 的已归档 id 过滤，已删文件的记录将无法重建；
        # 页间防重叠仍由 local_seen 在本轮内维护
        local_seen = set() if (ignore_seen and start_ts > 0) else set(seen)
        consecutive_empty = 0  # v1.3.4: 连续无新增页数，防翻页死循环
        while True:
            guard += 1
            if guard > 600:  # 防御：单次最多翻 600 页
                logger.warning(f"[NapCatExporter] {target_id} 翻页超过 600 页，强制停止")
                break
            msgs = await self._fetch(action, key, target_id, start,
                                      self.batch, client=client)
            if not msgs:
                break
            if limit > 0:
                # 按需导出：收集未写过的消息，达到 limit 条为止
                fresh = [m for m in msgs
                         if self._mid_of(m) not in local_seen]
                fetched.extend(fresh)
                local_seen.update(self._mid_of(m) for m in fresh)
                if len(fetched) >= limit:
                    break
                # 本页没有新消息且包含旧消息 → 没有更多可导出的了
                if not fresh and any(self._time_of(m) <= last_t for m in msgs):
                    break
                if not fresh:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        break
                else:
                    consecutive_empty = 0
            else:
                # 回溯时间段导出（手动归档历史，不推进游标）
                if start_ts > 0:
                    zone = [m for m in msgs
                            if end_ts <= 0 or self._time_of(m) <= end_ts]
                    fresh = [m for m in zone
                             if self._time_of(m) >= start_ts
                             and self._mid_of(m) not in local_seen]
                    fetched.extend(fresh)
                    local_seen.update(self._mid_of(m) for m in fresh)
                    if any(self._time_of(m) < start_ts for m in msgs):
                        break  # 已到时间段起点
                    if zone and not fresh:
                        consecutive_empty += 1
                        if consecutive_empty >= 3:
                            break
                    elif zone and fresh:
                        consecutive_empty = 0
                    # zone 为空（页内全是 end_ts 之后的消息）→ 不计数，继续往前翻
                else:
                    # 增量：只保留 time > last_t 且未写过的消息
                    fresh = [m for m in msgs
                             if self._time_of(m) > last_t
                             and self._mid_of(m) not in local_seen]
                    if today_only:
                        # 自动归档只归档当天
                        fresh = [m for m in fresh
                                 if self._time_of(m) >= today_start]
                    fetched.extend(fresh)
                    local_seen.update(self._mid_of(m) for m in fresh)
                    if today_only:
                        # 遇当天之前（含昨天及更早）的消息 → 本页已到当天边界
                        if any(self._time_of(m) < today_start for m in msgs):
                            break
                    else:
                        if any(self._time_of(m) <= last_t for m in msgs):
                            break
                    if not fresh:
                        consecutive_empty += 1
                        if consecutive_empty >= 3:
                            break
                    else:
                        consecutive_empty = 0
            # 向前翻页（拿更早的）
            if self._backend_now() == "snowluma":
                # SnowLuma：锚点=本页最旧消息的 message_id（int32 哈希，须
                # 原值回传，由其内部定位 sequence 往前翻；reverse_order 默认
                # true 会包含锚点消息本身，靠 local_seen 去重）
                anchor_msg = min(
                    msgs, key=lambda m: (self._time_of(m), self._seq_of(m)))
                anchor_id = anchor_msg.get("message_id")
                if anchor_id is None or anchor_id == "":
                    break
                start = int(anchor_id)
                if start == 0 or len(fetched) >= 5000:
                    break  # 哈希锚点为 0（无有效 id）或已达上限 → 停止
                # 本页只有锚点一条 → 已翻到最早，下页必然重复/为空
                if (len(msgs) == 1
                        and self._mid_of(msgs[0]) == self._mid_of(anchor_msg)):
                    break
            else:
                # NapCat：v1.3.4 优先用 real_seq（单调递增，传 min-1 精确定位
                # 更早消息）；无 real_seq 时回退页内最小 message_seq（真实存在，
                # NapCat 返回其之前更早的消息）
                anchors = [a for a in (self._anchor_of(m) for m in msgs)
                           if a is not None]
                if anchors:
                    start = min(anchors) - 1
                else:
                    start = min(self._seq_of(m) for m in msgs)
                if start < 1 or len(fetched) >= 5000:
                    break
        if not fetched:
            return 0
        # v2.1.0: 写入段串行化（启动归档检查 / 手动 / 循环任务并发防护）
        async with self._file_lock:
            # 按 time 排序
            new = sorted(fetched[:limit] if limit > 0 else fetched,
                         key=self._time_of)
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
            new_ids = [self._mid_of(m) for m in new if self._mid_of(m)]
            seen |= set(new_ids)
            cur = self._state.setdefault(chat, {})
            if today_only:
                # v1.3.2: 当天模式游标不推进（固定为今天 0 点 -1 秒），
                # 每轮都尝试拉取当天全部消息，配合 message_id 去重：
                # 翻页失败时也不漏"最新"消息，翻页可用时能补全当天更早的
                cur[target_id] = {"t": today_start - 1,
                                  "ids": list(seen)[-5000:]}
            elif start_ts > 0:
                # v1.4.0: 回溯时间段归档不推进游标（不影响后续增量），仅更新去重 ids
                cur[target_id] = {"t": last_t, "ids": list(seen)[-5000:]}
            else:
                cur[target_id] = {"t": max_t, "ids": list(seen)[-5000:]}
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
                for f in friends or []:
                    uid = str(f.get("user_id", ""))
                    if not uid:
                        continue
                    self._register_name("private", uid, f.get("nickname") or "")
                    if uid in seen_targets:
                        continue
                    seen_targets.add(uid)
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

    def _cleanup_old_files(self) -> None:
        """v1.3.0: 自动清理过期历史文件（仅循环导出模式调用）。

        删除文件名日期早于（今天 - clean_days）的 napcat_*_YYYY-MM-DD.jsonl；
        手动归档（protected）过的目标文件不清理。
        """
        if not self.auto_clean:
            return
        cutoff = (datetime.now().date()
                  - timedelta(days=self.clean_days)).strftime("%Y-%m-%d")
        protected = set()
        for g in self._state.get("protected", {}).get("group", []) or []:
            protected.add(f"napcat_{g}_")
        for u in self._state.get("protected", {}).get("private", []) or []:
            protected.add(f"napcat_private_{u}_")
        import re as _re
        removed = 0
        for f in self.export_dir.glob("napcat_*_????-??-??.jsonl"):
            m = _re.match(
                r"napcat_(private_)?(\d+)_(\d{4}-\d{2}-\d{2})\.jsonl$", f.name)
            if not m:
                continue
            prefix = f"napcat_{m.group(1) or ''}{m.group(2)}_"
            if prefix in protected:
                continue  # 手动归档过的目标，不自动清理
            fdate = m.group(3)
            if fdate < cutoff:
                try:
                    f.unlink(missing_ok=True)
                    logger.info(f"[NapCatExporter] 自动清理过期文件: "
                                f"{f.name}（早于 {cutoff}）")
                    removed += 1
                except Exception as e:
                    logger.error(f"[NapCatExporter] 清理文件失败 {f.name}: {e}")
        if removed:
            logger.info(f"[NapCatExporter] 本轮自动清理 {removed} 个过期文件")

    async def _startup_verify_once(self) -> int:
        """v2.1.0: 启动归档检查——对已归档目标逐天回溯补全最近 verify_days 天缺口。

        对导出目录中已有归档的目标（群/私聊），逐天拉取最近 N 天消息并与现有
        文件幂等合并（按 id 去重）：
          - 整天空洞（文件缺失但该天有消息）→ 生成补全；
          - 记录不全（文件存在但缺少部分消息）→ 补齐差值。
        超出自动清理保留期的天跳过；后端（SL/NapCat）本地无记录的消息无法补。
        """
        if not self.startup_verify:
            return 0
        logger.info(f"[归档检查] 启动检查开始：范围最近 {self.verify_days} 天")
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
        targets = self._scan_archived_targets()
        if not targets:
            logger.info("[归档检查] 导出目录暂无已归档目标，跳过检查")
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
        today = datetime.now().date()
        cutoff = today - timedelta(days=self.clean_days)  # 自动清理保留期边界
        total = 0
        t0 = time.time()
        for back in range(self.verify_days):
            day = today - timedelta(days=back)
            if self.auto_clean and day < cutoff:
                logger.info(f"[归档检查] {day} 已超出保留期"
                            f"（{self.clean_days} 天），跳过补全")
                continue
            start_ts = int(datetime.combine(day, datetime.min.time()).timestamp())
            end_ts = int(datetime.combine(day, datetime.max.time()).timestamp())
            if day == today:
                end_ts = int(time.time())
            for chat, tid in targets:
                label = f"群 {tid}" if chat == "group" else f"私聊 {tid}"
                if chat == "group" and tid in gid2client:
                    pid, qq, client = gid2client[tid]
                else:
                    pid, qq, client = clients[0]
                bot_tag = f"bot[{qq or pid or '?'}]"
                try:
                    n = await self._export_target(
                        chat, tid, start_ts=start_ts, end_ts=end_ts,
                        client=client, bot_tag=bot_tag, ignore_seen=True)
                except Exception as e:
                    logger.error(f"[归档检查] {label} {day} 检查失败: {e}")
                    n = 0
                if n:
                    logger.info(f"[归档检查] {label} {day} 补齐 {n} 条")
                total += n
                await asyncio.sleep(0.3)
        logger.info(f"[归档检查] 完成：{len(targets)} 个目标 × 最多 "
                    f"{self.verify_days} 天，共补齐 {total} 条，"
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
            await asyncio.sleep(self.interval)

    # ---------------------------------------------------------------
    # 生命周期
    # ---------------------------------------------------------------

    async def initialize(self) -> None:
        # v2.2.0: 别名系统初始化（加载 + 同步配置 + 后台刷新名称）
        try:
            self.aliases = self._load_aliases()
            self._sync_alias_config()
            asyncio.create_task(self._refresh_names_safe())
        except Exception as e:
            logger.error(f"[别名] 初始化异常: {e}")
        # v2.3.0: 注册插件页面（控制台）API
        try:
            self._register_web_apis()
        except Exception as e:
            logger.error(f"[页面] 注册 Web API 失败: {e}")
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

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._verify_task:
            self._verify_task.cancel()
            try:
                await self._verify_task
            except asyncio.CancelledError:
                pass
            self._verify_task = None

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
        """解析 JSONL 归档行。"""
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

    # ---------------- v2.3.0: 插件页面（控制台）API ----------------

    _PAGE_CONFIG_FIELDS = [
        ("backend", "str"),
        ("export_dir", "str"),
        ("auto_export", "bool"),
        ("interval_seconds", "int"),
        ("startup_verify", "bool"),
        ("verify_days", "int"),
        ("whitelist", "list"),
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
        prefix = "napcat_private_" if chat == "private" else "napcat_"
        return sorted(self.export_dir.glob(f"{prefix}{tid}_*.jsonl"))

    def _count_rows_cached(self, files) -> int:
        """统计 JSONL 行数（按 mtime/size 缓存，避免每次全量重算）。"""
        cache = getattr(self, "_row_cache", None)
        if cache is None:
            cache = {}
            self._row_cache = cache
        total = 0
        for fp in files:
            try:
                st = fp.stat()
                key = str(fp)
                hit = cache.get(key)
                if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
                    total += hit[2]
                    continue
                rows = 0
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for _line in f:
                        rows += 1
                cache[key] = (st.st_mtime_ns, st.st_size, rows)
                total += rows
            except Exception:
                continue
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
            return json_response(self._overview_data())
        except Exception as e:
            logger.error(f"[页面] 总览数据失败: {e}")
            return error_response(f"总览数据失败: {e}", status_code=500)

    async def _api_targets(self):
        try:
            return json_response({"targets": self._overview_data()["targets"]})
        except Exception as e:
            return error_response(f"目标列表失败: {e}", status_code=500)

    async def _api_messages(self):
        q = request.query
        raw = (q.get("target") or "").strip()
        date = (q.get("date") or "").strip()
        if date and not self._safe_date(date):
            return error_response("date 非法（应为 YYYY-MM-DD）", status_code=400)
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
            return json_response({"target": info, "dates": [], "messages": [], "total": 0})
        if date and date in dates:
            fp = files[dates.index(date)]
        else:
            fp = files[-1]
            date = dates[-1]
        records = []
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            return error_response(f"读取失败: {e}", status_code=500)
        total = len(records)
        end = max(0, total - offset)
        start = max(0, end - limit)
        page = records[start:end]
        items = []
        for rec in page:
            items.append({
                "seq": rec.get("seq"),
                "time": rec.get("t", ""),
                "sender_id": str(rec.get("user_id") or ""),
                "sender_name": rec.get("nickname") or "",
                "text": rec.get("content") or "",
                "type": rec.get("chat") or chat,
            })
        return json_response({
            "target": info,
            "dates": dates,
            "date": date,
            "total": total,
            "offset": offset,
            "has_more": start > 0,
            "messages": page,
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
        return json_response({"ok": True, "saved": sorted(cleaned.keys())})

    # ---------------- 阶段 2：日期 / 统计 / 头像 ----------------

    @staticmethod
    def _safe_id(v) -> bool:
        v = (v or "").strip()
        return bool(v) and v.isdigit() and len(v) <= 20

    @staticmethod
    def _safe_date(v) -> bool:
        v = (v or "").strip()
        if len(v) != 10 or v[4] != "-" or v[7] != "-":
            return False
        return all(c.isdigit() or c == "-" for c in v)

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
        out = []
        for fp in self._target_files(chat, tid):
            date = fp.name.split("_")[-1][:-6]
            try:
                size = fp.stat().st_size
            except Exception:
                size = 0
            out.append({"date": date, "count": self._count_rows_cached([fp]), "size": size})
        return json_response({
            "target": {"chat": chat, "target": tid, "label": self._target_label(chat, tid)},
            "dates": out,
        })

    async def _api_stats(self):
        q = request.query
        r, err = self._resolve_chat_target(q)
        if err:
            return error_response(err, status_code=400)
        chat, tid = r
        try:
            days = int(q.get("days") or 30)
        except Exception:
            days = 30
        days = max(1, min(days, 365))
        files = self._target_files(chat, tid)
        recent = files[-days:] if len(files) > days else files
        daily = []
        by_sender = {}
        hourly = [0] * 24
        total = 0
        for fp in recent:
            date = fp.name.split("_")[-1][:-6]
            n = 0
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        n += 1
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        uid = str(rec.get("user_id") or "")
                        if uid:
                            item = by_sender.setdefault(
                                uid, {"user_id": uid, "sender_name": "", "count": 0})
                            item["count"] += 1
                            if not item["sender_name"]:
                                item["sender_name"] = str(rec.get("nickname") or "")
                        t = str(rec.get("t") or "")
                        if len(t) >= 13:
                            try:
                                hh = int(t[11:13])
                                if 0 <= hh < 24:
                                    hourly[hh] += 1
                            except Exception:
                                pass
            except Exception:
                continue
            daily.append({"date": date, "count": n})
            total += n
        top = sorted(by_sender.values(), key=lambda x: -x["count"])[:10]
        return json_response({
            "target": {"chat": chat, "target": tid, "label": self._target_label(chat, tid)},
            "days": len(recent),
            "total": total,
            "daily": daily,
            "top_senders": top,
            "hourly": hourly,
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
            except Exception:
                pass
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
        hits = []
        cap = limit * 5
        for fp in self._target_files(chat, tid):
            date = fp.name.split("_")[-1][:-6]
            if start and date < start:
                continue
            if end and date > end:
                continue
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        text = str(rec.get("content") or "")
                        if kw and kw.lower() not in text.lower():
                            continue
                        uid = str(rec.get("user_id") or "")
                        nick = str(rec.get("nickname") or "")
                        if user and user not in uid and user.lower() not in nick.lower():
                            continue
                        hits.append({
                            "date": date,
                            "seq": rec.get("seq"),
                            "time": rec.get("t", ""),
                            "sender_id": uid,
                            "sender_name": nick,
                            "text": text,
                        })
                        if len(hits) >= cap:
                            break
            except Exception:
                continue
            if len(hits) >= cap:
                break
        hits.sort(key=lambda x: x["time"])
        return json_response({"total": len(hits), "items": hits[-limit:]})

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
        for fp in reversed(self._target_files(chat, tid)):
            date = fp.name.split("_")[-1][:-6]
            idx = 0
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            idx += 1
                            continue
                        cands = [str(rec.get("seq") or "")]
                        rep = rec.get("reply") or {}
                        if isinstance(rep, dict):
                            cands.append(str(rep.get("id") or ""))
                            cands.append(str(rep.get("message_id") or ""))
                            cands.append(str(rep.get("seq") or ""))
                        if want in cands:
                            return json_response({"date": date, "index": idx})
                        idx += 1
            except Exception:
                continue
        return error_response("未找到该消息", status_code=404)

    async def _api_archive(self):
        """手动触发一次增量归档（后台执行，不阻塞页面）。"""
        if getattr(self, "_manual_archiving", False):
            return json_response({"ok": True, "busy": True})
        self._manual_archiving = True

        async def run():
            try:
                n = await self._auto_export_once()
                logger.info(f"[页面] 手动触发归档完成，新增 {n} 条")
            except Exception as e:
                logger.error(f"[页面] 手动触发归档失败: {e}")
            finally:
                self._manual_archiving = False

        asyncio.create_task(run())
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
        except Exception:
            pass
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
            raise RuntimeError("该日期没有归档消息")
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
                    except Exception:
                        pass
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
                except Exception:
                    pass
        if not parts:
            raise RuntimeError("还没有每日总结可汇总（请先在「统计」里生成某天总结）")
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
                return json_response({"summary": cached.read_text(encoding="utf-8"),
                                      "date": date, "cached": True})
            except Exception:
                pass
        self._ensure_summary_worker()
        fut = asyncio.get_running_loop().create_future()
        job = {"chat": chat, "tid": tid, "date": date, "trend": trend, "future": fut}
        await self._summary_queue.put(job)
        try:
            await asyncio.wait_for(fut, timeout=300)
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
        for fp in self._target_files(chat, tid):
            d = fp.name.split("_")[-1][:-6]
            p = self._summary_path(chat, tid, d)
            if p.exists():
                try:
                    items.append({"date": d, "summary": p.read_text(encoding="utf-8")})
                except Exception:
                    pass
        return json_response({"items": items})

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
                    return json_response({"brief": files[-1].read_text(encoding="utf-8"),
                                          "date": files[-1].parent.name, "cached": True})
                except Exception:
                    pass
        self._ensure_summary_worker()
        fut = asyncio.get_running_loop().create_future()
        job = {"kind": "brief", "days": days, "future": fut}
        await self._summary_queue.put(job)
        try:
            await asyncio.wait_for(fut, timeout=360)
        except Exception as e:
            return error_response(f"简报生成失败或超时: {e}", status_code=500)
        res = job.get("result") or {}
        if not res.get("brief"):
            return error_response("简报生成失败", status_code=500)
        res["cached"] = False
        return json_response(res)

    async def _api_summary_config(self):
        return await self._api_config_save()
