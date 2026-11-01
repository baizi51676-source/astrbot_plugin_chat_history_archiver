"""跨进程文件锁（P2-12 的补充）。

为什么还需要它
--------------
`os.replace` 的原子写（`atomic.py`）保证「不会出现半截文件」，
但**不保证「两次并发写不丢更新」**：两个进程同时执行
「读旧文件 → 合并 → 重写」时，后者会覆盖前者的新增条目。

`main.py` 里的 `_file_lock` 是**实例级** `asyncio.Lock`，只在单进程内有效。
实际会出问题的场景：
- 同机跑两份插件副本（测试环境 / 迁移期）；
- 「旧实例尚未完全停止 + 新实例已加载」（重载竞态）；
- 用户手工用脚本写入归档目录。

实现
----
优先用 `fcntl.flock`（Linux，AstrBot 容器就是 Linux）；
Windows 退回 `msvcrt.locking`；两者都不可用时退化为无锁（只记 debug）。

用法：
    with locked(lock_path):
        ...读改写...
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

_LOCK_SUFFIX = ".lock"


def lock_path_for(target: Path) -> Path:
    """给定被保护的文件，返回它的锁文件路径。"""
    target = Path(target)
    return target.with_name(target.name + _LOCK_SUFFIX)


@contextlib.contextmanager
def locked(target: Path, timeout_note: str = ""):
    """对目标文件加独占锁（跨进程）。

    Args:
        target: 需要保护的文件（会在其旁边建 `<name>.lock`）。
        timeout_note: 仅用于排障提示（阻塞式加锁，超时由 OS 处理）。
    """
    lp = lock_path_for(target)
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lp, "a+b")
    except Exception:
        # 打不开锁文件：退化为无锁（不能因此阻断归档）
        yield
        return

    try:
        try:
            import fcntl  # Linux / macOS
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            return
        except ImportError:
            pass

        try:
            import msvcrt  # Windows
            # msvcrt.locking 需要文件有内容才能定位；写入 1 字节占位
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            return
        except ImportError:
            pass

        # 两个模块都没有：退化为无锁
        yield
    finally:
        try:
            fh.close()
        except Exception:
            pass
