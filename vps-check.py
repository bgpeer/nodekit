#!/usr/bin/env python3
# ============================================================================
# vps-check.py —— VPS 线路（三网回程）与 IP 纯净度体检
# ----------------------------------------------------------------------------
# 设计上的两条硬原则：
#   1. 能查权威数据就不猜。逐跳 ASN 走 Team Cymru 的 DNS whois（免密钥、不限速），
#      比匹配 IP 前缀可靠。
#   2. 查不准就明说，不给"自信但错误"的结论。GIA/GT 的细分本脚本【不下结论】，
#      只把证据（59.43 跳、跳数、延迟）摆出来——理由见文件末尾「局限」。
# ============================================================================
import json, os, re, socket, struct, random, subprocess, sys, time, urllib.request, concurrent.futures as cf

C = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "c": "\033[36m",
     "b": "\033[1m", "d": "\033[2m", "n": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}

# 上次「本机检测」的结果落这儿，给 xy-installer 的菜单直接读出来显示。
# 只存本机的：外部 IP 检测查的是别人的 IP，跟这台机器的线路无关，存了会误导。
LAST_FILE = ("/etc/bgpeer/vps-check.last.json" if os.path.isdir("/etc/bgpeer")
             else os.path.expanduser("~/.vps-check.last.json"))

def hr(ch="─", n=68): print(C["d"] + ch * n + C["n"])
def title(t):
    print(f"\n{C['b']}{C['c']}{t}{C['n']}"); hr()

# ---------------------------------------------------------------- 最小 DNS 客户端
# 不依赖 dig / dnspython：VPS 上少装一个包就少一个变数。
def _sys_resolvers():
    """系统解析器优先——Spamhaus 等黑名单会【拒绝】来自公共 DNS 的查询，
       用系统自己的解析器命中率才正常。取不到再退回公共的。"""
    out = []
    try:
        for l in open("/etc/resolv.conf"):
            m = re.match(r"\s*nameserver\s+(\d+\.\d+\.\d+\.\d+)", l)
            if m and not m.group(1).startswith("127."):
                out.append(m.group(1))
    except OSError:
        pass
    return out or ["1.1.1.1", "8.8.8.8"]

