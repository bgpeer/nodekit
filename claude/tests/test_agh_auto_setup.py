#!/usr/bin/env python3
"""自建 DNS 装完替你走完网页向导、后台直接随机端口（仓库主人 10/30：「本来希望可安装的时候可以直接把它改好随机的，
  以前叫你改一直改不好所以就一直默认的是3000」→「要是直接能改好随机端口最好」）。
  调向导背后的接口 /control/install/configure（跟浏览器点完向导一样）；10/30 拿真的 AdGuard Home v0.107.79 跑通过。
  这里用一个照它的行为写的假向导：200 后在新端口起服务；密码少于 8 位回 422。
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
def run(names, pws):
    names, pws = list(names), list(pws)
    a._ask = lambda p="": names.pop(0) if names else ""
    a.getpass.getpass = lambda p="": pws.pop(0) if pws else ""
    GOT.clear(); USAGE.clear()
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = a._auto_setup()
    return r, b.getvalue()

r, out = run(["myadmin"], ["short", "Passw0rd!x", "Passw0rd!x"])
check("走成：返回 True", r is True)
check("少于 8 位当场拦下、不交给它", "至少 8 位" in out and len(GOT) == 1)
check(f"交过去的：随机后台端口、DNS 53、用户名、密码（{ {k: v for k, v in GOT[0].items() if k != 'password'} }）",
      GOT[0]["web"]["port"] == NEWP[0] and GOT[0]["dns"]["port"] == 53 and GOT[0]["username"] == "myadmin"
      and GOT[0]["password"] == "Passw0rd!x")
check("屏上一行 ✔ + 后台地址顶格单独一行", "✔ 后台设好了" in out and "http" + f"://{IP}:{NEWP[0]}" in out.splitlines())
check("随后给使用方法（按新端口）", USAGE == [NEWP[0]])
check("密码不上屏", "Passw0rd!x" not in out)

NEWP[0] = free()
r, out = run([""], ["Passw0rd!x", "different1"] + ["Passw0rd!x", "Passw0rd!x"])
check("用户名回车 = admin；两次不一样重输", r and GOT[0]["username"] == "admin" and "两次不一样" in out)
r, out = run([""], [""])
check("密码直接回车：改用网页向导（返回 False、什么都没交）", r is False and not GOT)
a.getpass.getpass = lambda p="": "Passw0rd!x"
a._agh_api = (lambda orig: (lambda path, body=None, port=None, timeout=5:
              (500, "boom") if path == "/control/install/configure" else orig(path, body, port, timeout)))(a._agh_api)
r, out = run(["x"], ["Passw0rd!x", "Passw0rd!x"])
check("接口报错：说一行、改用网页向导", r is False and "改用网页向导" in out)
src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("装完先自动设置，没成才给网页向导步骤", "if not _auto_setup():" in src and src.index("if not _auto_setup():") < src.index("        _first_setup()"))
for s_ in SERVERS:
    s_.shutdown()
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
