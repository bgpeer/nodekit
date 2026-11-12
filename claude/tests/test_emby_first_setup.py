#!/usr/bin/env python3
"""装的时候直接建 Emby 管理员账号、自动生成 API 密钥（仓库主人 11/12：「现在emby在第一次安装的时候跑代码就可以直接设定
  用户名和密码吗，不需要在网页去设置吗」「做，而且你说的这个能直接生成API密钥填上，这是一个非常有用的」）。

  以前：装完打开 Emby 网页走首次向导建账号 → 设置 → 高级 → API 密钥 → 新建 → 复制 → 回来填到「7 设置 → 1」。
  1.5.405：安装问答里定 Emby 用户名密码，容器起来后脚本走完向导、登录、建密钥、写进 MediaWarp。
钉住：
  一、走向导的顺序和内容（照 Emby 开源版 StartupWizardService）：语言地区 → 账号 → 允许远程访问（UPnP 不开）→ 完成
  二、向导收密码的版本、不收密码的版本（开源版只认名字）都能登上、密码都对
  三、建密钥：管理员令牌 POST /Auth/Keys?App=MediaWarp，再按 App 名取回；登录的痕迹（会话、设备）清掉
  四、各种不成：Emby 没起来 / 早就设置过 / 密码不对 / 不是管理员 —— 说清原因，不瞎写
  五、「7 设置 → 1」输 a 自动生成；手动贴的老路还在
  六、安装：新装问 Emby 用户名密码（回车 = 不建，走老路）；重跑沿用已有的 Key；密码不进 CREDENTIALS.txt
假 Emby 是照它最后一版开源代码写的 —— 会话容器里起不了真的，新版向导的字段没法确认，所以两种都测。"""
import importlib.util, os, sys, io, re, json, tempfile, threading, contextlib, types, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()

E = {}
def reset(wizard_takes_pw=True, done=False, up=True):
    E.clear()
    E.update(up=up, done=done, takes_pw=wizard_takes_pw, calls=[], cfg={}, remote={}, keys=[], toks={}, devices=set(), n=0,
             users={"u0": {"Name": "MyEmbyServer", "pw": "", "admin": True}})
class FakeEmby(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, data=None):
        b = json.dumps(data).encode() if data is not None else b""
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _go(self, method):
        if not E["up"]:
            return self._send(503)
        u = urllib.parse.urlsplit(self.path); path, q = u.path, urllib.parse.parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        E["calls"].append((method, path, body, dict(q)))
        tok = self.headers.get("X-Emby-Token") or ""
        who = E["users"].get(E["toks"].get(tok, ""))
        if path == "/System/Info/Public":
            return self._send(200, {"StartupWizardCompleted": E["done"]})
        if path.startswith("/Startup/"):
            if E["done"]:
                return self._send(401)                     # 向导完成后这几条要管理员
            if path == "/Startup/Configuration" and method == "POST":
                E["cfg"] = body
            elif path == "/Startup/User" and method == "POST":
                E["users"]["u0"]["Name"] = body.get("Name")
                if E["takes_pw"]:
                    E["users"]["u0"]["pw"] = body.get("Password") or ""
            elif path == "/Startup/RemoteAccess":
                E["remote"] = body
            elif path == "/Startup/Complete":
                E["done"] = True
            return self._send(200, {"Name": E["users"]["u0"]["Name"]} if method == "GET" else None)
        if path == "/Users/AuthenticateByName":
            auth = self.headers.get("X-Emby-Authorization") or ""
            dev = re.search(r'DeviceId="([^"]+)"', auth)
            if not dev:
                return self._send(400)
            for uid, usr in E["users"].items():
                if usr["Name"] == body.get("Username") and usr["pw"] == body.get("Pw"):
                    E["n"] += 1; t = f"tok{E['n']}"; E["toks"][t] = uid; E["devices"].add(dev.group(1))
                    return self._send(200, {"AccessToken": t, "User": {"Id": uid}})
            return self._send(401)
        if not who:
            return self._send(401)
        mm = re.match(r"/Users/(\w+)/Password$", path)
        if mm and method == "POST":
            usr = E["users"][mm.group(1)]
            if body.get("CurrentPw") != usr["pw"]:
                return self._send(403)
            usr["pw"] = body.get("NewPw"); return self._send(204)
        if path == "/Auth/Keys":
            if not who["admin"]:
                return self._send(403)
            if method == "POST":
                E["keys"].append({"AccessToken": "%032x" % (len(E["keys"]) + 0xabc), "AppName": q.get("App", [""])[0],
                                  "DateCreated": "2026-01-0%dT00:00:00Z" % (len(E["keys"]) + 1)})
                return self._send(204)
            return self._send(200, {"Items": E["keys"]})
        if path == "/Sessions/Logout":
            E["toks"].pop(tok, None); return self._send(204)
        if path == "/Devices" and method == "DELETE":
            E["devices"].discard(q.get("Id", [""])[0]); return self._send(204)
        self._send(404)
    def do_GET(self): self._go("GET")
    def do_POST(self): self._go("POST")
    def do_DELETE(self): self._go("DELETE")
srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeEmby)
threading.Thread(target=srv.serve_forever, daemon=True).start()
m.EMBY_LOCAL = f"http://127.0.0.1:{srv.server_address[1]}"
m.time = types.SimpleNamespace(sleep=lambda s: None, time=m.time.time, monotonic=m.time.monotonic,
                               strftime=m.time.strftime, gmtime=m.time.gmtime, localtime=m.time.localtime)
posts = lambda: [c[1] for c in E["calls"] if c[0] == "POST" and c[1].startswith("/Startup/")]

print("一、走向导")
reset()
key, why = m.emby_first_setup("boss", "S3cret-pw")
check("返回一把 32 位的密钥、没有报错", re.fullmatch(r"[0-9a-f]{32}", key) is not None and why == "")
check("顺序：语言地区 → 账号 → 远程访问 → 完成", posts() == ["/Startup/Configuration", "/Startup/User", "/Startup/RemoteAccess", "/Startup/Complete"])
check("每一步先读后写（网页向导也是这样；有的版本靠这一读才把第一个账号建出来）",
      [c[:2] for c in E["calls"] if c[1] in ("/Startup/Configuration", "/Startup/User")][:4]
      == [("GET", "/Startup/Configuration"), ("POST", "/Startup/Configuration"), ("GET", "/Startup/User"), ("POST", "/Startup/User")])
