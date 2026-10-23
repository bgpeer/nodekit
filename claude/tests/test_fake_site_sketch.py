#!/usr/bin/env python3
"""伪装站再像真一点，全在本机完成、不联网（仓库主人 10/23）：
  · 摄影站改成插画站：配图原来是渐变色块（「这个图怎么只有光彩没有画面」），现在按标题在本机画矢量风景（海 / 山 / 城镇 / 雨 / 窗台，
    季节定配色、早晨傍晚定太阳），措辞从「拍照」改成「画画」；不从网上下图（访问图库会暴露这台机器，同一批照片满天飞本身是特征）。
  · 文件时间跟着文章日期走：nginx 回的 Last-Modified 不再全是装机那一刻。
  · /posts/ 有归档页；没有首页的目录（/img/）回网站自己的 404 页，不回 403（nginx try_files）。
  会话里用无头 Chromium 截过图、真 nginx 上逐个路径核对过。
"""
import os, sys, re, glob, random, tempfile, datetime, calendar, importlib.util
import xml.etree.ElementTree as ET
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
seeds = [s for s in range(600) if random.Random(s).choice(list(m._FS_KINDS)) == "photo"][:40]
words, imgs_ok, xml_ok, time_ok, uniq = [], True, True, True, set()
for seed in seeds:
    d = tempfile.mkdtemp(); m.fake_site_write(d, random.Random(seed))
    text = " ".join(open(f).read() for f in glob.glob(f"{d}/**/*.html", recursive=True)).lower()
    words += [w for w in ("photograph", "camera", "lens", "shoot", "gallery") if w in re.sub(r"<style>.*?</style>", "", text, flags=re.S)]
    for f in glob.glob(f"{d}/posts/*.html"):
        if f.endswith("index.html"):
            continue
        slug = os.path.basename(f)[:-5]
        svg = f"{d}/img/{slug}.svg"
        imgs_ok &= os.path.exists(svg) and f'src="/img/{slug}.svg"' in open(f).read()
        try:
            ET.parse(svg); uniq.add(open(svg).read())
        except Exception:
            xml_ok = False
        dt = re.search(r'<div class="meta">([^<]+)</div>', open(f).read()).group(1)
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%d %b %Y"):
            try:
                day = datetime.datetime.strptime(dt, fmt).date(); break
            except ValueError:
                day = None
        mt = datetime.datetime.utcfromtimestamp(os.path.getmtime(f)).date()
        time_ok &= (mt == day) and datetime.datetime.utcfromtimestamp(os.path.getmtime(svg)).date() == day
check(f"插画站里没有摄影的说法（{set(words)}）", not words)
check("每篇文章都有本机画的配图，页面引用它", imgs_ok)
check(f"配图都是合法的 SVG、每张都不一样（{len(uniq)} 张）", xml_ok and len(uniq) > 200)
check("文章和配图的文件时间 = 文章日期（Last-Modified 不再全是装机那一刻）", time_ok)

d = tempfile.mkdtemp(); m.fake_site_write(d, random.Random(1))
check("有归档页 /posts/", os.path.exists(f"{d}/posts/index.html") and "<h1>Archive</h1>" in open(f"{d}/posts/index.html").read())
ts = {os.path.getmtime(f) for f in glob.glob(f"{d}/**/*", recursive=True) if os.path.isfile(f)}
check(f"各文件时间不是同一刻（{len(ts)} 种）", len(ts) > 3)
for t, want in (("A morning at the harbor", "<path d=\"M"), ("Rain over the old town", "stroke-width=\"1.5\""),
                ("Winter light at the hills", m._FS_SEASON_COL["winter"][4])):
    svg = m._fs_scene_svg(t, datetime.date(2026, 1, 5), random.Random(3))
    check(f"「{t}」画出来对得上", want in svg)
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
fs = src[src.index("def fake_site_write("):src.index("\ndef ", src.index("def fake_site_write(") + 5)]
check("生成过程不联网", not re.search(r"fetch_url|urlopen|requests\.|curl|wget", fs + src[src.index("def _fs_scene_svg("):src.index("def fake_site_write(")]))
m.G.update({"domain": "d.example"})
srv = m._nginx_https_server("  listen 443 ssl;\n")
check("nginx：没有首页的目录回网站的 404 页（try_files），不回 403", "try_files $uri $uri/index.html =404;" in srv)
check("老机器 nginx 没有 try_files：算老规则，自动重建一次", m._nginx_rules_old(srv.replace(" try_files $uri $uri/index.html =404;", "")))
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