def _dns(name, qtype, server, timeout=3.0):
    tid = random.randint(0, 0xFFFF)
    pkt = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for part in name.rstrip(".").split("."):
        pkt += bytes([len(part)]) + part.encode()
    pkt += b"\x00" + struct.pack("!HH", qtype, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(timeout)
    try:
        s.sendto(pkt, (server, 53)); data, _ = s.recvfrom(4096)
    except OSError:
        return None                                   # None = 查询失败（区别于「查到但没记录」）
    finally:
        s.close()
    if len(data) < 12 or struct.unpack("!H", data[:2])[0] != tid:
        return None
    qd, an = struct.unpack("!HH", data[4:8]); i = 12
    def skip(j):
        while j < len(data):
            l = data[j]
            if l == 0: return j + 1
            if l & 0xC0 == 0xC0: return j + 2
            j += 1 + l
        return j
    for _ in range(qd): i = skip(i) + 4
    out = []
    for _ in range(an):
        i = skip(i)
        if i + 10 > len(data): break
        rt, _, _, rl = struct.unpack("!HHIH", data[i:i+10]); i += 10
        rd = data[i:i+rl]; i += rl
        if rt == 16 and qtype == 16 and rd:
            out.append(rd[1:1+rd[0]].decode("utf-8", "replace"))
        elif rt == 1 and qtype == 1 and rl == 4:
            out.append(socket.inet_ntoa(rd))
    return out

def dns_txt(n, server="1.1.1.1"): return _dns(n, 16, server)
def dns_a(n, server):             return _dns(n, 1, server)
def _rev(ip):                     return ".".join(reversed(ip.split(".")))

_ASN_CACHE = {}
def asn_of(ip):
    """(asn, 前缀, 国家, AS名)。查不到返回 (None,"","","")。

       一个 IP 可能落在【多条】origin 记录里（同一段被不同 AS 以不同掩码宣告），
       Cymru 每条一个 TXT，顺序还不固定——取第一条会导致同一个 IP 两次查出不同
       结果（实测 202.103.24.68 一会儿 AS4134/18、一会儿 AS137266/20）。
       按 BGP 语义取【最长前缀】那条：最具体的宣告才是实际生效的。"""
    if ip in _ASN_CACHE: return _ASN_CACHE[ip]
    recs = dns_txt(f"{_rev(ip)}.origin.asn.cymru.com") or []
    best, best_len = None, -1
    for rec in recs:
        f = [x.strip() for x in rec.split("|")]
        if len(f) < 3: continue
        try: plen = int(f[1].split("/")[1])
        except (IndexError, ValueError): plen = 0
        if plen > best_len:
            best, best_len = f, plen
    res = (None, "", "", "")
    if best:
        asn = best[0].split()[0] if best[0].split() else None
        name = ""
        if asn:
            r2 = dns_txt(f"AS{asn}.asn.cymru.com")
            if r2:
                g = [x.strip() for x in r2[0].split("|")]
                name = g[4] if len(g) > 4 else ""
        res = (asn, best[1], best[2], name)
    _ASN_CACHE[ip] = res
    return res

def as_name(asn):
    r = dns_txt(f"AS{asn}.asn.cymru.com")
    if not r: return ""
    g = [x.strip() for x in r[0].split("|")]
    return g[4] if len(g) > 4 else ""

def _asn_short(name):
    """Cymru 的 AS 名字是 "IIJ - Internet Initiative Japan Inc., JP"，
       破折号前那截才是简称，够认人了，后面一长串只会把行撑爆。"""
    return name.split(" - ")[0].strip()[:18]

def peers_of(ip, cap=4):
    """这个 IP 所在前缀的【邻居 AS】（Cymru peer 记录）→ [(asn, 名字)]。
       跟 asn_of 一样按最长前缀取——同一个 IP 可能落在多条记录里。

       ⚠ 这【查不出 CN2】。实测：一台确认走 CN2 GIA 的 DMIT 机器
       （69.63.210.0/24），邻居只有 AS2914(NTT) / AS3257(GTT) / AS137409，
       没有 4809 也没有 4134。原因是 CN2 是私有电路、59.43 根本不在 BGP
       全局表里，商家把出境流量交给哪条电路是转发决策，全局路由表看不见。
       所以这行只当【上游构成】看（NTT/GTT/Cogent/HE 之类），不能拿来判线路。"""
    recs = dns_txt(f"{_rev(ip)}.peer.asn.cymru.com") or []
    best, best_len = None, -1
    for rec in recs:
        f = [x.strip() for x in rec.split("|")]
        if len(f) < 2: continue
        try: plen = int(f[1].split("/")[1])
        except (IndexError, ValueError): plen = 0
        if plen > best_len:
            best, best_len = f, plen
    if not best: return []
    origin = (asn_of(ip)[0] or "")            # peer 记录里会带上源 AS 自己，去掉
    out = [a for a in best[0].split() if a != origin]
    return [(a, _asn_short(as_name(a))) for a in out[:cap]]

# ---------------------------------------------------------------- 骨干判定
# ⚠ 59.43/202.97 必须按【前缀】认，不能按 ASN：
#    59.43.0.0/16 是 CN2 的内部传输网，【不在 BGP 全局路由表里】，Cymru 查不到 AS。
#    （实测 59.43.0.1 → 无结果；而 202.97.0.1 → AS4134 CHINANET-BACKBONE。）
# 第三项是这条骨干【属于哪家】——判定必须按目标运营商挑本网骨干，
# 否则从 CN2 机器出去，去联通/移动的路径头几跳也全是 59.43，
# 会把三家都标成「电信 CN2」（实测就是这个现象）。
# 第四项是【国际段 intl / 国内落地网 domestic】。
# 为什么要分：能定性一条线路好坏的是【国际段】走哪条（CN2 还是 163、9929 还是
# 169、CMIN2 还是 CMI）。国内落地网是到达那家用户必经的，跟线路档次无关。
# 移动尤其要分——CMNET(AS9808) 是移动【国内】网，从境外去移动的路径末尾必然
# 进它，而国际段是另外两个 AS（CMIN2 58807 / CMI 58453）。不分的话「取最后一个
# 本网骨干」永远落到 CMNET，把真正该看的国际段盖掉（实测就是这个现象：8 个点
# 全标 CMNET，看不出到底是 CMIN2 还是 CMI）。
# 电信 163(AS4134) 和联通 169(AS4837) 国际国内共用一个 AS，分不开，仍按国际段算。
BACKBONE_AS = {
    "4809":  ("电信 CN2",        "premium", "电信", "intl"),
    "4134":  ("电信 163",        "normal",  "电信", "intl"),
    "9929":  ("联通 9929",       "premium", "联通", "intl"),
    "4837":  ("联通 169",        "normal",  "联通", "intl"),
    "58807": ("移动 CMIN2",      "premium", "移动", "intl"),
    "58453": ("移动 CMI",        "normal",  "移动", "intl"),
    "9808":  ("移动 CMNET",      "normal",  "移动", "domestic"),
    "4847":  ("电信 CNIX",       "normal",  "电信", "intl"),
}
def classify_hop(ip):
    """→ (标签, 等级, 归属, 范围)。等级：premium / normal / ""；范围：intl / domestic / "" """
    if ip.startswith("59.43."):
        return "电信 CN2 (59.43)", "premium", "电信", "intl"
    if ip.startswith("202.97."):
        return "电信 163 (202.97)", "normal", "电信", "intl"
    asn, _, cc, name = asn_of(ip)
    if asn and asn in BACKBONE_AS:
        lab, lvl, car, scope = BACKBONE_AS[asn]
        return f"{lab} (AS{asn})", lvl, car, scope
    if asn:
        return f"AS{asn} {name[:34]}", "", "", ""
    return "", "", "", ""

# ---------------------------------------------------------------- 回程路由
# 三网常用测试点。⚠ 不盲信：跑之前先查每个目标的真实 ASN/国家，
#    对不上就标成「目标存疑」并跳过，宁可少测也不给错结论。
# 归属校验：按 Cymru 返回的 AS【名称】判，不枚举 ASN——省级子 AS 太多列不全，
# 而且实测同一家会冒出 CHINANET-BACKBONE / CHINATELECOM-HUBEI-… / CHINA TELECOM
# 好几种写法。\s* 是为了同时吃 "CHINA TELECOM" 和 "CHINATELECOM"。
CARRIER_PAT = {
    "电信": r"CHINANET|CHINA\s*TELECOM|CNIX",
    "联通": r"CHINA169|CNCGROUP|CHINA\s*UNICOM|CUII|UNICOM",
    "移动": r"CMNET|CHINA\s*MOBILE",
}
# 8 城 × 三网 = 24 个点。每个都用上面的模式实测校验过归属（见 README）。
TARGETS = [
    ("电信", "北京", "219.141.136.12"),  ("电信", "上海", "202.96.209.133"),
    ("电信", "莆田", "218.85.152.99"),   ("电信", "深圳", "202.96.134.133"),
    ("电信", "南宁", "202.103.224.68"),  ("电信", "成都", "61.139.2.69"),
    ("电信", "西安", "218.30.19.40"),    ("电信", "武汉", "202.103.24.68"),
    ("联通", "北京", "202.106.50.1"),    ("联通", "上海", "210.22.97.1"),
    ("联通", "莆田", "218.104.128.106"), ("联通", "深圳", "221.5.88.88"),
    ("联通", "南宁", "221.7.128.68"),    ("联通", "成都", "119.6.6.6"),
    ("联通", "西安", "221.11.1.67"),     ("联通", "武汉", "218.104.111.114"),
    ("移动", "北京", "221.179.155.161"), ("移动", "上海", "211.136.112.200"),
    ("移动", "莆田", "211.138.151.161"), ("移动", "深圳", "120.196.212.25"),
    ("移动", "南宁", "211.138.245.180"), ("移动", "成都", "211.137.96.205"),
    ("移动", "西安", "211.137.130.3"),   ("移动", "武汉", "211.137.58.20"),
]
CITIES   = ["北京", "上海", "莆田", "深圳", "南宁", "成都", "西安", "武汉"]
CARRIERS = ["电信", "联通", "移动"]

def have(b): 
    from shutil import which; return which(b) is not None

def ensure_traceroute():
    if have("traceroute") or have("mtr"): return True
    print(f"  {C['y']}未装 traceroute，尝试安装…{C['n']}")
    for cmd in ("apt-get update -y && apt-get install -y traceroute",
                "yum install -y traceroute", "apk add --no-cache traceroute"):
        subprocess.run(cmd, shell=True, capture_output=True)
        if have("traceroute"): return True
    return False

def traceroute(ip, maxhop=30, timeout=120):
    """→ [(hop, ip, ms)]；ip 为 None 表示该跳无响应。

       两种工具输出格式不同，必须分开解析：
         traceroute:  ` 3  59.43.186.1  142.331 ms`      延迟带 ms 后缀
         mtr -r -c1:  `  3.|-- 59.43.186.1  0.0%  1  142.3 142.3 ...`
                      跳号后是 `.|--`，且【没有 ms 后缀】，
                      IP 之后依次是 Loss% / Snt / Last…，Last 才是本跳延迟。"""
    if have("traceroute"):
        cmd, tool = f"traceroute -n -q 1 -w 1 -m {maxhop} {ip}", "traceroute"
    elif have("mtr"):
        cmd, tool = f"mtr -n -r -c 1 -m {maxhop} {ip}", "mtr"
    else:
        return []
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    hops = []
    for line in r.stdout.splitlines():
        m = re.match(r"\s*(\d+)[.|\s-]+(.*)", line)          # 同时吃 `1  ` 和 `1.|-- `
        if not m: continue
        n, rest = int(m.group(1)), m.group(2)
        ipm = re.search(r"(\d+\.\d+\.\d+\.\d+)", rest)
        hip = ipm.group(1) if ipm else None
        ms = None
        if hip:
            if tool == "traceroute":
                msm = re.search(r"([\d.]+)\s*ms", rest)
                ms = float(msm.group(1)) if msm else None
            else:                                            # mtr：IP 之后第 3 个数字是 Last
                nums = re.findall(r"[\d.]+", rest[rest.index(hip) + len(hip):])
                if len(nums) >= 3:
                    try: ms = float(nums[2])
                    except ValueError: pass
        hops.append((n, hip, ms))
    return hops

# ---------------------------------------------------------------- IP 画像
def http_json(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vps-check"})
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except Exception:
        return None

def my_ip():
    for u in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "vps-check"})
            ip = urllib.request.urlopen(req, timeout=8).read().decode().strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip): return ip
        except Exception:
            continue
    return None

