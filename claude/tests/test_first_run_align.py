#!/usr/bin/env python3
"""全新安装后第一次生成：媒体库是这一趟才建的，建完要再对齐一遍（仓库主人 11/13：「东西都扫出来了为什么动漫里面没有集数
  可以播放，AV影片和电影里面点进去都是可以播放的」「感觉电视类型行规则里面差个什么引擎一样，怎么可能扫完了连集数都没有」）。

  「4 生成媒体库」收尾的顺序是：通知 Emby → align_library（剧集编号、片名……）→ 按规则建媒体库。老装机库早就在，没问题；
  全新安装时 align_library 跑的那一刻 Emby 里还没有库、一个条目都没有，「剧集编号」处理了 0 个，库建好以后没人再跑它。
  网盘里「第170集」「231 4K」这种文件名 Emby 自己认不出集号，动漫点进去就是「共0季」；电影不靠这一步，所以能播。
钉住：
  一、建库那一步把「新建 / 补了几处」返回出来
  二、这一趟新建了库 → 再跑一遍 align_library；没新建 → 不多跑（老装机不受影响）
  三、建库后那次扫描还是强制的、在再对齐之前"""
import importlib.util, os, sys, re
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
ala = SRC[SRC.index("def auto_libraries_apply"):SRC.index("\ndef auto_libraries():")]
fin = SRC[SRC.index("def strm_finish"):]; fin = fin[:fin.index("\ndef ", 10)]

print("一、建库那一步报出建了几处")
check("最后返回 made + added", ala.rstrip().splitlines()[-1].strip().startswith("return made + added"))
check("没事可做的那几处提前返回的是空（= 没新建）", len(re.findall(r"(?m)^        return$", ala)) >= 3)
check("新建后的那次扫描是强制的（去重只看本地 strm 数，这里变的是 Emby 那边）", 'label="扫描新建的媒体库", force=True' in ala)
check("扫描在返回之前", ala.index('label="扫描新建的媒体库"') < ala.rindex("return made + added"))

print("二、新建了库才再对齐一遍")
i1 = fin.index("align_library(d, key, heal=False, migrate=False)")
i2 = fin.index("if auto_libraries_apply(d, key, quiet=bool(only)):")
i3 = fin.index("align_library(d, key, heal=False, migrate=False)", i1 + 10)
check("顺序：先对齐一遍 → 建库 → 新建了就再对齐一遍", i1 < i2 < i3)
check("再对齐那一遍在 if 里面：没新建库（老装机）不多跑", fin[i2:i3].count("\n") <= 9 and "info(" in fin[i2:i3])
check("屏上一行说清为什么又做一遍", "媒体库是刚建的：再对齐一遍" in fin)
check("整个函数里 align_library 只有这两处", fin.count("align_library(d, key, heal=False, migrate=False)") == 2)

print("三、行为：用假的跑一遍这一小段")
CALLS = []
ns = {"align_library": lambda d, key, heal=True, migrate=True: CALLS.append("align"), "info": lambda t: CALLS.append("info"),
      "d": "/d", "key": "k", "only": None}
seg = fin[i1:fin.index("        finally:", i3)]
seg = "\n".join(l[12:] if l.startswith(" " * 12) else l for l in seg.splitlines())
for made in (4, 0, None):
    CALLS.clear(); ns["auto_libraries_apply"] = lambda d, key, quiet=False, _m=made: (CALLS.append("libs"), _m)[1]
    exec(seg, ns)
    want = ["align", "libs", "info", "align"] if made else ["align", "libs"]
    check(f"建库返回 {made!r}：{' → '.join(CALLS)}", CALLS == want)
check("media-stack 1.5.423 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 423))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
