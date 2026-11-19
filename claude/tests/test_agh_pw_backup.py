#!/usr/bin/env python3
"""自建 DNS · 后台密码映射一份存本机（仓库主人 11/19：「VPS 设置的应该是可以映射一份的，emby 就是
  在 VPS 设置的密码可以映射一份作为备份显示出来」——跟 Emby 的 emby_pw_*（.secrets）一个做法）：
  脚本经手设的密码（装向导、菜单 8 → 2、菜单 8 → 3、换 ClientID 时验过的）记在 BGP_DIR/agh.password
  （0600）；菜单 2 显示前拿它登录验一次，对 → 显示，不对（网页上改过）→ 作废，问不到 → 原样返回。
  测试里只用假密码，不出现任何真实用户信息。
"""
import os, io, sys, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

TMP = tempfile.mkdtemp()
a.BGP_DIR = TMP
a.AGH_PW_FILE = TMP + "/agh.password"
# CI 跑在普通用户下，remember_admin_password 非 root 直接返回（跟 test_agh_doh_h3 一个做法）。
a.os.geteuid = lambda: 0
PW = "Pw12345678"

check("没存过：取出来是空", a._agh_pw_get() == "")
a._agh_pw_save(PW)
check("存了：文件里就是那份密码", open(a.AGH_PW_FILE).read() == PW)
check("文件是 0600，只有 root 能读", oct(os.stat(a.AGH_PW_FILE).st_mode & 0o777) == "0o600")
check("取回来一样", a._agh_pw_get() == PW)
a._agh_pw_save("")
check("给空 = 作废：文件删了、取出来是空", not os.path.exists(a.AGH_PW_FILE) and a._agh_pw_get() == "")

a._agh_pw_save(PW)
a._agh_api = lambda path, body=None, port=None, timeout=5, auth=None: (200, "OK")
check("显示前登录验一次：对 → 返回存的那份", a._agh_pw_checked("dns.example.com") == PW)
a._agh_api = lambda path, body=None, port=None, timeout=5, auth=None: (401, "unauthorized")
check("网页上改过（401）：作废、返回空", a._agh_pw_checked("dns.example.com") == "" and not os.path.exists(a.AGH_PW_FILE))
a._agh_pw_save(PW)
a._agh_api = lambda path, body=None, port=None, timeout=5, auth=None: (0, "refused")
check("后台问不到：原样返回、文件留着", a._agh_pw_checked("dns.example.com") == PW and os.path.exists(a.AGH_PW_FILE))
a._agh_pw_save("")

a._installed = lambda: True
a._agh_users = lambda: ["dns.example.com"]
a._current_web_port = lambda: 4742
def go_remember(answers, code=200):
    a._ask_fresh = lambda p="": answers.pop(0) if answers else ""
    a._agh_api = lambda path, body=None, port=None, timeout=5, auth=None: (code, "")
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.remember_admin_password()
    return b.getvalue()

out = go_remember([PW])
check("菜单 8 → 3：密码对（200）→ 记下来了，密码不上屏", "记下来了" in out and PW not in out and a._agh_pw_get() == PW)
a._agh_pw_save("")
out = go_remember([PW], code=401)
check("菜单 8 → 3：密码不对（401）→ 没记", "没记" in out and a._agh_pw_get() == "")
out = go_remember([PW], code=0)
check("菜单 8 → 3：连不上后台 → 没记", "没记" in out and a._agh_pw_get() == "")
out = go_remember([""])
check("菜单 8 → 3：回车取消、没记", "已取消" in out and a._agh_pw_get() == "")

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