def ip_profile(ip):
    """ip-api.com 免密钥，直接给 hosting/proxy/mobile 三个标记——
       这几个标记正是「纯净度」里最硬的部分（是否被识别为机房/代理出口）。"""
    d = http_json(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,"
                  f"isp,org,as,asname,reverse,mobile,proxy,hosting")
    return d if d and d.get("status") == "success" else None

# ---------------------------------------------------------------- 黑名单
# 只收【免费、允许普通解析器查询】的表。Spamhaus 留着但会识别「拒答」，
# 因为它对公共 DNS 返回 127.255.255.x 而不是空——当成「干净」就是误报。
DNSBLS = [
    ("Spamhaus ZEN",   "zen.spamhaus.org"),
    ("SpamCop",        "bl.spamcop.net"),
    ("Barracuda",      "b.barracudacentral.org"),
    ("SORBS",          "dnsbl.sorbs.net"),
    ("PSBL",           "psbl.surriel.com"),
    ("UCEPROTECT-1",   "dnsbl-1.uceprotect.net"),
    ("Abuseat CBL",    "cbl.abuseat.org"),
    ("Mailspike",      "bl.mailspike.net"),
    ("Backscatterer",  "ips.backscatterer.org"),
    ("Interserver",    "rbl.interserver.net"),
]
def bl_alive(zone, resolver):
    """用各表【官方测试地址 127.0.0.2】探活：它必然被列。

       为什么非探不可：Spamhaus 这类会拒绝来自公共 DNS 的查询，而且拒绝的方式是
       【返回空】而不是报错——直接把空当成「干净」就是假阴性，等于告诉用户
       「你的 IP 没被拉黑」，实际上根本没查成。实测 8.8.8.8 查 Spamhaus 就是这样。"""
    r = dns_a(f"2.0.0.127.{zone}", resolver)
    return bool(r) and not any(x.startswith("127.255.255.") for x in r)

