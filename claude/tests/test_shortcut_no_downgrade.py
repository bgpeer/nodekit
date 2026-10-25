#!/usr/bin/env python3
"""install_shortcut 只升不降 —— 真机踩过（DMIT 那台）：
  install_shortcut() 不带内容时，把「当前正在跑的脚本」原样写进 /etc/bgpeer/xy-installer.py。
  节点安装/重装走的就是这条（不带内容）。于是有人用一份旧脚本跑重装，就把磁盘那份降级了；
  旧到连 self-update / SCRIPT_VERSION 都没有时，每天 02:50 的 `python3 SELF_LOCAL self-update`
  根本跑不起来，永远卡在旧版救不回来，而 bgpeer（每次临时下载最新再跑）一直显示新版把问题盖住。
  修法：不带内容时比较「正在跑的」和「磁盘已有的」版本，只留更新的那份，绝不降级；
  带内容（self-update / 菜单18 传下载好的新版）照常强制写。
"""
import os, sys, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
m.BGP_DIR = tmp
m.SELF_LOCAL = f"{tmp}/xy-installer.py"
RUN = f"{tmp}/running.py"            # 冒充「当前正在跑的脚本」= __file__
m.__file__ = RUN

def body(ver):                      # 造一份能编译的最小脚本；ver=None → 老到没有 SCRIPT_VERSION 这行
    return (f'SCRIPT_VERSION = "{ver}"\n' if ver else "") + "print(1)\n"
def setup(disk_ver, run_ver):
    open(m.SELF_LOCAL, "w").write(body(disk_ver) + "# disk\n")
    open(RUN, "w").write(body(run_ver) + "# running\n")
def disk_ver():
    return m._script_ver(open(m.SELF_LOCAL).read())

# 1. 用旧脚本重装：磁盘已有新版 → 不降级，留磁盘那份
setup("1.1.135", "1.1.100"); m.install_shortcut()
check("旧脚本重装：磁盘 1.1.135 不被 1.1.100 覆盖", disk_ver() == "1.1.135")

# 2. 更极端：正在跑的连 SCRIPT_VERSION 都没有 → 磁盘新版照样保住
setup("1.1.135", None); m.install_shortcut()
check("正在跑的没版本号：磁盘 1.1.135 保住", disk_ver() == "1.1.135")

# 3. 正常升级：正在跑的更新 → 写进磁盘
setup("1.1.100", "1.1.135"); m.install_shortcut()
check("正在跑的更新：磁盘升到 1.1.135", disk_ver() == "1.1.135")

# 4. 磁盘老到没版本号（已经降级卡死的机器）：正在跑的新版能把它救回
setup(None, "1.1.135"); m.install_shortcut()
check("磁盘没版本号：被正在跑的新版救回 1.1.135", disk_ver() == "1.1.135")

# 5. 带内容（self-update / 菜单18）：强制写，不受降级守卫影响
setup("1.1.135", "1.1.135"); m.install_shortcut('SCRIPT_VERSION = "1.1.140"\nprint(1)\n')
check("带内容：强制写成 1.1.140", disk_ver() == "1.1.140")

# 6. 守卫只改降级行为：bgpeer wrapper 仍是「临时下载最新 → 覆盖 → 跑」
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("wrapper 仍每次临时下载最新再跑",
      'mv "$t" /etc/bgpeer/xy-installer.py' in src and "exec python3 /etc/bgpeer/xy-installer.py" in src)

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
