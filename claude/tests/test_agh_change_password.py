#!/usr/bin/env python3
"""自建 DNS 菜单 12「改后台密码」（仓库主人 10/31：「可不可以用代码修改密码」→「加」）。
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
a._own_domain = lambda: "dns.example.com"
a._bcrypt = lambda pw: "$2y$10$NEWHASH" + str(len(pw))
a.time.sleep = lambda s: None
CMDS, LOGIN, ANS = [], [], []
a.sh = lambda c, *x, **k: CMDS.append(c) or ""
a._ask_fresh = lambda p="": ANS.pop(0) if ANS else ""
def go(answers, login_ok=True):
    open(Y, "w").write(YAML); CMDS.clear(); LOGIN.clear(); ANS[:] = answers
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
check("✔ 一行、密码不上屏", "✔ 后台密码改好了" in out and "NewPassw0rd!" not in out)
out, y = go(["NewPassw0rd!"], login_ok=False)
check("新密码登不进去：放回原配置（旧密码照旧能用）、说一行", y == YAML and "已改回原来的密码" in out
      and CMDS.count("systemctl start AdGuardHome") == 2)
out, y = go([""])
check("回车：取消、什么都没动", "已取消" in out and y == YAML and not CMDS)
a._agh_users = lambda: []
out, y = go(["NewPassw0rd!"])
check("还没设账号：提示先选 1", "先选 1" in out and not CMDS)
src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("菜单里有 12「改后台密码」，原来 1~11 编号不动", '(12, "改后台密码", None)' in src and '(11, "节点使用私有DNS解析"' in src
      and 'elif c == "12": change_admin_password()' in src)
check("htpasswd 从标准输入喂密码（不放命令行）", '["htpasswd", "-niBC", "10", ""], input=pw' in src)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
