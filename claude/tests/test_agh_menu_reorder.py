#!/usr/bin/env python3
"""自建 DNS 菜单重排（仓库主人 10/31：「把这个重新排序」，「腾出 53 端口」并进自检、「手机实测」跟自检合在一起 →「做」）。
  新顺序：1 安装 / 2 使用方法 / 3 写入订阅 / 4 节点私有解析 / 5 DoT ClientID / 6 苹果描述文件 / 7 改后台端口 /
  8 修改用户名和密码 / 9 自检（含手机实测）/ 10 卸载 / 0 退出。
  两项功能都还在：自检查完，53 被 systemd-resolved 占着才问腾不腾；再问要不要手机实测；都默认否。
"""
import os, io, sys, re, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

CALLS = []
for f in ("install", "status", "_selfdns_toggle", "_privdns_menu", "dot_clientid", "ios_profile",
          "change_web_port", "change_admin_account", "selfcheck", "uninstall", "phone_probe", "free_port53"):
    setattr(a, f, (lambda n: lambda *x, **k: CALLS.append(n))(f))
a._installed = lambda: True
a._running = lambda: True
a._privdns_mode = lambda: "remote"
a.os.path.exists = (lambda e: lambda p: False if p == a.SELFDNS_FLAG else e(p))(a.os.path.exists)

def run_menu(keys):
    q = list(keys); CALLS.clear()
    a._ask = lambda p="": q.pop(0) if q else "0"
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.menu()
    return re.sub(r"\033\[[0-9;]*m", "", b.getvalue())

out = run_menu(["0"])
want = ["1. 安装", "2. 使用方法（设备怎么填）", "3. 自建 DNS 写入订阅　当前：未写入", "4. 节点使用私有DNS解析　当前：外机",
        "5. DoT 带 ClientID（签泛域名证书）", "6. 苹果手机描述文件（iOS 一键装 DNS）", "7. 改后台端口",
        "8. 修改用户名和密码", "9. 自检（含手机实测）", "10. 卸载（整套撤干净，不动节点）", "0. 退出"]
pos = [out.find(w) for w in want]
check("新顺序、每项一行", all(p >= 0 for p in pos) and pos == sorted(pos))
check("「腾出 53 端口」「手机实测」不再单独一项", "腾出 53" not in out and "11." not in out)
for k, f in [("1", "install"), ("2", "status"), ("3", "_selfdns_toggle"), ("4", "_privdns_menu"), ("5", "dot_clientid"),
             ("6", "ios_profile"), ("7", "change_web_port"), ("8", "change_admin_account"), ("10", "uninstall")]:
    run_menu([k, "0"]); check(f"{k} → {f}", CALLS == [f])

print("自检之后两问")
def more(busy, ans):
    q = list(ans); CALLS.clear()
    a._port_busy = lambda p: busy
    a._ask = lambda p="": q.pop(0) if q else ""
    with contextlib.redirect_stdout(io.StringIO()) as b:
        run = a._selfcheck_more()
    return b.getvalue()
o = more("systemd-resolve", [""])
check("53 被 systemd-resolved 占着：提示一行、交给腾 53（它自己问 y/N）", CALLS == ["free_port53"] and "53 被 systemd-resolved 占着" in o)
check("手机实测默认否", "phone_probe" not in CALLS)
more("AdGuardHome", ["y"])
check("53 是 AdGuard 自己的：不问腾；回 y 才做手机实测", CALLS == ["phone_probe"])
more("", ["n"])
check("53 空着 + 回 n：什么都不做", CALLS == [])
CALLS.clear(); q = ["9", "0"]
a._ask = lambda p="": q.pop(0) if q else "0"
a._port_busy = lambda p: ""
with contextlib.redirect_stdout(io.StringIO()):
    a.menu()
check("9 → 先自检再问", CALLS[:1] == ["selfcheck"])
src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("屏上不再指向旧编号（选 4 腾53 / 选 7 自检 / 选 9 手机实测 / 『2 卸载』）",
      not re.search(r"选 ?4 腾|选 ?7 自检|选 9 手机实测|『2 卸载』|『4 腾出", src))
xy = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("主脚本里指向自建 DNS 的编号跟着改（13 → 4 / 13 自建DNS → 5 / 本菜单 9 自检）",
      "13 → 11" not in xy and "自建DNS → 8" not in xy and "本菜单 7 自检" not in xy and "本菜单 9 自检" in xy)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
