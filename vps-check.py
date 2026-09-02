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
import json, os, re, socket, struct, random, subprocess, sys, urllib.request, concurrent.futures as cf

C = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "c": "\033[36m",
     "b": "\033[1m", "d": "\033[2m", "n": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}

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
    """(asn, 前缀, 国家, AS名)。查不到返回 (None,"","","")。"""
    if ip in _ASN_CACHE: return _ASN_CACHE[ip]
    r = dns_txt(f"{_rev(ip)}.origin.asn.cymru.com")
    res = (None, "", "", "")
    if r:
        f = [x.strip() for x in r[0].split("|")]
        asn = f[0].split()[0] if f and f[0].split() else None
        name = ""
        if asn:
            r2 = dns_txt(f"AS{asn}.asn.cymru.com")
            if r2:
                g = [x.strip() for x in r2[0].split("|")]
                name = g[4] if len(g) > 4 else ""
        res = (asn, f[1] if len(f) > 1 else "", f[2] if len(f) > 2 else "", name)
    _ASN_CACHE[ip] = res
    return res

# ---------------------------------------------------------------- 骨干判定
# ⚠ 59.43/202.97 必须按【前缀】认，不能按 ASN：
#    59.43.0.0/16 是 CN2 的内部传输网，【不在 BGP 全局路由表里】，Cymru 查不到 AS。
#    （实测 59.43.0.1 → 无结果；而 202.97.0.1 → AS4134 CHINANET-BACKBONE。）
BACKBONE_AS = {
    "4809":  ("电信 CN2",        "premium"),
    "4134":  ("电信 163",        "normal"),
    "9929":  ("联通 9929",       "premium"),
    "4837":  ("联通 169",        "normal"),
    "58807": ("移动 CMIN2",      "premium"),
    "58453": ("移动 CMI",        "normal"),
    "9808":  ("移动 CMNET",      "normal"),
    "4847":  ("电信 CNIX",       "normal"),
}
def classify_hop(ip):
    """→ (标签, 等级)。等级：premium / normal / "" """
    if ip.startswith("59.43."):
        return "电信 CN2 (59.43)", "premium"
    if ip.startswith("202.97."):
        return "电信 163 (202.97)", "normal"
    asn, _, cc, name = asn_of(ip)
    if asn and asn in BACKBONE_AS:
        lab, lvl = BACKBONE_AS[asn]
        return f"{lab} (AS{asn})", lvl
    if asn:
        return f"AS{asn} {name[:34]}", ""
    return "", ""

# ---------------------------------------------------------------- 回程路由
# 三网常用测试点。⚠ 不盲信：跑之前先查每个目标的真实 ASN/国家，
#    对不上就标成「目标存疑」并跳过，宁可少测也不给错结论。
TARGETS = [
    ("电信", "北京", "219.141.136.12"), ("电信", "上海", "202.96.209.133"),
    ("电信", "广州", "58.60.188.222"),
    ("联通", "北京", "202.106.50.1"),   ("联通", "上海", "210.22.97.1"),
    ("联通", "广州", "210.21.196.6"),
    ("移动", "北京", "221.179.155.161"),("移动", "上海", "211.136.112.200"),
    ("移动", "广州", "120.196.165.24"),
]

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

def traceroute(ip, maxhop=20, timeout=90):
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
def report_ip(ip):
    title("一、本机 IP 画像")
    asn, pfx, cc, name = asn_of(ip)
    print(f"  IP        : {C['b']}{ip}{C['n']}")
    print(f"  ASN       : {'AS'+asn if asn else '查不到'}  {name}")
    print(f"  宣告前缀   : {pfx or '-'}    注册地: {cc or '-'}")
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

