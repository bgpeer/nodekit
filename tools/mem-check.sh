#!/usr/bin/env bash
# 内存到底够不够、被谁占着。只读，不改任何东西，不出网。
#
#   bash mem-check.sh
#
# 【为什么不能看着「剩多少」就下结论】Linux 把暂时用不到的内存拿去做文件缓存，
# 那部分【算在"用掉"里，但随时能还回来】。所以面板上一个很小的 "Free" 完全可能
# 是健康的 —— 真正该看的是 MemAvailable（内核自己估的"要用的话能拿回多少"）。
# 这两个数在一台跑着容器的机器上经常差好几个 G，只看前一个必然吓自己一跳。
#
# 这个脚本按【会不会真的出事】的顺序问四句：
#
#   ① 还能拿回多少   MemAvailable，不是 Free。低于一成才值得紧张
#   ② 有没有在换页   swap 用了多少、【此刻还在不在换】。换页才是"真的不够"的
#                    硬证据 —— 用掉一点 swap 但完全不换页，是好事不是坏事
#   ③ 有没有被杀过   OOM killer 动过手没有。这是确定的答案，不用猜
#   ④ 谁在占         容器按 RSS 排、宿主机进程按 RSS 排，再看 tmpfs
#
# 【不打印任何命令行参数】进程的 argv 里可能带着节点的 UUID / 密码 / reality 私钥
# 和订阅 token（仓库规矩第一条点名的东西）。这里只打进程名和数字 —— 排查够用，
# 而这一屏是要截图发人的。
set -u

TOOL_VER="2026-09-19a"
echo "  ${0##*/}  版本 $TOOL_VER"

python3 - <<'PY'
import os, re, subprocess

G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"
D = "\033[2m"; B = "\033[1m"; C = "\033[36m"; X = "\033[0m"


def sec(t):
    print()
    print(f"  {B}{t}{X}")
    print("  " + "-" * 60)


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                m = re.match(r"\s*(\d+)", v)
                if m:
                    out[k] = int(m.group(1)) * 1024      # 字节
    except OSError:
        pass
    return out


def gb(n):
    return f"{n / 1024 ** 3:.2f} GB" if n >= 1 << 30 else f"{n / 1024 ** 2:.0f} MB"


mi = meminfo()
total = mi.get("MemTotal", 0)
if not total:
    print(f"  {R}✖ 读不到 /proc/meminfo —— 这台机器上没法量{X}")
    raise SystemExit(1)
free = mi.get("MemFree", 0)
avail = mi.get("MemAvailable", free)
cached = mi.get("Cached", 0) + mi.get("Buffers", 0) + mi.get("SReclaimable", 0)
swtot = mi.get("SwapTotal", 0)
swfree = mi.get("SwapFree", 0)
swused = swtot - swfree

# ================= ① 还能拿回多少 =================
sec("① 还剩多少 —— 看 available，不是 free")
print(f"  总共        {C}{gb(total)}{X}")
print(f"  真正空着    {gb(free)}   {D}← 面板上那个「Free」多半就是它{X}")
print(f"  文件缓存    {gb(cached)}   {D}算在「用掉」里，但要用时立刻还回来{X}")
pct = avail / total * 100 if total else 0
col = G if pct >= 20 else (Y if pct >= 10 else R)
print(f"  {B}能拿回来    {col}{gb(avail)}（{pct:.0f}%）{X}  "
      f"{D}← 只有这个数低才值得紧张{X}")
print()
if free < avail * 0.5:
    print(f"  {D}注意这两个数差了 {gb(avail - free)} —— 那就是缓存。"
          f"面板上「剩 {gb(free)}」看着吓人，实际随时能拿回 {gb(avail)}。{X}")

# ================= ② 有没有在换页 =================
sec("② 有没有在换页 —— 这才是「真的不够」的硬证据")
if swtot == 0:
    print(f"  {D}这台机器没有 swap。{X}")
    print(f"  {D}没有 swap = 内存一旦真不够，内核直接杀进程（见 ③），"
          f"不会先变慢。所以 ③ 那一节在这种机器上格外重要。{X}")
else:
    print(f"  swap 用了 {gb(swused)} / {gb(swtot)}")
    # 【用了多少不算数，此刻还换不换才算数】开机时被换出去的东西可能几个月都没
    # 再用过，它占着 swap 一点问题都没有。真正的病是【现在还在来回换】。
    a = run(["vmstat", "1", "2"])
    lines = [l for l in a.splitlines() if re.match(r"^\s*\d", l)]
    if len(lines) >= 2:
        f = lines[-1].split()
        try:
            si, so = int(f[6]), int(f[7])
        except (IndexError, ValueError):
            si = so = -1
        if si < 0:
            print(f"  {D}（vmstat 的列对不上，换页速率没量到）{X}")
        elif si + so == 0:
            print(f"  {G}✔ 此刻没有在换页{X}  "
                  f"{D}用掉的那点 swap 是历史遗留，放着不管就行{X}")
        else:
            print(f"  {R}✖ 此刻正在换页{X}  {D}换入 {si} KB/s、换出 {so} KB/s —— "
                  f"内存是真的不够，机器会一卡一卡的{X}")
    else:
        print(f"  {D}（没有 vmstat，换页速率没量到）{X}")

# ================= ③ 有没有被杀过 =================
sec("③ 内核有没有因为内存不够杀过进程")
# 【这是确定的答案，不用猜】被 OOM 杀过 = 内存确实不够过，而且那一刻某个服务
# 是直接消失的 —— 容器会自己重启，屏幕上什么都看不出来，只有这里有记录。
oom = ""
for cmd in (["journalctl", "-k", "--no-pager", "-n", "2000"],
            ["dmesg", "-T"], ["dmesg"]):
    oom = run(cmd, timeout=30)
    if oom:
        break
