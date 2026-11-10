#!/usr/bin/env python3
"""CDN 量子加密（菜单 9 → 6）整条路对一遍（仓库主人 11/11：「这个开着是否能完美运行你能不能做个检查」）。
  以前这个功能一个测试都没有。容器里不让跑下载来的 xray / mihomo，连不了真的；这里把能在本机对的都对上：
  开 / 关时服务端配置、内核、钥匙怎么变，失败怎么还原，分享链接和三种订阅里各是什么样。
  钥匙串的写法照 xray `vlessenc` 的输出：服务端 mlkem768x25519plus.native.600s.<私钥>，客户端 …native.0rtt.<公钥>。
"""
import os, io, sys, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
PRIV, PUB = "PrivKey_-abc123", "PubKey_-xyz789"          # xray x25519 的钥匙是 base64url，带 - 和 _
x.reality_keys = lambda b, c: (PRIV, PUB)
SVC, FAIL = [], [False]
def fake_service(name, binpath, cfg):
    if FAIL[0]:
        raise RuntimeError("xy-cdn 配置校验失败\n第二行")
    SVC.append((name, binpath, json.load(open(cfg))))
x.write_service = fake_service
x.sh = lambda c, check=True: ""
def node(proto, core="sing-box", nid=1):
    conf = f"{tmp}/c{nid}.json"
    n = {"id": nid, "proto": proto, "core": core, "domain": "cdn.example", "cred": "11111111-2222-3333-4444-555555555555",
         "path": "/ab12cd34", "cf_port": 2053, "tag": f"CDN·{proto}", "svc": f"xy-cdn{nid}", "crt": f"{tmp}/c.crt", "key": f"{tmp}/c.key",
         "conf": conf, "in_sub": True, "pref": ""}
    json.dump(x._cdn_config(proto, core, n["cred"], n["path"], 2053, "cdn.example", n["crt"], n["key"])[0], open(conf, "w"))
    return n

print("一、钥匙")
dec, enc = x._cdn_qe_keys()
check("服务端 decryption = mlkem768x25519plus.native.600s.<私钥>，客户端 encryption = …native.0rtt.<公钥>",
      dec == f"mlkem768x25519plus.native.600s.{PRIV}" and enc == f"mlkem768x25519plus.native.0rtt.{PUB}")
x.sh = lambda c, check=True: "PrivateKey: AAA\nPassword: BBB\nHash32: CCC" if "x25519" in c else ""
real_keys = importlib.util.module_from_spec(spec); spec.loader.exec_module(real_keys)
real_keys.sh = x.sh
check("新版 xray x25519 的输出（PrivateKey / Password / Hash32 三行）取得对", real_keys.reality_keys("/x/xray", "x25519") == ("AAA", "BBB"))
real_keys.sh = lambda c, check=True: "Private key: AAA\nPublic key: BBB"
check("老版输出（Private key / Public key）也取得对", real_keys.reality_keys("/x/xray", "x25519") == ("AAA", "BBB"))
x.sh = lambda c, check=True: ""

print("二、开：VLESS+WS（原来是 sing-box 内核）")
n = node("vless-ws", "sing-box")
old_conf = open(n["conf"]).read()
check("开成了", x._cdn_qe_switch(n, True) is True)
name, binp, conf = SVC[-1]
ib = conf["inbounds"][0]
check("改用 xray 内核，原内核记下（关的时候还原）", n["core"] == "xray" and n["core_orig"] == "sing-box" and binp == x.XRAY_BIN)
check("服务端 decryption 写进去了，端口 / UUID / 路径 / 证书都没变", ib["settings"]["decryption"] == dec and ib["port"] == 2053
      and ib["settings"]["clients"] == [{"id": n["cred"]}] and ib["streamSettings"]["wsSettings"]["path"] == "/ab12cd34"
      and ib["streamSettings"]["tlsSettings"]["certificates"][0]["certificateFile"] == n["crt"])
link = x._cdn_link(n)
check("分享链接里 encryption 是客户端那半把钥匙，别的段不变", f"encryption={enc}&" in link and "security=tls&sni=cdn.example" in link
      and "type=ws&host=cdn.example&path=/ab12cd34" in link)