def check_bl(ip, name, zone, resolver):
    if not bl_alive(zone, resolver):
        return (name, "unknown", "列表不可用/拒答本解析器")
    r = dns_a(f"{_rev(ip)}.{zone}", resolver)
    if r is None:
        return (name, "unknown", "查询超时/失败")
    if not r:
        return (name, "clean", "")
    if any(x.startswith("127.255.255.") for x in r):   # 超额/策略拒答，不是命中
        return (name, "unknown", f"列表拒答({r[0]})")
    return (name, "listed", ",".join(r))

# ---------------------------------------------------------------- 可达性
SERVICES = [("Google", "www.google.com"), ("YouTube", "www.youtube.com"),
            ("ChatGPT", "chatgpt.com"), ("Netflix", "www.netflix.com"),
            ("Disney+", "www.disneyplus.com"), ("GitHub", "github.com")]
def reach(host, port=443, timeout=5):
    import time
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout); s.close()
        return round((time.time() - t0) * 1000)
    except Exception:
        return None

# ---------------------------------------------------------------- 报告
def report_ip(ip, num="一", local=True):
    title(f"{num}、{'本机' if local else '目标'} IP 画像")
    asn, pfx, cc, name = asn_of(ip)
    print(f"  IP        : {C['b']}{ip}{C['n']}")
    print(f"  ASN       : {'AS'+asn if asn else '查不到'}  {name}")
    print(f"  宣告前缀   : {pfx or '-'}    注册地: {cc or '-'}")
    peers = peers_of(ip)
    if peers:
        print(f"  上游/邻居  : " + "、".join(f"AS{a} {n}" for a, n in peers)
              + f"   {C['d']}（查不到 CN2，见末尾局限）{C['n']}")
    p = ip_profile(ip)
    if not p:
        print(f"  {C['y']}ip-api.com 拿不到画像（限流或无外网），跳过标记检测{C['n']}")
        return
    print(f"  地理       : {p.get('country','')} {p.get('regionName','')} {p.get('city','')}")
    print(f"  ISP/组织   : {p.get('isp','')} / {p.get('org','') or '-'}")
    if p.get("reverse"): print(f"  rDNS      : {p['reverse']}")
    hr("·")
    flags = [("hosting", "机房/托管 IP", "被识别为数据中心出口——流媒体/风控更容易拦"),
             ("proxy",   "代理/VPN 标记", "已被标成代理出口，纯净度差"),
             ("mobile",  "移动网络标记", "")]
    worst = 0
    for k, lab, why in flags:
        v = bool(p.get(k))
        if k == "mobile" and not v: continue
        col = C["r"] if (v and k == "proxy") else (C["y"] if v else C["g"])
        print(f"  {lab:<12}: {col}{'是' if v else '否'}{C['n']}" + (f"   {C['d']}{why}{C['n']}" if v and why else ""))
        if v and k == "proxy": worst = max(worst, 2)
        elif v and k == "hosting": worst = max(worst, 1)
    return worst

def _short(lab):
    """备注里用短名，"电信 CN2 (59.43)" → "电信 CN2"。手机上一行放不下会折行。"""
    return lab.split(" (")[0]

