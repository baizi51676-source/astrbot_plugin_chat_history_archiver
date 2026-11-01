"""后端（NapCat / SnowLuma）探测与翻页锚点工具。"""

from __future__ import annotations


def probe_backend(msgs: list) -> str | None:
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


def seq_of(msg: dict) -> int:
    return msg.get("message_seq") or msg.get("message_id") or 0


def anchor_of(msg: dict) -> int | None:
    """v1.3.4: 翻页定位锚点——优先 real_seq（会话内序号，单调递增，
    NapCat 内部用其定位），否则回退 message_id。"""
    rs = msg.get("real_seq")
    if rs is not None:
        try:
            return int(str(rs).strip())
        except Exception:
            pass
    return None


def mid_of(msg: dict) -> str:
    """消息唯一 ID（用于去重）。"""
    return str(msg.get("message_id") or msg.get("message_seq") or "")


def time_of(msg: dict) -> int:
    try:
        return int(msg.get("time") or 0)
    except Exception:
        return 0


def safe_id(v) -> bool:
    v = (v or "").strip()
    return bool(v) and v.isdigit() and len(v) <= 20


def safe_date(v) -> bool:
    """校验是否为合法的 YYYY-MM-DD 日期（P3-4）。

    此前只校验形状，`2026-99-99`、`2026-02-31` 都会通过；后续仅做字符串
    比较，导致统计/搜索静默返回空而不报错。现在用真实日期解析。
    """
    from datetime import datetime

    v = (v or "").strip()
    if len(v) != 10 or v[4] != "-" or v[7] != "-":
        return False
    if not all(c.isdigit() or c == "-" for c in v):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return True
    except Exception:
        return False
