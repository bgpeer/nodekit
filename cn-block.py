#!/usr/bin/env python3
# cn-block.py —— 屏蔽中国域名/IP（sing-box 服务端路由）+ 白名单放行
# 独立文件，方便单独维护；nodekit 主脚本(xy-installer.py)通过子进程调用：
#   python3 cn-block.py            交互菜单
#   python3 cn-block.py apply      按已存状态重新注入（未开启则直接跳过）——重装后自动调用
#   python3 cn-block.py refresh    刷新规则集缓存并重启（cron 每天北京 03:00 调用）
#   python3 cn-block.py remove     卸载屏蔽规则
#
# 规则集用 sing-box 远程 srs（.srs binary），并挂 cron 每天北京时间 03:00 定点刷新：
#   CN 域名 geosite/geolocation-cn.srs、CN IP geoip/cn.srs → reject
#   白名单（作者名单对齐 vps-net/whitelist-inject.sh 的 WHITELIST_TAGS）→ 命中直连放行
import os, re, ast, sys, json, time, ipaddress, subprocess, urllib.request, urllib.error

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

def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()

def _ask(prompt=""):
    """交互输入：优先读 /dev/tty，使 curl|python3 管道下仍可交互。"""
    try:
        with open("/dev/tty", "r") as t:
            print(prompt, end="", flush=True)
            line = t.readline()
            if line == "":
                raise EOFError
            return line.rstrip("\n").strip()
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
                req = urllib.request.Request(u, headers={"User-Agent": "xy-installer"})
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
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "xy-installer"})
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

def _link_data(cfg):
    """拉取并解析自定义链接，返回校验后的 (tags, domains, ips)。失败返回 None。"""
    url = (cfg.get("wl_url") or "").strip()
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

def setup_cron():
    """装每日定点刷新的 cron：北京时间 03:00 = UTC 19:00。
       Debian/Ubuntu 默认 cron 不支持 CRON_TZ（那是 cronie 的特性），
       所以按服务器当前时区把 UTC 19:00 换算成本地时刻写入。"""
    try:
        if os.path.abspath(__file__) != SELF_PATH:      # 确保 cron 调的本地副本存在
            os.makedirs(BGP_DIR, exist_ok=True)
            import shutil; shutil.copy(os.path.abspath(__file__), SELF_PATH)
        import datetime
        local = (datetime.datetime.now(datetime.timezone.utc)
                 .replace(hour=19, minute=0, second=0, microsecond=0).astimezone())
        txt = (f"# bgpeer 屏蔽规则集每日刷新（北京时间 03:00 = UTC 19:00 = 本机 {local:%H:%M}）\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               f"{local.minute} {local.hour} * * * root python3 {SELF_PATH} refresh >> {CRON_LOG} 2>&1\n")
        open(CRON_FILE, "w").write(txt); os.chmod(CRON_FILE, 0o644)
    except OSError as e:
        print("  安装定时任务失败（不影响屏蔽，仅少了每日刷新）:", e)

def remove_cron():
    try: os.remove(CRON_FILE)
    except OSError: pass

def refresh():
    """定点刷新：清 sing-box 规则集缓存后重启，强制重新拉取远程 srs；
       起不来就回滚缓存，绝不因刷新把节点搞挂。cron 调用。"""
    if not cnblock_load().get("enabled"):
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
    print(time.strftime("%F %T"), "规则集已刷新")

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
        print("\n" + "-" * 60)
        print("  单条放行（域名 / IP）—— 命中即直连，不被 CN 屏蔽拦下")
        print("-" * 60)
        if sd or si:
            print("  脚本内置（改仓库里的 cn-block.py 才能动，这里删不掉）:")
            for v in sd: print(f"      [域名] {v}")
            for v in si: print(f"      [IP]   {v}")
            print()
        print("  本机添加:")
        if items:
            for n, (kind, v) in enumerate(items, 1):
                print(f"    {n:>2}. [{kind}] {v}")
        else:
            print("    (还没添加)")
        print(f"\n  {Y}域名按后缀匹配{N}：填 example.com，它和它的所有子域都放行")
        print("  1 添加（可一次多个，逗号分隔）   2 删除   0 返回")
        c = _ask("  选择: ").strip()
        if c == "1":
            s = _ask("  输入域名或IP(可逗号分隔，如 baidu.com,1.2.3.4,10.0.0.0/8): ").strip()
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
        print("\n" + "=" * 60 + "\n屏蔽中国域名和IP\n" + "=" * 60)
        print(f"  当前状态: {'已开启 ✓' if on else '未开启'}    放行白名单: {wl}")
        print(f"  自定义放行名单链接: {cfg.get('wl_url') or '(未设置)'}")
        _d, _i = _wl_custom(cfg)
        print(f"  单条放行: {len(_d)} 个域名 + {len(_i)} 个 IP" if (_d or _i) else "  单条放行: (未添加)")
        print("-" * 60)
        print("  1 屏蔽中国域名和IP" + ("（已开，再选可关闭）" if on else ""))
        print("  2 放行白名单（作者名单 / 自定义名单）")
        print("  3 自定义放行名单脚本链接（规则集 + 单条域名/IP，可抄样板改）")
        print("  4 单条放行域名/IP（自己加，按后缀匹配含子域）")
        print("  5 立即更新（拉取最新放行名单/规则集并生效，不用等每天定时刷新）")
        print("  6 卸载（不想屏蔽了，直接清掉规则）")
        print("  0 退出")
        c = _ask("选择: ").strip()
        if c == "1":
            if on:
                if _ask("  已开启，关闭屏蔽? [y/N]: ").lower() in ("y", "yes"):
                    remove_cn_block()
            else:
                apply_cn_block(cfg)
        elif c == "2":
            print("    1 作者名单   2 自定义名单   0 返回")
            s = _ask("    选择: ").strip()
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
            url = _ask("  自定义放行名单链接(必须是 raw 链接): ").strip()
            if url:
                cfg["wl_url"] = url; cfg["wl_mode"] = "custom"; cnblock_save(cfg)
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
