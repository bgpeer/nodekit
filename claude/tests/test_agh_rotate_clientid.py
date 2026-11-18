#!/usr/bin/env python3
"""自建 DNS · 菜单 5 → 3「更换 ClientID」：一键生成新 ID，白名单 + 订阅一起换。
  顺序：先把新 ID 加进白名单（旧的先留着）→ 写文件、刷新订阅 → 再把旧 ID 从白名单删掉；
  密码不对或白名单加新失败，文件一个字节都不动；订阅刷新失败整套退回。
  测试里只用 example.com / 127.0.0.1 / xy 开头的假 ID，不出现任何真实用户信息。
  主脚本菜单 13 → 3 → 2 也有换 ID，但只换文件 + 订阅、白名单要手动，这里把白名单也接上。
"""
import os, io, sys, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

TMP = tempfile.mkdtemp()
a.BGP_DIR = TMP
a.SELFDNS_CID_FILE = TMP + "/selfdns.clientid"
a.SELFDNS_FLAG = TMP + "/selfdns.on"
a.AGH_PW_FILE = TMP + "/agh.password"
open(TMP + "/xy-installer.py", "w").write('"selfdns-refresh"\n')
OLD, NEW = "xy0123456789ab", "xyaabbccddeeff"
a.secrets.token_hex = lambda n: "aabbccddeeff"
a._domain = lambda: "dns.example.com"
a._installed = lambda: True
a._cert_wildcard_ok = lambda dom: True
a._doh_port = lambda: 10443
a._agh_users = lambda: ["admin"]
a._privdns_mode = lambda: "off"
a._current_web_port = lambda: 4321
open(a.SELFDNS_FLAG, "w").write("1")

ACCESS = {"allowed_clients": [OLD], "disallowed_clients": [], "blocked_hosts": ["bad.example"]}
SETS = []
def fake_api(path, body=None, port=None, timeout=5, auth=None):
    if path == "/control/status":
        return (200, "OK") if auth == ("admin", "Passw0rd!x") else (401, "unauthorized")
    if path == "/control/access/list":
        return 200, json.dumps(ACCESS)
    if path == "/control/access/set":
        SETS.append(list(body["allowed_clients"]))
        ACCESS["allowed_clients"] = list(body["allowed_clients"])
        return 200, "OK"
    return 404, ""
a._agh_api = fake_api
REFRESH = []
class Res:
    def __init__(self, rc=0, out="✔ 订阅里的自建 DNS 地址已换成新域名\n"):
        self.returncode, self.stdout, self.stderr = rc, out, ""
a.subprocess.run = lambda *x, **k: (REFRESH.append(x[0] if x else k.get("args", "")), Res())[1]

def reset():
    open(a.SELFDNS_CID_FILE, "w").write(OLD)
    ACCESS["allowed_clients"] = [OLD]
    SETS.clear(); REFRESH.clear()
    a._cert_wildcard_ok = lambda dom: True

def run(ans):
    q = list(ans)
    a._ask = lambda p="": q.pop(0) if q else ""
    a._ask_fresh = lambda p="": q.pop(0) if q else ""
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = a._rotate_clientid()
    return r, b.getvalue()

def cid():
    return open(a.SELFDNS_CID_FILE).read().strip()

reset()
r, out = run(["y", "", "Passw0rd!x"])
check("全程走通：返回 True", r is True)
check("白名单先加新（旧的先留着），订阅刷新完再删旧", SETS == [[OLD, NEW], [NEW]])
check("文件换成新 ID", cid() == NEW)
check("订阅刷新调了主脚本 selfdns-refresh", len(REFRESH) == 1 and "selfdns-refresh" in REFRESH[0])
check("屏上给新 DoH 和新安卓主机名",
      (":10443/dns-query/" + NEW) in out and (NEW + "." + a._domain()) in out and "已更换" in out)
check("密码没印到屏上", "Passw0rd" not in out)
check("换 ClientID 时验过的密码顺手记一份（菜单 2 里能看）", open(TMP + "/agh.password").read() == "Passw0rd!x")

reset()
ACCESS["allowed_clients"] = []
r, out = run(["y", "", "Passw0rd!x"])
check("白名单没设：不调 access/set，只换文件 + 订阅", r is True and not SETS and cid() == NEW)

reset()
r, out = run(["y", "", "wrong-password"])
check("密码不对：返回 False，文件、白名单一个都没动",
      r is False and cid() == OLD and not SETS and not REFRESH and "密码不对" in out)

reset()
r, out = run(["n"])
check("确认时取消：什么都没改", r is False and cid() == OLD and not SETS and not REFRESH)

reset()
a.subprocess.run = lambda *x, **k: Res(rc=1, out="")
r, out = run(["y", "", "Passw0rd!x"])
check("订阅刷新失败：文件还原、白名单里的新 ID 撤掉、返回 False",
      r is False and cid() == OLD and SETS == [[OLD, NEW], [OLD]])
a.subprocess.run = lambda *x, **k: (REFRESH.append(x[0] if x else ""), Res())[1]

reset()
a._cert_wildcard_ok = lambda dom: False
r, out = run(["y", "", "Passw0rd!x"])
check("没签泛域名证书：照常换 ID + 白名单，只是不给安卓主机名、提示先签证书",
      r is True and cid() == NEW and "没签泛域名证书" in out and "安卓「专用DNS」改填" not in out)

reset()
os.remove(a.SELFDNS_FLAG)
r, out = run(["y", "", "Passw0rd!x"])
check("订阅没写（菜单 3 没开）：不刷新订阅，只换白名单 + 文件", r is True and not REFRESH and cid() == NEW)
open(a.SELFDNS_FLAG, "w").write("1")

src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
blk = src[src.index("证书已经覆盖"):src.index("def _rotate_clientid")]
check("菜单 5 子菜单有第 3 项「更换 ClientID」且走到 _rotate_clientid",
      "更换 ClientID" in blk and '_rotate_clientid()' in blk)
fn = src[src.index("def _rotate_clientid("):src.index("def _do_free53():")]
check("先验后台账号（/control/status）再写文件，密码不对文件不动",
      fn.index("/control/status") < fn.index("_write_clientid(new)"))
check("先加新 ID 再删旧 ID（中间不断）", fn.index("allowed + [new]") < fn.index("[x for x in allowed if x != old]"))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
