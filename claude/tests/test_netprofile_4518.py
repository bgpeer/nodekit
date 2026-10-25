#!/usr/bin/env python3
"""节点画像（net-optimize 4.5.18）—— 仓库主人 10/25：「还要看节点和这个网络优化是不是对接上了……你还有没有更优秀的架构设计方案」，
  选了「修隐患 + 换 fq + 节点画像架构」。本测试管第二步：
  · xy-installer 把节点占的端口写成 /etc/bgpeer/netprofile.json（0600，只有端口）：两核心入站（含听 127.0.0.1 的）、
    CDN、订阅端口、nginx 本地 8443-8445、hy2 跳跃段，加上本机别的服务在听的 TCP 端口（OpenList / Emby…）
  · net-optimize 照单保留：挑端口的整段池子照留（新协议从挑端口到绑上有空档）+ 池子外的全补上；没画像退回老规矩
  · 节点一变（装 / 加 / 删协议、CDN 增删、换订阅端口、进面板）→ netprofile_sync：签名（PROFILE_SIG）对不上才叫
    `--apply-profile`，只改一个内核参数和 sysctl 文件那一行，不重启任何服务、不出网
  · 老版网络优化不认 --apply-profile（parse_known_args 会转头跑整套优化）→ 绝不喂给它
  · 手动指定的 RESERVED_PORTS 照旧生效、之后不按画像改；--check 只报个数，不把端口印上屏
真内核参数的端到端（独立 mount + net 命名空间里跑真脚本、bind(0) 验证内核不分保留端口）开发时跑过，见 PR 说明。
"""
import os, re, sys, io, json, socket, hashlib, logging, tempfile, contextlib, importlib.util
logging.raiseExceptions = False
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def load(name, path, env=None):
    old = dict(os.environ)
    if env is not None:
        os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear(); os.environ.update(old)

def body(src, name):
    m_ = re.search(rf"^def {name}\(.*?(?=^def |^# ===|\Z)", src, re.S | re.M)
    return m_.group(0) if m_ else ""

os.environ.pop("RESERVED_PORTS", None)
n = load("netopt", "net-optimize.py")
nsrc = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
x = load("xy", "xy-installer.py")
xsrc = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
tmp = tempfile.mkdtemp()
quiet = lambda: contextlib.redirect_stdout(io.StringIO())

# ── 1. 版本 ─────────────────────────────────────────────────────────────────
check(f"net-optimize 版本 ≥ 4.5.18（{n.VERSION}）", tuple(map(int, n.VERSION.split("."))) >= (4, 5, 18))

# ── 2. 端口段写法 ───────────────────────────────────────────────────────────
check("段解析：单端口 / 起-止，认不出的跳过", n._spec_parse("443, 15000-45000,x,9-3,70000") == [(443, 443), (15000, 45000)])
check("段合并：重叠、相邻合成一段，跟内核读回来的写法一样",
      n._spec_join([(8445, 8445), (8443, 8444), (15000, 45000), (18047, 18047), (2053, 2053)]) == "2053,8443-8445,15000-45000")

# ── 3. 网络优化按画像算保留端口 ───────────────────────────────────────────────
n.NETPROFILE = f"{tmp}/netprofile.json"
n.tcp_listen_ports = lambda: {22, 5244, 18047}
prof = {"v": 1, "pool": "15000-45000", "ranges": ["50000-51000"], "tcp": [80, 443, 2053, 8443, 8444, 8445, 18047, 52001],
        "udp": [31000], "listen": [8096]}
open(n.NETPROFILE, "w").write(json.dumps(prof, sort_keys=True, separators=(",", ":")) + "\n")
spec, info, sig = n.reserved_plan()
check(f"有画像：池子 + 池子外的节点端口 + 跳跃段 + 本机服务（画像里的 + 现在在听的），80/443/22 不在临时端口池不收（{spec}）",
      spec == "2053,5244,8096,8443-8445,15000-45000,50000-51000,52001")
check(f"个数说明：节点 {info['node']} 个、跳跃段 {info['ranges']} 段、本机其它服务 {info['listen']} 个",
      info == {"profile": True, "node": 7, "ranges": 1, "listen": 2})
