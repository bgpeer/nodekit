#!/usr/bin/env python3
"""自建 DNS 菜单 12「修改用户名和密码」（仓库主人 10/31：「可不可以用代码修改密码」→「加」→
  「用户名可以修改吗，要不用户名和密码一起修改」：1 修改后台用户名　当前：… / 2 修改后台密码 / 0 返回）。
  改用户名：换 users 段第一个 - name:，密码不动；10/31 真机实测新用户名 200、旧的 401。
  AdGuard 没有改密码的命令 / 接口：停服务 → 换掉 AdGuardHome.yaml 里 users 段那行 bcrypt 哈希（htpasswd -B 生成）→
  起服务 → 拿新密码登一次接口验证；登不进去就放回原配置。10/31 真 AdGuard Home v0.107.79 实测：新密码 200、旧密码 401。
"""
import os, io, sys, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

a.AGH_DIR = tempfile.mkdtemp()
Y = a.AGH_DIR + "/AdGuardHome.yaml"
OLD = "$2a$10$oldoldoldoldoldoldoldoldoldoldoldoldoldoldoldoldoldo"
YAML = ("http:\n  address: 0.0.0.0:2646\nusers:\n  - name: dns.example.com\n    password: " + OLD + "\n"
        "auth_attempts: 5\ntls:\n  enabled: false\n  password: keepme\n")
a._installed = lambda: True
SDB = a.AGH_DIR + "/data/sessions.db"; os.makedirs(a.AGH_DIR + "/data")
a._own_domain = lambda: "dns.example.com"
a._bcrypt = lambda pw: "$2y$10$NEWHASH" + str(len(pw))
a.time.sleep = lambda s: None
CMDS, LOGIN, ANS = [], [], []
a.sh = lambda c, *x, **k: CMDS.append(c) or ""
a._ask_fresh = lambda p="": ANS.pop(0) if ANS else ""
def go(answers, login_ok=True):
    open(Y, "w").write(YAML); open(SDB, "w").write("s"); CMDS.clear(); LOGIN.clear(); ANS[:] = answers
    a._agh_api = lambda path, body=None, port=None, timeout=5, auth=None: (LOGIN.append((port, auth)),
                                                                          (200 if login_ok else 401, ""))[1]
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.change_admin_password()
    return b.getvalue(), open(Y).read()

out, y = go(["short", "dns.example.com", "NewPassw0rd!"])
check("太短 / 跟用户名域名一样：不收、再问", "至少 8 位" in out and "别跟用户名 / 域名一样" in out)
check("只换 users 段那一行哈希，别处（tls 里的 password）不动",
      "    password: $2y$10$NEWHASH12\n" in y and OLD not in y and "  password: keepme" in y)
check("停服务 → 写 → 起服务", CMDS == ["systemctl stop AdGuardHome", "systemctl start AdGuardHome"])
check("拿新密码登一次接口验证（当前后台端口）", LOGIN and LOGIN[0] == (2646, ("dns.example.com", "NewPassw0rd!")))
check("✔ 一行、密码不上屏", "✔ 后台密码改好了，网页已登出" in out and "NewPassw0rd!" not in out)
check("改完密码：删掉 sessions.db，网页上已登录的被踢出（10/31 实测删前旧 cookie 200、删后 401）", not os.path.exists(SDB))
out, y = go(["NewPassw0rd!"], login_ok=False)
check("新密码登不进去：放回原配置（旧密码照旧能用）、说一行", y == YAML and "已改回原来的密码" in out
      and CMDS.count("systemctl start AdGuardHome") == 2)
out, y = go([""])
check("回车：取消、什么都没动", "已取消" in out and y == YAML and not CMDS and os.path.exists(SDB))
print("改用户名")
def goname(answers, up=True):
    open(Y, "w").write(YAML); open(SDB, "w").write("s"); CMDS.clear(); ANS[:] = answers
    a._agh_api = lambda path, body=None, port=None, timeout=5, auth=None: (401 if up else 0, "")
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.change_admin_name()
    return b.getvalue(), open(Y).read()
out, y = goname(["bad name", "a:b", "myboss"])
check("带空格 / 冒号的不收、再问", out.count("别带空格") == 2)
check("只换 users 段那个 name，密码那行不动", "  - name: myboss\n    password: " + OLD in y and "dns.example.com" not in y)
check("停服务 → 写 → 起服务、✔ 一行", CMDS == ["systemctl stop AdGuardHome", "systemctl start AdGuardHome"]
      and "✔ 后台用户名改成 myboss，密码不变，网页已登出" in out)
check("改完用户名：也踢掉已登录的网页", not os.path.exists(SDB))
out, y = goname(["myboss"], up=False)
check("改完后台没起来：放回原配置、说一行", y == YAML and "已改回原来的用户名" in out)
out, y = goname([""])
check("回车：取消、什么都没动", "已取消" in out and y == YAML and not CMDS)
out, y = goname(["dns.example.com"])
check("跟现在一样：没改", "跟现在一样" in out and not CMDS)

print("菜单")
ANS2 = ["0"]
a._ask = lambda p="": ANS2.pop(0) if ANS2 else "0"
b = io.StringIO()
with contextlib.redirect_stdout(b):
    a.change_admin_account()
check("12 里：1 修改后台用户名　当前：用户名 / 2 修改后台密码 / 0 返回",
      "1. 修改后台用户名　当前：dns.example.com" in b.getvalue() and "2. 修改后台密码" in b.getvalue() and "0. 返回" in b.getvalue())

a._agh_users = lambda: []
out, y = go(["NewPassw0rd!"])
check("还没设账号：提示先选 1", "先选 1" in out and not CMDS)
src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("主菜单 8 叫「修改用户名和密码」（10/31 重排后）", '(8, "修改用户名和密码", None)' in src
      and 'elif c == "8": change_admin_account()' in src)
check("htpasswd 从标准输入喂密码（不放命令行）", '["htpasswd", "-niBC", "10", ""], input=pw' in src)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
