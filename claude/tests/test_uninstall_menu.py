#!/usr/bin/env python3
"""菜单 20 卸载（仓库主人 11/09）：
  「1 卸载代理主体 2 卸载网络优化 3 卸载自建DNS 4 卸载自建emby 5 全部卸载 0 返回；
   这个全部卸载一定要输入三个字母 yes，里面还要做一行红字作提示：将卸载脚本所有功能包括系统内核回到初始状态，请考虑清楚再执行」。
  1~4 交给各模块自己的卸载；5 一次全卸（Emby → 自建DNS → 换回系统内核 → 网络优化 → 代理主体和面板 → acme.sh 里本脚本签的证书）。
  卸代理主体时别的模块还在：bgpeer 和 /etc/bgpeer 留着（不然连卸它们的入口都没了），只删节点自己的文件。
"""
import io, os, re, sys, types, tempfile, contextlib, subprocess, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp()
os.environ["HOME"] = TMP                      # acme.sh 在 ~/.acme.sh：指到临时目录
def load(name, f):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, f))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
def quiet(f, *a, **k):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        try:
            r = f(*a, **k)
        except SystemExit:
            r = "EXIT"
    return r, b.getvalue()

x = load("xy", "xy-installer.py")
ANS, CALLS = [], []
x._ask = lambda p="": (print(p, end="") or ANS.pop(0)) if ANS else "0"
x.sh = lambda c, check=True: CALLS.append(("sh", c)) or ""
x._run_module = lambda url, local, args, need="": CALLS.append(("mod", os.path.basename(local), args)) or 0
x._run_net_optimize = lambda args="", env_extra=None: CALLS.append(("netopt", args))
x._netopt_apply_profile = lambda: CALLS.append(("profile",))
REAL_CORE = x._uninstall_core
x._uninstall_core = lambda keep_panel=False: CALLS.append(("core", keep_panel))
REAL_ACME = x._acme_cleanup
x._acme_cleanup = lambda f, purge_if_empty=False: CALLS.append(("acme", purge_if_empty)) or (0, False, False)
ST = {"core": True, "nopt": True, "dns": True, "emby": True, "dep": False, "xan": True}
x._core_installed = lambda: ST["core"]
x._netopt_installed = lambda: ST["nopt"]
x._agh_installed = lambda: ST["dns"]
x._media_stack_installed = lambda: ST["emby"]
x._agh_uses_node_cert = lambda: ST["dep"]
x._xanmod_on_boot = lambda boot="/boot": ST["xan"]
def menu(*a):
    ANS[:] = list(a); CALLS.clear()
    return quiet(x.uninstall_all)

print("一、菜单")
_, out = menu("0")
check("六项：1 代理主体 / 2 网络优化 / 3 自建DNS / 4 自建Emby / 5 全部卸载 / 0 返回",
      all(s in out for s in ("1. 卸载代理主体", "2. 卸载网络优化", "3. 卸载自建DNS", "4. 卸载自建Emby",
                             "5. 全部卸载", "0. 返回")))
check("每项后面显示装没装", "卸载代理主体　当前：已安装" in out and "卸载网络优化　当前：已启用" in out)
check("选项下面不写解释行（第五节）", "sing-box / xray、订阅服务" not in out)
ST.update(core=False, nopt=False, dns=False, emby=False)
_, out = menu("1", "2", "3", "4", "0")
check("没装的：说不用卸，什么都不动", out.count("不用卸") == 4 and not CALLS)
check("没装显示未安装 / 未启用", "未安装" in out and "未启用" in out)
ST.update(core=True, nopt=True, dns=True, emby=True)

print("二、5 全部卸载：要打 yes")
for a, why in (("y", "y"), ("", "回车"), ("no", "no"), ("yess", "多打一个字")):
    _, out = menu("5", a, "0")
    check(f"打 {why}：取消、什么都不动", not CALLS and "已取消" in out)
check("一行红字（原话）", x.UNINSTALL_ALL_WARN == "将卸载脚本所有功能包括系统内核回到初始状态，请考虑清楚再执行"
      and "⚠ " + x.UNINSTALL_ALL_WARN in out)