check("签名 = 画像文件 sha256 前 12 位", sig == hashlib.sha256(open(n.NETPROFILE, "rb").read()).hexdigest()[:12])
os.remove(n.NETPROFILE)
spec, info, sig = n.reserved_plan()
check(f"没画像（没装节点脚本 / 老版）：老规矩整段 15000-45000 + 在听的（{spec}）",
      spec == "5244,15000-45000" and not info["profile"] and sig == "")
open(n.NETPROFILE, "w").write("[1, 2]")
check("画像坏了当没有", n.reserved_plan()[0] == "5244,15000-45000")

# 监听端口：两边同一个算法（同一份 /proc/net/tcp 样本），只收 LISTEN（0A），不收已连上的
TCP4 = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1\n"
        "   1: 0100007F:9C40 0100007F:1F90 01 00000000:00000000 00:00000000 00000000     0        0 2 1\n")
TCP6 = ("  sl  local_address                         remote_address                        st\n"
        "   0: 00000000000000000000000000000000:146C 00000000000000000000000000000000:0000 0A 0 0 0 0 0 3 1\n")
SAMPLE = {"/proc/net/tcp": TCP4, "/proc/net/tcp6": TCP6}
n2 = load("netopt2", "net-optimize.py")
n2.read_text = lambda f, d="": SAMPLE.get(f, d)
x_open = open
x.open = lambda f, *a, **k: io.StringIO(SAMPLE[f]) if f in SAMPLE else x_open(f, *a, **k)
a_, b_ = n2.tcp_listen_ports(), x._tcp_listen_ports()
del x.open
check(f"本机在听的 TCP 端口两边认得一样，只收 LISTEN（{sorted(a_)}）", a_ == b_ == {8080, 5228})
s4 = socket.socket(); s4.bind(("127.0.0.1", 0)); s4.listen(); lp = s4.getsockname()[1]
check(f"真 /proc/net/tcp 里刚开的监听口 {lp} 认得出来", lp in load("netopt2b", "net-optimize.py").tcp_listen_ports())
s4.close()

# ── 4. 整套优化：写 sysctl、记签名 ──────────────────────────────────────────
def sysctl_out(mod):
    out, real = {}, (mod.write_text, mod.run)
    mod.meminfo_kb = lambda k: 1048576
    mod.container_runtime_present = lambda: False
    mod.write_text = lambda p, c, **k: out.__setitem__(p, (c, k.get("mode")))
    mod.run = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})()     # 不真 sysctl --system
    mod.FINAL_CC, mod.FINAL_QDISC = "bbr", "fq"
    try:
        with quiet():
            mod.write_sysctl_conf()
    finally:
        mod.write_text, mod.run = real
    return out.get(mod.SYSCTL_AUTH_FILE, ("", None))
open(n.NETPROFILE, "w").write(json.dumps(prof, sort_keys=True, separators=(",", ":")) + "\n")
txt, mode = sysctl_out(n)
check("整套优化写进 sysctl 的就是按画像算的那串",
      "net.ipv4.ip_local_reserved_ports = 2053,5244,8096,8443-8445,15000-45000,50000-51000,52001" in txt)
check("sysctl 文件写成 0600（里面有节点端口）", mode == 0o600)
check("这一趟按哪版画像算的记下了", n._RESERVED_SIG == hashlib.sha256(open(n.NETPROFILE, "rb").read()).hexdigest()[:12])
mo = body(nsrc, "main_optimize")
check("跑完记签名排在 setup_mss_clamping 后面（它会整份重写 config）",
      0 < mo.find("setup_mss_clamping()") < mo.find("_remember_reserved()"))
check("换 BBRv3 修拥塞算法那步写回 sysctl 文件也保持 0600", "SYSCTL_AUTH_MODE if f == SYSCTL_AUTH_FILE" in body(nsrc, "_xanmod_fix_cc"))

for val, want in (("", ""), ("20000-21000", "20000-21000")):
    nf = load(f"netopt_fixed{len(val)}", "net-optimize.py", {"RESERVED_PORTS": val})
    nf.NETPROFILE = n.NETPROFILE
    t2, _m = sysctl_out(nf)
    line = re.search(r"^net\.ipv4\.ip_local_reserved_ports = (.*)$", t2, re.M)
    nf.CONFIG_FILE = f"{tmp}/cfg_fixed{len(val)}"
    nf._remember_reserved()
    check(f"手动指定 RESERVED_PORTS={val!r}：照它写（空 = 不写这一行），并记进 config",
          (line.group(1) if line else "") == want and nf.config_read().get("RESERVED_PORTS") == want)

