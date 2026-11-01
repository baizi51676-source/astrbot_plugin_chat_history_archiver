"""等价性验证：对比重构前基线（git 里的 v2.3.3 main.py）与当前 main.py 的
插件类方法集、签名、装饰器，以及模块级公共符号。

不导入 main.py（它需要 AstrBot 运行时），改用 AST 静态比对。
基线不再依赖本地 _baseline/ 目录，改为直接从 git 取 tag/commit，
因此该脚本在干净检出（CI）里也能跑。
"""
import ast
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
NEW = HERE / "main.py"
# 基线来源：优先 tag v2.3.3；取不到则退回该 tag 的固定 commit
BASELINE_REFS = ("v2.3.3", "5bedb0b9465ea3a0c4904a41a88adab76aa899dc")


def load_baseline() -> str:
    """从 git 读取重构前基线源码。"""
    for ref in BASELINE_REFS:
        try:
            out = subprocess.run(
                ["git", "-C", str(HERE), "show", f"{ref}:main.py"],
                capture_output=True, check=True,
            )
            text = out.stdout.decode("utf-8", errors="replace")
            if "NapcatHistoryExporter" in text:
                return text
        except Exception:
            continue
    raise SystemExit(
        "无法从 git 取到基线（尝试过 %s）。请在插件仓库内运行，"
        "或先 git fetch --tags。" % ", ".join(BASELINE_REFS))


def load(path):
    src = path.read_text(encoding="utf-8")
    return ast.parse(src), src


def deco_repr(d):
    if isinstance(d, ast.Name):
        return d.id
    if isinstance(d, ast.Attribute):
        return f"{deco_repr(d.value)}.{d.attr}"
    if isinstance(d, ast.Call):
        args = []
        for a in d.args:
            if isinstance(a, ast.Constant):
                args.append(repr(a.value))
        for k in d.keywords:
            args.append(f"{k.arg}={ast.unparse(k.value)}")
        return f"{deco_repr(d.func)}({','.join(args)})"
    return ast.dump(d)


def sig_of(fn: ast.AST):
    """函数签名（参数名 + 默认值 + 返回注解）。"""
    a = fn.args
    parts = []
    posonly = getattr(a, "posonlyargs", [])
    for arg in posonly:
        parts.append(arg.arg)
    for arg in a.args:
        parts.append(arg.arg)
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    for arg in a.kwonlyargs:
        parts.append(arg.arg)
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    dflt = len(a.defaults)
    offset = len(a.args) - dflt
    anns = []
    for i, arg in enumerate(a.args):
        if arg.annotation is not None:
            anns.append(f"{arg.arg}:{ast.unparse(arg.annotation)}")
    ret = f"->{ast.unparse(fn.returns)}" if fn.returns else ""
    return f"({', '.join(parts)}) | defaults={dflt} offset={offset} | ann={anns} {ret}"


def method_map(tree, class_name):
    cls = None
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == class_name:
            cls = n
            break
    if cls is None:
        return None, None
    out = {}
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = {
                "async": isinstance(n, ast.AsyncFunctionDef),
                "sig": sig_of(n),
                "deco": sorted(deco_repr(d) for d in n.decorator_list),
            }
    return out, cls


def module_symbols(tree):
    names = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names[n.name] = "func"
        elif isinstance(n, ast.ClassDef):
            names[n.name] = "class"
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names[t.id] = "var"
    return names


old_src = load_baseline()
old_tree = ast.parse(old_src)
new_tree, new_src = load(NEW)

old_methods, old_cls = method_map(old_tree, "NapcatHistoryExporter")
# v2.4.0 类已改名 ChatHistoryArchiver；旧名作为模块级别名保留
new_methods, new_cls = method_map(new_tree, "ChatHistoryArchiver")

