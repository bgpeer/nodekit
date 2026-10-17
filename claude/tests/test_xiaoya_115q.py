#!/usr/bin/env python3
"""小雅接 115 / 夸克，都做成扫码（1.5.371）。仓库主人：「做吧，115 和夸克都做成扫码」。
钉住：
  一、115：扫码确认的令牌换 cookie —— 不用网页那种客户端（OpenList 的 /115 用它，同一种会互相踢）；
      换不成换下一种；建「小雅转存」；ali2115.txt 没建成文件夹就不自动清（别清到 115 根目录）
  二、115 扫码不动「只拿令牌」那一屏记着的那串
  三、夸克：网页版扫码 → service_ticket → cookie；过期说清；没换到 cookie 不算成
  四、登录屏：五项、写进去 0600、cookie 不上屏、在跑就重启
  五、退出登录：登了 115 / 夸克先问退哪个；只退 115 → 只删那两个文件、重启；全部退 → 停下
  六、主屏写清登了哪几个
"""
import contextlib, importlib.util, io, json, os, sys, tempfile, urllib.parse
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
def quiet(fn, *a, **k):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = fn(*a, **k)
    return r, m.ANSI_RE.sub("", b.getvalue())

class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False

def opener_of(fn):
    REQ = []
    def op(req, timeout=None):
        REQ.append((req.full_url, req.data.decode() if req.data else "", dict(req.header_items())))
        r = fn(req.full_url, req.data.decode() if req.data else "")
        if isinstance(r, Exception):
            raise r
        return Resp(json.dumps(r).encode())
    return op, REQ

print("一、115 换 cookie")
def f115(url, data):
    if "/alipaymini/" in url:
        return OSError("下架了")
    return {"state": 1, "data": {"cookie": {"UID": "u1", "CID": "c1", "SEID": "s1", "KID": "k1"}}}
op, REQ = opener_of(f115)
ck, app = m.pan115_cookie("QRUID", opener=op)
check("拿到 UID / CID / SEID / KID 拼成 cookie", ck == "UID=u1; CID=c1; SEID=s1; KID=k1")
check("不用网页那种（OpenList 的 /115 在用）", all("/web/" not in u for u, _d, _h in REQ) and "web" not in m.PAN115_APPS)
check("第一种换不成换下一种", app == "wechatmini" and len(REQ) == 2)
check("带着扫码的令牌", "account=QRUID" in REQ[-1][1])
check("出网是中性 UA", REQ[-1][2].get("User-agent") == m.HTTP_UA)
op, REQ = opener_of(lambda u, d: {"state": 0, "data": {}})
check("都换不成 → 空", m.pan115_cookie("X", opener=op) == ("", ""))
t = m.ali2115_text("UID=u1")
check("ali2115.txt 跟小雅官方脚本一个样（1.5.374）", t == 'cookie="UID=u1"\npurge_pan115_temp=true\ndir_id=auto\n')
check("换 cookie 走 qrcodeapi.115.com（官方那个，1.5.374）", m.PAN115_LOGIN.startswith("https://qrcodeapi.115.com/app/1.0/"))

print("二、115 扫码不动「只拿令牌」那串")
ST = {"qr115_uid": "OLD", "qr115_tm": 1, "qr115_sign": "s", "qr115_at": 5}
m.ms_state = lambda: dict(ST)
def save(**kw):
    ST.update(kw)
m.save_ms_state = save
def fake_new():
    save(qr115_uid="NEW", qr115_at=9)
    return "NEW", 2
m._qr115_new = fake_new
m.pan115_cookie = lambda uid: ("UID=u1; SEID=s1", "alipaymini")
got, out = quiet(m._xiaoya_scan_115)
check("扫成：两个文件", set(got) == {"115_cookie.txt", "ali2115.txt"} and got["115_cookie.txt"] == "UID=u1; SEID=s1")
check("「只拿令牌」那串原样", ST["qr115_uid"] == "OLD" and ST["qr115_at"] == 5)
check("扫码时不摊开令牌（AUTOMOUNT 收回）", m._QR115_AUTOMOUNT[0] is False)
m._qr115_new = lambda: ("", -1)
check("没扫成 → 空", quiet(m._xiaoya_scan_115)[0] == {})

print("三、夸克")
SEQ = []
def fq(url, data):
    if "getTokenForQrcodeLogin" in url:
        return {"status": 2000000, "data": {"members": {"token": "QTOK"}}}
    return SEQ.pop(0)
