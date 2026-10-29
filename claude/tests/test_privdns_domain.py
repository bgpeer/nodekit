#!/usr/bin/env python3
"""节点私有解析 · 用域名装的机器（仓库主人 10/28）——「装节点不都是用的域名吗……如果不能解析那装这个玩意还有啥意义」
  「现在的这一个dns记录在cf也不删，然后节点用这个令牌的另外DNS来建，主要的是公网查不出节点的信息」，三点定了：
  节点专用域名 = 随机名.根域名、每台节点机各粘一次 Cloudflare 令牌、先在测试机上做通。
  · 节点地址、SNI、Host 换成「随机.根域名」，公网不建记录；共用证书多签一个 *.根域名（DNS 验证，现有名字一个不丢）
  · 所有重签共用证书的地方都带着 *.根域名：cert_want_names / ensure_acme / cert_fix_run / cert_install_apply，
    adguard-dns.py 菜单 8 和撤销（撤销时令牌还得留着续期）
  · 证书没盖住、DNS 没答对 → 本机节点照用公网域名、.links 不报私有名，照样能连
  · DNS 机拿成员机的公网域名解析出 IP 写进 AdGuard；成员机报的名字不在它自己根域名底下的不认
  · 公网查得到起好的名字（根域名上有泛解析）→ 不开
  · 三份配置里找不到成员机的公网域名；nginx 443 认节点专用域名
"""
import os, re, sys, io, json, types, ssl, socket, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fakedoh import FakeAGH
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

try:
    import yaml
except ImportError:
    yaml = None

x = load("xy", "xy-installer.py")
tmp = tempfile.mkdtemp()
quiet = lambda: contextlib.redirect_stdout(io.StringIO())
HOME = f"{tmp}/home"
os.makedirs(f"{HOME}/.acme.sh")
os.environ["HOME"] = HOME
open(f"{HOME}/.acme.sh/acme.sh", "w").write("#!/bin/sh\n")

ROOTD = "zone.example"                     # 根域名（Cloudflare 上那个区域）
DOM3, DOM1 = "dns3." + ROOTD, "jp1." + ROOTD   # 3 = DNS 机（装了 AdGuard），1 = 没建 DNS 的成员机，都是用域名装的
ip = lambda *o: ".".join(map(str, o))      # 文档专用网段（RFC 5737），运行时拼，claude/ 里不留 IP 字面量
IP3, IP1, CF = ip(192, 0, 2, 33), ip(203, 0, 113, 10), ip(104, 16, 1, 1)
AGH = FakeAGH(tmp, DOM3)
DOH3 = f"https://dns3.zone.example:{AGH.port}/dns-query/xyabc123"

PUBLIC = {DOM3: [IP3], DOM1: [IP1]}         # 公网 DNS 上有的记录；*.wild.example 是泛解析
ASKED = []                                  # 往公网 DNS 问过哪些名字
class _Sock:
    def __getattr__(self, k):
        return getattr(socket, k)
    def create_connection(self, addr, *a, **kw):
        return socket.create_connection(("127.0.0.1" if addr[0] == DOM3 else addr[0], addr[1]), *a, **kw)
    def getaddrinfo(self, host, *a, **kw):
        h = host.lower().rstrip(".")
        ASKED.append(h)
        ips = PUBLIC.get(h) or ([ip(198, 51, 100, 9)] if h.endswith(".wild.example") else None)
        if not ips:
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (i, 0)) for i in ips]
x.socket = _Sock()
x.ssl = types.SimpleNamespace(create_default_context=lambda: AGH.client_ctx, SSLError=ssl.SSLError)

for k in ("PRIVDNS_FILE", "PRIVDNS_MAP_FILE", "LINKS_FILE", "CFG_FILE", "SBOX_FILE", "SR_FILE", "SR_NODES_FILE",
          "HOST_FILE", "PEERS_FILE", "SELFDNS_FLAG", "SELFDNS_CID_FILE", "TOKENS_FILE", "HY2_BW_FILE", "AGH_YAML",
          "STATE_FILE", "CERT_META"):
    setattr(x, k, f"{tmp}/{k.lower()}")
for e, f in (("yaml", "CFG_FILE"), ("json", "SBOX_FILE"), ("conf", "SR_FILE")):
    x.FMT[e]["file"] = getattr(x, f)
