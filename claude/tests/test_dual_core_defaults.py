#!/usr/bin/env python3
"""两个核心都装时回车的默认（仓库主人 10/29：「singbox回车只安装Xray这边没有的全部协议，Xray这边回车安装全部协议」，
  听了两条后果后选「按你建议的改」）：xray 回车 = 全部但不含 trojan；sing-box 回车 = xray 没有的 + reality-vision
  （xray 的 reality sing-box 系客户端连不上）。只装一个核心时回车照旧全装。
"""
import os, sys, io, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

class Stop(Exception):
    pass
def run(core):
    seen = {}
    def pick(title, opts, default=None):
        seen[title] = default
        return []                                       # 都不选 → 向导说「没选任何协议」退出
    m._pick = pick
    m._installed_state = lambda: ({}, [], [])
    m.read_saved_links = lambda: []
    ans = iter([core])
    m._ask = lambda p="": next(ans, "")
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            m.install_flow()
        except (Stop, SystemExit, StopIteration):
            pass
    sb = next((v for k, v in seen.items() if "sing-box" in k), "无")
    xr = next((v for k, v in seen.items() if "xray" in k), "无")
    return sb, xr

sb, xr = run("3")
check(f"两个都装：sing-box 回车 = xray 没有的 + reality-vision（{sb}）",
      sb == ["hy2", "reality-vision", "tuic", "vmess-httpupgrade", "anytls", "grpc-tls"])
check(f"两个都装：xray 回车 = 全部、不含 trojan（{xr}）", xr == [n for n in m.XRAY if n != "trojan"] and "trojan" not in xr)
check("两边的协议加起来什么都不缺（trojan 照样能手动勾）", set(sb) | set(xr) | {"trojan"} == set(m.SB) | set(m.XRAY))
sb1, _ = run("1")
_, xr2 = run("2")
check(f"只装一个核心：回车照旧全装（{sb1} / {xr2}）", sb1 is None and xr2 is None)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
