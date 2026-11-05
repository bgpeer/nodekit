#!/usr/bin/env python3
"""自建 DNS 签证书失败要清干净（仓库主人 11/05：「这个DNS没有建成功不管是证书还是令牌全部给我删干净不要残留你这不是害人吗」）。
  以前签失败：acme.sh 把这次贴的令牌存进 account.conf，下回不再问、一直拿错的签；提示又说「别新建顶掉」，人卡死。
  现在：失败 → 这次的证书记录删掉、account.conf 放回签之前；存着的令牌管不到这个域名 → 当场问换不换
  （本机没别的证书靠它就默认换）；没证书靠它的旧令牌签失败后也一起删。
"""
import os, io, re, sys, tempfile, contextlib, importlib.util
HOME = tempfile.mkdtemp(); os.environ["HOME"] = HOME
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
AC = os.path.join(HOME, ".acme.sh"); os.makedirs(AC, exist_ok=True)
CONF = os.path.join(AC, "account.conf")
DOM = "ddns.example.org"
a.sh = lambda c, check=False: ""
RES = []                                   # 每次签的结果：(ok, out, 是否把令牌写进 conf)
CALLS = []
def fake_attempt(acme, dom, env):
    CALLS.append(env)
    ok, out = RES.pop(0)
    os.makedirs(os.path.join(AC, dom + "_ecc"), exist_ok=True)      # acme.sh 失败也会留下记录目录
    m = re.search(r"CF_Token='?([^' ]+)", env)
    if m:                                                           # acme.sh 会把这次的令牌存进去
        txt = open(CONF).read() if os.path.exists(CONF) else ""
        txt = re.sub(r"(?m)^SAVED_CF_Token=.*\n?", "", txt) + f"SAVED_CF_Token='{m.group(1)}'\n"
        open(CONF, "w").write(txt)
    return ok, out
a._issue_attempt = fake_attempt
a._cf_token_steps = lambda d: None
def run(ans, env=""):
    q = list(ans); CALLS.clear()
    a._ask = lambda p="": (print(p) or q.pop(0)) if q else ""
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = a._issue_own_cert("acme", DOM, env)
    return r, re.sub(r"\033\[[0-9;]*m", "", b.getvalue())
def reset(conf_txt=None):
    import shutil
    shutil.rmtree(AC); os.makedirs(AC)
    if conf_txt is not None: open(CONF, "w").write(conf_txt)

print("一、第一次填令牌就签失败")
reset("ACCOUNT_EMAIL='x'\n")
RES[:] = [(False, "invalid domain\nError adding TXT record")]
r, out = run([], env="CF_Token='badtok' ")
check("失败返回 False", r is False)
check("这次填的令牌没留在 account.conf", "SAVED_CF_Token" not in open(CONF).read() and "ACCOUNT_EMAIL" in open(CONF).read())
check("证书记录目录删了", not os.path.exists(os.path.join(AC, DOM + "_ecc")))
check("屏上一行 ✔ 已清干净", "✔ 已清干净：这次的证书记录和这次填的令牌都没留下" in out)

print("二、存着的令牌管不到，本机没别的证书靠它：默认换，换了签成")
reset("SAVED_CF_Token='old'\n")
RES[:] = [(False, "invalid domain"), (True, "Cert success")]
r, out = run(["", "newtok"])
check("提示令牌管不到、默认换（回车 = 是）", "管不到" in out and "[Y/n]" in out)
check("拿新令牌重签成功", r is True and len(CALLS) == 2 and "newtok" in CALLS[1])
check("新令牌留下（续期要用）", "SAVED_CF_Token='newtok'" in open(CONF).read())

print("三、换了还是不行：都撤掉，旧的那个没人用也删")
reset("SAVED_CF_Token='old'\n")
RES[:] = [(False, "invalid domain"), (False, "invalid domain")]
r, out = run(["", "newtok2"])
check("没留下任何令牌", "SAVED_CF_Token" not in open(CONF).read())
check("记录目录删了", not os.path.exists(os.path.join(AC, DOM + "_ecc")))

print("四、本机有别的证书靠这个令牌续期：醒目说清、默认不换、旧令牌留着")
reset("SAVED_CF_Token='old'\n")
nd = os.path.join(AC, "node.example.org_ecc"); os.makedirs(nd)
crt = os.path.join(HOME, "node.crt"); open(crt, "w").write("x")
open(os.path.join(nd, "node.example.org.conf"), "w").write(f"Le_Webroot='dns_cf'\nLe_RealFullChainPath='{crt}'\n")
RES[:] = [(False, "invalid domain")]
r, out = run([""])
check("列出靠它续期的证书、默认否", "node.example.org" in out and "[y/N]" in out)
check("回车不换：只签了一次", len(CALLS) == 1)
check("旧令牌留着（别的证书续期要用）", "SAVED_CF_Token='old'" in open(CONF).read())
check("这次的记录照样删", not os.path.exists(os.path.join(AC, DOM + "_ecc")))

print("五、Let's Encrypt 限次数（429）")
reset("SAVED_CF_Token='old'\n")
os.makedirs(os.path.join(AC, "x"), exist_ok=True)
RES[:] = [(False, '{\n  "type": "urn:ietf:params:acme:error:rateLimited",\n  "status": 429\n}')]
r, out = run([])
check("429：说清是限次数、等 1 小时或换名字；不问换令牌", "限次数" in out and "换个名字" in out and "[Y/n]" not in out)
check("429：令牌留着（令牌没毛病）", "SAVED_CF_Token='old'" in open(CONF).read())

print("六、两处都走同一个签发")
src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("安装和换域名都用 _issue_own_cert", src.count("_issue_own_cert(acme, dom, env)") == 2)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
