#!/usr/bin/env python3
"""「7 设置 → 12 Emby 账号」（1.5.390；1.5.402 起三个账号挨着，从第 11 挪到第 12）。仓库主人 10/31：「做吧，加进 7 设置里，这个用户名账号在后面还要显示出来」
    Emby 账号 / 1. 账号1（管理员）　当前：名字 / 2. 账号2　当前：名字 / 0. 返回
钉住：
  一、7 设置里有第 11 项，当前：列出账号名
  二、账号表：每行「账号N（管理员）　当前：名字」，「当前：」对齐
  三、改名：读回整份资料只改 Name 再 POST；重名 / 空名不改
  四、改密码：先管理员重置再设新的；第二步失败醒目报「没有密码」并能重试；新密码只打一次、不进日志
  五、改完密码可以顺带让这个账号所有设备退出登录（1.5.391，默认不勾，只删这个账号的设备）
  六、改密码默认不改（ask_yn 默认否）
"""
import importlib.util, os, sys, io, contextlib, re
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

USERS = {"u1": {"Id": "u1", "Name": "evan", "Policy": {"IsAdministrator": True}, "HasPassword": True, "Configuration": {"X": 1}},
         "u2": {"Id": "u2", "Name": "家人", "Policy": {"IsAdministrator": False}, "HasPassword": False}}
CALLS = []
FAIL_SET = [0]
def fake(path, key, method="GET", timeout=60, body=None):
    CALLS.append((method, path, body))
    if path == "/Users":
        return [dict(u) for u in USERS.values()]
    mm = re.match(r"/Users/(\w+)$", path)
    if mm and method == "GET":
        return dict(USERS[mm.group(1)])
    if mm and method == "POST":
        USERS[mm.group(1)] = dict(body); return {}
    if path == "/Sessions":
        return [{"Id": "s1", "UserId": "u1", "NowPlayingItem": {"Id": "9"}}, {"Id": "s2", "UserId": "u1"},
                {"Id": "s3", "UserId": "u2", "NowPlayingItem": {"Id": "8"}}]
    if re.match(r"/Sessions/\w+/Playing/Stop$", path) and method == "POST":
        return {}
    if path == "/Devices":
        return {"Items": [{"Id": "d1", "LastUserId": "u1"}, {"Id": "d 2", "LastUserId": "u1"}, {"Id": "d3", "LastUserId": "u2"}]}
    if path.startswith("/Devices?Id=") and method == "DELETE":
        return {}
    if path.endswith("/Password"):
        if "NewPw" in body and FAIL_SET[0]:
            FAIL_SET[0] -= 1; raise RuntimeError("boom")
        return {}
    raise AssertionError(path)
m._emby = fake
import tempfile
D = tempfile.mkdtemp()
m.ms_install_dir = lambda: D
VERIFY = {"up": None, "good": True, "logins": []}      # 存的那份密码拿去登录验：Emby 问不到 → 不验
m.emby_wizard_done = lambda: VERIFY["up"]
m._emby_login = lambda name, pw: (VERIFY["logins"].append((name, pw)), ("tok", "u1") if VERIFY["good"] else ("", ""))[1]
m._emby_logout = lambda tok, drop_device=False: None
LOG = []
m.heal_log = lambda lines: LOG.extend(lines)

def drive(fn, answers, yn):
    a, y = iter(answers), iter(yn)
    m.ask = lambda *x, **k: next(a)
    m.ask_yn = lambda p, d=True: next(y)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())

print("一、7 设置")
src = open(SRC, encoding="utf-8").read()
pm = src[src.index("def params_menu("):]; pm = pm[:pm.index("\ndef ", 1)]
check("第 12 项 Emby 账号，当前列账号名", "12. Emby 账号" in pm and "emby_users(cur)" in pm
      and re.search(r'c == "12":\n\s+emby_accounts_menu\(cur\)', pm) is not None)

