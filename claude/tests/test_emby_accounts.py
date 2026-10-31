#!/usr/bin/env python3
"""「7 设置 → 11 Emby 账号」（1.5.390）。仓库主人 10/31：「做吧，加进 7 设置里，这个用户名账号在后面还要显示出来」
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
check("第 11 项 Emby 账号，当前列账号名", "11. Emby 账号" in pm and "emby_users(cur)" in pm and 'c == "11"' in pm)

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
out = drive(lambda: m.emby_accounts_menu("K"), ["1", "1", "Evan2", "", "0", "0"], [])
check("改成了，别的字段原样带回", USERS["u1"]["Name"] == "Evan2" and USERS["u1"]["Configuration"] == {"X": 1})
check("屏上一行 ✔ + 提示用新用户名登录", "用户名改成「Evan2」" in out and "新用户名" in out)
check("跟别人重名 → 不改", m.emby_rename_user("K", "u1", "家人") and USERS["u1"]["Name"] == "Evan2")
check("空名 → 不改", m.emby_rename_user("K", "u1", "  ") and USERS["u1"]["Name"] == "Evan2")

print("四、改密码")
CALLS.clear(); LOG.clear()
out = drive(lambda: m.emby_accounts_menu("K"), ["2", "2", "r", "", "0", "0"], [True, False])
pw = [c for c in CALLS if c[1].endswith("/Password")]
check("先重置、再设新的", len(pw) == 2 and pw[0][2].get("ResetPassword") is True and pw[1][2].get("CurrentPw") == ""
      and len(pw[1][2].get("NewPw", "")) == 16)
newpw = pw[1][2]["NewPw"]
check("新密码顶格单独一行打一次", out.count(newpw) == 1 and (newpw + "\n") in out and ("\n" + newpw) in out)
check("不进日志", not any(newpw in l for l in LOG))
CALLS.clear(); FAIL_SET[0] = 1
out = drive(lambda: m.emby_accounts_menu("K"), ["2", "2", "abc123", "", "0", "0"], [True, True, False])
check("第二步失败 → 醒目报没有密码、重试成功", "这个账号现在没有密码" in out and "密码已改" in out
      and sum(1 for c in CALLS if c[1].endswith("/Password")) == 4)   # 重试 = 再重置一次 + 再设

print("五、改完密码顺带踢掉已登录设备（默认不勾）")
check("上一轮答否 → 一台都没删", not any(c[0] == "DELETE" for c in CALLS))
check("问的是默认否", re.search(r'ask_yn\(f"同时让「\{name\}」在所有设备上退出登录？", False\)', src) is not None)
CALLS.clear()
out = drive(lambda: m.emby_accounts_menu("K"), ["1", "2", "xyz", "", "0", "0"], [True, True])
dels = [c[1] for c in CALLS if c[0] == "DELETE"]
check("勾了 → 只删这个账号的两台，别人的不动", dels == ["/Devices?Id=d1", "/Devices?Id=d%202"] and "已让 2 台设备退出登录" in out)

print("六、默认不改")
CALLS.clear()
drive(lambda: m.emby_accounts_menu("K"), ["2", "2", "abc", "", "0", "0"], [False])
check("确认答否 → 一个请求都不发", not any(c[1].endswith("/Password") for c in CALLS))
ynd = re.search(r'ask_yn\(f"把「\{name\}」的密码改掉？", False\)', src)
check("确认默认否", ynd is not None)
check("版本 1.5.391 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 391))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
