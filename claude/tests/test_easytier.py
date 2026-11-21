#!/usr/bin/env python3
"""菜单 21 EasyTier 组网：装 / 配 / 状态 / 开机自启 / 卸载，全程不碰真服务、不联网。
  用到的 IP 全是首段一位数的占位（1.2.3.4 / 5.6.7.8），网址只有 github.com，
  免得 test_claude_folder.py 当成安装人信息拦下来。
"""
import os, re, io, sys, shutil, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("et", os.path.join(ROOT, "easytier.py"))
et = importlib.util.module_from_spec(spec); spec.loader.exec_module(et)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
et.BGP_DIR = tmp
et.ET_CONF = f"{tmp}/easytier.conf"
et.ET_TOML = f"{tmp}/easytier.toml"
et.ET_BIN = f"{tmp}/easytier-core"
et.ET_CLI = f"{tmp}/easytier-cli"
et.ET_UNIT = f"{tmp}/easytier.service"

CALLS = []
ACTIVE = {"active": True, "enabled": True}
CURL_FAIL = {"mode": "ok"}            # ok：curl 成功建文件；fail：curl 报错；empty：curl 没报错但只留空文件
# 真机上 easytier-cli -o json peer list 的形状（v2.6.4 实测）：本机那行 cost 是
# "Local"，远端那行 cost 是 "p2p"、lat_ms 是数字；这里就照这个样子造。
PEERS_JSON = {"text": '[{"cost": "Local", "hostname": "self", "ipv4": "5.6.7.8/24", "lat_ms": "-"}, {"cost": "p2p", "hostname": "Evan", "ipv4": "1.2.3.4/24", "lat_ms": "109.73"}]',
              "fail": False}
def fake_sh(cmd, check=False):
    CALLS.append(cmd)
    if cmd.startswith("systemctl is-active easytier"):
        return "active" if ACTIVE["active"] else "inactive"
    if cmd.startswith("systemctl is-enabled easytier"):
        return "enabled" if ACTIVE["enabled"] else "disabled"
    if " --version" in cmd:
        return "easytier-core 2.6.4-8428a89d"
    if " -o json peer list" in cmd:
        if PEERS_JSON["fail"]:
            raise RuntimeError("rpc fail")
        return PEERS_JSON["text"]
    if "easytier-cli peer" in cmd:
        return "PEER1 5.6.7.8"
    if cmd.startswith("curl "):
        if CURL_FAIL["mode"] == "fail":
            raise RuntimeError("curl: (7) Failed to connect")   # 真 sh(check=True) 就是这么抛的
        m = re.search(r"-o (\S+)", cmd)
        if m:
            with open(m.group(1), "wb") as f:
                if CURL_FAIL["mode"] == "ok":
                    f.write(b"FAKEZIP")   # 真下载下来的包不是空的；empty 模式故意留空文件
        return ""
    if cmd.startswith("rm -rf /tmp/easytier"):
        shutil.rmtree("/tmp/easytier", ignore_errors=True)
        os.makedirs("/tmp/easytier", exist_ok=True)
        return ""
    if cmd.startswith("unzip -o "):
        d = "/tmp/easytier/easytier-linux-x86_64"
        os.makedirs(d, exist_ok=True)
        open(d + "/easytier-core", "wb").close()
        open(d + "/easytier-cli", "wb").close()
        return ""
    if cmd.startswith("install -m755 "):
        dst = cmd.split()[-1]
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "wb").close()
        return ""
    if cmd.startswith("rm -f "):
        for p in cmd.split()[2:]:
            try:
                os.remove(p)
            except OSError:
                pass
        return ""
    return ""
et.sh = fake_sh
et._ensure_unzip = lambda: None

ANS = []
def ask(p=""):
    print(p, end="")
    if not ANS:
        raise AssertionError("还在问：答案没给够")
    return ANS.pop(0)
et._ask = ask
def run(f, *answers):
    ANS[:] = list(answers); CALLS.clear()
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = f()
    return r, b.getvalue()

print("一、纯函数：地址、校验、随机密钥、版本取数")
check("下载地址拼得对（x86_64 / aarch64）",
      et._et_url("2.6.4", "x86_64") == "https://github.com/EasyTier/EasyTier/releases/download/v2.6.4/easytier-linux-x86_64-v2.6.4.zip"
      and et._et_url("2.6.4", "aarch64").endswith("easytier-linux-aarch64-v2.6.4.zip"))
check("本机架构是 x86_64", et._arch_asset() == "x86_64")
_real_uname = os.uname
class _U:
    machine = "armv7l"
os.uname = lambda: _U()
try:
    et._arch_asset(); arch_ok = False
except SystemExit as e:
    arch_ok = "armv7l" in str(e)
