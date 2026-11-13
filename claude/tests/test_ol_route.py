#!/usr/bin/env python3
"""网盘接口选路：OpenList 连夸克接口时避开走不通的地址（仓库主人 11/12：「可以让他自动排除坏的地址自动选择优秀的地址吗，
  如果全部坏了然后让他自动测试换一批」）。

  真机 11/12：夸克 TV 接口的域名每次解析给的 IPv4 不一样，其中一段从那台机器 15 秒连不上、另一个 1 秒就通。VPS 本机有
  IPv6 会自己绕开，OpenList 在容器里只有 IPv4，抽到坏的就干等到超时 —— 列目录 93 秒、点开转圈后退出。
钉住：
  一、DNS 给的地址里有坏的 → 挑最快的通的，写进 OpenList 容器的 hosts；别的行原样不动
  二、现在钉着的还通 → 不换（别来回跳）；钉着的坏了 → 换成通的里最快的
  三、DNS 给的都通了 → 摘掉，交还给 DNS
  四、手上的全坏 → 重新多解析几轮换一批再测；还是全坏 → 摘掉、不乱钉，体检里报红
  五、只改容器里的 hosts：不重启容器、不碰 MediaWarp / Emby；容器重建后钉的那行没了会补上
  六、挂在每小时保活、体检上，能手动跑；只用本机解析，不问别的公共 DNS"""
import importlib.util, os, sys, io, re, tempfile, contextlib, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()

T = tempfile.mkdtemp(); D = T + "/ms"; os.makedirs(D)
open(f"{D}/docker-compose.yml", "w").write("services: {}\n")
m.BGP_DIR, m.MS_STATE, m.CRON_LOCK_DIR = T + "/bgpeer", T + "/bgpeer/media-stack.json", T + "/lock"
m.ms_install_dir = lambda: D
MOUNTS = [("/quark", "QuarkTV", "work", "0", "")]          # 这台机器只挂了夸克
m.openlist_storages = lambda d: list(MOUNTS)
HOST = m.OL_ROUTE_HOSTS[0]
A, B, C, E, F = "1.0.0.1", "1.0.0.2", "1.0.0.3", "1.0.0.4", "1.0.0.5"      # 测试用的占位地址
BASE = ["127.0.0.1\tlocalhost", "1.0.0.9\topenlist-container"]
BOX = {"hosts": list(BASE), "up": True, "writes": 0, "cmds": []}
DNS = {"normal": {A, B, C}, "fresh": set()}
SPEED = {}                                     # ip → 秒；不在里面 = 连不上
RES = {"rounds": []}
def fake_sh(cmd, check=False, timeout=None):
    BOX["cmds"].append(cmd)
    if cmd == "docker exec openlist cat /etc/hosts":
        return types.SimpleNamespace(returncode=0 if BOX["up"] else 1, stdout="\n".join(BOX["hosts"]) + "\n", stderr="")
    raise AssertionError(f"没料到的命令 {cmd}")
def fake_run(cmd, **kw):
    BOX["cmds"].append(cmd)
    assert cmd == ["docker", "exec", "-i", "-u", "0", "openlist", "sh", "-c", "cat > /etc/hosts"], cmd
    BOX["hosts"] = kw["input"].rstrip("\n").split("\n"); BOX["writes"] += 1
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
m.sh = fake_sh
m.subprocess = types.SimpleNamespace(run=fake_run)
m._route_resolve = lambda host, rounds=6: (RES["rounds"].append(rounds), set(DNS["normal"]) | (DNS["fresh"] if rounds > 6 else set()))[1]
m._route_probe = lambda host, ip, timeout=4: SPEED.get(ip)
V6 = {"good": [], "on_ok": True, "reach": True, "on": 0, "off": 0}       # IPv6 后路：默认本机没有 IPv6
REAL = {k: getattr(m, k) for k in ("_route6_addrs", "_route6_on", "_route6_off", "_ol_can_reach", "_route6_rules")}
m._route6_addrs = lambda host: list(V6["good"])
m._route6_on = lambda: (V6.__setitem__("on", V6["on"] + 1), m.save_ms_state(route6_gw="1.0.0.9") if V6["on_ok"] else None, "1.0.0.9" if V6["on_ok"] else "")[2]
m._route6_off = lambda: (V6.__setitem__("off", V6["off"] + 1), m.save_ms_state(route6_gw=""))[0]
m._ol_can_reach = lambda host: V6["reach"]
pinned = lambda: [l for l in BOX["hosts"] if l.endswith(m.OL_ROUTE_MARK)]
def run():
    BOX["cmds"].clear(); RES["rounds"].clear()
    return m.ol_route_check(D)

