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
fn = SRC[SRC.index("def _route_resolve"):SRC.index("def do_route")]
check("只用本机解析、只连网盘接口自己：不问公共 DNS、不发 HTTP 请求", "getaddrinfo" in fn and not re.search(r"dns-query|urlopen|doh|1\.1\.1\.1|8\.8\.8\.8|223\.5", fn))
check("只测 IPv4（容器里只有 IPv4）；握手要验证书", "socket.AF_INET" in fn and "create_default_context().wrap_socket(raw, server_hostname=host)" in fn)
check("钉的那行的记号不带脚本 / 仓库的名字", not re.search(r"(?i)media|stack|nodekit|bgpeer", m.OL_ROUTE_MARK))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.do_route()
out = plain(buf.getvalue())
check("手动跑的屏：每个地址一行 ✔ / ✖，标出 OpenList 固定用哪个", f"✔ {A}" in out and f"✖ {B}" in out and "← OpenList 固定用它" in out)

print("七、版本号")
check("media-stack 1.5.409 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 409))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