r, out = menu("5", "yes", "")
order = [c for c in CALLS if c[0] in ("mod", "netopt", "core", "acme")]
check("打 yes：Emby → 自建DNS → 换回系统内核 → 网络优化 → 代理主体和面板 → acme.sh",
      order == [("mod", "media-stack.py", "uninstall --yes"), ("mod", "adguard-dns.py", "remove --yes"),
                ("mod", "net-optimize.py", "--xanmod-remove --yes"), ("mod", "net-optimize.py", "--reset"),
                ("core", False), ("acme", True)])
check("问一次重启、默认否（回车不重启）", "现在重启" in out and not any(c == ("sh", "reboot") for c in CALLS))
check("卸完退出（面板已经没了）", r == "EXIT" and "全部卸载完毕" in out)
_, out = menu("5", "YES", "y")
check("大写 YES 也认；答 y 才重启", ("sh", "reboot") in CALLS)
ST.update(nopt=False, dns=False, emby=False, xan=False)
_, out = menu("5", "yes")
check("没装的模块跳过、不问重启", [c for c in CALLS if c[0] != "sh"] == [("core", False), ("acme", True)]
      and out.count("跳过") == 4 and "现在重启" not in out)
ST.update(nopt=True, dns=True, emby=True, xan=True)

print("三、1 卸载代理主体")
_, out = menu("1", "", "0")
check("确认默认否：回车不卸", not CALLS and "节点、订阅、证书全部删除" in out)
r, out = menu("1", "y", "0")
check("别的模块还在：留着面板（keep_panel），网络优化把端口放回去，回到这一屏",
      ("core", True) in CALLS and ("profile",) in CALLS and r != "EXIT")
ST.update(nopt=False, emby=False, dns=True, dep=True)
r, out = menu("1", "y")
check("老装法自建DNS（共用节点证书）：确认前写明一起卸；只剩它时面板不留、卸完退出",
      "自建DNS 用的是节点证书，一起卸掉" in out and ("core", False) in CALLS and r == "EXIT")
ST.update(dep=False)
r, out = menu("1", "y", "0")
check("新装法自建DNS（自己的证书）：不跟着卸，面板留着", ("core", True) in CALLS and r != "EXIT")
ST.update(nopt=True, emby=True)

print("四、2 / 3 / 4 交给各模块自己的卸载")
_, out = menu("2", "y", "0")
check("2：确认后 --reset；BBRv3 内核留着并说清去哪换回", ("netopt", "--reset") in CALLS and "BBRv3 内核留着" in out)
_, out = menu("2", "", "0")
check("2：默认否", not CALLS)
_, out = menu("3", "0")
check("3：节点还在 → adguard-dns.py uninstall（订阅里的 DoH 一起撤）", CALLS == [("mod", "adguard-dns.py", "uninstall")])
ST["core"] = False
_, out = menu("3", "0")
check("3：节点不在 → remove（不用刷订阅）", CALLS == [("mod", "adguard-dns.py", "remove")])
ST["core"] = True
_, out = menu("4", "0")
check("4：media-stack.py uninstall（它自己要打 yes、再问删不删数据）", CALLS == [("mod", "media-stack.py", "uninstall")])

print("五、_uninstall_core：留不留面板 / 证书")
x._uninstall_core = REAL_CORE
x._acme_cleanup = lambda f, purge_if_empty=False: CALLS.append(("acme-sb", f("x", "/etc/ssl/sb/acme.crt"),
                                                                   f("y", "/etc/ssl/agh/agh.crt"))) or (0, False, False)
x._cdn_load = lambda: []
x._hy2_hop_unpersist = x._nginxconf_remove_stream = x.jobq_cleanup_if_unused = lambda: None
x.have = lambda b: False
x.MS_NGINX_CONF = f"{TMP}/no-ms.conf"
def core(keep, ms=False, dep=False):
    CALLS.clear(); ST.update(emby=ms, dns=dep, dep=dep)
    quiet(x._uninstall_core, keep_panel=keep)
    return [c[1][7:] for c in CALLS if c[0] == "sh" and c[1].startswith("rm -rf ")]
rm = core(False)
check("什么都不剩：bgpeer、/etc/bgpeer、节点证书、自更新 cron 全收", all(p in rm for p in
      ("/usr/local/bin/bgpeer", x.BGP_DIR, "/etc/ssl/sb", x.SELFUPD_CRON, x.SB_BIN, x.NODE_FILE)))
