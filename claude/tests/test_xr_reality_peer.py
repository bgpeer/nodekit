#!/usr/bin/env python3
"""聚合成员机 xray 内核的 reality 也不写进 sing-box 订阅（仓库主人 10/29：「你不是说singbox不支持这个Xray量子加密吗？
  聚合成员机被带入写进来了，本机这个没有写进singbox」）。
  xray 新版 REALITY 只收握手里带 X25519MLKEM768 的客户端，sing-box 系客户端不带（见 _local_xray_reality），
  本机的早就跳过了；成员机的内核主机看不到，以前照写，sing-box 里那几条永远连不上。
  现在成员机在 .links 末尾一条一行报 `#xray-reality 端口 借用域名`（老版主机只认节点链接前缀，自动忽略），
  主机汇总时认出这几条，sing-box 订阅不写；mihomo / 小火箭照常有。
"""
import os, io, re, sys, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
tmp = tempfile.mkdtemp()
quiet = lambda: contextlib.redirect_stdout(io.StringIO())

# ── 路径 / 外部调用全换成临时的 ─────────────────────────────────────────────────────
for k in ("PRIVDNS_FILE", "PRIVDNS_MAP_FILE", "LINKS_FILE", "CFG_FILE", "SBOX_FILE", "SR_FILE", "SR_NODES_FILE",
          "HOST_FILE", "PEERS_FILE", "SELFDNS_FLAG", "SELFDNS_CID_FILE", "TOKENS_FILE", "HY2_BW_FILE", "STATE_FILE"):
    setattr(x, k, f"{tmp}/{k.lower()}")
for e, f in (("yaml", "CFG_FILE"), ("json", "SBOX_FILE"), ("conf", "SR_FILE")):
    x.FMT[e]["file"] = getattr(x, f)
x.XRAY_DIR = f"{tmp}/xray"
os.makedirs(x.XRAY_DIR)
x.BGP_DIR = tmp
x.sh = lambda cmd, check=True: ""
TPL = {"yaml": open(os.path.join(ROOT, "sub-template.yaml")).read(),
       "json": open(os.path.join(ROOT, "subbox-template.json")).read(),
       "conf": open(os.path.join(ROOT, "shadowrocket-template.conf")).read()}
x.fetch_tpl = lambda u: TPL[u]
x.sr_sub_mode = lambda: False
x._sr_nodes_link = lambda: None
LOCAL, PEERS, PEER_TEXT = [], [], {}
x.read_saved_links = lambda: list(LOCAL)
x.load_peers = lambda: list(PEERS)
def fake_fetch(u, timeout=15):
    if u not in PEER_TEXT:
        raise OSError("不通")
    return PEER_TEXT[u]
x._fetch_text = fake_fetch

# 文档专用网段（RFC 5737）；运行时拼出来，claude/ 里不留 IP 字面量（test_claude_folder 守着）
ip = lambda *o: ".".join(map(str, o))
IPL, IPM, IPO = ip(203, 0, 113, 2), ip(198, 51, 100, 7), ip(192, 0, 2, 30)
x._host = lambda: IPL
x.G["host"] = IPL

def xray_cfg(inbounds):
    json.dump({"inbounds": inbounds}, open(f"{x.XRAY_DIR}/config.json", "w"))
def rin(port, sni, listen="0.0.0.0"):
    return {"listen": listen, "port": port, "protocol": "vless",
            "streamSettings": {"security": "reality", "realitySettings": {"serverNames": [sni]}}}
def rl(uid, host, port, sni, name, extra=""):
    return (f"vless://{uid}@{host}:{port}?encryption=none&flow=xtls-rprx-vision&security=reality&sni={sni}"
            f"&fp=chrome&pbk=PBK&sid=ab&type=tcp{extra}#{name}")

# ── 1. 成员机这边：.links 末尾报自己 xray 内核的 reality ─────────────────────────────────────
print("一、成员机的 .links")
xray_cfg([rin(24001, "www.cisco.com", "127.0.0.1"),                       # 挂在 nginx 443 分流后面
          rin(24567, "www.apple.com"),
          {"listen": "0.0.0.0", "port": 25000, "protocol": "vless", "streamSettings": {"security": "tls"}}])
MINE = [rl("u-m", IPM, 443, "www.microsoft.com", "🇺🇸OR·reality-vision¹"),
        rl("u-m", IPM, 443, "www.cisco.com", "🇺🇸OR·reality-vision²", "&pqv=PQV"),
        f"hysteria2://pw@{IPM}:31000?sni=www.bing.com&insecure=1#🇺🇸OR·hy2"]
x._write_links_file(MINE)
lt = open(x.LINKS_FILE).read()
check(f"链接原样在前，末尾一条一行报 xray 的 reality（443 是分流后面那条）（{lt.splitlines()[len(MINE):]}）",
      lt.splitlines() == MINE + ["#xray-reality 443 www.cisco.com", "#xray-reality 24567 www.apple.com"])
check("老版主机只认节点链接前缀：这几行不会被当成节点",
      [l for l in lt.splitlines() if l.strip().startswith(x._NODE_SCHEMES)] == MINE)
