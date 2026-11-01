"""真·多进程并发测试：验证跨进程文件锁防止更新丢失。

原子写只能保证「不出现半截文件」，但不保证「两次并发读改写不丢更新」。
本测试用 multiprocessing 起多个进程同时向同一文件写不同 seq，
正确结果应是：所有条目的并集都在文件里（一条不丢）。

对照组：禁用锁（monkeypatch 成空上下文）时应当出现丢失。
"""
import sys, types, tempfile, json, os
from pathlib import Path
import multiprocessing as mp

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

# 桩件：store.py 依赖 astrbot.api.logger
api = types.ModuleType("astrbot.api")
class _L:
    def __getattr__(self, n): return lambda *a, **k: None
api.logger = _L()
at = types.ModuleType("astrbot"); at.api = api
sys.modules["astrbot"] = at
sys.modules["astrbot.api"] = api

from core import store, filelock, atomic  # noqa: E402


def _worker(path_str, seq_base, n, use_lock, barrier):
    """子进程：写入 n 条以 seq_base 开头的记录。"""
    import importlib
    import core.store  # noqa: F401  确保子进程环境一致
    path = Path(path_str)
    if not use_lock:
        # 对照组：把 locked 换成空上下文（模拟无跨进程锁）
        import contextlib
        filelock.locked = lambda *a, **k: contextlib.nullcontext()
        store._filelock = filelock
    barrier.wait()          # 尽量让各进程同时开始读改写
    recs = [{"t": f"2026-11-01 10:{i // 60:02d}:{i % 60:02d}",
             "seq": seq_base + i,
             "user_id": "u", "content": f"m{seq_base + i}"}
            for i in range(n)]
    store.merge_write(path, recs)


def run_case(tmp: Path, nproc: int, per_proc: int, use_lock: bool) -> int:
    """返回最终文件里的条目数（去重后）。"""
    path = tmp / ("locked.jsonl" if use_lock else "nolock.jsonl")
    path.unlink(missing_ok=True)
    barrier = mp.Barrier(nproc)
    procs = [mp.Process(target=_worker,
                        args=(str(path), 10000 + i * 1000, per_proc,
                              use_lock, barrier))
             for i in range(nproc)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    if not path.exists():
        return 0
    keys = set()
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            keys.add(json.loads(ln)["seq"])
    return len(keys)


fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond: fails.append(name)

if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp())
    NP, PER = 6, 40
    expect = NP * PER

    print(f"[并发] {NP} 进程 × {PER} 条 = {expect} 条（各自 seq 不重叠）")

    print()
    print("[1] 有跨进程锁")
    n_locked = run_case(tmp, NP, PER, use_lock=True)
    print(f"  最终条目数: {n_locked} / {expect}")
    check("无更新丢失（全部写入）", n_locked == expect, f"{n_locked} vs {expect}")

    print()
    print("[2] 对照组：禁用锁")
    n_nolock = run_case(tmp, NP, PER, use_lock=False)
    print(f"  最终条目数: {n_nolock} / {expect}")
    # 注意：并发是否真正撞上取决于调度，不保证每次都丢。
    # 这里记录现象而非严格断言「必须丢」。
    if n_nolock < expect:
        print(f"  → 观察到丢失 {expect - n_nolock} 条（对照组证实了该问题的存在）")
    else:
        print("  → 本次调度未撞上（不代表无锁安全，只是概率事件）")

    print()
    print("[3] 锁文件已生成")
    lp = filelock.lock_path_for(tmp / "locked.jsonl")
    check("存在 .lock 文件", lp.exists(), lp)
    check("lock_path_for 命名正确",
          lp.name == "locked.jsonl.lock", lp.name)

    print()
    print("[4] 锁不会破坏追加快路径")
    p = tmp / "append.jsonl"
    p.unlink(missing_ok=True)
    n1 = store.merge_write(p, [{"t": "2026-11-01 10:00:00", "seq": 1, "user_id": "u"}])
    n2 = store.merge_write(p, [{"t": "2026-11-01 10:01:00", "seq": 2, "user_id": "u"}])
    check("首次写入 1", n1 == 1, n1)
    check("二次追加 1", n2 == 1, n2)
    check("文件 2 行", len(p.read_text(encoding="utf-8").strip().splitlines()) == 2)

    print()
    print("[5] 无锁环境下仍能正常写入（退化不崩）")
    import contextlib
    orig = filelock.locked
    filelock.locked = lambda *a, **k: contextlib.nullcontext()
    store._filelock.locked = filelock.locked
    p2 = tmp / "nolock_env.jsonl"
    p2.unlink(missing_ok=True)
    try:
        n = store.merge_write(p2, [{"t": "2026-11-01 10:00:00", "seq": 1}])
        check("退化后仍可写", n == 1, n)
    finally:
        filelock.locked = orig
        store._filelock.locked = orig

    print()
    print("FAILS:", fails if fails else "none")
    sys.exit(1 if fails else 0)