print("一、有坏的 → 钉最快的通的")
SPEED.update({A: 0.9, C: 0.3})                 # B 连不上
r = run()[0]
check("报告：两个通（按快慢排）、一个不通", [ip for ip, _ in r["good"]] == [C, A] and r["bad"] == [B])
check("钉的是最快的那个，写进容器 hosts", r["pin"] == C and pinned() == [f"{C} {HOST} {m.OL_ROUTE_MARK}"])
check("容器 hosts 里原有的行原样不动", BOX["hosts"][:2] == BASE and len(BOX["hosts"]) == 3)
check("写法：往原文件里写（docker 挂进来的单个文件换不了），用 root", ["docker", "exec", "-i", "-u", "0", "openlist", "sh", "-c", "cat > /etc/hosts"] in BOX["cmds"])
check("一个容器都没重启、没碰 MediaWarp / Emby", not [c for c in BOX["cmds"] if re.search(r"restart|compose|mediawarp|emby", str(c))])

print("二、钉着的还通就不换；坏了才换")
SPEED.update({A: 0.1, C: 0.8})                 # A 现在比 C 快，但 C 还通
n = BOX["writes"]; r = run()[0]
check("钉着的还通：不因为别的更快就换（别来回跳），没重写 hosts", r["pin"] == C and BOX["writes"] == n)
del SPEED[C]                                   # C 也坏了
r = run()[0]
check("钉着的坏了：换成通的里最快的", r["pin"] == A and r["was"] == C and pinned() == [f"{A} {HOST} {m.OL_ROUTE_MARK}"])

print("三、都通了 → 摘掉")
SPEED.update({A: 0.2, B: 0.2, C: 0.2})
r = run()[0]
check("DNS 给的全都通：摘掉钉的那行，交还给 DNS；别的行还在", r["pin"] == "" and not pinned() and BOX["hosts"] == BASE)
n = BOX["writes"]; run()
check("本来就没钉、也不用钉：不去写 hosts", BOX["writes"] == n)

print("四、全坏 → 换一批；还是全坏 → 不乱钉")
SPEED.clear(); DNS["fresh"] = {E, F}; SPEED[F] = 0.5
r = run()[0]
check("手上的全不通：重新多解析几轮（换一批）再测", 20 in RES["rounds"] and "换了一批" in r["note"])
check("新的一批里有通的：钉上它", r["pin"] == F and pinned() == [f"{F} {HOST} {m.OL_ROUTE_MARK}"])
SPEED.clear()
r = run()[0]
check("换了一批还是全不通：摘掉、不乱钉，报告里说清", r["pin"] == "" and not pinned() and "全都不通" in r["note"] and not r["good"])

print("五、容器重建 / 不在")
SPEED.update({A: 0.4}); DNS["fresh"] = set()
run(); check("先钉上", pinned() == [f"{A} {HOST} {m.OL_ROUTE_MARK}"])
BOX["hosts"] = list(BASE)                      # 容器重建了：hosts 回到原样
r = run()[0]
check("容器重建后钉的那行没了：下一次检查补上", pinned() == [f"{A} {HOST} {m.OL_ROUTE_MARK}"])
BOX["up"] = False
check("OpenList 容器没在跑：什么都不做，不报错", run() == [])
BOX["up"] = True
st = m.ms_state().get("ol_route", {}).get(HOST, {})
check("状态里记着钉的是谁、哪些坏、DNS 给过哪些（一天内的都算它还会给）", st.get("pin") == A and B in st.get("bad", []) and set(st.get("pool", {})) >= {A, B, C})

