#!/usr/bin/env python3
"""节点私有解析（13 → 11「节点使用私有DNS解析」，仓库主人 10/28）——
  「有些机场他建的节点必须要用他自己对应的私有dns解析才可以解析」「我有三台机器1，2，3，有1，2两台没有建DNS，3建了，
  1，2的节点可以指定3的DNS解析吗」。定下来的做法：
  · 1、2（用 IP 装的）选「2 外机DNS解析」、粘贴 3 的 DoH：本机起固定私有名，.links 末尾一行 `#privdns 名字 地址`
    （老版主机只认节点链接前缀，这一行自动忽略）。本机自己的订阅不换（订阅地址本身就是本机 IP）
  · 3（装了 AdGuard）选「1 本机DNS解析」：汇总时把「1 的私有名 → 1 的 IP」写进自己 AdGuard 的自定义规则
    （只动自己那一段、用户规则一行不动；名字或 IP 变了才重启；外机走了留 7 天）
  · 谁汇总 1，先去 DNS 问一遍：对上了才把 1 的节点地址换成私有名（SNI / Host 一个字不变），问不通照写真实地址
  · mihomo：proxy-server-nameserver-policy；sing-box：DoH 服务器 + 出站 domain_resolver；
    小火箭：被藏的节点不放（「小火箭的也删掉」）；直连规则不再写被藏机器的真实 IP（「那个真实IP可以删掉」）
  · 三份配置里任何地方都不出现被藏机器的真实 IP
真 DoH：本地起一个 TLS 的假 AdGuard（自签 CA，按写进 yaml 的规则答），_doh_ask 照真流程握手、POST、解析回复。
"""
import os, re, sys, io, json, time, types, ssl, socket, struct, threading, subprocess, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

DOM = "dns.example"                # DNS 机（3）的域名
# ── 假 AdGuard：TLS + DoH，按 AdGuardHome.yaml 里我们那段规则答 ─────────────────────────────
D = f"{tmp}/pki"; os.makedirs(D)
def ossl(*a):
    subprocess.run(["openssl", *a], cwd=D, check=True, capture_output=True)
ossl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "ca.key",
     "-out", "ca.crt", "-subj", "/CN=test-ca", "-days", "2")
ossl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "srv.key",
     "-out", "srv.csr", "-subj", "/CN=dns.example")
open(f"{D}/ext", "w").write("subjectAltName=DNS:dns.example\n")
ossl("x509", "-req", "-in", "srv.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.crt",
     "-days", "2", "-extfile", "ext")
sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(f"{D}/srv.crt", f"{D}/srv.key")
cctx = ssl.create_default_context(cafile=f"{D}/ca.crt")
ANS, STATUS, SEEN = {}, [200], []
lsock = socket.socket(); lsock.bind(("127.0.0.1", 0)); lsock.listen(16)
PORT = lsock.getsockname()[1]

def dns_reply(q):
    i, labels = 12, []
    while q[i]:
        labels.append(q[i + 1:i + 1 + q[i]].decode()); i += q[i] + 1
    name, qt = ".".join(labels), struct.unpack(">H", q[i + 1:i + 3])[0]
    rc, ips = ANS.get((name, qt), (0, []) if any(k[0] == name for k in ANS) else (3, []))
    ans = b""
    for ip in ips:
        rd = socket.inet_pton(socket.AF_INET if qt == 1 else socket.AF_INET6, ip)
        ans += struct.pack(">HHHIH", 0xC00C, qt, 1, 60, len(rd)) + rd      # 名字用压缩指针指回问题
    return struct.pack(">HHHHHH", 0, 0x8180 | rc, 1, len(ips), 0, 0) + q[12:i + 5] + ans

def serve():
    while True:
        try:
            c, _ = lsock.accept()
        except OSError:
            return
        try:
            t = sctx.wrap_socket(c, server_side=True)
            data = b""
            while b"\r\n\r\n" not in data:
                d = t.recv(4096)
                if not d:
                    break
                data += d
            head, _, body = data.partition(b"\r\n\r\n")
            n = int(re.search(rb"(?i)content-length: (\d+)", head).group(1))
            while len(body) < n:
                body += t.recv(4096)
            SEEN.append(head.decode(errors="replace"))
            if STATUS[0] != 200:
                t.sendall(f"HTTP/1.0 {STATUS[0]} No\r\n\r\n".encode())
            else:
                msg = dns_reply(body)
                t.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/dns-message\r\nContent-Length: %d\r\n\r\n"
                          % len(msg) + msg)
            t.close()
        except Exception:
            try: c.close()
            except Exception: pass
threading.Thread(target=serve, daemon=True).start()

def agh_reload():
    """假 AdGuard「重启」：重新读 yaml 里 user_rules 的 $dnsrewrite 规则。"""
    ANS.clear()
    rules = yaml.safe_load(open(x.AGH_YAML)).get("user_rules") or [] if yaml else \
        re.findall(r"- '([^']*)'", open(x.AGH_YAML).read())
    for r in rules:
        m_ = re.match(r"^\|\|(.+)\^\$dnsrewrite=NOERROR;(A|AAAA);(.+)$", str(r))
        if m_:
            k = (m_.group(1), 1 if m_.group(2) == "A" else 28)
            ANS.setdefault(k, (0, []))[1].append(m_.group(3))

