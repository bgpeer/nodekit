#!/usr/bin/env python3
"""「7 设置 → 13 OpenList 账号」：管理员的用户名 / 密码能自己定（仓库主人 11/12：「三个用户名和密码可不可以自行设置」
  「OpenList用户名不可以修改吗？……能修改最好也可以做成修改」）。

  以前用户名写死 admin（脚本里 8 处登录、7 个排查脚本都直接写着）、密码装的时候随机生成。想换只能去 OpenList 网页后台，
  可脚本自己存着一份、AutoFilm / MediaWarp 的配置里也各有一份，后台一改全对不上。
钉住：
  一、用户名从 .secrets 的 OPENLIST_USER 读，没有 = admin；脚本和排查脚本里不再有写死的 admin 登录
  二、写进 YAML 的账号密码：老装机（admin + 随机字母数字）一个字不变；带符号 / 纯数字 / true 这类加引号
  三、改用户名：OpenList 收下才记账；它不收、登不上都什么都不改；sso_id 原样带回
  四、改密码：走 admin set；它没收下原密码不动；后台把用户名改过的顺带对上；核对最多错 3 次（5 次会被 OpenList 锁 5 分钟）
  五、菜单：第 13 项；格式不对不收；AutoFilm 在扫不改；有人在播先问；确认默认否；改完两份配置 + CREDENTIALS 跟着换、各重启一次
  六、重装沿用改过的用户名（OpenList 的库不在了就回到 admin）；使用信息显示真的用户名
假 OpenList 是照它的源码写的（server/handles/auth.go：登录按 IP 数失败、me/update 只动用户名和非空的密码）—— 会话容器里起不了真的。"""
import importlib.util, os, sys, io, re, json, stat, tempfile, threading, contextlib, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()

# ---------------------------------------------------------------- 假 OpenList
OL = {"users": {"admin": {"pw": "Old0ldPassw0rd16", "sso_id": "", "role": 2}, "guest": {"pw": "", "sso_id": "", "role": 1}},
      "fails": 0, "max_fails": 0, "tok": {}, "n": 0, "reject_update": False, "calls": []}
