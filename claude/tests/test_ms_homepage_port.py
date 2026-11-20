#!/usr/bin/env python3
"""Homepage 导航页宿主机端口 3000 → 3001 → 2999（仓库主人 11/19：3000 跟 AdGuard Home 安装向导抢，
  向导绑不上、服务一起一停；11/20 再改成 2999）。只改导航页关联的端口：宿主机侧走 HOMEPAGE_PORT = 2999，
  容器里监听的还是 3000；Emby / OpenList / AdGuard 的端口一个都不动。
"""
import importlib.util, os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
check("HOMEPAGE_PORT = 2999", m.HOMEPAGE_PORT == 2999)
home = [t for t in m.SUBDOMAINS if t[0] == "home"]
check("SUBDOMAINS 里 home 那行走 HOMEPAGE_PORT（nginx 反代、显示的地址都跟这张表走）",
      len(home) == 1 and home[0][1] == 2999 and home[0][2] == "homepage")
cfg = {"has_domain": True, "install_dir": "/tmp/ms-test", "homepage": True,
       "metatube": False, "emby": True, "openlist": True}
compose = m.gen_compose(cfg)
check("新装的 compose：宿主机 2999 → 容器 3000", '"127.0.0.1:2999:3000"' in compose)
check("新装的 compose：宿主机上不再有 3000:3000", '"127.0.0.1:3000:3000"' not in compose)
src = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
HP = "http" + "://" + "home" + "page:3000"   # 拼出来，claude/ 里不留网址字面量
check("容器内健康检查还是 homepage:3000（容器里监听的还是 3000，别动）",
      ('"homepage": "' + HP + '"') in src)
# 3000 这个数字在 media-stack.py 里只许出现在：compose 冒号右边（容器端口）、
# 容器内健康检查、\u3000 全角空格的正则、EP_TRIED_MAX 这个重试次数——别的 3000 都是误改
others = [ln for ln in src.splitlines() if "3000" in ln
          and not ln.lstrip().startswith("#")          # 注释里提一句旧端口不算改
          and ":3000\"" not in ln and ("home" + "page:3000") not in ln
          and "\\u3000" not in ln and "EP_TRIED_MAX" not in ln]
check("没有别的 3000 被顺手改了", not others)
agh = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("AdGuard 的 WEB_PORT 还是 3000（向导端口，跟导航页没关系，不动）",
      "WEB_PORT = 3000" in agh)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
