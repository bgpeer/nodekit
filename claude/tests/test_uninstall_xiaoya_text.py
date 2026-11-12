#!/usr/bin/env python3
"""卸载确认那一行：没装过小雅的机器不再提小雅（仓库主人 11/12：「这个小雅我的删掉了为什么还出现这个」）。

  小雅那一栏 1.5.385 就整栏删了，可卸载的确认文字一直写着「在这里装的小雅连它一起」，没装过的人看了莫名其妙。
  1.5.413：只有这台机器上还留着旧版装的那份（状态里 xiaoya_ours）才提；真有的话照旧一起删、照旧说清。"""
import importlib.util, os, sys, io, re, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
m.require_root = lambda: None
m.ms_install_dir = lambda: "/nonexistent-ms"
m.is_installed = lambda d=None: True
m.ask = lambda prompt, default="": ""              # 回车 = 取消
STATE = {}
m.ms_state = lambda: dict(STATE)
def run():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        m.do_uninstall()
    return plain(buf.getvalue())
out = run()
check("没装过小雅：确认那一行不提小雅", "小雅" not in out and "将要删除：媒体栈的容器、nginx 站点配置、media-stack/emby 命令。" in out)
check("回车 = 取消，什么都没动", "已取消，什么都没动" in out)
STATE["xiaoya_ours"] = True
out = run()
check("还留着旧版装的那份小雅：照旧说清会连它一起删", "旧版在这里装的小雅连它一起" in out)
src = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
check("真有的话照旧一起删（功能没动）", 'if ms_state().get("xiaoya_ours"):\n        sh(f"docker rm -f {XIAOYA_NAME}"' in src)
check("media-stack 1.5.413 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 413))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