class _Sock:                     # 连 DNS 机域名的都连到本机假 AdGuard；别的照真 socket
    def __getattr__(self, k):
        return getattr(socket, k)
    def create_connection(self, addr, *a, **kw):
        return socket.create_connection(("127.0.0.1" if addr[0] == DOM else addr[0], addr[1]), *a, **kw)
x.socket = _Sock()
x.ssl = types.SimpleNamespace(create_default_context=lambda: cctx, SSLError=ssl.SSLError)

# ── 路径 / 外部调用全换成临时的 ─────────────────────────────────────────────────────
for k in ("PRIVDNS_FILE", "PRIVDNS_MAP_FILE", "LINKS_FILE", "CFG_FILE", "SBOX_FILE", "SR_FILE", "SR_NODES_FILE",
          "HOST_FILE", "PEERS_FILE", "SELFDNS_FLAG", "SELFDNS_CID_FILE", "TOKENS_FILE", "HY2_BW_FILE", "AGH_YAML"):
    setattr(x, k, f"{tmp}/{k.lower()}")
for e, f in (("yaml", "CFG_FILE"), ("json", "SBOX_FILE"), ("conf", "SR_FILE")):
    x.FMT[e]["file"] = getattr(x, f)
x.XRAY_DIR = f"{tmp}/xray"
x.BGP_DIR = tmp
CALLS = []
def fake_sh(cmd, check=True):
    CALLS.append(cmd)
    if "restart AdGuardHome" in cmd:
        agh_reload()
    return ""
x.sh = fake_sh
x.selfdns_clientid = lambda: "xyabc123"
TPL = {"yaml": open(os.path.join(ROOT, "sub-template.yaml")).read(),
       "json": open(os.path.join(ROOT, "subbox-template.json")).read(),
       "conf": open(os.path.join(ROOT, "shadowrocket-template.conf")).read()}
x.fetch_tpl = lambda u: TPL[u]
x.sr_sub_mode = lambda: False
x._sr_nodes_link = lambda: None
HOST = [DOM]
x._host = lambda: HOST[0]
LOCAL, PEERS, PEER_TEXT = [], [], {}
x.read_saved_links = lambda: list(LOCAL)
x.load_peers = lambda: list(PEERS)
def fake_fetch(u, timeout=15):
    if u not in PEER_TEXT:
        raise OSError("不通")
    return PEER_TEXT[u]
x._fetch_text = fake_fetch

DOH = f"https://dns.example:{PORT}/dns-query/xyabc123"
open(x.AGH_YAML, "w").write(
    "http:\n  pprof:\n    port: 6060\n  address: 0.0.0.0:3000\nusers:\n  - name: admin\n    password: $2a$10$x\n"
    "dns:\n  bind_hosts:\n    - 0.0.0.0\n  port: 53\ntls:\n  enabled: true\n  server_name: dns.example\n"
    f"  port_https: {PORT}\nfilters:\n  - enabled: true\n    url: https://filters.example/f.txt\n    name: f\n    id: 1\n"
    "user_rules:\n  - '@@||mine.example^'\n  - '||ads.example^'\ndhcp:\n  enabled: false\nschema_version: 29\n")
os.chmod(x.AGH_YAML, 0o644)
x._agh_doh_url = lambda: DOH if HOST[0] == DOM else ""

# 文档专用网段（RFC 5737）；运行时拼出来，claude/ 里不留 IP 字面量（test_claude_folder 守着）
ip = lambda *o: ".".join(map(str, o))
IP1, IP2, CF = ip(203, 0, 113, 10), ip(198, 51, 100, 20), ip(104, 16, 1, 1)
IP1B, IPX, IPY, IPZ = ip(203, 0, 113, 77), ip(192, 0, 2, 7), ip(192, 0, 2, 9), ip(192, 0, 2, 99)
def vm(add, ps):
    return x.vmess_link({"v": "2", "ps": ps, "add": add, "port": "36001", "id": "8c1f-uuid", "aid": "0", "net": "ws",
                         "type": "none", "host": "www.bing.com", "path": "/vm", "tls": "tls", "sni": "www.bing.com",
                         "fp": "chrome", "allowInsecure": "1"})
