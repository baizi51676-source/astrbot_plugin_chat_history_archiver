"""JSONL 归档的读写、合并、拆分与扫描（不涉及网络与插件状态）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from astrbot.api import logger

from . import atomic as _atomic
from . import filelock as _filelock

_TARGET_FILE_RE = re.compile(
    r"^napcat_(private_)?(\d+)_\d{4}-\d{2}-\d{2}\.jsonl$")
_MERGED_FILE_RE = re.compile(r"napcat_(private_)?(\d+)\.jsonl$")
_CLEAN_FILE_RE = re.compile(
    r"napcat_(private_)?(\d+)_(\d{4}-\d{2}-\d{2})\.jsonl$")


def target_path(export_dir: Path, chat: str, target_id: str, date: str) -> Path:
    """按天分文件：napcat_<群号>_YYYY-MM-DD.jsonl（私聊 napcat_private_<QQ>_*）。"""
    if chat == "group":
        return Path(export_dir) / f"napcat_{target_id}_{date}.jsonl"
    return Path(export_dir) / f"napcat_private_{target_id}_{date}.jsonl"


def target_files(export_dir: Path, chat: str, tid: str) -> list:
    """某目标全部归档文件（按文件名升序 = 旧 → 新）。"""
    prefix = "napcat_private_" if chat == "private" else "napcat_"
    return sorted(Path(export_dir).glob(f"{prefix}{tid}_*.jsonl"))


def date_of_file(fp: Path) -> str:
    """从文件名取日期（YYYY-MM-DD）。"""
    return fp.name.split("_")[-1][:-6]


def scan_archived_targets(export_dir: Path) -> list:
    """v2.1.0: 扫描导出目录，收集已有归档记录的目标 [(chat, target_id)]。"""
    out: list = []
    seen: set = set()
    try:
        for f in Path(export_dir).glob("napcat_*_????-??-??.jsonl"):
            m = _TARGET_FILE_RE.match(f.name)
            if not m:
                continue
            chat = "private" if m.group(1) else "group"
            tid = m.group(2)
            key = (chat, tid)
            if key not in seen:
                seen.add(key)
                out.append(key)
    except Exception as e:
        logger.error(f"[归档检查] 扫描导出目录失败: {e}")
    return out


def merge_write(path: Path, records: list) -> int:
    """合并写入。返回实际新增条数（重复消息不计入）。

    v1.3.3 语义：读现有行 + 新记录，按 seq 去重、按 t 排序后整体重写；
    文件自愈（截断/缺行可恢复）、永不重复、天然有序。

    v2.4.0 优化：
    - **追加快路径**（P2-2）：新记录全部「比现有末条更新」、内部有序且
      不与末条重复 → 直接 append（O(新增条数)，不再 O(文件大小)）。
    - **原子慢路径**（P2-12）：需要重排/去重时，写临时文件再 ``os.replace``，
      避免进程被杀时留下半截文件。
    - **跨进程锁**（P2-12 补充）：整个「读-改-写」在 `<文件>.lock` 的独占锁内
      完成。原子写只保证不出现半截文件，不保证两次并发写不丢更新——
      实例级 asyncio.Lock 管不了「同机两份副本 / 重载竞态 / 外部脚本写入」。
    """
    if not records:
        return 0

    with _filelock.locked(path):
        return _merge_write_locked(path, records)


def _merge_write_locked(path: Path, records: list) -> int:
    """持锁后的实际合并逻辑（快路径 / 慢路径）。"""
    # 快路径：顺序追加（P2-2）
    if _atomic.can_append(path, records):
        keys = {_atomic.record_key(r) for r in records}
        _atomic.append_records(path, records)
        # can_append 已保证不与末行重复且 t 递增，新增数 = 去重后的条数
        return len(keys)

    # 慢路径：合并-去重-排序-原子重写
    merged: dict = {}
    if path.exists():
        for ln in path.read_text(encoding="utf-8",
                                 errors="replace").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
                merged[_atomic.record_key(r)] = r
            except Exception:
                continue
    before = len(merged)  # v2.1.0: 合并前已有（去重）条数基线
    for r in records:
        merged[_atomic.record_key(r)] = r  # 新记录覆盖旧记录
    ordered = sorted(merged.values(), key=lambda r: str(r.get("t", "")))
    # P2-12: 原子替换，避免半截文件
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered)
    _atomic.atomic_write_text(path, text)
    return max(0, len(ordered) - before)


def split_merged_files(export_dir: Path) -> None:
    """v1.2.0 迁移：若存在 v1.1.0 单文件 napcat_<群号>.jsonl，
    按行内 t 字段拆回按天文件 napcat_<群号>_YYYY-MM-DD.jsonl，
    随后删除单文件（避免与按天分文件模式冲突）。
    """
    export_dir = Path(export_dir)
    try:
        if not export_dir.is_dir():
            return
        for p in sorted(export_dir.glob("napcat_*.jsonl")):
            if p.name == "state.json":
                continue
            m = _MERGED_FILE_RE.match(p.name)
            if not m:
                continue  # 已是按天文件，跳过
            prefix = m.group(1) or ""
            gid = m.group(2)
            try:
                lines = [ln for ln in
                         p.read_text(encoding="utf-8", errors="replace")
                         .splitlines() if ln.strip()]
            except Exception as e:
                logger.error(f"[NapCatExporter] 拆分读取失败 {p.name}: {e}")
                continue
            if not lines:
                p.unlink(missing_ok=True)
                continue
            # 按行内 t 字段的前 10 位（YYYY-MM-DD）分组
            by_date: dict = {}
            for ln in lines:
                d = ""
                try:
                    d = str(json.loads(ln).get("t", ""))[:10]
                except Exception:
                    pass
                by_date.setdefault(d or "unknown", []).append(ln)
            for date, day_lines in by_date.items():
                target = export_dir / f"napcat_{prefix}{gid}_{date}.jsonl"
                try:
                    with open(target, "a", encoding="utf-8") as f:
                        f.write("\n".join(day_lines) + "\n")
                    logger.info(f"[NapCatExporter] 拆分: {p.name} → "
                                f"{target.name}（{len(day_lines)}行）")
                except Exception as e:
                    logger.error(f"[NapCatExporter] 拆分写入失败 "
                                 f"{target.name}: {e}")
            p.unlink(missing_ok=True)
            logger.info(f"[NapCatExporter] 单文件已删除: {p.name}")
    except Exception as e:
        logger.error(f"[NapCatExporter] 单文件拆分迁移异常: {e}")


def cleanup_old_files(export_dir: Path, state: dict, clean_days: int,
                      auto_clean: bool) -> int:
    """v1.3.0: 自动清理过期历史文件（仅循环导出模式调用）。

    删除文件名日期早于（今天 - clean_days）的 napcat_*_YYYY-MM-DD.jsonl；
    手动归档（protected）过的目标文件不清理。返回删除数量。
    """
    if not auto_clean:
        return 0
    from datetime import datetime, timedelta

    cutoff = (datetime.now().date()
              - timedelta(days=clean_days)).strftime("%Y-%m-%d")
    protected = set()
    for g in state.get("protected", {}).get("group", []) or []:
        protected.add(f"napcat_{g}_")
    for u in state.get("protected", {}).get("private", []) or []:
        protected.add(f"napcat_private_{u}_")
    removed = 0
    for f in Path(export_dir).glob("napcat_*_????-??-??.jsonl"):
        m = _CLEAN_FILE_RE.match(f.name)
        if not m:
            continue
        prefix = f"napcat_{m.group(1) or ''}{m.group(2)}_"
        if prefix in protected:
            continue  # 手动归档过的目标，不自动清理
        fdate = m.group(3)
        if fdate < cutoff:
            try:
                f.unlink(missing_ok=True)
                logger.info(f"[NapCatExporter] 自动清理过期文件: "
                            f"{f.name}（早于 {cutoff}）")
                removed += 1
            except Exception as e:
                logger.error(f"[NapCatExporter] 清理文件失败 {f.name}: {e}")
    if removed:
        logger.info(f"[NapCatExporter] 本轮自动清理 {removed} 个过期文件")
    return removed


def _count_newlines(fp) -> int:
    """统计行数（P2-1）。

    实测结论（本机，90 文件 / 57,600 行）：在操作系统文件缓存命中时，
    直接逐行迭代（~74ms）比按字节数换行（~102ms）**更快**——因为 Python
    的文本迭代器是 C 实现，而字节分段读取还多了一次 1MiB 拷贝。
    （早期做过一次「字节计数 vs 逐行」的对比得出 73 倍差距，那个结论是错的：
      当时拿「首次冷读」与「缓存热读」比较，差异来自磁盘而非算法。）

    因此这里保留逐行迭代，只保证「不阻塞事件循环」（由调用方 to_thread）。
    """
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def count_rows_cached(files, cache: dict) -> int:
    """统计 JSONL 行数（按 mtime/size 缓存，避免每次全量重算）。"""
    total = 0
    for fp in files:
        try:
            st = fp.stat()
            key = str(fp)
            hit = cache.get(key)
            if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
                total += hit[2]
                continue
            rows = _count_newlines(fp)   # P2-1：字节计数替代逐行迭代
            cache[key] = (st.st_mtime_ns, st.st_size, rows)
            total += rows
        except Exception:
            continue
    return total


def read_archive_records(files) -> list:
    """把若干 JSONL 文件读成 [{"t","user_id","nickname","content"}]。"""
    out: list = []
    for fpath in files:
        try:
            raw_lines = fpath.read_text(encoding="utf-8",
                                        errors="replace").splitlines()
        except Exception as e:
            logger.error(f"读取归档文件失败 {fpath}: {e}")
            continue
        for raw in raw_lines:
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            out.append({
                "t": d.get("t", ""),
                "user_id": str(d.get("user_id", "") or ""),
                "nickname": d.get("nickname", ""),
                "content": d.get("content", ""),
            })
    return out