d = x.link_to_proxy(link)
check("mihomo 订阅：节点带 encryption（1.19.13+ 认），ws 路径 / Host 照旧", d["encryption"] == enc and d["network"] == "ws"
      and d["ws-opts"] == {"path": "/ab12cd34", "headers": {"Host": "cdn.example"}} and d["servername"] == "cdn.example")
check("写进 YAML 时钥匙带引号（不会被读成别的类型）", f'encryption: "{enc}"' in x._yfmt(d))
check("sing-box 订阅不写这条（它不支持，写了只会连不上）", x.mihomo_to_sb_outbound(x.proto_key(d), d) is None)
check("小火箭订阅不写这条", x.shadowrocket_line("n", d) is None)

print("三、开：VLESS+XHTTP（本来就是 xray）；VMess / Trojan 不受影响")
n2 = node("vless-xhttp", "xray", 2)
x._cdn_qe_switch(n2, True)
ib2 = SVC[-1][2]["inbounds"][0]
check("xhttp：decryption 写上、内核本来就是 xray 不记 core_orig", ib2["settings"]["decryption"] == dec and "core_orig" not in n2
      and ib2["streamSettings"]["network"] == "xhttp")
d2 = x.link_to_proxy(x._cdn_link(n2))
check("xhttp 的 mihomo 节点也带 encryption", d2["encryption"] == enc and d2["network"] == "xhttp")
for p in ("vmess-ws", "trojan-ws"):
    m = node(p, "sing-box", 9)
    check(f"{p} 不算 VLESS：开关不碰它，三种订阅照旧都有", not x._cdn_is_vless(m) and "encryption" not in (x.link_to_proxy(x._cdn_link(m)) or {})
          and x.mihomo_to_sb_outbound(x.proto_key(x.link_to_proxy(x._cdn_link(m))), x.link_to_proxy(x._cdn_link(m))) is not None)

print("四、关：还原")
check("关成了", x._cdn_qe_switch(n, False) is True)
name, binp, conf = SVC[-1]
check("内核换回 sing-box、钥匙清掉、链接回到 encryption=none", n["core"] == "sing-box" and "core_orig" not in n and "dec" not in n and "enc" not in n
      and binp == x.SB_BIN and conf["inbounds"][0]["type"] == "vless" and "encryption=none&" in x._cdn_link(n))
d = x.link_to_proxy(x._cdn_link(n))
check("关了以后 sing-box / 小火箭订阅里又有它", "encryption" not in d and x.mihomo_to_sb_outbound(x.proto_key(d), d) is not None
      and x.shadowrocket_line("n", d))

print("五、没改成：原样还原")
n3 = node("vless-ws", "sing-box", 3)
before, conf_before = dict(n3), open(n3["conf"]).read()
FAIL[0] = True
b = io.StringIO()
with contextlib.redirect_stdout(b):
    r = x._cdn_qe_switch(n3, True)
FAIL[0] = False
check("xray 不认配置（内核太旧之类）：返回 False，节点记录、配置文件都跟开之前一样，屏上一行 ⚠",
      r is False and n3 == before and open(n3["conf"]).read() == conf_before and "没改成，已还原" in b.getvalue()
      and "第二行" not in b.getvalue())

print("六、优选候选跟着基础节点走")
x._pref_load = lambda: {"cands": ["1.2.3.4", "1.2.3.5"], "quantum": True}
x._cdn_qe_switch(n, True)
cs = x._cdn_cand_nodes([n, node("trojan-ws", "sing-box", 4)], x._pref_load())
check("候选是从 VLESS+WS 克隆的：开着量子加密时候选也带钥匙（所以 sing-box / 小火箭订阅里没有优选候选，屏上开之前有提示）",
      len(cs) == 2 and all(f"encryption={enc}&" in x._cdn_link(c) and "@1.2.3." in x._cdn_link(c) for c in cs))
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("开之前屏上写明：服务端只支持 xray、只有 mihomo 系 1.19.13+ 能连、sing-box / 小火箭订阅里不再有它们（含优选）",
      "只有 mihomo 系客户端（1.19.13+）能连" in src and "sing-box / 小火箭订阅里不再有它们（含优选）" in src)
check("xray 版本下限 25.9.5", x.CDN_QE_MIN_XRAY == (25, 9, 5))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
