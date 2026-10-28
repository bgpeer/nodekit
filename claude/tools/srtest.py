#!/usr/bin/env python3
"""小火箭配置行写法实测（只读，只往屏幕印）：拿本机节点生成一份测试配置，同一个节点放几种写法，手机上 C 测试看哪个通。

用法（VPS 上，第二行把印出来的配置放进订阅目录；种类见下面 KINDS，不写 = qe）：
  T=$(python3 -c 'import json;print(json.load(open("/etc/bgpeer/tokens.json"))["conf"])')
  curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/srtest.py | python3 - qe > /etc/bgpeer/sub/$T-srtest.conf
小火箭里把订阅地址末尾的 /<token>/xxx.conf 换成 /<token>-srtest.conf 加进去，最上面 T- 开头的就是测试节点。
测完删掉：rm -f /etc/bgpeer/sub/*-srtest.conf
不带私人信息：节点参数现读本机 /etc/bgpeer/nodes.links，只印到屏幕 / 你重定向到的文件。

10/20 实测结论（都已写进 xy-installer 的 shadowrocket_line）：路径 / Host / grpc 服务名一律 obfs-uri / obfs-host，
不是分享链接里的 path / obfsParam。下面每种里标「✔」的是实测通的，其余是实测不通的，留着小火箭更新后复测用。

私有解析（种类 pdns，10/28）：本机 13 → 11 开着时，拿本机第一条 reality（vless tcp）节点出两个测试节点——
T-pdns-0 照写原来的地址（对照，它不通就是节点本身的事），T-pdns-<写法> 地址换成私有名，看小火箭能不能用指定的 DNS 解析出来。
写法放第二个参数，一次只能测一种（A 是全局设置，跟 B 混在一份里分不清是谁起的作用）：
  A：[General] proxy-dns-server = 那个 DoH（小火箭文档：「使用指定 DNS 解析所有节点域名」）
  B：[Host] 私有名 = server:那个 DoH（文档只举了 server:IP，DoH 写不写得进去没说）
  … | python3 - pdns A > /etc/bgpeer/sub/$T-srtest.conf    （测完换 B 再来一次）
"""
import base64, json, re, sys, urllib.parse as up
B = "/etc/bgpeer"
KIND = (sys.argv[1] if len(sys.argv) > 1 else "qe").lower()
# 种类 → {字母: 传输段模板}。{P}=路径 {H}=Host {S}=grpc 服务名 {E}=VLESS Encryption 串
KINDS = {
    "ws":    {"A": "obfs=websocket,obfs-uri={P},obfs-host={H}",                       # ✔
              "B": "obfs=websocket,path={P},obfsParam={H}",
              "C": "obfs=websocket,path={P},obfsParam={H},alpn=http/1.1",
              "D": 'obfs=websocket,path={P},obfsParam={{"Host":"{H}"}}'},
    "hu":    {"A": "obfs=httpupgrade,obfs-uri={P},obfs-host={H}",                     # ✔
              "B": "obfs=http-upgrade,obfs-uri={P},obfs-host={H}",
              "C": "obfs=websocket,obfs-uri={P},obfs-host={H},v2ray-http-upgrade=true",
              "D": "obfs=httpupgrade,path={P},obfsParam={H}"},
    "grpc":  {"A": "obfs=grpc,obfs-uri={S}",                                          # ✔
              "B": "obfs=grpc,path={S}",
              "C": "obfs=grpc,obfs-uri=/{S}",
              "D": "obfs=grpc,grpc-service-name={S}"},
    "xhttp": {"A": "obfs=xhttp,obfs-uri={P},obfs-host={H}",                           # ✔
              "B": "obfs=xhttp,path={P},obfsParam={H}",
              "C": "obfs=splithttp,obfs-uri={P},obfs-host={H}"},
    # VLESS Encryption（量子加密）：还没测出来。传输段按上面 ✔ 的写，加密串试三种参数名
    "qe":    {"A": "{T},encryption={E}", "B": "{T},method={E}", "C": "{T},cipher={E}"},
}
if KIND not in KINDS and KIND != "pdns":
    sys.exit(f"种类只有：{' / '.join(KINDS)} / pdns")
TR = {"ws": KINDS["ws"]["A"], "httpupgrade": KINDS["hu"]["A"], "grpc": KINDS["grpc"]["A"], "xhttp": KINDS["xhttp"]["A"]}