finally:
    os.uname = _real_uname
check("不支持的架构直接报错（不硬装）", arch_ok)
check("网络名校验：字母数字 - _ 才行",
      et._valid_name("bgpeer-mesh") and et._valid_name("a1")
      and not et._valid_name("bad name!") and not et._valid_name("-x")
      and not et._valid_name(""))
check("密钥校验：至少 8 位、不能带空格",
      et._valid_secret("mysecret123") and not et._valid_secret("short")
      and not et._valid_secret("has space 123"))
check("IPv4 / 对端校验",
      et._valid_ipv4("1.2.3.4") and not et._valid_ipv4("1.2.3.999")
      and not et._valid_ipv4("not-ip")
      and et._valid_peer("1.2.3.4") and et._valid_peer("node.example")
      and not et._valid_peer("bad-peer"))
s1, s2 = et._random_secret(), et._random_secret()
check("随机密钥：32 位十六进制，两次不一样",
      len(s1) == 32 and re.match(r"^[0-9a-f]{32}$", s1) and s1 != s2)
et._fetch = lambda *a, **k: None
check("版本取不到就用兜底 2.6.4", et._latest_ver() == "2.6.4")
et._fetch = lambda *a, **k: b'{"tag_name": "v2.6.5"}'
check("版本取到了就用最新的", et._latest_ver() == "2.6.5")
def _boom(*a, **k):
    raise Exception("x")
et._fetch = _boom
check("版本接口报错也兜底", et._latest_ver() == "2.6.4")

print("二、配置存盘 + 服务文件")
cfg = {"network_name": "test-mesh", "network_secret": "mysecret123",
       "ipv4": "5.6.7.8", "role": "join", "peer": "1.2.3.4"}
et._conf_save(cfg)
check("配置存盘是 0600", (os.stat(et.ET_CONF).st_mode & 0o777) == 0o600)
check("配置读回来一字不差", et._conf_load() == cfg)
t = et._toml_text({"network_name": "test-mesh", "network_secret": "mysecret123",
                   "ipv4": et.ET_DEFAULT_IP1, "role": "first"})
check("首节点 TOML：网络名密钥在 [network_identity] 里，带监听、不带 peer",
      'network_name = "test-mesh"' in t and 'network_secret = "mysecret123"' in t
      and f'ipv4 = "{et.ET_DEFAULT_IP1}"' in t
      and 'listeners = ["tcp://0.0.0.0:11010", "udp://0.0.0.0:11010"]' in t
      and "[[peer]]" not in t)
t = et._toml_text(cfg)
check("加入节点 TOML：带 [[peer]] 指首节点，不带 listeners",
      '[[peer]]' in t and 'uri = "tcp://1.2.3.4:11010"' in t
      and "listeners" not in t)
check("TOML 转义：密钥里的引号和反斜杠不会把文件写坏",
      et._toml_text({"network_name": "a", "network_secret": 'x"y\\z1234',
                     "ipv4": "1.2.3.4", "role": "first"}).count('network_secret = "x\\"y\\\\z1234"') == 1)
u = et._unit_text()
check("服务文件：只有 -c 配置路径，不含密钥和网络名（ps 和文件里都看不到密钥）",
      "-c " + et.ET_TOML in u and "mysecret123" not in u and "test-mesh" not in u
      and "Restart=on-failure" in u and "Type=simple" in u)

print("二之二、下载失败兜底（_dl_zip）")
dest = f"{tmp}/dl-test.zip"
CURL_FAIL["mode"] = "fail"
et._fetch = lambda url, timeout=20: b"FAKEZIP"
try:
    os.remove(dest)
except OSError:
    pass
CALLS.clear()
et._dl_zip("https://github.com/EasyTier/EasyTier/releases/download/v2.6.4/easytier-linux-x86_64-v2.6.4.zip", dest)
check("curl 失败：走 urllib 兜底，文件照样落下来",
      open(dest, "rb").read() == b"FAKEZIP"
      and any("curl " in c for c in CALLS))
CURL_FAIL["mode"] = "empty"
try:
    os.remove(dest)
except OSError:
    pass
et._dl_zip("https://github.com/EasyTier/EasyTier/releases/download/v2.6.4/easytier-linux-x86_64-v2.6.4.zip", dest)
check("curl 没报错但只留空文件：同样走兜底，不当成下好了",
      open(dest, "rb").read() == b"FAKEZIP")
CURL_FAIL["mode"] = "fail"
et._fetch = lambda *a, **k: None
try:
    os.remove(dest)
except OSError:
    pass
try:
    et._dl_zip("https://github.com/EasyTier/EasyTier/releases/download/v2.6.4/easytier-linux-x86_64-v2.6.4.zip", dest)
    dl_ok = False