M1 = [f"vless://8c1f-uuid@{IP1}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.cisco.com&fp=chrome&pbk=PBK1&sid=ab&type=tcp#🇺🇸1·reality-vision",
      f"hysteria2://pw1@{IP1}:31000?sni=www.bing.com&insecure=1&mport=31000-32000#🇺🇸1·hy2",
      f"tuic://8c1f-uuid:pw1@{IP1}:32001?congestion_control=bbr&alpn=h3&sni=www.bing.com&allow_insecure=1#🇺🇸1·tuic",
      f"anytls://pw1@{IP1}:33001?sni=www.bing.com&fp=chrome&insecure=1#🇺🇸1·anytls",
      f"ss://{x.ss_userinfo('2022-blake3-aes-128-gcm', 'K1==')}@{IP1}:34001#🇺🇸1·ss2022",
      f"trojan://pw1@{IP1}:35001?security=tls&sni=www.bing.com&fp=chrome&allowInsecure=1#🇺🇸1·trojan",
      vm(IP1, "🇺🇸1·vmess-ws"),
      f"vless://8c1f-uuid@{IP1}:37001?encryption=none&security=tls&sni=www.bing.com&fp=chrome&type=ws&host=www.bing.com&path=%2Fvl&allowInsecure=1#🇺🇸1·vless-ws",
      f"vless://8c1f-uuid@{CF}:2053?encryption=none&security=tls&sni=cdn.example.com&fp=chrome&type=ws&host=cdn.example.com&path=%2Fcdn#🇺🇸1·CDN·vless-ws"]
M2 = [f"vless://9d2e-uuid@{IP2}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.apple.com&fp=chrome&pbk=PBK2&sid=cd&type=tcp#🇯🇵2·reality-vision"]
L3 = [f"vless://7a0b-uuid@{DOM}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.cisco.com&fp=chrome&pbk=PBK3&sid=ef&type=tcp#🇭🇰3·reality-vision"]

# ── 1. 换地址：别的一个字节不动，SNI / Host 照旧 ─────────────────────────────────────────
NAME1 = "k7xq2m4p9z.internal"
ok_all = True
for u in M1[:-1]:
    v = x._link_set_server(u, NAME1)
    d0, d1 = x.link_to_proxy(u), x.link_to_proxy(v)
    same = {k: w for k, w in d0.items() if k != "server"} == {k: w for k, w in d1.items() if k != "server"}
    if not (d1["server"] == NAME1 and same and (u.startswith("vmess://") or v == u.replace(f"@{IP1}:", f"@{NAME1}:"))):
        ok_all = False; print("    换坏了：", u[:40], "→", v[:60])
check("九种链接换成私有名：只有地址变了，SNI / Host / 端口 / 跳跃段 / 密码原样", ok_all)
v6 = x._link_set_server("trojan://pw@[2001:db8::10]:443?security=tls&sni=www.bing.com#x", NAME1)
check(f"IPv6 地址（方括号）也换得对（{v6}）", v6 == f"trojan://pw@{NAME1}:443?security=tls&sni=www.bing.com#x")

# ── 2. 哪些机器能藏 ─────────────────────────────────────────────────────────────────
check(f"1（IP 装的）能藏：IP 列表只有它自己，Cloudflare 的地址不算（{x._privdns_ips(M1)}）", x._privdns_ips(M1) == [IP1])
check("用域名装的不藏（地址是域名）", x._privdns_ips(L3) == [])
check("有一条没写 SNI：整台不藏（不然私有名会明文出现在握手里）",
      x._privdns_ips(M1 + [f"trojan://pw@{IP1}:1?security=tls#🇺🇸1·trojan²"]) == [])
check("ws 没写 Host：整台不藏", x._privdns_ips(M1 + [f"vless://u@{IP1}:2?security=tls&sni=a.b&type=ws&path=%2F#🇺🇸1·x"]) == [])
check("老式整串 base64 的 ss 认不准：整台不藏", x._privdns_ips(M1 + ["ss://" + "QUJD" * 6 + "#🇺🇸1·ss"]) == [])

# ── 3. .links 末尾那一行 ────────────────────────────────────────────────────────────
x.privdns_save({"mode": "remote", "dns": DOH, "name": NAME1})
check(f"设置文件只给 root 看（{oct(os.stat(x.PRIVDNS_FILE).st_mode & 0o777)}）", os.stat(x.PRIVDNS_FILE).st_mode & 0o777 == 0o600)
x._write_links_file(M1)
lt = open(x.LINKS_FILE).read()
check("外机DNS解析：.links 链接照写真实 IP，末尾多一行 #privdns 名字 地址",
      lt.splitlines()[:len(M1)] == M1 and lt.splitlines()[-1] == f"#privdns {NAME1} {DOH}")
check("老版主机只认节点链接前缀：这一行不会被当成节点",
      [l for l in lt.splitlines() if l.strip().startswith(x._NODE_SCHEMES)] == M1)
check("读得回来", x._privdns_meta(lt) == (NAME1, DOH))
check("名字 / 地址写错的不认", x._privdns_meta("#privdns k7.example http://a.example/dns-query") is None
      and x._privdns_meta(f"#privdns {NAME1} https://dns.example/other") is None)
x.privdns_save({"mode": "off", "dns": DOH, "name": NAME1})
x._write_links_file(M1)
check("关了：.links 只剩链接", open(x.LINKS_FILE).read().splitlines() == M1)

# ── 4. 真 DoH 问答 ─────────────────────────────────────────────────────────────────
ANS.clear(); ANS[(NAME1, 1)] = (0, [IP1])
check("问到了：(0, [IP])", x._doh_ask(DOH, NAME1) == (0, [IP1]))
check(f"发出去的请求：POST 到带 ClientID 的路径、中性 UA、不报家门（{SEEN[-1].splitlines()[0]}）",
      SEEN[-1].startswith("POST /dns-query/xyabc123 HTTP/1.0") and f"User-Agent: {x.HTTP_UA}" in SEEN[-1]
      and not re.search(r"(?i)xy-installer|bgpeer|nodekit|python", SEEN[-1]))
