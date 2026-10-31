#!/usr/bin/env python3
"""自建 DNS 菜单 10「更换域名证书」（仓库主人 10/31：「自建DNS里面也做一个更换域名证书的口子吧，避免以后要用，
  这样他们就互相不干预了，改动有点大做好一点哦」）。
  · 签新域名 + *.新域名（只签不装，签不下来什么都没动）→ 装到自己的路径、核名字 → 改 AdGuardHome.yaml 加密那段
    （服务器名、证书路径；贴在配置里的证书正文清空）→ 重启、等 DoT 端口起来 → 起不来整体放回去
  · 老装法（跟节点共用域名 / 证书）从这里分出来：节点证书、节点的 acme 记录一概不碰
  · 有自己域名的：成了才撤旧域名那张的续期
  · 订阅里写着自建 DNS（写入订阅 / 当 DNS 机）→ 叫主脚本 selfdns-refresh；当 DNS 机时红字一行说清外机要重新粘贴"""
import os, io, re, sys, types, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
T = tempfile.mkdtemp()
os.environ["HOME"] = T
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)

a.BGP_DIR = T + "/bgp"; os.makedirs(a.BGP_DIR)
a.AGH_DIR = T + "/agh"; os.makedirs(a.AGH_DIR)
a.AGH_CERT_DIR = T + "/ssl-agh"
a.AGH_CRT, a.AGH_KEY = a.AGH_CERT_DIR + "/agh.crt", a.AGH_CERT_DIR + "/agh.key"
a.AGH_DOMAIN_FILE = a.BGP_DIR + "/agh.domain"
a.SELFDNS_FLAG = a.BGP_DIR + "/selfdns.on"
a.SELFDNS_CID_FILE = a.BGP_DIR + "/selfdns.clientid"; open(a.SELFDNS_CID_FILE, "w").write("xyabc123")
a.HOST_FILE = a.BGP_DIR + "/sub.host"; open(a.HOST_FILE, "w").write("node.zone.example")
ACME = T + "/.acme.sh"; os.makedirs(ACME)
open(ACME + "/acme.sh", "w").write("#!/bin/sh\n")
open(ACME + "/account.conf", "w").write("SAVED_CF_Token='tok'\n")
IP = "198.51.100" + ".7"
a._installed = lambda: True
a._node_domain = lambda: "node.zone.example"
a._public_ip = lambda: IP
a._resolve4 = lambda n: [IP]
a._wildcard_dns_check = lambda d: (True, "")
a.time.sleep = lambda s: None
UP = [True]
a._running = lambda: UP[0]
a._port_busy = lambda p: "AdGuardHome" if UP[0] else ""
a._cert_names = lambda p: ([open(p).read().split(":", 1)[1], "*." + open(p).read().split(":", 1)[1]]
                           if os.path.exists(p) else [])
SH, RUN = [], []
def fake_sh(cmd, check=False):
    SH.append(cmd)
    m = re.search(r"--install-cert -d (\S+) --ecc --fullchain-file (\S+) --key-file (\S+)", cmd)
    if m:
        open(m.group(2), "w").write("CERT:" + m.group(1)); open(m.group(3), "w").write("KEY")
    return ""
a.sh = fake_sh
ISSUE_OK = [True]
def fake_run(cmd, **k):
    RUN.append(cmd)
    return types.SimpleNamespace(returncode=0 if ISSUE_OK[0] else 1,
                                 stdout="Cert success." if ISSUE_OK[0] else "error", stderr="")
a.subprocess.run = fake_run

YAML = ("http:\n  address: 0.0.0.0:2646\nusers:\n  - name: u\n    password: $2y$10$x\n"
        "tls:\n  enabled: true\n  server_name: node.zone.example\n  force_https: false\n  port_https: 10443\n"
        "  port_dns_over_tls: 853\n  certificate_chain: \"\"\n  private_key: \"\"\n"
        "  certificate_path: /etc/ssl/sb/acme.crt\n  private_key_path: /etc/ssl/sb/acme.key\n"
        "user_rules:\n  - '! xy-privdns begin'\n")