print("六、挂在哪、怎么跑")
ka = SRC[SRC.index("def do_keepalive"):]; ka = ka[:ka.index("\ndef ", 10)]
check("每小时保活：先选路再列目录；出错不拖累保活", ka.index("ol_route_check(d)") < ka.index("/api/auth/login") and "except Exception:\n        pass" in ka)
hc = SRC[SRC.index("def do_healthcheck"):]
check("「5 链路体检」里有「接口选路」一行，全不通时进问题清单", '"接口选路 "' in hc and "全都连不上" in hc and "一个地址都连不上" in hc)
check("media-stack route 手动跑；帮助里有", 'arg == "route"' in SRC and "\n  route           网盘接口选路" in SRC)
tick = SRC[SRC.index("def do_heal_gate"):]; tick = tick[:tick.index("\ndef ", 10)]
check("没放进开播门（平时一直在跑的路径不添东西）", "ol_route" not in tick)
fn = SRC[SRC.index("def _route_resolve"):SRC.index("def do_route():")]
check("只用本机解析、只连网盘接口自己：不问公共 DNS、不发 HTTP 请求", "getaddrinfo" in fn and not re.search(r"dns-query|urlopen|doh|1\.1\.1\.1|8\.8\.8\.8|223\.5", fn))
check("只测 IPv4（容器里只有 IPv4）；握手要验证书", "socket.AF_INET" in fn and "create_default_context().wrap_socket(raw, server_hostname=host)" in fn)
check("钉的那行的记号不带脚本 / 仓库的名字", not re.search(r"(?i)media|stack|nodekit|bgpeer", m.OL_ROUTE_MARK))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.do_route()
out = plain(buf.getvalue())
check("手动跑的屏：每个地址一行 ✔ / ✖，标出 OpenList 固定用哪个", f"✔ {A}" in out and f"✖ {B}" in out and "← OpenList 固定用它" in out)

up = SRC[SRC.index("def do_update"):]; up = up[:up.index("\ndef ", 10)]
check("「8 更新」完当场查一遍（1.5.410：上一版注释里写了、代码里漏了）", "ol_route_check(d)" in up)

print("六之二、media-stack 命令的壳里也有 route（1.5.410：真机敲下去是「未知命令」）")
shell = SRC[:SRC.index('  *) echo "未知命令:$1。看 media-stack help"')]
check("壳的 case 里有 route)，交给 Python 的 route", re.search(r'\n  route\)\n(?:\s*#.*\n)*\s*S=/etc/bgpeer/media-stack.py\n.*\n\s*exec python3 "\$S" route ;;', shell) is not None)
helps = re.findall(r"(?m)^  ([a-z0-9-]+)\s{2,}\S", SRC[SRC.index("  url             只列出访问地址"):SRC.index("  heal-log [行数]")])
miss = [h for h in helps if not re.search(r"(?m)^  (?:[a-z0-9|-]+\|)?%s(?:\|[a-z0-9|-]+)?\)" % re.escape(h), shell)]
check(f"帮助里列的命令壳里都认（查了 {len(helps)} 条，缺：{miss or '无'}）", "route" in helps and not miss)

print("八、IPv4 全坏时走 IPv6 中转（1.5.411，仓库主人：「用着用着又坏了怎么办呢，都没得选」「两件一起做吧要做就做好」）")
SPEED.clear(); DNS["fresh"] = set(); V6.update(good=[("fd00::1", 0.3)], on_ok=True, reach=True, on=0, off=0)
r = run()[0]
check("IPv4 一个通的都没有、本机 IPv6 通：起中转，容器里把域名钉到假地址上", r["via6"] and r["pin"] == m.OL_ROUTE6_IP and V6["on"] == 1
      and pinned() == [f"{m.OL_ROUTE6_IP} {HOST} {m.OL_ROUTE_MARK}"] and "IPv6 中转" in r["note"])
r = run()[0]
check("下一轮还是全坏：继续走中转；假地址自己不拿去测、不算进坏地址", r["via6"] and m.OL_ROUTE6_IP not in r["bad"] and V6["off"] == 0)
SPEED[A] = 0.6
r = run()[0]
check("IPv4 有一个恢复了：切回 IPv4，中转停掉、规则撤掉", r["pin"] == A and not r["via6"] and V6["off"] == 1 and pinned() == [f"{A} {HOST} {m.OL_ROUTE_MARK}"])
n_off = V6["off"]; run()
check("中转没在用：不反复去停", V6["off"] == n_off)
SPEED.clear(); V6["reach"] = False
r = run()[0]
check("切到中转后从容器里实连不通（比如防火墙挡了）：撤回、不钉着一条死路，说清", r["pin"] == "" and not r["via6"] and not pinned()
      and "也没连通" in r["note"] and V6["off"] == n_off + 1)
