"""启动归档检查的进度跟踪与工作量估算（P1-5）。

背景
----
v2.3.3 的 `_startup_verify_once` 是 `verify_days × 全部目标` 的串行循环，
结果不可续传：每次「保存配置 / 重载插件」都从头重跑。现网实测 6 目标 ×
30 天（实际被静默截断）在 57,797 行 / 87 文件规模下可持续数小时，
期间反复整文件重写、与定时轮争抢 API 配额。

本模块只做纯逻辑：把「已经检查过的 (目标, 日期)」记成可落盘的状态，
让中断后能续跑，并提供一个工作量估算用于日志与 UI。

关键设计
--------
- **按 (chat, tid, date) 三键记录**，而不是只记「跑到第几个目标」：
  这样即使用户中途改了 `verify_days`，已完成的日期仍然有效、不会重跑。
- **按日期窗口裁剪**：只保留当前检查窗口内的键，避免状态文件无限增长。
- **检查完成时间戳**：全部跑完后记一条，重载时若窗口未变可直接跳过。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta


def item_key(chat: str, tid: str, day: date | str) -> str:
    """单个检查项的唯一键：``<chat>|<tid>|<YYYY-MM-DD>``。"""
    d = day if isinstance(day, str) else day.strftime("%Y-%m-%d")
    return f"{chat}|{tid}|{d}"


def window_key(targets, days: int) -> str:
    """当前检查窗口的指纹（目标集合 + 天数）。

    用于判断「重载后是否需要重新跑」：窗口没变且上次已跑完 → 直接跳过。
    """
    parts = sorted(item_key(c, t, "x") for c, t in targets)
    raw = ",".join(parts) + f"|days={days}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def new_state() -> dict:
    return {"done": [], "window": "", "finished_at": ""}


def normalize_loaded(data) -> dict:
    """把读到的 JSON 规范成预期结构。"""
    if not isinstance(data, dict):
        return new_state()
    st = new_state()
    done = data.get("done")
    if isinstance(done, list):
        st["done"] = [str(x) for x in done if isinstance(x, str)]
    st["window"] = str(data.get("window") or "")
    st["finished_at"] = str(data.get("finished_at") or "")
    return st


def is_done(state: dict, key: str) -> bool:
    return key in (state.get("done") or ())


def mark_done(state: dict, key: str) -> bool:
    """标记某项已完成。返回 True 表示本次是新标记（调用方可据此决定是否落盘）。"""
    done = state.setdefault("done", [])
    if key in done:
        return False
    done.append(key)
    return True


def plan_items(targets, days: int, today: date, state: dict,
               auto_clean: bool = False, clean_days: int = 14,
               start_from_today: bool = True):
    """规划本轮要检查的 (day, chat, tid) 清单，并跳过已完成的项。

    返回 (items, skipped_done, skipped_retention)：
      items              — 仍需检查的项，按「从今天往前」的顺序
      skipped_done       — 因已完成而跳过的数量
      skipped_retention  — 因超出保留期而跳过的数量
    """
    items = []
    skipped_done = 0
    skipped_retention = 0
    cutoff = today - timedelta(days=clean_days)
    for back in range(days):
        day = today - timedelta(days=back)
        if auto_clean and day < cutoff:
            skipped_retention += 1
            continue
        for chat, tid in targets:
            key = item_key(chat, tid, day)
            if is_done(state, key):
                skipped_done += 1
                continue
            items.append({"day": day, "chat": chat, "tid": tid, "key": key})
    return items, skipped_done, skipped_retention


def prune(state: dict, targets, days: int, today: date,
          keep_extra_days: int = 7) -> int:
    """裁掉窗口外的已完成键，防止状态文件无限增长。返回被裁掉的数量。"""
    valid_targets = {(c, str(t)) for c, t in targets}
    earliest = today - timedelta(days=days + keep_extra_days)
    kept = []
    removed = 0
    for key in state.get("done") or ():
        try:
            chat, tid, ds = key.split("|", 2)
            d = datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            removed += 1
            continue
        if (chat, tid) not in valid_targets or d < earliest:
            removed += 1
            continue
        kept.append(key)
    state["done"] = kept
    return removed


def format_workload(targets, days: int) -> str:
    """给日志用的工作量描述，例如「6 目标 × 30 天 = 180 次」。"""
    return f"{len(targets)} 目标 × {days} 天 = {len(targets) * days} 次检查"