Y = a.AGH_DIR + "/AdGuardHome.yaml"
def reset(own=None, yaml=YAML, cert=None):
    open(Y, "w").write(yaml)
    for p in (a.AGH_DOMAIN_FILE, a.AGH_CRT, a.AGH_KEY, a.SELFDNS_FLAG):
        if os.path.exists(p): os.remove(p)
    if own:
        open(a.AGH_DOMAIN_FILE, "w").write(own + "\n")
        os.makedirs(a.AGH_CERT_DIR, exist_ok=True)
        open(a.AGH_CRT, "w").write("CERT:" + own); open(a.AGH_KEY, "w").write("OLDKEY")
    SH.clear(); RUN.clear(); UP[0] = True; ISSUE_OK[0] = True
def go(answers, mode="off"):
    q = list(answers)
    a._ask = lambda p="": q.pop(0) if q else ""
    a._privdns_mode = lambda: mode
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.change_domain()
    return plain(b.getvalue())

print("老装法（跟节点共用）分出来")
reset()
out = go(["", "y"])
y = open(Y).read()
check("回车默认 dns.根域名；签 dns + *.dns（DNS 验证）", any("-d dns.zone.example -d '*.dns.zone.example'" in c for c in RUN))
check("yaml：服务器名、证书路径换成自己的，别的一个字节不动",
      "  server_name: dns.zone.example\n" in y and f"  certificate_path: {a.AGH_CRT}\n" in y
      and f"  private_key_path: {a.AGH_KEY}\n" in y and "  port_https: 10443\n" in y and "password: $2y$10$x" in y
      and "'! xy-privdns begin'" in y)
check("记下自己的域名", open(a.AGH_DOMAIN_FILE).read().strip() == "dns.zone.example")
check("节点证书、节点的 acme 记录一概不碰", not any("node.zone.example" in c and "--remove" in c for c in SH)
      and not any("/etc/ssl/sb" in c for c in SH))
check("重启了 AdGuard、屏上 ✔ + 新的主机名 / DoH 地址顶格一行",
      "systemctl restart AdGuardHome" in SH and "✔ 自建 DNS 换成 dns.zone.example" in out
      and "\nxyabc123.dns.zone.example\n" in out and "\nhttps://dns.zone.example:10443/dns-query/xyabc123\n" in out)
check("老装法：提醒节点域名的记录还在用、别删", "节点域名的记录还在用，别删" in out)

print("有自己域名的再换")
reset(own="dns.zone.example")
os.makedirs(ACME + "/dns.zone.example_ecc", exist_ok=True)
out = go(["dns2.zone.example", "y"])
check("成了才撤旧域名那张的续期（记录目录也删掉）",
      any("--remove -d dns.zone.example --ecc" in c for c in SH) and not os.path.isdir(ACME + "/dns.zone.example_ecc"))
check("新证书装上、yaml 指过去", open(a.AGH_CRT).read() == "CERT:dns2.zone.example" and "server_name: dns2.zone.example" in open(Y).read())

print("起不来：整体放回去")
reset(own="dns.zone.example")
UP[0] = False
out = go(["dns3.zone.example", "y"])
check("yaml、域名记号、证书文件全回原样", open(Y).read() == YAML and open(a.AGH_DOMAIN_FILE).read().strip() == "dns.zone.example"
      and open(a.AGH_CRT).read() == "CERT:dns.zone.example" and open(a.AGH_KEY).read() == "OLDKEY")
check("新签的那张撤掉续期、旧的没撤；屏上一行说放回去了",
      any("--remove -d dns3.zone.example --ecc" in c for c in SH) and not any("--remove -d dns.zone.example " in c for c in SH)
      and "已全部放回原来的域名和证书" in out)