V6.update(reach=True, on_ok=False)
r = run()[0]
check("中转起不来（没有 systemctl / iptables）：不钉，说清", r["pin"] == "" and "没起来" in r["note"] and not pinned())
V6.update(good=[], on_ok=True)
r = run()[0]
check("本机也没有 IPv6：跟以前一样报全都不通", r["pin"] == "" and "全都不通" in r["note"])
V6["good"] = [("fd00::1", 0.3)]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.do_route()
check("屏上说清走的是 IPv6 中转", "改走本机的 IPv6 中转" in plain(buf.getvalue()))
SPEED.update({A: 0.4}); run()

for k, v in REAL.items():
    setattr(m, k, v)
IPT = {"have": set(), "cmds": []}
def sh_ipt(cmd, check=False, timeout=None):
    IPT["cmds"].append(cmd)
    mm = re.match(r"iptables -w -t (\w+) -([CID]) (\w+) (.+)$", cmd)
    if mm:
        key = (mm.group(1), mm.group(3), mm.group(4)); op = mm.group(2)
        if op == "C":
            return types.SimpleNamespace(returncode=0 if key in IPT["have"] else 1, stdout="", stderr="")
        (IPT["have"].add if op == "I" else IPT["have"].discard)(key)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd.startswith("docker inspect"):
        return types.SimpleNamespace(returncode=0, stdout="1.0.0.9 \n", stderr="")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
m.sh = sh_ipt
m.shutil = types.SimpleNamespace(which=lambda b: "/usr/sbin/" + b)
check("网关地址从 docker inspect 读", m._ol_gateway() == "1.0.0.9")
check("装规则：假地址:443 → DNAT 到网关上的中转端口；放行容器连中转", m._route6_rules("1.0.0.9", True) and IPT["have"] == {
      ("nat", "PREROUTING", f"-d {m.OL_ROUTE6_IP} -p tcp --dport 443 -j DNAT --to-destination 1.0.0.9:{m.OL_ROUTE6_PORT}"),
      ("filter", "INPUT", f"-d 1.0.0.9 -p tcp --dport {m.OL_ROUTE6_PORT} -j ACCEPT")})
n = len([c for c in IPT["cmds"] if " -I " in c]); m._route6_rules("1.0.0.9", True)
check("重复装不会叠（先 -C 查有没有）", len([c for c in IPT["cmds"] if " -I " in c]) == n)
m._route6_rules("1.0.0.9", False)
check("撤规则：两条都删干净", not IPT["have"])
check("假地址在测试用网段（公网上不会有）；中转只听网桥网关地址", m.OL_ROUTE6_IP.startswith("198.18.") and "srv.bind((gw, OL_ROUTE6_PORT))" in SRC)

import socket, threading
# 真连接测中转搬字节。会话容器里没有 IPv6 回环，上游用本机回环顶替（中转只管「连上去、两头对搬」，跟地址族无关）
up_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM); up_srv.bind(("127.0.0.1", 0)); up_srv.listen(4)
def upstream():
    c, _ = up_srv.accept(); data = c.recv(100); c.sendall(b"echo:" + data); c.close()