check("没有这个名字：rcode 3", x._doh_ask(DOH, "nope000000.internal")[0] == 3)
check("对上了：_privdns_check 回空", x._privdns_check(NAME1, DOH, [IP1]) == "")
check("IP 对不上：说清楚", "对不上" in x._privdns_check(NAME1, DOH, [IPY]))
check("名字没有：说清楚", "没有这个名字" in x._privdns_check("nope000000.internal", DOH, [IP1]))
STATUS[0] = 403
check("被白名单挡了（非 200）：问不到", "问不到" in x._privdns_check(NAME1, DOH, [IP1]))
STATUS[0] = 200
ANS[(NAME1, 1)] = (5, [])
check("DNS 拒绝：说是 ClientID", "ClientID" in x._privdns_check(NAME1, DOH, [IP1]))
check("连不上：问不到（不抛异常）", "问不到" in x._privdns_check(NAME1, "https://dns.example:1/dns-query", [IP1]))
try:
    x._doh_ask(f"https://wrong.example:{PORT}/dns-query", NAME1, local="127.0.0.1"); cert_ok = False
except Exception as e_:
    cert_ok = "certificate" in str(e_).lower() or "hostname" in str(e_).lower() or isinstance(e_, ssl.SSLError)
check("证书对不上域名就不认（照样验证书）", cert_ok)
ANS.clear()

# ── 5. AdGuard 自定义规则：只动自己那段 ──────────────────────────────────────────────
base = open(x.AGH_YAML).read()
R = [f"||{NAME1}^$dnsrewrite=NOERROR;A;{IP1}"]
y1 = x._agh_user_rules_put(base, R)
check("用户自己的两条规则原样留着，我们那段接在后面（开头 / 结尾两行注释圈住）",
      "  - '@@||mine.example^'\n  - '||ads.example^'\n  - '! xy-privdns begin'\n"
      f"  - '||{NAME1}^$dnsrewrite=NOERROR;A;{IP1}'\n  - '! xy-privdns end'\ndhcp:" in y1)
check("别的段一个字节不动", y1.replace("\n  - '! xy-privdns begin'\n" f"  - '||{NAME1}^$dnsrewrite=NOERROR;A;{IP1}'\n"
                                     "  - '! xy-privdns end'", "") == base)
if yaml:
    check("写出来还是合法 YAML，user_rules 四项", yaml.safe_load(y1)["user_rules"][-1] == "! xy-privdns end"
          and len(yaml.safe_load(y1)["user_rules"]) == 5)
check("规则没变：原文返回（不写、不重启）", x._agh_user_rules_put(y1, R) == y1)
requoted = y1.replace(f"'||{NAME1}^$dnsrewrite=NOERROR;A;{IP1}'", f'"||{NAME1}^$dnsrewrite=NOERROR;A;{IP1}"')
check("AdGuard 自己换了引号也算没变", x._agh_user_rules_put(requoted, R) == requoted)
y2 = x._agh_user_rules_put(y1, [f"||{NAME1}^$dnsrewrite=NOERROR;A;{IPX}"])
check("IP 变了：只换我们那段", f"A;{IPX}'" in y2 and IP1 not in y2 and "'||ads.example^'" in y2)
check("没有要写的：整段撤掉，回到原样", x._agh_user_rules_put(y1, []) == base)
e1 = "user_rules: []\ndhcp:\n  enabled: false\n"
e2 = x._agh_user_rules_put(e1, R)
check("原来是空列表 []：改成一项一行", e2.startswith("user_rules:\n  - '! xy-privdns begin'\n") and e2.endswith("dhcp:\n  enabled: false\n"))
check("撤空了写回 []", x._agh_user_rules_put(e2, []) == e1)
check("一行写完的 [a, b]：认不准，不动", x._agh_user_rules_put("user_rules: ['a', 'b']\n", R) is None)
check("只有开头没有结尾（手改坏了）：不动，免得把用户规则当成我们的删了",
      x._agh_user_rules_put("user_rules:\n  - '! xy-privdns begin'\n  - '||user.rule^'\n", R) is None)
check("跨行的长规则：认不准，不动", x._agh_user_rules_put("user_rules:\n  - 'a'\n    b\n", R) is None)
check("没有 user_rules：不动", x._agh_user_rules_put("dns:\n  port: 53\n", R) is None)

# ── 6. 当 DNS 机：映射 → AdGuard，变了才重启，外机走了留 7 天 ─────────────────────────────
HOST[0] = DOM
x.privdns_save({"mode": "self", "dns": "", "name": ""})
CALLS.clear()
with quiet():
    mp = x._privdns_sync([(M1, (NAME1, DOH)), (M2, None)])