x.XRAY_DIR, x.BGP_DIR = f"{tmp}/xray", tmp
CALLS = []
def fake_sh(cmd, check=True):
    CALLS.append(cmd)
    if "restart AdGuardHome" in cmd:
        AGH.reload(x.AGH_YAML)
    return ""
x.sh = fake_sh
x.selfdns_clientid = lambda: "xyabc123"
TPL = {"yaml": open(os.path.join(ROOT, "sub-template.yaml")).read(),
       "json": open(os.path.join(ROOT, "subbox-template.json")).read(),
       "conf": open(os.path.join(ROOT, "shadowrocket-template.conf")).read()}
x.fetch_tpl = lambda u: TPL[u]
x.sr_sub_mode = lambda: False
x._sr_nodes_link = lambda: None
CERT = [DOM3]                               # 共用证书现在盖哪些名字（测试里不跑 openssl）
x.cert_names = lambda path=None: list(CERT)
LOCAL, PEERS, PEER_TEXT = [], [], {}
x.read_saved_links = lambda: list(LOCAL)
x.load_peers = lambda: list(PEERS)
def fake_fetch(u, timeout=15):
    if u not in PEER_TEXT:
        raise OSError("不通")
    return PEER_TEXT[u]
x._fetch_text = fake_fetch
def state(dom):
    json.dump({"domain": dom, "host": dom, "nginx": "1"}, open(x.STATE_FILE, "w"))
HOST = [DOM3]
x._host = lambda: HOST[0]
open(x.AGH_YAML, "w").write(
    "http:\n  address: 0.0.0.0:3000\ntls:\n  enabled: true\n  server_name: dns3.zone.example\n"
    f"  port_https: {AGH.port}\nuser_rules:\n  - '||ads.example^'\ndhcp:\n  enabled: false\n")
x._agh_doh_url = lambda: DOH3 if HOST[0] == DOM3 else ""

def vm(add, ps, sni):
    return x.vmess_link({"v": "2", "ps": ps, "add": add, "port": "443", "id": "8c1f-uuid", "aid": "0", "net": "ws",
                         "type": "none", "host": sni, "path": "/vm", "tls": "tls", "sni": sni, "fp": "chrome"})
def node_links(dom, pfx):
    return [f"vless://8c1f-uuid@{dom}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.cisco.com&fp=chrome&pbk=PBK&sid=ab&type=tcp#{pfx}·reality-vision",
            f"hysteria2://pw1@{dom}:31000?sni={dom}&mport=31000-32000#{pfx}·hy2",
            f"tuic://8c1f-uuid:pw1@{dom}:32001?congestion_control=bbr&alpn=h3&sni={dom}#{pfx}·tuic",
            f"anytls://pw1@{dom}:33001?sni={dom}&fp=chrome#{pfx}·anytls",
            f"ss://{x.ss_userinfo('2022-blake3-aes-128-gcm', 'K1==')}@{dom}:34001#{pfx}·ss2022",
            f"trojan://pw1@{dom}:35001?security=tls&sni={dom}&fp=chrome#{pfx}·trojan",
            vm(dom, f"{pfx}·vmess-ws", dom),
            f"vless://8c1f-uuid@{dom}:443?encryption=none&security=tls&sni={dom}&fp=chrome&type=ws&host={dom}&path=%2Fvl#{pfx}·vless-ws",
            f"vless://8c1f-uuid@{CF}:2053?encryption=none&security=tls&sni=cdn.{ROOTD}&fp=chrome&type=ws&host=cdn.{ROOTD}&path=%2Fcdn#{pfx}·CDN·vless-ws"]
M1, L3 = node_links(DOM1, "🇯🇵1"), node_links(DOM3, "🇭🇰3")

# ── 1. 节点专用域名的名字、换地址 ────────────────────────────────────────────────────────
N1 = x._privdns_new_name(ROOTD)
check(f"起的名字是「10 位随机.6 位随机.根域名」（{N1}）",
      re.match(r"^[a-z0-9]{10}\.[a-z0-9]{6}\.zone\.example$", N1) and x._privdns_is_domain_name(N1)
      and not x._privdns_is_domain_name("k7xq2m4p9z.internal"))