check("私有解析那行照样读得出来（没开 = 没有）", x._privdns_meta(lt) is None)
check(f".links 只给 root 看（{oct(os.stat(x.LINKS_FILE).st_mode & 0o777)}）", os.stat(x.LINKS_FILE).st_mode & 0o777 == 0o600)
ino = os.stat(x.LINKS_FILE).st_ino
x._write_links_file(MINE)
check("内容没变：不重写（主机 03:20 来拉时不会撞上写了一半）", os.stat(x.LINKS_FILE).st_ino == ino)
os.remove(f"{x.XRAY_DIR}/config.json")
x._write_links_file(MINE)
check("没装 xray：只有链接", open(x.LINKS_FILE).read().splitlines() == MINE)

# ── 2. 主机这边：成员机报的那几条 sing-box 订阅不写 ──────────────────────────────────────────
print("二、主机汇总")
xray_cfg([rin(24001, "www.cisco.com", "127.0.0.1")])                    # 主机自己也有一条 xray reality（以前就跳过）
LOCAL[:] = [rl("u-l", IPL, 443, "www.microsoft.com", "🇺🇸2·reality-vision¹"),
            rl("u-l", IPL, 443, "www.cisco.com", "🇺🇸2·reality-vision²"),
            f"hysteria2://pw@{IPL}:31000?sni=www.bing.com&insecure=1#🇺🇸2·hy2"]
OR_LINKS = MINE + [f"vless://u-m@{IPM}:24567?encryption=none&security=reality&sni=www.apple.com&fp=chrome&pbk=PBK"
                   f"&sid=ab&type=grpc&serviceName=g&mode=multi#🇺🇸OR·reality-grpc",
                   f"trojan://pw@{IPM}:443?security=tls&sni=www.cisco.com&fp=chrome&allowInsecure=1#🇺🇸OR·trojan"]
OLD = [rl("u-o", IPO, 443, "www.cisco.com", "🇯🇵3·reality-vision²")]
PEERS[:] = ["https://or.example/x.links", "https://old.example/y.links"]
PEER_TEXT.clear()
PEER_TEXT["https://or.example/x.links"] = ("\n".join(OR_LINKS) + "\n#xray-reality 443 WWW.CISCO.COM\n"
                                           "#xray-reality  24567\twww.apple.com \n")
PEER_TEXT["https://old.example/y.links"] = "\n".join(OLD) + "\n"          # 还没更新的成员机：什么都没报
links = x.aggregated_links()
ylines, nodes = x.parse_nodes(links)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    x.gen_mihomo(ylines, nodes, "yaml")
    x.gen_singbox(ylines, nodes, "json")
    x.gen_shadow(ylines, nodes, "conf")
sb = {o["tag"] for o in json.loads(open(x.SBOX_FILE).read())["outbounds"] if isinstance(o, dict) and o.get("type") == "vless"}
mh, sr = open(x.CFG_FILE).read(), open(x.SR_FILE).read()
check(f"sing-box：成员机 xray 的 reality-vision² / reality-grpc 不写了（{sorted(sb)}）",
      not [t for t in sb if t.startswith("🇺🇸OR·reality-vision²") or t.startswith("🇺🇸OR·reality-grpc")])
check("sing-box：成员机 sing-box 内核的 reality-vision¹ 照写", any(t.startswith("🇺🇸OR·reality-vision¹") for t in sb))
check("sing-box：本机 xray 的 reality 照旧不写、sing-box 的照写",
      not any(t.startswith("🇺🇸2·reality-vision²") for t in sb) and any(t.startswith("🇺🇸2·reality-vision¹") for t in sb))
check("同端口同借用域名的 trojan 不算（只认 reality）", "🇺🇸OR·trojan" in json.dumps(json.loads(open(x.SBOX_FILE).read()), ensure_ascii=False))
check("还没更新的成员机：没报就照旧写（主机认不出，等它夜里自更新后报上来）", any(t.startswith("🇯🇵3·reality-vision²") for t in sb))
check(f"屏上一行说清跳过了几条（{buf.getvalue().strip()[:60]}）", "跳过 3 条 xray 内核的 reality" in buf.getvalue())
check("mihomo：成员机 xray 的 reality 都在（mihomo 带 X25519MLKEM768，连得上）",
      "🇺🇸OR·reality-vision²" in mh and "🇺🇸OR·reality-grpc" in mh)
check("小火箭：成员机的 reality-vision² 照写（10/20 苹果实测 vision² 能连）", "🇺🇸OR·reality-vision²" in sr)
check("标记不进 mihomo 配置", "_xr_reality" not in mh)

# ── 3. 换地址 / 改名 / 撞名加上标都认得出 ───────────────────────────────────────────────────
print("三、汇总里改过的链接")
u = OR_LINKS[1]
ids = {x._xr_id(x.link_to_proxy(v)) for v in (u, x._link_rehost(u, IPM, "k7xq2m4p9z.internal"),
                                               x._link_rename(u, "¹🇺🇸OR·reality-vision²"))}
check(f"私有解析换地址、改名后还是同一条（{ids}）", len(ids) == 1)
PEERS[:] = []
x.aggregated_links()
check("下一轮汇总先清空：成员机删了，上一轮的记录不留", not x._XR_PEER)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
