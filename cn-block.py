#!/usr/bin/env python3
# cn-block.py —— 屏蔽中国域名/IP（sing-box 服务端路由）+ 白名单放行
# 独立文件，方便单独维护；nodekit 主脚本(xy-installer.py)通过子进程调用：
#   python3 cn-block.py            交互菜单
#   python3 cn-block.py apply      按已存状态重新注入（未开启则直接跳过）——重装后自动调用
#   python3 cn-block.py refresh    规则集有变化才刷新缓存并重启（cron 每天北京 03:00 调用，任何时区 / 夏令时都对）
#   python3 cn-block.py remove     卸载屏蔽规则
#
# 规则集用 sing-box 远程 srs（.srs binary），并挂 cron 每天北京时间 03:00 定点刷新：
#   CN 域名 geosite/geolocation-cn.srs、CN IP geoip/cn.srs → reject
#   白名单（作者名单对齐 vps-net/whitelist-inject.sh 的 WHITELIST_TAGS）→ 命中直连放行
import os, re, ast, sys, json, time, ipaddress, subprocess, urllib.request, urllib.error

# 出网请求统一的 User-Agent。原来各脚本各报各的家门（xy-installer / media-stack /
# vps-check / net-optimize / xy-sub），等于主动告诉沿途任何人「这台机器在跑 nodekit」——
# GitHub、jsDelivr、公共反代、ip-api 都看得到。换成最常见的 curl 串：不自报家门，
# 而且脚本里 urllib 和 curl 两条路发出去的请求看起来是一致的，不会一台机器两副面孔。
# 别指望它防指纹：TLS 握手特征、请求头顺序照样能认出是 Python。这一步只是不主动声明身份。
HTTP_UA = "curl/8.5.0"

# 跟 xy-installer.py 同一套（菜单 10 是子进程跑本文件，主脚本那份管不到这里）
# 【菜单编号统一高亮】（独占一行的「  3. xx」和并排的「1. a   2. b」都算）仓库主人：「像这种被选项数字 0-20 可以做成高亮的吗，包括里面的选项数字，所有的只要是被选项
# 数字都变成高亮……跟下面的 bgpeer 一样的颜色」。不去改几十处菜单代码：屏幕上每一行「行首空格 + 1~2 位数字 + 点 +
# 空格」的，数字那段换成 bgpeer 同色（粗体绿 1;32）。只在直接印到终端时上色 —— 进日志 / 管道 / 测试的照旧是纯文本。
import builtins as _builtins
import shutil
import sys as _sys
_MENU_NUM_RE = re.compile(r"(^[ \t]*|(?<=  ))(\d{1,2}\.)(?=\s)", re.M)   # 行首的，或并排的（前面两个空格）


_MENU_SEEN = set()                        # 这一屏印出来的编号；问「请选择」时打的是这里面的，才回头染绿（见 _ask_paint）


def _menu_num_paint(m):
    _MENU_SEEN.add(m.group(2)[:-1])
    return f"{m.group(1)}\033[1;32m{m.group(2)}\033[0m"


def _ask_paint(prompt_shown, v, seen):
    """打完回车之后：打的是菜单上有的编号 → 把这一行重印一遍、数字染成同色；菜单上没有的照旧普通颜色。

    仓库主人：「这个输入只有上面预设好了的才变成高亮，如果你输入的上面是没有的应该就是普通的颜色」。
    打字那一刻是终端自己回显的，还不知道最后打什么，所以只能回车后回头重印。整行放不下一行（会折行）
    就不重印，免得把上一行盖花。"""
    try:
        if not v or v not in seen or not _sys.stdout.isatty():
            return
        cols = shutil.get_terminal_size((60, 20)).columns
        wide = sum(2 if ord(ch) > 0x2E80 else 1 for ch in prompt_shown + v)
        if wide >= cols:
            return
        _builtins.print(f"\033[1A\r\033[2K{prompt_shown}\033[1;32m{v}\033[0m", flush=True)
    except Exception:
        pass


# 【不带点的编号也算】子菜单大多写成「  1 修改配置」「1 作者模板   2 自定义模板   0 返回」，上面那条只认
# 「1.」，于是只有主菜单亮、里面几层全是白的。仓库主人：「我当时是想把所有的需要输入按钮的都要做成高亮」。
# 不带点的数字太容易撞上普通文字（「  80 端口被占」「  5 条候选」），所以收得紧：
#   · 只认 0~29；后面空格隔开紧跟中文（量词「条个台次秒分……」不算），
#     独占一行的单个数字后面跟英文也算（「  1 smux 开关」「  4 Emby 证书」）；
#   · 并排的要前面至少三个空格（菜单项之间都是三四个空格），或紧跟中文冒号（「计费方式：1 双向相加」）。
_MENU_BARE_RE = re.compile(r"(^[ \t]+|(?<=   )|(?<=：))([12]?\d)( +)(?=(\S))", re.M)
_MENU_UNIT = "条个台次秒分小天行项端倍位张份组路周月年号字块元"


def _menu_bare_paint(m):
    head, num, gap, nxt = m.groups()
    cjk = "\u4e00" <= nxt <= "\u9fff" and nxt not in _MENU_UNIT
    word = bool(head) and len(num) == 1 and nxt.isascii() and nxt.isalpha()
    if not (cjk or word):
        return m.group(0)
    _MENU_SEEN.add(num)
    return f"{head}\033[1;32m{num}\033[0m{gap}"


def _menu_paint(s):
    return _MENU_BARE_RE.sub(_menu_bare_paint, _MENU_NUM_RE.sub(_menu_num_paint, s))


def print(*args, **kw):                   # noqa: A001 —— 故意盖住内置 print，见上
    try:
        f = kw.get("file") or _sys.stdout
        if args and isinstance(args[0], str) and f is _sys.stdout and f.isatty():
            args = (_menu_paint(args[0]),) + args[1:]
    except Exception:
        pass
    return _builtins.print(*args, **kw)

SB_DIR  = "/etc/sing-box"
SB_BIN  = "/usr/local/bin/sing-box"
BGP_DIR = "/etc/bgpeer"
CNBLOCK_FILE = BGP_DIR + "/cnblock.json"        # 记住是否开启 + 白名单来源
SELF_PATH    = BGP_DIR + "/cn-block.py"          # cron 调用的本地副本
CRON_FILE    = "/etc/cron.d/bgpeer-cnblock"      # 每日定点刷新规则集
CRON_LOG     = "/var/log/bgpeer-cnblock.log"
# 规则集优先走 jsDelivr 镜像（不受 GitHub raw 的 429 限流），回退 raw。
RULES_CDN    = "https://cdn.jsdelivr.net/gh/bgpeer/rules@main/geo"
RULES_RAW    = "https://raw.githubusercontent.com/bgpeer/rules/main/geo"
# 作者放行白名单：这些 CN 服务照常直连，其余 CN 一律拦
CN_WHITELIST = [
  "bytedance", 
  "tiktok", 
  "category-games-!cn", 
  "bilibili",
  "xiaohongshu", 
  "alibaba", 
  "tencent", 
  "kuaishou",
  "geolocation-!cn"
]