ok_all = True
for u in M1[:-1]:
    v = x._link_rehost(u, DOM1, N1)
    d0, d1 = x.link_to_proxy(u), x.link_to_proxy(v)
    sni0 = d0.get("servername") or d0.get("sni")
    sni1 = d1.get("servername") or d1.get("sni")
    host1 = ((d1.get("ws-opts") or {}).get("headers") or {}).get("Host")
    want_sni = N1 if sni0 == DOM1 else sni0
    same = {k: w for k, w in d0.items() if k not in ("server", "servername", "sni", "ws-opts")} == \
           {k: w for k, w in d1.items() if k not in ("server", "servername", "sni", "ws-opts")}
    if not (d1["server"] == N1 and sni1 == want_sni and (host1 in (None, N1)) and same and DOM1 not in v.split("#")[0]):
        ok_all = False; print("    换坏了：", u[:50], "→", v[:80])
check("八种链接：地址、SNI、ws Host 原来是公网域名的全换成节点专用域名，reality 借来的 SNI、密码、端口、跳跃段不动", ok_all)
check("节点名（# 后面）不碰", x._link_rehost(M1[1] + "jp1.zone.example", DOM1, N1).endswith("jp1.zone.example"))

# ── 2. 能不能藏、IP 从哪来 ─────────────────────────────────────────────────────────────
check(f"用域名装的成员机：地址就是它的公网域名，IP 从公网解析拿（{x._privdns_addrs(M1, N1)}）",
      x._privdns_addrs(M1, N1) == (DOM1, [IP1]))
check("报的名字不在它自己的根域名底下（xxx.别人家）：不认，免得 AdGuard 帮着劫持别人的域名",
      x._privdns_addrs(M1, "k7xq2m4p9z.other.example") == ("", []))
check("名字就是公网域名本身：不认", x._privdns_addrs(M1, DOM1) == ("", []))
check("用 IP 装的报了节点专用域名、或者用域名装的报 .internal：都不认",
      x._privdns_addrs(M1, "k7xq2m4p9z.internal") == ("", []))
check("一台机器的节点地址不是同一个域名：不藏", x._privdns_addrs(M1 + [M1[1].replace(DOM1, DOM3)], N1) == ("", []))
PUBLIC.pop(DOM1)
check("公网域名解析不出来：这一轮不藏", x._privdns_addrs(M1, N1) == ("", []))
PUBLIC[DOM1] = [IP1]
ASKED.clear()
check("根域名上没有泛解析才用；有泛解析（*.wild.example）就不行",
      x._privdns_public_free(ROOTD) and not x._privdns_public_free("wild.example"))
check("查泛解析拿的是用完就扔的随机名，不是真要用的节点专用域名", N1 not in ASKED and len(set(ASKED)) == 2)

# ── 3. 证书：*.根域名 处处带着 ───────────────────────────────────────────────────────────
N3 = x._privdns_new_name(ROOTD)
P3 = N3.split(".", 1)[1]                    # 随机段.根域名
W3 = "*." + P3                              # 证书多签的就是它
state(DOM3)
x.privdns_save({"mode": "off", "dns": "", "name": N3})
check("关着：不多签", x._privdns_root() == "" and W3 not in x.cert_want_names(DOM3, False))
x.privdns_save({"mode": "self", "dns": "", "name": N3})
check(f"开着：cert_want_names 带上 *.{ROOTD}（{x.cert_want_names(DOM3, False)}）",
      x._privdns_root() == P3 and x.cert_want_names(DOM3, False)[-1] == W3)
state("node.another.example")
check("换过节点域名（根域名变了）：旧的 *.根域名 不再带", x._privdns_root() == "")
state(DOM3)
want = x.cert_want_names(DOM3, True)
check(f"节点域名是根域名的子域：要签的泛域名盖不住它（Let's Encrypt 不收「redundant with a wildcard」，10/29 真机）（{want}）",
      not [n for n in want if n.startswith("*.") and n != "*." + DOM3 and x._name_covers([n], DOM3)])
state(DOM3)
RUNS = []
class Stop(Exception):
    pass
def fake_run(cmd, *a, **kw):
    RUNS.append(cmd if isinstance(cmd, str) else " ".join(cmd))
    return types.SimpleNamespace(returncode=1, stdout="test stop", stderr="")