reset()
UP[0] = False
out = go(["", "y"])
check("老装法起不来：自己那两个证书文件删掉、没留下域名记号", not os.path.exists(a.AGH_CRT)
      and not os.path.exists(a.AGH_DOMAIN_FILE) and open(Y).read() == YAML)

print("签不下来 / 不该换的")
reset(); ISSUE_OK[0] = False
out = go(["", "y"])
check("证书没签下来：什么都没改", open(Y).read() == YAML and not os.path.exists(a.AGH_DOMAIN_FILE) and "什么都没改" in out)
reset(); ISSUE_OK[0] = False
_real_run = a.subprocess.run
def leave_dir(cmd, **k):                     # acme.sh 签失败也会建记录目录（10/31 DMIT 真机）
    os.makedirs(ACME + "/dns.zone.example_ecc", exist_ok=True)
    return _real_run(cmd, **k)
a.subprocess.run = leave_dir
go(["", "y"])
a.subprocess.run = _real_run
check("签失败留下的记录目录清掉（不然改好令牌回来再换，被「已经有一张证书在用」拦住）",
      not os.path.isdir(ACME + "/dns.zone.example_ecc") and any("--remove -d dns.zone.example --ecc" in c for c in SH))
reset(); out = go(["", "y"])
check("清掉以后再试：能换", "✔ 自建 DNS 换成 dns.zone.example" in out)
reset(own="dns.zone.example"); y0 = open(Y).read(); out = go(["node.zone.example", "y"])
check("跟节点同一个域名：不签、不改", not RUN and open(Y).read() == y0 and "已经有一张证书在用" in out)
reset(); os.makedirs(ACME + "/*.media.zone.example_ecc", exist_ok=True)
out = go(["media.zone.example", "y"])
check("别人（Emby）的 acme 记录同名：不签、不改", not RUN and "已经有一张证书在用" in out)
reset(own="dns.zone.example"); out = go(["dns.zone.example"])
check("跟现在一样：不改", not RUN and "跟现在一样" in out)
reset(); a._resolve4 = lambda n: ["203.0.113" + ".9"]
out = go(["", "y"])
check("新域名没解析到本机：不签、不改", not RUN and "不是本机" in out)
a._resolve4 = lambda n: [IP]
reset(); out = go(["", ""])
check("确认那问回车（默认否）：没改", not RUN and open(Y).read() == YAML and "已取消" in out)

print("订阅 / 外机")
reset()
open(a.SELFDNS_FLAG, "w").write("1")
open(a.BGP_DIR + "/xy-installer.py", "w").write('if x == "selfdns-refresh": pass\n')
out = go(["", "y"], mode="self")
check("写进订阅了：叫主脚本 selfdns-refresh 重生成", any("selfdns-refresh" in c for c in RUN))
check("当 DNS 机：确认前红字一行说外机要重新粘贴；换完给出那一行",
      "外机要到「13 → 4 → 2」重新粘贴本机新的 DNS 地址" in out
      and "外机「13 → 4 → 2」重新粘贴这一行\nhttps://dns.zone.example:10443/dns-query/xyabc123" in out)
reset()
open(a.SELFDNS_FLAG, "w").write("1")
open(a.BGP_DIR + "/xy-installer.py", "w").write("old\n")
out = go(["", "y"])
check("主脚本是老版本：不叫不认识的子命令，一行说今晚自动刷新", not any("selfdns-refresh" in c for c in RUN)
      and "今晚 03:20 自动刷新" in out)

print("yaml 改写")
t = a._agh_tls_retarget(YAML.replace('certificate_chain: ""', "certificate_chain: '-----BEGIN X'"), "d.zone2.example")
check("贴在配置里的证书正文清空（AdGuard 不认路径和正文同时有）、private_key_path 不被 private_key 那条误改",
      'certificate_chain: ""' in t and '  private_key: ""\n' in t and f"private_key_path: {a.AGH_KEY}" in t)
check("没有 tls 段：原样返回", a._agh_tls_retarget("http:\n  x: 1\n", "d.zone2.example") == "http:\n  x: 1\n")
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
