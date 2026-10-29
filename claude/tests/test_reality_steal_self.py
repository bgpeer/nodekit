#!/usr/bin/env python3
"""reality「偷自己」（仓库主人 10/29：「reality 借用自己的网站，不借大厂的，这个可以做比如输入a=偷自己域名」
  「输入a或自己域名都是相当于偷自己的」）。借大厂时墙看到 SNI 写着 www.boeing.com、连的却是一台 VPS 的 IP；
  偷自己是 SNI = 本机域名，这台上真有这个网站，扫过来看到的就是它。
  只在 nginx 443 分流下做：stream 按 SNI 把本机域名交给 reality，reality 把不是自己客户端的连接转给本机
  127.0.0.1:SNI_HTTPS_PORT 上的网站（ws / grpc-tls 也这样经过它到 nginx）；map 里本机域名那行不再单列（重复键 nginx -t 不过）。
  只能一个核心偷（分流按域名分），xray 那个照旧借大厂；xray 偷的话抗量子签名按证书链长短自动关，密钥交换不受影响。
"""
import os, io, sys, json, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
DOM = "us.node.example"
x.G.update({"domain": DOM, "host": DOM, "sni_split": "1", "sni": DOM, "sni_xr": "www.boeing.com"})
x.reality_keys = lambda *a: ("PRIV", "PUB")

print("一、握手目标")
check("本机域名 = 偷自己（大小写不论）", x._sni_is_self(DOM) and x._sni_is_self(DOM.upper()) and not x._sni_is_self("www.boeing.com"))
check(f"偷自己转给本机网站 127.0.0.1:{x.SNI_HTTPS_PORT}，借大厂转 大厂:443",
      x._reality_dest(DOM) == ("127.0.0.1", x.SNI_HTTPS_PORT) and x._reality_dest("www.boeing.com") == ("www.boeing.com", 443))
x.NGINX_STREAM[:] = []
ib, lk = x.sb_reality_vision(24001, "P·reality-vision")
hs = ib["tls"]["reality"]["handshake"]
check(f"sing-box reality：SNI 是本机域名、握手转本机网站（{hs}）",
      ib["tls"]["server_name"] == DOM and hs == {"server": "127.0.0.1", "server_port": x.SNI_HTTPS_PORT})
check("链接：443、sni=本机域名", f"@{DOM}:443?" in lk and f"sni={DOM}&" in lk)
x.NGINX_STREAM[:] = [{"sni": DOM, "port": 24001}, {"sni": "www.boeing.com", "port": 24002}]
conf = x._stream_conf_text()
check("443 分流：本机域名只交给 reality（不再单列网站那行，重复键 nginx -t 过不了）",
      conf.count(f"    {DOM}  ") == 1 and f"    {DOM}  127.0.0.1:24001;" in conf
      and f"default  127.0.0.1:{x.SNI_HTTPS_PORT};" in conf)
x.NGINX_STREAM[:] = [{"sni": "www.boeing.com", "port": 24002}]
check("没偷自己：本机域名照旧直给网站", f"    {DOM}  127.0.0.1:{x.SNI_HTTPS_PORT};" in x._stream_conf_text())
x.G["sni_xr"] = DOM
check("xray 偷自己：dest 也转本机网站", x._xr_reality_stream("P", "ab", "raw")["realitySettings"]["dest"] == f"127.0.0.1:{x.SNI_HTTPS_PORT}")
x.G["sni_xr"] = "www.boeing.com"
check("xray 借大厂照旧", x._xr_reality_stream("P", "ab", "raw")["realitySettings"]["dest"] == "www.boeing.com:443")

print("二、菜单 4 换伪装域名")
sbc = {"inbounds": [{"tls": {"server_name": "www.airbus.com", "reality": {"handshake": {"server": "www.airbus.com", "server_port": 443}}}}]}
x._set_sni_singbox(sbc, DOM)
check("换成偷自己：握手改转本机网站", sbc["inbounds"][0]["tls"]["reality"]["handshake"] == {"server": "127.0.0.1", "server_port": x.SNI_HTTPS_PORT})
x._set_sni_singbox(sbc, "www.airbus.com")
check("换回大厂：端口也回 443", sbc["inbounds"][0]["tls"]["reality"]["handshake"] == {"server": "www.airbus.com", "server_port": 443})
xc = {"inbounds": [{"streamSettings": {"realitySettings": {"dest": "www.airbus.com:443", "serverNames": ["www.airbus.com"]}}}]}
x._set_sni_xray(xc, DOM)
check("xray 同理", xc["inbounds"][0]["streamSettings"]["realitySettings"]["dest"] == f"127.0.0.1:{x.SNI_HTTPS_PORT}")
check("手动输入里认 a 和本机域名", 'v == "a" or (v and v == (G.get("domain") or "").lower())' in src)

print("三、问伪装域名")
ASK = []
x._ask = lambda p="": (print(p), ASK.pop(0))[1] if ASK else ""
def ask(own, *ans, avoid=""):
    ASK[:] = list(ans)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = x.ask_reality_sni("伪装域名", avoid=avoid, own=own)
    return r, buf.getvalue()
(r, _), out = ask(DOM, "a")
check(f"输 a = 偷自己，提示里写着 a=偷自己（{r}）", r == DOM and "a=偷自己" in out)
(r, _), _ = ask(DOM, DOM)
check("直接输本机域名也一样", r == DOM)
(r, _), out = ask("", "a", "www.ibm.com")
check("这台用不了（没域名 / 没走 443 分流）：红字说清、回去重填", r == "www.ibm.com" and "用不了" in out)
(r, _), out = ask(DOM, "a", "www.ibm.com", avoid=DOM)
check("另一个核心已经偷了：重复、重填", r == "www.ibm.com" and "重复" in out)

print("四、别的地方")
u = f"vless://u@{DOM}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni={DOM}&fp=chrome&pbk=P&sid=ab&type=tcp#n"
v = x._link_rehost(u, DOM, "k7xq2m4p9z.node.example")
check("私有解析换地址：偷自己的 reality 不换 SNI（443 分流只认本机域名）", "@k7xq2m4p9z.node.example:443" in v and f"sni={DOM}&" in v)
w = f"vless://u@{DOM}:443?encryption=none&security=tls&sni={DOM}&type=ws&host={DOM}&path=%2Fa#w"
check("ws 照旧跟着换", "sni=k7xq2m4p9z.node.example" in x._link_rehost(w, DOM, "k7xq2m4p9z.node.example"))
x.G["sni"] = DOM
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    x._sni_precheck_one("sni")
check("装前预检：偷自己不去探（网站装好才有）", "偷自己" in buf.getvalue())
check("安装向导：偷自己直接走 443 分流、不再问", "if steal_self:\n        split = \"1\"" in src)
check("分流装不上：伪装域名换回大厂、说一行", "偷自己做不了，reality 伪装域名改成" in src)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