check(f"指定了本机的外机记进映射（{mp}）", list(mp) == [NAME1] and mp[NAME1]["ips"] == [IP1] and mp[NAME1]["label"] == "🇺🇸1")
check("映射文件、AdGuard 配置都收成 0600（里面有别的机器的 IP）",
      os.stat(x.PRIVDNS_MAP_FILE).st_mode & 0o777 == 0o600 and os.stat(x.AGH_YAML).st_mode & 0o777 == 0o600)
check("重启了一次 AdGuard，重启后真按规则答", CALLS.count("systemctl restart AdGuardHome") == 1 and ANS.get((NAME1, 1)) == (0, [IP1]))
CALLS.clear()
with quiet():
    x._privdns_sync([(M1, (NAME1, DOH))])
check("再对一遍、什么都没变：不重启", not CALLS)
M1b = [u.replace(IP1, IP1B) if "CDN·" not in u else u for u in M1[:-1]] + [M1[-1]]
M1b[6] = vm(IP1B, "🇺🇸1·vmess-ws")
with quiet():
    x._privdns_sync([(M1b, (NAME1, DOH))])
check("1 换了 IP：规则跟着改、重启一次，名字不变（客户端不用刷新订阅）",
      CALLS.count("systemctl restart AdGuardHome") == 1 and ANS.get((NAME1, 1)) == (0, [IP1B]))
CALLS.clear()
with quiet():
    x._privdns_sync([])
check("1 这一轮拉不到 / 删掉了：映射照留（7 天内）、不重启", NAME1 in x._privdns_map() and not CALLS)
real_time = time.time
x.time.time = lambda: real_time() + x.PRIVDNS_GRACE + 60
with quiet():
    x._privdns_sync([])
x.time.time = real_time
check("过了 7 天：撤掉规则、重启一次、映射文件删掉，用户规则还在",
      CALLS.count("systemctl restart AdGuardHome") == 1 and "xy-privdns" not in open(x.AGH_YAML).read()
      and "'||ads.example^'" in open(x.AGH_YAML).read() and not os.path.exists(x.PRIVDNS_MAP_FILE))
NAME2 = "a1b2c3d4e5.internal"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    mp = x._privdns_sync([(M1, (NAME1, DOH)), ([u.replace(IP1, IPX) for u in M1[:1]], (NAME1, DOH))])
check(f"两台报的私有名一样（整机克隆）：先到先得，后一台说一行（{buf.getvalue().strip()[:50]}）",
      mp[NAME1]["ips"] == [IP1] and "克隆" in buf.getvalue())
x.privdns_save({"mode": "off", "dns": "", "name": ""})
with quiet():
    mp = x._privdns_sync([(M1, (NAME1, DOH))])
check("本机没开「本机DNS解析」：不收新的（旧的照 7 天到期）", mp.get(NAME1, {}).get("ips") == [IP1] and NAME2 not in mp)

# ── 7. 汇总：问过 DNS、对上了才换 ─────────────────────────────────────────────────────
for f_ in (x.PRIVDNS_MAP_FILE,):
    if os.path.exists(f_): os.remove(f_)
open(x.AGH_YAML, "w").write(base); agh_reload()
x.privdns_save({"mode": "self", "dns": "", "name": ""})
LOCAL[:] = L3
PEERS[:] = ["https://m1.example/x.links", "https://m2.example/y.links", "https://down.example/z.links"]
PEER_TEXT.clear()
PEER_TEXT["https://m1.example/x.links"] = "\n".join(M1) + f"\n#privdns {NAME1} {DOH}\n"
PEER_TEXT["https://m2.example/y.links"] = "\n".join(M2) + "\n"
CALLS.clear()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    links = x.aggregated_links()
srv = {x._link_name(u): x._link_addr(u)[0] for u in links}
check("3 自己的节点不动（域名装的）", any(v == DOM for k, v in srv.items() if "🇭🇰3" in k))
check("1 的节点全换成私有名，CDN 那条照旧连 Cloudflare",
      all(v == NAME1 for k, v in srv.items() if "🇺🇸1" in k and "CDN" not in k)
      and [v for k, v in srv.items() if "CDN" in k] == [CF])
check("2 没开私有解析：照写真实 IP", [v for k, v in srv.items() if "🇯🇵2" in k] == [IP2])
check("拉不到的成员机照旧跳过", len(links) == len(L3) + len(M1) + len(M2))
check("头一次：AdGuard 重启一次，屏上没有 ⚠", CALLS.count("systemctl restart AdGuardHome") == 1 and "⚠" not in buf.getvalue())
check("这一轮真换了的记下来（三个生成器看它）", x._PRIVDNS_LIVE == {NAME1: DOH})

# ── 8. 三份配置 ─────────────────────────────────────────────────────────────────────
ylines, nodes = x.parse_nodes(links)
with quiet():
    x.gen_mihomo(ylines, nodes, "yaml")
    x.gen_singbox(ylines, nodes, "json")
    x.gen_shadow(ylines, nodes, "conf")