threading.Thread(target=upstream, daemon=True).start()
real_gai = socket.getaddrinfo
ASKED = []
m.socket.getaddrinfo = lambda host, port, fam=0, typ=0, *a: (ASKED.append((host, port, fam)), [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", up_srv.getsockname()[1]))])[1]
a_sock, b_sock = socket.socketpair()
threading.Thread(target=m._relay_serve, args=(b_sock, "relay.example"), daemon=True).start()
a_sock.sendall(b"hello"); a_sock.settimeout(5)
got = b""
try:
    while len(got) < 10:
        ch = a_sock.recv(100)
        if not ch:
            break
        got += ch
except OSError:
    pass
m.socket.getaddrinfo = real_gai
check("中转真搬字节：这头进来的送到那头，回来的原样送回（不看内容）", got == b"echo:hello")
check("中转去连的是那个域名的 443、只要 IPv6 地址", ASKED[:1] == [("relay.example", 443, socket.AF_INET6)])
check("中转只连 OL_ROUTE_HOSTS 里的域名（不是谁让连哪就连哪）", "args=(cli, OL_ROUTE_HOSTS[0])" in SRC)

print("九、每 5 分钟查一次 + 卸载撤干净")
check("单独一条 cron：每 5 分钟调 route-tick，带超时", m.ROUTE_EVERY_MIN == 5 and "*/{ROUTE_EVERY_MIN} * * * * root {cron_cmd('route-tick')}" in SRC
      and m.CRON_TIMEOUT.get("route-tick", 999) < 300)
check("route-tick 抢到锁才跑（上一轮没完不叠）", re.search(r'arg == "route-tick":.*\n\s+if take_task_lock\("route"\):', SRC) is not None)
check("安装和「8 更新」都会装这条 cron", SRC.count("install_route_cron()") >= 3)
un = SRC[SRC.index("KEEPALIVE_CRON, SYNC_CRON, WARM_CRON"):]; un = un[:un.index("jobq_cleanup_if_unused()")]
check("卸载：cron 文件删掉、IPv6 中转和规则撤掉", "ROUTE_CRON" in un and "_route6_off()" in un)
check("没放进每分钟那一轮和开播门（它自己一条 cron）", "ol_route" not in SRC[SRC.index("def do_heal_gate"):SRC.index("def do_heal_gate") + 6000])

print("十、阿里盘和 115 也查；只查挂着的盘（1.5.412，仓库主人：「那就把阿里盘和115也加上吧」）")
m.sh = fake_sh; m.subprocess = types.SimpleNamespace(run=fake_run)
m._route6_addrs = lambda host: []; m._route6_on = lambda: ""; m._route6_off = lambda: None; m._ol_can_reach = lambda host: True
check("域名表（照 OpenList 驱动源码核对的）：夸克 / 阿里 / 115 列目录 / 115 换链", m.OL_ROUTE_HOSTS == (
      "open-api-drive.quark.cn", "openapi.alipan.com", "webapi.115.com", "proapi.115.com"))
check("IPv6 中转还是只给夸克（第一项）", m.OL_ROUTE_HOSTS[0] == "open-api-drive.quark.cn")
check("只挂夸克的机器：只查夸克，不去连阿里 / 115", m._route_hosts(D) == ["open-api-drive.quark.cn"])
MOUNTS[:] = [("/quark", "QuarkTV", "work", "0", ""), ("/aliyun", "AliyundriveOpen", "work", "root", "alipanTV"),
             ("/115", "115 Cloud", "work", "0", ""), ("/dav", "WebDav", "work", "", "")]
check("夸克、阿里、115 都挂着：四个域名都查；WebDAV 不查", m._route_hosts(D) == list(m.OL_ROUTE_HOSTS))
BOX["hosts"] = list(BASE); SPEED.clear(); SPEED.update({A: 0.3, C: 0.5}); DNS["normal"] = {A, B, C}; DNS["fresh"] = set()
PER = {"openapi.alipan.com": {A, B}, "webapi.115.com": {C}, "proapi.115.com": {A, C}, HOST: {A, C}}
m._route_resolve = lambda host, rounds=6: set(PER.get(host, set()))
m.save_ms_state(ol_route={})
rep_ = {r["host"]: r for r in run()}
check("每个域名各管各的：阿里有一个地址不通 → 只给阿里钉；别的都通 → 不钉",
      rep_["openapi.alipan.com"]["pin"] == A and all(rep_[h]["pin"] == "" for h in (HOST, "webapi.115.com", "proapi.115.com"))
      and pinned() == [f"{A} openapi.alipan.com {m.OL_ROUTE_MARK}"])
check("报告里带中文名，体检和屏上用它", [rep_[h]["name"] for h in m.OL_ROUTE_HOSTS] == ["夸克", "阿里", "115 列目录", "115 换链"])
MOUNTS[:] = [("/quark", "QuarkTV", "work", "0", "")]
run()
check("阿里盘撤掉以后：给阿里钉的那行跟着摘掉", not pinned())
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.do_route()
check("手动跑的屏：标题是中文名 + 域名", "▸ 夸克  open-api-drive.quark.cn" in plain(buf.getvalue()))
MOUNTS[:] = [("/dav", "WebDav", "work", "", "")]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.do_route()
check("只挂了 WebDAV：说清没查，不报错", "没查" in plain(buf.getvalue()))

print("十一、要挂一个新盘之前先选路（1.5.427，真机：重装后夸克扫码两次「没拿到登录二维码：网盘接口超时」）")
m.sh = fake_sh; m.subprocess = types.SimpleNamespace(run=fake_run)
m._route_resolve = lambda host, rounds=6: {A, B} if host == HOST else set()
SPEED.clear(); SPEED.update({A: 0.3})                   # B 连不上
BOX["hosts"] = list(BASE); m.save_ms_state(ol_route={})
MOUNTS[:] = [("/dav", "WebDav", "work", "", "")]        # 夸克还没挂
r = m.ol_route_check(D)
check("夸克没挂：平常那一轮不查它", r == [] and not pinned())
m.ol_route_before_mount(D, "quark")
check("要挂夸克之前：照样查它，坏的避开、钉上通的那个", pinned() == [f"{A} {HOST} {m.OL_ROUTE_MARK}"])
m._route_resolve = lambda host, rounds=6: (_ for _ in ()).throw(RuntimeError("boom"))
m.ol_route_before_mount(D, "quark")
check("查的时候出错：不拦着挂（不抛出来）", True)
for flow, key in (("_add_qtv_flow", "quark"), ("_add115_flow", "115"), ("_add_ali_flow", "aliyun")):
    fn = SRC[SRC.index(f"def {flow}"):]; fn = fn[:fn.index("\ndef ", 10)]
    check(f"{flow}：挂之前先 ol_route_before_mount(d, \"{key}\")", f'ol_route_before_mount(d, "{key}")' in fn)
fq = SRC[SRC.index("def _add_qtv_flow"):]; fq = fq[:fq.index("\ndef ", 10)]
check("夸克：在建存储（要二维码）之前选路", fq.index('ol_route_before_mount(d, "quark")') < fq.index("/api/admin/storage/create"))

print("十二、慢的地址也要避开（1.5.430，真机：选路屏上全是 ✔、握手 2～4 秒，夸克要二维码照样超时）")
m.sh = fake_sh; m.subprocess = types.SimpleNamespace(run=fake_run)
m._route_resolve = lambda host, rounds=6: {A, B, C} if host == HOST else set()
MOUNTS[:] = [("/quark", "QuarkTV", "work", "0", "")]
BOX["hosts"] = list(BASE); m.save_ms_state(ol_route={})
SPEED.clear(); SPEED.update({A: 0.4, B: 2.6, C: 3.9})       # 全都连得通，但两个很慢
r = run()[0]
check("全都连得通、但有慢的：钉最快的那个（OpenList 要二维码只给 5 秒）", r["pin"] == A and sorted(r["slow"]) == [B, C] and not r["bad"])
SPEED.update({A: 1.9, B: 0.5})                              # 钉着的那个变慢了，另一个变快了
r = run()[0]
check("钉着的那个变慢了：换成快的", r["pin"] == B)
SPEED.update({B: 0.6, A: 0.5})
r = run()[0]
check("钉着的还不慢：别人只快一点点也不换（别来回跳）", r["pin"] == B)
SPEED.clear(); SPEED.update({A: 0.4, B: 0.5, C: 0.9})
r = run()[0]
check("全都又快又通：摘掉，交还给 DNS", r["pin"] == "" and not pinned())
SPEED.clear(); SPEED.update({A: 2.0, B: 3.0})
r = run()[0]
check("全都慢：照样钉最快的那个（比随机抽强）", r["pin"] == A)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.do_route()
check("手动跑的屏：慢的标 ⚠ 慢", "⚠ " + B in plain(buf.getvalue()) and "慢" in plain(buf.getvalue()))
fq = SRC[SRC.index("def _add_qtv_flow"):]; fq = fq[:fq.index("\ndef ", 10)]
check("夸克要二维码超时：撤掉、重新选路、自动再试一次（只再试一次）", "for _try in range(2):" in fq and "换一个快的接口地址再试一次" in fq
      and fq.index("_ol_drop_storage(d, mount, tok)") < fq.index('ol_route_before_mount(d, "quark")\n    if r.get'))

print("七、版本号")
check("media-stack 1.5.430 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 430))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
