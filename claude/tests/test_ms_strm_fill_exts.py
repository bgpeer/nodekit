#!/usr/bin/env python3
"""补扫 AutoFilm 漏掉的视频格式（1.5.433）。

AutoFilm 的 VIDEO_EXTS 写死了十种，.mov 这类它直接跳过 —— 扫描统计里「发现文件」
照样数它，就是不生成 strm。真机：仙逆 S01E168.4K.mov（869MB）在网盘里，扫描报
「发现文件 102 个、新增 0」，Emby 里只有到 167 集。
钉住：
  一、.mov 及其他补扫格式能生成 strm，内容是 AlistPath 形态（网盘路径），落在
     同样的镜像目标目录；AutoFilm 已有的格式（.mp4/.mkv 等）不动它的活
  二、非视频（字幕/图片/压缩包/文本）不生成
  三、已有 strm 的不重复生成、不覆盖（跟 overwrite: false 一个意思）
  四、计数正确：新增 / 已有跳过 / 失败 / 没列出来的目录
  五、登不上 OpenList 就跳过，不报错；只扫一个盘时不碰别的盘
  六、DISC_VIDEO_EXT（原盘挑正片那张）一个字没动"""
import importlib.util, os, sys, tempfile, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

D = tempfile.mkdtemp()
open(os.path.join(D, ".env"), "w").write("DATA_ROOT=%s\n" % os.path.join(D, "media"))
m.ms_state = lambda: {}
PATHS = ["/quark", "/aliyun"]
m.effective_scan_paths = lambda d: list(PATHS)
m._ol_token = lambda d: "TOK"

# 网盘树：名字是假的，只借 S01E168.4K.mov 这个形态
TREE = {
    "/quark": [("动漫", True)],
    "/quark/动漫": [("示例剧集", True), ("电影", True)],
    "/quark/动漫/示例剧集": [
        ("S01E167.4K.mp4", False), ("S01E168.4K.mov", False),
        ("S01E169.4K.m4v", False), ("S01E170.4K.mts", False),
        ("S01E171.4K.3gp", False), ("S01E172.4K.MOV", False),
        ("S01E180.4K.mov", False), ("S01E181.4K.mov", False),
        ("readme.txt", False), ("cover.jpg", False),
        ("S01E173.4K.srt", False), ("pack.zip", False),
        ("花絮", True),
    ],
    "/quark/动漫/示例剧集/花絮": [("interview.mov", False), ("photo.png", False)],
    "/quark/动漫/电影": [("示例电影.m2t", False), ("示例电影2.mkv", False)],
    "/aliyun": [("剧集", True)],
    "/aliyun/剧集": [("S01E01.mov", False)],
}
FAIL_DIRS = set()
def fake_ls(path, tok):
    if path in FAIL_DIRS:
        return None
    return [{"name": n, "is_dir": d} for n, d in TREE.get(path, [])]
m._ol_ls = fake_ls

def strm_host(*parts):
    return os.path.join(D, "media", "strm", "cloud", *parts)

def reset():
    shutil.rmtree(os.path.join(D, "media"), ignore_errors=True)
    # 已有的 strm：.mp4 那集是 AutoFilm 生成的；180 集的 .mov 已有 strm（带一个换行，
    # 补扫要是重写了它，换行就没了）；181 集的计划位置被一个指向别的片的 strm 占了
    p167 = strm_host("quark", "动漫", "示例剧集", "S01E167.4K.strm")
    os.makedirs(os.path.dirname(p167), exist_ok=True)
    open(p167, "w").write("/quark/动漫/示例剧集/S01E167.4K.mp4")
    p180 = strm_host("quark", "动漫", "示例剧集", "S01E180.4K.strm")
    open(p180, "w").write("/quark/动漫/示例剧集/S01E180.4K.mov\n")
    p181 = strm_host("quark", "动漫", "示例剧集", "S01E181.4K.strm")
    open(p181, "w").write("/quark/动漫/示例剧集/别的片.mp4")

print("一、.mov 及其他补扫格式能生成")
reset(); FAIL_DIRS.clear()
r = m.fill_missing_strm(D)
check("新增 8 个（168.mov、169.m4v、170.mts、171.3gp、172.MOV、"
      "花絮/interview.mov、电影/m2t、aliyun 的 S01E01.mov）", r["created"] == 8)