mh, sb, sr = (open(f).read() for f in (x.CFG_FILE, x.SBOX_FILE, x.SR_FILE))
check("三份配置里都找不到 1 的真实 IP（节点地址、直连规则都没有）", all(IP1 not in t for t in (mh, sb, sr)))
check("2 没藏：直连规则照旧写它的 IP（mihomo / sing-box / 小火箭）",
      f"IP-CIDR,{IP2}/32,DIRECT,no-resolve" in mh and f"{IP2}/32" in sb and f"IP-CIDR,{IP2}/32,DIRECT,no-resolve" in sr)
if yaml:
    cy = yaml.safe_load(mh)
    pol = cy["dns"].get("proxy-server-nameserver-policy") or {}
    px = [p for p in cy["proxies"] if "🇺🇸1" in p["name"]]
    check(f"mihomo：proxy-server-nameserver-policy 只把这个名字指给 3（{pol}）", pol == {NAME1: [DOH]})
    check("mihomo：proxy-server-nameserver 原样还在（mihomo 规定 policy 不能单独出现）",
          cy["dns"]["proxy-server-nameserver"] == yaml.safe_load(TPL["yaml"])["dns"]["proxy-server-nameserver"])
    check("mihomo：1 的节点地址是私有名，SNI 原样",
          px and all(p["server"] in (NAME1, CF) for p in px)
          and [p.get("servername") for p in px if p["type"] == "vless" and p.get("reality-opts")] == ["www.cisco.com"])
cj = json.loads(sb)
ps = [s for s in cj["dns"]["servers"] if s.get("tag") == "privdns"]
check(f"sing-box：加了一台 DoH（3 的域名、端口、带 ClientID 的路径，bootstrap 解析、走直连）（{ps}）",
      ps == [{"tag": "privdns", "type": "https", "server": DOM, "server_port": PORT,
              "path": "/dns-query/xyabc123", "domain_resolver": "bootstrap", "detour": "🎯直连"}])
ob = [o for o in cj["outbounds"] if isinstance(o, dict) and o.get("server") == NAME1]
check(f"sing-box：1 的出站全指向它（{len(ob)} 个），别的出站不带",
      ob and all(o.get("domain_resolver") == "privdns" for o in ob)
      and not [o for o in cj["outbounds"] if isinstance(o, dict) and o.get("domain_resolver") and o.get("server") != NAME1])
check("sing-box：1 的 reality 出站 SNI 原样", [o["tls"]["server_name"] for o in ob if o["type"] == "vless"
                                            and o["tls"].get("reality")] == ["www.cisco.com"])
check("小火箭：1 的节点一个都不放（「小火箭的也删掉」），CDN 那条和 2、3 照放",
      not re.search(r"(?m)^🇺🇸1·(?!CDN)", sr) and re.search(r"(?m)^🇺🇸1·CDN·vless-ws = ", sr)
      and re.search(r"(?m)^🇯🇵2·reality-vision = ", sr) and re.search(r"(?m)^🇭🇰3·reality-vision = ", sr))
x.sr_sub_mode = lambda: True
with quiet():
    x.build_shadowrocket_sub(nodes, "conf")
txt = __import__("base64").b64decode(open(x.SR_NODES_FILE).read()).decode()
check("小火箭节点订阅（.txt）也不放", IP1 not in txt and NAME1 not in txt and IP2 in txt)
x.sr_sub_mode = lambda: False
only = [(k, d) for k, d in nodes if d.get("_privdns")]
open(x.SR_FILE, "w").write(f"old = vless,{IP1},443\n")
with quiet():
    x.build_shadowrocket_sub(only, "conf")
check("全是被藏的节点：小火箭照样重写，上一份写着真实 IP 的不留在订阅上", IP1 not in open(x.SR_FILE).read())
targets = x._direct_targets(nodes)
check(f"直连目标里没有 1（{targets}）", ("ip", IP1) not in targets and not any(NAME1 in v for _, v in targets))

# ── 9. 问不通：照写真实地址 ───────────────────────────────────────────────────────────
ANS[(NAME1, 1)] = (0, [IPZ])          # DNS 答的是别的 IP
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    links_bad = x._privdns_apply([(L3, None), (M1, (NAME1, DOH))])
check(f"DNS 答的 IP 对不上：1 照写真实 IP，屏上一行 ⚠（{buf.getvalue().strip()[:60]}）",
      links_bad == L3 + M1 and "⚠ 🇺🇸1 私有解析没对上" in buf.getvalue() and not x._PRIVDNS_LIVE)
yl, nd = x.parse_nodes(links_bad)
with quiet():
    x.gen_shadow(yl, nd, "conf")
check("没藏成：小火箭照放 1 的节点、直连规则照写 1 的 IP", re.search(r"(?m)^🇺🇸1·reality-vision = ", open(x.SR_FILE).read())
      and f"IP-CIDR,{IP1}/32" in open(x.SR_FILE).read())
x.privdns_save({"mode": "off", "dns": "", "name": ""})
agh_reload()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    links_off = x._privdns_apply([(L3, None), (M1, (NAME1, DOH))])
check("1 指定了本机，但本机没开「本机DNS解析」：不换，说一句", links_off == L3 + M1 and "本机没开" in buf.getvalue())