# ── 写死的单条放行（改这里 = 所有装了本脚本的机器都生效）───────────────────────
# 和菜单「4 单条放行域名/IP」是同一套机制，两边的条目会合并、自动去重。区别：
#   · 写在这里     → 跟着脚本走，改一次全网生效；但只能改【仓库里的】cn-block.py，
#                    因为 VPS 上的 /etc/bgpeer/cn-block.py 每次进菜单都会被重新下载覆盖
#   · 菜单里加     → 只对这台机器生效，存在 /etc/bgpeer/cnblock.json，更新脚本不会丢
# 域名和 IP 分开两个列表，各自只按自己的类型校验，不做自动识别，避免把打错的 IP
# （如 999.1.1.1）当成域名收下变成一条永不命中的死规则。
ALLOW_DOMAINS = [
    # "example.com",        # 后缀匹配：连同它的所有子域一起放行
]
ALLOW_IPS = [
    # "1.2.3.4",            # 单个 IP，自动补成 /32
    # "10.0.0.0/8",         # CIDR
    # "2001:db8::/32",      # IPv6 也行
]

# ── 屏上排版小工具（同 xy-installer.py，CLAUDE.md 第五节「排版要整齐干净」）──────────
def _hl(code, text):
    return f"\033[{code}m{text}\033[0m" if _sys.stdout.isatty() else text

def ui_title(text):
    print("\n" + "=" * 60 + f"\n  {text}\n" + "=" * 60)

def ui_line():
    print("-" * 60)

def ui_items(items):
    for num, name, cur in items:
        print(f"  {num}. {name}" + (f"　当前：{cur}" if cur not in (None, "") else ""))

def ui_tip(text):
    print("  " + _hl("1;33", f"提示：{text}"))

def ui_kv(label, value):
    print(f"  {_hl('36', label + '：')}{value}")

def ui_url(label, url, code="1;32"):
    print(_hl("1;33", f"▸ {label}"))
    print(_hl(code, url) if url else _hl("2", "（还没有）"))


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()

def _ask(prompt=""):
    """交互输入：优先读 /dev/tty，使 curl|python3 管道下仍可交互。"""
    try:
        with open("/dev/tty", "r") as t:
            _seen = set(_MENU_SEEN)
            _MENU_SEEN.clear()
            print(prompt, end="", flush=True)
            line = t.readline()
            if line == "":
                raise EOFError
            v = line.rstrip("\n").strip()
            _ask_paint(prompt, v, _seen)
            return v
    except (OSError, EOFError):
        return input(prompt).strip()

def _mirrors(url):
    """raw.githubusercontent 常被限流(429)，补上 jsDelivr 镜像作兜底。"""
    urls = [url]
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url)
    if m:
        o, repo, br, path = m.groups()
        urls.append(f"https://cdn.jsdelivr.net/gh/{o}/{repo}@{br}/{path}")
        urls.append(f"https://fastly.jsdelivr.net/gh/{o}/{repo}@{br}/{path}")
    return urls

def fetch_url(url):
    """带重试 + 镜像兜底的拉取，缓解 GitHub 429 限流。"""
    last = None
    for rd in range(2):
        for u in _mirrors(url):
            try:
                req = urllib.request.Request(u, headers={"User-Agent": HTTP_UA})
                return urllib.request.urlopen(req, timeout=15).read().decode()
            except Exception as e:
                last = e
        time.sleep(2 * (rd + 1))
    raise last

def cnblock_load():
    try: return json.load(open(CNBLOCK_FILE))
    except Exception: return {}

def _write_json(path, obj):
    """原子写：先写临时文件再 replace，进程中途被杀也不会留下写了一半的配置。"""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def cnblock_save(d):
    os.makedirs(BGP_DIR, exist_ok=True)
    _write_json(CNBLOCK_FILE, d)