# ── 5. --apply-profile：只刷这一项 ───────────────────────────────────────────
n3 = load("netopt3", "net-optimize.py")
n3.NETPROFILE, n3.tcp_listen_ports = n.NETPROFILE, (lambda: {5244})
n3.SYSCTL_AUTH_FILE, n3.CONFIG_FILE = f"{tmp}/99-net-optimize.conf", f"{tmp}/config"
sets = []
n3.set_sysctl = lambda k, v: (sets.append((k, v)), True)[1]
old_conf = ("net.core.default_qdisc = fq\nnet.ipv4.ip_local_port_range = 1024 65535\n"
            "net.ipv4.ip_local_reserved_ports = 15000-45000\nnet.ipv4.tcp_fastopen = 3\n")
open(n3.SYSCTL_AUTH_FILE, "w").write(old_conf); os.chmod(n3.SYSCTL_AUTH_FILE, 0o644)
open(n3.CONFIG_FILE, "w").write("ENABLE_MSS_CLAMP=1\nADAPTIVE_DAEMON=0\n")
with quiet() as o:
    n3.cmd_apply_profile()
want = "2053,5244,8096,8443-8445,15000-45000,50000-51000,52001"
conf = open(n3.SYSCTL_AUTH_FILE).read()
check("只换 sysctl 文件里保留端口那一行，别的不动", conf == old_conf.replace("15000-45000", want))
check("sysctl 文件收成 0600", os.stat(n3.SYSCTL_AUTH_FILE).st_mode & 0o777 == 0o600)
check("内核参数当场改（sysctl -w）", sets == [("net.ipv4.ip_local_reserved_ports", want)])
cfg = n3.config_read()
check("config 记下画像签名、原有项不丢",
      cfg.get("PROFILE_SIG") == hashlib.sha256(open(n.NETPROFILE, "rb").read()).hexdigest()[:12] and cfg.get("ADAPTIVE_DAEMON") == "0")
check("屏上一行 ✔，只有个数", "✔ 保留端口已按节点画像刷新" in o.getvalue() and "52001" not in o.getvalue())
open(n3.SYSCTL_AUTH_FILE, "w").write("net.ipv4.ip_local_port_range = 1024 65535\nvm.swappiness = 1\n")
with quiet():
    n3.cmd_apply_profile()
check("文件里没有那一行（老版手动关过）：插在 ip_local_port_range 下面",
      open(n3.SYSCTL_AUTH_FILE).read() == f"net.ipv4.ip_local_port_range = 1024 65535\nnet.ipv4.ip_local_reserved_ports = {want}\nvm.swappiness = 1\n")
# 整套优化正好同时在跑、刚重写了 sysctl 文件：别拿旧内容盖回去
open(n3.SYSCTL_AUTH_FILE, "w").write(old_conf)
real_rt, reads = n3.read_text, []
def racing(path, default=""):
    reads.append(path)
    if path == n3.SYSCTL_AUTH_FILE and reads.count(path) == 2:
        open(path, "w").write("# 整套优化刚写的新内容\n")
    return real_rt(path, default)
n3.read_text = racing
with quiet() as o:
    n3.cmd_apply_profile()
n3.read_text = real_rt
check("整套优化同时在跑：这次先不刷，它刚写的内容不被盖掉",
      open(n3.SYSCTL_AUTH_FILE).read() == "# 整套优化刚写的新内容\n" and "先不刷" in o.getvalue())
# 内核参数写不进去（容器里没权限）：照实说，签名照记（不然面板每进一次都叫一遍）
open(n3.SYSCTL_AUTH_FILE, "w").write(old_conf)
n3.set_sysctl, n3.write_proc = (lambda k, v: False), (lambda p_, v: False)
open(n3.CONFIG_FILE, "w").write("ENABLE_MSS_CLAMP=1\n")
with quiet() as o:
    n3.cmd_apply_profile()