hits = [l for l in oom.splitlines()
        if "Out of memory" in l or "oom-kill" in l.lower()
        or "Killed process" in l]
if not oom:
    print(f"  {D}读不到内核日志（没有 dmesg / journalctl 权限？）—— 这一项跳过{X}")
elif not hits:
    print(f"  {G}✔ 没有 OOM 记录{X}  {D}（这份日志覆盖的范围内）{X}")
else:
    print(f"  {R}✖ 被 OOM 杀过 {len(hits)} 次{X}")
    for l in hits[-5:]:
        # 只留"杀了谁"，时间戳和其余内容不打 —— 那些行里有时带路径
        m = re.search(r"Killed process \d+ \(([^)]+)\)", l) or \
            re.search(r"oom-kill:.*?task=([^,]+)", l)
        print(f"    {Y}{m.group(1) if m else l.strip()[:60]}{X}")
    print(f"  {D}被杀的那个服务当时是直接消失的，容器会自己重启 —— "
          f"屏幕上什么都看不出来，只有这里有记录。{X}")

# ================= ④ 谁在占 =================
sec("④ 谁在占")
# ---- 容器 ----
dock = run(["docker", "stats", "--no-stream", "--format",
            "{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}"], timeout=40)
rows = [l.split("\t") for l in dock.splitlines() if "\t" in l]
if rows:
    print(f"  {B}容器{X}")
    for name, usage, perc in sorted(rows, key=lambda r: r[0]):
        # MemUsage 长这样：「1.2GiB / 3.8GiB」。后一半是【limit】，
        # 没设 limit 时它就是整台机器的内存 —— 那种情况下 MemPerc 是对整机说的，
        # 不是"这个容器快满了"。不说清楚的话会被当成快爆了。
        print(f"    {name:<16}{C}{usage}{X}  {D}{perc}{X}")
    print(f"  {D}「/」后面那个数是这个容器的上限；没给容器设上限时它就是整机内存，"
          f"那时候百分比是【对整机说的】，不是这个容器快满了。{X}")
else:
    print(f"  {D}（docker stats 没读到 —— 没装 docker 或没权限）{X}")

# ---- 宿主机进程 ----
# 【只打进程名，不打命令行】argv 里可能带节点的 UUID / 密码 / reality 私钥和
# 订阅 token（仓库规矩第一条点名的东西），而这一屏是要截图发人的。
print()
print(f"  {B}宿主机进程（按占用排，只列前 12 个）{X}")
ps = run(["ps", "-eo", "rss,comm", "--sort=-rss"], timeout=20)
seen = 0
for ln in ps.splitlines()[1:]:
    f = ln.split(None, 1)
    if len(f) < 2:
        continue
    try:
        rss = int(f[0]) * 1024
    except ValueError:
        continue
    if rss < 20 << 20:            # 20 MB 以下的不值得占屏
        break
    print(f"    {f[1][:22]:<24}{C}{gb(rss):>8}{X}")
    seen += 1
    if seen >= 12:
        break
if not seen:
    print(f"  {D}（没有占用超过 20 MB 的进程）{X}")

# ---- tmpfs ----
# 【tmpfs 是真的吃内存的】Emby 的转码目录要是落在 tmpfs 上，转一部片就能吃掉
# 几个 G，而 df 看起来只是"一个文件系统满了"，跟内存一点关系都看不出来。
print()
tm = run(["df", "-B1", "--output=source,fstype,used,size,target"], timeout=20)
rows2 = []
for ln in tm.splitlines()[1:]:
    f = ln.split()
    if len(f) >= 5 and f[1] in ("tmpfs", "ramfs") and int(f[2]) > 16 << 20:
        rows2.append((f[4], int(f[2]), int(f[3])))
if rows2:
    print(f"  {B}住在内存里的文件系统（tmpfs / ramfs，超过 16 MB 的）{X}")
    for tgt, used, size in sorted(rows2, key=lambda r: -r[1]):
        print(f"    {tgt[:28]:<30}{C}{gb(used)}{X} {D}/ {gb(size)}{X}")
    print(f"  {D}这些【真的占着内存】。Emby 的转码目录要是落在这里，"
          f"转一部片就能吃掉几个 G。{X}")
else:
    print(f"  {D}没有占用明显的 tmpfs —— 内存没有被当硬盘用。{X}")

# ================= ⑤ 结论 =================
sec("⑤ 结论")
bad = []
if pct < 10:
    bad.append("能拿回来的不到一成")
if swtot and swused > swtot * 0.5:
    bad.append("swap 用掉一半以上")
if hits:
    bad.append(f"被 OOM 杀过 {len(hits)} 次")
if not bad:
    print(f"  {G}✔ 没看出内存问题{X}")
    print(f"  {D}还能拿回 {gb(avail)}（{pct:.0f}%），没有换页，没有被杀过。"
          f"面板上那个「Free」小是因为缓存占着 —— 那是好事，说明内存没闲着。{X}")
    print(f"  {D}真要省：Emby 是这里最大的一块，它的内存随媒体库大小和"
          f"同时在播的人数涨。库大就是会占，属正常。{X}")
else:
    print(f"  {R}✖ 有问题：{'；'.join(bad)}{X}")
    print(f"  {D}按上面 ④ 那一栏从大到小看，最大的那个就是要处理的。"
          f"这台机器上最常见的两种：Emby 的库太大、以及转码（转码会把整段视频"
          f"读进内存再编码）。{X}")
    print(f"  {D}转码这一条在这套东西里【本来就不该发生】—— 文件在网盘上，"
          f"本机只有一条 URL。跑 bash why-stall.sh <片名> 的 ⑥ 能看出来在不在转。{X}")
PY
