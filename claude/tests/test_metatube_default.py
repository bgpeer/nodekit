#!/usr/bin/env python3
"""MetaTube 首次安装默认带上；每天凌晨那一轮有新版自动换（仓库主人 11/13：「把这个插件做成首次安装emby的时候默认把它带着
  安装了并自动设置好，而且把他加进每天凌晨自动更新的任务里面检查到最新版就自动更新」）。

钉住：
  一、首次安装：不问，默认带上；放在起容器之前（Emby 第一次启动就带着插件和设置，不为它多重启一次）
  二、带上 = 插件文件 + 服务器地址写好 + compose 里有服务端 + 版本号记下；插件下不下来 → 说清、安装照常往下走
  三、已经装着再重跑安装：照旧问一句，默认跟现在一样
  四、每天凌晨：已是最新不动；有新版但有人在用 → 今晚不换；没人用才下载、换、重启 Emby 一次；没装的不管
  五、结果记进 selfupdate.json；出错不拖累脚本自己的更新"""
import importlib.util, os, sys, io, re, json, tempfile, contextlib, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
main = SRC[SRC.index("\ndef main():"):SRC.index("\ndef mediawarp_conf_path")]
T = tempfile.mkdtemp()
m.BGP_DIR, m.MS_STATE, m.CRON_LOCK_DIR = T + "/bgpeer", T + "/bgpeer/media-stack.json", T + "/lock"

print("一、首次安装默认带上")
check("第一次装不问、默认带；重跑才问，默认跟现在一样", 'q_yn("带上 MetaTube 番号刮削插件？", True if simple else metatube_on(cfg["install_dir"]))' in main)
i_pre, i_cmp, i_up = main.index("metatube_preinstall(cfg)"), main.index("gen_compose(cfg))"), main.index("up -d")
check("在生成 compose、起容器之前就带上（不用为它再重启 Emby）", i_pre < i_cmp < i_up)
check("版本号和文件名记进状态（给自动更新比对）", 'save_ms_state(metatube_files=cfg["metatube_files"], metatube_ver=cfg["metatube_ver"])' in main)

print("二、带上都做了什么")
D = T + "/ms"; os.makedirs(D)
FETCH = {"ok": True, "n": 0}
def fake_fetch(d, rel=None, quiet=False):
    FETCH["n"] += 1
    if not FETCH["ok"]:
        raise RuntimeError("timed out")
    m._MT_TAG[0] = (rel or ("v2", {}))[0]
    return ["MetaTube.dll"]
m._metatube_fetch_plugin = fake_fetch
cfg = {"install_dir": D}
buf = io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
    r = m.metatube_preinstall(cfg)
out = plain(buf.getvalue())
check("成了：cfg 里 metatube 置上（compose 会带上服务端容器）", r is True and cfg["metatube"] is True and cfg["metatube_ver"] == "v2" and cfg["metatube_files"] == ["MetaTube.dll"])
check("服务器地址当场写进插件的设置文件", json.load(open(m.metatube_conf_path(D)))["Server"].endswith(f"metatube:{m.METATUBE_PORT}"))
check("服务端的数据目录建好", os.path.isdir(m.metatube_dir(D)))
check("屏上一行 ✔ 说带上了哪一版", "MetaTube 插件已带上（v2）" in out)
FETCH["ok"] = False; cfg2 = {"install_dir": T + "/ms2"}; os.makedirs(cfg2["install_dir"])
buf = io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
    r = m.metatube_preinstall(cfg2)
out = plain(buf.getvalue())
check("插件下不下来：返回 False、不置 metatube，说清原因并指路，安装照常往下走", r is False and not cfg2.get("metatube")
      and "没下载下来" in out and "7 设置 → 2" in out)
FETCH["ok"] = True

print("四、每天凌晨自动更新")
ON, QUIET, REL, RUN = [True], [True], [("v3", {"name": "x"})], []
m.metatube_on = lambda d: ON[0]
m._metatube_release = lambda timeout=60: REL[0] if not isinstance(REL[0], Exception) else (_ for _ in ()).throw(REL[0])
m.play_quiet = lambda key, recent_s=600: QUIET[0]
m.read_emby_api_key = lambda d=None: "k"
m.subprocess = types.SimpleNamespace(run=lambda cmd, **k: (RUN.append(cmd), types.SimpleNamespace(returncode=0))[1])
def run():
    RUN.clear(); FETCH["n"] = 0
    return m.metatube_auto_update(D)
ON[0] = False
check("没装 MetaTube：什么都不做", run() == "" and not FETCH["n"] and not RUN)
ON[0] = True; m.save_ms_state(metatube_ver="v3")
check("已是最新：不下载、不重启", run() == "latest" and not FETCH["n"] and not RUN)
REL[0] = ("v4", {"name": "x"}); QUIET[0] = False
check("有新版，但有人在播 / 刚播过 / 问不到 Emby：今晚不换，不下载、不重启", run() == "busy" and not FETCH["n"] and not RUN
      and m.ms_state().get("metatube_ver") == "v3")
QUIET[0] = True
check("有新版、没人用：下载、换上、重启 Emby 一次", run() == "updated:v4" and FETCH["n"] == 1 and RUN == [["docker", "restart", "emby"]])
check("版本号记成新的；下一晚不再重复换", m.ms_state().get("metatube_ver") == "v4" and run() == "latest" and not RUN)
m.save_ms_state(metatube_ver="")
check("以前装的没记过版本：装一次最新的把版本记下来", run() == "updated:v4" and m.ms_state().get("metatube_ver") == "v4")
REL[0] = RuntimeError("rate limited")
check("问不到最新版（限流 / 断网）：记原因，不动", run().startswith("error:") and not RUN)
REL[0] = ("v5", {"name": "x"}); FETCH["ok"] = False
check("下载失败：记原因，版本号不变、不重启", run().startswith("error:") and m.ms_state().get("metatube_ver") == "v4" and not RUN)
fn = SRC[SRC.index("def metatube_auto_update"):SRC.index("def metatube_preinstall")]
check("先看有没有人在用，再下载（顺序不能反）", fn.index("play_quiet(") < fn.index("_metatube_fetch_plugin("))

print("五、挂在每天那一轮上")
su = SRC[SRC.index("def do_selfupdate"):SRC.index("def install_selfupdate_cron")]
check("每天的脚本自动更新里调它，结果记进 selfupdate.json", 'rec["metatube"] = metatube_auto_update(d)' in su
      and su.index("metatube_auto_update(d)") < su.rindex("json.dump(rec"))
check("它出错不拖累脚本自己的更新（单独 try）", 'rec["metatube"] = "error:" + _short_err(e)' in su)
check("手动装的那条路也记版本号", "save_ms_state(metatube_files=files, metatube_ver=_MT_TAG[0])" in SRC)
check("media-stack 1.5.426 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 426))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