check("写不进内核：报 ⚠ 不报 ✔，文件照改、签名照记",
      "写不进内核" in o.getvalue() and "✔" not in o.getvalue() and want in open(n3.SYSCTL_AUTH_FILE).read()
      and n3.config_read().get("PROFILE_SIG"))
n3.set_sysctl = lambda k, v: (sets.append((k, v)), True)[1]
open(n3.CONFIG_FILE, "a").write("RESERVED_PORTS=20000-21000\n")
sets.clear(); before = open(n3.SYSCTL_AUTH_FILE).read()
with quiet():
    n3.cmd_apply_profile()
check("手动指定过 RESERVED_PORTS：不按画像改", not sets and open(n3.SYSCTL_AUTH_FILE).read() == before)
n3.SYSCTL_AUTH_FILE = f"{tmp}/none.conf"
with quiet():
    n3.cmd_apply_profile()
check("没跑过网络优化（没有 sysctl 文件）：跳过，不凭空建", not os.path.exists(n3.SYSCTL_AUTH_FILE) and not sets)
ap = body(nsrc, "cmd_apply_profile")
check("--apply-profile 不出网、不自更新、不重启任何服务（不误伤播放）",
      not re.search(r"auto_update|self_install|systemctl|restart|fetch|urlopen|curl", ap))
check("命令行认 --apply-profile，并分派到 cmd_apply_profile",
      '"--apply-profile"' in nsrc and re.search(r'elif getattr\(args, "apply_profile"\):\s*require_root\(\)\s*cmd_apply_profile\(\)', nsrc))

# --check 那一行
n4 = load("netopt4", "net-optimize.py")
n4.NETPROFILE, n4.tcp_listen_ports = n.NETPROFILE, (lambda: {5244})
n4.RESERVED_PROC = f"{tmp}/reserved"
open(n4.RESERVED_PROC, "w").write(want + "\n")
with quiet() as o:
    n4.check_reserved({})
check("--check：对上了报 ✅ 个数", "已跟节点对上" in o.getvalue())
check("--check 不把端口印上屏（这屏常截图发出去）", not re.search(r"2053|5244|8096|8443|52001|50000", o.getvalue()))
open(n4.RESERVED_PROC, "w").write("15000-45000\n")
with quiet() as o:
    n4.check_reserved({})
check("--check：内核里还没对上报 ⚠", "还没对上" in o.getvalue())
with quiet() as o:
    n4.check_reserved({"RESERVED_PORTS": "1-2"})
check("--check：手动指定的照实说", "手动指定" in o.getvalue())
check("--check 第 [1] 段调了 check_reserved", "check_reserved(cfg)" in body(nsrc, "cmd_check"))

# ── 6. xy-installer 写画像 ─────────────────────────────────────────────────
x.SB_DIR, x.XRAY_DIR = f"{tmp}/sb", f"{tmp}/xr"; os.makedirs(x.SB_DIR); os.makedirs(x.XRAY_DIR)
x._CORE_META = {k: (v[0], v[1], f"{x.SB_DIR if k == 'sb' else x.XRAY_DIR}/config.json", v[3], v[4])
                for k, v in x._CORE_META.items()}
x.BGP_DIR = tmp
x.SUBPORT_FILE, x.STATE_FILE, x.NETPROFILE = f"{tmp}/sub.port", f"{tmp}/state.json", f"{tmp}/xy-netprofile.json"
x.NGINX_CONF, x.NGINX_FB_CONF, x.NGINX_STREAM_CONF = f"{tmp}/ng.conf", f"{tmp}/fb.conf", f"{tmp}/stream.conf"
x.NETOPT_CONFIG = f"{tmp}/netopt-config"
x._cdn_load = lambda: [{"cf_port": 2053, "svc": "xy-cdn-1"}]
x._tcp_listen_ports = lambda: {22, 5244, 8096, 18047}
json.dump({"inbounds": [{"type": "trojan", "listen": "::", "listen_port": 18047, "password": "pw-secret"},
                        {"type": "hysteria2", "listen": "::", "listen_port": 31000},
                        {"type": "vless", "listen": "127.0.0.1", "listen_port": 29999, "users": [{"uuid": "uuid-secret"}]},
                        {"type": "shadowsocks", "listen": "::", "listen_port": 33333}]}, open(f"{x.SB_DIR}/config.json", "w"))