def _http_code(url):
    """HEAD 探测 HTTP 状态码。不走 shell（url 可能含外部内容，避免注入）。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": HTTP_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return str(r.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception:
        return "000"

def _rule_url(rel):
    """选规则集 URL（rel 形如 'geosite/geolocation-cn.srs'）：
       - 优先 jsDelivr、回退 raw，确认能拿到 200 就用它；
       - 只是临时拉不到（429/超时/5xx 等）时，仍返回 jsDelivr 地址「先注入着」，
         sing-box 会在 24h 自动更新时重新拉——不因一时限流拖累其它能用的规则集；
       - 只有两个源都明确 404（压根不存在，如 wildrift）才返回 '' 跳过。"""
    codes = []
    for base in (RULES_CDN, RULES_RAW):
        u = f"{base}/{rel}"
        c = _http_code(u)
        if c == "200":
            return u
        codes.append(c)
    if all(c == "404" for c in codes):                   # 确认不存在 → 跳过
        return ""
    return f"{RULES_CDN}/{rel}"                           # 临时拉不到 → 先注入，交给自动更新重拉

def _is_cnblk_rule(r):
    """判断一条 route.rule 是不是本脚本注入的（引用了 cnblk- 开头的规则集）。"""
    rs = r.get("rule_set")
    if isinstance(rs, str):  return rs.startswith("cnblk-")
    if isinstance(rs, list): return any(str(x).startswith("cnblk-") for x in rs)
    return False

# ── 单条放行（域名 / IP）──────────────────────────────────────────────────────
# 规则集白名单是「整组放行」(bilibili、tencent 这种)，粒度太粗；这里补一个单条的口子，
# 用 sing-box 的 inline rule_set 实现（tag 同样以 cnblk- 开头，复用现有的清理逻辑，
# 不会重复注入）。域名按【后缀】匹配：填 example.com 连它的子域一起放行。
_DOM_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
    r"(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)+$")

def _clean(s):
    """去掉常见的前缀/大小写/结尾点等写法差异。"""
    s = (s or "").strip().lower().rstrip(".")
    for p in ("*.", "+.", "."):
        if s.startswith(p):
            s = s[len(p):]
    return s

def norm_domain(s):
    """只按【域名】校验，返回规范化域名或 (None, 原因)。中文域名转 punycode。"""
    s = _clean(s)
    if not s:
        return None, "不能为空"
    if ":" in s or re.fullmatch(r"[\d.]+(/\d+)?", s):
        return None, "这看着是 IP，请写到 IP 列表里"
    if not s.isascii():
        try:
            s = s.encode("idna").decode()
        except Exception:
            return None, "域名含无法编码的字符"
    if not _DOM_RE.match(s):
        return None, "不是合法域名"
    return s, ""

def norm_ip(s):
    """只按【IP / CIDR】校验，返回规范化 CIDR 或 (None, 原因)。单个 IP 自动补掩码。"""
    s = _clean(s)
    if not s:
        return None, "不能为空"
    try:
        return str(ipaddress.ip_network(s, strict=False)), ""
    except ValueError:
        return None, "不是合法 IP/CIDR"

def parse_wl_entry(s):
    """菜单交互用：自动判断是域名还是 IP，返回 ('ip'|'domain', 值) / (None, 原因)。
       先试 IP——域名正则也能匹配 1.2.3.4，顺序反了会把 IP 当域名。
       看着像 IP 却解析失败（999.1.1.1）直接报错，不默默当成永不命中的域名。"""
    s = _clean(s)
    if not s:
        return None, "不能为空"
    v, _ = norm_ip(s)
    if v:
        return "ip", v
    if ":" in s or re.fullmatch(r"[\d.]+(/\d+)?", s):
        return None, "IP/CIDR 格式不对"
    v, why = norm_domain(s)
    return ("domain", v) if v else (None, why)

def _script_custom():
    """脚本里写死的单条放行。域名/IP 分开校验，写错的跳过并提示，不让它污染配置。"""
    doms, ips = [], []
    for raw in ALLOW_DOMAINS:
        v, why = norm_domain(raw)
        if v:
            doms.append(v)
        else:
            print(f"  跳过 ALLOW_DOMAINS 里的 {raw!r}：{why}")
    for raw in ALLOW_IPS:
        v, why = norm_ip(raw)
        if v:
            ips.append(v)
        else:
            print(f"  跳过 ALLOW_IPS 里的 {raw!r}：{why}")
    return doms, ips

def _link_custom(cfg):
    """自定义链接里的单条放行（域名/IP）。只在选了「自定义名单」时才算数。"""
    if cfg.get("wl_mode") != "custom":
        return [], []
    d = _link_data(cfg)
    return (d[1], d[2]) if d else ([], [])

def _wl_custom(cfg):
    """单条放行的最终列表 = 脚本写死的 + 自定义链接里的 + 菜单加的，按顺序去重。
       存量条目也过一遍规范化：手改过 cnblock.json 时 8.8.8.8 与 8.8.8.8/32
       会被当成两条而去重失败，统一成 CIDR 形式才能真正去重。"""
    sd, si = _script_custom()
    ld, li = _link_custom(cfg)
    doms, ips = list(sd) + list(ld), list(si) + list(li)
    for raw in (cfg.get("wl_domains") or []):
        v, _ = norm_domain(raw)
        if v:
            doms.append(v)
    for raw in (cfg.get("wl_ips") or []):
        v, _ = norm_ip(raw)
        if v:
            ips.append(v)
    return list(dict.fromkeys(doms)), list(dict.fromkeys(ips))

_LINK_CACHE = {}                                          # url -> (tags, domains, ips)，避免一次运行里重复拉

def _parse_remote_list(txt):
    """从远端名单文件里提取 (tags, domains, ips)。
       ⚠ 只做【解析】，绝不执行远端文件——用 ast.parse 取字面量，拿不到再退回
         bash 数组 / 纯文本。远端内容一律当数据看待，逐条校验后才使用。
       支持三种写法：
         ① Python 列表（推荐，即本脚本同款样板）：
              WHITELIST_TAGS / CN_WHITELIST = ["bilibili", ...]
              ALLOW_DOMAINS = ["example.com", ...]
              ALLOW_IPS     = ["1.2.3.4", ...]
         ② bash 数组：WHITELIST_TAGS=(...)   （兼容 whitelist-inject.sh）
         ③ 纯文本：每行一个 tag，# 开头为注释"""
    tags, doms, ips = [], [], []
    got = False
    try:
        for node in ast.parse(txt).body:                  # ast.parse 只解析不执行
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                name = getattr(tgt, "id", "")
                if name not in ("WHITELIST_TAGS", "CN_WHITELIST", "ALLOW_DOMAINS", "ALLOW_IPS"):
                    continue
                try:
                    val = ast.literal_eval(node.value)    # 只认字面量，函数调用等一律取不到
                except Exception:
                    continue
                if not isinstance(val, (list, tuple)):
                    continue
                got = True
                items = [str(x) for x in val]
                if name in ("WHITELIST_TAGS", "CN_WHITELIST"):
                    tags += items
                elif name == "ALLOW_DOMAINS":
                    doms += items
                else:
                    ips += items
    except (SyntaxError, ValueError):
        pass                                              # 不是 Python 文件，往下退
    if not got:
        m = re.search(r"WHITELIST_TAGS=\(([^)]*)\)", txt, re.S)
        if m:                                             # bash 数组（whitelist-inject.sh）
            tags = re.findall(r'[A-Za-z0-9!_.\-]+', m.group(1)); got = True
    if not got:
        for ln in txt.splitlines():                       # 纯文本：每行一个 tag
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                tags.append(ln.split()[0])
    return tags, doms, ips

