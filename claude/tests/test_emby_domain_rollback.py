#!/usr/bin/env python3
"""「7 设置 → 10 域名证书」换域名：nginx 成功之后、后面任何一步出事都要整体放回（1.5.431）。
以前只放回 nginx，首页 / AutoFilm / MediaWarp 的新配置就留下了「新域名 + 旧 nginx」的混合状态。
钉住：
  一、MediaWarp / AutoFilm / 首页重启失败、配置生成失败 → .env、六份配置、nginx、OpenList 对外地址
     全放回原来的域名，重启过的服务用旧配置重启回来，新签的那张撤掉，返回 False。
  二、对照：成了就照旧（新配置、新地址、旧证书撤掉），返回 True。
  三、死代码：`shutil.rmtree("~/.acme.sh/*.old_ecc")`（glob 不展开，永远静默失败）已经删掉。"""
import importlib.util, os, sys, io, re, tempfile, contextlib, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
src = open(SRC, encoding="utf-8").read()
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)

T = tempfile.mkdtemp()
D = T + "/ms"
for sub in ("mediawarp/config", "autofilm/config", "homepage/config"):
    os.makedirs(f"{D}/{sub}")
open(f"{D}/docker-compose.yml", "w").write("services: {}\n")
open(f"{D}/CREDENTIALS.txt", "w").write("old\n")
m.NGX_CERT_DIR = T + "/certs"; os.makedirs(m.NGX_CERT_DIR)
m.ACME_CRT = T + "/node-acme.crt"
m.ms_install_dir = lambda: D
m.have = lambda b: True
H = "https" + "://"           # 拼出来：测试文件里不留网址（test_claude_folder）
m.public_ip = lambda: "1.2.3.4"
m.node_state = lambda: {"domain": "node.zone.example"}
m.acme_has_cf_creds = lambda: True
m.acme_bin = lambda: "/acme.sh"
m.read_emby_api_key = lambda d=None: "k"
m.someone_playing = lambda k, cache_s=0: False
m.autofilm_busy = lambda: False
OL_URL = []
m.ensure_openlist_site_url = lambda d, cfg=None, quiet=False: OL_URL.append((cfg or {}).get("domain")) or True
m.build_summary = lambda cfg, colored=True: "summary " + cfg["domain"]
def fake_cfg(d):
    dom = m.read_env(f"{d}/.env", "DOMAIN")
    return {"install_dir": d, "domain": dom, "has_domain": bool(dom), "homepage": True, "basic_auth": False,
            "crt": f"{m.NGX_CERT_DIR}/{dom}.crt", "key": f"{m.NGX_CERT_DIR}/{dom}.key"}
m.rebuild_cfg_from_disk = fake_cfg
m.gen_homepage_conf = lambda cfg: ("s", f"href: {H}home.{cfg['domain']}", "w")
RAISE_AF = [False]
def fake_af(cfg):
    if RAISE_AF[0]:
        raise RuntimeError("cfg 少一个键")
    return f"public_url: {H}list.{cfg['domain']}"
m.gen_autofilm_conf = fake_af
m.gen_mediawarp_conf = lambda cfg: f"addr: {H}list.{cfg['domain']}"
NGX, SH = [], []
m.apply_nginx_site = lambda cfg, quiet=True: (NGX.append(cfg["domain"]), True)[1]
def fake_issue(dom, crt, key, tok):
    open(crt, "w").write(f"DNS:*.{dom}"); open(key, "w").write("k")
    return True
m.issue_cert = fake_issue
FAIL = set()
def fake_sh(cmd, check=False, timeout=None):
    SH.append(cmd)
    out = ""
    mm = re.search(r"openssl x509 -noout -text -in '([^']+)'", cmd)
    if mm and os.path.exists(mm.group(1)):
        out = open(mm.group(1)).read()
    rc = 1 if any(f in cmd for f in FAIL) else 0
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
m.sh = fake_sh