# ── 10. 别的主机汇总 1（自己不是 DNS 机），去 3 问 ───────────────────────────────────────
HOST[0] = "agg4.example.com"
ANS.clear(); ANS[(NAME1, 1)] = (0, [IP1])
for f_ in (x.PRIVDNS_MAP_FILE,):
    if os.path.exists(f_): os.remove(f_)
x.privdns_save({"mode": "off", "dns": "", "name": ""})
CALLS.clear()
with quiet():
    lk = x._privdns_apply([([], None), (M1, (NAME1, DOH))])
check("4（没 AdGuard）汇总 1：去 3 问、对上了照样换名，自己的 AdGuard 一个字不碰",
      all(x._link_addr(u)[0] == NAME1 for u in lk if "CDN·" not in x._link_name(u)) and not CALLS)

# ── 11. 没用这个功能的机器：什么都不变 ─────────────────────────────────────────────────
orig_sync = x._privdns_sync
x._privdns_sync = lambda *a: (_ for _ in ()).throw(AssertionError("不该对映射"))
PEER_TEXT["https://m1.example/x.links"] = "\n".join(M1) + "\n"
with quiet():
    plain = x.aggregated_links()
check("成员机没开、本机也没开：不碰 AdGuard、不出网问 DNS、链接原样",
      [x._link_addr(u)[0] for u in plain if "🇺🇸1" in x._link_name(u) and "CDN" not in x._link_name(u)] == [IP1] * 8
      and not x._PRIVDNS_LIVE)
yl, nd = x.parse_nodes(plain)
with quiet():
    x.gen_mihomo(yl, nd, "yaml"); x.gen_singbox(yl, nd, "json")
check("配置里没有 privdns 的痕迹", "proxy-server-nameserver-policy" not in open(x.CFG_FILE).read()
      and "privdns" not in open(x.SBOX_FILE).read())
x._privdns_sync = orig_sync

# ── 12. mihomo 自定义模板的几种写法 ──────────────────────────────────────────────────
used_nodes = [("reality-vision", {"server": NAME1, "_privdns": DOH})]
t1 = "dns:\n  enable: true\n  proxy-server-nameserver:\n    - https://doh.example/dns-query\n  nameserver: [doh2.example]\nrules:\n"
r1 = x._mihomo_privdns(t1, used_nodes)
check("proxy-server-nameserver 写成多行列表：policy 接在整个列表后面", yaml is None or
      yaml.safe_load(r1)["dns"]["proxy-server-nameserver-policy"] == {NAME1: [DOH]}
      and yaml.safe_load(r1)["dns"]["proxy-server-nameserver"] == ["https://doh.example/dns-query"])
t2 = "dns:\n  proxy-server-nameserver: [a]\n  proxy-server-nameserver-policy:\n    \"x.com\": [b]\n"
r2 = x._mihomo_privdns(t2, used_nodes)
check("模板已经有 policy：加进去，原有的留着", yaml is None or
      yaml.safe_load(r2)["dns"]["proxy-server-nameserver-policy"] == {"x.com": ["b"], NAME1: [DOH]})
t3 = "dns:\n  nameserver-policy:\n    \"+.cn\": [c]\n"
r3 = x._mihomo_privdns(t3, used_nodes)
check("没写 proxy-server-nameserver：写进 nameserver-policy（这时 mihomo 解析节点地址就用它）", yaml is None or
      yaml.safe_load(r3)["dns"]["nameserver-policy"] == {"+.cn": ["c"], NAME1: [DOH]})
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    r4 = x._mihomo_privdns("proxies:\n", used_nodes)
check("两样都没有：原样、说一行", r4 == "proxies:\n" and "连不上" in buf.getvalue())

# ── 13. 菜单 13 → 11 ────────────────────────────────────────────────────────────────
ASK = []
x._ask = lambda p="": (print(p + (ASK[0] if ASK else "")) or ASK.pop(0)) if ASK else ""
REGEN = []
x._privdns_regen = lambda: REGEN.append(1)
def run_menu(*answers):
    ASK[:] = list(answers) + ["0"]
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        x.privdns_menu()
    return b.getvalue()

HOST[0] = "agg1-ip-host"
x._host = lambda: IP1
LOCAL[:] = L3
x.privdns_save({"mode": "off", "dns": "", "name": ""})
out = run_menu("2")
check("域名装的机器选 2：一行提示藏不了，不改设置", "藏不了" in out and x.privdns_conf()["mode"] == "off")
LOCAL[:] = M1
out = run_menu("2", "http://bad.example")
check("地址不对：不认", "不是外机的 DNS 地址" in out and x.privdns_conf()["mode"] == "off")
out = run_menu("2", DOH, "")
check(f"粘了地址、后果用红字一行说清，默认否 → 没改动", "小火箭那份不放本机节点" in out and "已取消" in out
      and x.privdns_conf()["mode"] == "off")
ANS.clear()
out = run_menu("2", DOH, "y")
c = x.privdns_conf()
check(f"确认 → 外机DNS解析：起了私有名、.links 末尾一行（{c['name']}）",
      c["mode"] == "remote" and c["dns"] == DOH and re.match(r"^[a-z0-9]{10}\.internal$", c["name"])
      and open(x.LINKS_FILE).read().splitlines()[-1] == f"#privdns {c['name']} {DOH}")
