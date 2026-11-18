#!/usr/bin/env python3
"""_auto_setup 轮询失败不再一声不吭（仓库主人 11/19 实装：3000 端口被别的网页程序占了，
  AdGuard 绑不上、一起一停，15 秒等不到向导就默默退回网页向导，用户根本不知道哪步掉了）。
  按回包分三种，说清楚：3000 上是个网页（不是 AdGuard）/ 服务没在运行 / 15 秒没连上。
  install() 装之前先查 3000 是否被占（跟查 53 一个路子），被占先问再装。
"""
import os, io, sys, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
a.time.sleep = lambda s: None          # 轮询 15 次别真等 15 秒
def run(api, running):
    a._agh_api = api
    a._running = running
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = a._auto_setup()
    return r, b.getvalue()
# 3000 上是个网页（curl 回了 404 的 HTML，不是 AdGuard 的向导接口）
r, out = run(lambda path, body=None, port=None, timeout=5, auth=None: (404, "<!DOCTYPE html><html>404"),
             lambda: False)
check("3000 被别的程序占了：返回 False、屏上说清是 3000 被占、网页向导也进不去",
      r is False and "3000 端口被别的程序占了" in out and "改用网页向导也进不去" in out)
# 什么都没在听（连不上）、服务也没在跑
r, out = run(lambda path, body=None, port=None, timeout=5, auth=None: (0, "refused"),
             lambda: False)
check("服务没在运行：返回 False、屏上说清服务没跑、给 systemctl status 的方向",
      r is False and "服务没在运行" in out and "systemctl status AdGuardHome" in out)
# 连不上、但服务在跑（启动慢）：只说 15 秒没连上，不乱说是被占了
r, out = run(lambda path, body=None, port=None, timeout=5, auth=None: (0, "refused"),
             lambda: True)
check("服务在跑但向导没响应：返回 False、只说 15 秒没连上、不提被占",
      r is False and "15 秒都没连上安装向导" in out and "被别的程序占了" not in out)
src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
ins = src[src.index("def install():"):src.index("\ndef ", src.index("def install(") + 5)]
check("install()：装之前先查 3000（在签证书、跑安装脚本之前），被占先问、默认不装",
      "_port_busy(WEB_PORT)" in ins and ins.index("_port_busy(WEB_PORT)") < ins.index("_setup_own_domain()")
      and ins.index("_port_busy(WEB_PORT)") < ins.index("正在安装 AdGuard Home")
      and "还继续安装吗" in ins and "[y/N]" in ins)
check("install()：3000 被占的提示里写着占了它的人、写着向导要先占 3000",
      "端口被" in ins and "向导要先占" in ins)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