print("二、账号表")
out = drive(lambda: m.emby_accounts_menu("K"), ["0"], [])
rows = [l for l in out.splitlines() if "当前：" in l]
check("两行，账号名在「当前：」后面", len(rows) == 2 and rows[0].endswith("当前：evan") and rows[1].endswith("当前：家人")
      and "账号1（管理员）" in rows[0] and "（管理员）" not in rows[1])
w = lambda t: sum(2 if ord(c) > 0x2E80 else 1 for c in t)
check("「当前：」对齐", w(rows[0][:rows[0].index("当前：")]) == w(rows[1][:rows[1].index("当前：")]))
check("没 API Key → 不进", "先填「1」" in drive(lambda: m.emby_accounts_menu(""), [""], []))

print("三、改名")
CALLS.clear()
out = drive(lambda: m.emby_accounts_menu("K"), ["1", "1", "Evan2", "0", "0"], [False])
check("先说清会踢人、确认答否 → 不改、不踢", USERS["u1"]["Name"] == "evan" and "正在看的会断" in out
      and not any(c[0] in ("DELETE", "POST") for c in CALLS))
check("改名的确认默认否", re.search(r'ask_yn\(f"把「\{name\}」改名成「\{new\}」？", False\)', src) is not None)
CALLS.clear()
out = drive(lambda: m.emby_accounts_menu("K"), ["1", "1", "Evan2", "", "0", "0"], [True])
check("改成了，别的字段原样带回", USERS["u1"]["Name"] == "Evan2" and USERS["u1"]["Configuration"] == {"X": 1})
check("屏上一行 ✔ + 提示用新用户名登录", "用户名改成「Evan2」" in out and "新用户名" in out)
check("改名后这个账号的设备全踢掉（仓库主人 11/12：改完立刻要用新的重新登录）：只删它的两台",
      [c[1] for c in CALLS if c[0] == "DELETE"] == ["/Devices?Id=d1", "/Devices?Id=d%202"] and "2 台设备已退出登录" in out)
check("它正在播的那一个先叫停；没在播的、别人的不碰", [c[1] for c in CALLS if c[1].endswith("/Playing/Stop")] == ["/Sessions/s1/Playing/Stop"])
check("跟别人重名 → 不改", m.emby_rename_user("K", "u1", "家人") and USERS["u1"]["Name"] == "Evan2")
check("空名 → 不改", m.emby_rename_user("K", "u1", "  ") and USERS["u1"]["Name"] == "Evan2")

print("四、改密码")
CALLS.clear(); LOG.clear()
out = drive(lambda: m.emby_accounts_menu("K"), ["2", "2", "r", "", "0", "0"], [True])
pw = [c for c in CALLS if c[1].endswith("/Password")]
check("先重置、再设新的", len(pw) == 2 and pw[0][2].get("ResetPassword") is True and pw[1][2].get("CurrentPw") == ""
      and len(pw[1][2].get("NewPw", "")) == 16)
newpw = pw[1][2]["NewPw"]
check("新密码顶格单独一行", (newpw + "\n") in out and ("\n" + newpw + "\n") in out)
check("不进日志", not any(newpw in l for l in LOG))
CALLS.clear(); FAIL_SET[0] = 1
out = drive(lambda: m.emby_accounts_menu("K"), ["2", "2", "abc123", "", "0", "0"], [True, True])
check("第二步失败 → 醒目报没有密码、重试成功", "这个账号现在没有密码" in out and "密码已改" in out
      and sum(1 for c in CALLS if c[1].endswith("/Password")) == 4)   # 重试 = 再重置一次 + 再设

print("五、改完密码这个账号的设备全踢掉（1.5.404 起不再问，仓库主人 11/12）")
check("不再有「同时让…退出登录？」那一问，也不再说「不会自己掉线」", "在所有设备上退出登录？" not in src and "不会自己掉线" not in src)
CALLS.clear()
out = drive(lambda: m.emby_accounts_menu("K"), ["1", "2", "xyz", "", "0", "0"], [True])
dels = [c[1] for c in CALLS if c[0] == "DELETE"]
check("只删这个账号的两台，别人的不动；屏上说踢了几台、要用新密码重新登录", dels == ["/Devices?Id=d1", "/Devices?Id=d%202"]
      and "2 台设备已退出登录" in out and "新密码重新登录" in out)
