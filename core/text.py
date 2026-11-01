"""消息段 → 文本、归档记录格式化、引用解析。

全部为纯函数（不依赖插件实例），便于单测。
"""

from __future__ import annotations

from datetime import datetime

#: 消息段类型 → 占位符（不导出媒体文件）
SEG_PLACEHOLDER = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "reply": "[引用消息]",
    "forward": "[合并转发]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
}


def segments_to_text(segments, nickmap=None) -> str:
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
        elif t in SEG_PLACEHOLDER:
            parts.append(SEG_PLACEHOLDER[t])
        else:
            parts.append(f"[{t}]")
    return "".join(parts)


def reply_of(msg: dict) -> dict | None:
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


def fmt_record(msg: dict, chat: str, target_id: str, nickmap=None) -> dict:
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
        "content": segments_to_text(msg.get("message", ""), nickmap),
        "reply": reply_of(msg),
    }


def date_of(msg: dict) -> str:
    try:
        return datetime.fromtimestamp(int(msg.get("time") or 0)).strftime("%Y-%m-%d")
    except Exception:
        return "unknown"
