"""对话别名（aliases.json）的纯逻辑：解析、增删改校验、与配置项互转。

不直接读写文件，也不依赖插件实例；状态由调用方传入（dict）。
"""

from __future__ import annotations

from datetime import datetime

CHATS = ("group", "private")


def empty_aliases() -> dict:
    return {"group": {}, "private": {}}


def normalize_loaded(data) -> dict:
    """把读到的 JSON 规范成 {"group":{}, "private":{}}。"""
    if isinstance(data, dict):
        data.setdefault("group", {})
        data.setdefault("private", {})
        return data
    return empty_aliases()


def get_entry(aliases: dict, chat: str, tid: str, create: bool = False):
    """获取（或创建）某目标的别名条目。"""
    tid = str(tid)
    bucket = aliases.get(chat)
    if bucket is None:
        if not create:
            return None
        bucket = aliases.setdefault(chat, {})
    ent = bucket.get(tid)
    if ent is None and create:
        ent = {"name": "", "aliases": [], "updated": ""}
        bucket[tid] = ent
    return ent if isinstance(ent, dict) else None


def aliases_of(aliases: dict, chat: str, tid: str) -> list:
    ent = get_entry(aliases, chat, tid) or {}
    return [str(a) for a in (ent.get("aliases") or []) if str(a).strip()]


def target_label(aliases: dict, chat: str, tid: str) -> str:
    """目标展示标签：「名称/别名 (群号或QQ号)」。"""
    ent = get_entry(aliases, chat, tid) or {}
    name = str(ent.get("name") or "").strip()
    als = aliases_of(aliases, chat, tid)
    prefix = name or (als[0] if als else ("群聊" if chat == "group" else "私聊"))
    tag = f"群{tid}" if chat == "group" else f"QQ{tid}"
    return f"{prefix} ({tag})"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def register_name(aliases: dict, chat: str, tid: str, name: str) -> bool:
    """记录/更新目标名称（群名或好友昵称）。返回是否发生变更。"""
    name = (name or "").strip()
    if not name:
        return False
    ent = get_entry(aliases, chat, tid, create=True)
    if ent is None:
        return False
    if ent.get("name") != name:
        ent["name"] = name
        ent["updated"] = _now()
        return True
    return False


def find_alias_owner(aliases: dict, chat: str, tid: str, alias: str):
    """查找别名是否已被其它目标占用；返回 (chat, tid) 或 None。"""
    for c, bucket in (aliases or {}).items():
        for t, ent in (bucket or {}).items():
            if c == chat and str(t) == str(tid):
                continue
            if not isinstance(ent, dict):
                continue
            if alias in (ent.get("aliases") or []):
                return (c, str(t))
    return None


def add(aliases: dict, chat: str, tid: str, alias: str):
    """新增别名。返回错误信息；成功返回 None。"""
    alias = (alias or "").strip()
    if not alias:
        return "别名不能为空"
    if alias.isdigit():
        return "别名不能是纯数字（会与群号/QQ号冲突）"
    owner = find_alias_owner(aliases, chat, tid, alias)
    if owner:
        return (f"别名「{alias}」已被 "
                f"{target_label(aliases, owner[0], owner[1])} 使用")
    ent = get_entry(aliases, chat, tid, create=True)
    if ent is None:
        return "创建别名条目失败"
    lst = ent.setdefault("aliases", [])
    if alias not in lst:
        lst.append(alias)
    ent["updated"] = _now()
    return None


def remove(aliases: dict, chat: str, tid: str, alias: str):
    """删除别名。返回错误信息；成功返回 None。"""
    alias = (alias or "").strip()
    ent = get_entry(aliases, chat, tid)
    if ent is None:
        return "该目标还没有登记（先归档一次或添加别名）"
    lst = ent.get("aliases") or []
    if alias not in lst:
        return (f"该目标没有别名「{alias}」"
                f"（当前：{'、'.join(lst) if lst else '无'}）")
    lst.remove(alias)
    ent["updated"] = _now()
    return None


