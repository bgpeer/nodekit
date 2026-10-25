#!/usr/bin/env python3
"""「8 更新」换了 OpenList 的容器 → 等它起好再重启 MediaWarp（1.5.387）。
真机 10/25：「8 更新」把 OpenList 换成 v4.2.9，之后只有开播门先替它取好地址的那一集能播，别的集一点就闪退
（MediaWarp 手里的令牌作废，或者登录时 OpenList 还没好）。每小时那一轮最多晚一小时才修。
钉住：
  一、拉镜像之前记下 OpenList 这次是什么时候起来的
  二、刷完配置之后：OpenList 被重启了（起来的时刻变了）才 restart_mediawarp_when_ready（等 OpenList 好了再重启）
  三、没变就不碰 MediaWarp（重启会清空直链缓存，第六节）
  四、restart_mediawarp_when_ready 是先等 OpenList 再重启（顺序反了照样拿不到令牌）
"""
import importlib.util, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
src = open(SRC, encoding="utf-8").read()
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
up = src[src.index("def do_update("):]; up = up[:up.index("\ndef ", 1)]
i0 = up.index('_ol_start0 = _started_at("openlist")[1]')
i_pull = up.index("pull_images(compose, env_file)")
i_up = up.index("up -d")
i_cfg = up.index("for path, gen, svc in ((mw_cfg, gen_mediawarp_conf")
i_fix = up.index('if _started_at("openlist")[1] != _ol_start0:')
print("一、二、三")
check("拉镜像之前记下 OpenList 起来的时刻", i0 < i_pull < i_up)
check("刷完配置之后才看（配置那一步也可能重启 MediaWarp）", i_cfg < i_fix)
check("变了才等 OpenList 好了再重启 MediaWarp", "restart_mediawarp_when_ready(d)" in up[i_fix:i_fix + 120])

print("四、先等再重启")
ORDER = []
m.wait_openlist_ready = lambda d, timeout=90: ORDER.append("等 OpenList") or True
m.subprocess.run = lambda args, **k: ORDER.append(" ".join(args))
m.info = lambda *a, **k: None
check("先等 OpenList、再重启 MediaWarp", m.restart_mediawarp_when_ready("D") is True
      and ORDER == ["等 OpenList", "docker restart mediawarp"])
ORDER.clear()
m.wait_openlist_ready = lambda d, timeout=90: ORDER.append("等 OpenList") or False
m.warn = lambda *a, **k: None
check("等不到：不重启", m.restart_mediawarp_when_ready("D") is False and ORDER == ["等 OpenList"])
print("五、_started_at")
class R:
    def __init__(self, out): self.stdout, self.stderr, self.returncode = out, "", 0
m.sh = lambda cmd, **k: R("'2026-10-25T12:23:07.123456789Z'\n")
t, raw = m._started_at("openlist")
check("读得懂 docker 的时刻", raw == "2026-10-25T12:23:07.123456789Z" and t and int(t) % 60 == 7)
m.sh = lambda cmd, **k: R("")
check("问不到 → (None, '')", m._started_at("openlist") == (None, ""))
check("版本 1.5.387 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 387))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