except RuntimeError as e:
    dl_ok = "下载失败" in str(e)
check("两个都失败：报错，不静默返回", dl_ok and not os.path.exists(dest))
CURL_FAIL["mode"] = "ok"
et._fetch = _boom

print("二之三、对端数")
check("对端数：本机那行（Local）不算，远端那行算",
      et._peer_count() == 1)
PEERS_JSON["text"] = '[{"cost": "Local", "hostname": "self", "ipv4": "5.6.7.8/24", "lat_ms": "-"}]'
check("对端数：只有本机那行就是 0 台", et._peer_count() == 0)
check("对端 0 台：加入节点写「未连上」，首节点写「还没人连进来」",
      "未连上" in et._peer_text({"role": "join"})
      and "还没人连进来" in et._peer_text({"role": "first"}))
PEERS_JSON["text"] = "not json"
check("对端数：CLI 回来的不是 JSON 就当没拿到，不报错", et._peer_count() is None)
PEERS_JSON["fail"] = True
check("对端数：CLI 挂了也当没拿到，不报错", et._peer_count() is None)
PEERS_JSON["fail"] = False
PEERS_JSON["text"] = '[{"cost": "Local", "hostname": "self", "ipv4": "5.6.7.8/24", "lat_ms": "-"}, {"cost": "p2p", "hostname": "Evan", "ipv4": "1.2.3.4/24", "lat_ms": "109.73"}]'

print("三、安装")
et._latest_ver = lambda: "2.6.4"
for p in (et.ET_BIN, et.ET_CLI):
    try:
        os.remove(p)
    except OSError:
        pass
r, out = run(et.install)
check("装完两个二进制都在",
      os.path.exists(et.ET_BIN) and os.path.exists(et.ET_CLI))
check("装的时候按顺序：下载 → 解压 → 装 → 校版本",
      any("curl " in c and "easytier-linux-x86_64-v2.6.4.zip" in c for c in CALLS)
      and any(c.startswith("unzip -o ") for c in CALLS)
      and any(c.startswith("install -m755 ") and c.endswith(et.ET_BIN) for c in CALLS)
      and "装好了" in out)
CALLS.clear()
r, out = run(et.install)
check("已是最新版：不再下载", not any("curl " in c for c in CALLS) and "已是最新版" in out)

print("四、配置组网")
try:
    os.remove(et.ET_CONF)
except OSError:
    pass
try:
    os.remove(et.ET_TOML)
except OSError:
    pass
try:
    os.remove(et.ET_UNIT)
except OSError:
    pass
r, out = run(et.configure, "1", "", "", "", "")
cfg = et._conf_load()
check("首节点：一路回车就用默认（网络名 bgpeer-mesh、IP 是首节点默认、密钥随机生成）",
      cfg.get("role") == "first" and cfg.get("network_name") == "bgpeer-mesh"
      and cfg.get("ipv4") == et.ET_DEFAULT_IP1
      and re.match(r"^[0-9a-f]{32}$", cfg.get("network_secret", "")))
toml = open(et.ET_TOML).read()
unit = open(et.ET_UNIT).read()
check("首节点：TOML 写好了（监听 + 网络名密钥）、是 0600；服务文件只有 -c，不含密钥",
      os.path.exists(et.ET_TOML) and (os.stat(et.ET_TOML).st_mode & 0o777) == 0o600
      and "listeners" in toml and "[[peer]]" not in toml
      and "-c " + et.ET_TOML in unit and cfg["network_secret"] not in unit
      and any(c == "systemctl daemon-reload" for c in CALLS)
      and any(c == "systemctl enable easytier" for c in CALLS)
      and any(c == "systemctl restart easytier" for c in CALLS))
r, out = run(et.configure, "2", "bad name!", "test-mesh", "short", "mysecret123",
             "5.6.7.8", "bad-peer", "1.2.3.4", "")
cfg = et._conf_load()
check("加入节点：写错的重填（网络名、密钥、对端都各错一次才过）",
      cfg.get("role") == "join" and cfg.get("network_name") == "test-mesh"
      and cfg.get("network_secret") == "mysecret123"
      and cfg.get("ipv4") == "5.6.7.8" and cfg.get("peer") == "1.2.3.4")
check("加入节点：TOML 里 [[peer]] 指到首节点",
      '[[peer]]' in open(et.ET_TOML).read()
      and 'uri = "tcp://1.2.3.4:11010"' in open(et.ET_TOML).read())
saved = et._conf_load()
r, out = run(et.configure, "1", "", "", "", "")
cfg = et._conf_load()
check("重配：回车保持原样（密钥没变、网络名没变）",
      cfg.get("network_secret") == saved["network_secret"]
      and cfg.get("network_name") == "test-mesh")
