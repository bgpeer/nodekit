#!/usr/bin/env python3
"""小火箭配置写进 xhttp —— 仓库主人 10/20：「xhttp 我记得小火箭是支持的吧，没写吗？」
  苹果实测六种写法：obfs=xhttp,obfs-uri=路径,obfs-host=Host 的 xhttp-tls / reality-xhttp 都通；
  path= + obfsParam=、obfs=splithttp 不通。量子加密（VLESS Encryption）的 xhttp 配置行写不了，照旧跳过。
"""
import os, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
L = m.shadowrocket_line
xt = L("a", dict(type="vless", server="h.example.com", port=443, uuid="u", tls=True, servername="h.example.com",
                 network="xhttp", **{"xhttp-opts": {"path": "/xp", "host": "h.example.com"}}))
rx = L("b", dict(type="vless", server="h.example.com", port=8443, uuid="u", tls=True, servername="www.cisco.com",
                 network="xhttp", **{"xhttp-opts": {"path": "/rp", "host": "www.cisco.com"},
                                     "reality-opts": {"public-key": "K", "short-id": "s"}}))
qe = L("c", dict(type="vless", server="h.example.com", port=443, uuid="u", tls=True, network="xhttp", encryption="mlkem768x25519plus",
                 **{"xhttp-opts": {"path": "/q"}}))
check(f"xhttp-tls：obfs=xhttp,obfs-uri=/xp,obfs-host=h.example.com（{xt}）", xt and ",obfs=xhttp,obfs-uri=/xp,obfs-host=h.example.com," in xt)
check(f"reality-xhttp：带公钥 + obfs=xhttp,obfs-uri=/rp,obfs-host=www.cisco.com（{rx}）",
      rx and "publicKey=K,shortId=s," in rx and ",obfs=xhttp,obfs-uri=/rp,obfs-host=www.cisco.com," in rx)
check("量子加密的 xhttp：照旧不写", qe is None)
check("不写 path= / obfsParam=", not any(k in (xt + rx) for k in (",path=", "obfsParam=")))
ylines, nodes = m.parse_nodes(["vless://u" + "@h.example.com:443?encryption=none&security=tls&sni=h.example.com&type=xhttp&host=h.example.com&path=%2Fzz#x"])
d = dict(nodes)[next(iter(dict(nodes)))]
check("分享链接 → 节点 → 小火箭行，路径 / Host 一路带过来", ",obfs-uri=/zz,obfs-host=h.example.com," in (L("x", d) or ""))
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
