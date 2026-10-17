#!/usr/bin/env python3
"""小雅的令牌一律照小雅官方仓库（xiaoyaDev/xiaoya-alist）的拿法（1.5.374）。
真机 10/10：小雅分享目录一点就 500「failed to refresh token: invalid client_id」—— 开放平台 token 是拿 /aliyun 那套
TV 接口扫的，小雅去自己那家 auth.xiaoya.pro 续期对不上。仓库主人：「你最好是接小雅的原生盘，不要接第三方的」。
钉住：
  一、开放授权：auth.xiaoya.pro 给 sid → 码是阿里 oauth 授权页 → 等 LoginSuccess → 拿 authCode 去 auth.xiaoya.pro 换
      refresh_token；过期说清；换不到不算成
  二、扫码那一整套：第二次扫的是小雅自己那家，不再是 TV 接口；写 opentoken_url.txt；有 open_tv_token_url.txt 挪成 .bak
  三、老机器（有令牌、没 opentoken_url.txt）→ 屏上写「要重扫」；手动填的照官方那家续期
  四、全部退出连 opentoken_url.txt 一起删
  五、夸克要 __puus（官方脚本认它）
"""
import contextlib, importlib.util, io, json, os, sys, tempfile
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

print("一、开放授权走小雅自己那家")
REQ = []; SEQ = []
def op(req, timeout=None):
    body = json.loads(req.data) if req.data else None
    REQ.append((req.full_url, body, dict(req.header_items())))
    u = req.full_url
    if u.endswith("/ali_open/qr"):
        return Resp(json.dumps({"sid": "SID1"}).encode())
    if "/oauth/qrcode/SID1/status" in u:
        return Resp(json.dumps(SEQ.pop(0)).encode())
    if u.endswith("/ali_open/refresh"):
        return Resp(json.dumps({"refresh_token": "OPENRT"}).encode())
    raise OSError(u)
m.time.sleep = lambda s: None
SEQ[:] = [{"status": "WaitLogin"}, {"status": "ScanSuccess"}, {"status": "LoginSuccess", "authCode": "AC1"}]
tok, out = quiet(m.xiaoya_open_scan, opener=op)
check("拿到 refresh_token", tok == "OPENRT")
check("sid 从 auth.xiaoya.pro 要", REQ[0][0] == "http://auth.xiaoya.pro/api/ali_open/qr")
check("码是阿里 oauth 授权页", m.ALI_OAUTH_QR.format("SID1") == "https://www.aliyundrive.com/o/oauth/authorize?sid=SID1")
check("等的是 openapi.aliyundrive.com 的状态", any("openapi.aliyundrive.com/oauth/qrcode/SID1/status" in u for u, _b, _h in REQ))
ex = [b for u, b, _h in REQ if u.endswith("/refresh")][0]
check("authCode 换令牌：跟官方脚本同一包", ex == {"code": "AC1", "grant_type": "authorization_code", "client_id": "", "client_secret": ""})
check("中性 UA", all(h.get("User-agent") == m.HTTP_UA for _u, _b, h in REQ))
check("令牌不上屏", "OPENRT" not in out)
REQ.clear(); SEQ[:] = [{"status": "QRCodeExpired"}]
tok, out = quiet(m.xiaoya_open_scan, opener=op)
check("过期说清", tok == "" and "过期" in out)
def op2(req, timeout=None):
    if req.full_url.endswith("/refresh"):
        return Resp(b'{"code":500}')
    return op(req, timeout)
SEQ[:] = [{"status": "LoginSuccess", "authCode": "AC2"}]
tok, out = quiet(m.xiaoya_open_scan, opener=op2)
check("换不到不算成", tok == "" and "没换到" in out)