json.dump({"inbounds": [{"listen": "0.0.0.0", "port": 36844, "protocol": "trojan"},
                        {"listen": "0.0.0.0", "port": 41234, "protocol": "vless", "streamSettings": {"network": "xhttp"}}]},
          open(f"{x.XRAY_DIR}/config.json", "w"))
open(x.SUBPORT_FILE, "w").write("52001")
open(x.NGINX_CONF, "w").write("server {\n  listen 80;\n  listen [::]:443 ssl;\n}\nserver {\n  listen 127.0.0.1:8443 ssl;\n}\n"
                              "server {\n  listen 127.0.0.1:8444;\n  location / { return 404; }\n}\n")
open(x.NGINX_FB_CONF, "w").write("server {\n  listen 127.0.0.1:8445;\n}\n")
json.dump({"hy2_ports": "50000-51000", "domain": "secret.example"}, open(x.STATE_FILE, "w"))
p = x.netprofile()
check(f"画像：两核心入站（含 127.0.0.1 的）、CDN、订阅、nginx 本地口都在 tcp（{p['tcp']}）",
      p["tcp"] == [80, 443, 2053, 8443, 8444, 8445, 18047, 29999, 33333, 36844, 41234, 52001])
check(f"画像：hy2 / ss / xhttp 的 UDP 在 udp（{p['udp']}）", p["udp"] == [31000, 33333, 41234])
check("画像：跳跃段、挑端口的池子", p["ranges"] == ["50000-51000"] and p["pool"] == f"{x.PORT_LO}-{x.PORT_HI}")
check(f"画像：本机别的服务在听的（去掉节点自己的、去掉 1024 以下）（{p['listen']}）", p["listen"] == [5244, 8096])
blob = json.dumps(p)
check("画像只有端口：没有 UUID / 密码 / 域名", set(p) == {"v", "pool", "tcp", "udp", "ranges", "listen"}
      and not re.search(r"secret|uuid|password|example", blob))
for raw, rng in (("n", []), ("off", []), ("", [x.HY2_PORTS]), ("bad", [x.HY2_PORTS])):
    json.dump({"hy2_ports": raw}, open(x.STATE_FILE, "w"))
    check(f"跳跃段跟 _hy2_hop_heal 一个判法：hy2_ports={raw!r} → {rng}", x.netprofile()["ranges"] == rng)
json.dump({"inbounds": [{"type": "trojan", "listen": "::", "listen_port": 18047}]}, open(f"{x.SB_DIR}/config.json", "w"))
json.dump({"hy2_ports": "50000-51000"}, open(x.STATE_FILE, "w"))
check("没有 hy2 就没有跳跃段（state 里留着也不算）", x.netprofile()["ranges"] == [])

# ── 7. netprofile_sync：签名对不上才叫网络优化 ─────────────────────────────────
calls = []
x._netopt_apply_profile = lambda: (calls.append(1), True)[1]
r = x.netprofile_sync()
check("没跑过网络优化：只写画像，不叫它", r is False and not calls and os.path.exists(x.NETPROFILE))
check("画像 0600", os.stat(x.NETPROFILE).st_mode & 0o777 == 0o600)
check("画像文件就是 netprofile() 的规范 JSON",
      open(x.NETPROFILE).read() == json.dumps(x.netprofile(), sort_keys=True, separators=(",", ":")) + "\n")
open(x.NETOPT_CONFIG, "w").write("ENABLE_MSS_CLAMP=1\n")
check("跑过网络优化、还没对上号（老版跑的、没有 PROFILE_SIG）：叫它", x.netprofile_sync() is True and len(calls) == 1)
n.NETPROFILE = x.NETPROFILE
xsig = n.read_netprofile()[1]
check("两边算的签名一样（xy-installer 写的文件 → net-optimize 读出来的签名）",
      xsig == hashlib.sha256(open(x.NETPROFILE, "rb").read()).hexdigest()[:12])
