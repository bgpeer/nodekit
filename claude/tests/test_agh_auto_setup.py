#!/usr/bin/env python3
"""自建 DNS 装完替你走完网页向导、后台直接随机端口（仓库主人 10/30：「本来希望可安装的时候可以直接把它改好随机的，
  以前叫你改一直改不好所以就一直默认的是3000」→「要是直接能改好随机端口最好」）。
  调向导背后的接口 /control/install/configure（跟浏览器点完向导一样）；10/30 拿真的 AdGuard Home v0.107.79 跑通过。
  这里用一个照它的行为写的假向导：200 后在新端口起服务；密码少于 8 位回 422。
  10/30 又改：用户名默认用自建 DNS 自己的域名（真 AGH 认带点的用户名）；密码明着输、只输一遍（「新手不知道有没有输入成功」）。
"""
import os, io, sys, json, socket, threading, contextlib, http.server, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
GOT, SERVERS = [], []
def serve(port, handler):
    srv = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start(); SERVERS.append(srv)
class Panel(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302); self.send_header("Location", "/login.html"); self.end_headers()
    def log_message(self, *x): pass
class Wizard(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/control/install/get_addresses":
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"web_port":80,"dns_port":53}')
        else:
            self.send_response(302); self.end_headers()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        GOT.append(body)
        if len(body.get("password", "")) < 8:
            self.send_response(422); self.end_headers(); self.wfile.write(b"password must be at least 8 symbols long"); return
        if FAIL_NEW_PORT:
            self.send_response(200); self.end_headers(); return
        serve(body["web"]["port"], Panel)
        self.send_response(200); self.end_headers()
    def log_message(self, *x): pass

WIZ = free()
a.WEB_PORT = WIZ
serve(WIZ, Wizard)
IP = ".".join(["203", "0", "113", "9"])
a._public_ip = lambda: IP
USAGE = []
a._usage = lambda port=None, head=None: USAGE.append(port)
FAIL_NEW_PORT = False
NEWP = [free()]
a._pick_web_port = lambda avoid: NEWP[0]
PROMPTS = []
a._flush_typeahead = lambda: None
CLOCK = [0.0]
a.time.monotonic = lambda: CLOCK[0]
def human(seq):
    """人敲的：每问过 1 秒才回答（不到 0.3 秒的空回车 _ask_fresh 当成凭空多出来的、不算）。"""
    def ask(p=""):
        PROMPTS.append(p); CLOCK[0] += 1.0
        return seq.pop(0) if seq else ""
    return ask
def run(names, pws):
    seq = list(names) + list(pws)
    a._ask = human(seq)
    PROMPTS.clear()
    GOT.clear(); USAGE.clear()
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = a._auto_setup()
    return r, b.getvalue()

OWN = [""]
a._own_domain = lambda: OWN[0]
r, out = run(["myadmin"], ["short", "Passw0rd!x"])
check("走成：返回 True", r is True)
check("少于 8 位当场拦下、不交给它", "至少 8 位" in out and len(GOT) == 1)
check(f"交过去的：随机后台端口、DNS 53、用户名、密码（{ {k: v for k, v in GOT[0].items() if k != 'password'} }）",
      GOT[0]["web"]["port"] == NEWP[0] and GOT[0]["dns"]["port"] == 53 and GOT[0]["username"] == "myadmin"
      and GOT[0]["password"] == "Passw0rd!x")
check("屏上一行 ✔ + 后台地址顶格单独一行", "✔ 后台设好了" in out and "http" + f"://{IP}:{NEWP[0]}" in out.splitlines())
check("随后给使用方法（按新端口）", USAGE == [NEWP[0]])
check("密码明着输、只问一遍（没有「再输一次」的确认问）", not [p for p in PROMPTS if p.strip().startswith("再输一次")]
      and [p for p in PROMPTS if "后台密码" in p] and "不显示" not in "".join(PROMPTS))