# 自定义链接常被贴成「网页」地址——GitHub 文件页 / gist 页面，拉下来是一整页 HTML。
# 换成对应的原始文件链接（前面带了镜像前缀的也认，前缀保留）；和 xy-installer 的 tpl_raw_url 同一套：
#   github.com/<o>/<r>/blob/<ref>/<path>[?plain=1][#L10] → raw.githubusercontent.com/<o>/<r>/<ref>/<path>
#   gist.github.com/<user>/<id>                         → gist.githubusercontent.com/<user>/<id>/raw
_GH_BLOB_RE = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/blob/([^?#\s]+)")
_GIST_PAGE_RE = re.compile(r"https://gist\.github\.com/([^/\s]+)/([0-9A-Fa-f]+)/?(?:[?#]\S*)?$")

def raw_link(url):
    """GitHub 文件页 / gist 页面链接换成原始文件链接；其它原样返回。"""
    u = (url or "").strip()
    m = _GH_BLOB_RE.search(u)
    if m:
        o, r, rest = m.groups()
        return u[:m.start()] + f"https://raw.githubusercontent.com/{o}/{r}/{rest}"
    m = _GIST_PAGE_RE.search(u)
    if m:
        return u[:m.start()] + f"https://gist.githubusercontent.com/{m.group(1)}/{m.group(2)}/raw"
    return u

def _link_data(cfg):
    """拉取并解析自定义链接，返回校验后的 (tags, domains, ips)。失败返回 None。
       页面链接先换成原始文件链接（老版本存下的也照样能用）。"""
    url = raw_link(cfg.get("wl_url"))
    if not url:
        return None
    if url in _LINK_CACHE:
        return _LINK_CACHE[url]
    try:
        txt = fetch_url(url)
    except Exception as e:
        print("  拉取自定义名单失败:", e); return None
    raw_t, raw_d, raw_i = _parse_remote_list(txt)
    tags = []
    for t in raw_t:
        # 远端内容只当 tag 用，字符集收紧到规则集名允许的范围，防止混入奇怪内容
        if re.fullmatch(r"[A-Za-z0-9!_.\-]+", t):
            tags.append(t)
        else:
            print(f"  跳过非法 tag: {t!r}")
    doms, ips = [], []
    for r in raw_d:
        v, why = norm_domain(r)
        if v: doms.append(v)
        else: print(f"  跳过链接里的域名 {r!r}：{why}")
    for r in raw_i:
        v, why = norm_ip(r)
        if v: ips.append(v)
        else: print(f"  跳过链接里的 IP {r!r}：{why}")
    _LINK_CACHE[url] = (tags, doms, ips)
    return _LINK_CACHE[url]

def _whitelist_tags(cfg):
    """取白名单 tag 列表：作者名单 / 自定义名单（链接里的 tag 部分）。"""
    mode = cfg.get("wl_mode", "author")
    if mode == "none":
        return []
    if mode == "custom":
        if not (cfg.get("wl_url") or "").strip():
            print("  未设置自定义放行名单链接，改用作者名单。"); return list(CN_WHITELIST)
        d = _link_data(cfg)
        if d is None:
            print("  改用作者名单。"); return list(CN_WHITELIST)
        return d[0]
    return list(CN_WHITELIST)

def apply_cn_block(cfg=None):
    """把 CN 屏蔽 + 白名单放行规则注入 sing-box 服务端配置并重启（失败回滚）。"""
    sb_cfg = f"{SB_DIR}/config.json"
    if not os.path.exists(sb_cfg):
        print("没检测到 sing-box 配置，请先在主脚本『1.安装』。"); return False
    cfg = cfg or cnblock_load()
    try:
        conf = json.load(open(sb_cfg))
    except Exception as e:
        print("读取 sing-box 配置失败:", e); return False
    backup = json.loads(json.dumps(conf))                # 深拷贝，校验失败时回滚

    # 直连出站需有 tag（白名单命中后 detour 到它放行）
    obs = conf.get("outbounds") or [{"type": "direct"}]
    direct_tag = ""
    for o in obs:
        if o.get("type") == "direct":
            o.setdefault("tag", "direct"); direct_tag = o["tag"]; break
    if not direct_tag:
        obs.append({"type": "direct", "tag": "direct"}); direct_tag = "direct"
    conf["outbounds"] = obs

    route = conf.get("route") or {}
    # 清掉本脚本上次注入的规则集/规则（cnblk- 前缀），保留其它
    rsets = [r for r in route.get("rule_set", []) if not str(r.get("tag", "")).startswith("cnblk-")]
    keep_rules = [r for r in route.get("rules", []) if not _is_cnblk_rule(r)]

    wl_refs = []
    print("  预检白名单规则集…")
    for t in _whitelist_tags(cfg):
        url = _rule_url(f"geosite/{t}.srs")
        if url:
            tag = "cnblk-wl-" + t
            rsets.append({"type": "remote", "tag": tag, "format": "binary", "url": url,
                          "download_detour": direct_tag, "update_interval": "24h"})
            wl_refs.append(tag)
        else:
            print(f"    跳过 {t}（该规则集不存在）")
    cn_site = _rule_url("geosite/geolocation-cn.srs")   # 全部 CN 域名
    cn_ip   = _rule_url("geoip/cn.srs")                 # 全部 CN IP
    if not cn_site or not cn_ip:                         # 只有确认 404 才会走到这（正常不会）
        print("CN 核心规则集不存在，无法屏蔽。未改动配置。")
        return False
    rsets.append({"type": "remote", "tag": "cnblk-cn-site", "format": "binary", "url": cn_site,
                  "download_detour": direct_tag, "update_interval": "24h"})
    rsets.append({"type": "remote", "tag": "cnblk-cn-ip", "format": "binary", "url": cn_ip,
                  "download_detour": direct_tag, "update_interval": "24h"})

    # 单条放行（域名/IP）：inline rule_set，域名与 IP 必须拆成两条 headless 规则——
    # 同一条规则里不同字段是 AND 关系，写一起就变成「既要域名匹配又要 IP 匹配」永不命中。
    # 空数组会被 sing-box 判为非法配置，所以没有条目时整个跳过、不注入空壳。
    wl_doms, wl_ips = _wl_custom(cfg)
    custom_ref = ""
    if wl_doms or wl_ips:
        hr = []
        if wl_doms:
            hr.append({"domain_suffix": wl_doms})        # 后缀匹配：含该域名及其全部子域
        if wl_ips:
            hr.append({"ip_cidr": wl_ips})
        custom_ref = "cnblk-wl-custom"
        rsets.append({"type": "inline", "tag": custom_ref, "rules": hr})

    # 规则顺序：单条放行 → 规则集白名单放行（都在前，命中即直连不被拦）
    #           → CN 域名拦 → CN IP 拦 → 原有其它规则
    inj = []
    if custom_ref:
        inj.append({"rule_set": custom_ref, "outbound": direct_tag})
    if wl_refs:
        inj.append({"rule_set": wl_refs, "outbound": direct_tag})
    inj.append({"rule_set": "cnblk-cn-site", "action": "reject"})
    inj.append({"rule_set": "cnblk-cn-ip", "action": "reject"})

    route["rule_set"] = rsets
    route["rules"] = inj + keep_rules
    conf["route"] = route
    # 远程 rule_set 建议开 cache_file 持久化（否则每次重启都重新拉、且 sing-box 会告警）
    exp = conf.get("experimental") or {}
    cf = exp.get("cache_file") or {}
    cf["enabled"] = True; cf.setdefault("path", f"{SB_DIR}/cache.db")
    exp["cache_file"] = cf; conf["experimental"] = exp
    _write_json(sb_cfg, conf)

    def _rollback():
        """回滚到注入前配置，并把 enabled 状态对齐回滚后的实际情况——
           重装后配置里已无 cnblk 规则时，不再让菜单显示『已开启』误导用户。"""
        _write_json(sb_cfg, backup)
        cfg["enabled"] = any(str(r.get("tag", "")).startswith("cnblk-")
                             for r in (backup.get("route") or {}).get("rule_set", []))
        cnblock_save(cfg)

    r = subprocess.run(f"{SB_BIN} check -c {sb_cfg}", shell=True, text=True, capture_output=True)
    if r.returncode:
        _rollback()
        print("注入后配置校验失败，已回滚未生效：\n" + (r.stderr or r.stdout).strip()); return False
    cfg["enabled"] = True; cnblock_save(cfg)             # 校验已过，状态先落盘：即便重启掐断 SSH，状态也已正确
    sh("systemctl restart sing-box", check=False)
    # 确认真的起来了；万一注入后起不来（比如规则集这会儿全拉不到），回滚到屏蔽前配置，
    # 绝不影响原本能用的节点
    active = False
    for _ in range(10):
        time.sleep(1)
        if sh("systemctl is-active sing-box", check=False) == "active":
            active = True; break
    if not active:
        _rollback()
        sh("systemctl restart sing-box", check=False)
        print("注入后 sing-box 未能启动，已回滚到屏蔽前配置（节点照常可用）。可能是规则集暂时全拉不到，稍后再试。")
        return False
    cfg["enabled"] = True; cnblock_save(cfg)
    setup_cron()                                        # 每天北京 03:00 定点刷新规则集
    extra = f"，单条放行 {len(wl_doms)} 域名 + {len(wl_ips)} IP" if (wl_doms or wl_ips) else ""
    print(f"\n✓ 已开启屏蔽中国域名/IP：放行白名单 {len(wl_refs)} 组{extra}，其余 CN 域名+IP 一律拦截。")
    print("  规则集每天北京时间 03:00 自动刷新（cron）；临时拉不到的会在下次刷新补齐，不影响已生效的。")
    return True

def remove_cn_block(silent=False):
    """移除本脚本注入的 CN 屏蔽/白名单规则，恢复不拦截。"""
    sb_cfg = f"{SB_DIR}/config.json"
    if os.path.exists(sb_cfg):
        try:
            conf = json.load(open(sb_cfg))
            route = conf.get("route") or {}
            route["rule_set"] = [r for r in route.get("rule_set", []) if not str(r.get("tag", "")).startswith("cnblk-")]
            route["rules"] = [r for r in route.get("rules", []) if not _is_cnblk_rule(r)]
            for k in ("rule_set", "rules"):                 # 清空的键不留着
                if not route.get(k):
                    route.pop(k, None)
            if route:                                       # route 里还有 final 等其它键 → 保留
                conf["route"] = route
            else:
                conf.pop("route", None)
            _write_json(sb_cfg, conf)
            sh("systemctl restart sing-box", check=False)
        except Exception as e:
            print("处理配置失败:", e)
    remove_cron()                                         # 一并撤掉每日刷新的定时任务
    try: os.remove(CNBLOCK_FILE)                          # 卸载即清状态，之后重装不会再自动注入
    except OSError: pass
    if not silent:
        print("已卸载屏蔽，恢复为不拦截 CN。")

def _cache_path():
    try:
        conf = json.load(open(f"{SB_DIR}/config.json"))
        return conf.get("experimental", {}).get("cache_file", {}).get("path") or f"{SB_DIR}/cache.db"
    except Exception:
        return f"{SB_DIR}/cache.db"

def bj_cron_lines(hh, mm, cmd, day=None):
    """北京时间 hh:mm（day 给了 = 每月那一号）→ /etc/cron.d 里的行。

    为什么不能只换算一次：Debian/Ubuntu 的 cron 不认 CRON_TZ，只能按本机时区写；而美国、欧洲、
    澳洲这些地方有夏令时，同一个北京时刻在本机冬天和夏天差一小时（洛杉矶冬 11:00 / 夏 12:00），
    只按装的那天算，换季后就整整偏一小时，还可能撞上别的凌晨任务。
    所以按今年 1 月和 7 月各算一次，两个本机时刻都写；每行前面用北京时间再核对一次
    （TZ=CST-8 是 POSIX 写法，不依赖 tzdata），只有对的那行真跑。没有夏令时的时区只有一行。
    每月任务：本机日期可能跟北京差一天，所以日期字段写 *、改由核对里的「几号」把关。"""
    import datetime
    bj = datetime.timezone(datetime.timedelta(hours=8))
    y = datetime.date.today().year
    times = []
    for mon in (1, 7):
        t = datetime.datetime(y, mon, 15, hh, mm, tzinfo=bj).astimezone()
        if (t.hour, t.minute) not in times:
            times.append((t.hour, t.minute))
    fmt, want = ("\\%d\\%H", f"{day:02d}{hh:02d}") if day else ("\\%H", f"{hh:02d}")
    guard = f'[ "$(TZ=CST-8 date +{fmt})" = "{want}" ] && '
    return [f"{m} {h} * * * root {guard}{cmd}" for h, m in times], times


# ── 凌晨任务排队（bgpeer-jobq）────────────────────────────────────────────────
# 仓库主人：「如果有影响就在后面排队，如果要等服务器重启了排队的就自动跟上进行继续做」。
# 凌晨这几条（cn-block 刷新会重启 sing-box、nginx 升级会重启 nginx、内核更新会重启 sing-box/xray、
# 三大配置自动更新）一律先交给 bgpeer-jobq：一个跑完再跑下一个，按登记顺序；记录落硬盘，
# 重启打断的、没轮到的，开机由 bgpeer-jobq.service 接着跑。xy-installer / cn-block / net-optimize /
# media-stack 各带一份【一模一样】的（谁先装谁写，内容相同就不重写；测试会核对四份一致）。
JOBQ_BIN  = "/usr/local/sbin/bgpeer-jobq"
JOBQ_UNIT = "/etc/systemd/system/bgpeer-jobq.service"
JOBQ_SH = r"""#!/bin/bash
# bgpeer-jobq —— 凌晨定时任务排队器（nodekit 生成，四个脚本各带一份同样的，勿手改）
#   bgpeer-jobq run <任务名> '<命令>'   登记 → 排队 → 前面的跑完再跑（同名任务已在排/在跑就不重复登记）
#   bgpeer-jobq resume                  开机时调：上次重启打断的、还没轮到的，按原先顺序接着跑
#   bgpeer-jobq list                    看队列
# 记录落在硬盘（不是 /run），断电重启也丢不了；登记超过 12 小时还没跑上的丢掉，免得白天冒出凌晨的活。
Q=${BGPEER_JOBQ_DIR:-/var/lib/bgpeer-jobq}
LOCK=${BGPEER_JOBQ_LOCK:-/run/bgpeer-jobq.lock}
LOG=${BGPEER_JOBQ_LOG:-/var/log/bgpeer-jobq.log}
MAX_AGE=${BGPEER_JOBQ_MAX_AGE:-43200}
JOB_TIMEOUT=${BGPEER_JOBQ_TIMEOUT:-14400}   # 4 小时：Emby 每日对齐自己就给了 3 小时
BOOT_WAIT=${BGPEER_JOBQ_BOOT_WAIT:-60}
mkdir -p "$Q" && chmod 700 "$Q"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

queued() {   # 同名任务是否已在排 / 在跑
  local f
  for f in "$Q"/*.job "$Q"/*.run; do
    [ -e "$f" ] && [ "$(head -n1 "$f")" = "$1" ] && return 0
  done
  return 1
}

drain() {    # 拿到锁的那个把整条队按登记顺序跑完；没拿到的等着（= 排队）
  exec 9>"$LOCK"
  flock 9
  local f id cmd t0 rc
  while :; do
    f=$(ls -1 "$Q"/*.job 2>/dev/null | sort | head -n1)
    [ -n "$f" ] || break
    id=$(sed -n 1p "$f"); cmd=$(sed -n 2p "$f")
    if [ $(( $(date +%s) - $(stat -c %Y "$f") )) -gt "$MAX_AGE" ]; then
      log "$id 登记超过 $((MAX_AGE / 3600)) 小时没跑上，丢弃"; rm -f "$f"; continue
    fi
    mv "$f" "${f%.job}.run"                 # 正在跑：这时重启，开机会接着跑它
    t0=$(date +%s); log "$id 开始"
    timeout "$JOB_TIMEOUT" bash -c "$cmd" 9>&-   # 9>&-：锁别漏给任务起的进程，不然锁永远放不掉
    rc=$?
    rm -f "${f%.job}.run"; log "$id 结束 rc=$rc 用时 $(( $(date +%s) - t0 ))s"
  done
  exec 9>&-
}

case "$1" in
  run)
    [ -n "$2" ] && [ -n "$3" ] || { echo "用法: $0 run <任务名> '<命令>'" >&2; exit 2; }
    if queued "$2"; then log "$2 已在队列里，不重复登记"; exit 0; fi
    tmp=$(mktemp "$Q/.new.XXXXXX")
    printf '%s\n%s\n' "$2" "$3" > "$tmp"
    mv "$tmp" "$Q/$(date +%s%N)-$2.job"
    if ! flock -n "$LOCK" true 2>/dev/null; then log "$2 排队：前面还有任务在跑"; fi
    drain
    ;;
  resume)
    n=0
    for f in "$Q"/*.run; do [ -e "$f" ] && mv "$f" "${f%.run}.job" && n=$((n + 1)); done
    m=$(ls -1 "$Q"/*.job 2>/dev/null | wc -l)
    [ "$m" -gt 0 ] || exit 0
    log "开机续跑：被重启打断 $n 个，共 $m 个待跑；等 ${BOOT_WAIT}s 让网络和服务先起来"
    sleep "$BOOT_WAIT"
    drain
    ;;
  list)
    for f in "$Q"/*.job "$Q"/*.run; do
      [ -e "$f" ] && echo "$(basename "$f")  $(sed -n 2p "$f")"
    done
    ;;
  *) echo "用法: $0 run <任务名> '<命令>' | resume | list" >&2; exit 2 ;;
esac
"""
JOBQ_UNIT_TXT = ("[Unit]\nDescription=bgpeer 凌晨任务排队：开机接着跑被重启打断 / 还没轮到的\n"
                 "Wants=network-online.target\nAfter=network-online.target sing-box.service xray.service nginx.service\n"
                 "[Service]\nType=simple\nExecStart=" + JOBQ_BIN + " resume\n"
                 "[Install]\nWantedBy=multi-user.target\n")


def ensure_jobq():
    """装 / 更新排队器和开机续跑服务。内容没变什么都不做。装不上返回 False（调用方退回直接跑）。"""
    try:
        changed = False
        for path, txt, mode in ((JOBQ_BIN, JOBQ_SH, 0o755), (JOBQ_UNIT, JOBQ_UNIT_TXT, 0o644)):
            try:
                same = open(path).read() == txt
            except OSError:
                same = False
            if not same:
                # 写临时文件再换名：排队器可能正在跑（bash 是边跑边读脚本的），原地改写会把它读花
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".new", "w") as f:
                    f.write(txt)
                os.chmod(path + ".new", mode)
                os.replace(path + ".new", path)
                changed = True
            os.chmod(path, mode)
        if changed:
            subprocess.run("systemctl daemon-reload; systemctl enable bgpeer-jobq.service",
                           shell=True, capture_output=True)
        return True
    except OSError:
        return False


def jobq_wrap(job, cmd):
    """把一条命令包成「交给排队器」：排队器不在（被别的卸载删了）就直接跑，至少不丢任务。"""
    import shlex
    return f"if [ -x {JOBQ_BIN} ]; then {JOBQ_BIN} run {job} {shlex.quote(cmd)}; else {cmd}; fi"


def bj_job_lines(hh, mm, job, cmd, day=None):
    """北京时间定时任务的 cron 行：时间换算 + 到点核对（见 bj_cron_lines）+ 交给排队器。"""
    ensure_jobq()
    return bj_cron_lines(hh, mm, jobq_wrap(job, cmd), day=day)


def jobq_cleanup_if_unused():
    """卸载时调：/etc/cron.d 里已经没有任务用排队器了，才把它和开机服务、队列一起撤掉。
       （网络优化是独立模块，卸代理主体时它的 nginx 月度升级还在用排队器，那就留着。）"""
    try:
        for n in os.listdir("/etc/cron.d"):
            try:
                if JOBQ_BIN in open(os.path.join("/etc/cron.d", n)).read():
                    return False
            except OSError:
                pass
    except OSError:
        pass
    subprocess.run("systemctl disable bgpeer-jobq.service", shell=True, capture_output=True)
    for p in (JOBQ_BIN, JOBQ_UNIT):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree("/var/lib/bgpeer-jobq", ignore_errors=True)
    try:
        os.remove("/var/log/bgpeer-jobq.log")
    except OSError:
        pass
    subprocess.run("systemctl daemon-reload", shell=True, capture_output=True)
    return True


def setup_cron():
    """装每日定点刷新的 cron：北京时间 03:00（时区 / 夏令时换算见 bj_cron_lines）。幂等。"""
    try:
        if os.path.abspath(__file__) != SELF_PATH:      # 确保 cron 调的本地副本存在
            os.makedirs(BGP_DIR, exist_ok=True)
            import shutil; shutil.copy(os.path.abspath(__file__), SELF_PATH)
        lines, times = bj_job_lines(3, 0, "cnblock", f"python3 {SELF_PATH} refresh >> {CRON_LOG} 2>&1")
        txt = (f"# bgpeer 屏蔽规则集每日刷新（北京时间 03:00 = 本机 "
               f"{' / '.join(f'{h:02d}:{m:02d}' for h, m in times)}，到点核对北京钟点，进排队器）\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               + "".join(l + "\n" for l in lines))
        try:
            if open(CRON_FILE).read() == txt:
                return
        except OSError:
            pass
        open(CRON_FILE, "w").write(txt); os.chmod(CRON_FILE, 0o644)
    except OSError as e:
        print("  安装定时任务失败（不影响屏蔽，仅少了每日刷新）:", e)

def remove_cron():
    try: os.remove(CRON_FILE)
    except OSError: pass

def _cnblk_remote_sets():
    """配置里本脚本注入的远程规则集 {tag: url}（sing-box 拉的就是这几个地址）。"""
    try:
        conf = json.load(open(f"{SB_DIR}/config.json"))
    except Exception:
        return {}
    return {r["tag"]: r["url"] for r in (conf.get("route") or {}).get("rule_set", [])
            if str(r.get("tag", "")).startswith("cnblk-") and r.get("type") == "remote" and r.get("url")}

def _rs_hashes(sets):
    """把这几个规则集按 sing-box 用的同一个地址拉一遍，算 sha256。任何一个拉不下来返回 None。"""
    import hashlib
    out = {}
    for tag, url in sorted(sets.items()):
        data = None
        for rd in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
                data = urllib.request.urlopen(req, timeout=30).read()
                break
            except Exception:
                time.sleep(2 * (rd + 1))
        if not data:
            return None
        out[tag] = hashlib.sha256(data).hexdigest()
    return out

def refresh():
    """定点刷新：规则集【有变化】才清 sing-box 规则集缓存并重启，强制重新拉取远程 srs；
       起不来就回滚缓存，绝不因刷新把节点搞挂。cron 调用。

       【没变化不重启】仓库主人：「03:00 那个改成规则有变化才重启吧」。以前每晚都重启一次 sing-box，
       凌晨挂着的连接都断几秒；而 geoip/cn 一个月只变十来天、域名那份更少。先按 sing-box 用的同一批地址
       拉一遍算指纹，跟上次成功刷新时记的比：一样就什么都不做；拉不下来也不重启（sing-box 自己每 24 小时
       也会更新这几个规则集，不会因为这一晚没刷就过期）。第一次（还没记过指纹）照旧刷一次。"""
    if not cnblock_load().get("enabled"):
        return
    setup_cron()                                        # 顺手对齐 cron（老装机按装的那天换算、改过时区）
    sets = _cnblk_remote_sets()
    new = _rs_hashes(sets) if sets else None
    if new is None:
        print(time.strftime("%F %T"), "规则集拉不下来，这次不重启（sing-box 自己每 24 小时也会更新）")
        return
    if cnblock_load().get("rs_hash") == new:
        print(time.strftime("%F %T"), "规则集没有变化，不重启 sing-box")
        return
    cache = _cache_path(); bak = cache + ".bak"
    if os.path.exists(cache):
        try: os.replace(cache, bak)
        except OSError: bak = None
    else:
        bak = None
    sh("systemctl restart sing-box", check=False)
    active = False
    for _ in range(15):
        time.sleep(1)
        if sh("systemctl is-active sing-box", check=False) == "active":
            active = True; break
    if not active:                                      # 起不来 → 有旧缓存就回滚，无论如何要报出来
        if bak:
            os.replace(bak, cache)
            sh("systemctl restart sing-box", check=False)
            print(time.strftime("%F %T"), "刷新后 sing-box 未启动，已回滚缓存")
        else:
            print(time.strftime("%F %T"), "刷新后 sing-box 未启动（无缓存可回滚），请检查 systemctl status sing-box")
        return
    if bak and os.path.exists(bak):
        try: os.remove(bak)
        except OSError: pass
    cfg = cnblock_load(); cfg["rs_hash"] = new; cnblock_save(cfg)   # 起来了才记，没起来下次还会再试
    print(time.strftime("%F %T"), "规则集有变化，已刷新（sing-box 重启一次）")

def update_now():
    """立即更新：重新拉取最新放行名单 + 规则集并即时生效，不必等每天 03:00 的定时刷新。
       覆盖两种改动——① 改了放行名单(作者名单随最新 cn-block.py、自定义名单从链接实时拉)
       → 重新注入；② 改了 rules 仓库里的规则集数据(.srs) → 清 sing-box 缓存强制重拉。
       沿用 apply 的校验/回滚：失败则退回原本能用的状态，绝不把节点搞挂。"""
    cfg = cnblock_load()
    if not cfg.get("enabled"):
        print("  屏蔽还没开启——先选 1 开启，开启时本来就是按最新名单注入的。")
        return
    # 备份并清掉规则集缓存，逼 sing-box 重启时重新拉最新 srs（拉不到可回滚，不影响节点）
    cache = _cache_path(); baks = []
    for p in (cache, cache + "-wal", cache + "-shm"):
        if os.path.exists(p):
            try: os.replace(p, p + ".bak"); baks.append(p)
            except OSError: pass
    print("  重新拉取最新放行名单 + 规则集…")
    ok = apply_cn_block(cfg)                              # 重读名单 + 重注入 + 重启（自带校验/回滚）
    for p in baks:                                        # 成功→丢弃旧缓存备份；失败→还原，保住原本能用的缓存
        try:
            if ok: os.remove(p + ".bak")
            else:  os.replace(p + ".bak", p)
        except OSError:
            pass
    if ok:
        print("  ✓ 已按最新放行名单 + 规则集刷新生效。")
    else:
        print("  更新未生效（多半规则集临时拉不到），已保持原状，稍后再试。")

def custom_allow_menu():
    """单条放行（域名/IP）的增删查。域名按后缀匹配，含子域；IP 支持单个或 CIDR、v4/v6。"""
    R, G, Y, N = "\033[1;31m", "\033[1;32m", "\033[1;33m", "\033[0m"
    while True:
        cfg = cnblock_load()
        sd, si = _script_custom()                        # 脚本写死的：菜单里删不掉
        doms = list(cfg.get("wl_domains") or [])         # 本机加的：可增删
        ips = list(cfg.get("wl_ips") or [])
        items = [("域名", d) for d in doms] + [("IP", i) for i in ips]
        # 命中即直连，不被 CN 屏蔽拦下。脚本内置的要改仓库里的 cn-block.py 才能动，这里删不掉。
        ui_title("单条放行（域名 / IP）")
        if sd or si:
            print(_hl("1;33", "▸ 脚本内置（这里删不掉）"))
            for v in sd: print(f"  [域名] {v}")
            for v in si: print(f"  [IP]   {v}")
            print()
        print(_hl("1;33", f"▸ 本机添加（{len(items)} 条）"))
        for n, (kind, v) in enumerate(items, 1):
            print(_hl("1;36", f"[{n}] ") + f"[{kind}] " + _hl("94", v))
        if not items:
            print(_hl("2", "（还没添加）"))
        ui_line()
        ui_items([(1, "添加（可一次多个，逗号分隔）", None), (2, "删除", None), (0, "返回", None)])
        ui_tip("域名按后缀匹配：填 example.com，它和所有子域都放行")
        c = _ask("选择: ").strip()
        # 直接在这里贴域名/IP 也认。上面写着「1 添加（可一次多个，逗号分隔）」，
        # 紧接着就是「选择」，很容易让人以为在这儿直接输 —— 实际使用中就是这么
        # 填了一次、看到「还没添加」以为功能坏了。与其让他白填，不如认下来。
        pre = ""
        if c not in ("", "0", "1", "2") and re.search(r"[.:]", c):
            pre, c = c, "1"
        if c == "1":
            s = pre or _ask("  输入域名或IP(可逗号分隔，如 baidu.com,1.2.3.4,10.0.0.0/8): ").strip()
            if not s:
                continue
            added = 0
            for raw in s.replace("，", ",").split(","):
                raw = raw.strip()
                if not raw:
                    continue
                kind, val = parse_wl_entry(raw)
                if not kind:
                    print(f"    {R}跳过 {raw!r}{N}：{val}"); continue
                if val in (sd if kind == "domain" else si):
                    print(f"    脚本内置里已有，跳过：{val}"); continue
                key = "wl_domains" if kind == "domain" else "wl_ips"
                lst = list(cfg.get(key) or [])
                if val in lst:
                    print(f"    已存在，跳过：{val}"); continue
                lst.append(val); cfg[key] = lst; added += 1
                print(f"    {G}已添加{N} [{'域名' if kind=='domain' else 'IP'}] {val}")
            if added:
                cnblock_save(cfg)
                if cfg.get("enabled"):
                    apply_cn_block(cfg)                  # 已开启则立即生效
                else:
                    print("    （当前未开启屏蔽，已保存；开启时会自动带上）")
        elif c == "2":
            if not items:
                continue
            s = _ask("  删除哪几条(逗号分隔如 1,3；a=全部；回车取消): ").strip().lower()
            if not s:
                continue
            if s in ("a", "all"):
                idxs = list(range(len(items)))
            else:
                idxs = []
                for p in s.replace("，", ",").split(","):
                    p = p.strip()
                    if p.isdigit() and 1 <= int(p) <= len(items):
                        idxs.append(int(p) - 1)
                    elif p:
                        print(f"    {R}忽略无效序号 {p!r}{N}")
                if not idxs:
                    continue
            gone = [items[i] for i in sorted(set(idxs))]
            cfg["wl_domains"] = [d for d in doms if ("域名", d) not in gone]
            cfg["wl_ips"] = [i for i in ips if ("IP", i) not in gone]
            cnblock_save(cfg)
            for kind, v in gone:
                print(f"    已删除 [{kind}] {v}")
            if cfg.get("enabled"):
                apply_cn_block(cfg)
        elif c in ("0", ""):
            return

def menu():
    while True:
        cfg = cnblock_load()
        on = cfg.get("enabled")
        wl = {"author": "作者名单", "custom": "自定义名单", "none": "不放行"}.get(cfg.get("wl_mode", "author"), "作者名单")
        _d, _i = _wl_custom(cfg)
        # 3 自定义放行名单脚本链接：规则集 + 单条域名/IP，可抄样板改；5 立即更新：不用等每天 03:00 定时刷新
        ui_title("屏蔽中国域名和 IP")
        ui_url("自定义放行名单链接", cfg.get("wl_url") or "", "94")
        ui_line()
        ui_items([(1, "屏蔽中国域名和 IP", _hl("1;32", "已开（再选可关闭）") if on else "未开"),
                  (2, "放行白名单", wl),
                  (3, "自定义放行名单链接", None),
                  (4, "单条放行域名 / IP", f"{len(_d)} 个域名 + {len(_i)} 个 IP" if (_d or _i) else None),
                  (5, "立即更新（拉最新名单 / 规则集）", None),
                  (6, "卸载（清掉屏蔽规则）", None),
                  (0, "退出", None)])
        c = _ask("选择: ").strip()
        if c == "1":
            if on:
                if _ask("  已开启，关闭屏蔽? [y/N]: ").lower() in ("y", "yes"):
                    remove_cn_block()
            else:
                apply_cn_block(cfg)
        elif c == "2":
            print()
            ui_items([(1, "作者名单", None), (2, "自定义名单", None), (0, "返回", None)])
            s = _ask("选择: ").strip()
            if s == "1":   cfg["wl_mode"] = "author"
            elif s == "2": cfg["wl_mode"] = "custom"
            else:          continue
            cnblock_save(cfg)
            print("    已设为", "作者名单" if cfg["wl_mode"] == "author" else "自定义名单")
            if cfg.get("enabled"): apply_cn_block(cfg)    # 已开启则立即用新名单重注入
        elif c == "3":
            cur = (cfg.get("wl_url") or "").strip()
            if cur:                                      # 加过了：先显示当前链接，问要不要换
                print(f"  已添加过自定义放行名单链接：{cur}")
                if _ask("  是否更换? [y/N]: ").lower() not in ("y", "yes"):
                    continue                             # n 返回菜单，不动原链接
            print("  样板可直接抄走改：https://github.com/bgpeer/nodekit/blob/main/whitelist-template.py")
            print("  支持 WHITELIST_TAGS / ALLOW_DOMAINS / ALLOW_IPS 三个列表；也兼容纯文本 tag 列表、whitelist-inject.sh")
            url0 = _ask("  自定义放行名单链接(GitHub 文件页 / raw / gist 都行): ").strip()
            url = raw_link(url0)
            if url:
                cfg["wl_url"] = url; cfg["wl_mode"] = "custom"; cnblock_save(cfg)
                if url != url0:
                    print(f"  网页链接已换成原始文件链接：{url}")
                print("  已保存，并切到自定义名单。")
                if cfg.get("enabled"): apply_cn_block(cfg)
        elif c == "4":
            custom_allow_menu()
        elif c == "5":
            update_now()
        elif c == "6":
            remove_cn_block()
        elif c in ("0", ""):
            return

def main():
    act = sys.argv[1] if len(sys.argv) > 1 else ""
    if act == "apply":                                   # 主脚本重装后调用：仅在已开启时重注入
        if cnblock_load().get("enabled"):
            apply_cn_block()
    elif act == "refresh":                               # cron 每日定点调用：刷新规则集
        refresh()
    elif act == "remove":
        remove_cn_block()
    else:
        menu()

if __name__ == "__main__":
    main()