open(x.NETOPT_CONFIG, "w").write(f"ENABLE_MSS_CLAMP=1\nPROFILE_SIG={xsig}\n")
check("签名对上了：不叫（进面板不拖慢）", x.netprofile_sync() is False and len(calls) == 1)
x._tcp_listen_ports = lambda: {22, 5244, 8096, 18047, 8920}
check("本机多了个服务在听（画像变了）：叫它", x.netprofile_sync() is True and len(calls) == 2)
open(x.NETOPT_CONFIG, "w").write("PROFILE_SIG=zzz\nRESERVED_PORTS=20000-21000\n")
check("手动指定过 RESERVED_PORTS：不叫", x.netprofile_sync() is False and len(calls) == 2)
x.netprofile = lambda: 1 / 0
check("出什么错都不抛（进面板、装协议不能被它搞崩）", x.netprofile_sync() is False)

# ── 8. 叫网络优化：只叫认 --apply-profile 的，挑新的 ─────────────────────────
x2 = load("xy2", "xy-installer.py")
x2.NETOPT_SBIN, x2.NETOPT_LOCAL = f"{tmp}/sbin-netopt.py", f"{tmp}/local-netopt.py"
ran = []
x2.subprocess.run = lambda cmd, **k: (ran.append(cmd), type("R", (), {"returncode": 0})())[1]
open(x2.NETOPT_SBIN, "w").write('VERSION = "4.5.17"\n# 老版：不认那个参数\n')
check("都是老版：不叫（老版 parse_known_args 会忽略不认的参数、转头跑整套优化）",
      x2._netopt_apply_profile() is False and not ran)
open(x2.NETOPT_LOCAL, "w").write('VERSION = "4.5.18"\ng.add_argument("--apply-profile")\n')
check("面板缓存那份是新版：用它", x2._netopt_apply_profile() is True and ran[-1] == ["python3", x2.NETOPT_LOCAL, "--apply-profile"])
open(x2.NETOPT_SBIN, "w").write('VERSION = "4.5.19"\ng.add_argument("--apply-profile")\n')
x2._netopt_apply_profile()
check("两份都认：挑版本高的", ran[-1] == ["python3", x2.NETOPT_SBIN, "--apply-profile"])
ap = body(xsrc, "_netopt_apply_profile")
check("叫的时候不出网（不走 ensure_remote_script）、有超时", "ensure_remote_script" not in ap and "timeout=" in ap)

# ── 9. 节点一变就跟上：各个入口都调了 ─────────────────────────────────────────
for fn, why in (("run", "全新安装 / 全部重装"), ("_add_apply", "只添加新协议"), ("_del_apply", "删除协议"),
                ("cdn_add", "新增 CDN 节点"), ("cdn_remove", "卸载 CDN 节点"), ("main_menu", "进面板（老装机、错过的变动）")):
    check(f"{why}（{fn}）之后调 netprofile_sync", "netprofile_sync()" in body(xsrc, fn))
sp = xsrc[xsrc.index("set_sub_port(newp)"):]
check("换订阅端口：先交给网络优化保留、再起新端口的订阅服务",
      sp.index("netprofile_sync()") < sp.index("_ghrelay_regen()"))
mm = body(xsrc, "main_menu")
check("进面板那次排在各项自愈后面（nginx 规则重建等可能动端口）", mm.index("_heal_other_crons()") < mm.index("netprofile_sync()") < mm.index("while True:"))
check("画像收进 root-only 清单（harden_perms 每次入口收紧）", "NETPROFILE" in xsrc[xsrc.index("_PRIV_FILES = lambda"):xsrc.index("def harden_perms")])
uc = body(xsrc, "_uninstall_core")
check("卸载代理主体、/etc/bgpeer 留给 Emby 那套时，画像也删", re.search(r'for p in \("/etc/bgpeer/state\.json", CFG_AUTO_FILE, NETPROFILE\)', uc))
ua = body(xsrc, "uninstall_all")
check("只卸代理主体、网络优化留着：叫它把节点端口放回去", re.search(r"_uninstall_core\(\)\s*if nopt:\s*_netopt_apply_profile\(\)", ua))
x3 = load("xy3", "xy-installer.py"); n5 = load("netopt5", "net-optimize.py")
check("画像路径两边一致（/etc/bgpeer/netprofile.json）", x3.NETPROFILE == n5.NETPROFILE == "/etc/bgpeer/netprofile.json")
check("_netopt_version 也用 NETOPT_SBIN（同一个常量）", "NETOPT_SBIN" in body(xsrc, "_netopt_version"))

print("ALL PASS" if not bad else f"{bad} FAIL")
sys.exit(1 if bad else 0)