r, out = run(et.configure, "1", "", "", "", "n")
check("最后确认打 n：什么都没动", et._conf_load() == cfg and "已取消" in out)

print("五、状态 / 开机自启 / 卸载")
ACTIVE["active"] = True
r, out = run(et.status)
check("状态：印出网络名、本机 IP、密钥和对端表",
      "test-mesh" in out and "5.6.7.8" in out and "mysecret123" in out
      and "PEER1" in out and "运行中" in out and "对端 1 台" in out)
PEERS_JSON["text"] = '[{"cost": "Local", "hostname": "self", "ipv4": "5.6.7.8/24", "lat_ms": "-"}]'
et._conf_save({"network_name": "test-mesh", "network_secret": "mysecret123",
               "ipv4": "5.6.7.8", "role": "join", "peer": "1.2.3.4"})
r, out = run(et.status)
check("状态：对端 0 台就红字写未连上", "对端 0 台" in out and "未连上" in out)
PEERS_JSON["text"] = '[{"cost": "Local", "hostname": "self", "ipv4": "5.6.7.8/24", "lat_ms": "-"}, {"cost": "p2p", "hostname": "Evan", "ipv4": "1.2.3.4/24", "lat_ms": "109.73"}]'
ACTIVE["active"] = False
r, out = run(et.status)
check("服务没跑：写未运行，不印对端表", "未运行" in out and "PEER1" not in out)
ACTIVE["active"] = True
ACTIVE["enabled"] = True
r, out = run(et._autostart_toggle)
check("开机自启开着：点一下就关", "systemctl disable easytier" in CALLS and "已关" in out)
ACTIVE["enabled"] = False
r, out = run(et._autostart_toggle)
check("开机自启关着：点一下就开", "systemctl enable easytier" in CALLS and "已开" in out)
r, out = run(et.uninstall, "n")
check("卸载打 n：什么都没动",
      os.path.exists(et.ET_BIN) and os.path.exists(et.ET_CONF)
      and os.path.exists(et.ET_TOML)
      and not any("disable --now" in c for c in CALLS))
r, out = run(et.uninstall, "y")
check("卸载打 y：服务停、二进制、配置、TOML、服务文件一起走",
      not os.path.exists(et.ET_BIN) and not os.path.exists(et.ET_CLI)
      and not os.path.exists(et.ET_CONF) and not os.path.exists(et.ET_UNIT)
      and not os.path.exists(et.ET_TOML)
      and any("systemctl disable --now easytier" in c for c in CALLS))
for p in (et.ET_BIN, et.ET_CLI, et.ET_CONF, et.ET_UNIT, et.ET_TOML):
    open(p, "w").close()
r, out = run(lambda: et.uninstall(assume_yes=True))
check("卸载 --yes：不问就卸", not os.path.exists(et.ET_CONF) and "已卸载" in out)
r, out = run(et.uninstall)
check("没装还卸：说不用卸", "不用卸" in out)

print("六、菜单循环 + 接线")
et._conf_save({"network_name": "test-mesh", "network_secret": "mysecret123",
               "ipv4": "5.6.7.8", "role": "join", "peer": "1.2.3.4"})
open(et.ET_BIN, "w").close()
open(et.ET_CLI, "w").close()
r, out = run(et.menu, "3", "0")
check("菜单：3 看状态，0 返回，不报错", "状态" in out and r is None)
r, out = run(et.menu, "0")
check("菜单顶上：运行中就带对端数", "运行中" in out and "对端 1 台" in out)
PEERS_JSON["text"] = '[{"cost": "Local", "hostname": "self", "ipv4": "5.6.7.8/24", "lat_ms": "-"}]'
r, out = run(et.menu, "0")
check("菜单顶上：对端 0 台就红字写未连上", "对端 0 台" in out and "未连上" in out)
PEERS_JSON["text"] = '[{"cost": "Local", "hostname": "self", "ipv4": "5.6.7.8/24", "lat_ms": "-"}, {"cost": "p2p", "hostname": "Evan", "ipv4": "1.2.3.4/24", "lat_ms": "109.73"}]'
r, out = run(et.menu, "9", "0")
check("菜单：打了没有的编号，接着问，0 才回", r is None)
xy = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("主菜单接上了 21 号入口",
      "21. EasyTier 组网" in xy and 'c == "21"' in xy
      and "EASYTIER_URL" in xy and "def easytier_menu" in xy)
readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
doc = os.path.join(ROOT, "docs", "21-easytier.md")
check("README 和文档都挂上了",
      "docs/21-easytier.md" in readme and os.path.exists(doc)
      and "11010" in open(doc, encoding="utf-8").read())

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