x.subprocess = types.SimpleNamespace(run=fake_run, PIPE=-1, DEVNULL=-3, Popen=None)
def acct(token):
    open(f"{HOME}/.acme.sh/account.conf", "w").write(("SAVED_CF_Token='tok'\n" if token else "ACCOUNT_EMAIL=''\n"))
acct(False)
RUNS.clear()
with quiet() as _:
    r = x.cert_install_apply(DOM3, False, "", [DOM3, W3])
check("要签 *.根域名、没令牌：说清楚、不碰现有证书（不去签）", r is False and not RUNS)
acct(True)
RUNS.clear()
with quiet():
    x.cert_install_apply(DOM3, False, "", [DOM3, W3])
check(f"有令牌：走 DNS 验证、两个名字都签（{RUNS[-1][:90] if RUNS else ''}）",
      RUNS and "--dns dns_cf" in RUNS[-1] and f"-d '{W3}'" in RUNS[-1] and f"-d '{DOM3}'" in RUNS[-1])
RUNS.clear()
with quiet():
    x.cert_install_apply(DOM3, True, "", ["*." + DOM3, DOM3, W3])     # 现有证书的顺序：*.dom 在前（10/29 真机）
check(f"节点域名排第一个 -d（acme.sh 的主域名，install-cert -d 它才导得出新证书）（{RUNS[-1][:90] if RUNS else ''}）",
      RUNS and re.search(r"--dns dns_cf --keylength ec-256 -d '([^']+)'", RUNS[-1]).group(1) == DOM3)
_run0 = x.subprocess.run
x.subprocess.run = lambda cmd, *a, **kw: (RUNS.append(cmd), types.SimpleNamespace(
    returncode=1, stdout="Adding TXT value: x for domain: _acme-challenge.q.zone.example\ninvalid domain", stderr=""))[1]
with quiet() as buf:
    r = x.cert_install_apply(DOM3, False, "", [DOM3, W3])
check("令牌管不到这个区域（invalid domain）：失败、一行提示去 CF 改令牌的区域资源",
      r is False and "区域资源加上这个域名" in buf.getvalue())
x.subprocess.run = _run0
x.cert_covers = lambda p, d: False
x.have = lambda b: True
x._wildcard_wanted = lambda: (False, "")
x.G["domain"], x.G["email"], x.G["nginx"] = DOM3, "", "1"
RUNS.clear()
try:
    with quiet():
        x.ensure_acme()
except Exception:
    pass
check(f"重装时 ensure_acme 重签：DNS 验证、带着 *.{ROOTD}（{RUNS[-1][:90] if RUNS else ''}）",
      RUNS and "--dns dns_cf" in RUNS[-1] and f"-d '{W3}'" in RUNS[-1] and "--webroot" not in RUNS[-1])
x._ensure_cron_running = lambda: None
x.acme_hijackers = lambda keep="": []
RUNS.clear()
with quiet():
    x.cert_fix_run()
check(f"「15 → 修复证书」重签：DNS 验证、带着 *.{ROOTD}（{RUNS[-1][:90] if RUNS else ''}）",
      RUNS and "--dns dns_cf" in RUNS[-1] and f"-d '{W3}'" in RUNS[-1] and "--standalone" not in RUNS[-1])
acct(False)
RUNS.clear()
with quiet() as buf:
    x.cert_fix_run()
check("修复证书时没令牌：不降级成单域名、不动（*.根域名 丢了节点专用域名就握手失败）", not RUNS)

acct(True)
x.privdns_save({"mode": "self", "dns": "", "name": N3})
check("要签的是别的根域名底下的域名：不带这个 *.根域名（令牌多半管不着那个区域，带上反而签不出来）",
      W3 not in x.cert_want_names("node.another.example", False))
x.G["domain"] = "node.another.example"
RUNS.clear()
try:
    with quiet():
        x.ensure_acme()
except Exception:
    pass
check("用别的根域名重装：ensure_acme 不带旧的 *.根域名（单域名照常签）", RUNS and W3 not in RUNS[-1])
x.G["domain"] = DOM3
os.makedirs(x.XRAY_DIR, exist_ok=True)
json.dump({"inbounds": [{"port": 23456, "streamSettings": {"security": "reality",
                                                           "realitySettings": {"serverNames": ["www.cisco.com"]}}}]},
          open(f"{x.XRAY_DIR}/config.json", "w"))
