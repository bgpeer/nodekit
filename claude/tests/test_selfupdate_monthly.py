#!/usr/bin/env python3
"""net-optimize 脚本本体月度自更新 —— 仓库主人：「网络优化也加进自动更新时间表里面排队，
  跟 Nginx 一样每个月更新一次」。有人装好就不进菜单，脚本修的 bug / 加的兜底一直用不上。
  新增 --self-update：只换 /usr/local/sbin 的 .py 文件，不重跑优化、不碰服务（不影响播放）；
  挂月度 cron（每月 1 号北京 03:40，进排队器），开机 / 首次手动跑也补装。
"""
import os, re, sys, io, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("netopt", os.path.join(ROOT, "net-optimize.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

# 1. write_selfupd_cron 的内容：--self-update、月度 1 号、进排队器、不是跑整套优化
tmp = tempfile.mkdtemp()
m.SELFUPD_CRON = f"{tmp}/net-optimize-selfupdate"
m.write_selfupd_cron()
cron = open(m.SELFUPD_CRON).read()
check("cron 调的是 --self-update（不是跑整套优化）", "--self-update" in cron and "--boot" not in cron)
check("cron 是每月 1 号北京 03 点（date 核对 0103）", '= "0103" ]' in cron)
check("cron 进了排队器 bgpeer-jobq", "bgpeer-jobq" in cron and "netopt-selfupd" in cron)
check("cron 指向本机安装的 SCRIPT_PATH", m.SCRIPT_PATH in cron)

# 2. cmd_self_update：只调 auto_update(--self-update) + 补装 cron + 报一行，不跑优化
calls = {}
m.require_root = lambda: None
m.auto_update = lambda argv: calls.__setitem__("au", argv)
m.write_selfupd_cron = lambda: calls.__setitem__("cron", True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_self_update()
check("cmd_self_update 调 auto_update(['--self-update'])", calls.get("au") == ["--self-update"])
check("cmd_self_update 顺手补装月度 cron", calls.get("cron") is True)
check("cmd_self_update 报一行版本", "已是最新" in buf.getvalue())

# 3. 源码接线：CLI 参数、分发、开机/安装补装、卸载清理、不走 main_optimize
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
check("加了 --self-update 参数", '"--self-update"' in src)
check("分发到 cmd_self_update，排在 main_optimize 之前",
      src.index('cmd_self_update()') < src.index("main_optimize(sys.argv[1:])"))
check("install_boot_service 装月度自更新 cron",
      "write_selfupd_cron()" in src[src.index("def install_boot_service"):src.index("def write_nginx_cron")])
check("开机(cmd_boot_apply)补装", src.count("write_selfupd_cron()") >= 3)
check("卸载(--reset)清理 SELFUPD_CRON", "_rm(SELFUPD_CRON)" in src)

# 4. 版本号 ≥ 4.5.14
mv = tuple(int(x) for x in re.findall(r"\d+", re.search(r'VERSION = "([\d.]+)"', src).group(1)))
check("VERSION ≥ 4.5.14", mv >= (4, 5, 14))

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
