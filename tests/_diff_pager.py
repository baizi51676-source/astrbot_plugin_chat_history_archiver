"""新旧翻页算法差分测试：用同一批模拟后端页，对比 v2.3.3 原逻辑与 Pager。

原逻辑从 _baseline/main_v233.py 的 _export_target 内联代码还原为独立函数。
只对比「收哪些消息 / 是否停 / 下一页锚点」，不涉及文件写入。
"""
import sys, types, importlib.util
from pathlib import Path

api = types.ModuleType("astrbot.api")
class _L:
    def __getattr__(self, n): return lambda *a, **k: None
api.logger = _L()
astrbot = types.ModuleType("astrbot"); astrbot.api = api
sys.modules["astrbot"] = astrbot; sys.modules["astrbot.api"] = api

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from core import pager as newpager
from core.backend import anchor_of, mid_of, seq_of, time_of


def old_export_target(fetch_pages, *, limit=0, today_only=False,
                      start_ts=0, end_ts=0, last_t=0, seen=None,
                      ignore_seen=False, today_start=0, backend="napcat"):
    """v2.3.3 原逻辑（逐行还原，仅把 await _fetch 换成预置页序列）。"""
    seen = set(seen or ())
    fetched = []
    guard = 0
    local_seen = set() if (ignore_seen and start_ts > 0) else set(seen)
    consecutive_empty = 0
    start = 0
    pages = list(fetch_pages)
    idx = 0
    stops = []
    while True:
        guard += 1
        if guard > 600:
            stops.append("超过600页"); break
        msgs = pages[idx] if idx < len(pages) else []
        idx += 1
        if not msgs:
            stops.append("空页"); break
        if limit > 0:
            fresh = [m for m in msgs if mid_of(m) not in local_seen]
            fetched.extend(fresh)
            local_seen.update(mid_of(m) for m in fresh)
            if len(fetched) >= limit:
                stops.append("达到limit"); break
            if not fresh and any(time_of(m) <= last_t for m in msgs):
                stops.append("无新消息且旧"); break
            if not fresh:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    stops.append("连续空页"); break
            else:
                consecutive_empty = 0
        else:
            if start_ts > 0:
                zone = [m for m in msgs if end_ts <= 0 or time_of(m) <= end_ts]
                fresh = [m for m in zone if time_of(m) >= start_ts
                         and mid_of(m) not in local_seen]
                fetched.extend(fresh)
                local_seen.update(mid_of(m) for m in fresh)
                if any(time_of(m) < start_ts for m in msgs):
                    stops.append("到区间起点"); break
                if zone and not fresh:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        stops.append("连续空区间"); break
                elif zone and fresh:
                    consecutive_empty = 0
            else:
                fresh = [m for m in msgs if time_of(m) > last_t
                         and mid_of(m) not in local_seen]
                if today_only:
                    fresh = [m for m in fresh if time_of(m) >= today_start]
                fetched.extend(fresh)
                local_seen.update(mid_of(m) for m in fresh)
                if today_only:
                    if any(time_of(m) < today_start for m in msgs):
                        stops.append("到当天边界"); break
                else:
                    if any(time_of(m) <= last_t for m in msgs):
                        stops.append("到游标边界"); break
                if not fresh:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        stops.append("连续空页"); break
                else:
                    consecutive_empty = 0
        if backend == "snowluma":
            anchor_msg = min(msgs, key=lambda m: (time_of(m), seq_of(m)))
            anchor_id = anchor_msg.get("message_id")
            if anchor_id is None or anchor_id == "":
                stops.append("snow锚点缺失"); break
            start = int(anchor_id)
            if start == 0 or len(fetched) >= 5000:
                stops.append("snow锚点0或上限"); break
            if len(msgs) == 1 and mid_of(msgs[0]) == mid_of(anchor_msg):
                stops.append("snow已到最早"); break
        else:
            anchors = [a for a in (anchor_of(m) for m in msgs) if a is not None]
            start = (min(anchors) - 1) if anchors else min(seq_of(m) for m in msgs)
            if start < 1 or len(fetched) >= 5000:
                stops.append("nap锚点<1或上限"); break
    return fetched, stops