check("本机 xray 的 reality 换成节点专用域名以后，sing-box 那份照样认得出来（照旧跳过）",
      (N3, 23456, "www.cisco.com") in x._local_xray_reality())

# ── 4. nginx 443 认节点专用域名 ─────────────────────────────────────────────────────────
x.G["domain"] = DOM3
CERT[:] = [DOM3]
check("证书没盖住：443 只认公网域名", f"server_name {DOM3};" in x._nginx_https_server("  listen 443 ssl;\n"))
CERT[:] = [DOM3, W3]
check("证书盖住了：443 两个名字都认", f"server_name {DOM3} {N3};" in x._nginx_https_server("  listen 443 ssl;\n"))

# ── 5. DNS 机（3）：本机节点也换、替 1 解析 ──────────────────────────────────────────────
N1R = x._privdns_new_name(ROOTD)
LOCAL[:] = L3
PEERS[:] = ["https://m1.example/x.links"]
PEER_TEXT["https://m1.example/x.links"] = "\n".join(M1) + f"\n#privdns {N1R} {DOH3}\n"
check(".links 末尾报的是本机节点专用域名 + 本机 DNS 地址（证书盖得住才报）",
      x._privdns_links_line() == f"#privdns {N3} {DOH3}")
CALLS.clear()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    links = x.aggregated_links()
mp = x._privdns_map()
check(f"AdGuard 里两条：本机的 → 本机 IP，1 的 → 1 的 IP（从它的公网域名解析出来的）（{ {k: v['ips'] for k, v in mp.items()} }）",
      {k: v["ips"] for k, v in mp.items()} == {N3: [IP3], N1R: [IP1]}
      and AGH.ANS.get((N3, 1)) == (0, [IP3]) and AGH.ANS.get((N1R, 1)) == (0, [IP1]))
check("重启了一次 AdGuard，屏上没有 ⚠", CALLS.count("systemctl restart AdGuardHome") == 1 and "⚠" not in buf.getvalue())
srv = {x._link_name(u): x._link_addr(u) for u in links}
check("3 自己的节点：地址、SNI 换成 3 的节点专用域名（reality 借来的 SNI 不动），CDN 那条不动",
      all(a[0] == N3 and a[1] in (N3, "www.cisco.com", "") for k, a in srv.items() if "🇭🇰3" in k and "CDN" not in k)
      and [a[0] for k, a in srv.items() if "🇭🇰3" in k and "CDN" in k] == [CF])
check("1 的节点：地址、SNI 换成 1 的节点专用域名",
      all(a[0] == N1R and a[1] in (N1R, "www.cisco.com", "") for k, a in srv.items() if "🇯🇵1" in k and "CDN" not in k))
check("这一轮真换了的两个都记下", x._PRIVDNS_LIVE == {N3: DOH3, N1R: DOH3})
ylines, nodes = x.parse_nodes(links)
with quiet():
    x.gen_mihomo(ylines, nodes, "yaml"); x.gen_singbox(ylines, nodes, "json"); x.gen_shadow(ylines, nodes, "conf")
mh, sb, sr = (open(f).read() for f in (x.CFG_FILE, x.SBOX_FILE, x.SR_FILE))
check("三份配置里找不到 1 的公网域名（地址、SNI、Host 都换了），也找不到 1 的 IP",
      all(DOM1 not in t and IP1 not in t for t in (mh, sb, sr)))
if yaml:
    pol = yaml.safe_load(mh)["dns"].get("proxy-server-nameserver-policy") or {}
    check(f"mihomo：两个节点专用域名只去问 3（{sorted(pol)}）", pol == {N3: [DOH3], N1R: [DOH3]})
cj = json.loads(sb)
check("sing-box：两台的出站都指向 3 的 DoH",
      {o["server"] for o in cj["outbounds"] if isinstance(o, dict) and o.get("domain_resolver") == "privdns"} == {N3, N1R})
check("小火箭：被藏的两台都不放（CDN 那两条照放）",
      not re.search(r"(?m)^(🇭🇰3|🇯🇵1)·(?!CDN)", sr) and len(re.findall(r"(?m)^(🇭🇰3|🇯🇵1)·CDN·", sr)) == 2)