check("改之前一行说清：所有设备立刻退出、正在看的会断", "所有设备立刻退出登录，正在看的会断" in out)
check("正在播的先叫停", [c[1] for c in CALLS if c[1].endswith("/Playing/Stop")] == ["/Sessions/s1/Playing/Stop"])
_old = m._emby
m._emby = lambda path, key, method="GET", timeout=60, body=None: (_ for _ in ()).throw(RuntimeError("x")) if path in ("/Devices", "/Sessions") else _old(path, key, method, timeout, body)
out = drive(lambda: m.emby_accounts_menu("K"), ["1", "2", "xyz2", "", "0", "0"], [True])
check("设备列表读不到：密码照改，屏上明说旧设备没踢掉、去哪手动删", "密码已改" in out and "没踢掉" in out and "手动删" in out)
m._emby = _old

print("五之二、脚本经手的密码存在 VPS 上（仓库主人 11/12：「这个密码在VPS设置的不能映射保存下来吗」）")
import stat
SEC = D + "/.secrets"
check("刚才改的密码记在 .secrets（0600），按账号 id 记", m.emby_pw_get(D, "u1") == "xyz2" and stat.S_IMODE(os.stat(SEC).st_mode) == 0o600
      and "EMBY_PW_u1=xyz2" in open(SEC).read())
out = drive(lambda: m.emby_accounts_menu("K"), ["1", "0", "0"], [])
check("账号那一屏「2 密码　当前：」直接显示它", "  2. 密码              当前：xyz2" in out)
out = drive(lambda: m.emby_accounts_menu("K"), ["2", "0", "0"], [])
check("没经过脚本的账号（网页上设的 / 没设）：不乱显示", "xyz2" not in out and ("没设" in out or "这里看不到" in out))
m.emby_pw_save(D, "u2", "")                       # 前面第四节用脚本给账号 2 改过密码，这里要看的是「没经过脚本」的样子
USERS["u2"]["HasPassword"] = True
out = drive(lambda: m.emby_accounts_menu("K"), ["2", "0", "0"], [])
check("网页上设了密码的：写「已设（在网页上设的，这里看不到）」", "已设（在网页上设的，这里看不到）" in out)
USERS["u2"]["HasPassword"] = False
VERIFY.update(up=True, good=True); VERIFY["logins"].clear()
out = drive(lambda: m.emby_accounts_menu("K"), ["1", "0", "0"], [])
check("Emby 问得到：进这一屏拿存的那份登录验一次，对 → 照常显示", VERIFY["logins"] == [("Evan2", "xyz2")] and "当前：xyz2" in out)
VERIFY["good"] = False
out = drive(lambda: m.emby_accounts_menu("K"), ["1", "0", "0"], [])
check("验不过（在 Emby 网页上改过）：作废、不再显示，不留一份错的", "xyz2" not in out and m.emby_pw_get(D, "u1") == "" and "这里看不到" in out)
VERIFY.update(up=None, good=True)
m.emby_pw_save(D, "u1", "keep1"); USERS["u1"]["Name"] = "Renamed"
check("按 id 记：改了名还在", m.emby_pw_get(D, "u1") == "keep1")
m.emby_pw_remember(D, "K", "Renamed", "byname")
check("只知道用户名时按名字找到账号再记", m.emby_pw_get(D, "u1") == "byname")
m.emby_pw_remember(D, "K", "没有这个人", "x")
check("找不到这个名字：不乱记", "=x" not in open(SEC).read())
USERS["u1"]["Name"] = "Evan2"

print("六、默认不改")
CALLS.clear()
drive(lambda: m.emby_accounts_menu("K"), ["2", "2", "abc", "", "0", "0"], [False])
check("确认答否 → 一个请求都不发", not any(c[1].endswith("/Password") for c in CALLS))
ynd = re.search(r'ask_yn\(f"把「\{name\}」的密码改掉？", False\)', src)
check("确认默认否", ynd is not None)
check("版本 1.5.406 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 406))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
