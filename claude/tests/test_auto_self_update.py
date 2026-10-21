#!/usr/bin/env python3
"""脚本本体每天自动更新 —— 仓库主人 10/21：「万一有的人不上服务器也不更新脚本一直停在那里」；
  第二天又要求：单独做进菜单 18（1 立即更新 / 2 自动更新 开关），单独一条排队任务，不跟三大配置自动更新混在一起
  （关了配置自动更新，脚本照样更新）；时间往前挪、别撞别的凌晨任务 → 北京 02:50，排在 03:00 cn-block 之前。
  有新版且能编译 → 换上；拉不到 / 版本没变新 / 坏文件 → 什么都不动。
"""
import os, sys, io, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
m.SELF_LOCAL = f"{tmp}/xy-installer.py"; open(m.SELF_LOCAL, "w").write("OLD")
m.SCRIPT_VERSION = "1.1.122"
saved = []
m.install_shortcut = lambda c=None: (saved.append(c), open(m.SELF_LOCAL, "w").write(c))
def run(text=None, err=None):
    saved.clear(); open(m.SELF_LOCAL, "w").write("OLD")
    def f(u):
        if err: raise err
        return text
    m.fetch_url = f
    return m._auto_self_update()

# 1. 换不换
check("有新版：换上", run('SCRIPT_VERSION = "1.1.123"\nprint(1)\n') and saved)
check("版本一样：不动", not run('SCRIPT_VERSION = "1.1.122"\n') and not saved)
check("镜像还是旧的（版本更低）：不降级", not run('SCRIPT_VERSION = "1.1.99"\n') and not saved)
check("新版下了半截 / 语法坏：不换", not run('SCRIPT_VERSION = "1.1.130"\ndef broken(:\n') and open(m.SELF_LOCAL).read() == "OLD")
check("拉不到：不动", not run(err=OSError("timeout")) and not saved)

# 2. 单独一条定时任务，北京 02:50，进排队器；开关撤 / 装
m.SELFUPD_CRON = f"{tmp}/bgpeer-selfupdate"; m.SELFUPD_OFF = f"{tmp}/selfupdate.off"
check("默认开", m.selfupd_on())
m.setup_selfupd_cron(); txt = open(m.SELFUPD_CRON).read()
check("写了定时任务：self-update、进排队器 selfupdate、北京 02:50", "self-update" in txt and "bgpeer-jobq" in txt and "selfupdate" in txt
      and "北京时间 02:50" in txt and "cfg-auto" not in txt)
check("02:50 排在 03:00 cn-block、03:10 nginx 升级、03:20 三大配置之前", m.SELFUPD_AT < (3, 0) and m.SELFUPD_AT < m.CFG_AUTO_AT)
open(m.SELFUPD_OFF, "w").write("1"); m.setup_selfupd_cron()
check("关掉：定时任务撤掉", not m.selfupd_on() and not os.path.exists(m.SELFUPD_CRON))
os.remove(m.SELFUPD_OFF)

# 3. 菜单 18：1 立即更新 / 2 自动更新 当前：开（每天 02:50）/ 0 返回；2 来回切
it = iter(["2", "2", "0"]); m._ask = lambda q="": next(it)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.script_update_menu()
o = buf.getvalue()
check("菜单三项：立即更新 / 自动更新 当前值 / 返回", "1. 立即更新" in o and "2. 自动更新" in o and "开（每天 02:50）" in o and "0. 返回" in o)
check("2 切关再切开，各一行 ✔", "✔ 自动更新已关" in o and "✔ 自动更新已开" in o and m.selfupd_on() and os.path.exists(m.SELFUPD_CRON))

# 4. 跟三大配置自动更新分开
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
i = src.index("def cfg_auto_run():"); body = src[i:i + 1500]
check("cfg-auto 里不再自己更新脚本（只补装定时任务）", "_auto_self_update()" not in body and "setup_selfupd_cron()" in body)
check("主菜单 18 走新菜单", 'elif c == "18":  script_update_menu()' in src)
check("卸载时一起清掉", "SELFUPD_CRON, SELFUPD_LOG]" in src)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
