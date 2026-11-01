"""打包脚本：生成 AstrBot 插件市场可直接安装的 zip。

打包工具优先级
-------------
1. **7-Zip（完整版）** —— 本机 `D:\\Program Files\\7-Zip\\7z.exe` 24.07 x64。
   直接用 `-xr` 排除规则打包，产物结构正确。
2. 7-Zip standalone `7za`（如 NVIDIA GeForce Experience 自带 16.02）——
   可用，但旧版 `-x` 排除语法不稳，故此时**退回暂存目录方案**。
3. Python `zipfile` —— 兜底，先建干净暂存目录再打包。

踩过的坑（务必保留）
-------------------
- `tar --format=zip`（bsdtar 3.5.2）会给所有条目加 `./` 前缀，
  解压后变成 `./main.py`，AstrBot 的 `_get_modules` 按目录名找 `main.py` 时失配。
- 用 `startswith(".git")` 判断"是否混入 .git 目录"是错的：
  会误伤正常的 `.gitignore` 文件。应判断 `.git/` 或以 `.git/` 开头的条目。

用法
----
    python tests/_package.py                 # 输出到 dist/
    python tests/_package.py --out D:/x      # 指定输出目录
    python tests/_package.py --stage-only    # 只建暂存目录（调试用）

产物：<输出目录>/astrbot_plugin_chat_history_archiver-<版本>.zip
包含：main.py / metadata.yaml / _conf_schema.json / core/ / pages/ / 图标等
排除：.git/ / tests/ / dist/ / __pycache__ / *.pyc
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
NAME = "astrbot_plugin_chat_history_archiver"

#: 暂存方案下的文件白名单（避免依赖排除语法）
INCLUDE_EXTS = {".py", ".json", ".yaml", ".md", ".jpg", ".png", ".txt"}
INCLUDE_DIRS = {"core", "pages"}

#: 打包时统一排除（7-Zip 直接打包路径使用）
EXCLUDE_PATTERNS = ("!.git", "!tests", "!dist", "!__pycache__", "!*.pyc",
                    "!_ziptest", "!_baseline")


def read_version() -> str:
    meta = PLUGIN_DIR / "metadata.yaml"
    m = re.search(r"^version:\s*([0-9.]+)", meta.read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("无法从 metadata.yaml 读取版本号")
    return m.group(1)


def find_7z() -> str | None:
    """查找完整版 7-Zip（优先），返回可执行文件路径。"""
    candidates = [
        r"D:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        # standalone 版（旧版排除语法不稳，仅作兜底，会被标记为 legacy）
        r"C:\Program Files (x86)\NVIDIA Corporation\NVIDIA GeForce Experience\7z.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for n in ("7z", "7za", "7zz"):
        p = shutil.which(n)
        if p:
            return p
    return None


def is_legacy_7z(exe: str) -> bool:
    """判断是否为 standalone 老版本（16.x），其 -x 排除语法不稳。"""
    return "NVIDIA" in exe or Path(exe).name.lower() in ("7za.exe", "7zr.exe")


def build_stage(stage: Path) -> None:
    """把要发布的文件复制到干净暂存目录（白名单方式，不依赖排除语法）。"""
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    for item in PLUGIN_DIR.iterdir():
        if item.is_file() and item.suffix.lower() in INCLUDE_EXTS:
            shutil.copy2(item, stage / item.name)
        elif item.is_dir() and item.name in INCLUDE_DIRS:
            shutil.copytree(item, stage / item.name)

    for pyc in stage.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)
    for pyc in stage.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)


def make_zip(stage: Path, out: Path) -> str:
    """打 zip。返回所用工具的说明字符串。"""
    exe = find_7z()
    if exe and not is_legacy_7z(exe):
        # 完整版：直接从插件目录打包 + 排除规则（产物最小、无需暂存）
        args = [exe, "a", "-tzip", str(out), "."]
        for pat in EXCLUDE_PATTERNS:
            args += ["-xr" + pat]
        subprocess.run(args, cwd=str(PLUGIN_DIR), check=True,
                       capture_output=True)
        return f"7-Zip ({Path(exe).parent.parent.name} {Path(exe).name})"

    # 老版 7z 或没有 7z：用暂存目录 + 无条件打包
    build_stage(stage)
    if exe:
        subprocess.run([exe, "a", "-tzip", str(out), "."], cwd=str(stage),
                       check=True, capture_output=True)
        return f"7-Zip standalone（暂存目录方案）"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(stage):
            for f in files:
                p = Path(root) / f
                z.write(p, p.relative_to(stage))
    return "python zipfile（暂存目录方案）"


def verify_zip(out: Path) -> list[str]:
    """产物自检：完整性、必需文件、无脏目录、无 ./ 前缀。"""
    problems = []
    with zipfile.ZipFile(out) as z:
        if z.testzip() is not None:
            problems.append("zip 完整性校验失败")
        names = z.namelist()

    for must in ("main.py", "metadata.yaml", "_conf_schema.json",
                 "core/__init__.py", "pages/console/app.js"):
        if must not in names:
            problems.append(f"缺少必需文件：{must}")

    def has_dir(prefix: str) -> bool:
        # 只匹配真正的目录条目或该目录下的条目，
        # 不能写成 startswith(".git")——会把 .gitignore 误判成 .git
        return any(n == prefix or n.startswith(prefix.rstrip("/") + "/")
                   for n in names)

    for bad in (".git/", "tests/", "dist/", "__pycache__/"):
        if has_dir(bad):
            problems.append(f"混入了不该发布的内容：{bad}")

    if any(n.startswith("./") for n in names):
        problems.append("条目名带 ./ 前缀（AstrBot 会找不到 main.py）")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(PLUGIN_DIR / "dist"))
    ap.add_argument("--stage-only", action="store_true")
    args = ap.parse_args()

    version = read_version()
    stage = Path(tempfile.mkdtemp()) / "stage"

    if args.stage_only:
        build_stage(stage)
        print(f"暂存目录: {stage}")
        print(f"文件数  : {sum(1 for p in stage.rglob('*') if p.is_file())}")
        return 0

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{NAME}-{version}.zip"
    if out.exists():
        out.unlink()

    tool = make_zip(stage, out)
    problems = verify_zip(out)
    shutil.rmtree(stage.parent, ignore_errors=True)

    with zipfile.ZipFile(out) as z:
        n_entries = len(z.namelist())
    print(f"版本    : {version}")
    print(f"工具    : {tool}")
    print(f"条目数  : {n_entries}")
    print(f"产物    : {out}")
    print(f"大小    : {out.stat().st_size / 1024:.0f} KB")
    if problems:
        print("自检    : 失败")
        for p in problems:
            print("          -", p)
        return 1
    print("自检    : 通过（结构完整、无 .git/tests/pycache、无 ./ 前缀）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
