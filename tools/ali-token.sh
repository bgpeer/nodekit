#!/usr/bin/env bash
# 阿里云盘挂不上（empty token returned from official API）：查清楚是哪一步不对。
#
#   bash ali-token.sh                只看不动，把配置、令牌形态、报错原话摆出来
#   bash ali-token.sh /aliyun        只看某一个挂载点
#   bash ali-token.sh --probe        真的拿令牌去官方 API 换一次（见下面的警告）
#
# 【为什么要有 --probe，以及它为什么不是默认】
# 只读那部分靠"令牌长什么样"推断它是哪条流程取的，能覆盖绝大多数情况，但它是
# 推断。真正说了算的是官方 API 自己的回话 —— 而问它一次就会【轮换】：换成功了，
# 库里那份旧令牌当场作废，OpenList 手里就是一份死令牌，比问之前更糟。
# 所以 --probe 换成功之后会立刻把新令牌写回 OpenList（顺带把类型改成对得上的
# 那一种），保证不留下一个更坏的状态。默认不做，是因为它会动东西。
#
# 【全程不打印令牌本身】只报长度、开头几位和形态。这份输出是会被截图发出去的。
set -u

TOOL_VER="2026-08-30c"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

DIR="${MS_DIR:-/opt/media-stack}"
MP=""
PROBE=0
for a in "$@"; do
  case "$a" in
    --probe) PROBE=1 ;;
    -*)      echo "不认识的参数：$a"; exit 2 ;;
    *)       MP="$a" ;;
  esac
done

python3 - "$DIR" "$MP" "$PROBE" <<'PY'
import base64, json, os, sqlite3, subprocess, sys, time
import urllib.parse, urllib.request

DIR, WANT_MP, PROBE = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
DB = os.path.join(DIR, "openlist", "config", "data.db")
OL = "http://127.0.0.1:5244"

C = "\033[36m"; G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"
D = "\033[2m";  B = "\033[1m"; X = "\033[0m"

# alipan_type ←→ OpenList 向官方 API 报的驱动标识 ←→ 取令牌页面上的那一项。
# 三者必须是同一条线，错一环就是 empty token。对应关系来自 OpenList 源码
# drivers/aliyundrive_open/util.go 和取令牌页面 public/index.html 的 option value。
PAIR = {
    "default":  ("alicloud_qr", "阿里云盘 (OAuth2) 扫码登录"),
    "alipanTV": ("alicloud_tv", "阿里云盘 (Client) TV版扫码"),
}
RENEW = "https://api.oplist.org/alicloud/renewapi"


def die(m):
    print(f"{R}✖{X} {m}")
    sys.exit(1)


def mask(t):
    """令牌只报得出形状，报不出内容 —— 这份输出是会被截图的。"""
    n = len(t)
    if n <= 12:
        return f"{n} 字符"
    return f"{n} 字符，{t[:6]}…{t[-4:]}"


def shape(t):
    """从形状推断这份令牌是哪条流程取的。返回 (推断的 alipan_type, 说明)。

    阿里 OAuth2 那条路（跳转/扫码登录）发的刷新令牌是 JWT：三段、点分隔、
    ey 开头，里面还带 sub 和 exp —— 脚本别处就是靠这个特征读它的到期日。
    客户端那条路（TV版扫码/直接登录）发的不是 JWT，是一串没有结构的串。
    """
    if t.count(".") == 2 and t.startswith("ey"):
        return "default", "JWT 形态（三段、ey 开头）"
    return "alipanTV", "不是 JWT，一串无结构的串"


def jwt_days(t):
    try:
        seg = t.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        exp = json.loads(base64.urlsafe_b64decode(seg)).get("exp")
        return (exp - time.time()) / 86400 if exp else None
    except Exception:
        return None