# ── 6. 兜底：证书丢了 *.根域名 → 照用公网域名 ───────────────────────────────────────────────
CERT[:] = [DOM3]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    links_nc = x.aggregated_links()
check("证书没盖住：3 自己的节点照用公网域名、说一行；.links 也不再报私有名",
      [x._link_addr(u)[0] for u in links_nc if "🇭🇰3·reality" in x._link_name(u)] == [DOM3]
      and "证书里没有" in buf.getvalue() and x._privdns_links_line() == "")
CERT[:] = [DOM3, W3]

# ── 7. 成员机报了个别人家的名字 ───────────────────────────────────────────────────────
PEER_TEXT["https://m1.example/x.links"] = "\n".join(M1) + f"\n#privdns k7xq2m4p9z.other.example {DOH3}\n"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    links_bad = x.aggregated_links()
check("名字不在 1 自己的根域名底下：AdGuard 不收、1 照写原来的地址",
      "k7xq2m4p9z.other.example" not in x._privdns_map()
      and [x._link_addr(u)[0] for u in links_bad if "🇯🇵1·reality" in x._link_name(u)] == [DOM1])

# ── 8. 菜单：1（用域名装的成员机）选 2 ─────────────────────────────────────────────────────
ASK, REGEN, DISPATCH, APPLY, NGX = [], [], [], [], []
x._ask = lambda p="": (print(p + (ASK[0] if ASK else "")) or ASK.pop(0)) if ASK else ""
x._privdns_regen = lambda: REGEN.append(1)
x._node_op_dispatch = lambda plan, fb: (DISPATCH.append(plan), fb())
def fake_apply(dom, wildcard, cf, names=None, node_dom=None):
    APPLY.append((dom, wildcard, cf, list(names or [])))
    CERT[:] = list(names or [])
    return True
x.cert_install_apply = fake_apply
x._restore_state_to_G = lambda st: None
x._nginx_sync_from_cfg = lambda: NGX.append(1) or "✔ nginx 443 反代表已按现有节点重建"
def run_menu(*answers):
    ASK[:] = list(answers) + ["0"]
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        x.privdns_menu()
    return b.getvalue()
HOST[0] = DOM1
state(DOM1)
LOCAL[:] = M1
CERT[:] = [DOM1]
x.privdns_save({"mode": "off", "dns": "", "name": ""})
acct(False)
out = run_menu("2", DOH3, "")
check("没存令牌：先列出 Cloudflare 令牌怎么做、要粘令牌；没粘 → 取消，什么都没动",
      "Cloudflare 令牌怎么做" in out and "已取消" in out and not DISPATCH and x.privdns_conf()["mode"] == "off")
out = run_menu("2", DOH3, "tok-1", "")
check("粘了令牌、后果红字一行（证书要重签、小火箭不放），默认否 → 没动",
      "小火箭那份不放本机节点" in out and re.search(r"多签 \*\.[a-z0-9]{6}\.zone\.example", out) and not DISPATCH)
out = run_menu("2", DOH3, "tok-1", "y")
c = x.privdns_conf()
check(f"确认：转后台做，证书多签 *.随机段.根域名、原来的名字一个不丢、带着令牌（{APPLY[-1] if APPLY else ''}）",
      DISPATCH and DISPATCH[-1]["op"] == "privdns-domain" and APPLY
      and APPLY[-1][0] == DOM1 and APPLY[-1][2] == "tok-1"
      and APPLY[-1][3] == [DOM1, "*." + c["name"].split(".", 1)[1]])
check(f"存下：外机DNS解析、外机地址、节点专用域名在根域名底下（{c['name']}）",
      c["mode"] == "remote" and c["dns"] == DOH3 and c["name"].endswith("." + ROOTD))
check("nginx 按新设置重建、.links 末尾报节点专用域名 + 外机地址、重生成了配置",
      NGX and open(x.LINKS_FILE).read().splitlines()[-1] == f"#privdns {c['name']} {DOH3}" and REGEN)
first = c["name"]
APPLY.clear()
run_menu("2", DOH3, "y")
check("已经开着、证书也盖得住：不再签证书、名字不变", not APPLY and x.privdns_conf()["name"] == first)
acct(True)
REGEN.clear()
out = run_menu("3")
check("关：.links 末尾那行撤掉、本机配置重生成（节点换回公网域名），证书和 nginx 留着",
      x.privdns_conf()["mode"] == "off" and "#privdns" not in open(x.LINKS_FILE).read() and REGEN
      and "*." + first.split(".", 1)[1] in CERT)