check("节点证书删了：acme.sh 里那条一起撤（只撤 /etc/ssl/sb 的）", ("acme-sb", True, False) in CALLS)
rm = core(True)
check("别的模块还在：bgpeer、/etc/bgpeer、自更新 cron 留着", not any(p in rm for p in
      ("/usr/local/bin/bgpeer", x.BGP_DIR, x.SELFUPD_CRON)))
check("……节点自己的文件照删（链接 / token / 订阅 / 画像 / 证书事实）", all(p in rm for p in
      (x.STATE_FILE, x.TOKENS_FILE, x.LINKS_FILE, x.SUB_DIR, x.NETPROFILE, x.CERT_META, x.NODE_FILE)))
check("……别的模块的文件一个不碰", not any(p in rm for p in
      (x.MS_STATE_FILE, x.AGH_DOMAIN_FILE, x.SELFDNS_CID_FILE, x.PRIVDNS_MAP_FILE, x.ADGUARD_LOCAL,
       x.MEDIA_LOCAL, x.NETOPT_LOCAL, x.SELF_LOCAL, x.TRAFFIC_FILE, x.SELFUPD_OFF)))
open(x.MS_NGINX_CONF, "w").close()
rm = core(True, ms=True)
check("Emby 的网站用着节点证书：/etc/ssl/sb 留着、acme.sh 记录不撤",
      "/etc/ssl/sb" not in rm and not any(c[0] == "acme-sb" for c in CALLS))
os.remove(x.MS_NGINX_CONF)
core(False, dep=True)
check("老装法自建DNS：先走它自己的卸载（remove --yes）", CALLS[0] == ("mod", "adguard-dns.py", "remove --yes"))

print("六、acme.sh 清理")
x._acme_cleanup = REAL_ACME
A = os.path.join(TMP, ".acme.sh")
CERTS = os.path.join(TMP, "nginx-certs")      # 测试里 Emby 的证书文件放这儿（判据照真路径，文件路径另记一份）
def rec(name, path, cf=True, ecc=True, key=""):
    d = os.path.join(A, name + ("_ecc" if ecc else "")); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, name + ".conf"), "w").write(
        f"Le_Domain='{name}'\nLe_Webroot='{'dns_cf' if cf else 'no'}'\nLe_RealFullChainPath='{path}'\n"
        + (f"Le_RealKeyPath='{key}'\n" if key else ""))
def fresh(mine_only):
    import shutil; shutil.rmtree(A, ignore_errors=True); os.makedirs(os.path.join(A, "ca"))
    open(os.path.join(A, "acme.sh"), "w").close()
    open(os.path.join(A, "account.conf"), "w").write("SAVED_CF_Token='tok'\nSAVED_CF_Account_ID='acc'\nLOG_LEVEL=1\n")
    rec("node.example", "/etc/ssl/sb/acme.crt")
    rec("dns.example", "/etc/ssl/agh/agh.crt")
    os.makedirs(CERTS, exist_ok=True); open(os.path.join(CERTS, "ys.example.key"), "w").close()
    rec("*.ys.example", "/etc/nginx/certs/ys.example.crt", key=os.path.join(CERTS, "ys.example.key"))
    if not mine_only:
        rec("mysite.example", "/etc/nginx/ssl/mysite.crt")
fresh(False); CALLS.clear()
gone, tok, purged = quiet(x._acme_cleanup, x._acme_ours, purge_if_empty=True)[0]
left = sorted(os.listdir(A))
check("全部卸载、你还有自己网站的证书：只撤本脚本那三张，acme.sh 和令牌留着",
      gone == 3 and not purged and not tok and "mysite.example_ecc" in left
      and "SAVED_CF_Token" in open(os.path.join(A, "account.conf")).read())
check("装出去的证书文件一起删（Emby 那张的私钥）", not os.path.exists(os.path.join(CERTS, "ys.example.key")))
check("撤之前先让 acme.sh 不再续期（--remove，泛域名名字带引号）",
      any("--remove -d '*.ys.example' --ecc" in c[1] for c in CALLS if c[0] == "sh"))
