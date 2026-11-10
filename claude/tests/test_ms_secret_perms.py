#!/usr/bin/env python3
"""media-stack：带密码 / Key 的文件只给 root 读；另外三处小毛病（11/10 仓库主人：「再检查一遍 media-stack 有没有 bug」）。

  一、「8 更新」把 mediawarp / autofilm 的 config.yaml 改回 0644。里面是 Emby API Key 和 OpenList 管理员密码，
      安装时是 0600；do_update 重写配置用了 write_atomic 的默认 0644，换上去的新文件人人可读（每月自动更新也走这条）。
      安装目录是 0755，nginx 的 www-data 这类非 root 进程都读得到（CLAUDE.md 第一节）。
      现在写这些文件一律 0600；老装机由 harden_secret_files 收回来（「8 更新」、每天 04:10 自更新各过一遍，只 chmod）。
  二、.secrets / CREDENTIALS.txt / 填 API Key 那几处是 open(path, "w")：先清空再写，写到一半被打断就是空文件；
      新建时还先落成 0644 再 chmod。改成 write_atomic(…, 0o600)。
  三、挂载点名字里带英文双引号：nginx 站点配置那一行引号对不上，nginx -t 过不了。
  四、手动「8 更新」换脚本前没有先 compile（每天 04:10 那条有）。
"""
import os, io, re, ast, sys, stat, shutil, tempfile, contextlib, subprocess, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)
def quiet(f, *a, **k):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = f(*a, **k)
    return r, b.getvalue()
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)
tmp = tempfile.mkdtemp()

print("一、老装机收回来：harden_secret_files")
d = f"{tmp}/inst"
for rel in m.SECRET_FILES:
    os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
    open(os.path.join(d, rel), "w").write("x\n"); os.chmod(os.path.join(d, rel), 0o644)
os.makedirs(f"{d}/homepage/config"); open(f"{d}/homepage/config/services.yaml", "w").write("x\n")
open(f"{d}/docker-compose.yml", "w").write("x\n")
check("清单里有这六个：两份 config.yaml、.secrets、.env、CREDENTIALS.txt、观看进度表",
      set(m.SECRET_FILES) == {"mediawarp/config/config.yaml", "autofilm/config/config.yaml", ".secrets", ".env",
                              "CREDENTIALS.txt", m.PROGRESS_MAP})
n = m.harden_secret_files(d)
check("0644 的六个全收成 0600", n == 6 and all(mode(os.path.join(d, r)) == 0o600 for r in m.SECRET_FILES))
check("内容一个字不动（只 chmod）", all(open(os.path.join(d, r)).read() == "x\n" for r in m.SECRET_FILES))
check("不带密码的不碰（homepage 配置、docker-compose.yml）", mode(f"{d}/homepage/config/services.yaml") == 0o644
      and mode(f"{d}/docker-compose.yml") == 0o644)
check("已经是 0600 的不再动；文件不在也不报错", m.harden_secret_files(d) == 0 and m.harden_secret_files(f"{tmp}/nope") == 0)
os.chmod(f"{d}/.secrets", 0o640)
check("组可读（0640）也算松，收回 0600", m.harden_secret_files(d) == 1 and mode(f"{d}/.secrets") == 0o600)

