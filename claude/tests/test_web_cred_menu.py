#!/usr/bin/env python3
"""「7 设置 → 11 浏览器弹框账号」改成跟另外两个账号一个样子（仓库主人 11/12：「这个要不也修改成这样」，
  附的样子：1 修改用户名 当前： / 2 修改密码 当前： / 0 返回）。

  原来：先印当前用户名密码 → 问「要修改吗」→ 连着问新用户名、新密码。
  1.5.403：1 用户名 / 2 密码 / 0 返回，各带当前值，改完留在这一屏；设置里那一行也显示用户名。
钉住：
  一、屏：标题、两项带当前值、0 返回、一行提示
  二、改用户名：密码不动；密码文件、.secrets、CREDENTIALS.txt、nginx reload 都跟上；格式不对不收
  三、改密码：用户名不动；输 r 随机；确认默认否；新密码顶格单独一行
  四、没启用（没加密码）时入口还在、说清怎么加；密码文件没生成出来什么都不改
  五、7 设置里第 11 项显示用户名；进去出来不再多停一次"""
import importlib.util, os, sys, io, re, stat, tempfile, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()

T = tempfile.mkdtemp(); D = T + "/ms"; os.makedirs(D)
open(f"{D}/docker-compose.yml", "w").write("services: {}\n")
SEC, CRED = f"{D}/.secrets", f"{D}/CREDENTIALS.txt"
open(SEC, "w").write("OPENLIST_PASS=keepme\nBA_USER=media\nBA_PASS=OldPopupPass\n")
open(CRED, "w").write("      用户名  media\n      密  码  OldPopupPass\n")
m.HTPASSWD_FILE = f"{T}/htpasswd"; open(m.HTPASSWD_FILE, "w").write("media:hash\n")
m.ms_install_dir = lambda: D
HT, RELOAD, HT_OK = [], [], [True]
m.write_htpasswd = lambda u, p: (HT.append((u, p)), HT_OK[0])[1]
m.nginx_reload = lambda: RELOAD.append(1)
ANS, YN, YES = [], [], [True]
m.ask = lambda prompt, default="": ANS.pop(0) if ANS else "0"
m.ask_yn = lambda q, default=True: (YN.append((q, default)), YES[0])[1]
def run(*answers, yes=True):
    ANS[:] = list(answers); YN.clear(); HT.clear(); RELOAD.clear(); YES[0] = yes
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):      # err() 印在 stderr
        m.set_web_credentials()
    return plain(buf.getvalue())
sec = lambda k: m.read_env(SEC, k)

print("一、屏")
out = run()
check("标题一行、1 用户名 / 2 密码 各带当前值、0 返回", "  浏览器弹框账号" in out and "  1. 用户名            当前：media" in out
      and "  2. 密码              当前：OldPopupPass" in out and "  0. 返回" in out)
check("一行提示说清这是哪道密码；不再先问「要修改吗」", out.count("提示：") == 1 and "首页入口" in out and "要修改吗" not in out and not YN)
check("回车 / 0 直接返回，什么都没动", not HT and not RELOAD)

print("二、改用户名")
out = run("1", "boss", "")
check("改成功：屏上一行 ✔，回到这一屏、当前值是新的", "✔ 用户名改成「boss」" in out and "  1. 用户名            当前：boss" in out)
check("密码不动；密码文件用（新用户名, 原密码）重写", HT == [("boss", "OldPopupPass")] and sec("BA_USER") == "boss" and sec("BA_PASS") == "OldPopupPass")
check("CREDENTIALS.txt 跟着换、nginx reload 一次", "用户名  boss" in open(CRED).read() and "密  码  OldPopupPass" in open(CRED).read() and RELOAD == [1])
check(".secrets 还是 0600、别的键没丢", stat.S_IMODE(os.stat(SEC).st_mode) == 0o600 and sec("OPENLIST_PASS") == "keepme")
check("改用户名不用再确认一次", not YN)
for badname in ("a b", "a:b", "x" * 65):
    out = run("1", badname, "")
    check(f"用户名「{badname[:8]}」不收（htpasswd 用冒号分隔），没改", "没改：" in out and not HT and sec("BA_USER") == "boss")
out = run("1", "", "")
check("回车 = 不改", "没改。" in out and not HT)
out = run("1", "boss", "")
check("跟现在一样 = 不改", "没改。" in out and not HT)

print("三、改密码")
out = run("2", "My New Pass!", "", yes=False)
check("确认默认否；答否就不改", YN == [("把弹框密码改掉？", False)] and not HT and sec("BA_PASS") == "OldPopupPass")
out = run("2", "MyNewPass!$", "")
check("改成功：新密码顶格单独一行；回到这一屏、当前值是新的", "✔ 密码已改" in out and "\nMyNewPass!$\n" in out and "  2. 密码              当前：MyNewPass!$" in out)
check("用户名不动；密码文件、.secrets、CREDENTIALS.txt、nginx reload 都跟上", HT == [("boss", "MyNewPass!$")] and sec("BA_USER") == "boss"
      and sec("BA_PASS") == "MyNewPass!$" and "密  码  MyNewPass!$" in open(CRED).read() and RELOAD == [1])
check("提醒浏览器记着旧的", "无痕窗口" in out)
out = run("2", "r", "")
check("输 r：随机 16 位", re.fullmatch(r"[A-Za-z0-9]{16}", sec("BA_PASS")) is not None and HT[0][1] == sec("BA_PASS"))
out = run("2", sec("BA_PASS"), "")
check("跟现在一样：不折腾", "跟现在一样" in out and not HT and not YN)
out = run("2", "", "")
check("回车 = 不改", "没改。" in out and not HT)
out = run("9")
check("乱按：无效选择，不退出", "无效选择" in out and "  0. 返回" in out.split("无效选择")[-1])

print("四、没启用 / 出错")
cur = sec("BA_PASS")
HT_OK[0] = False
out = run("2", "WontStick", "")
check("密码文件没生成出来：说没有改动，.secrets / nginx 都不动", "没有改动" in out and sec("BA_PASS") == cur and not RELOAD)
HT_OK[0] = True
os.remove(m.HTPASSWD_FILE)
out = run()
check("没加密码的机器：入口还在，一行说清怎么加，不让改", "首页入口没加密码" in out and "1 安装" in out and "1. 用户名" not in out and not HT)
m.ms_install_dir = lambda: T + "/nope"
out = run()
check("还没安装：说先装", "还没安装" in out)

print("五、7 设置里的第 11 项")
check("显示用户名（跟 Emby 账号、OpenList 账号一样），没启用显示「未启用」",
      "read_env(sec, 'BA_USER'" in SRC and 'print(f"  11. 浏览器弹框账号   当前：{ba_state}")' in SRC and "未启用" in SRC)
check("子菜单自己管停顿：出来不再多按一次回车", re.search(r'elif c == "11":\n\s+set_web_credentials\(\)\n\s+continue', SRC) is not None)
check("media-stack passwd 这个命令还在", re.search(r'arg in \("passwd", "password"\):\n\s+require_root\(\); set_web_credentials\(\)', SRC) is not None)

print("六、版本号")
check("media-stack 1.5.403 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 403))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