SEQ[:] = [{"status": 50004001}, {"status": 2000000, "data": {"members": {"service_ticket": "ST1"}}}]
op, REQ = opener_of(fq)
m.time.sleep = lambda s: None
TK = []
m.quark_cookie_from_ticket = lambda st: TK.append(st) or "__pus=P1; __puus=P2"
ck, out = quiet(m.quark_web_scan_login, opener=op)
check("等到确认 → 拿 ticket 换 cookie", ck == "__pus=P1; __puus=P2" and TK == ["ST1"])
check("二维码里是夸克的扫码地址", "token=QTOK" in m.QUARK_QR.format("QTOK") and "su.quark.cn" in m.QUARK_QR)
check("浏览器的脸（网页接口）", REQ[0][2].get("User-agent") == m.BROWSER_UA)
check("cookie 不上屏", "P1" not in out)
SEQ[:] = [{"status": 50004002}]
op, REQ = opener_of(fq)
ck, out = quiet(m.quark_web_scan_login, opener=op)
check("过期说清", ck == "" and "过期" in out)
SEQ[:] = [{"status": 2000000, "data": {"members": {"service_ticket": "ST2"}}}]
m.quark_cookie_from_ticket = lambda st: "__pus=P1"            # 没 __puus 不算（1.5.374）
op, REQ = opener_of(fq)
ck, out = quiet(m.quark_web_scan_login, opener=op)
check("没换到 cookie 不算成", ck == "" and "没换到" in out)

print("四、登录屏")
T = tempfile.mkdtemp(); m.XIAOYA_DIR = os.path.join(T, "xiaoya")
os.makedirs(m.XIAOYA_DIR, mode=0o700)
for f, _n in m.XIAOYA_FILES:
    open(os.path.join(m.XIAOYA_DIR, f), "w").write("x\n")
XS = ["在跑"]; CALLS = []
m.xiaoya_state = lambda: XS[0]
m.sh = lambda cmd, **k: CALLS.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
m._xiaoya_scan_115 = lambda: {"115_cookie.txt": "UID=SECRET115", "ali2115.txt": m.ali2115_text("UID=SECRET115")}
m.quark_web_scan_login = lambda: "__pus=SECRETQ; __puus=Q2"
m.ask = lambda *a, **k: ""
_r, out = quiet(m.xiaoya_set_tokens, "D")
check("五项都在、115 / 夸克当前未登录", "2. 115 扫码（不限速）" in out and "3. 夸克扫码" in out
      and out.count("未登录") == 2 and "5. 手动填令牌" in out)
m.ask = lambda *a, **k: "2"
r, out = quiet(m.xiaoya_set_tokens, "D")
p = os.path.join(m.XIAOYA_DIR, "ali2115.txt")
check("115：两个文件写好、0600", r and os.path.exists(p) and oct(os.stat(p).st_mode & 0o777) == "0o600"
      and open(os.path.join(m.XIAOYA_DIR, "115_cookie.txt")).read() == "UID=SECRET115\n")
check("ali2115.txt 多行原样（结尾一个换行）", open(p).read() == m.ali2115_text("UID=SECRET115"))
check("在跑 → 重启换上", CALLS == ["docker restart xiaoya"])
check("cookie 不上屏", "SECRET" not in out)
m.ask = lambda *a, **k: "3"
CALLS.clear(); quiet(m.xiaoya_set_tokens, "D")
check("夸克：quark_cookie.txt", open(os.path.join(m.XIAOYA_DIR, "quark_cookie.txt")).read() == "__pus=SECRETQ; __puus=Q2\n")

print("六、主屏写清")
check("已登录 · 115 · 夸克", m.xiaoya_login_label() == " · 115 · 夸克")

print("五、退出登录")
ANS = iter(["4", "2"])
m.ask = lambda *a, **k: next(ANS, "")
m.ask_yn = lambda *a, **k: True
CALLS.clear()
r, out = quiet(m.xiaoya_set_tokens, "D")
ex = lambda f: os.path.exists(os.path.join(m.XIAOYA_DIR, f))
check("先问退哪个", "1. 全部退出" in out and "2. 只退 115" in out and "3. 只退 夸克" in out)
check("只退 115：只删那两个文件、阿里和夸克留着", r and not ex("115_cookie.txt") and not ex("ali2115.txt")
      and ex("mytoken.txt") and ex("quark_cookie.txt"))
check("只退 115：重启，不停", CALLS == ["docker restart xiaoya"])
ANS = iter(["4", "1"]); CALLS.clear()
r, out = quiet(m.xiaoya_set_tokens, "D")
check("全部退出：全删、停下", r and not any(ex(f) for f in os.listdir(m.XIAOYA_DIR)) and CALLS == ["docker stop xiaoya"])
check("版本 1.5.371 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 371))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
