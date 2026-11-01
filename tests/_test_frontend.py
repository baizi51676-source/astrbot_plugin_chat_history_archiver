"""JS 静态检查：括号配平 + 关键函数存在性 + 无脏引用。"""
import re
import sys
from pathlib import Path

APP = Path(r"D:\astrbot\astrbot_plugin_chat_history_archiver\pages\console\app.js")
html = Path(r"D:\astrbot\astrbot_plugin_chat_history_archiver\pages\console\index.html")
s = APP.read_text(encoding="utf-8")

fails = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" {extra}"))
    if not cond:
        fails.append(name)

print("[1] 括号配平（忽略字符串/注释的粗检）")
# 去掉行注释与字符串后再配平，降低误报
stripped = re.sub(r"//[^\n]*", "", s)
stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
# 去掉单/双引号字符串与模板字符串（粗略）
stripped = re.sub(r"'[^'\n]*'", "''", stripped)
stripped = re.sub(r'"[^"\n]*"', '""', stripped)
stripped = re.sub(r"`[^`]*`", "``", stripped)
for o, c in [("{", "}"), ("(", ")"), ("[", "]")]:
    no, nc = stripped.count(o), stripped.count(c)
    check(f"括号配平 {o}{c}", no == nc, f"{no} vs {nc}")

print("[2] 关键函数存在")
for fn in ("gotoLocated", "locateByKey", "onReplyClick", "highlightAt",
           "gotoHit", "loadMessages", "loadOlder", "bubble", "replyChip"):
    check(f"定义 {fn}", bool(re.search(rf"function\s+{fn}\b", s)))

print("[3] 无残留的旧用法")
check("不再用 parseInt(seq) 当索引",
      "highlightAt(parseInt(seq, 10))" not in s)
check("highlightAt 不再接收裸行号 index 语义",
      "highlightAt(loc.index)" not in s, "仍传 loc.index")
check("gotoHit 走 locateByKey", "locateByKey(seq, date)" in s)
check("onReplyClick 走 gotoLocated", "gotoLocated(loc)" in s)

print("[4] 后端契约字段被前端消费")
check("前端使用 offset_from_end", "offset_from_end" in s)
check("前端不再读 data.messages",
      re.search(r"data\.messages", s) is None, "仍读 data.messages")

print("[5] reply 链路")
check("replyChip 读 m.reply", "m.reply" in s)
check("点击绑定 reply-chip", ".reply-chip" in s and "closest" in s)
check("调用 onReplyClick", "onReplyClick(chip.dataset.reply)" in s)

print("[6] index.html 未引用已删函数")
ih = html.read_text(encoding="utf-8")
check("html 里没有内联调用旧 highlightAt", "highlightAt(" not in ih)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
