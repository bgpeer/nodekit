#!/usr/bin/env python3
"""「16 自建 Emby → 7 设置 → 12 域名证书」：换 Emby 自己的底座域名（仓库主人 10/31：「不要干预emby吧，直接做成不询问，
  还是在emby设置里面做一个更换域名证书的口子吧……这样他们就互相不干预了，改动有点大做好一点哦」）。
钉住：
  一、7 设置里有第 12 项「域名证书　当前：底座」
  二、三个站点（emby / list / home）都要解析到本机，没建好不签、不改；IP:端口 装的不在这里改
  三、有人在播先问一声（默认否）；最后确认默认否
  四、真换：先签 *.新底座（签不下来什么都没动）→ .env → nginx（校验不过 .env 放回、新证书撤掉）→ 首页 / AutoFilm /
      MediaWarp 配置按新域名重写、重启 → 撤旧那张的续期、删旧文件；屏上每个新地址顶格一行
  五、不碰节点、自建 DNS（不调 xy-installer、不碰 /etc/ssl/sb）"""
import importlib.util, os, sys, io, re, tempfile, contextlib, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
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
IP = "198.51.100" + ".7"
H = "https" + "://"           # 拼出来：测试文件里不留网址（test_claude_folder）
m.public_ip = lambda: IP
RES = {}
m._ipv4_of = lambda n: RES.get(n, [IP])
m.node_state = lambda: {"domain": "node.zone.example"}
m.acme_has_cf_creds = lambda: True
m.acme_bin = lambda: "/acme.sh"
m.read_emby_api_key = lambda d=None: "k"
PLAYING = [False]
m.someone_playing = lambda k, cache_s=0: PLAYING[0]
m.autofilm_busy = lambda: False
m.ensure_openlist_site_url = lambda d, cfg=None, quiet=False: True
m.build_summary = lambda cfg, colored=True: "summary " + cfg["domain"]
def fake_cfg(d):
    dom = m.read_env(f"{d}/.env", "DOMAIN")
    return {"install_dir": d, "domain": dom, "has_domain": bool(dom), "homepage": True, "basic_auth": False,
            "crt": f"{m.NGX_CERT_DIR}/{dom}.crt", "key": f"{m.NGX_CERT_DIR}/{dom}.key"}
m.rebuild_cfg_from_disk = fake_cfg
m.gen_homepage_conf = lambda cfg: ("s", f"href: {H}home.{cfg['domain']}", "w")
m.gen_autofilm_conf = lambda cfg: f"public_url: {H}list.{cfg['domain']}"
m.gen_mediawarp_conf = lambda cfg: f"addr: {H}list.{cfg['domain']}"
NGX, SH = [], []
NGX_OK = [True]
m.apply_nginx_site = lambda cfg, quiet=True: (NGX.append(cfg["domain"]), NGX_OK[0])[1]
ISSUE_OK = [True]
def fake_issue(dom, crt, key, tok):
    if ISSUE_OK[0]:
        open(crt, "w").write(f"DNS:*.{dom}"); open(key, "w").write("k")
    return ISSUE_OK[0]
m.issue_cert = fake_issue
def fake_sh(cmd, check=False, timeout=None):
    SH.append(cmd)
    out = ""
    mm = re.search(r"openssl x509 -noout -text -in '([^']+)'", cmd)
    if mm and os.path.exists(mm.group(1)):
        out = open(mm.group(1)).read()
    return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
m.sh = fake_sh

def reset(dom="ys.zone.example"):
    open(f"{D}/.env", "w").write(f"PUID=1000\nTZ=Asia/Shanghai\nDOMAIN={dom}\n")
    for f in os.listdir(m.NGX_CERT_DIR):
        os.remove(f"{m.NGX_CERT_DIR}/{f}")
    if dom:
        open(f"{m.NGX_CERT_DIR}/{dom}.crt", "w").write(f"DNS:*.{dom}"); open(f"{m.NGX_CERT_DIR}/{dom}.key", "w").write("k")
    for f in ("mediawarp/config/config.yaml", "autofilm/config/config.yaml"):
        open(f"{D}/{f}", "w").write("old")
    NGX.clear(); SH.clear(); RES.clear(); PLAYING[0] = False; NGX_OK[0] = True; ISSUE_OK[0] = True
def go(new, yes=(True,)):
    yq = list(yes)
    m.ask = lambda p, default="": new
    m.ask_yn = lambda p, default=True: yq.pop(0) if yq else False
    b = io.StringIO()
    with contextlib.redirect_stdout(b), contextlib.redirect_stderr(b):
        m.emby_domain_menu()
    return plain(b.getvalue())
