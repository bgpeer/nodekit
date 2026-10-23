#!/usr/bin/env python3
"""tuic 上 UDP 443 + sing-box 新增 grpc-tls 挂 nginx（仓库主人 10/23）。
  tuic：有域名真证书、UDP 443 空着（或占着的是本机 sing-box）就用 443——同一个域名 TCP 443 网站、UDP 443 h3，跟真网站开 HTTP/3 一样；
  运营商 4G 对随机高端口 UDP 限速丢包最狠。没域名 / 自签 / 被别人占着：照旧随机端口。没开 IPv6 的机器不能崩。
  grpc-tls：nginx 前置时本地明文 grpc、nginx 按 /服务名/ grpc_pass，链接 443 + 自己的域名；不前置时自带证书听自己的端口。
  grpc 路径被浏览器 / curl 摸到：不是 gRPC 请求回 404（伪装站自己的 404 页），10/23 照 mack-a 加的。
  reality-grpc 一个不动（功能不许悄悄没了）。真 nginx + sing-box + mihomo / sing-box 客户端的端到端在会话里跑过。
"""
import os, sys, json, socket, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
m.G.update({"domain": "d.example", "host": "d.example", "sni": "s.example", "nginx": "1", "reality443": "", "sni_split": ""})
m.ensure_acme = lambda: ("/c.pem", "/k.pem", False)

# tuic
m._udp443_ok = lambda: True
ib, lk = m.sb_tuic(30001, "P·tuic")
check(f"有域名真证书、UDP 443 空着：tuic 上 443（{lk.split('?')[0][-12:]}）", ib["listen_port"] == 443 and "@d.example:443?" in lk)
m._udp443_ok = lambda: False
ib, lk = m.sb_tuic(30001, "P·tuic")
check("UDP 443 被别人占着：照旧随机端口", ib["listen_port"] == 30001 and ":30001?" in lk)
m._udp443_ok = lambda: True
m.ensure_acme = lambda: ("/c.pem", "/k.pem", True)
ib, lk = m.sb_tuic(30001, "P·tuic")
check("自签：照旧随机端口", ib["listen_port"] == 30001)
m.ensure_acme = lambda: ("/c.pem", "/k.pem", False)
spec2 = importlib.util.spec_from_file_location("xy2", os.path.join(ROOT, "xy-installer.py"))
m2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(m2)
real_socket = socket.socket
def no_v6(fam=socket.AF_INET, *a, **k):
    if fam == socket.AF_INET6:
        raise OSError(97, "Address family not supported by protocol")
    return real_socket(fam, *a, **k)
m2.socket = type("S", (), {"socket": staticmethod(no_v6), "AF_INET": socket.AF_INET, "AF_INET6": socket.AF_INET6,
                           "SOCK_DGRAM": socket.SOCK_DGRAM})
m2.sh = lambda c, check=True: ""
try:
    r = m2._udp443_ok()
    check(f"没开 IPv6 的机器：不崩（{r}）", r in (True, False))
except OSError as e:
    check(f"没开 IPv6 的机器：不崩（{e}）", False)

# grpc-tls：nginx 前置
m.NGINX_WS.clear()
ib, lk = m.sb_vless_grpc_tls(30002, "P·grpc-tls")
svc = ib["transport"]["service_name"]
check("nginx 前置：本地明文 grpc、登记进 nginx（kind=grpc）", ib["listen"] == "127.0.0.1" and "tls" not in ib
      and m.NGINX_WS == [{"path": "/" + svc, "port": 30002, "kind": "grpc"}])
check("链接：443 + 自己的域名 + tls + grpc 服务名", "@d.example:443?" in lk and "security=tls" in lk and "sni=d.example" in lk
      and f"serviceName={svc}" in lk and "type=grpc" in lk)
loc = m._nginx_ws_locations()
check("nginx：/服务名/ 前缀匹配、grpc_pass", f"location /{svc}/ {{" in loc and "grpc_pass grpc://127.0.0.1:30002;" in loc)
check("nginx：不是 gRPC 请求回 404（走伪装站自己的 404 页，跟不存在的页面一样；mack-a 同做法）",
      loc.index(f"location /{svc}/") < loc.index('if ($content_type !~ "application/grpc")') < loc.index("grpc_pass grpc://127.0.0.1:30002;"))
m.NGINX_WS[:] = [{"path": "/xp", "port": 30009, "kind": "xhttp"}]
check("xhttp 不加这条（它的请求不带 grpc 的 content-type）", "content_type" not in m._nginx_ws_locations())
d = m.link_to_proxy(lk)
check(f"mihomo：vless + tls + grpc（{d.get('network')}）", d["type"] == "vless" and d.get("tls") in (True, "true")
      and d.get("network") == "grpc" and d["grpc-opts"]["grpc-service-name"] == svc and "reality-opts" not in d)
k = m.proto_key(d)
ob = m.mihomo_to_sb_outbound(k, dict(d, tls=True))
check(f"sing-box 订阅：认成 vless-grpc、出站 grpc 传输（{k}）", k == "vless-grpc" and ob["transport"] == {"type": "grpc", "service_name": svc}
      and ob["tls"]["server_name"] == "d.example" and "reality" not in ob["tls"])
sr = m.shadowrocket_line("P·grpc-tls", d)
check(f"小火箭：obfs=grpc、服务名进 obfs-uri（{(sr or '')[:80]}…）", sr and "obfs=grpc" in sr and f"obfs-uri={svc}" in sr and "tls=true" in sr)

# 从配置还原 nginx 反代表（增删后重建用）
tmp = tempfile.mkdtemp()
m._CORE_META = {"sb": ("sing-box", "x", f"{tmp}/sb.json", m.SB, "¹"), "xray": ("xray", "x", f"{tmp}/xr.json", m.XRAY, "²")}
json.dump({"inbounds": [ib]}, open(f"{tmp}/sb.json", "w"))
ws, st = m._nginx_backends_from_cfg()
check(f"按配置重建：认得出 grpc-tls（{ws}）", ws == [{"path": "/" + svc, "port": 30002, "kind": "grpc"}] and st == [])

# 不前置：自带证书、自己的端口
m.G["nginx"] = ""
ib, lk = m.sb_vless_grpc_tls(30003, "P·grpc-tls")
check("没有 nginx 前置：自带证书 h2、听自己的端口", ib["listen"] == "::" and ib["tls"]["alpn"] == ["h2"] and ":30003?" in lk)
check("reality-grpc 还在两张表里", "reality-grpc" in m.SB and "reality-grpc" in m.XRAY and m.SB["grpc-tls"]["ws_front"])
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
