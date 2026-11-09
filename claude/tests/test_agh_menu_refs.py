#!/usr/bin/env python3
"""自建 DNS 屏上指路的菜单编号跟现在的菜单对得上（11/10 全仓检查查出来的）。
  11/01 加了「11 DoH 走 HTTP/3」，卸载挪到 12；10/31 重排后改端口是 7。屏上还有三处指着老编号：
    · 菜单 5 里「想把整套自建 DNS 卸掉：回上级菜单选 11」→ 照着点进去的是 HTTP/3 开关；
    · 「卸了重装（11 → 1）」同上；
    · 网页向导那一屏第 4 步「回菜单选 5 改」端口 → 5 是 DoT 带 ClientID。
  顺带：菜单 5 签泛域名时粘的 CF 令牌拼进命令行要用 shlex.quote（别的几处都是），不手写单引号。
"""
import os, io, re, sys, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("agh", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()

a._ask = lambda p="": "0"
a._installed = lambda: False
a._running = lambda: False
a._privdns_mode = lambda: ""
b = io.StringIO()
with contextlib.redirect_stdout(b):
    a.menu()
menu = b.getvalue()
num = {name: int(n) for n, name in re.findall(r"(?m)^\s+(\d+)\. (\S+)", menu)}
check("现在的菜单：改后台端口 7、DoH 走 HTTP/3 是 11、卸载是 12",
      num.get("改后台端口") == 7 and num.get("DoH") == 11 and num.get("卸载自建DNS") == 12)

a._public_ip = lambda: "1.2.3.4"
a._usage = lambda *x, **k: None
b = io.StringIO()
with contextlib.redirect_stdout(b):
    a._first_setup()
out = re.sub(r"\033\[[0-9;]*m", "", b.getvalue())
check("网页向导那一屏：两处都指「菜单 7」改端口，没有「选 5 改」", out.count("回菜单选 7 改") == 2 and "选 5 改" not in out)
check("菜单 5 里指卸载：回上级菜单选 12", "回上级菜单选 12" in src and "回上级菜单选 11" not in src)
check("证书出岔子那行：卸了重装是 12 → 1", "卸了重装（12 → 1）" in src and "11 → 1" not in src)
check("install() 里「已经装过了」那行照旧指 12 卸载、10 换域名", "先『12 卸载』再装" in src and "『10 更换域名证书』" in src)
check("粘的 CF 令牌进命令行一律 shlex.quote，不手写单引号",
      not re.search(r"(?<!SAVED_)CF_Token='\{", src) and src.count("CF_Token={shlex.quote(") >= 4)

import tempfile
home = tempfile.mkdtemp(); os.makedirs(f"{home}/.acme.sh")
conf = f"{home}/.acme.sh/account.conf"
os.environ["HOME"] = home
a._cf_token_steps = lambda dom: None
a._domain = lambda: "dns.example.com"
def replace(tok):
    open(conf, "w").write("SAVED_CF_Token='old'\nLOG_LEVEL=1\n")
    a._ask = lambda p="": tok
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a._replace_cf_token()
    return open(conf).read(), b.getvalue()
txt, out = replace("abc' ; touch /tmp/x ; '")
check("换令牌：带引号 / 空格的不写进 account.conf（acme.sh 把它当 shell 脚本读）", "SAVED_CF_Token='old'" in txt and "未改动" in out)
txt, out = replace("AbC_123-xyz")
check("换令牌：正常的照旧换、别的设置不动", "SAVED_CF_Token='AbC_123-xyz'" in txt and "LOG_LEVEL=1" in txt and "已更新" in out)

doc = open(os.path.join(ROOT, "docs", "13-dns.md"), encoding="utf-8").read()
check("文档：整体卸载指主菜单 20（不是早先的 17 / 19），换域名指菜单 10",
      "主菜单 `20 卸载`" in doc and "主菜单 `20 → 5`" in doc and "主菜单 `17`" not in doc and "主菜单 `19 卸载`" not in doc
      and "换域名 = 卸了重装" not in doc)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