class FakeOL(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, data=None, msg="success"):
        b = json.dumps({"code": code, "message": msg, "data": data}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _user(self):
        name = OL["tok"].get(self.headers.get("Authorization") or "")
        return name if name in OL["users"] else None
    def do_GET(self):
        OL["calls"].append(self.path)
        u = self._user()
        if self.path == "/api/me":
            return self._send(200, {"username": u, "sso_id": OL["users"][u]["sso_id"], "role": 2}) if u else self._send(401, msg="x")
        self._send(404, msg="not found")
    def do_POST(self):
        OL["calls"].append(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/auth/login":
            if OL["fails"] >= 5:
                return self._send(429, msg="Too many unsuccessful sign-in attempts")
            u = OL["users"].get(body.get("username"))
            if not u or u["pw"] != body.get("password"):
                OL["fails"] += 1; OL["max_fails"] = max(OL["max_fails"], OL["fails"])
                return self._send(401, msg="Invalid username or password")
            OL["fails"] = 0; OL["n"] += 1
            t = f"tok{OL['n']}"; OL["tok"][t] = body["username"]
            return self._send(200, {"token": t})
        if self.path == "/api/me/update":
            u = self._user()
            if not u:
                return self._send(401, msg="x")
            new = body.get("username")
            if OL["reject_update"] or (new != u and new in OL["users"]):
                return self._send(500, msg="UNIQUE constraint failed: x_users.username")
            rec = OL["users"].pop(u); rec["sso_id"] = body.get("sso_id", "")
            if body.get("password"):
                rec["pw"] = body["password"]
            OL["users"][new] = rec
            return self._send(200)
        self._send(404, msg="not found")
srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOL)
threading.Thread(target=srv.serve_forever, daemon=True).start()
m.OPENLIST_PORT = srv.server_address[1]
def admin_name():
    return next(n for n, u in OL["users"].items() if u["role"] == 2)

# docker exec openlist ./openlist admin set <pw>：照 cmd/admin.go —— 改库里的密码、印出真正的用户名；失败也回 0
CLI = {"rc": 0, "apply": True, "calls": []}
def fake_run(cmd, **kw):
    if isinstance(cmd, list) and cmd[:6] == ["docker", "exec", "openlist", "./openlist", "admin", "set"]:
        CLI["calls"].append(cmd[6])
        if CLI["rc"]:
            return types.SimpleNamespace(returncode=CLI["rc"], stdout="", stderr="Error: No such container: openlist")
        if not CLI["apply"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="ERRO failed update admin user: database is locked")
        OL["users"][admin_name()]["pw"] = cmd[6]
        return types.SimpleNamespace(returncode=0, stdout=f"admin user has been updated:\nusername: {admin_name()}\npassword: {cmd[6]}\n", stderr="")
    raise AssertionError(f"没料到的命令 {cmd}")
m.subprocess = types.SimpleNamespace(run=fake_run)
SH = []
m.sh = lambda cmd, check=False, timeout=None: (SH.append(cmd), types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1]
m.time = types.SimpleNamespace(sleep=lambda s: None, time=m.time.time, monotonic=m.time.monotonic,
                               strftime=m.time.strftime, gmtime=m.time.gmtime, localtime=m.time.localtime)

T = tempfile.mkdtemp(); D = T + "/ms"
for sub in ("mediawarp/config", "autofilm/config"):
    os.makedirs(f"{D}/{sub}")
open(f"{D}/docker-compose.yml", "w").write("services: {}\n")
open(f"{D}/.env", "w").write(f"DOMAIN=\nDATA_ROOT={D}/media\n")
SEC = f"{D}/.secrets"
open(SEC, "w").write("OPENLIST_PASS=Old0ldPassw0rd16\nBA_USER=media\nBA_PASS=x\n")
m.ms_install_dir = lambda: D
m.public_ip = lambda: "127.0.0.1"
MW, AF, CRED = f"{D}/mediawarp/config/config.yaml", f"{D}/autofilm/config/config.yaml", f"{D}/CREDENTIALS.txt"
cfg0 = m.rebuild_cfg_from_disk(D)
open(MW, "w").write(m.gen_mediawarp_conf(cfg0)); open(AF, "w").write(m.gen_autofilm_conf(cfg0)); open(CRED, "w").write("old\n")
BEFORE = (open(MW).read(), open(AF).read())
sec = lambda k: m.read_env(SEC, k)
creds_in = lambda p: [l.strip() for l in open(p).read().splitlines() if re.match(r"\s*(username|password):", l)]

print("一、用户名从 .secrets 读，没有 = admin；没有写死的 admin 登录")
check("没改过：ol_user = admin", m.ol_user(D) == "admin")
check("脚本里 8 处登录都用 ol_user(d)，不再写死", '"username": "admin"' not in SRC
      and SRC.count('{"username": ol_user(d), "password": pw}') == 8)
check("从磁盘重建的 cfg 用的是存下来的用户名", '"ol_user":  ol_user(d),' in SRC)
tools = {f: open(os.path.join(ROOT, "tools", f), encoding="utf-8").read() for f in os.listdir(os.path.join(ROOT, "tools")) if f.endswith(".sh")}
check("排查脚本（tools/）登录也不写死 admin：读 .secrets 的 OPENLIST_USER，没有才用 admin",
      not [f for f, t in tools.items() if '"username": "admin"' in t]
      and len([f for f, t in tools.items() if "auth/login" in t and "OPENLIST_USER" in t]) == 7)

print("二、写进 YAML 的账号密码")
y = m._yaml_scalar
check("admin、随机生成的字母数字密码：照原样、不加引号", y("admin") == "admin" and y("Old0ldPassw0rd16") == "Old0ldPassw0rd16"
      and y("9Zk3abc") == "9Zk3abc")
check("老装机的两份配置一个字都不变（「8 更新」不会多重启一次 MediaWarp）",
      creds_in(MW) == ["username: admin", "password: Old0ldPassw0rd16"] == creds_in(AF))
check("带符号的加单引号", y("my.name-1") == "'my.name-1'" and y("p@ss#w0rd!") == "'p@ss#w0rd!'")
check("会被 YAML 读成别的东西的也加：纯数字、1e5、0x1f、true / null / yes", all(y(v) == f"'{v}'" for v in
      ("12345678", "1e5", "0x1f", "true", "NULL", "yes", "off")))
check("单引号本身写成两个", y("it's") == "'it''s'")

print("三、改用户名")
OL["users"]["admin"]["sso_id"] = "sso-abc"
check("改成功：返回空", m.ol_rename_admin(D, "boss") == "")
check("OpenList 里管理员叫新名字了，密码没动、sso_id 原样带回",
      admin_name() == "boss" and OL["users"]["boss"]["pw"] == "Old0ldPassw0rd16" and OL["users"]["boss"]["sso_id"] == "sso-abc")
check(".secrets 记下 OPENLIST_USER，之后脚本用新名字登得上", sec("OPENLIST_USER") == "boss" and m.ol_user(D) == "boss" and bool(m._ol_token(D)))
check(".secrets 还是 0600、别的键没丢", stat.S_IMODE(os.stat(SEC).st_mode) == 0o600 and sec("BA_PASS") == "x" and sec("OPENLIST_PASS") == "Old0ldPassw0rd16")
OL["reject_update"] = True
why = m.ol_rename_admin(D, "other")
check(f"OpenList 不收（{why[:30]}…）：说原因、什么都没改", bool(why) and admin_name() == "boss" and sec("OPENLIST_USER") == "boss")
OL["reject_update"] = False
why = m.ol_rename_admin(D, "guest")
check("跟已有账号重名：OpenList 回的原话带出来，没改", "UNIQUE" in why and admin_name() == "boss" and sec("OPENLIST_USER") == "boss")
m.write_secret(SEC, "OPENLIST_PASS", "wrong-on-purpose")
why = m.ol_rename_admin(D, "other")
check("登不上：说原因、指路去重设密码，没改", "登不上" in why and "密码" in why and admin_name() == "boss")
m.write_secret(SEC, "OPENLIST_PASS", "Old0ldPassw0rd16"); OL["fails"] = 0

print("四、改密码")
CLI["calls"].clear(); OL["max_fails"] = 0
check("改成功：返回空", m.ol_set_admin_password(D, "NewPass-2026!") == "")
check("走的是 docker exec openlist ./openlist admin set", CLI["calls"] == ["NewPass-2026!"])
check("OpenList 收下了、.secrets 记下了、登得上", OL["users"]["boss"]["pw"] == "NewPass-2026!" and sec("OPENLIST_PASS") == "NewPass-2026!" and bool(m._ol_token(D)))
CLI["rc"] = 1
why = m.ol_set_admin_password(D, "Another-Pass1")
check("容器没在跑：说原因，什么都没改", bool(why) and sec("OPENLIST_PASS") == "NewPass-2026!")
CLI["rc"], CLI["apply"] = 0, False
OL["fails"] = 0; OL["max_fails"] = 0
why = m.ol_set_admin_password(D, "Another-Pass1")
check("命令回 0 但 OpenList 没收下（它失败也回 0）：原密码没动、.secrets 没动", "原密码没动" in why and sec("OPENLIST_PASS") == "NewPass-2026!")
check(f"核对时登录最多错 3 次（这次 {OL['max_fails']} 次）—— 5 次会被 OpenList 锁 5 分钟", 0 < OL["max_fails"] <= 3)
check("之后照样登得上", bool(m._ol_token(D)))
CLI["apply"] = True
rec = OL["users"].pop("boss"); OL["users"]["WebRenamed"] = rec          # 有人在网页后台把用户名改了
check("后台改过用户名：脚本登不上了", not m._ol_token(D)); OL["fails"] = 0
check("在这里重设一次密码：连用户名一起对上", m.ol_set_admin_password(D, "Fix1t-Again") == ""
      and sec("OPENLIST_USER") == "WebRenamed" and sec("OPENLIST_PASS") == "Fix1t-Again" and bool(m._ol_token(D)))

print("五、菜单")
i = SRC.index("def settings_menu") if "def settings_menu" in SRC else SRC.index("13. OpenList 账号")
check("7 设置里有第 13 项「OpenList 账号　当前：用户名」，选 13 进去", '  13. OpenList 账号    当前：' in SRC
      and re.search(r'elif c == "13":\n\s+ol_account_menu\(\)', SRC) is not None)
check("前面 12 项一个没少", all(f"  {n}. " in SRC for n in range(1, 13)) and all(f'c == "{n}"' in SRC for n in range(1, 13)))
BUSY, PLAYING, YN, ANS = [False], [False], [], []
restarts = lambda: sorted(c for c in SH if re.search(r"docker (restart|stop|start|rm|compose)", c))
m.autofilm_busy = lambda: BUSY[0]
m.read_emby_api_key = lambda d=None: "k"
m.someone_playing = lambda k, cache_s=0: PLAYING[0]
YES = [True]
m.ask_yn = lambda q, default=True: (YN.append((q, default)), YES[0])[1]
m.ask = lambda prompt, default="": ANS.pop(0) if ANS else "0"
def run(*answers, yes=True, busy=False, playing=False):
    ANS[:] = list(answers); YN.clear(); SH.clear(); YES[0], BUSY[0], PLAYING[0] = yes, busy, playing
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.ol_account_menu()
    return plain(buf.getvalue())
out = run()
check("屏：标题一行、两项各一行带当前值、0 返回、一行提示", "  OpenList 账号" in out and "  1. 用户名            当前：WebRenamed" in out
      and "  2. 密码              当前：Fix1t-Again" in out and "  0. 返回" in out and out.count("提示：") == 1)
for badname in ("guest", "1abc", "有中文", "a b", "x" * 33):
    out = run("1", badname, "")
    check(f"用户名「{badname[:8]}」不收，没改", "没改：" in out and admin_name() == "WebRenamed" and not YN)
CLI["calls"].clear()
for badpw in ("short1", "-LeadingDash1", "has space12", "quote'12345", "dollar$12345", "密码密码密码密码"):
    out = run("2", badpw, "")
    check(f"密码「{badpw}」不收，没改", "没改：" in out and sec("OPENLIST_PASS") == "Fix1t-Again" and not YN and not CLI["calls"])
out = run("1", "", "")
check("回车 = 不改", "没改。" in out and not YN)
out = run("1", "mine", "", busy=True)
check("AutoFilm 正在扫：不改（它手里的令牌会作废），提示等扫完", "等它扫完再改" in out and admin_name() == "WebRenamed" and not YN and not restarts())
out = run("1", "mine", "", yes=False, playing=True)
check("有人在看片：说会断、先问，默认否；答否就不改", "正在播的会断" in out and YN == [("还是现在就改？", False)] and admin_name() == "WebRenamed" and not restarts())
out = run("1", "mine", "", yes=False)
check("没人在播：说要重启什么、确认默认否；答否就不改", "重启 MediaWarp 和 AutoFilm" in out and YN == [("确认修改？", False)]
      and admin_name() == "WebRenamed" and not restarts())
out = run("1", "mine", "")
check("确认了：用户名改掉", "✔ 用户名改成「mine」" in out and admin_name() == "mine" and sec("OPENLIST_USER") == "mine")
check("AutoFilm / MediaWarp 配置里的用户名跟着换", creds_in(MW) == ["username: mine", "password: 'Fix1t-Again'"] == creds_in(AF))
check("两个容器各重启一次，别的容器不碰", restarts() == ["docker restart autofilm", "docker restart mediawarp"])
check("两份配置还是 0600", all(stat.S_IMODE(os.stat(p).st_mode) == 0o600 for p in (MW, AF)))
check("CREDENTIALS.txt 跟着换（0600）", "mine / Fix1t-Again" in open(CRED).read() and stat.S_IMODE(os.stat(CRED).st_mode) == 0o600)
out = run("2", "Chosen_Pass.9", "")
check("密码改成自己定的：屏上新密码顶格单独一行", "✔ 密码已改" in out and "\nChosen_Pass.9\n" in out and OL["users"]["mine"]["pw"] == "Chosen_Pass.9"
      and sec("OPENLIST_PASS") == "Chosen_Pass.9" and YN == [("确认修改？", False)])
check("配置、CREDENTIALS 里的密码跟着换，两个容器各重启一次", creds_in(MW)[1] == "password: 'Chosen_Pass.9'" == creds_in(AF)[1]
      and "mine / Chosen_Pass.9" in open(CRED).read() and restarts() == ["docker restart autofilm", "docker restart mediawarp"])
out = run("2", "r", "")
new = sec("OPENLIST_PASS")
check("输 r：随机 16 位字母数字，配置里照老样子不加引号", re.fullmatch(r"[A-Za-z0-9]{16}", new) is not None and OL["users"]["mine"]["pw"] == new
      and creds_in(MW)[1] == f"password: {new}")
out = run("2", new, "")
check("跟现在一样：不折腾", "跟现在一样" in out and not YN and not restarts())
CLI["apply"] = False
out = run("2", "Wont_Stick_1", "")
check("OpenList 没收下：屏上说没改成，配置不动、不重启", "没改成" in out and sec("OPENLIST_PASS") == new and not restarts())
CLI["apply"] = True
out = run("9")
check("乱按：无效选择，不退出", "无效选择" in out)
fn = SRC[SRC.index("def _ol_account_apply"):SRC.index("def ol_account_menu")]
check("先生成再写（生成抛异常时原配置不动）、原子写 0600", fn.index("text = gen(cfg)") < fn.index("write_atomic(path, text, 0o600)"))

print("六、重装 / 使用信息")
ins = SRC[SRC.index('secret_file = os.path.join(cfg["install_dir"], ".secrets")'):]
ins = ins[:ins.index("# ---- nginx / 证书 ----")]
check("重装沿用改过的用户名；OpenList 的库不在了（它会重新建 admin）就回到 admin",
      'read_env(secret_file, "OPENLIST_USER")' in ins and "data.db" in ins and 'or "admin"' in ins)
check("重装整份重写 .secrets 时带上 OPENLIST_USER（不然一重装就丢）", '''f"OPENLIST_USER={cfg['ol_user']}\\n"''' in SRC)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    try:
        m.show_info()
    except Exception as e:                     # 后半段要问 docker，这里只看账号那几行
        print("EXC", type(e).__name__)
check("「2 使用信息」显示真的用户名，不再写死 admin", f"      用户名   mine\n      密  码   {new}" in plain(buf.getvalue()))

print("七、版本号")
check("media-stack 1.5.401 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 401))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