fresh(True)
gone, tok, purged = quiet(x._acme_cleanup, x._acme_ours, purge_if_empty=True)[0]
check("acme.sh 只给本脚本签过：连它一起卸（--uninstall 撤 cron，目录删掉）",
      purged and not os.path.exists(A) and any(c == ("sh", os.path.join(A, "acme.sh") + " --uninstall") for c in CALLS))
fresh(True)
d = os.path.join(A, "old.example"); os.makedirs(d)          # 你自己以前 --remove 过的记录：文件可能还被你的网站直接引用
open(os.path.join(d, "old.example.conf.removed"), "w").write("Le_Domain='old.example'\nLe_RealFullChainPath=''\n")
gone, tok, purged = quiet(x._acme_cleanup, x._acme_ours, purge_if_empty=True)[0]
check("……有你自己 --remove 过的记录也不卸 acme.sh、不动它", not purged and os.path.isdir(d) and gone == 3)
fresh(True)
gone, tok, purged = quiet(x._acme_cleanup, lambda n, p: p.startswith("/etc/ssl/sb/"))[0]
check("只卸代理主体：撤节点那张；Emby / 自建DNS 还靠 CF 令牌续期，令牌留着",
      gone == 1 and not tok and "SAVED_CF_Token" in open(os.path.join(A, "account.conf")).read()
      and "dns.example_ecc" in os.listdir(A))
fresh(True)
for n in ("dns.example_ecc", "*.ys.example_ecc"):
    import shutil; shutil.rmtree(os.path.join(A, n))
gone, tok, purged = quiet(x._acme_cleanup, lambda n, p: p.startswith("/etc/ssl/sb/"))[0]
acct = open(os.path.join(A, "account.conf")).read()
check("……再没人用 CF 令牌：令牌清掉，别的设置不动、仍是 root-only",
      tok and "SAVED_CF_" not in acct and "LOG_LEVEL=1" in acct
      and oct(os.stat(os.path.join(A, "account.conf")).st_mode & 0o777) == "0o600")
check("Emby 判据：*.底座 装成 /etc/nginx/certs/底座.crt 才算，你自己放那儿的不算",
      x._acme_ours("*.ys.example", "/etc/nginx/certs/ys.example.crt")
      and not x._acme_ours("shop.example", "/etc/nginx/certs/shop.example.crt"))

print("七、各模块的免确认参数")
a = load("agh", "adguard-dns.py")
got = []
a.uninstall = lambda refresh_sub=True, assume_yes=False: got.append((refresh_sub, assume_yes))
for argv, want in ((["x", "remove", "--yes"], (False, True)), (["x", "uninstall"], (True, False)),
                   (["x", "remove"], (False, False))):
    got.clear(); sys.argv = argv; a.main()
    check(f"adguard-dns.py {' '.join(argv[1:])} → refresh_sub={want[0]} assume_yes={want[1]}", got == [want])
a = load("agh2", "adguard-dns.py")
asked = []
a._ask = lambda p="": asked.append(p) or "n"
a._installed = lambda: True
a._domain = a._own_domain = a._selfdns_clientid = lambda: ""
a._privdns_mode = lambda: ""
a._allowed_clients = lambda: None
a._h3_on = lambda: False
a.sh = lambda c: ""
a._restore_resolved = lambda: None
a.AGH_BIN = f"{TMP}/nope"; a.AGH_DIR = f"{TMP}/nope-dir"; a.SELFDNS_FLAG = f"{TMP}/nope-flag"
a.RESOLVED_DROPIN = f"{TMP}/nope-dropin"
_, out = quiet(a.uninstall, refresh_sub=False, assume_yes=True)
check("adguard-dns：assume_yes 不列清单、不问，直接卸", not asked and "已卸载 AdGuard Home" in out)
_, out = quiet(a.uninstall, refresh_sub=False)
check("adguard-dns：平时照旧列清单、问 [y/N]（答 n 不动）", asked and "已取消" in out)