def rename(aliases: dict, chat: str, tid: str, old: str, new: str):
    """修改别名（把 old 改成 new）。返回错误信息；成功返回 None。"""
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new:
        return "修改别名需要提供「旧别名」和「新别名」"
    if old == new:
        return "新别名与旧别名相同，无需修改"
    if new.isdigit():
        return "别名不能是纯数字（会与群号/QQ号冲突）"
    ent = get_entry(aliases, chat, tid)
    if ent is None:
        return "该目标还没有登记（先归档一次或添加别名）"
    lst = ent.get("aliases") or []
    if old not in lst:
        return (f"该目标没有别名「{old}」"
                f"（当前：{'、'.join(lst) if lst else '无'}）")
    owner = find_alias_owner(aliases, chat, tid, new)
    if owner:
        return (f"别名「{new}」已被 "
                f"{target_label(aliases, owner[0], owner[1])} 使用")
    if new in lst:
        return f"本目标已有别名「{new}」（可先删除「{old}」）"
    lst[lst.index(old)] = new
    ent["updated"] = _now()
    return None


def resolve(aliases: dict, text: str, prefer_chat: str = ""):
    """把「群号 / QQ号 / 别名」解析为 (chat, tid)。
    返回 None = 未找到；返回 ("AMBIGUOUS", hits) = 别名歧义。"""
    s = (text or "").strip()
    if not s:
        return None
    if s.isdigit():
        if prefer_chat:
            return (prefer_chat, s)
        for c in CHATS:
            if get_entry(aliases, c, s):
                return (c, s)
        return ("group", s)
    low = s.lower()
    hits: list = []
    for c, bucket in (aliases or {}).items():
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


def hint(aliases: dict, text: str) -> str:
    """别名歧义时的用户提示。"""
    r = resolve(aliases, text)
    if isinstance(r, tuple) and len(r) == 2 and r[0] == "AMBIGUOUS":
        opts = "；".join(target_label(aliases, c, t) for c, t in r[1])
        return (f"⚠️ 别名「{text.strip()}」对应多个目标：{opts}。"
                f"请改用群号/QQ号指定。")
    return ""


def resolve_gid(aliases: dict, raw: str, kind: str = "group"):
    """工具入口的目标解析：支持群号或别名。
    返回 (gid, None)；失败时 (None, 错误提示)。"""
    s = (raw or "").strip()
    if s.isdigit():
        return (s, None)
    r = resolve(aliases, s, prefer_chat=kind)
    if r is None:
        name = "群" if kind == "group" else "好友"
        return (None, f"❌ 未找到{name}「{s}」：尚未归档或别名不存在"
                      f"（可先用群号/QQ号归档一次）")
    if isinstance(r, tuple) and len(r) == 2 and r[0] == "AMBIGUOUS":
        return (None, hint(aliases, s))
    if r[0] != kind:
        return (None, f"❌「{s}」对应的是"
                      f"{'私聊' if r[0] == 'private' else '群聊'}目标，"
                      f"请改用对应的"
                      f"{'QQ号' if r[0] == 'private' else '群号'}。")
    return (r[1], None)


def parse_config_item(raw: str):
    """解析配置项「别名,群号」→ (alias, target)；无别名时 alias 为空串。"""
    raw = str(raw).strip()
    if not raw:
        return ("", "")
    if "," in raw or "，" in raw:
        a, _, t = raw.replace("，", ",").partition(",")
        return (a.strip(), t.strip())
    return ("", raw)


def to_config_lines(aliases: dict) -> list:
    """把别名映射导出成配置项行（形如「别名,群号」）。"""
    lines = []
    for chat in CHATS:
        for tid, ent in (aliases.get(chat) or {}).items():
            if not isinstance(ent, dict):
                continue
            for a in (ent.get("aliases") or []):
                lines.append(f"{a},{tid}")
    return lines