def route_verdict(hops, car):
    """→ (长途段标签, 等级, 该跳延迟, 落地标签)。

       决定一条回程好坏的是【长途段】——把流量从境外拉回中国的那一段骨干。
       落地进了哪家的网是另一件事，单独列（`→ 联通 169`），不参与档次判定。

       长途段怎么挑：**优质骨干出现在路径的任何位置就算优质**，不是「第一个骨干跳」。
       实测踩的坑：DMIT LA 的路径里，进 59.43 之前还有一跳属于 AS4134（中国电信
       在美国那侧的接入），按「第一个」取就把整条 CN2 GIA 判成了「163 普通」——
       24 个点一个优质都没有，而落地那列明晃晃写着 `→ 电信 CN2`，自相矛盾。
       59.43 / 9929 / CMIN2 只要在路径上出现过，这条长途就是走的它们。

       没有优质骨干时，普通骨干取【第一次出现】而不是最后一次：到移动的路径
       末尾必然是 CMNET(AS9808，移动国内网)，取最后一次就永远是 CMNET，把前面
       真正的国际段（CMI/CMIN2）盖掉。国内落地网干脆不进长途段候选。
       同为普通时优先目标运营商自己的骨干——去联通的路径描述成「联通 169」
       比描述成路上蹭过的「电信 163」贴切。

       这个函数前后翻过三次车，都是把【长途段】和【落地】混成一件事：
       1. 取第一个 premium 跳、安到目标运营商头上 → 输出「联通 → 电信 CN2」，
          读起来像「联通的骨干是 CN2」，是胡话。
       2. 矫枉过正，只认目标运营商自己的骨干 → 三网回程 GIA 的机器联通被判成
          「联通 169 普通」，把人家真正的卖点判没了。
       3. 改取「第一个能认出的骨干跳」 → 被 CN2 前面那跳 AS4134 骗了，全判成 163。

       延迟取【最后一个有响应的跳】而不是长途段入口那跳：入口跳只是刚进骨干，
       拿它比三家等于比谁家入口近，没意义；最后一跳约等于到目的地的 RTT，
       三家之间才可比。路径被截断时它是个下限。"""
    prem = own_norm = any_norm = land = end_ms = None
    for _, hip, ms in hops:
        if not hip:
            continue
        if ms is not None:
            end_ms = ms                          # 一路刷到最后一个有响应的跳
        lab, lvl, hcar, scope = classify_hop(hip)
        # 只认已知骨干（premium/normal）。classify_hop 对认不出的 AS 会回一个
        # "AS#### 名字"、等级为空——那多半是【目的地自己的省级接入网】
        # （如 AS4835 CHINANET-IDC-SN），把它当骨干会把判定带偏。
        if lvl not in ("premium", "normal"):
            continue
        if hcar == car:
            land = _short(lab)                   # 目标本网，取最后一跳 = 落地
        if scope == "domestic":
            continue                             # 国内落地网不做长途段候选
        if lvl == "premium":
            if prem is None:
                prem = (lab, lvl)
        else:
            if hcar == car and own_norm is None:
                own_norm = (lab, lvl)
            if any_norm is None:
                any_norm = (lab, lvl)
    haul = prem or own_norm or any_norm
    if haul is None:
        return ("未识别", "", end_ms, land or "")
    return haul + (end_ms, "" if land == _short(haul[0]) else (land or ""))

def _mark(lvl):
    """判定行的符号+颜色：一眼扫过去就知道好坏。"""
    return {"premium": (C["g"] + C["b"], "●"),
            "normal":  (C["y"], "○")}.get(lvl, (C["d"], "·"))

def run_targets(targets, workers=6):
    """并发跑，边跑边报进度。24 个点串行要七八分钟，并发压到一两分钟。
       返回 {(carrier,city,ip): ("ok", hops, (asn,name)) | ("suspect", asn, name)}"""
    out, done = {}, [0]
    def job(t):
        car, city, ip = t
        asn, _, cc, name = asn_of(ip)
        # 测试点先验归属：注册地必须是 CN、AS 名字必须对得上这家运营商。
        # 对不上就标『存疑』跳过——宁可少测，也不拿一个其实不属于该运营商的
        # 目标去下结论（219.141.136.12 就归在 AS4847 CNIX 而不是 AS4134）。
        if cc != "CN" or not re.search(CARRIER_PAT[car], name or "", re.I):
            r = ("suspect", asn, name)
        else:
            r = ("ok", traceroute(ip), (asn, name))
        done[0] += 1
        if sys.stdout.isatty():                 # 非 tty 下 \r 不生效，会刷出一堆行
            print(f"\r  进度 {done[0]}/{len(targets)}  ", end="", flush=True)
        return t, r
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for t, r in ex.map(job, targets):
            out[t] = r
    if sys.stdout.isatty():
        print("\r" + " " * 40 + "\r", end="")
    return out

def report_routes(num="二"):
    title(f"{num}、三网回程路由（本机 → 中国）")
    if not ensure_traceroute():
        print(f"  {C['r']}没有 traceroute/mtr 且装不上，跳过本节{C['n']}"); return None
    targets = TARGETS
    print(f"  {C['d']}{len(targets)} 个测试点（{len(CITIES)} 城 × 三网），并发跑，约 2 分钟。{C['n']}")
    print(f"  {C['d']}判定看【长途段】——把流量从境外拉回中国的那一段骨干，它决定延迟和拥塞；{C['n']}")
    print(f"  {C['d']}后面的『→ xxx』是落地进了哪家的网，延迟取最后一个有响应的跳（≈到目的地 RTT）。{C['n']}")
    res = run_targets(targets)

    verdicts = {}
    for car in CARRIERS:
        rows = [(t[1], t[2], res[t]) for t in targets if t[0] == car]
        if not rows: continue
        print(f"\n  {C['b']}{C['c']}{car}{C['n']}")
        hr("·")
        for city, ip, r in rows:
            if r[0] == "suspect":
                print(f"    {C['y']}? {city:<4} 目标存疑（AS{r[1] or '?'} {(r[2] or '')[:24]}），已跳过{C['n']}")
                verdicts.setdefault(car, []).append((city, "目标存疑", "skip"))
                continue
            hops, (asn, name) = r[1], r[2]
            if not hops:
                print(f"    {C['r']}✗ {city:<4} 无响应（ICMP/UDP 被拦或不可达）{C['n']}")
                verdicts.setdefault(car, []).append((city, "无响应", "fail"))
                continue
            lab, lvl, ms, land = route_verdict(hops, car)
            col, sym = _mark(lvl)
            lat = f"{ms:.0f} ms" if ms else ""
            print(f"    {col}{sym} {city:<4} {lab:<22}{C['n']}"
                  f"{C['d']}{('→ ' + land) if land else '':<16}{lat:>9}{C['n']}")
            verdicts.setdefault(car, []).append((city, lab, lvl))
    return verdicts