check("外机还没对上：屏上一行 ⚠ + 一行提示去外机加 links、点更新配置", "外机还没对上" in out and "聚合节点链接" in out)
check("本机自己的订阅不重生成（本机节点不换，订阅地址本身就是本机 IP）", not REGEN)
ANS[(c["name"], 1)] = (0, [IP1])
out = run_menu()
check("外机对上了：标题「当前：外机」、一行 ✔", "当前：外机" in out and "✔ 外机已对上" in out)
first = c["name"]
run_menu("2", DOH, "y")
check("已经是外机DNS解析时换地址：名字不变", x.privdns_conf()["name"] == first)
out = run_menu("3")
check("关：.links 末尾那行撤掉、不用确认（节点只是改回真实地址）",
      x.privdns_conf()["mode"] == "off" and "#privdns" not in open(x.LINKS_FILE).read() and "确认" not in out)
run_menu("2", DOH, "y")
check("关了再开：起个新名字（整机克隆的机器这样就分开了）", x.privdns_conf()["name"] != first)

x._host = lambda: DOM
x._agh_doh_url = lambda: ""
out = run_menu("1")
check("没装好 AdGuard 选 1：一行提示，不改", "先装好 AdGuard" in out and x.privdns_conf()["mode"] == "remote")
x._agh_doh_url = lambda: DOH
REGEN.clear()
out = run_menu("1")
check("选 1：本机当 DNS 机，.links 末尾那行撤掉、重生成配置、屏上给出要粘到外机的地址",
      x.privdns_conf()["mode"] == "self" and "#privdns" not in open(x.LINKS_FILE).read() and REGEN
      and "本机 DNS 地址（给外机粘贴）" in out and DOH in out)
json.dump({NAME1: {"ips": [IP1], "seen": int(time.time()), "label": "🇺🇸1"}}, open(x.PRIVDNS_MAP_FILE, "w"))
out = run_menu()
check("屏上写替哪几台解析", "替外机解析：1 台（🇺🇸1）" in out)
out = run_menu("3", "")
check("当 DNS 机时关：红字一行说清后果，默认否", "关掉后外机的节点改回真实地址" in out and x.privdns_conf()["mode"] == "self")
REGEN.clear()
out = run_menu("3", "y")
check("确认才关，关了重生成配置", x.privdns_conf()["mode"] == "off" and REGEN)

# ── 14. 卸载 AdGuard 时 ─────────────────────────────────────────────────────────────
x.privdns_save({"mode": "self", "dns": "", "name": ""})
REGEN.clear()
with quiet():
    x.privdns_off()
check("privdns-off：关掉、删映射、重生成配置", x.privdns_conf()["mode"] == "off" and not os.path.exists(x.PRIVDNS_MAP_FILE) and REGEN)
REGEN.clear()
x.privdns_save({"mode": "remote", "dns": DOH, "name": NAME1})
with quiet():
    x.privdns_off()
check("本机不是 DNS 机：什么都不动", x.privdns_conf()["mode"] == "remote" and not REGEN)
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("主脚本认 privdns / privdns-off 两个子命令", 'sys.argv[1] == "privdns"' in src and 'sys.argv[1] == "privdns-off"' in src)

# ── 15. 13 菜单（adguard-dns.py） ─────────────────────────────────────────────────────
a = load("agh", "adguard-dns.py")
a.BGP_DIR = tmp; a.PRIVDNS_FILE = x.PRIVDNS_FILE
RUNS = []
a.subprocess = types.SimpleNamespace(run=lambda cmd, shell=True: RUNS.append(cmd) or types.SimpleNamespace(returncode=0))
a._ask = lambda p="": "0"
for mode, want in (("off", "关"), ("self", "本机"), ("remote", "外机")):
    x.privdns_save({"mode": mode, "dns": DOH, "name": NAME1})
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.menu()
    check(f"13 菜单第 11 项「节点使用私有DNS解析　当前：{want}」", f"11. 节点使用私有DNS解析　当前：{want}" in b.getvalue())
open(f"{tmp}/xy-installer.py", "w").write("# 老版主脚本\n")
b = io.StringIO()
with contextlib.redirect_stdout(b):
    a._privdns_menu(); a._privdns_remove()
check("主脚本是老版本：不叫它（不认的子命令会报错退出），一行提示先更新", not RUNS and "老版本" in b.getvalue())
open(f"{tmp}/xy-installer.py", "w").write('if sys.argv[1] == "privdns": pass\nif sys.argv[1] == "privdns-off": pass\n')
with quiet():
    a._privdns_menu(); a._privdns_remove()
check("新版：11 叫 privdns，卸载叫 privdns-off", RUNS == [f"python3 {tmp}/xy-installer.py privdns", f"python3 {tmp}/xy-installer.py privdns-off"])
asrc = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("卸载 AdGuard：撤订阅那一步顺带关掉私有解析（在删 AdGuard 之前）",
      re.search(r"_privdns_remove\(\)\s+#", asrc).start() < asrc.index('sh(f"{AGH_BIN} -s uninstall")'))

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
