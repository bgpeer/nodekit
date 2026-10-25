#!/usr/bin/env python3
"""菜单 12 档位 1/2/3 跑优化前要 y/n 确认 —— 仓库主人：「点进去时候输入 y/n 来确认执行和返回，
  而不是一输入序号就开始跑」。跑优化会重应用内核参数、可能短暂重启 nginx/sing-box（谷歌机那次
  还 OOM 卡死过），所以默认否、明确 y 才跑；n/回车就回菜单。只读的「5 状况」不拦，卸载「6」原本就有确认。
"""
import os, sys, io, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

# 公共打桩：菜单渲染、状态、版本都走假的；记录 _run_net_optimize 的调用
m._netopt_state = lambda: (None, None)
m._netopt_version = lambda: "4.5.14"
m._bbr_kernel_cur = lambda: ""
m._netopt_xanmod = lambda: None
for fn in ("ui_title", "ui_kv", "ui_line", "ui_items"):
    setattr(m, fn, lambda *a, **k: None)
runs = []
m._run_net_optimize = lambda args="", env_extra=None: runs.append((args, env_extra))

def drive(inputs):
    """喂一串输入跑一次 net_optimize_menu（末尾自动补 '0' 退出），返回这轮的 _run_net_optimize 调用。"""
    runs.clear()
    it = iter(list(inputs) + ["0"])
    m._ask = lambda q="": next(it)
    with contextlib.redirect_stdout(io.StringIO()):
        m.net_optimize_menu()
    return list(runs)

# 1. 选 1 再按 n / 回车：不跑
check("档位1 → n：不执行", drive(["1", "n"]) == [])
check("档位1 → 回车：不执行", drive(["1", ""]) == [])
# 2. 选 1 再按 y：跑一次（整套默认档）
r = drive(["1", "y"])
check("档位1 → y：执行一次（无 env）", r == [("", None)])
# 3. 选 3 再 n / y
check("档位3 → n：不执行", drive(["3", "n"]) == [])
check("档位3 → y：执行 fixed_cake", drive(["3", "y"]) == [("", {"ADAPTIVE_QOS_MODE": "fixed_cake"})])
# 4. 选 2：先问阈值，再确认
check("档位2 阈值20 → n：不执行", drive(["2", "20", "n"]) == [])
r = drive(["2", "20", "y"])
check("档位2 阈值20 → y：执行（带阈值 env）",
      len(r) == 1 and "ADAPTIVE_QOS_THRESHOLD" in (r[0][1] or {}))
r = drive(["2", "0", "y"])
check("档位2 输0 → y：执行 fixed_burst", r == [("", {"ADAPTIVE_QOS_MODE": "fixed_burst"})])
check("档位2 无效数字：当场拒、不问确认不执行", drive(["2", "abc"]) == [])
# 5. 只读的「5 状况」不拦，直接 --check
check("档位5 状况：不问确认，直接 --check", drive(["5"]) == [("--check", None)])
# 6. 卸载「6」原有确认仍在（n 不执行）
check("卸载6 → n：不执行 --reset", drive(["6", "n"]) == [])
check("卸载6 → y：执行 --reset", drive(["6", "y"]) == [("--reset", None)])

# 源码层：确认在 _run_net_optimize 之前
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("有 _netopt_confirm 助手", "def _netopt_confirm()" in src)

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