check("已有跳过 2 个（180 已有 strm、181 位置被占）", r["skipped"] == 2)
check("失败 0，没列出来的目录 0", r["failed"] == 0 and r["unlisted"] == 0)
p168 = strm_host("quark", "动漫", "示例剧集", "S01E168.4K.strm")
check("168 集 strm 落在镜像目标目录，内容是 AlistPath 形态（网盘路径）",
      os.path.exists(p168)
      and open(p168).read() == "/quark/动漫/示例剧集/S01E168.4K.mov")
check("大写 .MOV 也补", os.path.exists(
    strm_host("quark", "动漫", "示例剧集", "S01E172.4K.strm")))
check("子目录里的 .mov 也补", os.path.exists(
    strm_host("quark", "动漫", "示例剧集", "花絮", "interview.strm")))
check("m2t 也补", os.path.exists(
    strm_host("quark", "动漫", "电影", "示例电影.strm")))

print("二、AutoFilm 的格式和非视频不动")
check(".mp4 没多生成（167 集还是原来那一个）",
      open(strm_host("quark", "动漫", "示例剧集", "S01E167.4K.strm")).read()
      == "/quark/动漫/示例剧集/S01E167.4K.mp4")
check(".mkv 不生成", not os.path.exists(
    strm_host("quark", "动漫", "电影", "示例电影2.strm")))
for f in ("readme.strm", "cover.strm", "S01E173.4K.strm", "pack.strm",
          "photo.strm"):
    check("不生成：" + f, not os.path.exists(
        strm_host("quark", "动漫", "示例剧集", f))
        and not os.path.exists(strm_host("quark", "动漫", "示例剧集", "花絮", f)))

print("三、已有 strm 不重复、不覆盖")
check("180 集的 strm 内容原样不动（含换行）",
      open(strm_host("quark", "动漫", "示例剧集", "S01E180.4K.strm")).read()
      == "/quark/动漫/示例剧集/S01E180.4K.mov\n")
check("181 集的 strm 没被覆盖（还是指向别的片）",
      open(strm_host("quark", "动漫", "示例剧集", "S01E181.4K.strm")).read()
      == "/quark/动漫/示例剧集/别的片.mp4")

print("四、再跑一轮：幂等")
r2 = m.fill_missing_strm(D)
check("第二轮新增 0，全部算已有跳过", r2["created"] == 0 and r2["skipped"] == 10
      and r2["failed"] == 0)

print("五、列不出来的目录")
reset(); FAIL_DIRS.clear(); FAIL_DIRS.add("/quark/动漫/电影")
r = m.fill_missing_strm(D)
check("没列出来的目录记 1 个，那个目录的 .m2t 没生成",
      r["unlisted"] == 1 and not os.path.exists(
          strm_host("quark", "动漫", "电影", "示例电影.strm")))
check("别的目录照常补（168 集生成了）", r["created"] == 7
      and os.path.exists(p168))

print("六、登不上、只扫一个盘")
reset(); FAIL_DIRS.clear()
m._ol_token = lambda d: ""
r = m.fill_missing_strm(D)
check("登不上：全 0，不报错、不生成", r == {"created": 0, "skipped": 0,
      "failed": 0, "unlisted": 0} and not os.path.exists(p168))
m._ol_token = lambda d: "TOK"
r = m.fill_missing_strm(D, only="/aliyun")
check("只扫 /aliyun：quark 的 .mov 不动，aliyun 的补了",
      r["created"] == 1 and not os.path.exists(p168)
      and os.path.exists(strm_host("aliyun", "剧集", "S01E01.strm")))

print("七、清单和版本")
want = {".mov", ".m4v", ".mts", ".m2t", ".3gp", ".3g2", ".f4v", ".asf",
        ".vob", ".ogv", ".m2t", ".wtv", ".dvr-ms", ".divx", ".mpeg", ".mpe",
        ".mpv", ".xvid", ".ogm"}
check("补扫清单含任务要求的格式", want <= set(m.FILL_VIDEO_EXTS))
check("补扫清单不含 AutoFilm 已有的格式",
      not (set(m.FILL_VIDEO_EXTS) & {".mp4", ".mkv", ".flv", ".avi", ".wmv",
                                     ".ts", ".rmvb", ".webm", ".mpg", ".m2ts"}))
check("补扫清单不含字幕/图片/压缩包",
      not (set(m.FILL_VIDEO_EXTS) & {".srt", ".ass", ".jpg", ".png", ".zip",
                                     ".nfo", ".rar"}))
check("DISC_VIDEO_EXT 一个字没动",
      m.DISC_VIDEO_EXT == (".m2ts", ".mts", ".ts", ".mkv", ".mp4", ".m4v"))
check("media-stack 1.5.433 起",
      tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 433))

shutil.rmtree(D, ignore_errors=True)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