def report_routes(full=False):
    title("二、三网回程路由（VPS → 中国）")
    if not ensure_traceroute():
        print(f"  {C['r']}没有 traceroute/mtr 且装不上，跳过本节{C['n']}"); return None
    targets = TARGETS if full else [t for t in TARGETS if t[1] == "北京"]
    print(f"  {C['d']}测试点先校验归属，对不上的会标『存疑』并跳过；"
          f"共 {len(targets)} 条{'（--full 可测全部 9 条）' if not full else ''}{C['n']}")
    verdict = {}
    for carrier, city, ip in targets:
        asn, _, cc, name = asn_of(ip)
        hr("·")
        head = f"  {C['b']}{carrier} · {city}{C['n']}  {ip}"
        if cc != "CN":
            print(head + f"  {C['y']}[目标存疑：注册地 {cc or '未知'}，跳过]{C['n']}"); continue
        print(head + f"  {C['d']}(AS{asn} {name[:30]}){C['n']}")
        hops = traceroute(ip)
        if not hops:
            print(f"    {C['y']}无响应（ICMP/UDP 被拦或目标不可达）{C['n']}"); continue
        seen = []
        for n, hip, ms in hops:
            if not hip:
                continue
            lab, lvl = classify_hop(hip)
            col = C["g"] if lvl == "premium" else (C["y"] if lvl == "normal" else C["d"])
            tail = f"  {col}{lab}{C['n']}" if lab else ""
            print(f"    {n:>2}  {hip:<16}{(str(ms)+' ms') if ms else '':>10}{tail}")
            if lvl: seen.append((lab, lvl))
        prem = [l for l, v in seen if v == "premium"]
        verdict[f"{carrier}·{city}"] = prem[0] if prem else (seen[-1][0] if seen else "未识别")
    return verdict

def report_bl(ip):
    title("三、IP 黑名单（DNSBL）")
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
    return listed

def report_reach():
    title("四、常用服务可达性（TCP:443 握手）")
    print(f"  {C['d']}只测能否连上，不代表解锁——解锁要看返回内容和区域判定{C['n']}")
    hr("·")
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for (lab, host), ms in zip(SERVICES, ex.map(lambda s: reach(s[1]), SERVICES)):
            if ms is None: print(f"  {C['r']}✗ {lab:<10} 连不上{C['n']}")
            else:          print(f"  {C['g']}✓ {lab:<10} {ms} ms{C['n']}")

def main():
    full = "--full" in sys.argv
    print(f"\n{C['b']}VPS 线路与 IP 纯净度体检{C['n']}")
    ip = my_ip()
    if not ip:
        print(f"{C['r']}拿不到公网 IP，检查外网连通性。{C['n']}"); return 1
    flag = report_ip(ip)
    v = report_routes(full)
    listed = report_bl(ip)
    report_reach()

    title("五、结论")
    if v:
        for k, val in v.items():
            prem = "CN2" in val or "9929" in val or "CMIN2" in val
            print(f"  {k:<10} → {(C['g'] if prem else C['y'])}{val}{C['n']}")
    print()
    print(f"  IP 标记   : " + ("有 proxy 标记，纯净度差" if flag == 2 else
                               "机房 IP（正常现象，流媒体可能受限）" if flag == 1 else "无异常标记"))
    print(f"  黑名单     : " + (f"命中 {len(listed)} 个：{', '.join(listed)}" if listed else "未命中"))
    hr()
    print(f"{C['b']}局限（必须知道，否则会误判）{C['n']}")
    print(f"  1. {C['y']}本脚本不区分 CN2 GIA 和 CN2 GT{C['n']}——两者都走 59.43 段，"
          f"而 59.43.0.0/16\n     不在 BGP 全局路由表里（查不到 AS），"
          f"公开的细分规则我无法核实，硬编码\n     一套没验证过的前缀表只会给你「自信但错误」的结论。"
          f"要定性请用 nexttrace：\n     "
          f"{C['c']}curl -sL nxtrace.org/nt | bash && nexttrace 219.141.136.12{C['n']}")
    print(f"  2. 这里测的是【回程】（VPS→中国）。去程（中国→VPS）本机测不到，"
          f"\n     要在国内侧测。三网的去程和回程经常走【不同】骨干。")
    print(f"  3. 「可达」≠「解锁」。流媒体解锁要看区域判定，需专门的解锁检测脚本。")
    print(f"  4. 黑名单标「未知」的项是被拒答/超时，不是干净。")
    return 0

if __name__ == "__main__":
    sys.exit(main())
