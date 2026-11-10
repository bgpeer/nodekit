#!/usr/bin/env python3
"""media-stack.json 多个进程同时写不再互相覆盖（仓库主人 11/10：「你如果觉得这个有问题该怎么修改就怎么修改吧」）。

  开播门、常驻补时长、每分钟那一轮、保活、菜单是好几个进程，都往同一个 media-stack.json 里写，而且都是
  「整份读出来 → 改自己那几项 → 整份写回去」，没加锁。两个进程撞上时，后写的那个手里是旧的一份，会把先写的
  那一项盖回旧值。最坏是菜单里刚改的开关被悄悄改回去。
  修法：save_ms_state 的读和写包在一把 flock 里。它在开播门的路上，所以等锁有上限（MS_STATE_LOCK_WAIT 秒），
  等不到就照老办法直接写——绝不把谁卡住。
  第一节是真起几个进程同时写：没有锁的老代码在这里会丢项（容器里实测 6 个进程各写 150 项，900 项丢了几百）。
"""
import os, io, sys, json, time, fcntl, tempfile, threading, subprocess, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
STATE, LOCKD = f"{tmp}/bgpeer/media-stack.json", f"{tmp}/lock"
def point(mod):
    mod.BGP_DIR, mod.MS_STATE, mod.CRON_LOCK_DIR = os.path.dirname(STATE), STATE, LOCKD
point(m)

print("一、几个进程同时写：一项都不丢")
CHILD = f"""
import importlib.util, os, sys, time
spec = importlib.util.spec_from_file_location("ms", {os.path.join(ROOT, "media-stack.py")!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.BGP_DIR, m.MS_STATE, m.CRON_LOCK_DIR = {os.path.dirname(STATE)!r}, {STATE!r}, {LOCKD!r}
who, n = sys.argv[1], int(sys.argv[2])
open({tmp!r} + "/ready-" + who, "w").close()
while not os.path.exists({tmp!r} + "/go"):
    time.sleep(0.005)
for j in range(n):
    m.save_ms_state(**{{"p%s_%d" % (who, j): j}})
"""
open(f"{tmp}/child.py", "w").write(CHILD)
PROCS, EACH = 6, 150
m.save_ms_state(tmp, keep_me="原来就有的项")
ps = [subprocess.Popen([sys.executable, f"{tmp}/child.py", str(i), str(EACH)]) for i in range(PROCS)]
t0 = time.time()
while len([n for n in os.listdir(tmp) if n.startswith("ready-")]) < PROCS and time.time() - t0 < 120:
    time.sleep(0.05)
open(f"{tmp}/go", "w").close()                    # 都加载好了再一起开写，保证真撞得上
rc = [p.wait(timeout=240) for p in ps]
st = json.load(open(STATE))
got = sum(1 for k in st if k.startswith("p"))
check(f"{PROCS} 个进程各写 {EACH} 项，全都在（{got}/{PROCS * EACH}）", rc == [0] * PROCS and got == PROCS * EACH)
check("原来就有的项、安装目录没被谁盖掉", st.get("keep_me") == "原来就有的项" and st.get("install_dir") == tmp)
check("文件始终是一份完整的 JSON、没有留下临时文件", isinstance(st, dict)
      and not [n for n in os.listdir(os.path.dirname(STATE)) if ".tmp" in n])

print("二、同一个进程里几个线程（开播门是多线程的）")
def worker(i):
    for j in range(60):
        m.save_ms_state(**{f"t{i}_{j}": j})
ts = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
[t.start() for t in ts]; [t.join() for t in ts]
check("5 个线程各写 60 项，全都在", sum(1 for k in json.load(open(STATE)) if k.startswith("t")) == 300)

m.MS_STATE_LOCK_WAIT, _w = 0.0, m.MS_STATE_LOCK_WAIT           # 不等锁 = 退回老办法：这时候靠的是临时文件各用各的
ts = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
[t.start() for t in ts]; [t.join() for t in ts]
m.MS_STATE_LOCK_WAIT = _w
try:
    whole = isinstance(json.load(open(STATE)), dict) and json.load(open(STATE)).get("install_dir") == tmp
