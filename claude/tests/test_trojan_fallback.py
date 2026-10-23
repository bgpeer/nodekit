#!/usr/bin/env python3
"""自己端口上的 trojan 加回落（仓库主人 10/23）：扫端口的人连上来、发的不是 trojan，回落到本机 127.0.0.1:SITE_FB_PORT
  的明文伪装站，看到的是网站首页 / 网站自己的 404 页，跟 443 上一样；以前是握手完一句话不回就断。
  sing-box trojan 用 fallback；xray trojan 用 fallbacks，TLS 只报 http/1.1（不然跟浏览器谈成 h2，回落过去明文站读不懂、回 400）。
  只在有伪装站时开（有域名 + nginx：前置 / SNI 分流 / reality 占 443 时 nginx 留 :80）；自签不开。
  vless-vision、anytls 是 sing-box 的，这两个协议没有回落功能，做不了。
  会话里真 nginx + sing-box + xray：nginx 前置 / SNI 分流 / reality 占 443 三种模式扫两个 trojan 端口，首页和 404 跟 443 逐字节相同；节点照常通。
"""
import os, sys, json, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
m.NGINX_FB_CONF = f"{tmp}/conf.d/bgpeer-fallback.conf"
import types, subprocess
rc = [0]
m.subprocess = types.SimpleNamespace(run=lambda *a, **k: subprocess.CompletedProcess(a, rc[0], "", ""))
m.sh = lambda c, check=True: ""
m.ensure_acme = lambda: ("/c.pem", "/k.pem", False)
m.G.update({"domain": "d.example", "host": "d.example", "sni": "s.example", "nginx": "1", "sni_split": "", "reality443": ""})

ib, lk = m.sb_trojan(30001, "P·trojan¹")
check("sing-box trojan：回落到本机伪装站", ib.get("fallback") == {"server": "127.0.0.1", "server_port": m.SITE_FB_PORT})
conf = open(m.NGINX_FB_CONF).read()
check("本机伪装站：只听 127.0.0.1、同一份网站、同一张 404 页", f"listen 127.0.0.1:{m.SITE_FB_PORT};" in conf
      and f"root {m.WEBROOT};" in conf and "error_page 404 /404.html;" in conf and "server_tokens off;" in conf)
ib, lk = m.xr_trojan(30002, "P·trojan²")
check("xray trojan：fallbacks + TLS 只报 http/1.1", ib["settings"].get("fallbacks") == [{"dest": m.SITE_FB_PORT}]
      and ib["streamSettings"]["tlsSettings"]["alpn"] == ["http/1.1"])
check("xray trojan 不会被当成 reality 入口", not m._xr_is_entry(ib)
      and m._xr_link_uuids({"inbounds": [ib, {"listen": "127.0.0.1", "tag": "P·reality-grpc²", "settings": {"clients": [{"id": "g"}]},
                                              "streamSettings": {"network": "grpc"}}]}, ib) == [])

m.G["nginx"] = ""
ib, _ = m.sb_trojan(30003, "P·trojan¹"); ib2, _ = m.xr_trojan(30004, "P·trojan²")
check("没有 nginx（没有伪装站）：不加回落", "fallback" not in ib and "fallbacks" not in ib2["settings"])
m.G["sni_split"] = "1"
ib, _ = m.sb_trojan(30005, "P·trojan¹")
check("SNI 分流：加回落", "fallback" in ib)
m.ensure_acme = lambda: ("/c.pem", "/k.pem", True)
ib, _ = m.sb_trojan(30006, "P·trojan¹")
check("自签：不加回落", "fallback" not in ib)

os.remove(m.NGINX_FB_CONF); rc[0] = 1
m.ensure_site_fallback()
check("nginx -t 不过：撤掉这份，不连累 443", not os.path.exists(m.NGINX_FB_CONF))
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("卸载时一起删", 'sh(f"rm -f {NGINX_FB_CONF}", check=False)' in src)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
