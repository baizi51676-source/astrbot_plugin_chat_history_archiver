"""归档目标的清理（P2-10）。

背景
----
`_scan_archived_targets()` 只看导出目录里有没有 `napcat_*_YYYY-MM-DD.jsonl`。
一旦某个目标（比如错误账号残留的私聊）留下了文件，它就会被永久纳入
「定时轮 + 启动检查」的巡检范围——即使后端每次都返回空列表。

现网实例：`names_private_2490530663.json` 存在，导致每轮/每天调用
`get_friend_msg_history(2490530663)` 都空返回并打 WARN。

本模块只做纯逻辑：给定一个目标，列出需要删除的全部关联文件；
以及从 state.json 结构中移除该目标的游标与保护名单条目。
真正删文件由调用方执行（便于先备份、可回滚）。
"""

from __future__ import annotations


def related_files(export_dir, chat: str, tid: str) -> list:
    """列出某目标的全部关联文件（不含实际删除）。

    覆盖：
    - 归档数据：napcat_<tid>_YYYY-MM-DD.jsonl / napcat_private_<tid>_*.jsonl
    - 旧版单文件：napcat_<tid>.jsonl / napcat_private_<tid>.jsonl
    - 昵称映射：names_<tid>.json / names_private_<tid>.json
    - 头像缓存：avatars/<tid>.png
    - 该目标的每日总结：summaries/<date>/<tid>.md
    """
    from pathlib import Path

    export_dir = Path(export_dir)
    tid = str(tid)
    prefix = "private_" if chat == "private" else ""
    out = []

    # 归档数据（按天 + 旧版单文件）
    for p in export_dir.glob(f"napcat_{prefix}{tid}_*.jsonl"):
        out.append(p)
    merged = export_dir / f"napcat_{prefix}{tid}.jsonl"
    if merged.exists():
        out.append(merged)

    # 昵称映射
    nick = export_dir / f"names_{prefix}{tid}.json"
    if nick.exists():
        out.append(nick)

    # 头像
    avatar = export_dir / "avatars" / f"{tid}.png"
    if avatar.exists():
        out.append(avatar)

    # 每日总结
    sroot = export_dir / "summaries"
    if sroot.is_dir():
        for folder in sroot.iterdir():
            if not folder.is_dir():
                continue
            md = folder / f"{tid}.md"
            if md.exists():
                out.append(md)

    return sorted(out)


def strip_from_state(state: dict, chat: str, tid: str) -> dict:
    """从 state 结构中移除该目标的游标与保护名单条目（原地修改并返回）。"""
    tid = str(tid)
    bucket = state.get(chat)
    if isinstance(bucket, dict):
        bucket.pop(tid, None)
    prot = state.get("protected")
    if isinstance(prot, dict):
        lst = prot.get(chat)
        if isinstance(lst, list) and tid in lst:
            prot[chat] = [x for x in lst if x != tid]
    return state


def strip_from_aliases(aliases: dict, chat: str, tid: str) -> dict:
    """从 aliases 结构中移除该目标（原地修改并返回）。"""
    tid = str(tid)
    bucket = aliases.get(chat)
    if isinstance(bucket, dict):
        bucket.pop(tid, None)
    return aliases


def strip_from_config_lines(lines, tid: str) -> list:
    """从配置项 aliases（形如「别名,群号」）中移除指向该目标的项。

    配置项若不清理，下次 `_sync_alias_config` 会把别名重新加回 aliases.json。
    """
    tid = str(tid)
    kept = []
    for raw in lines or []:
        text = str(raw).strip()
        if not text:
            continue
        target = text.replace("，", ",").partition(",")[2].strip() \
            if ("," in text or "，" in text) else text
        if target == tid:
            continue
        kept.append(raw)
    return kept


def describe(chat: str, tid: str, files) -> str:
    """给用户看的一句话摘要。"""
    kind = "群" if chat == "group" else "私聊"
    return (f"{kind} {tid}：将删除 {len(files)} 个文件"
            f"（归档数据/昵称/头像/总结），并从状态文件中移除该目标")
