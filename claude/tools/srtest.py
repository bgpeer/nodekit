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
"""
import base64, json, sys, urllib.parse as up
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
if KIND not in KINDS:
    sys.exit(f"种类只有：{' / '.join(KINDS)}")
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