def report_bl(ip, num="三"):
    title(f"{num}、IP 黑名单（DNSBL）")
    resolver = _sys_resolvers()[0]
    print(f"  {C['d']}用系统解析器 {resolver} 查询——Spamhaus 等会拒绝来自公共 DNS 的查询{C['n']}")
    hr("·")
    listed, unknown = [], []
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for name, st, detail in ex.map(lambda t: check_bl(ip, t[0], t[1], resolver), DNSBLS):
            if st == "listed":
                listed.append(name);  print(f"  {C['r']}✗ {name:<16} 命中  {detail}{C['n']}")
            elif st == "unknown":
                unknown.append(name); print(f"  {C['y']}? {name:<16} {detail}{C['n']}")
            else:
                print(f"  {C['g']}✓ {name:<16} 干净{C['n']}")
    hr("·")
    print(f"  命中 {C['r'] if listed else C['g']}{len(listed)}{C['n']} / "
          f"未知 {len(unknown)} / 共 {len(DNSBLS)}")
    if unknown:
        print(f"  {C['d']}「未知」是查询被拒或超时，不代表干净——别当成通过{C['n']}")
    return listed, unknown

def report_reach(num="四"):
    title(f"{num}、常用服务可达性（TCP:443 握手）")
    print(f"  {C['d']}只测能否连上，不代表解锁——解锁要看返回内容和区域判定{C['n']}")
    hr("·")
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for (lab, host), ms in zip(SERVICES, ex.map(lambda s: reach(s[1]), SERVICES)):
            if ms is None: print(f"  {C['r']}✗ {lab:<10} 连不上{C['n']}")
            else:          print(f"  {C['g']}✓ {lab:<10} {ms} ms{C['n']}")

def _conclude_routes(v):
    """把三网判定压成一行一家，看得见差异。"""
    for car in CARRIERS:
        rows = v.get(car)
        if not rows: continue
        prem = [c for c, l, lv in rows if lv == "premium"]
        norm = [c for c, l, lv in rows if lv == "normal"]
        miss = [c for c, l, lv in rows if lv in ("fail", "skip")]
        unk  = [c for c, l, lv in rows if lv == ""]
        labs = sorted({l for _, l, lv in rows if lv == "premium"}) or \
               sorted({l for _, l, lv in rows if lv == "normal"})
        col = C["g"] + C["b"] if prem else (C["y"] if norm else C["d"])
        sym = "●" if prem else ("○" if norm else "·")
        tail = f"优质 {len(prem)} / 普通 {len(norm)}"
        if unk:  tail += f" / 未探到 {len(unk)}"
        if miss: tail += f" / 没测到 {len(miss)}"
        print(f"  {col}{sym} {car}{C['n']}  {col}{'、'.join(labs) or '未识别'}{C['n']}"
              f"   {C['d']}{tail}（共 {len(rows)} 个点）{C['n']}")
        if prem and norm:      # 同一家里既有优质又有普通 → 分城列出来，看得见差异
            print(f"      {C['g']}优质：{'、'.join(prem)}{C['n']}")
            print(f"      {C['y']}普通：{'、'.join(norm)}{C['n']}")

def _flag_line(flag):
    if flag is None:                       # 画像没取到，别把「没查成」说成「无异常」
        return "画像未取到（ip-api 限流或无外网）"
    return ("有 proxy 标记，纯净度差" if flag == 2 else
            "机房 IP（正常现象，流媒体可能受限）" if flag == 1 else "无异常标记")

