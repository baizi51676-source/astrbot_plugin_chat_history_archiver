"""归档数据的聚合查询：搜索、统计、定位、总览汇总。

全部为纯函数：只接收文件路径列表与查询参数，返回可 JSON 化的数据。
不做网络请求，也不依赖插件实例。
"""

from __future__ import annotations

import json
from pathlib import Path


def _iter_records(files, start: str = "", end: str = ""):
    """按文件遍历 JSONL，产出 (date, rec)。日期过滤在文件级先做一次。"""
    for fp in files:
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
                    yield date, rec
        except Exception:
            continue


def search(files, kw: str = "", user: str = "", limit: int = 200,
           start: str = "", end: str = ""):
    """在归档文件中搜索消息（子串匹配，大小写不敏感）。

    扫描上限为 limit*5，返回最后 limit 条（与 v2.3.3 行为一致）。
    """
    hits = []
    cap = limit * 5
    for date, rec in _iter_records(files, start, end):
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
    hits.sort(key=lambda x: x["time"])
    return {"total": len(hits), "items": hits[-limit:]}


def locate(files, want: str):
    """在归档文件中定位目标消息，返回 (date, index) 或 None。

    匹配优先级：记录自身的 seq，以及其引用（reply）的 id/message_id/seq。
    index 为文件内 0 基行号；从最新文件往回找（与 v2.3.3 一致）。
    """
    if not want:
        return None
    for fp in reversed(files):
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
                        return (date, idx)
                    idx += 1
        except Exception:
            continue
    return None


def stats(files):
    """统计：每日消息量、Top10 发送者、24 时段分布、总条数。"""
    daily = []
    by_sender = {}
    hourly = [0] * 24
    total = 0
    for fp in files:
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
    return {"total": total, "daily": daily, "top_senders": top, "hourly": hourly}


def file_date(fp: Path) -> str:
    """从归档文件名取日期。"""
    return fp.name.split("_")[-1][:-6]


def read_messages(files) -> list:
    """把若干 JSONL 文件读成带 `_date` 的完整记录列表（P2-1）。

    单独暴露成函数，便于调用方用 `asyncio.to_thread` 在线程池执行——
    整日文件可达上千行，同步读会阻塞 AstrBot 的事件循环。
    """
    out: list = []
    for fp in files:
        date = file_date(fp)
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
                    rec["_date"] = date
                    out.append(rec)
        except Exception:
            raise
    return out


def pick_recent_files(files, start: str = "", end: str = "",
                      days: int = 30):
    """按日期区间或最近 N 个文件筛选（区间优先）。"""
    if start or end:
        if start and end and start > end:
            start, end = end, start
        return [fp for fp in files
                if (not start or file_date(fp) >= start)
                and (not end or file_date(fp) <= end)]
    days = max(1, min(days, 365))
    return files[-days:] if len(files) > days else files
