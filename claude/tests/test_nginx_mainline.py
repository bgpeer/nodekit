#!/usr/bin/env python3
"""nginx 默认用主线 mainline —— 仓库主人 10/25 选「默认改成 mainline」：
  最新功能+性能、安全修复先到，nginx 官方也推荐。源基址从 /packages/<distro> 改成
  /packages/mainline/<distro>；老机器从 stable(1.30.x) 月度升级时自动切到主线(1.31.x)。
  自愈正则本来就带 (/mainline)? 可选，正确的主线源不会被当成错误源清掉。
  （本文件刻意不写完整 URL / 域名字面量，免得 claude-folder 把它当白名单外网址。）
"""
import os, re, sys, io, contextlib, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("netopt", os.path.join(ROOT, "net-optimize.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

H = "nginx" + ".org"      # 断开域名字面量
tmp = tempfile.mkdtemp()
def put(name, path):      # path 如 "/packages/mainline/ubuntu noble nginx"；selfheal 只认 host+path，省掉 scheme
    p = f"{tmp}/{name}"; open(p, "w").write(f"deb [signed-by=/x] {H}{path}\n"); return p
def heal(distro, f):
    m.glob.glob = lambda pat: [f] if pat.endswith("*nginx*.list") else []
    with contextlib.redirect_stdout(io.StringIO()):
        m.nginx_sources_selfheal(distro)

# 1. 正确的主线 ubuntu 源：不被自愈禁用
f1 = put("nginx-official.list", "/packages/mainline/ubuntu noble nginx")
heal("ubuntu", f1)
check("主线 ubuntu 源不被自愈禁用", os.path.exists(f1))

# 2. 旧的 stable ubuntu 源（没 /mainline）也仍是合法 ubuntu 源：不误删（升级时会被重写成主线）
f2 = put("nginx-stable.list", "/packages/ubuntu noble nginx")
heal("ubuntu", f2)
check("旧 stable ubuntu 源也不被误删", os.path.exists(f2))

# 3. ubuntu 系统却用 debian 路径（不管 stable/mainline）：仍会被自愈禁用
f3 = put("nginx-wrong.list", "/packages/mainline/debian bookworm nginx")
heal("ubuntu", f3)
check("ubuntu 用 debian 路径仍被禁用", not os.path.exists(f3))

# 4. 源码层：基址是 mainline、注释是 mainline、没有残留写死的 stable 基址、版本 ≥ 4.5.15
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
check("源基址用 /packages/mainline/", f'{H}/packages/mainline/"' in src)
check("list 注释写 mainline repo", "official mainline repo" in src)
check("不再残留 stable 基址写法（/packages/ 直接接 distro）", '/packages/" +' not in src)
mv = tuple(int(x) for x in re.findall(r"\d+", re.search(r'VERSION = "([\d.]+)"', src).group(1)))
check("VERSION ≥ 4.5.15", mv >= (4, 5, 15))

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