print("二、扫码整套")
T = tempfile.mkdtemp(); m.XIAOYA_DIR = os.path.join(T, "xiaoya"); os.makedirs(m.XIAOYA_DIR)
open(os.path.join(m.XIAOYA_DIR, "open_tv_token_url.txt"), "w").write("http://tv\n")
m.aliweb_scan_login = lambda: {"refreshToken": "RT", "accessToken": "AT"}
m.aliweb_temp_folder = lambda login: "F1"
OS = []
m.xiaoya_open_scan = lambda: OS.append(1) or "OPENRT"
m.alitv_scan_login = lambda *a, **k: (_ for _ in ()).throw(AssertionError("还在用 TV 接口"))
m.xiaoya_state = lambda: "未装"
m.ask = lambda *a, **k: "1"
r, out = quiet(m.xiaoya_set_tokens)
rd = lambda f: open(os.path.join(m.XIAOYA_DIR, f)).read().strip()
check("第二次扫的是小雅自己那家", OS == [1] and rd("myopentoken.txt") == "OPENRT")
check("写 opentoken_url.txt 指着小雅那家续期", rd("opentoken_url.txt") == "http://auth.xiaoya.pro/api/ali_open/refresh")
check("TV 那条路的 open_tv_token_url.txt 挪成 .bak", not os.path.exists(os.path.join(m.XIAOYA_DIR, "open_tv_token_url.txt"))
      and os.path.exists(os.path.join(m.XIAOYA_DIR, "open_tv_token_url.txt.bak")))
check("0600", oct(os.stat(os.path.join(m.XIAOYA_DIR, "opentoken_url.txt")).st_mode & 0o777) == "0o600")
check("现在不算要重扫", m.xiaoya_open_stale() is False)

print("三、老机器 / 手动填")
os.remove(os.path.join(m.XIAOYA_DIR, "opentoken_url.txt"))
check("有令牌、没 opentoken_url.txt → 要重扫", m.xiaoya_open_stale() is True)
m.ask = lambda *a, **k: ""
_r, out = quiet(m.xiaoya_set_tokens, "D")
check("登录屏写「要重扫」", "1. 阿里扫码（必需）" in out and "要重扫" in out)
m.xiaoya_mounted = lambda d: False
m.xiaoya_default_whole = lambda d: None
m.drive_channel = lambda *a: ("原画直链", False)
for k in ("strm_cron_desc", "title_policy_of", "rename_policy_of"):
    setattr(m, k, lambda mp: "")
m.skip_dirs_of = lambda mp: []
m.ms_state = lambda: {}
m.ask = lambda *a, **k: "0"
_r, out = quiet(m._xiaoya_menu, "D")
check("小雅主屏也写清要重扫", "要重扫阿里" in out)
vals = iter(["", "MANUALOPEN", ""])
m._ask_secret = lambda p: next(vals)
m.ask = lambda *a, **k: "5"
quiet(m.xiaoya_set_tokens)
check("手动填开放 token：照小雅那家续期", rd("myopentoken.txt") == "MANUALOPEN"
      and rd("opentoken_url.txt") == "http://auth.xiaoya.pro/api/ali_open/refresh")

print("四、全部退出")
m.ask = lambda *a, **k: "4"
m.ask_yn = lambda *a, **k: True
m.sh = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
quiet(m.xiaoya_set_tokens)
check("opentoken_url.txt 一起删", not os.path.exists(os.path.join(m.XIAOYA_DIR, "opentoken_url.txt")))

print("五、夸克 __puus")
src = open(SRC, encoding="utf-8").read()
q = src[src.index("def quark_web_scan_login"):]; q = q[:q.index("\ndef ", 1)]
check("没 __puus 不算成", '"__puus" not in cookie' in q)
qc = src[src.index("def quark_cookie_from_ticket"):]; qc = qc[:qc.index("\ndef ", 1)]
check("带着 __pus 去 drive-pc 的 config 拿 __puus（夸克 PC 的脸）", "QUARK_CONFIG" in qc and "QUARK_PC_UA" in qc)
check("版本 1.5.374 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 374))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
