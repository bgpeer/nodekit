#!/usr/bin/env python3
"""已经装好的机器点「1 安装」先确认一次；域名那一问不收不像域名的输入（仓库主人 11/13：「如果不小心点进去了输入n回车
  他会退回去吗」—— 原来不会：进来第一问就是域名，敲 n 回车，n 被当成域名往下装）。
钉住：
  一、已经装好：先一行提示 + 「确定要重新走一遍安装？」默认否；回车 / n → 回菜单，什么都不动
  二、没装过：不问这一句，直接装
  三、域名：没有点、带空格、单个字母 → 说一句再问；回车 = 默认；正常域名、*.开头的照收"""
import importlib.util, os, sys, io, re, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()

print("一、已经装好")
INST, YN, YES, DOCK = [True], [], [False], []
m.require_root = lambda: None
m.ms_install_dir = lambda: "/x"
m.is_installed = lambda d=None: INST[0]
m.ask_yn = lambda q, default=True: (YN.append((q, default)), YES[0])[1]
m.ensure_docker = lambda: (DOCK.append(1), (_ for _ in ()).throw(SystemExit("stop")))[1]
def run():
    YN.clear(); DOCK.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            m.main()
        except SystemExit:
            pass
    return plain(buf.getvalue())
out = run()
check("先一行提示说清会怎样，再问「确定要重新走一遍安装？」默认否", out.count("提示：") == 1 and YN == [("确定要重新走一遍安装？", False)])
check("答否（回车 / n）：回菜单，什么都不动（连 docker 都没碰）", "没有改动" in out and not DOCK)
YES[0] = True
run()
check("答 y：接着往下装", DOCK == [1])
INST[0] = False
run()
check("没装过：不问这一句，直接装", not YN and DOCK == [1])

print("二、域名那一问")
ANS = []
m.ask = lambda prompt, default="": (ANS.pop(0) if ANS else "") or default      # 跟真的一样：回车 = 默认值
def dom(*answers, default="ys.example.com"):
    ANS[:] = list(answers)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        v = m.ask_domain("域名", default)
    return v, plain(buf.getvalue())
v, out = dom("n", "")
check("敲 n：不当域名，说一句、再问；回车用默认", v == "ys.example.com" and "不像域名" in out and "Ctrl-C" in out)
for junk in ("y", "q", "abc", "ys example.com", "中文"):
    v, out = dom(junk, "")
    check(f"「{junk}」不收", v == "ys.example.com" and "不像域名" in out)
for good in ("ys.example.com", "emby.ys.example.com", "*.ys.example.com", "my-site.example.com"):
    v, out = dom(good)
    check(f"「{good}」照收", v == good and "不像域名" not in out)
v, out = dom("", default="")
check("没装节点那一问：留空 = 用 IP:端口，照收空", v == "")
check("安装里两处问域名都换成 ask_domain", SRC.count('cfg["domain"] = ask_domain(') == 2 and 'cfg["domain"] = ask("' not in SRC)
check("media-stack 1.5.429 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 429))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