check("语言地区：中文 / 中国", E["cfg"] == {"UICulture": "zh-CN", "MetadataCountryCode": "CN", "PreferredMetadataLanguage": "zh"})
check("允许远程访问开着（播放器从外面进来）、UPnP 不开", E["remote"] == {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
check("向导完成了，账号名是定的那个", E["done"] and E["users"]["u0"]["Name"] == "boss")

print("二、密码两头都兜")
check("向导收密码的版本：密码对", E["users"]["u0"]["pw"] == "S3cret-pw")
reset(wizard_takes_pw=False)
key, why = m.emby_first_setup("boss", "S3cret-pw")
check("向导不收密码的版本（开源版只认名字）：照样拿到密钥", bool(key) and why == "")
check("账号没有留成空密码：完成后当场设上了", E["users"]["u0"]["pw"] == "S3cret-pw"
      and any(c[1] == "/Users/u0/Password" and c[2] == {"Id": "u0", "CurrentPw": "", "NewPw": "S3cret-pw"} for c in E["calls"]))

print("三、建密钥")
mk = [c for c in E["calls"] if c[1] == "/Auth/Keys"]
check("POST /Auth/Keys?App=MediaWarp，再 GET 取回", [c[0] for c in mk] == ["POST", "GET"] and mk[0][3].get("App") == ["MediaWarp"])
check("拿回来的就是刚建的那一把", key == E["keys"][-1]["AccessToken"] and E["keys"][-1]["AppName"] == "MediaWarp")
check("登录的痕迹清掉：令牌退出了、设备列表里不留", not E["toks"] and not E["devices"])
check("登录头里的名字是中性的，不报脚本 / 仓库的名字", not re.search(r"(?i)media-stack|nodekit|bgpeer|xy-", m._EMBY_SETUP_AUTH + m.EMBY_KEY_APP))
E["keys"].insert(0, {"AccessToken": "f" * 32, "AppName": "别的应用", "DateCreated": "2025-01-01T00:00:00Z"})
E["keys"].insert(0, {"AccessToken": "e" * 32, "AppName": "MediaWarp", "DateCreated": "2025-01-01T00:00:00Z"})
k2, _ = m.emby_make_api_key("boss", "S3cret-pw")
check("已经有同名的旧钥匙 / 别的应用的钥匙：取回的是最新建的那一把", k2 == E["keys"][-1]["AccessToken"] and k2 not in ("e" * 32, "f" * 32))

print("四、不成的时候")
reset(up=False)
key, why = m.emby_first_setup("boss", "pw", wait_s=0.2)
check(f"Emby 没起来：{why}", key == "" and "还没起来" in why)
reset(done=True); E["users"]["u0"].update(Name="old", pw="oldpw")
key, why = m.emby_first_setup("boss", "pw")
check(f"早就设置过、给的账号又登不上：不动向导，说清（{why}）", key == "" and "早就设置过" in why and not posts() and E["users"]["u0"]["Name"] == "old")
key, why = m.emby_first_setup("old", "oldpw")
check("早就设置过、给的正好是管理员账号：直接建密钥", bool(key) and why == "" and not posts())
E["users"]["u1"] = {"Name": "kid", "pw": "k", "admin": False}
key, why = m.emby_make_api_key("kid", "k")
check(f"不是管理员：{why}", key == "" and "不是管理员" in why and not E["toks"])
key, why = m.emby_make_api_key("old", "wrong")
check(f"密码不对：{why}", key == "" and "登不上" in why)

print("五、「7 设置 → 1」输 a 自动生成")
T = tempfile.mkdtemp(); D = T + "/ms"; os.makedirs(D + "/mediawarp/config")
open(f"{D}/docker-compose.yml", "w").write("services: {}\n")
MW = f"{D}/mediawarp/config/config.yaml"
open(MW, "w").write("emby:\n  auth:      # Emby API Key，留空则 MediaWarp 无法工作\n")
m.ms_install_dir = lambda: D
RUN = []
m.subprocess = types.SimpleNamespace(run=lambda cmd, **k: (RUN.append(cmd), types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1])
ANS = []
m.ask = lambda prompt, default="": ANS.pop(0) if ANS else ""
m.ask_yn = lambda q, default=True: True
def run_key(*answers):
    ANS[:] = list(answers); RUN.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        m.set_emby_api_key()
    return plain(buf.getvalue())
reset()
out = run_key("a", "boss", "")
check("Emby 还没设置过：问用户名密码，密码留空随机生成、顶格单独一行只显示一次",
      "Emby 账号「boss」已建好" in out and re.search(r"\n[A-Za-z0-9]{16}\n", out) is not None and E["done"])
check("密钥写进 MediaWarp 配置（行尾注释还在）、.secrets 存一份、MediaWarp 重启",
      re.search(r"auth: [0-9a-f]{32}\s+# Emby API Key", open(MW).read()) is not None
      and m.read_env(f"{D}/.secrets", "EMBY_API_KEY") == E["keys"][-1]["AccessToken"] and ["docker", "restart", "mediawarp"] in RUN)
open(MW, "w").write("emby:\n  auth:      # x\n"); os.remove(f"{D}/.secrets")
out = run_key("a", "boss", "wrong-pw", "")
check("已经设置过、密码填错：说没生成出来，回到那一问，什么都没写", "没生成出来" in out and "auth:      # x" in open(MW).read() and not RUN)
out = run_key("a", "boss", E["users"]["u0"]["pw"])
check("已经设置过、填对管理员账号密码：建密钥填上；账号密码不落盘", re.search(r"auth: [0-9a-f]{32}", open(MW).read()) is not None
      and E["users"]["u0"]["pw"] not in open(f"{D}/.secrets").read())
open(MW, "w").write("emby:\n  auth:      # x\n")
out = run_key("0123456789abcdef0123456789abcdef")
check("手动贴密钥的老路还在", "auth: 0123456789abcdef0123456789abcdef" in open(MW).read())
out = run_key("")
check("留空 = 取消", "已取消" in out)
check("屏上一行提示「输 a 自动生成」", 'tip("不想自己去拿：输 a 自动生成")' in SRC)

print("六、安装")
ins = SRC[SRC.index("    # Emby 账号 + API Key（见 emby_first_setup）"):]
ins = ins[:ins.index("    env_file = os.path.join")]
check("新装（没有 Key）：问 Emby 用户名、密码（留空随机）；回车 = 不建，走老路",
      'ask("Emby 用户名（回车＝不建，装完自己去网页设）", "")' in ins and 'ask("Emby 密码（留空则随机生成）", "")' in ins and "rand_pw(16)" in ins)
check("重跑（已有 Key）：默认沿用，不再把它问丢", '_k_old = read_emby_api_key(cfg["install_dir"])' in ins
      and 'ask("Emby API Key（回车＝沿用现在的）", _k_old)' in ins)
i_up, i_setup, i_sum = SRC.index("docker compose --env-file {env_file} up -d"), SRC.index('emby_first_setup(cfg["emby_user"], cfg["emby_pass"])'), SRC.index('cred_file = os.path.join(cfg["install_dir"], "CREDENTIALS.txt")\n    write_atomic(cred_file, build_summary')
check("容器起来之后、写总览之前去建账号和密钥；建成了写进 MediaWarp", i_up < i_setup < i_sum and 'apply_emby_api_key(cfg["install_dir"], _key)' in SRC)
check("没建成：屏上说原因 + 指路「7 设置 → 1 输 a」，安装继续", 'warn(f"Emby 没自动设好：{_why}")' in SRC and "输 a 自动生成密钥" in SRC)
cfg = {"install_dir": D, "data_root": D + "/media", "domain": "", "has_domain": False, "homepage": False, "basic_auth": False, "host_ip": "127.0.0.1",
       "ol_user": "admin", "ol_pass": "OLPASS", "ba_user": "media", "ba_pass": "", "emby_user": "boss", "emby_pass": "TOPSECRET", "emby_ready": True}
try:
    summ = m.build_summary(cfg, colored=False)
except KeyError as e:
    summ = f"KeyError {e}"
check("总览里 Emby 那一行写用户名；密码不进 CREDENTIALS.txt（以后在 7 → 12 改了这里不会跟着变）", "boss / 装的时候定的" in summ and "TOPSECRET" not in summ)
cfg["emby_ready"] = False
check("没自动建的：还是「首登自设」", "首登自设" in m.build_summary(cfg, colored=False))
check("随机生成的密码装完在屏上顶格单独一行显示一次", 'print(cfg["emby_pass"])' in SRC and "只显示这一次" in SRC)
check("收尾那几步：密钥就绪就不再叫人去网页建", "Emby 账号和 API 密钥已经就绪" in SRC)

print("七、版本号")
check("media-stack 1.5.405 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 405))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