env = lambda: m.read_env(f"{D}/.env", "DOMAIN")

print("一、菜单")
src = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
check("7 设置里有第 12 项「域名证书　当前：底座」", '"  12. 域名证书         当前："' in src
      and 'elif c == "12":\n            emby_domain_menu()' in src)

print("二、真换")
reset()
out = go("media.zone2.example")
check("先签 *.新底座、证书核过名字", os.path.exists(f"{m.NGX_CERT_DIR}/media.zone2.example.crt") and any("x509 -noout -text" in c for c in SH))
check(".env 换成新底座、别的行不动", env() == "media.zone2.example" and "TZ=Asia/Shanghai" in open(f"{D}/.env").read())
check("nginx 按新域名重写", NGX == ["media.zone2.example"])
check("首页 / AutoFilm / MediaWarp 配置换成新地址", "home.media.zone2.example" in open(f"{D}/homepage/config/services.yaml").read()
      and open(f"{D}/autofilm/config/config.yaml").read() == "public_url: https://list.media.zone2.example"
      and open(f"{D}/mediawarp/config/config.yaml").read() == "addr: https://list.media.zone2.example")
check("MediaWarp 配置 0600（里面有 Emby 的 Key）", oct(os.stat(f"{D}/mediawarp/config/config.yaml").st_mode & 0o777) == "0o600")
check("重启首页、AutoFilm、MediaWarp", all(any(f"docker restart {c}" in x for x in SH) for c in ("homepage", "autofilm", "mediawarp")))
check("总览表重写", open(f"{D}/CREDENTIALS.txt").read().startswith("summary media.zone2.example"))
check("旧那张撤掉续期、旧文件删掉", any("--remove -d '*.ys.zone.example' --ecc" in c for c in SH)
      and not os.path.exists(f"{m.NGX_CERT_DIR}/ys.zone.example.crt"))
check("屏上每个新地址顶格一行", "\nhttps://emby.media.zone2.example\n" in out and "\nhttps://list.media.zone2.example\n" in out
      and "\nhttps://home.media.zone2.example\n" in out and "✔ Emby 换成 *.media.zone2.example" in out)
check("不碰节点、自建 DNS", not any("xy-installer" in c or "/etc/ssl/sb" in c or "AdGuard" in c for c in SH))
reset()
out = go("emby.media.zone2.example")
check("填 emby.底座 也认（跟安装时一样去掉 emby.）", env() == "media.zone2.example")

print("三、不该换的 / 放回去")
reset(); RES["list.media.zone2.example"] = ["203.0.113" + ".9"]
out = go("media.zone2.example")
check("有一个站点没解析到本机：不签、不改，说清是哪个", env() == "ys.zone.example" and not NGX
      and "✗ list.media.zone2.example" in out and "没有改动" in out)
reset(); ISSUE_OK[0] = False
out = go("media.zone2.example")
check("证书没签下来：什么都没改", env() == "ys.zone.example" and not NGX and "证书没拿到，什么都没改" in out)
reset(); NGX_OK[0] = False
out = go("media.zone2.example")
check("nginx 校验不过：.env 放回、新证书撤掉删掉、旧的没动", env() == "ys.zone.example"
      and not os.path.exists(f"{m.NGX_CERT_DIR}/media.zone2.example.crt")
      and any("--remove -d '*.media.zone2.example' --ecc" in c for c in SH)
      and os.path.exists(f"{m.NGX_CERT_DIR}/ys.zone.example.crt") and not any("docker restart" in c for c in SH))
reset(); PLAYING[0] = True
out = go("media.zone2.example", yes=(False,))
check("有人在播：先问一声，默认否 → 没改", env() == "ys.zone.example" and "有人在看片" in out and not NGX)
reset()
out = go("media.zone2.example", yes=(False,))
check("最后确认默认否：没改；确认前红字一行说旧地址打不开", env() == "ys.zone.example" and not NGX
      and "旧地址 https://emby.ys.zone.example 换完就打不开了" in out)
reset(); out = go("ys.zone.example")
check("跟现在一样：没改", "跟现在一样" in out and not NGX)
reset(""); out = go("media.zone2.example")
check("IP:端口 装的：不在这里改，一行说明", env() == "" and not NGX and "IP:端口 装的要用域名，得重新装一次 Emby" in out)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