def get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": "media-stack"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ol(path, body=None, token="", timeout=60):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OL + path, data=data, headers=h,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ol_login():
    pw = ""
    for f in (".secrets", ".env"):
        p = os.path.join(DIR, f)
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            if line.strip().startswith("OPENLIST_PASS="):
                pw = line.split("=", 1)[1].strip().strip('"').strip("'")
        if pw:
            break
    if not pw:
        die("读不到 OpenList 的管理密码（.secrets / .env 里的 OPENLIST_PASS）")
    r = ol("/api/auth/login", {"username": "admin", "password": pw})
    tok = (r.get("data") or {}).get("token", "")
    if not tok:
        die(f"登录 OpenList 失败：{r.get('message') or r}")
    return tok


def ol_errors(n=400):
    """从 openlist 日志里捞阿里相关的报错原话，顺手把令牌抹掉。

    OpenList 换令牌成功时会打一行 `token exchange: 旧 -> 新` —— 【原文带令牌】。
    这里要把日志摆给人看，就必须自己把它抹了，不能指望看的人注意到。
    """
    try:
        out = subprocess.run(["docker", "logs", "--tail", str(n), "openlist"],
                             capture_output=True, timeout=30)
        raw = (out.stdout + out.stderr).decode("utf-8", "replace")
    except Exception:
        return []
    hit = []
    for line in raw.splitlines():
        low = line.lower()
        if "ali" not in low:
            continue
        if "token exchange" in low:
            line = line.split("token exchange")[0] + "token exchange: (令牌已抹去)"
        elif any(k in low for k in ("error", "failed", "empty token")):
            pass
        else:
            continue
        hit.append(line.strip()[:200])
    return hit[-6:]


if not os.path.exists(DB):
    die(f"找不到 OpenList 的库：{DB}（MS_DIR 是不是不对？）")

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute("select id, mount_path, driver, status, addition "
                   "from x_storages order by mount_path").fetchall()
con.close()

ali = [r for r in rows if str(r[2]).lower() == "aliyundriveopen"]
if WANT_MP:
    ali = [r for r in ali if r[1] == WANT_MP]
if not ali:
    die("没有阿里云盘（AliyundriveOpen）存储" + (f"：{WANT_MP}" if WANT_MP else ""))
if PROBE and len(ali) > 1:
    die(f"有 {len(ali)} 个阿里存储，--probe 一次只动一个。"
        f"指定挂载点：{' / '.join(r[1] for r in ali)}")

# 同一份刷新令牌被两个存储用，是个自己会把自己搞死的配置：谁先换谁把对方作废。
seen = {}
for sid, mp, drv, status, add in ali:
    try:
        seen.setdefault(json.loads(add).get("refresh_token", ""), []).append(mp)
    except Exception:
        pass
dup = [v for k, v in seen.items() if k and len(v) > 1]