def save_last(ip, flag, v, listed, unknown):
    """把这次本机检测压成一份小 JSON 落盘，菜单里直接显示，省得每次都要重跑。
       只落本机结果——外部 IP 检测查的是别人的 IP，跟这台机的线路无关。"""
    routes = {}
    for car in CARRIERS:
        rows = v.get(car) if v else None
        if not rows: continue
        prem = [c for c, l, lv in rows if lv == "premium"]
        norm = [c for c, l, lv in rows if lv == "normal"]
        labs = sorted({l for _, l, lv in rows if lv == "premium"}) or \
               sorted({l for _, l, lv in rows if lv == "normal"})
        routes[car] = {"labs": labs, "prem": len(prem), "norm": len(norm),
                       "unk": len([1 for _, _, lv in rows if lv == ""]),
                       "miss": len([1 for _, _, lv in rows if lv in ("fail", "skip")]),
                       "total": len(rows)}
    data = {"ts": int(time.time()), "ip": ip, "flag": flag, "routes": routes,
            "bl_listed": listed, "bl_unknown": len(unknown), "bl_total": len(DNSBLS)}
    try:
        os.makedirs(os.path.dirname(LAST_FILE), exist_ok=True)
        with open(LAST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError as e:
        print(f"  {C['d']}（结果没能写进 {LAST_FILE}：{e}）{C['n']}")

def check_local():
    """本机 IP 检测：画像 + 三网回程 + 黑名单 + 可达性，四节全上。"""
    print(f"\n{C['b']}本机 IP 检测（线路 + 纯净度）{C['n']}")
    ip = my_ip()
    if not ip:
        print(f"{C['r']}拿不到公网 IP，检查外网连通性。{C['n']}"); return 1
    flag = report_ip(ip, "一", local=True)
    v = report_routes("二")
    listed, unknown = report_bl(ip, "三")
    report_reach("四")

    title("五、结论")
    if v: _conclude_routes(v)
    print()
    print(f"  IP 标记   : {_flag_line(flag)}")
    print(f"  黑名单     : " + (f"命中 {len(listed)} 个：{', '.join(listed)}" if listed else "未命中"))
    save_last(ip, flag, v, listed, unknown)
    hr()
    print(f"{C['b']}局限（必须知道，否则会误判）{C['n']}")
    print(f"  1. {C['y']}本脚本不区分 CN2 GIA 和 CN2 GT{C['n']}——两者都走 59.43 段，"
          f"而 59.43.0.0/16\n     不在 BGP 全局路由表里（查不到 AS），"
          f"公开的细分规则我无法核实，硬编码\n     一套没验证过的前缀表只会给你「自信但错误」的结论。"
          f"要定性请用 nexttrace：\n     "
          f"{C['c']}curl -sL nxtrace.org/nt | bash && nexttrace 219.141.136.12{C['n']}")
    print(f"  2. 这里测的是【回程】（VPS→中国）。去程（中国→VPS）本机测不到，"
          f"\n     要在国内侧测。三网的去程和回程经常走【不同】骨干。")
    print(f"  3. 判定看的是【长途段】，不是落地网。一台「三网回程 CN2 GIA」的机器，"
          f"\n     去联通、去移动的长途段也是 59.43——这正是它卖的东西。落地进了哪家的网"
          f"\n     单独列在『→』后面（移动的落地是 CMNET/AS9808，那是移动国内网，"
          f"\n     跟线路档次无关）。两件事分开看，别混成一件。")
    print(f"  4. 「可达」≠「解锁」。流媒体解锁要看区域判定，需专门的解锁检测脚本。")
    print(f"  5. 黑名单标「未知」的项是被拒答/超时，不是干净。")
    return 0

SELF_URL = "https://raw.githubusercontent.com/bgpeer/nodekit/main/vps-check.py"

def report_link(ip, num="二"):
    """本机 → 目标 IP 的链路。注意这【不是】那台机器到中国的线路。

       为什么还是值得测：做中转/落地链（本仓库的聚合节点就是这个场景）时，
       要知道的正是「我这台机到那台机有多远、走不走得通」——这条链路本机测得到，
       而且只有本机测得到。至于那台机回中国走哪条骨干，本机没有任何办法探到，
       别把这两件事混了。"""
    title(f"{num}、本机 → 目标 的链路")
    print(f"  {C['y']}这是【你这台机】到它的路，不是【它】到中国的路。{C['n']}")
    print(f"  {C['d']}做中转/落地链时看这个：两台机之间通不通、多远。{C['n']}")
    hr("·")
    ms = reach(ip, 443)
    hs = reach(ip, 80)
    if ms is None and hs is None:
        print(f"  TCP 握手   : {C['y']}443/80 都不通{C['n']}   {C['d']}（对方没开这两个端口，或防火墙挡了，不代表机器不在）{C['n']}")
    else:
        got = [f"443 {ms} ms" if ms is not None else "", f"80 {hs} ms" if hs is not None else ""]
        print(f"  TCP 握手   : {C['g']}{' / '.join(x for x in got if x)}{C['n']}")
    if not ensure_traceroute():
        print(f"  {C['r']}没有 traceroute/mtr 且装不上，逐跳跳过{C['n']}"); return
    hops = traceroute(ip)
    if not hops:
        print(f"  逐跳       : {C['y']}无响应（ICMP/UDP 被拦）{C['n']}"); return
    end = [m for _, h, m in hops if h and m is not None]
    print(f"  逐跳       : {len(hops)} 跳" + (f"，末跳 {end[-1]:.0f} ms" if end else ""))
    gap = []                                  # 连续无响应的跳折成一行，别刷 20 行 *
    def flush():
        if gap:
            span = f"{gap[0]}" if len(gap) == 1 else f"{gap[0]}-{gap[-1]}"
            print(f"    {C['d']}{span:>5}  * 无响应 {len(gap)} 跳{C['n']}")
            gap.clear()
    for n, hip, hms in hops:
        if not hip:
            gap.append(n); continue
        flush()
        asn, _pfx, cc, name = asn_of(hip)
        who = f"AS{asn} {name[:30]}" if asn else "查不到 AS"
        print(f"    {C['d']}{n:>2}  {hip:<16}{(f'{hms:.1f} ms' if hms else ''):>10}  {who} {cc}{C['n']}")
    flush()

def check_external(ip):
    """外部 IP 检测：这个 IP 自身的属性（画像、黑名单）+ 本机到它的链路。

       测不了、也不该假装能测的是【它到中国走哪条骨干】——那要在它自己身上跑
       traceroute，本机没有任何办法探到。所以这里不给回程判定，只在结论里
       把「去那台机上跑」的命令原样给出来。"""
    print(f"\n{C['b']}外部 IP 检测{C['n']}  {C['d']}目标 {ip}{C['n']}")
    print(f"  {C['d']}查三样：该 IP 的画像、本机到它的链路、它的黑名单。{C['n']}")
    print(f"  {C['y']}测不了它的三网回程{C['n']}{C['d']}——那要在它自己身上跑 traceroute，"
          f"本机探不到。命令见结论。{C['n']}")
    flag = report_ip(ip, "一", local=False)
    report_link(ip, "二")
    listed, _ = report_bl(ip, "三")

    title("四、结论")
    print(f"  IP 标记   : {_flag_line(flag)}")
    print(f"  黑名单     : " + (f"命中 {len(listed)} 个：{', '.join(listed)}" if listed else "未命中"))
    hr("·")
    print(f"  {C['b']}要测它的三网回程，SSH 上去跑这一行{C['n']}（纯 stdlib，不装任何东西）：")
    print(f"    {C['c']}curl -fsSL {SELF_URL} | python3{C['n']}")
    hr()
    print(f"{C['b']}局限{C['n']}")
    print(f"  1. {C['y']}三网回程只能在那台机器本机上测{C['n']}——traceroute 是从【发起方】"
          f"往外探，\n     在这儿跑，探到的永远是本机的出网路径，跟目标那台机怎么回中国无关。"
          f"\n     这不是没做，是原理上做不到。")
    print(f"  2. 「本机 → 目标」那一节测的是你这两台机之间的链路（中转/落地链看这个），"
          f"\n     不要拿它当目标机的线路质量。")
    print(f"  3. {C['y']}「上游/邻居」那行查不出 CN2{C['n']}，别拿它判线路。实测过：一台确认走"
          f"\n     CN2 GIA 的机器（DMIT LA / 69.63.210.0/24），邻居只有 AS2914(NTT)、"
          f"\n     AS3257(GTT)、AS137409，没有 4809 也没有 4134。CN2 是私有电路、"
          f"\n     59.43 不在 BGP 全局表里，商家把出境流量交给哪条电路是转发决策，"
          f"\n     全局路由表看不见。那行只当【上游构成】看。")
    print(f"  4. 想在【不登录目标机】的前提下摸它的中国线路，只有一条路：从国内侧测"
          f"\n     【去程】（中国→目标）。去程和回程经常走不同骨干，只能当参考。可用："
          f"\n     {C['c']}itdog.cn{C['n']} / {C['c']}ping.pe{C['n']}（网页，手动跑）、"
          f"商家自己的 Looking Glass、RIPE Atlas（要 API key\n     和积分，国内探针很少）。"
          f"这些都要么是网页要么要密钥，没法塞进本脚本，所以这里不做。")
    print(f"  5. 画像来自 ip-api.com，机房/代理标记是它的判定，各家风控库不完全一致。")
    print(f"  6. 黑名单标「未知」的项是被拒答/超时，不是干净。")
    return 0

def show_hops(ip):
    """把到某个 IP 的逐跳连同分类打出来。判定不对时先看这个，别猜。

       加它的由头：判定接连改错了三版，每次都是靠截图里的蛛丝马迹反推路径长什么样
       ——而路径本来是可以直接打出来的。有这个口子，下次一条命令就看清了。"""
    if not ensure_traceroute():
        print(f"{C['r']}没有 traceroute/mtr 且装不上{C['n']}"); return 1
    print(f"\n{C['b']}逐跳 → {ip}{C['n']}")
    hr()
    hops = traceroute(ip)
    if not hops:
        print(f"  {C['r']}无响应{C['n']}"); return 1
    for n, hip, ms in hops:
        if not hip:
            print(f"  {C['d']}{n:>2}  *{C['n']}"); continue
        lab, lvl, hcar, scope = classify_hop(hip)
        col, sym = _mark(lvl)
        asn, pfx, cc, name = asn_of(hip)
        tag = f"{col}{sym} {lab}{C['n']}" if lab else f"{C['d']}· {('AS'+asn+' '+name[:30]) if asn else '查不到 AS'}{C['n']}"
        meta = f"{C['d']}[{lvl or '-'}/{hcar or '-'}/{scope or '-'}] {pfx or ''} {cc or ''}{C['n']}"
        print(f"  {n:>2}  {hip:<16}{(f'{ms:.1f} ms' if ms else ''):>10}  {tag}  {meta}")
    hr()
    for car in CARRIERS:
        lab, lvl, ms, land = route_verdict(hops, car)
        col, sym = _mark(lvl)
        print(f"  按【{car}】判定 → {col}{sym} {lab}{C['n']}"
              f"{('　落地 ' + land) if land else ''}"
              f"{C['d']}　{f'{ms:.0f} ms' if ms else ''}{C['n']}")
    return 0

def main():
    if "--hops" in sys.argv:
        rest = sys.argv[sys.argv.index("--hops") + 1:]
        if not rest:
            print(f"{C['r']}用法：vps-check.py --hops <IP>{C['n']}"); return 1
        return show_hops(rest[0])
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        return check_local()
    ip = args[0]
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) or \
       any(int(x) > 255 for x in ip.split(".")):
        print(f"{C['r']}不是合法的 IPv4 地址：{ip}{C['n']}"); return 1
    return check_external(ip)

if __name__ == "__main__":
    sys.exit(main())