m = load("ms", "media-stack.py")
D = os.path.join(TMP, "ms"); os.makedirs(D); open(os.path.join(D, "docker-compose.yml"), "w").close()
asked = []
m.require_root = lambda: None
m.ms_install_dir = lambda: D
m.is_installed = lambda d: True
m.ask = lambda *a, **k: asked.append(a) or ""
m.ask_yn = lambda *a, **k: asked.append(a) or False
m.sh = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr="")
m.ms_state = lambda: {}
for f in ("remove_heal_daemon", "remove_alitv_service", "remove_heal_gate", "remove_hls_service", "nginx_reload",
          "jobq_cleanup_if_unused", "bcast_remove"):
    setattr(m, f, lambda *a, **k: None)
m.NGX_SITE = f"{TMP}/no-site"
for c in ("HTPASSWD_FILE", "CLI_PATH", "CLI_ALIAS", "MS_STATE", "KEEPALIVE_CRON", "SYNC_CRON", "WARM_CRON",
          "SELFUP_CRON", "IMGUP_CRON", "TRAFFIC_CRON", "HEAL_CRON", "META_CRON"):
    setattr(m, c, f"{TMP}/no-{c}")
_, out = quiet(m.do_uninstall, assume_yes=True)
check("media-stack：assume_yes 不问、数据目录一起删（全部卸载 = 回到初始状态）", not asked and not os.path.exists(D))
os.makedirs(D)
D2 = os.path.join(TMP, "not-ms"); os.makedirs(D2)          # 没有 docker-compose.yml：不是媒体栈装的目录
m.ms_install_dir = lambda: D2
_, out = quiet(m.do_uninstall, assume_yes=True)
un = src_ms = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
un = un[un.index("def do_uninstall("):]; un = un[:un.index("\ndef ", 1)]
check("media-stack：不问的时候自己把关——不是它的目录（没 docker-compose.yml）不删，/opt 这类系统目录写死不删",
      os.path.isdir(D2) and "没删" in out and '"docker-compose.yml"' in un and '"/opt"' in un and '"/root"' in un)
m.ms_install_dir = lambda: D
asked.clear()
_, out = quiet(m.do_uninstall)
check("media-stack：平时照旧要打 yes（回车取消）", asked and os.path.isdir(D) and "已取消" in out)
src = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
check("media-stack：命令行 uninstall --yes 走免确认", 'do_uninstall(assume_yes="--yes" in sys.argv[2:])' in src)

n = load("netopt", "net-optimize.py")
asked, ran, reb = [], [], []
n._boot_kernels = lambda: ["6.1.0-28-amd64", "6.12.5-x64v3-xanmod1"]
n._xanmod_installed_pkgs = lambda: ["linux-xanmod-x64v3"]
n.check_dpkg_clean = lambda: None
n._ask_tty = lambda p="": asked.append(p) or ""
n._xanmod_reboot_prompt = lambda y: reb.append(y)
n.have_cmd = lambda c: False
n._xanmod_other_sources = lambda: True
def fake_run(cmd, timeout=0, env_extra=None, text_input=None):
    ran.append(cmd)
    if cmd[:2] == ["apt-get", "-o"] and "purge" in cmd:
        n._boot_kernels = lambda: ["6.1.0-28-amd64"]
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
n.run = fake_run
r, out = quiet(n.xanmod_remove, assume_yes=True)
check("net-optimize：--xanmod-remove --yes 不问、卸掉 XanMod、不问重启（那边统一问）",
      r is True and not asked and not reb and any("purge" in c for c in ran))
n._boot_kernels = lambda: ["6.12.5-x64v3-xanmod1"]
r, out = quiet(n.xanmod_remove, assume_yes=True)
check("net-optimize：/boot 里没有系统内核可回，免确认也不卸（卸了开不了机）", r is False and "没卸" in out)
n._boot_kernels = lambda: ["6.1.0-28-amd64", "6.12.5-x64v3-xanmod1"]
asked.clear(); ran.clear()
r, out = quiet(n.xanmod_remove)
check("net-optimize：平时照旧要打 yes（回车取消）", r is False and asked and not any("purge" in c for c in ran))
h = subprocess.run([sys.executable, os.path.join(ROOT, "net-optimize.py"), "--help"], capture_output=True, text=True)
check("net-optimize：命令行认 --xanmod-remove", "--xanmod-remove" in h.stdout)

print("八、版本号")
check("media-stack 1.5.396 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 396))
check("net-optimize 4.5.26 起", tuple(map(int, n.VERSION.split("."))) >= (4, 5, 26))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
