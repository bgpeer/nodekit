#!/usr/bin/env python3
"""伪装站文章读着要通顺（仓库主人 10/23 截图：《烤面包笔记》下面写「还剩几页没读完」、《秋天的早晨》日期是一月、两篇同一天）。
  现在：每种标题句式配一组跟它对得上的句子（填同一个地点 / 话题 / 食物），第一句当首页摘要；标题带季节 / 月份的，
  日期落在那个季节 / 月份里；同一个站里日期不撞；散步 / 周末出游各用各的地点（不会「沿着公园走」「在车站过周末」）。
  每台机器照样随机、各不一样。
"""
import os, sys, re, glob, random, tempfile, datetime, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
SM = {"winter": (12, 1, 2), "spring": (3, 4, 5), "summer": (6, 7, 8), "autumn": (9, 10, 11)}
def pdate(s):
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
wrong_season, dup, left, mismatch, names, n_posts = [], 0, 0, [], set(), 0
for seed in range(300):
    d = tempfile.mkdtemp()
    names.add(m.fake_site_write(d, random.Random(seed)))
    for f in glob.glob(f"{d}/**/*.html", recursive=True):
        if re.search(r"\{[a-zA-Z]+\}", re.sub(r"<style>.*?</style>", "", open(f).read(), flags=re.S)):
            left += 1
    idx = open(f"{d}/index.html").read()
    items = re.findall(r'<h2><a href="/posts/([^"]+)\.html">([^<]+)</a></h2><div class="meta">([^<]+)</div>', idx)
    dup += len({x[2] for x in items}) != len(items)
    for slug, title, dm in items:
        n_posts += 1
        dt = pdate(dm)
        for se, ms in SM.items():
            if se in title.lower() and dt.month not in ms:
                wrong_season.append((title, dm))
        for i, mo in enumerate(m._FS_MONTH):
            if mo in title and dt.month != i + 1:
                wrong_season.append((title, dm))
        # 摘要（正文第一句）里要出现标题里填进去的那个词
        post = open(f"{d}/posts/{slug}.html").read()
        first = re.search(r"<article>.*?<p>([^<]+)</p>", post, re.S).group(1).replace("&#x27;", "'")
        for tpl, lines in m._FS_THEMES[next(k for k in m._FS_THEMES if any(re.fullmatch(re.sub(r"\\\{\w+\\\}", ".+", re.escape(t)), title) for t in m._FS_THEMES[k]))].items():
            rx = re.sub(r"\\\{\w+\\\}", "(.+)", re.escape(tpl))
            mt = re.fullmatch(rx, title)
            if mt:
                words = [w for w in mt.groups()]
                if not any(w.lower() in first.lower() for w in words) and any("{" in x for x in lines[:1]):
                    mismatch.append((title, first))
                break
check(f"{n_posts} 篇：标题带季节 / 月份的，日期都落在那个季节 / 月份里（不对的 {wrong_season[:3]}）", not wrong_season)
check(f"同一个站里日期不撞（撞了的站 {dup} 个）", dup == 0)
check(f"没有没填上的占位符（{left}）", left == 0)
check(f"摘要（第一句）跟标题说的是同一件事（对不上的 {mismatch[:3]}）", not mismatch)
check(f"每个站照样各不一样（300 个站 {len(names)} 个不同的站名）", len(names) > 150)
check("散步 / 周末出游各用各的地点，「沿着公园 / 在车站过周末」不会出现",
      "the park" not in m._FS_WALK and "the station" not in m._FS_WALK + m._FS_TRIP)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
