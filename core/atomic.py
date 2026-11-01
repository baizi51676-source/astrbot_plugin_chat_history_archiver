"""原子写 + 追加快路径（P2-12 / P2-2）。

两个独立但相关的问题
--------------------
**P2-12 原子写**：原 `_merge_write` 直接 `open(path, "w")` 覆盖写。
一旦进程在写一半时被杀（重载插件、OOM、断电），文件会变成半截，
下次读到的就是截断数据。改为「写临时文件 → `os.replace` 原子替换」。

**P2-2 整文件重写**：每轮定时（默认 120s）对当天有新消息的每个目标
都重写整个文件（O(n) I/O），文件越大越慢。改为：
- **快路径**：若新记录全部「比现有最后一条更新」且无重复 → 直接追加，
  代价 O(新增条数)；
- **慢路径**：出现乱序 / 重复 / 文件为空 → 走原来的合并-去重-排序-重写
  （保持 v1.3.3 的「文件自愈」语义不变）。

不能无条件追加的原因：归档文件按 `t` 排序，而回溯补全（`start_ts` 模式）
会追加更早的消息；若直接 append 会破坏顺序，后续读取与定位都会错。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """原子写：临时文件 + os.replace（P2-12）。

    `os.replace` 在同一目录内是原子操作（Windows 上也是），
    因此文件要么是旧内容、要么是新内容，不会出现半截。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_",
                               suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def record_key(r: dict) -> str:
    """与 v1.3.3 一致的去重键：优先 seq，退化 user_id+t。"""
    return str(r.get("seq") or (r.get("user_id", "") + r.get("t", "")))


def _last_line_key(path: Path):
    """读文件最后一行的 (key, t)；空文件或读失败返回 None。"""
    try:
        with open(path, "rb") as f:
            # 从尾部读一小段找最后一行，避免整文件读入
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            back = min(size, 4096)
            f.seek(size - back)
            tail = f.read().decode("utf-8", errors="replace")
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        if not lines:
            return None
        obj = json.loads(lines[-1])
        return record_key(obj), str(obj.get("t", ""))
    except Exception:
        return None


def can_append(path: Path, records: list) -> bool:
    """判断能否走追加快路径（P2-2）。

    条件（全部满足才追加）：
    1. 文件已存在且非空；
    2. records 内部按 t 严格不减；
    3. 第一条的 t >= 文件最后一行的 t（不破坏排序）；
    4. records 的 key 与最后一行不重复（去重语义仍成立）。

    否则交给慢路径（合并-去重-排序-重写），保持自愈能力。
    """
    if not records:
        return False
    path = Path(path)
    if not path.exists():
        return False
    tail = _last_line_key(path)
    if tail is None:
        return False
    last_key, last_t = tail

    prev_t = None
    keys = set()
    for r in records:
        t = str(r.get("t", ""))
        if prev_t is not None and t < prev_t:
            return False        # 内部乱序
        prev_t = t
        keys.add(record_key(r))
    first_t = str(records[0].get("t", ""))
    if first_t < last_t:
        return False            # 会破坏与已有内容的顺序
    if last_key in keys:
        return False            # 与最后一行重复（需走合并覆盖语义）
    return True


def append_records(path: Path, records: list) -> None:
    """追加写入（快路径）。逐行写，不重排。"""
    lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(lines)
        f.flush()