def nodes():
    """nodes.links → (类型, 公共段, 网络, P, H, S, E)。"""
    for u in open(f"{B}/nodes.links").read().split():
        if u.startswith("vmess://"):
            j = json.loads(base64.b64decode(u[8:] + "=" * (-len(u[8:]) % 4)))
            H = j.get("host") or j.get("sni") or j["add"]
            base = f"vmess,{j['add']},{j['port']},password={j['id']},alterId=0,method=auto" + \
                   (f",tls=true,peer={j.get('sni') or H}" if j.get("tls") == "tls" else "")
            yield "vmess", base, j.get("net", "tcp"), j.get("path") or "/", H, "", ""
        elif u.startswith("vless://"):
            p = up.urlsplit(u); q = dict(up.parse_qsl(p.query)); sni = q.get("sni") or p.hostname
            base = f"vless,{p.hostname},{p.port},password={p.username},tls=true,peer={sni}"
            if q.get("security") == "reality":
                base += f",publicKey={q.get('pbk', '')},shortId={q.get('sid', '')}"
            if q.get("flow") == "xtls-rprx-vision":
                base += ",xtls=2"
            yield "vless", base, q.get("type", "tcp"), q.get("path", "/"), q.get("host") or sni, \
                  q.get("serviceName", ""), "" if q.get("encryption", "none") == "none" else q["encryption"]

def pdns():
    """私有解析测试：(原来的地址, 私有名, 那个 DoH, 对照节点行, 私有名节点行)。"""
    pd = json.load(open(f"{B}/privdns.json"))
    name = pd.get("name", "")
    if pd.get("mode") not in ("self", "remote") or not name:
        sys.exit("本机 13 → 11 还没开私有解析")
    if pd["mode"] == "remote":
        doh = pd.get("dns", "")
    else:                                   # 本机当 DNS 机：跟主脚本 _agh_doh_url 一样拼
        m = re.search(r"(?m)^\s*port_https:\s*(\d+)", open("/opt/AdGuardHome/AdGuardHome.yaml").read())
        sch = "https"
        doh = f"{sch}://{open(f'{B}/sub.host').read().strip()}:{m.group(1) if m else '10443'}" \
              f"/dns-query/{open(f'{B}/selfdns.clientid').read().strip()}"
    for t, base, net, P, H, S, E in nodes():
        if t == "vless" and net == "tcp" and not E:
            host = base.split(",")[1]
            return host, name, doh, f"T-pdns-0 = {base},udp=1,tfo=1", \
                f"T-pdns-{WAY} = {base.replace(',' + host + ',', ',' + name + ',', 1)},udp=1,tfo=1"
    sys.exit("本机没有 reality（vless tcp）节点，测不了")

if KIND == "pdns":
    WAY = (sys.argv[2] if len(sys.argv) > 2 else "A").upper()
    if WAY not in ("A", "B"):
        sys.exit("写法只有 A / B")
    host, name, doh, ctl, test = pdns()
    conf = open(f"{B}/shadowrocket.conf").read().replace("[Proxy]\n", f"[Proxy]\n{ctl}\n{test}\n", 1)
    if WAY == "A":
        conf = re.sub(r"(?m)^proxy-dns-server\s*=.*\n?", "", conf)
        conf = conf.replace("[General]\n", f"[General]\nproxy-dns-server = {doh}\n", 1)
    elif "[Host]\n" in conf:
        conf = conf.replace("[Host]\n", f"[Host]\n{name} = server:{doh}\n", 1)
    else:
        conf = conf.rstrip("\n") + f"\n\n[Host]\n{name} = server:{doh}\n"
    sys.stderr.write(f"pdns {WAY}：T-pdns-0 照写原来的地址，T-pdns-{WAY} 换成私有名\n")
    sys.stdout.write(conf)
    sys.exit(0)

out = []; n = 0
for t, base, net, P, H, S, E in nodes():
    if KIND == "qe":
        if not E:
            continue
        T = TR.get(net, "obfs=none").format(P=P, H=H, S=S)
    elif {"ws": "ws", "hu": "httpupgrade", "grpc": "grpc", "xhttp": "xhttp"}[KIND] != net or E:
        continue
    else:
        T = ""
    n += 1
    for k, tpl in KINDS[KIND].items():
        out.append(f"T-{KIND}{n}-{k} = {base},{tpl.format(P=P, H=H, S=S, E=E, T=T)},udp=1,tfo=1")
conf = open(f"{B}/shadowrocket.conf").read()
sys.stderr.write(f"{KIND}：{n} 个节点 × {len(KINDS[KIND])} 种写法 = {len(out)} 个测试节点\n")
sys.stdout.write(conf.replace("[Proxy]\n", "[Proxy]\n" + "\n".join(out) + "\n", 1))