fails = []
def check(name, cond, extra=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {extra}")
        fails.append(name)

print("=== 1. 插件类存在性 ===")
check("旧版有 NapcatHistoryExporter", old_methods is not None)
check("新版保留同名类", new_methods is not None)
check("基类仍是 Star", new_cls.bases and ast.unparse(new_cls.bases[0]) == "Star",
      ast.unparse(new_cls.bases[0]) if new_cls.bases else "none")

print("=== 2. 方法集等价（允许已知新增）===")
# v2.4.0 之后新增的方法（P1-1 定时总结、P2-6 任务清理等）。
# 注意：这里只允许「新增」，绝不允许「丢失」或「签名变化」。
KNOWN_ADDED = {
    "_load_sched_state",       # P1-1 调度状态读取
    "_save_sched_state",       # P1-1 调度状态落盘
    "_run_daily_summaries",    # P1-1 批量生成当日总结
    "_run_daily_briefing",     # P1-1 生成简报
    "_daily_sched_check",      # P1-1 每日到点判定
    "_sched_loop",             # P1-1 auto_export 关闭时的独立轮询
    "_apply_runtime_config",   # P1-2 配置热生效：重新解析运行态
    "_sync_background_tasks",  # P1-2 配置热生效：启停后台任务
    "_load_verify_state",      # P1-5 启动检查续跑状态读取
    "_save_verify_state",      # P1-5 启动检查续跑状态落盘
    "remove_archived_target",  # P2-10 移除归档目标（LLM 工具）
    "_remove_target",          # P2-10 移除归档目标（实现）
    "_existing_target_dirs",   # P2-10 判断 tid 属于哪种类型
    "_api_remove_target",      # P2-10 移除归档目标（控制台 API）
    "_dates_sync",             # P2-1 日期列表的同步实现（供 to_thread）
    "_summary_items_sync",     # P2-1 总结读取的同步实现（供 to_thread）
    "_load_row_cache",         # P2-1 行数索引持久化读取
    "_save_row_cache",         # P2-1 行数索引持久化落盘
    "_warm_row_index",         # P2-1 后台预热行数索引（全新安装首次打开总览）
    "_pick_private_targets",   # v2.4.0 私聊归档白名单：选择本轮巡检目标
}
missing = set(old_methods) - set(new_methods)
added = set(new_methods) - set(old_methods)
unexpected_added = added - KNOWN_ADDED
check("无方法丢失", not missing, missing)
check("无意外新增", not unexpected_added, unexpected_added)
check("已知新增齐全（P1-1）", KNOWN_ADDED <= added, KNOWN_ADDED - added)
print(f"  （已知新增 {len(KNOWN_ADDED)} 个，实际新增 {len(added)} 个）")

print("=== 3. 签名与装饰器逐项一致 ===")
diff_sig, diff_deco, diff_async = [], [], []
# 有意变更签名的方法（新增可选参数，向后兼容）。其余签名必须逐项一致。
KNOWN_SIG_CHANGED = {
    # P2-5: 新增 cache_key 可选参数，避免平台实例 id 缺失时缓存串号
    "_bot_qq",
}
for name in sorted(set(old_methods) & set(new_methods)):
    o, n = old_methods[name], new_methods[name]
    if o["sig"] != n["sig"] and name not in KNOWN_SIG_CHANGED:
        diff_sig.append((name, o["sig"], n["sig"]))
    if o["deco"] != n["deco"]:
        diff_deco.append((name, o["deco"], n["deco"]))
    if o["async"] != n["async"]:
        diff_async.append((name, o["async"], n["async"]))
check("所有签名一致（已知变更除外）", not diff_sig, diff_sig[:3])
check("已知签名变更齐全", KNOWN_SIG_CHANGED <= set(new_methods),
      KNOWN_SIG_CHANGED - set(new_methods))
check("所有装饰器一致", not diff_deco, diff_deco[:3])
check("async/同步属性一致", not diff_async, diff_async[:3])

print("=== 4. LLM 工具全部仍在 main.py ===")
old_tools = {k for k, v in old_methods.items()
             if any(d.startswith("filter.llm_tool") for d in v["deco"])}
new_tools = {k for k, v in new_methods.items()
             if any(d.startswith("filter.llm_tool") for d in v["deco"])}
# P2-10 新增 remove_archived_target；其余工具必须原样保留（不允许丢失/改名）
KNOWN_NEW_TOOLS = {"remove_archived_target"}
check("原有 LLM 工具无丢失", old_tools <= new_tools, sorted(old_tools - new_tools))
check("无意外新增工具", not (new_tools - old_tools - KNOWN_NEW_TOOLS),
      sorted(new_tools - old_tools - KNOWN_NEW_TOOLS))
check(f"LLM 工具数={len(old_tools) + len(KNOWN_NEW_TOOLS)}",
      len(new_tools) == len(old_tools) + len(KNOWN_NEW_TOOLS), sorted(new_tools))

print("=== 5. 模块级公共符号 ===")
old_syms = module_symbols(old_tree)
new_syms = module_symbols(new_tree)
for want in ("PLUGIN_NAME", "PLUGIN_VERSION", "NapcatHistoryExporter",
             "_plugin_data_root", "_plugin_dir_name", "_default_export_dir",
             "_legacy_export_dir_path", "resolve_export_dir", "migrate_dir"):
    check(f"保留 {want}", want in new_syms, sorted(new_syms))

print("=== 6. 行数变化 ===")
old_lines = len(old_src.splitlines())
new_lines = len(new_src.splitlines())
print(f"  旧 main.py: {old_lines} 行")
print(f"  新 main.py: {new_lines} 行")
# P3-1 拆分时 main.py 显著缩减；v2.4.0 后续又加了 P1-1/P1-2/P1-5/P2-10 等新功能
# 与辅助方法，行数会回升。这里只记录变化，不强制「必须缩减」。
delta = new_lines - old_lines
print(f"  main.py 行数变化: {old_lines} → {new_lines}（{delta:+d}）")

core_lines = 0
for f in ("paths.py", "text.py", "store.py", "backend.py", "__init__.py"):
    p = HERE / "core" / f
    if p.exists():
        n = len(p.read_text(encoding="utf-8").splitlines())
        core_lines += n
        print(f"  core/{f}: {n} 行")
print(f"  core/ 合计: {core_lines} 行")

print()
print("FAILS:", [f[0] if isinstance(f, tuple) else f for f in fails] if fails else "none")
sys.exit(1 if fails else 0)
