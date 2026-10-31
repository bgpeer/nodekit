#!/usr/bin/env python3
"""自建 DNS 用自己的域名、自己的证书，跟节点分开（仓库主人 10/29：「建议分开，因为节点老是出问题我想把他单独用域名，
  这样各个域名分开互不干预，好操作」「怎么还要加一个选项12呢分不开吗？都分开的大不了重做吗」）。
  DMIT 重装节点时因为「上次装的是泛域名」（其实是给自建 DNS 背的 *.节点域名）被迫走 DNS-01，令牌管不到新域名就装不上。
  · adguard-dns 安装时问自建 DNS 的域名 → 查两条 A 记录 → 单独签 域名 + *.域名 → /etc/ssl/agh/，节点证书一个字节不动；
    换域名 = 卸了重装，不加菜单项；卸载撤掉自己那张，不去重签节点证书
  · 主脚本：节点证书只在真有人靠 *.节点域名 时才要泛域名（老装法的自建 DNS、跟节点共用证书的 Emby）；
    订阅里的自建 DoH、私有解析的 DNS 地址都用自建 DNS 自己的域名
"""
import os, io, sys, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

tmp = tempfile.mkdtemp()
os.environ["HOME"] = tmp
a = load("agh", "adguard-dns.py")
a.BGP_DIR = tmp
a.AGH_DOMAIN_FILE = f"{tmp}/agh.domain"
a.AGH_CERT_DIR = f"{tmp}/agh"
a.AGH_CRT, a.AGH_KEY = f"{tmp}/agh/agh.crt", f"{tmp}/agh/agh.key"
a.ACME_CRT, a.ACME_KEY = f"{tmp}/sb/acme.crt", f"{tmp}/sb/acme.key"
a.HOST_FILE = f"{tmp}/sub.host"
open(a.HOST_FILE, "w").write("us.node.example\n")
os.makedirs(f"{tmp}/sb"); open(a.ACME_CRT, "w").write("NODE"); open(a.ACME_KEY, "w").write("NODEKEY")
os.makedirs(f"{tmp}/.acme.sh"); open(f"{tmp}/.acme.sh/acme.sh", "w").write("")
CONF = f"{tmp}/.acme.sh/account.conf"
ip = lambda *o: ".".join(map(str, o))
IP = ip(203, 0, 113, 5)
a._public_ip = lambda: IP
RES = {}
a._resolve4 = lambda n: RES.get(n, [])
a._wildcard_dns_check = lambda d: (True, "")
ASK = []
a._ask = lambda p="": ASK.pop(0) if ASK else ""
SH, RUN = [], []
def fake_sh(cmd, check=False):
    SH.append(cmd)
    if "--install-cert" in cmd:
        open(a.AGH_CRT, "w").write("AGH"); open(a.AGH_KEY, "w").write("AGHKEY")
    return ""
a.sh = fake_sh
OUT = ["Cert success."]
class R:
    def __init__(self, rc, out): self.returncode, self.stdout, self.stderr = rc, out, ""
a.subprocess.run = lambda cmd, **k: (RUN.append(cmd), R(0 if "Cert success" in OUT[0] else 1, OUT[0]))[1]
DOM = "dns.other.example"

CFD = "https://" + "dash.cloudflare" + ".com"           # 拼出来：测试文件里不留网址
print("一、装的时候给自建 DNS 自己的域名、自己的证书")
open(CONF, "w").write("SAVED_CF_Token='tok-old'\n")
RES[DOM] = [IP]
ASK[:] = [DOM]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = a._setup_own_domain()
check("先一行提示：去 Cloudflare 建 dns.根域名 和 *.dns.根域名（名字可换）",
      "提示：请在 " + CFD + " 做好两条 DNS 记录关黄云 dns.node.example 和 *.dns.node.example（名称不想用 dns 改用其他也行）"
      in a.re.sub(r"\033\[[0-9;]*m", "", buf.getvalue()))
check(f"签好了、记下域名（{got}）", got == DOM and open(a.AGH_DOMAIN_FILE).read().strip() == DOM)
check(f"签的是 域名 + *.域名、DNS 验证、用 acme.sh 存着的令牌（{RUN[-1][:80]}…）",
      f"--dns dns_cf -d {DOM} -d '*.{DOM}'" in RUN[-1] and "CF_Token=" not in RUN[-1])
check("装到自己的路径、续期只重启 AdGuard", any(f"--fullchain-file {a.AGH_CRT}" in c and "try-restart AdGuardHome" in c
                                         and "sing-box" not in c for c in SH))
check("节点证书一个字节没动", open(a.ACME_CRT).read() == "NODE" and not any(a.ACME_CRT in c for c in SH + RUN))
check(f"私钥目录只给 root（{oct(os.stat(a.AGH_CERT_DIR).st_mode & 0o777)}）", os.stat(a.AGH_CERT_DIR).st_mode & 0o777 == 0o700)
check("之后 _domain / 证书路径都是自建 DNS 自己的", a._domain() == DOM and a._crt() == a.AGH_CRT and a._key() == a.AGH_KEY)

print("二、签不下来的几种")
os.remove(a.AGH_DOMAIN_FILE)
check("没有自己的域名：退回老装法（跟节点共用）", a._domain() == "us.node.example" and a._crt() == a.ACME_CRT)
RES[DOM] = []; ASK[:] = [DOM]; RUN.clear()
with contextlib.redirect_stdout(io.StringIO()):
    got = a._setup_own_domain()
