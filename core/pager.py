"""导出翻页算法（从 main.py 的 _export_target 提取）。

本模块只负责「给定一页消息，决定要不要收、要不要停、下一个锚点是什么」，
不做网络 IO、不写文件、不读 state——这些由调用方注入。

三种导出模式（与 v2.3.3 语义等价）
---------------------------------
- ``limit > 0``     按需导出最近 N 条：收集未写过的消息，达到 limit 即停
- ``start_ts > 0``  回溯导出 [start_ts, end_ts]：不推进游标，忽略已归档 id
- 其余              增量导出：以 last_t 为边界（today_only 时只收当天）

硬性守卫（一个都不能少，都是历史踩坑的沉淀）
-----------------------------------------
- 单次最多翻 600 页
- 累计 fetched >= 5000 条即停
- 连续 3 页无新增即停
- NapCat：start < 1 停；SnowLuma：锚点 id 为 0 或单页仅锚点一条即停
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .backend import anchor_of, mid_of, seq_of, time_of

#: 防御：单次最多翻页数（v1.3.4）
MAX_PAGES = 600
#: 单次导出的消息条数上限
MAX_FETCHED = 5000
#: 连续无新增页数阈值
MAX_CONSECUTIVE_EMPTY = 3


@dataclass
class PageDecision:
    """一页消息处理后的决策结果。"""

    fresh: list = field(default_factory=list)
    """本页应收录的新消息。"""

    stop: bool = False
    """是否应停止翻页。"""

    next_anchor: int | None = None
    """下一页的起点锚点；None 表示沿用原值 / 已无需翻页。"""

    reason: str = ""
    """停止原因（用于日志与排障）。"""


class Pager:
    """翻页状态机。

    调用方每拿到一页就 ``decide_page()``，按 ``decision.stop`` 决定是否退出，
    再按 ``decision.next_anchor`` 更新起点。``fetched`` 由本对象累积。
    """

    def __init__(self, *, limit: int = 0, today_only: bool = False,
                 start_ts: int = 0, end_ts: int = 0,
                 last_t: int = 0, seen=None, ignore_seen: bool = False,
                 today_start: int = 0):
        self.limit = limit
        self.today_only = today_only
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.last_t = last_t
        self.today_start = today_start
        # v2.1.0: ignore_seen 用于回溯补全——文件才是唯一权威；页间防重叠
        # 仍由 local_seen 在本轮内维护
        self.local_seen = (set() if (ignore_seen and start_ts > 0)
                           else set(seen or ()))
        self.fetched: list = []
        self.pages = 0
        self.consecutive_empty = 0

    # ---------------- 页内筛选 ----------------

    def _take_limit(self, msgs) -> PageDecision:
        """模式 1：按需导出最近 limit 条。"""
        fresh = [m for m in msgs if mid_of(m) not in self.local_seen]
        self.fetched.extend(fresh)
        self.local_seen.update(mid_of(m) for m in fresh)
        if len(self.fetched) >= self.limit:
            return PageDecision(fresh=fresh, stop=True, reason="达到 limit")
        # 本页没有新消息且包含旧消息 → 没有更多可导出的了
        if not fresh and any(time_of(m) <= self.last_t for m in msgs):
            return PageDecision(fresh=fresh, stop=True, reason="无新消息且已到旧消息")
        if not fresh:
            self.consecutive_empty += 1
            if self.consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                return PageDecision(fresh=fresh, stop=True,
                                    reason="连续空页")
        else:
            self.consecutive_empty = 0
        return PageDecision(fresh=fresh)

    def _take_range(self, msgs) -> PageDecision:
        """模式 2：回溯时间段导出（不推进游标）。"""
        zone = [m for m in msgs if self.end_ts <= 0 or time_of(m) <= self.end_ts]
        fresh = [m for m in zone
                 if time_of(m) >= self.start_ts
                 and mid_of(m) not in self.local_seen]
        self.fetched.extend(fresh)
        self.local_seen.update(mid_of(m) for m in fresh)
        if any(time_of(m) < self.start_ts for m in msgs):
            return PageDecision(fresh=fresh, stop=True, reason="已到时间段起点")
        if zone and not fresh:
            self.consecutive_empty += 1
            if self.consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                return PageDecision(fresh=fresh, stop=True,
                                    reason="连续空区间页")
        elif zone and fresh:
            self.consecutive_empty = 0
        # zone 为空（页内全是 end_ts 之后的消息）→ 不计数，继续往前翻
        return PageDecision(fresh=fresh)

    def _take_incremental(self, msgs) -> PageDecision:
        """模式 3：增量导出（today_only 时只收当天）。"""
        fresh = [m for m in msgs
                 if time_of(m) > self.last_t
                 and mid_of(m) not in self.local_seen]
        if self.today_only:
            fresh = [m for m in fresh if time_of(m) >= self.today_start]
        self.fetched.extend(fresh)
        self.local_seen.update(mid_of(m) for m in fresh)
        if self.today_only:
            # 遇当天之前（含昨天及更早）的消息 → 本页已到当天边界
            if any(time_of(m) < self.today_start for m in msgs):
                return PageDecision(fresh=fresh, stop=True, reason="已到当天边界")
        else:
            if any(time_of(m) <= self.last_t for m in msgs):
                return PageDecision(fresh=fresh, stop=True, reason="已到游标边界")
        if not fresh:
            self.consecutive_empty += 1
            if self.consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                return PageDecision(fresh=fresh, stop=True,
                                    reason="连续空页")
        else:
            self.consecutive_empty = 0
        return PageDecision(fresh=fresh)

    # ---------------- 对外接口 ----------------

    def decide_page(self, msgs, backend: str) -> PageDecision:
        """处理一页消息：返回收录结果 + 是否停止 + 下一页锚点。"""
        self.pages += 1
        if self.pages > MAX_PAGES:
            return PageDecision(stop=True, reason="超过 600 页")
        if not msgs:
            return PageDecision(stop=True, reason="空页")

        if self.limit > 0:
            d = self._take_limit(msgs)
        elif self.start_ts > 0:
            d = self._take_range(msgs)
        else:
            d = self._take_incremental(msgs)

        if d.stop:
            return d

        # 向前翻页（拿更早的）
        if backend == "snowluma":
            anchor_msg = min(msgs, key=lambda m: (time_of(m), seq_of(m)))
            anchor_id = anchor_msg.get("message_id")
            if anchor_id is None or anchor_id == "":
                return PageDecision(fresh=d.fresh, stop=True,
                                    reason="SnowLuma 锚点缺失")
            start = int(anchor_id)
            if start == 0 or len(self.fetched) >= MAX_FETCHED:
                return PageDecision(fresh=d.fresh, stop=True,
                                    reason="锚点为 0 或已达上限")
            # 本页只有锚点一条 → 已翻到最早，下页必然重复/为空
            if len(msgs) == 1 and mid_of(msgs[0]) == mid_of(anchor_msg):
                return PageDecision(fresh=d.fresh, stop=True,
                                    reason="已到最早")
            return PageDecision(fresh=d.fresh, next_anchor=start)
        else:
            # NapCat：v1.3.4 优先用 real_seq（单调递增，传 min-1 精确定位
            # 更早消息）；无 real_seq 时回退页内最小 message_seq
            anchors = [a for a in (anchor_of(m) for m in msgs) if a is not None]
            start = (min(anchors) - 1) if anchors else min(seq_of(m) for m in msgs)
            if start < 1 or len(self.fetched) >= MAX_FETCHED:
                return PageDecision(fresh=d.fresh, stop=True,
                                    reason="锚点<1 或已达上限")
            return PageDecision(fresh=d.fresh, next_anchor=start)

    def final_records(self, limit: int = 0) -> list:
        """按 time 排序后的最终消息集合（limit 模式再截断）。"""
        return sorted(self.fetched[:limit] if limit > 0 else self.fetched,
                      key=time_of)

    def merge_ids(self, existing=None, cap: int = 5000) -> list:
        """更新后的去重 id 列表（保留最近 cap 个）。"""
        seen = set(existing or ())
        seen |= {mid_of(m) for m in self.fetched if mid_of(m)}
        return list(seen)[-cap:]