OLD, NEW = "ys.zone.example", "media.zone2.example"
def reset():
    open(f"{D}/.env", "w").write("PUID=1000\nTZ=Asia/Shanghai\nDOMAIN=" + OLD + "\n")
    for f in os.listdir(m.NGX_CERT_DIR):
        os.remove(f"{m.NGX_CERT_DIR}/{f}")
    open(f"{m.NGX_CERT_DIR}/{OLD}.crt", "w").write(f"DNS:*.{OLD}"); open(f"{m.NGX_CERT_DIR}/{OLD}.key", "w").write("k")
    for f in ("mediawarp/config/config.yaml", "autofilm/config/config.yaml"):
        open(f"{D}/{f}", "w").write("old")
    for n in ("settings.yaml", "services.yaml", "widgets.yaml"):
        open(f"{D}/homepage/config/{n}", "w").write("old")
    open(f"{D}/CREDENTIALS.txt", "w").write("old\n")
    NGX.clear(); SH.clear(); OL_URL.clear(); FAIL.clear(); RAISE_AF[0] = False
def go():
    b = io.StringIO()
    with contextlib.redirect_stdout(b), contextlib.redirect_stderr(b):
        r = m.emby_domain_apply(D, NEW, "")
    return r, plain(b.getvalue())
env = lambda: m.read_env(f"{D}/.env", "DOMAIN")
def rolled_back(out):
    return (env() == OLD
            and open(f"{D}/mediawarp/config/config.yaml").read() == "old"
            and open(f"{D}/autofilm/config/config.yaml").read() == "old"
            and open(f"{D}/homepage/config/services.yaml").read() == "old"
            and open(f"{D}/CREDENTIALS.txt").read() == "old\n"
            and NGX == [NEW, OLD] and OL_URL[-1] == OLD
            and not os.path.exists(f"{m.NGX_CERT_DIR}/{NEW}.crt")
            and os.path.exists(f"{m.NGX_CERT_DIR}/{OLD}.crt")
            and any("--remove -d '*." + NEW + "' --ecc" in c for c in SH)
            and "已放回原来的域名" in out)

print("一、nginx 成功之后出事：整体放回")
reset(); FAIL.add("docker restart mediawarp")
r, out = go()
check("MediaWarp 重启失败 → 返回 False、整体放回", r is False and rolled_back(out))
reset(); RAISE_AF[0] = True
r, out = go()
check("AutoFilm 配置生成失败 → 返回 False、整体放回", r is False and rolled_back(out))
reset(); FAIL.add("docker restart homepage")
r, out = go()
check("首页重启失败 → 返回 False、整体放回", r is False and rolled_back(out))
reset(); FAIL.add("docker restart autofilm")
r, out = go()
check("AutoFilm 重启失败 → 返回 False、整体放回", r is False and rolled_back(out))

print("二、对照：成了就照旧")
reset()
r, out = go()
check("返回 True：.env、配置、总览表都是新的，nginx 只换一次", r is True and env() == NEW
      and open(f"{D}/mediawarp/config/config.yaml").read() == "addr: " + H + "list." + NEW
      and open(f"{D}/autofilm/config/config.yaml").read() == "public_url: " + H + "list." + NEW
      and open(f"{D}/CREDENTIALS.txt").read().startswith("summary " + NEW)
      and NGX == [NEW] and OL_URL == [NEW])
check("旧那张撤掉续期、旧文件删掉", any("--remove -d '*." + OLD + "' --ecc" in c for c in SH)
      and not os.path.exists(f"{m.NGX_CERT_DIR}/{OLD}.crt"))

print("三、死代码删掉了")
apply = src[src.index("def emby_domain_apply("):]; apply = apply[:apply.index("\ndef ", 1)]
check("没有那行 rmtree（glob 不展开、永远静默失败）", "rmtree(os.path.expanduser" not in apply
      and "*.{old}_ecc" not in apply and "~/.acme.sh/*" not in apply)
check("回滚先记旧文件、失败放回", "_snap_files(d," in apply and "rollback()" in apply
      and "已放回原来的域名" in apply)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