for sid, mp, drv, status, add in ali:
    a = json.loads(add) if add else {}
    tok = str(a.get("refresh_token") or "")
    typ = str(a.get("alipan_type") or "default")
    api = str(a.get("api_url_address") or RENEW)
    online = a.get("use_online_api", True)
    cid = str(a.get("client_id") or "")

    print()
    print("=" * 58)
    print(f"  {B}{mp}{X}   {D}{drv}{X}")
    print("=" * 58)
    st = str(status or "")
    icon = f"{G}✔{X}" if st == "work" else f"{R}✖{X}"
    print(f"  {icon} 上次初始化   {st or '(空)'}")

    want_txt, want_page = PAIR.get(typ, ("?", "?"))
    print(f"    账户类型     {C}{typ}{X}  {D}→ 向官方 API 报 {want_txt}{X}")
    print(f"    在线接口     {'开' if online else f'{Y}关{X}'}   {D}{api}{X}")
    if not online:
        print(f"    {Y}在线接口关着{X}{D} —— 那就得自己填 client_id/client_secret 走"
              f"本地刷新，令牌类型这一套不适用{X}")
    if cid:
        print(f"    自建应用     {D}填了 client_id{X}")

    if not tok:
        print(f"  {R}✖ 刷新令牌是空的{X} —— 存的时候没粘上，或者粘进了访问令牌那一栏")
        continue

    guess, why = shape(tok)
    guess_txt, guess_page = PAIR[guess]
    print(f"    刷新令牌     {D}{mask(tok)}{X}")
    print(f"                 {D}{why} → 像是「{guess_page}」取的{X}")
    if tok != tok.strip():
        print(f"  {R}✖ 令牌首尾带空白/换行{X} —— 粘贴时多带了，重存一次")
    dys = jwt_days(tok)
    if dys is not None:
        c = G if dys > 30 else (Y if dys > 0 else R)
        print(f"    令牌到期     {c}{dys:.0f} 天{X}"
              + (f"  {R}已经过期了{X}" if dys <= 0 else ""))

    if any(mp in g for g in dup):
        print(f"  {R}✖ 这份令牌和另一个存储用的是同一份{X}"
              f"（{'、'.join(sum(dup, []))}）")
        print(f"    {D}换令牌是轮换制：谁先换，另一边手里那份当场作废。"
              f"两个存储要各扫各的{X}")

    print()
    # 【状态是 work 就先把这句说死】不然一屏"形态对得上"加几行红色的历史日志，
    # 看的人还是不知道到底好了没有 —— 这一版之前就是这样，等于白查。
    if st == "work" and guess == typ:
        print(f"  {G}▸ 没问题{X}：类型和令牌配对，存储是 {G}work{X}，现在挂着的。")
    elif guess == typ:
        print(f"  {D}形态和类型对得上。{X}"
              f"{Y}但它还是挂不上{X}{D} —— 那多半是令牌本身废了"
              f"（过期 / 被别处轮换掉 / 只复制了一半），要重新扫码{X}")
    else:
        print(f"  {R}▸ 对不上{X}：类型 {C}{typ}{X} 要的是「{want_page}」取的令牌，"
              f"库里这份像是「{guess_page}」取的")
        print(f"    {D}两条路二选一：{X}")
        print(f"    {D}① 把账户类型改成 {X}{C}{guess}{X}{D}，令牌不动{X}")
        print(f"    {D}② 保持 {typ}，去 https://api.oplist.org/ 选"
              f"「{want_page}」重扫一个{X}")

    errs = ol_errors()
    if errs:
        # 【日志是流水账，不是现状】存储已经 work 了，日志里那几行红字多半是修好
        # 之前留下的。不说这一句的话，一堆 ERROR 摆在"没问题"下面，等于把刚下的
        # 结论又推翻一次。每行都带时间戳，对一下就知道是不是刚才的事。
        print(f"\n  {B}OpenList 日志里的原话{X}{D}（令牌已抹去）{X}")
        if st == "work":
            print(f"  {D}这是流水账、不是现状 —— 存储现在是 work 的，"
                  f"下面这些多半是修好之前留下的。看行首的时间{X}")
        for e in errs:
            print(f"    {D}{e}{X}")
        # openFile/list 超时是【列目录慢】，和令牌一点关系都没有 —— 挂在这里
        # 只会让人以为是同一件事，所以单独点破它是什么、以及已经在治它了。
        if any("openfile/list" in e.lower() and "deadline" in e.lower()
               for e in errs):
            print(f"  {D}其中 openFile/list ... deadline exceeded 是"
                  f"{X}列目录跨境超时{D}，不是令牌的事。"
                  f"目录缓存拉长到 12 小时就是在少问几次{X}")

    if not PROBE:
        if st == "work":
            # 存储好好的还去 --probe，等于平白烧掉一次轮换。别提这个选项。
            print(f"\n  {D}挂着的存储没必要 --probe，那会平白换掉一次令牌。{X}")
        else:
            print(f"\n  {D}以上是按形态推断的。想让官方 API 自己说，加 --probe —— "
                  f"它会真的换一次令牌，换到了自动写回。{X}")
        continue

    # ---------------------------------------------------------------- 真换一次
    print(f"\n  {B}拿库里这份令牌去官方 API 换一次{X}"
          f"{D}（换成功 = 旧的当场作废，脚本会立刻写回新的）{X}")
    order = [typ] + [k for k in PAIR if k != typ]   # 先试当前配的这种
    good = None
    for t in order:
        txt = PAIR[t][0]
        u = api + "?" + urllib.parse.urlencode(
            {"refresh_ui": tok, "server_use": "true", "driver_txt": txt})
        try:
            r = get(u)
        except Exception as e:
            print(f"    按 {txt:<12} {R}问不到官方 API：{e}{X}")
            continue
        nr, na = r.get("refresh_token", ""), r.get("access_token", "")
        if nr and na:
            print(f"    按 {txt:<12} {G}成功{X}{D}，换到了新令牌{X}")
            good = (t, nr, na)
            break
        why = r.get("text") or r.get("message") or "官方 API 回了空"
        print(f"    按 {txt:<12} {R}失败{X}{D}：{str(why)[:120]}{X}")

    if not good:
        print(f"\n  {R}▸ 两种都换不到{X} —— 令牌本身已经不能用了"
              f"（过期 / 被别处轮换掉 / 复制漏了）")
        print(f"    {D}去 https://api.oplist.org/ 重扫一个，"
              f"选哪一项要和账户类型对上{X}")
        continue

    newtyp, nr, na = good
    a["refresh_token"], a["access_token"], a["alipan_type"] = nr, na, newtyp

    # 【从这里开始，旧令牌已经作废了】上面那次换取一成功，库里那份就成了废纸，
    # 手里这个 nr 是全世界唯一还能用的一份。所以这一段无论怎么炸，都不能就那么
    # 炸出去 —— 炸了就把 nr 原样打出来让人手动粘回 OpenList。
    # 这是这套脚本里【唯一】会打印令牌的地方，破例的理由就是上面这句：
    # 不打印 = 直接丢掉这个盘的授权。所以也要同时说清楚"这行别截图"。
    try:
        tk = ol_login()
        cur = (ol(f"/api/admin/storage/get?id={sid}", token=tk).get("data") or {})
        if not cur:
            raise RuntimeError("读不回这个存储")
        cur["addition"] = json.dumps(a, ensure_ascii=False)
        rr = ol("/api/admin/storage/update", cur, token=tk)
        if rr.get("code") != 200:
            raise RuntimeError(str(rr.get("message") or rr))
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n  {R}✖ 写不回 OpenList：{e}{X}")
        print(f"  {R}{B}旧令牌刚才那一下已经作废了{X}，下面这份是现在唯一能用的，"
              f"手动粘回去：")
        print(f"    {D}OpenList → 存储 → {mp} → 编辑 → 刷新令牌{X}")
        if newtyp != typ:
            print(f"    {D}顺手把「阿里盘账户类型」改成 {X}{C}{newtyp}{X}")
        print(f"\n{Y}    ↓ 这一行别截图发出去 ↓{X}")
        print(f"    {B}{nr}{X}\n")
        continue
    print(f"  {G}✔{X} 已写回 {mp}：新刷新令牌"
          + (f" + 账户类型 {typ} → {C}{newtyp}{X}" if newtyp != typ else ""))

    # 写回之后 OpenList 会自己重新初始化这个存储，等一下再回读状态才作数。
    s = ""
    for _ in range(10):
        time.sleep(2)
        try:
            s = str((ol(f"/api/admin/storage/get?id={sid}",
                        token=tk).get("data") or {}).get("status") or "")
        except Exception:
            continue
        if s and s != status:
            break
    if s == "work":
        print(f"  {G}✔{X} 存储状态：{G}work{X} —— 挂上了")
        print(f"  {D}MediaWarp 手里可能还是旧的 OpenList 会话，"
              f"播放报 401 就敲：docker restart mediawarp{X}")
    else:
        print(f"  {Y}⚠{X} 存储状态：{s or '(还在初始化)'}")
        print(f"    {D}去 OpenList → 存储 → 这一条 → 点一下「重新加载」再看{X}")
PY