check("A 记录没指向本机：不签、不写", got == "" and not RUN and not os.path.exists(a.AGH_DOMAIN_FILE))
RES[DOM] = [IP]; ASK[:] = [DOM]; OUT[0] = "invalid domain\nError adding TXT record"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = a._setup_own_domain()
check("令牌管不到这个区域：说清楚去 CF 编辑令牌加区域、别新建顶掉", got == "" and "编辑它" in buf.getvalue() and "别新建" in buf.getvalue())
OUT[0] = "Cert success."
open(CONF, "w").write("")
ASK[:] = [DOM, "tok new"]; RUN.clear()
with contextlib.redirect_stdout(io.StringIO()):
    got = a._setup_own_domain()
check(f"acme.sh 里没存令牌：问一个，命令行里转义好（{RUN[-1][:40]}）", got == DOM and RUN[-1].startswith("CF_Token='tok new' "))
ASK[:] = ["n"]
with contextlib.redirect_stdout(io.StringIO()):
    got = a._setup_own_domain()
check("输 n：不用加密", got == "")

print("二b、像 Emby 那样先认节点域名")
a._node_domain = lambda: "us.node.example"
for f in (a.AGH_DOMAIN_FILE,):
    if os.path.exists(f): os.remove(f)
RES["dns.node.example"] = [IP]; ASK[:] = ["", "tok"]; RUN.clear()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = a._setup_own_domain()
check(f"回车 = dns.节点根域名（{got}），屏上列出节点域名", got == "dns.node.example" and "us.node.example" in buf.getvalue())
ASK[:] = ["us.node.example"]; RUN.clear()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = a._setup_own_domain()
check("填节点域名本身：不签（会把节点那条 acme 记录顶掉）", got == "" and not RUN and "换一个" in buf.getvalue())
os.makedirs(f"{tmp}/.acme.sh/media.example_ecc")
ASK[:] = ["media.example"]; RUN.clear()
with contextlib.redirect_stdout(io.StringIO()):
    got = a._setup_own_domain()
check("acme.sh 里已有同名记录（别的服务的）：不签", got == "" and not RUN)
a._node_domain = lambda: ""
open(a.AGH_DOMAIN_FILE, "w").write(DOM + "\n")

print("三、卸载：撤掉自己那张，不重签节点证书")
open(a.AGH_DOMAIN_FILE, "w").write(DOM + "\n")
REV = []
a._revert_wildcard = lambda *x, **k: REV.append(1)
a._installed = lambda: True
a._selfdns_remove = a._privdns_remove = a._restore_resolved = lambda: None
a._allowed_clients = lambda: None
a._selfdns_clientid = lambda: ""
a._cert_wildcard_ok = lambda d: True
a.AGH_BIN = f"{tmp}/nope"; a.AGH_DIR = f"{tmp}/nope-dir"
ASK[:] = ["y"]; SH.clear()
with contextlib.redirect_stdout(io.StringIO()):
    a.uninstall()
check("没去重签节点证书（以前撤泛域名要动节点证书、还要重启核心）", not REV and open(a.ACME_CRT).read() == "NODE")
check("acme.sh 不再续期它、证书目录和域名记录删掉",
      any(f"--remove -d {DOM} --ecc" in c for c in SH) and not os.path.exists(a.AGH_CERT_DIR)
      and not os.path.exists(a.AGH_DOMAIN_FILE))

print("四、主脚本")
x = load("xy", "xy-installer.py")
x.AGH_DOMAIN_FILE = f"{tmp}/agh.domain"
x.AGH_BIN_PATH = f"{tmp}/AdGuardHome"
x.SELFDNS_FLAG = f"{tmp}/selfdns.on"; open(x.SELFDNS_FLAG, "w").write("")
x._host = lambda: "us.node.example"
x.selfdns_clientid = lambda: "xyabc"
open(x.AGH_DOMAIN_FILE, "w").write(DOM + "\n")
check(f"订阅里的自建 DoH 用自建 DNS 自己的域名（{x._selfdns_doh()}）", x._selfdns_doh().startswith("https" + "://" + DOM + ":"))
check("私有解析认得出本机的 DoH（自建 DNS 的域名）", x._privdns_is_own("https" + "://" + DOM + ":10443/dns-query/xyabc"))
x.cert_meta = lambda: {"wildcard": True}
x.cert_names = lambda: ["us.node.example", "*.us.node.example"]
x._emby_cert = lambda: ("", None)
x.G["domain"] = "us.node.example"
check(f"自建 DNS 分开了：节点证书不再要泛域名（以前「上次装的就是泛域名」照要）（{x._wildcard_wanted()}）", x._wildcard_wanted() == (False, ""))
os.remove(x.AGH_DOMAIN_FILE)
check("没装自建 DNS：也不要", x._wildcard_wanted()[0] is False)
open(x.AGH_BIN_PATH, "w").write("")
x._agh_tls = lambda: {"enabled": "true", "server_name": "us.node.example"}
check("老装法：自建 DNS 跟节点共用证书 → 照旧要泛域名", x._wildcard_wanted()[0] is True)
x._agh_tls = lambda: {"enabled": "false", "server_name": ""}
check("装了但没开加密：不要", x._wildcard_wanted()[0] is False)
x._emby_cert = lambda: ("media.example", None); x._emby_shared = lambda d: True
check("Emby 跟节点共用证书：照旧要", x._wildcard_wanted()[0] is True)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