except ValueError:
    whole = False
check("就算没拿到锁，几个线程同时写也不会把半截文件顶上去（临时文件名带线程号）：文件完整、安装目录还在", whole)
p0 = f"{tmp}/atomic.txt"
def aw(i):
    for _ in range(200):
        m.write_atomic(p0, str(i) * 4000)
ts = [threading.Thread(target=aw, args=(i,)) for i in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
got = open(p0).read()
check("write_atomic：4 个线程同时写同一个文件，留下的是其中一份完整的、不留临时文件",
      len(got) == 4000 and len(set(got)) == 1 and not [n for n in os.listdir(tmp) if n.startswith("atomic.txt.tmp")])

print("三、绝不卡住人")
holder = open(os.path.join(LOCKD, "media-stack-state.lock"), "w")
fcntl.flock(holder, fcntl.LOCK_EX)                # 模拟：拿着锁的进程卡在磁盘上不放
t0 = time.time()
m.save_ms_state(stuck_test=1)
dt = time.time() - t0
check(f"锁被别人一直占着：最多等 {m.MS_STATE_LOCK_WAIT:g} 秒就照老办法直接写（实际 {dt:.1f} 秒），不会一直等",
      m.MS_STATE_LOCK_WAIT <= 2.0 and m.MS_STATE_LOCK_WAIT - 0.2 <= dt < m.MS_STATE_LOCK_WAIT + 1.5
      and json.load(open(STATE)).get("stuck_test") == 1)
m.MS_STATE_LOCK_WAIT = 0.2
t0 = time.time(); m.save_ms_state(stuck_test=2)
check("等锁的上限是可调的常量，照它来", time.time() - t0 < 1 and json.load(open(STATE)).get("stuck_test") == 2)
fcntl.flock(holder, fcntl.LOCK_UN); holder.close()
t0 = time.time()
for i in range(50):
    m.save_ms_state(fast=i)
check(f"没人抢的时候不多花时间（50 次写 {time.time() - t0:.2f} 秒）", time.time() - t0 < 5)
m.CRON_LOCK_DIR = f"{tmp}/child.py/nope"          # 锁目录建不了（上一级是个文件）
m.save_ms_state(no_lock_dir=1)
check("锁目录建不了：退到 /tmp；照样写得进去", json.load(open(STATE)).get("no_lock_dir") == 1)
real_open = open
import builtins
def no_lock_open(p, *a, **k):
    if str(p).endswith("media-stack-state.lock"):
        raise OSError("read-only fs")
    return real_open(p, *a, **k)
m.open = no_lock_open                              # 模块里的 open 换掉：哪儿都建不了锁文件
try:
    m.save_ms_state(no_lock_at_all=1)
finally:
    del m.open
check("锁文件哪儿都建不了：不加锁照样写（跟以前一样），不报错", json.load(open(STATE)).get("no_lock_at_all") == 1)
point(m)
import ast
src = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "_ms_state_lock")
body = ast.get_source_segment(src, fn)
check("等锁用的是不阻塞的抢法（LOCK_NB）+ 上限，没有无限等的 flock", "LOCK_NB" in body and "MS_STATE_LOCK_WAIT" in body
      and "flock(fh, fcntl.LOCK_EX)" not in body)
fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "save_ms_state")
body = ast.get_source_segment(src, fn)
check("读（ms_state）在拿锁之后、写在放锁之前", body.index("_ms_state_lock()") < body.index("cur = ms_state()") < body.index("write_atomic(")
      < body.index("lk.close()"))
check("读状态不用等锁（改名是原子的，读到的永远是完整的一份）", "_ms_state_lock" not in ast.get_source_segment(
      src, next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "ms_state")))
check("锁文件不放在 /etc/bgpeer 里（放任务锁那个目录）", os.path.exists(os.path.join(LOCKD, "media-stack-state.lock"))
      and not [n for n in os.listdir(os.path.dirname(STATE)) if "lock" in n])

print("四、版本号")
check("media-stack 1.5.398 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 398))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
