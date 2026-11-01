"""每日定时总结 / 简报的调度判定（P1-1）。

纯逻辑：给定当前时间、目标时刻、上次执行日期，判断「本次是否该跑」。

设计要点
--------
- 只做**日期级**判重：每个自然日最多触发一次（记录 last_run_date）。
- 错过不补跑中间日：插件若停了一周再启动，只按「今天」跑一次，
  不会把七天的总结一次性补齐（避免启动时突然刷爆 LLM 额度）。
- 时刻容差：调度轮询间隔（默认 120s）可能跨过目标时刻，
  因此判定为「当前时间 >= 目标时刻 且 今天尚未跑过」，而不是精确匹配整分。
"""

from __future__ import annotations

from datetime import datetime, time


def parse_hhmm(raw, default: str = "23:30") -> time:
    """解析 HH:MM；非法输入回落到默认值。"""
    text = str(raw or "").strip()
    if not text:
        text = default
    try:
        hh, mm = text.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("out of range")
        return time(hour=hh, minute=mm)
    except Exception:
        hh, mm = default.split(":")
        return time(hour=int(hh), minute=int(mm))


def should_run(now: datetime, target: time, last_run_date: str,
               enabled: bool) -> bool:
    """判断到点任务此刻是否应执行。

    Args:
        now: 当前时间（本地时区）。
        target: 每日目标时刻。
        last_run_date: 上次执行的日期字符串 ``YYYY-MM-DD``（未执行为空）。
        enabled: 开关状态。

    Returns:
        True 表示本次轮询应触发。
    """
    if not enabled:
        return False
    if now.time() < target:
        return False
    return now.strftime("%Y-%m-%d") != (last_run_date or "")


def summary_date(now: datetime) -> str:
    """定时总结的目标日期：总是「当天」。

    23:30 触发时当天消息基本已经完整落盘，总结当天最合理。
    """
    return now.strftime("%Y-%m-%d")


def briefing_days(now: datetime, window: int = 3) -> int:
    """简报取最近多少天的总结。

    默认沿用页面上「最近 3 天」的口径；这里只做边界夹取。
    """
    try:
        n = int(window)
    except Exception:
        n = 3
    return max(1, min(n, 30))
