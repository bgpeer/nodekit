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
import os, re, sys, json, time, subprocess, urllib.request

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
def cnblock_save(d):
    os.makedirs(BGP_DIR, exist_ok=True)
    json.dump(d, open(CNBLOCK_FILE, "w"), ensure_ascii=False, indent=2)

def _http_code(url):
    return sh(f'curl -s -o /dev/null -w "%{{http_code}}" --max-time 15 {url}', check=False).strip()

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

def _whitelist_tags(cfg):
    """取白名单 tag 列表：作者名单 / 自定义名单。
       自定义链接可为纯文本（每行一个 tag，# 注释跳过），
       也可直接指向 whitelist-inject.sh —— 自动抽取其中 WHITELIST_TAGS=(...) 数组。"""
    mode = cfg.get("wl_mode", "author")
    if mode == "none":
        return []
    if mode == "custom":
        url = (cfg.get("wl_url") or "").strip()
        if not url:
            print("  未设置自定义放行名单链接，改用作者名单。"); return list(CN_WHITELIST)
        try:
            txt = fetch_url(url)
        except Exception as e:
            print("  拉取自定义名单失败，改用作者名单:", e); return list(CN_WHITELIST)
        m = re.search(r"WHITELIST_TAGS=\(([^)]*)\)", txt, re.S)
        if m:                                            # 直接喂 whitelist-inject.sh：抽数组
            return re.findall(r'[A-Za-z0-9!_.\-]+', m.group(1))
        tags = []                                        # 否则按纯文本：每行一个 tag
        for ln in txt.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                tags.append(ln.split()[0])
        return tags
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

    # 规则顺序：白名单放行（在前，命中即直连不被拦）→ CN 域名拦 → CN IP 拦 → 原有其它规则
    inj = []
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
    json.dump(conf, open(sb_cfg, "w"), ensure_ascii=False, indent=2)

    r = subprocess.run(f"{SB_BIN} check -c {sb_cfg}", shell=True, text=True, capture_output=True)
    if r.returncode:
        json.dump(backup, open(sb_cfg, "w"), ensure_ascii=False, indent=2)   # 回滚
        print("注入后配置校验失败，已回滚未生效：\n" + (r.stderr or r.stdout).strip()); return False
    sh("systemctl restart sing-box", check=False)
    # 确认真的起来了；万一注入后起不来（比如规则集这会儿全拉不到），回滚到屏蔽前配置，
    # 绝不影响原本能用的节点
    active = False
    for _ in range(10):
        time.sleep(1)
        if sh("systemctl is-active sing-box", check=False) == "active":
            active = True; break
    if not active:
        json.dump(backup, open(sb_cfg, "w"), ensure_ascii=False, indent=2)
        sh("systemctl restart sing-box", check=False)
        print("注入后 sing-box 未能启动，已回滚到屏蔽前配置（节点照常可用）。可能是规则集暂时全拉不到，稍后再试。")
        return False
    cfg["enabled"] = True; cnblock_save(cfg)
    setup_cron()                                        # 每天北京 03:00 定点刷新规则集
    print(f"\n✓ 已开启屏蔽中国域名/IP：放行白名单 {len(wl_refs)} 组，其余 CN 域名+IP 一律拦截。")
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
            if not route.get("rule_set") and not route.get("rules"):
                conf.pop("route", None)
            else:
                conf["route"] = route
            json.dump(conf, open(sb_cfg, "w"), ensure_ascii=False, indent=2)
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
    """装每日定点刷新的 cron：北京时间 03:00 = UTC 19:00。"""
    try:
        if os.path.abspath(__file__) != SELF_PATH:      # 确保 cron 调的本地副本存在
            os.makedirs(BGP_DIR, exist_ok=True)
            import shutil; shutil.copy(os.path.abspath(__file__), SELF_PATH)
        txt = ("# bgpeer 屏蔽规则集每日刷新（北京时间 03:00 = UTC 19:00）\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               "CRON_TZ=UTC\n"
               f"0 19 * * * root python3 {SELF_PATH} refresh >> {CRON_LOG} 2>&1\n")
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
    if not active and bak:                              # 起不来 → 回滚旧缓存
        os.replace(bak, cache)
        sh("systemctl restart sing-box", check=False)
        print(time.strftime("%F %T"), "刷新后 sing-box 未启动，已回滚缓存"); return
    if bak and os.path.exists(bak):
        try: os.remove(bak)
        except OSError: pass
    print(time.strftime("%F %T"), "规则集已刷新")

def menu():
    while True:
        cfg = cnblock_load()
        on = cfg.get("enabled")
        wl = {"author": "作者名单", "custom": "自定义名单", "none": "不放行"}.get(cfg.get("wl_mode", "author"), "作者名单")
        print("\n" + "=" * 60 + "\n屏蔽中国域名和IP\n" + "=" * 60)
        print(f"  当前状态: {'已开启 ✓' if on else '未开启'}    放行白名单: {wl}")
        print(f"  自定义放行名单链接: {cfg.get('wl_url') or '(未设置)'}")
        print("-" * 60)
        print("  1 屏蔽中国域名和IP" + ("（已开，再选可关闭）" if on else ""))
        print("  2 放行白名单（作者名单 / 自定义名单）")
        print("  3 自定义放行名单脚本链接")
        print("  4 卸载（不想屏蔽了，直接清掉规则）")
        print("  5 退出")
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
            url = _ask("  自定义放行名单链接(纯文本 tag 列表，或直接指向 whitelist-inject.sh): ").strip()
            if url:
                cfg["wl_url"] = url; cfg["wl_mode"] = "custom"; cnblock_save(cfg)
                print("  已保存，并切到自定义名单。")
                if cfg.get("enabled"): apply_cn_block(cfg)
        elif c == "4":
            remove_cn_block()
        elif c in ("5", "0", ""):
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
