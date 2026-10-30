#!/usr/bin/env python3
"""net-optimize 4.5.25 —— 仓库主人 10/30：「bbr v3里面要不要做几个按钮 1 BBR v3更新 2 卸载BBR v3回归系统内核 0返回，
  不过卸载一定要输yes」。12 → 4 装好以后进来：屏上一行现状 + 三项。
  · 更新：只升装着的 xanmod 包（--only-upgrade），needrestart 歇着；有新内核才问重启（默认否），没新的说「已经是最新」。
  · 卸载：/boot 里没有系统内核就不卸；红字一行后果；输 yes 才卸（y 不算）；先答好 debconf 的「删正在用的内核」；
    卸完 update-grub、撤掉我们加的源；问重启（默认否）。
  · --yes（非交互）照旧，不弹菜单。
"""
import os, io, sys, types, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("n", os.path.join(ROOT, "net-optimize.py"))
n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

check(f"版本 4.5.25（{n.VERSION}）", n.VERSION == "4.5.25")
XK, SK = "6.18.55-x64v3-xanmod1", "6.8.0-88-generic"
BOOT = [XK, SK]
CMDS, ANS = [], []
DPKG = "linux-xanmod-x64v3 ii \nlinux-image-6.18.55-x64v3-xanmod1 ii \nlinux-headers-6.18.55-x64v3-xanmod1 ii \nnginx ii \nlinux-xanmod-old rc \n"
def fake_run(cmd, timeout=15, env_extra=None, text_input=None):
    CMDS.append((cmd, env_extra or {}, text_input))
    out = DPKG if cmd[:2] == ["dpkg-query", "-W"] else ""
    if "--only-upgrade" in cmd and UPG:
        BOOT.append(UPG[0])
    if "purge" in cmd:
        BOOT[:] = [k for k in BOOT if "xanmod" not in k]
    return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
n.run = fake_run
n.apt = lambda args, timeout=900: CMDS.append((["apt-get"] + args, {}, None))
n._boot_kernels = lambda: list(BOOT)
n._ask_tty = lambda p: (print(p) or ANS.pop(0)) if ANS else ""
n.check_dpkg_clean = lambda: None
n.have_cmd = lambda c: True
n._xanmod_other_sources = lambda: []
RM = []
n._rm = lambda p: RM.append(p)
n._uname = lambda: types.SimpleNamespace(release=XK, machine="x86_64")
n.read_text = lambda p, d="": ""
n.get_sysctl = lambda k: "bbr"
n.set_sysctl = lambda k, v: None
n._xanmod_fix_cc = lambda: None
UPG = []
def go(*answers):
    CMDS.clear(); RM.clear(); ANS[:] = list(answers)
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        n.cmd_xanmod()
    return b.getvalue()

print("一、菜单")
out = go("0")
check("在跑 XanMod：一行现状 + 1 更新 / 2 卸载 / 0 返回", "已经换好了" in out and "1. 更新 BBRv3 内核" in out
      and "2. 卸载 BBRv3，回到系统内核" in out and "0. 返回" in out)
check("选 0：什么都没跑", not [c for c in CMDS if "apt-get" in c[0][0] or c[0][0] == "apt-get"])
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ANS[:] = []; n.cmd_xanmod(assume_yes=True)
check("--yes（非交互）不弹菜单", "1. 更新" not in b.getvalue())

print("二、更新")
UPG[:] = []
out = go("1")
up = [c for c in CMDS if "--only-upgrade" in c[0]]
check("只升装着的 xanmod 元包（不碰 nginx 等别的包）", up and up[0][0][-1:] == ["linux-xanmod-x64v3"])
check("needrestart 歇着（不重启 sing-box / nginx）", up and up[0][1].get("NEEDRESTART_SUSPEND") == "1")
check("没有新内核：说已经是最新、不问重启", "已经是最新" in out and "现在重启" not in out)
UPG[:] = ["6.18.60-x64v3-xanmod1"]
out = go("1", "")
check("有新内核：✔ 一行 + 问重启（默认否，回车不重启）", "更新到 6.18.60-x64v3-xanmod1" in out and "现在重启？[y/N]" in out
      and not [c for c in CMDS if c[0][:2] == ["systemctl", "reboot"]])
BOOT[:] = [XK, SK]

print("三、卸载")
out = go("2", "y")
check("红字一行说清后果、要输 yes", "BBRv3 换回 BBRv1" in out and "输入 yes" in out)
check("输 y 不算：取消、什么都没卸", "已取消" in out and not [c for c in CMDS if "purge" in c[0]])
out = go("2", "yes", "")
pg = [c for c in CMDS if "purge" in c[0]]
check("输 yes：卸掉装着的 xanmod 包（rc 残留的、别的包不算）",
      pg and sorted(pg[0][0][-3:]) == sorted(["linux-xanmod-x64v3", "linux-image-6.18.55-x64v3-xanmod1",
                                              "linux-headers-6.18.55-x64v3-xanmod1"]) and "nginx" not in pg[0][0])
ds = [c for c in CMDS if c[0] == ["debconf-set-selections"]]
check("卸之前答好「删正在用的内核」（不然没人答默认中止）",
      ds and "removing-running-kernel boolean false" in ds[0][2] and CMDS.index(ds[0]) < CMDS.index(pg[0]))
check("卸完 update-grub、撤掉我们加的源", [c for c in CMDS if c[0] == ["update-grub"]] and n.XANMOD_LIST in RM)
check("✔ 一行 + 问重启（默认否）", f"重启后回到系统内核（{SK}）" in out and "现在重启？[y/N]" in out
      and not [c for c in CMDS if c[0][:2] == ["systemctl", "reboot"]])
BOOT[:] = [XK]
out = go("2", "yes")
check("/boot 里没有系统内核：不卸（卸了开不了机）", "没有系统内核可回" in out and not [c for c in CMDS if "purge" in c[0]])

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