NEWP[0] = free()
r, out = run([""], ["Passw0rd!x"])
check("没有自建 DNS 域名：用户名回车 = admin", r and GOT[0]["username"] == "admin")
NEWP[0] = free()
OWN[0] = "dns.example.com"
r, out = run([""], ["Passw0rd!x"])
check("有自建 DNS 域名：用户名回车 = 这个域名（提示里写着）", r and GOT[0]["username"] == "dns.example.com"
      and "回车 = dns.example.com" in PROMPTS[0])
NEWP[0] = free()
r, out = run([""], ["", "Passw0rd!x"])
check("密码空回车不算放弃（装的时候误按的回车）：再问一次", r is True and "没输，再输一次" in out and len(GOT) == 1)
NEWP[0] = free()
r, out = run([""], ["dns.example.com", "Passw0rd!x"])
check("密码跟用户名 / 域名一样：不收、再问一次（域名公网查得到，等于没设）",
      r is True and "别跟用户名 / 域名一样" in out and GOT[-1]["password"] == "Passw0rd!x")
import tempfile
a.AGH_DIR = tempfile.mkdtemp()
def ytls(body):
    open(a.AGH_DIR + "/AdGuardHome.yaml", "w").write("http:\n  address: 0.0.0.0:2646\n" + body + "filters: []\n")
ytls("tls:\n  enabled: false\n  port_https: 443\n")
check("加密还没开：DoH 端口按 10443 教（yaml 里那个 443 是 AdGuard 默认值，10/31 实测会教成 443）", a._doh_port() == 10443)
ytls("tls:\n  enabled: true\n  port_https: 8443\n")
check("加密开着：照 yaml 里的", a._doh_port() == 8443)
r, out = run([""], ["n"])
check("明着输 n：改用网页向导（返回 False、什么都没交）", r is False and not GOT)
# 10/30 GCP 两次实装：用户名一回车，密码那问被凭空多出来的空回车当场顶掉
PH = ["", "", "Passw0rd!x"]
def phantom_then_human(p=""):
    PROMPTS.append(p)
    v = PH.pop(0)
    if len(PH) == 1:          # 第二个空回车是凭空来的：几乎不花时间
        CLOCK[0] += 0.01
    else:
        CLOCK[0] += 1.0
    return v
a._ask = phantom_then_human
PROMPTS.clear()
v1 = a._ask_fresh("用户名: ")
v2 = a._ask_fresh("密码: ")
check(f"不到 0.3 秒回来的空回车不算、再问；人按的回车照常算（{v1!r}, {v2!r}）", v1 == "" and v2 == "Passw0rd!x"
      and PROMPTS == ["用户名: ", "密码: ", "密码: "])
src0 = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
body = src0[src0.index("def _auto_setup("):src0.index("\ndef ", src0.index("def _auto_setup(") + 5)]
check("用户名、密码两问都用 _ask_fresh（问前清缓冲、不认凭空的空回车）", body.count("_ask_fresh(") == 2)
ins = src0[src0.index("def install("):src0.index("\ndef ", src0.index("def install(") + 5)]
check("装好了、向导没走完：再选 1 替你设（不用卸了重装）", "if not os.path.exists(_agh_yaml()):" in ins
      and ins.index("_auto_setup()") < ins.index("已经装过了"))
a._agh_api = (lambda orig: (lambda path, body=None, port=None, timeout=5:
              (500, "boom") if path == "/control/install/configure" else orig(path, body, port, timeout)))(a._agh_api)
r, out = run(["x"], ["Passw0rd!x"])
check("接口报错：说一行、改用网页向导", r is False and "改用网页向导" in out)
src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("装完先自动设置，没成才给网页向导步骤", "if not _auto_setup():" in src and src.index("if not _auto_setup():") < src.index("        _first_setup()"))
for s_ in SERVERS:
    s_.shutdown()
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
