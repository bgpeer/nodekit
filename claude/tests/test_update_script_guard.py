#!/usr/bin/env python3
"""菜单 18 → 1「立即更新」：拉到的先验过再换、只升不降（11/10 全仓检查查出来的）。
  02:50 自更新（_auto_self_update）、bgpeer 命令（BGPEER_WRAPPER）、模块脚本（ensure_remote_script）都是
  「先编译一遍、版本不比本机旧才换」，只有这条手动的还是「内容不一样就换」：
    · raw 一限流，fetch_url 退到 jsDelivr（@main 缓存最长 12 小时）/ 公共反代，拿到旧版 → 点一次更新退一个版本，
      屏上还写着「脚本已更新」；
    · 拿到半截文件 / 一页报错 → 换上去面板当场打不开，02:50 的 `python3 SELF_LOCAL self-update` 跑的也是它，自己救不回来。
"""
import os, io, sys, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
x.BGP_DIR = tmp
x.SELF_LOCAL = f"{tmp}/xy-installer.py"
x.SCRIPT_VERSION = "1.1.200"
def body(ver, extra="print(1)\n"):
    return (f'SCRIPT_VERSION = "{ver}"\n' if ver else "") + extra
INSTALLED, EXEC, REMOTE = [], [], [""]
x.install_shortcut = lambda content=None: INSTALLED.append(content)
x.os.execv = lambda exe, argv: EXEC.append(argv)        # 真 execv 会把测试进程换掉
x.fetch_url = lambda url: REMOTE[0]
def run(remote, disk="1.1.200"):
    INSTALLED.clear(); EXEC.clear(); REMOTE[0] = remote
    open(x.SELF_LOCAL, "w").write(body(disk))
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        x.update_script()
    return b.getvalue()

out = run(body("1.1.200"))
check("跟本机一样：说已是最新，不换", "已是最新版本" in out and not INSTALLED and not EXEC)
out = run(body("1.1.201"))
check("新版：换上、重新载入面板", INSTALLED == [body("1.1.201")] and EXEC and "v1.1.200 → v1.1.201" in out)
out = run(body("1.1.195"))
check("镜像缓存里的旧版：不换（以前点一次更新退一个版本）", not INSTALLED and not EXEC)
check("……屏上一行 ⚠ 说清拉到的是哪版、过几分钟再试", "⚠ 拉到的是 v1.1.195，比本机的 v1.1.200 旧" in out and "没有替换" in out)
out = run(body("1.1.201", "def broken(:\n"))
check("半截 / 语法坏的：不换、不重新载入（换上去面板打不开，02:50 自更新也跑不起来）",
      not INSTALLED and not EXEC and "校验没过" in out and "没有替换" in out)
out = run("<html><body>429 Too Many Requests</body></html>\n")
check("拉到一页报错（公共反代限流页）：不换", not INSTALLED and not EXEC)
out = run("print('some other script')\n")
check("能编译但没有版本号（不是本脚本）：不换", not INSTALLED and not EXEC)
out = run(body("1.1.203"), disk="1.1.205")
check("磁盘那份比正在跑的新（别的会话刚更新过）：按新的那个比，不降级", not INSTALLED and not EXEC)
out = run(body("1.1.200", "print(2)\n"))
check("同一个版本号、内容不同（本机那份被改过）：照换回仓库里那份", INSTALLED and EXEC)
def boom(url): raise OSError("timeout")
x.fetch_url = boom
b = io.StringIO()
with contextlib.redirect_stdout(b):
    x.update_script()
check("拉不到：照旧只报一行，不动", "更新脚本失败" in b.getvalue() and len(INSTALLED) == 1)
doc = open(os.path.join(ROOT, "docs", "18-update-script.md"), encoding="utf-8").read()
check("文档写明：语法不对 / 比本机旧都不换", "比本机还旧" in doc and "都不换" in doc)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