print("二、写的时候就是 0600")
def calls_in(fn_name):
    f = next(n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    return f, [c for c in ast.walk(f) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "write_atomic"]
def is600(c):
    v = c.args[2] if len(c.args) > 2 else next((k.value for k in c.keywords if k.arg == "mode"), None)
    return isinstance(v, ast.Constant) and v.value == 0o600
f, cs = calls_in("do_update")
cfg_writes = [c for c in cs if ast.get_source_segment(SRC, c.args[0]) in ("path", "cred")]
check("do_update 重写 mediawarp / autofilm 配置、凭据总览：都带 0o600（以前配置那条是默认的 0644）",
      len(cfg_writes) == 2 and all(is600(c) for c in cfg_writes))
check("do_update 开头先把老的收一遍", "harden_secret_files(d)" in ast.get_source_segment(SRC, f))
f, _ = calls_in("do_selfupdate")
check("每天 04:10 自更新也收一遍（不上终端的机器两天内收回来），而且排在等没人播之前、只 chmod",
      "harden_secret_files(d)" in ast.get_source_segment(SRC, f)
      and ast.get_source_segment(SRC, f).index("harden_secret_files(d)") < ast.get_source_segment(SRC, f).index("play_quiet_wait("))
loose = []
for c in (c for c in ast.walk(TREE) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "write_atomic" and c.args):
    seg = ast.get_source_segment(SRC, c.args[0])
    if re.search(r'config\.yaml|secret|env_file|cred|PROGRESS_MAP|_mw\b|\baf\b|cfg_path', seg) and not is600(c):
        loose.append(f"{c.lineno}: {seg[:50]}")
check("全文件里写 config.yaml / .secrets / .env / 凭据 / 观看进度的 write_atomic 都带 0o600" + (f"：漏了 {loose}" if loose else ""), not loose)
raw = [n.lineno for n in ast.walk(TREE) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open" and len(n.args) > 1
       and isinstance(n.args[1], ast.Constant) and n.args[1].value == "w"
       and re.search(r'config\.yaml|secret|cred|\.env', ast.get_source_segment(SRC, n.args[0]) or "")]
check("这几类文件不再有 open(…, \"w\") 直接写的" + (f"：还有 {raw}" if raw else ""), not raw)

print("三、.secrets / 凭据总览：原子写")
sec = f"{tmp}/.secrets"
m.write_secret(sec, "OPENLIST_PASS", "p1")
check("新建就是 0600（以前先落成 0644 再 chmod）", mode(sec) == 0o600 and open(sec).read() == "OPENLIST_PASS=p1\n")
m.write_secret(sec, "EMBY_API_KEY", "k1"); m.write_secret(sec, "OPENLIST_PASS", "p2")
check("改一个键、加一个键，别的键不丢", open(sec).read() == "OPENLIST_PASS=p2\nEMBY_API_KEY=k1\n" and mode(sec) == 0o600)
real_replace = m.os.replace
def boom(a, b): raise OSError("disk full")
m.os.replace = boom
try:
    m.write_secret(sec, "OPENLIST_PASS", "p3"); raised = False
except OSError:
    raised = True
m.os.replace = real_replace
check("写到一半出事：原来那份一个字不少（以前是先清空再写，这时已经是空文件）、临时文件不留",
      raised and open(sec).read() == "OPENLIST_PASS=p2\nEMBY_API_KEY=k1\n" and os.listdir(tmp).count(".secrets") == 1
      and not [n for n in os.listdir(tmp) if n.startswith(".secrets.tmp")])
cred = f"{d}/CREDENTIALS.txt"
open(cred, "w").write("  用户名    old\n  密  码    oldpw\n  别的行\n"); os.chmod(cred, 0o644)
m.patch_credentials_file(d, "newuser", "newpw")
check("改网页账号密码同步凭据总览：两行换掉、别的行不动、0600", "newuser" in open(cred).read() and "newpw" in open(cred).read()
      and "别的行" in open(cred).read() and "oldpw" not in open(cred).read() and mode(cred) == 0o600)

print("四、挂载点名字里带英文双引号")
cfg = dict(domain="ys.example", has_domain=True, install_dir=d, data_root=d + "/media", tz="Asia/Shanghai", puid="0", pgid="0",
           homepage=True, scan_spec="auto", scan_paths=["/quark"], strm_cron="0 20 4 * * *", emby_api_key="k" * 32, ol_user="admin",
           ol_pass="p", ba_user="media", ba_pass="pw", basic_auth=True, host_ip="1.2.3.4", ngx_port=8443, http2_directive=False,
           crt=f"{tmp}/c.pem", key=f"{tmp}/k.pem")
weird = ["/quark", '/我的 盘(1).{2}$"x;', "/a'b\\c}", '/"']
m.ua_spoof_mounts = lambda: weird
m.hls_wanted = lambda d: True; m.hls_port = lambda: 18765; m.hls_direct_on = lambda: False
m.heal_gate_ready = lambda: True
m.ali_tc_mounts = lambda: weird; m.pan115_mounts = lambda d: weird
site = m.gen_nginx_site(cfg)
lines = [l for l in site.splitlines() if l.lstrip().startswith('"~^/[dp]/')]
check("每个挂载点一行，引号成对：行里除了头尾两个，别的英文双引号前面都有反斜杠",
      len(lines) == 4 and all(re.fullmatch(r'\s+"(?:[^"\\]|\\.)*" 1;', l) for l in lines))
check("别的元字符照旧转义（. ( ) { } $ 空格），普通名字一个字不变", '    "~^/[dp]/quark(/|$)" 1;' in lines
      and any(r'\(1\)\.\{2\}\$\"x;' in l for l in lines))
if shutil.which("nginx"):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", f"{tmp}/k.pem",
                    "-out", f"{tmp}/c.pem", "-subj", "/CN=ys.example", "-days", "2"], check=True, capture_output=True)
    open(f"{tmp}/site.conf", "w").write(site)
    open(f"{tmp}/ng.conf", "w").write(
        f"user root; worker_processes 1; pid {tmp}/ng.pid; error_log stderr;\nevents {{ worker_connections 64; }}\n"
        "http { include /etc/nginx/mime.types; access_log off;\n"
        + "".join(f"  {k}_temp_path {tmp}/t{k};\n" for k in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
        + f"  include {tmp}/site.conf;\n}}\n")
    r = subprocess.run(["nginx", "-t", "-c", f"{tmp}/ng.conf"], capture_output=True, text=True)
    check("真 nginx -t：全功能开着（转码流、开播门、UA 改写）+ 这几个怪名字的站点配置过得了"
          + ("" if r.returncode == 0 else f"：{(r.stderr or '').strip().splitlines()[-2:]}"), r.returncode == 0)
else:
    print("  - 这台没装 nginx，跳过 nginx -t")

print("五、手动「8 更新」：换脚本前先 compile")
me = f"{tmp}/media-stack.py"
m.__file__ = me
def body(ver, extra=""):
    return f'SCRIPT_VERSION = "{ver}"\n' + "# pad\n" * 3000 + extra
open(me, "w").write(body(m.SCRIPT_VERSION))
m.fetch_self_latest = lambda timeout=20: (body("9.9.9", "def broken(:\n"), "9.9.9", "仓库")
r, out = quiet(m.self_update)
check("拉到的新版语法不对（没下全）：不换、返回 False、屏上一行说清", r is False and "SCRIPT_VERSION = \"9.9.9\"" not in open(me).read()
      and "语法不对" in out and "没有替换" in out and not os.path.exists(me + ".new"))
m.fetch_self_latest = lambda timeout=20: (body("9.9.9"), "9.9.9", "仓库")
r, out = quiet(m.self_update)
check("正常的新版：照旧换上、返回 True", r is True and 'SCRIPT_VERSION = "9.9.9"' in open(me).read() and "脚本已更新" in out)

print("六、版本号")
check("media-stack 1.5.397 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 397))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
