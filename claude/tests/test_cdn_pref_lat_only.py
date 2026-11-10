#!/usr/bin/env python3
"""CDN 优选加「只测延时、不测下载」开关（仓库主人 11/11：「那就加一个只测延时不测下载的开关吧」）。
  下载测速一轮最多走 n_top × dl_mb 的流量（默认 60 MB）。开了以后不下载：握手延时最低的一批再各测 3 次取中间值，
  按延时从低到高选。默认关，关着时照旧先筛延时、再测下载速度，一个字不变。
"""
import os, io, sys, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
x.BGP_DIR = tmp; x.CDN_PREF_FILE = f"{tmp}/cdn-pref.json"
NODE = {"id": 1, "proto": "vless-ws", "core": "sing-box", "domain": "cdn.example", "cred": "u", "path": "/p", "cf_port": 2053,
        "tag": "CDN·vless-ws", "svc": "xy-cdn", "in_sub": False, "pref": ""}
x._cdn_load = lambda: [dict(NODE)]
x._cf_cidrs = lambda: (["1.2.3.0/24"], True)
IPS = [f"1.2.3.{i}" for i in range(1, 41)]
x._sample_ips = lambda cidrs, n: list(IPS)
RTT = {ip: 10.0 + i for i, ip in enumerate(IPS)}          # 1.2.3.1 最低，往后一个比一个高
RTT["1.2.3.3"] = None                                      # 这个不通
FLAKY, CALLS, DL = {"1.2.3.2": 0}, [], []
def rtt(addr, port, timeout):
    CALLS.append(addr)
    if addr in FLAKY:                                      # 第一次握手很快，再测就不通：只测一次会把它排第一
        FLAKY[addr] += 1
        return 1.0 if FLAKY[addr] == 1 else None
    return RTT[addr]
x._tcp_rtt = rtt
x._dl_mbps = lambda ip, port, cfg: DL.append(ip) or 50.0
x._cdn_resync = lambda old, was: True
ANS = []
x._ask = lambda p="": (print(p, end="") or ANS.pop(0)) if ANS else "0"
def quiet(f, *a, **k):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = f(*a, **k)
    return r, b.getvalue()

print("一、默认关：照旧")
check("默认是关", x._pref_load()["lat_only"] is False and x.CDN_PREF_DEFAULTS["lat_only"] is False)
res, out = quiet(x.cdn_speedtest, x._pref_load())
check("关着：延时前 6 名照旧做下载测速，结果带 Mbps", len(DL) == 6 and all(r["mbps"] == 50.0 for r in res) and "下载测速" in out)

print("二、开：不下载，按延时选")
cfg = x._pref_load(); cfg["lat_only"] = True
DL.clear(); CALLS.clear(); FLAKY["1.2.3.2"] = 0
extra = {}
res, out = quiet(x.cdn_speedtest, cfg, extra=extra)
check("一次下载都没做（不耗流量）", not DL and "不下载、不耗流量" in out)
check("按延时从低到高排，结果里 mbps 是 None", [r["ip"] for r in res[:3]] == ["1.2.3.1", "1.2.3.4", "1.2.3.5"]
      and all(r["mbps"] is None for r in res) and res[0]["ms"] == 10.0)
check("最低的一批各复测 3 次：第一次很快、再测就不通的不要（单次握手不作数）", "1.2.3.2" not in [r["ip"] for r in res]
      and CALLS.count("1.2.3.1") == 4)
check("本来就不通的不在里面", "1.2.3.3" not in [r["ip"] for r in res])
check("复测的是最低的 15 个（写 5 条的 3 倍），后面排得上的 10 个留作后备", len(res) == 14 and len(extra["ranked"]) == 10
      and extra["ranked"][0] == "1.2.3.17")
cfg["min_mbps"] = 100.0
res2, _ = quiet(x.cdn_speedtest, cfg)
check("设了速度下限也不影响（没测速度，谈不上低于下限）", len(res2) >= 5)

print("三、菜单")
out = quiet(x.cdn_pref_menu)[1]
check("优选菜单多一项「6. 只测延时（不测下载、不耗流量）　当前：关」，原来 1~5 的编号不变",
      "6. 只测延时（不测下载、不耗流量）　当前：关" in out and "1. 测速筛候选" in out and "5. 更换优选 IP" in out and "0. 返回" in out)
ANS[:] = ["6", "0"]
out = quiet(x.cdn_pref_menu)[1]
check("点 6：开，存下来，屏上一行 ✔", json.load(open(x.CDN_PREF_FILE))["lat_only"] is True and "✔ 只测延时：开" in out and "当前：开" in out)
DL.clear(); ANS[:] = []
out = quiet(x.cdn_pref_scan)[1]
st = json.load(open(x.CDN_PREF_FILE))
check("开着时「1 测速筛候选」：一行提示不耗流量、不再问「继续?」，直接测", "只测延时，不下载、不耗流量" in out and "继续?" not in out and not DL)
check("选出 5 条写成候选，延时最低的排第一；后备也记下", st["cands"] == ["1.2.3.1", "1.2.3.4", "1.2.3.5", "1.2.3.6", "1.2.3.7"]
      and len(st["spare"]) >= 5)
check("结果那一屏只写延时，不写 Mbps", "本机测 10.0ms" in out and "Mbps" not in out and "None" not in out)
out = quiet(x.cdn_pref_menu)[1]
check("菜单「上次测速」写延时最低的那个", "延时最低 1.2.3.1 / 10.0 ms" in out and "None" not in out)
ANS[:] = ["6", "0"]
out = quiet(x.cdn_pref_menu)[1]
check("再点 6：关", json.load(open(x.CDN_PREF_FILE))["lat_only"] is False and "✔ 只测延时：关" in out)
ANS[:] = [""]
out = quiet(x.cdn_pref_scan)[1]
check("关着时照旧先提示流量、问「继续?」，回车不测", "MB 流量" in out and "继续?" in out and not DL)
ANS[:] = ["y"]
out = quiet(x.cdn_pref_scan)[1]
check("关着时结果照旧带 Mbps，「上次测速」照旧写最快", "/ 50.0 Mbps" in out and "最快 " in quiet(x.cdn_pref_menu)[1])

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