def new_export_target(pages_fn, **kw):
    backend = kw.pop("backend", "napcat")
    p = newpager.Pager(**kw)
    stops = []
    for pg in pages_fn:
        d = p.decide_page(pg, backend)
        if d.stop:
            stops.append(d.reason)
            break
    return p.fetched, stops


def mk(seq, t, real=None, mid=None, uid="u"):
    d = {"message_seq": seq, "time": t, "message": [],
         "sender": {"user_id": uid, "nickname": "n"}}
    d["message_id"] = mid if mid is not None else seq
    if real is not None:
        d["real_seq"] = real
    return d


fails = []
def cmp(name, pages, **kw):
    of, os_ = old_export_target(pages, **kw)
    nf, ns = new_export_target(pages, **kw)
    ok_fetched = [mid_of(m) for m in of] == [mid_of(m) for m in nf]
    ok_count = len(of) == len(nf)
    # 旧实现在「页序列耗尽」时会再取一次空页并记录"空页"；
    # 新实现由调用方循环退出，不再产生该标记。两边都按「业务停止原因」比较，
    # 空页作为真实停止条件时仍会出现在 ns 中。
    old_biz = [s for s in os_ if s != "空页"]
    new_biz = [s for s in ns if s != "空页"]
    ok_stop = (bool(old_biz) == bool(new_biz)) or (len(of) == len(nf) and not old_biz and not new_biz)
    ok = ok_fetched and ok_count and ok_stop
    print(("  PASS " if ok else "  FAIL ") + name
          + ("" if ok else f" | old={len(of)}/{os_} new={len(nf)}/{ns}"))
    if not ok:
        fails.append((name, [mid_of(m) for m in of], [mid_of(m) for m in nf], os_, ns))


print("[增量模式]")
cmp("napcat 多页增量",
    [[mk(10, 1000), mk(9, 900)], [mk(8, 850), mk(7, 800)], [mk(6, 790)]],
    last_t=0, backend="napcat")
cmp("napcat 到游标边界",
    [[mk(10, 1000), mk(9, 900)], [mk(8, 500)]],
    last_t=950, backend="napcat")
cmp("napcat 有 real_seq",
    [[mk(10, 1000, real=10), mk(9, 900, real=9)], [mk(8, 800, real=8)]],
    last_t=0, backend="napcat")
cmp("空页立即停", [[mk(10, 1000)], []], last_t=0)
cmp("首日全旧即停", [[mk(1, 100)]], last_t=200)

print("[today_only]")
cmp("只收当天",
    [[mk(10, 12000), mk(9, 11000)], [mk(8, 9000)]],
    today_only=True, today_start=10000, backend="napcat")

print("[limit 模式]")
cmp("limit=3 跨页停",
    [[mk(10, 100), mk(9, 90)], [mk(8, 80), mk(7, 70)]],
    limit=3, backend="napcat")
cmp("limit 未达即遇旧",
    [[mk(10, 100)], [mk(5, 50)]],
    limit=5, last_t=90, backend="napcat")

print("[回溯模式]")
cmp("区间全收后停",
    [[mk(10, 1500), mk(9, 1200)], [mk(8, 900)]],
    start_ts=1000, end_ts=2000, ignore_seen=True, backend="napcat")
cmp("区间上界过滤",
    [[mk(10, 2500), mk(9, 1500)], [mk(8, 800)]],
    start_ts=1000, end_ts=2000, ignore_seen=True, backend="napcat")

print("[snowluma]")
cmp("snow 多页",
    [[mk(10, 1000, mid=111), mk(9, 900, mid=222)],
     [mk(8, 800, mid=333)]],
    last_t=0, backend="snowluma")
cmp("snow 单条即早",
    [[mk(10, 1000, mid=111)]], last_t=0, backend="snowluma")
cmp("snow 锚点缺失",
    [[mk(10, 1000, mid="")]], last_t=0, backend="snowluma")

print()
print("FAILS:", [f[0] for f in fails] if fails else "none")
for f in fails:
    print("  ", f)
sys.exit(1 if fails else 0)
