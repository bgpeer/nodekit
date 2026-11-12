#!/usr/bin/env python3
"""「2 使用信息」重排（仓库主人 11/12：「把这个也更新一下」，附了整屏的排法）。

  原来：地址在最上面，「外部播放器」「浏览器弹框」「各服务自己的账号」「挂网盘」分四段摆在下面，看一个服务要上下对几处。
  1.5.407：开头一句话说装完怎么用；每个地址下面直接跟它自己的账号密码；Emby 那条标题里就写明是外部播放器的地址。
钉住：
  一、开头那句；段名「网页浏览器访问地址」
  二、首页入口 / Emby / OpenList 各自：标题 + 地址顶格一行 + 自己的账号密码；条间空一行
  三、Emby 每个账号一行账号、一行密码（脚本经手的显示，网页上设的说看不到）；那句必须填这个地址的提示还在
  四、旧的几段不再重复出现；常用命令、路径这些后面的段落都还在"""
import importlib.util, os, sys, io, re, tempfile, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)

T = tempfile.mkdtemp(); D = T + "/ms"; os.makedirs(D + "/homepage")
open(f"{D}/docker-compose.yml", "w").write("services: {}\n")
open(f"{D}/.env", "w").write(f"DOMAIN=media.zone.example\nDATA_ROOT={D}/media\n")
open(f"{D}/.secrets", "w").write("OPENLIST_USER=olboss\nOPENLIST_PASS=OLPASS123\nBA_USER=popuser\nBA_PASS=POPPASS\nEMBY_PW_u1=EMBYPW1\n")
m.ms_install_dir = lambda: D
m.read_emby_api_key = lambda d=None: "k"
m._emby_user_list = lambda key: [("u1", "甲", True, True), ("u2", "TV", False, True)]
m.openlist_storages = lambda d: []
m.strm_count = lambda d: 0
m.first_steps = lambda d, key=None, stores=None, n_strm=None: [(True, "x")]
def run():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            m.show_info()
        except Exception as e:                 # 后半段要问 docker，这里只看前面
            print("EXC", type(e).__name__, e)
    return plain(buf.getvalue())
out = run()
H = "https" + "://"

print("一、开头")
check("开头一句话：挂载路径扫码 → 生成媒体库 → 用播放器看", "安装完成后：到「3 挂载路径」用网盘扫码登录 →「4 生成媒体库」→ 用 Emby 之类的播放器观看" in out)
check("段名「▸ 网页浏览器访问地址」", "▸ 网页浏览器访问地址" in out and "▸ 访问地址" not in out)

print("二、每个地址下面跟自己的账号密码")
sec = out[out.index("▸ 网页浏览器访问地址"):out.index("▸ 常用命令")]
blocks = [b for b in sec.split("\n\n") if "[" in b]
check("三条，条与条之间空一行，顺序：首页入口 / Emby / OpenList", len(blocks) == 3 and "[首页入口]" in blocks[0] and "[Emby]" in blocks[1] and "[OpenList]" in blocks[2])
check("首页入口：标题带端口、地址顶格单独一行、弹框的用户名密码紧跟着",
      "[首页入口]  端口 443\n" + H + "home.media.zone.example\n      用户名   popuser\n      密  码   POPPASS" in blocks[0])
check("OpenList：地址下面是它的用户名密码（改过的用户名）",
      "[OpenList]  端口 443\n" + H + "list.media.zone.example\n      用户名   olboss\n      密  码   OLPASS123" in blocks[2])

print("三、Emby")
check("标题里写明是外部播放器的服务器地址", "[Emby] 外部播放器（Hills / Infuse / Emby App）服务器通用地址  端口 443\n" + H + "emby.media.zone.example\n" in blocks[1])
check("每个账号一行账号、一行密码；脚本经手的密码显示出来", "      账  号   甲（管理员）\n      密  码   EMBYPW1\n" in blocks[1])
check("网页上设的：说这里看不到", "      账  号   TV\n      密  码   在 Emby 网页上设的，这里看不到\n" in blocks[1])
check("「客户端必须填这个地址」那句提示还在", "提示：客户端必须填这个地址；直连 8096 会绕开 302" in blocks[1])
m._emby_user_list = lambda key: []
check("读不到账号（没 Key / Emby 没起）：说第一次打开网页时自己建的", "第一次打开 Emby 网页时自己建的" in run())

print("四、不重复、后面的都还在")
check("旧的三段不再单独出现", all(t not in out for t in ("▸ 外部播放器", "▸ 浏览器弹框", "▸ 各服务自己的账号", "▸ 挂网盘")))
check("每个密码只出现一次", all(out.count(p) == 1 for p in ("POPPASS", "OLPASS123", "EMBYPW1")))
check("常用命令、路径两段还在", "▸ 常用命令" in out and "▸ 路径" in out and "media-stack help" in out and "凭据存档" in out)
os.remove(f"{D}/.secrets"); open(f"{D}/.secrets", "w").write("OPENLIST_PASS=OLPASS123\n")
o2 = run()
check("没加弹框密码的机器：首页入口下面不印账号密码", "popuser" not in o2 and "[首页入口]" in o2)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
fn = SRC[SRC.index("def show_info"):SRC.index("def ", SRC.index("def show_info") + 10)]
check("「第一次用：按顺序做」那段还在（没做完才印）", "▸ 第一次用：按顺序做" in fn)
check("后面的段落一个没少：网盘挂载 / 媒体库内容", "▸ 网盘挂载" in fn and "▸ 媒体库内容" in fn)

print("五、版本号")
check("media-stack 1.5.407 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 407))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