x._privdns_new_name = lambda root="": "k7xq2m4p9z." + (root or "internal")
LOCAL[:] = node_links("jp9.wild.example", "🇯🇵9")      # vmess 是 base64，不能字符串替换
state("jp9.wild.example")
DISPATCH.clear()
out = run_menu("2", DOH3)
check("根域名上有泛解析（起的名字公网查得到）：红字说清、不开", "藏不住" in out and not DISPATCH)
real = {first, N3, N1R}
check(f"整个过程没把哪个真要用的节点专用域名送去公网 DNS 问（问过：{sorted(set(ASKED))[:4]}…）", not (real & set(ASKED)))

# ── 9. AdGuard 那边（adguard-dns.py）重签也带着 *.根域名 ─────────────────────────────────
a = load("agh", "adguard-dns.py")
a.BGP_DIR, a.PRIVDNS_FILE, a.ACME_CRT = tmp, x.PRIVDNS_FILE, f"{tmp}/acme.crt"
json.dump({"mode": "remote", "dns": DOH3, "name": "k7xq2m4p9z." + ROOTD}, open(x.PRIVDNS_FILE, "w"))
json.dump({"mode": "remote", "dns": DOH3, "name": N3}, open(x.PRIVDNS_FILE, "w"))
check("adguard-dns 认得新的两段式节点专用域名（证书要的是 *.随机段.根域名）", a._privdns_root() == P3)
json.dump({"mode": "remote", "dns": DOH3, "name": "k7xq2m4p9z." + ROOTD}, open(x.PRIVDNS_FILE, "w"))
check("老的「随机.根域名」照样认（已经开着的机器不受影响）", a._privdns_root() == ROOTD)
ARUNS, ASH = [], []
a.subprocess = types.SimpleNamespace(run=lambda cmd, **kw: ARUNS.append(cmd) or
                                     types.SimpleNamespace(returncode=0, stdout="Cert success", stderr=""))
a.sh = lambda cmd, check=True: ASH.append(cmd) or ""
a._port_busy = lambda p: None
a._install_cert_guarded = lambda *aa, **kw: True
a._cert_wildcard_ok = lambda d: False
a._cert_meta_set = lambda **kw: None
a.time = types.SimpleNamespace(sleep=lambda s: None)
acct(True)
with quiet():
    a._revert_wildcard(DOM3, "xyabc123", ask=False, auto_token=True)
check(f"撤销 DoT 泛域名：80 空着也只能 DNS 验证、带着 *.{ROOTD}（{ARUNS[-1][:90] if ARUNS else ''}）",
      ARUNS and "--dns dns_cf" in ARUNS[-1] and f"-d '*.{ROOTD}'" in ARUNS[-1] and "--standalone" not in ARUNS[-1])
check("撤销时 CF 令牌留着（节点专用域名续期还要用）",
      "SAVED_CF_Token" in open(f"{HOME}/.acme.sh/account.conf").read())
asrc = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("菜单 8 签泛域名时也带上 *.根域名", "-d '*.{dom}'\" + (f\" -d '*.{root}'\" if root else \"\")" in asrc)
a2 = load("agh2", "adguard-dns.py")
a2.PRIVDNS_FILE, a2.ACME_CRT, a2.ACME_KEY = x.PRIVDNS_FILE, f"{tmp}/acme.crt", f"{tmp}/acme.key"
open(a2.ACME_CRT, "w").write("old-cert"); open(a2.ACME_KEY, "w").write("old-key")
a2._node_domain = lambda: DOM3
a2._cert_names = lambda p: [DOM3]                # 新导出的这张丢了 *.根域名
A2SH = []
a2.sh = lambda cmd, check=True: A2SH.append(cmd) or ""
with quiet():
    ok = a2._install_cert_guarded("acme.sh", DOM3, "", "测试")
check("导出后发现丢了 *.根域名：当失败、还原原来那张，也不会去动 *.根域名 那条续期记录",
      ok is False and open(a2.ACME_CRT).read() == "old-cert" and not any("-d *." in c_ or "-d '*." in c_ for c_ in A2SH))

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
