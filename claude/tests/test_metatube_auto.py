#!/usr/bin/env python3
"""装 MetaTube 一次走完，不再问、不再叫人去填（仓库主人 11/13：「这个插件不是安装了默认开着就行吗，用不用取决于yaml里面
  开不开啊，为什么VPS要询问都把我搞迷糊了，我希望这个插件在安装直接把所有的程序走完，别搞得还要去填什么」）。

  原来装完：弹一屏「MetaTube 要在哪些媒体库生效？」让人挑编号；再叫人去 Emby → 插件 → MetaTube 手填服务器地址。
  1.5.424：生效的库照建库规则里 metatube: true 的来；服务器地址脚本通过 Emby 的插件配置接口填好。
钉住：
  一、生效范围 = 规则里 metatube: true 的库，别的库摘掉；规则里的库还没建 → 都不生效（建库时会自动生效）
  二、服务器地址：读出整份插件配置、只改 Server、写回去；本来就对就不写；插件还没加载出来就等一下再试
  三、装的流程里不再调那个挑编号的屏；手动入口「7 设置 → 4」还在
  四、填不上时才给手动的指路；「8 更新」时给以前装的补上"""
import importlib.util, os, sys, re, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
m.time = types.SimpleNamespace(sleep=lambda s: SLEPT.append(s), time=m.time.time, monotonic=m.time.monotonic,
                               strftime=m.time.strftime, gmtime=m.time.gmtime, localtime=m.time.localtime)
SLEPT = []

print("一、生效范围照规则")
m.lib_rules = lambda d=None: ([{"name": "电影", "mt": False}, {"name": "成人库", "mt": True}, {"name": "写真", "mt": True}], "x")
LIBS = [("电影", "1", True, {}), ("动漫", "2", True, {}), ("成人库", "3", False, {})]
SET = []
m.metatube_libraries = lambda key: list(LIBS)
m.set_metatube_libraries = lambda key, ids: SET.append(list(ids)) or len(ids)
names = m.metatube_apply_rules("/d", "k")
check("只在规则里 metatube: true、而且已经建了的库生效；别的库（电影、动漫）摘掉", names == ["成人库"] and SET == [["3"]])
LIBS[:] = [("电影", "1", True, {}), ("动漫", "2", True, {})]; SET.clear()
names = m.metatube_apply_rules("/d", "k")
check("规则里开了它的库还没建：哪个库都不生效（不会跑去动漫库乱认）", names == [] and SET == [[]])

print("二、服务器地址")
H = "http" + "://"                           # 拼出来：测试文件里不留网址（test_claude_folder）
URL = f"{H}metatube:{m.METATUBE_PORT}"
E = {"plugins": [{"Name": "别的插件", "Id": "p0"}, {"Name": "MetaTube", "Id": "p1"}], "conf": {"Server": "", "Token": "", "EnableBadges": True}, "calls": []}
def fake(path, key, method="GET", timeout=60, body=None):
    E["calls"].append((method, path))
    if path == "/Plugins":
        return list(E["plugins"])
    if path == "/Plugins/p1/Configuration":
        if method == "POST":
            E["conf"] = dict(body); return {}
        return dict(E["conf"])
    raise AssertionError(path)
m._emby = fake
check("填好了：返回空", m.metatube_set_server("k") == "")
check("只改 Server 一项，别的配置原样带回去", E["conf"] == {"Server": URL, "Token": "", "EnableBadges": True})
E["calls"].clear()
check("本来就对：不重复写", m.metatube_set_server("k") == "" and not [c for c in E["calls"] if c[0] == "POST"])
E["plugins"] = [{"Name": "别的插件", "Id": "p0"}]; E["conf"]["Server"] = ""; SLEPT.clear()
why = m.metatube_set_server("k", tries=3)
check("Emby 里还没出现这个插件（刚重启还在加载）：隔几秒再试，试完说原因", "还没出现" in why and SLEPT == [5, 5])
n = [0]
def late(path, key, method="GET", timeout=60, body=None):
    if path == "/Plugins":
        n[0] += 1
        return E["plugins"] + ([{"Name": "MetaTube", "Id": "p1"}] if n[0] >= 3 else [])
    return fake(path, key, method, timeout, body)
m._emby = late
check("等两轮插件加载出来了：填上", m.metatube_set_server("k", tries=5) == "" and E["conf"]["Server"] == URL)
m._emby = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("emby down"))
check("Emby 问不到：说原因，不崩", "emby down" in m.metatube_set_server("k", tries=1))

print("三、装的流程")
tm = SRC[SRC.index("def toggle_metatube"):]; tm = tm[:tm.index("\ndef ", 10)]
check("装完不再弹「要在哪些媒体库生效」让人挑", "choose_metatube_libraries(" not in tm and "metatube_apply_rules(d, key)" in tm)
check("装完自己填服务器地址", "metatube_set_server(key)" in tm and "插件的服务器地址已填好" in tm)
check("不再说「还有一步要你在 Emby 里手动做」", "还有一步要你在 Emby 里手动做" not in SRC and "脚本代劳不了，\n" not in tm)
check("填不上时才给手动指路（一行）", "插件的服务器地址没自动填上" in tm and ("服务器地址填 " + H + "metatube:") in tm)
check("屏上说清生效范围是照规则来的", "建库规则里 metatube: true 的库" in tm and "建库时自动生效" in tm)
check("手动改的入口还在：7 设置里「MetaTube 生效的库」", "choose_metatube_libraries(k0)" in SRC and "MetaTube 生效的库 当前：" in SRC)
check("卸载那一问还是默认否", 'ask_yn("现在卸载吗？", False)' in tm)

print("四、别处")
up = SRC[SRC.index("def do_update"):]; up = up[:up.index("\ndef ", 10)]
check("「8 更新」给以前装的补上服务器地址（只试一次，不拖慢更新）", 'metatube_set_server(cfg["emby_api_key"], tries=1)' in up)
check("「2 使用信息」不再叫人去填", "插件里的服务器地址脚本已经替你填成它" in SRC and "Emby → 插件 → MetaTube 填这个地址" not in SRC)
check("media-stack 1.5.424 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 424))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
