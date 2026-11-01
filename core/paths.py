"""数据目录解析与迁移（从 main.py 模块级函数提取，v2.3.1 规则不变）。"""

from __future__ import annotations

import shutil
from pathlib import Path

from astrbot.api import logger

#: v2.3.1 之前的默认导出目录（data/workspaces 属 AstrBot 旧工作区约定）。
LEGACY_EXPORT_DIR = "data/workspaces/napcat_exports"

PLUGIN_NAME = "astrbot_plugin_chat_history_archiver"


def plugin_data_root() -> Path:
    """返回 AstrBot 的 data/plugin_data 目录（优先官方 API，逐级降级）。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        return Path(get_astrbot_plugin_data_path())
    except Exception:
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return Path(get_astrbot_data_path()) / "plugin_data"
    except Exception:
        pass
    return Path("data") / "plugin_data"


def plugin_dir_name(plugin_file: str | Path) -> str:
    """由插件入口文件路径推导插件目录名（即市场里的插件名）。"""
    return Path(plugin_file).resolve().parent.name


def default_export_dir(plugin_file: str | Path) -> Path:
    return plugin_data_root() / plugin_dir_name(plugin_file)


def legacy_export_dir_path() -> Path:
    """旧默认目录：<root>/data/plugin_data -> <root>/data/workspaces/napcat_exports。"""
    return plugin_data_root().parent / "workspaces" / "napcat_exports"


def resolve_export_dir(raw, plugin_file: str | Path):
    """解析 export_dir 配置。

    规则（v2.3.1，满足插件市场「数据持久化位置」规范）：
    - 留空或仍是旧默认值 → 用新默认 data/plugin_data/<插件名>（并返回旧目录用于迁移）
    - 相对路径且落在 data/workspaces/** → 视为旧数据位置，改用新默认并自动迁移
    - 其它相对路径 → 限制在 data/plugin_data/<插件名>/ 下
    - 绝对路径 → 原样使用（在 plugin_data 之外会告警）

    返回 (最终目录, 需要迁移的旧目录或 None)
    """
    default_dir = default_export_dir(plugin_file)
    raw = str(raw or "").strip().replace("\\", "/")
    if raw in ("", LEGACY_EXPORT_DIR, "./" + LEGACY_EXPORT_DIR,
               LEGACY_EXPORT_DIR + "/"):
        return default_dir, legacy_export_dir_path()
    p = Path(raw).expanduser()
    if p.is_absolute():
        try:
            inside = str(p.resolve()).startswith(str(plugin_data_root().resolve()))
        except Exception:
            inside = False
        if not inside:
            logger.warning(
                f"[{PLUGIN_NAME}] export_dir 位于 plugin_data 之外：{p}；"
                "该位置不受 AstrBot 备份/迁移覆盖，建议改为 "
                f"{plugin_data_root() / plugin_dir_name(plugin_file)}")
        return p, None
    norm = raw[2:] if raw.startswith("./") else raw
    root = plugin_data_root().parent.parent  # AstrBot 根目录
    if norm.startswith("data/"):
        target = root / norm
        try:
            legacy_root = (root / "data" / "workspaces").resolve()
            if str(target.resolve()).startswith(str(legacy_root)):
                return default_dir, target
        except Exception:
            pass
        return target, None
    # 其它相对路径：约束到插件数据目录下
    return default_dir / norm.strip("/"), None


def _merge_move(src: Path, dst: Path, moved: list, skipped: list) -> None:
    """P2-4: 递归合并迁移。

    原实现遇到目标已存在的同名项就整项跳过，导致「新目录空 avatars、
    旧目录满 avatars」这种半套数据。这里对目录做递归逐文件合并：
    目标不存在 → 整体 move；两者都是目录 → 递归；其余 → 跳过。
    """
    if not dst.exists():
        try:
            shutil.move(str(src), str(dst))
            moved.append(src.name)
        except Exception as e:
            skipped.append(src.name)
            logger.warning(f"[{PLUGIN_NAME}] 迁移 {src} 失败：{e}")
        return
    if src.is_dir() and dst.is_dir():
        try:
            children = sorted(src.iterdir())
        except Exception:
            skipped.append(src.name)
            return
        for child in children:
            _merge_move(child, dst / child.name, moved, skipped)
        # 递归后若源目录已空则删除
        try:
            if not any(src.iterdir()):
                src.rmdir()
        except Exception:
            pass
        return
    # 同名但类型不符或目标已存在 → 保留目标，跳过源
    skipped.append(src.name)


def migrate_dir(src, dst) -> str:
    """把旧数据目录内容搬到新目录（同名目录递归合并），返回日志文案。"""
    if src is None:
        return ""
    try:
        src = src.resolve()
        dst = dst.resolve()
    except Exception:
        return ""
    if src == dst or not src.is_dir():
        return ""
    try:
        items = sorted(src.iterdir())
    except Exception:
        return ""
    if not items:
        return ""
    moved: list = []
    skipped: list = []
    for it in items:
        _merge_move(it, dst / it.name, moved, skipped)
    try:
        if not any(src.iterdir()):
            src.rmdir()
    except Exception as e:
        logger.debug(f"[{PLUGIN_NAME}] 清理旧数据目录 {src} 失败（可忽略）：{e}")
    msg = (f"旧数据目录 {src} 已迁移到 {dst}"
           f"（移动 {len(moved)} 项，跳过 {len(skipped)} 项）")
    if skipped:
        msg += f"；跳过项：{', '.join(skipped[:5])}" \
               + ("…" if len(skipped) > 5 else "")
    logger.info(f"[{PLUGIN_NAME}] {msg}")
    return msg
