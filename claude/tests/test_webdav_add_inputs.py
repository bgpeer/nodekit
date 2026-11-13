#!/usr/bin/env python3
"""添加 WebDAV：密码照常显示、端口单独问一次（仓库主人 11/13：「这里不要搞隐藏的输没输进去都不知道，应该还要显示一次
  输入端口吧」）。

  原来密码不回显（怕截图）—— 手机上敲完一点反应都没有，不知道敲没敲上；端口要自己塞进地址中间（主机名后面加冒号和端口）。
钉住：
  一、密码用普通输入（看得见），不再用不回显的那种
  二、地址里没带端口：问一次端口，回车 = 默认（https 443 / http 80）不改地址；填了就补进地址
  三、地址里已经带端口：不问
  四、端口乱填：拦住
  五、贴令牌那两处还是不回显，但贴完说一声收到了多少个字符"""
import importlib.util, os, sys, io, re, contextlib, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
H, HS = "http" + "://", "https" + "://"
f = m.dav_addr_with_port

print("一、端口补进地址")
check("https 地址 + 端口 5244", f(HS + "dav.example/dav", "5244") == HS + "dav.example:5244/dav")
check("没有路径的地址也行", f(H + "dav.example", "8080") == H + "dav.example:8080")
check("回车（空）= 不改地址", f(HS + "dav.example/dav", "") == HS + "dav.example/dav")
check("地址里本来就带端口：不动", f(HS + "dav.example:9443/dav", "5244") == HS + "dav.example:9443/dav")
UP = "u:p" + "@"                             # 拼出来：测试文件里不留邮箱样子的串（test_claude_folder）
check("地址里带账号（账号:密码@主机）也分得清哪个是端口", f(HS + UP + "dav.example/dav", "5244") == HS + UP + "dav.example:5244/dav")
check("端口乱填：返回 None", f(HS + "dav.example", "abc") is None and f(HS + "dav.example", "0") is None and f(HS + "dav.example", "70000") is None)

print("二、添加流程里问什么")
ASKED, ANS, BODY = [], [], []
m.ask = lambda prompt, default="": (ASKED.append(prompt), ANS.pop(0) if ANS else "")[1]
m._ask_secret = lambda prompt: (_ for _ in ()).throw(AssertionError("密码不该再用不回显的输入"))
m.openlist_storages = lambda d: []
m._ol_token = lambda d: "tok"
m._ol_storage_body = lambda mount, drv, add, proxy=False: {"mount": mount, "add": add}
m._ol_api = lambda path, body, token=None, timeout=60, method="POST", ua=None: (BODY.append(body), {"code": 200})[1]
def run(*answers):
    ASKED.clear(); BODY.clear(); ANS[:] = list(answers)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        r = m._add_webdav_flow("/d")
    return r, plain(buf.getvalue())
r, out = run(HS + "dav.example/dav", "5244", "me", "My Pass 1")
check("依次问：地址 → 端口 → 用户名 → 密码", [a.split("（")[0] for a in ASKED[:4]] == ["WebDAV 地址", "端口", "用户名", "密码"])
check("端口那一问写明回车用默认 443", "回车＝默认 443" in ASKED[1])
check("密码走普通输入（看得见）", r is True and BODY[0]["add"]["password"] == "My Pass 1")
check("填的端口进了地址", BODY[0]["add"]["address"] == HS + "dav.example:5244/dav")
r, out = run(H + "dav.example/dav", "", "me", "pw")
check("http 地址：默认端口写 80；回车不改地址", "回车＝默认 80" in ASKED[1] and BODY[0]["add"]["address"] == H + "dav.example/dav")
r, out = run(HS + "dav.example:9443/dav", "me", "pw")
check("地址里已经带端口：不问端口", not any(a.startswith("端口") for a in ASKED) and BODY[0]["add"]["address"] == HS + "dav.example:9443/dav")
r, out = run(HS + "dav.example/dav", "abc")
check("端口乱填：拦住，什么都没建", r is False and "1–65535" in out and not BODY)

print("三、贴令牌：不回显，但说一声收到了")
fn = SRC[SRC.index("def _ask_secret"):SRC.index("def dav_addr_with_port")]
check("贴完印「已收到 N 个字符」", "已收到 {len(v.strip())} 个字符" in fn and "getpass.getpass" in fn)
check("阿里贴刷新令牌的两处还用它", SRC.count('_ask_secret("把刷新令牌粘在这里（留空取消）")') == 2)
check("media-stack 1.5.422 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 422))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
