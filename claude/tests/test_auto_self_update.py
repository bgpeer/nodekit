#!/usr/bin/env python3
"""每天 03:20 先把脚本本体更新到最新 —— 仓库主人 10/21：「加，万一有的人不上服务器也不更新脚本一直停在那里」。
  有新版且能编译 → 换上、execv 重跑 cfg-auto；拉不到 / 版本没变新 / 坏文件 → 什么都不动；环境变量挡住二次更新。
"""
import os, sys, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
m.SELF_LOCAL = f"{tmp}/xy-installer.py"; open(m.SELF_LOCAL, "w").write("OLD")
m.SCRIPT_VERSION = "1.1.122"
saved, execd = [], []
m.install_shortcut = lambda c=None: (saved.append(c), open(m.SELF_LOCAL, "w").write(c))
m.os.execv = lambda exe, argv: execd.append(argv)
os.environ.pop("BGPEER_SELFUPD", None)
def run(text=None, err=None):
    saved.clear(); execd.clear(); os.environ.pop("BGPEER_SELFUPD", None); open(m.SELF_LOCAL, "w").write("OLD")
    def f(u):
        if err: raise err
        return text
    m.fetch_url = f
    m._auto_self_update()

run('SCRIPT_VERSION = "1.1.123"\nprint(1)\n')
check("有新版：换上并 execv 重跑 cfg-auto", saved and execd and execd[0][-1] == "cfg-auto" and os.environ.get("BGPEER_SELFUPD") == "1")
run('SCRIPT_VERSION = "1.1.122"\n')
check("版本一样：不动", not saved and not execd)
run('SCRIPT_VERSION = "1.1.99"\n')
check("镜像还是旧的（版本更低）：不降级", not saved and not execd)
run('SCRIPT_VERSION = "1.1.130"\ndef broken(:\n')
check("新版下了半截 / 语法坏：不换", not saved and not execd and open(m.SELF_LOCAL).read() == "OLD")
run(err=OSError("timeout"))
check("拉不到：不动", not saved and not execd)
saved.clear(); execd.clear(); os.environ["BGPEER_SELFUPD"] = "1"; m.fetch_url = lambda u: 'SCRIPT_VERSION = "9.9.9"\n'
m._auto_self_update()
check("重跑那一轮（环境变量在）：不再更新，不会来回跳", not saved and not execd)
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
i = src.index("def cfg_auto_run():"); body = src[i:i + 1200]
check("cfg_auto_run 一开头就先自动更新", body.index("_auto_self_update()") < body.index("setup_cfg_auto_cron()"))
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
