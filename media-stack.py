#!/usr/bin/env python3
# ============================================================================
#  media-stack.py · 自建 Emby（网盘直链媒体服务器）
#
#  Emby + OpenList + AutoFilm + MediaWarp，网盘文件生成 .strm 挂进 Emby，
#  播放时 MediaWarp 拦截并 302 重定向到网盘直链 —— 视频流不经过本机带宽。
#  本地只落地几 KB 的 strm 文本，小盘 VPS 也能开大媒体库。
#
#  与 xy-installer.py 的节点共存（这是本文件最重要的约束）：
#    · 只【读】节点的域名、SNI 内部端口、acme 证书状态
#    · 只【写】自己的文件：/etc/nginx/conf.d/media-stack.conf 和安装目录
#    · 绝不碰 nginx.conf、bgpeer.conf、bgpeer-stream.conf、state.json
#    · nginx 配置先 nginx -t，不过就还原并继续，绝不让节点因为本脚本挂掉
#
#  单独运行：sudo python3 media-stack.py          （进子菜单）
#  也可直接指定动作：install / info / update / uninstall
#      sudo python3 media-stack.py info
# ============================================================================
import base64
import glob
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SCRIPT_VERSION = "1.1.0"

# 本脚本在仓库里的地址，「更新」时用它把自己换成最新版
SELF_URL = "https://raw.githubusercontent.com/bgpeer/nodekit/main/media-stack.py"

# 容器日志里的 ANSI 颜色码。解析日志前必须剥掉，否则字段名被转义序列包着，
# 正则一个都匹配不到（而粘贴出来的文本又是干净的，极难察觉）
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# ---- 节点（xy-installer.py）的落盘状态，只读 ----
BGP_DIR    = "/etc/bgpeer"
STATE_FILE = BGP_DIR + "/state.json"        # 含 domain / sni_split 等
MS_STATE   = BGP_DIR + "/media-stack.json"  # 本脚本自己的：记住装在哪个目录
ACME_CRT   = "/etc/ssl/sb/acme.crt"         # 节点的 acme 证书（判断有没有真证书）

# ---- 本脚本自己的东西 ----
DEFAULT_DIR   = "/opt/media-stack"
NGX_SITE      = "/etc/nginx/conf.d/media-stack.conf"
HTPASSWD_FILE = "/etc/nginx/.media-stack.htpasswd"
# 媒体服务单独一份访问日志，「链路体检」靠它统计公网访问；
# 和节点的日志混在一起就分不出是谁在敲哪个服务了
NGX_ACCESS_LOG = "/var/log/nginx/media-stack.access.log"
CLI_PATH      = "/usr/local/bin/media-stack"
CLI_ALIAS     = "/usr/local/bin/emby"
SNI_HTTPS_PORT_FALLBACK = 8443              # 和 xy-installer.py 的常量一致

# AutoFilm 调度器的时区，以及默认的 strm 生成时刻（该时区下的 05:15）。
# 钉在北京时间：网盘在国内，「凌晨闲时」是按北京时间定义的，跟 VPS 摆在哪无关。
AUTOFILM_TZ       = "Asia/Shanghai"
DEFAULT_STRM_CRON = "0 15 5 * * *"          # 秒 分 时 日 月 周 —— 北京时间 05:15
OLD_STRM_CRON     = "0 0 5 * * *"           # 旧默认值，更新时静默迁移到上面那个

OPENLIST_PORT  = 5244
MEDIAWARP_PORT = 9000
STRM_SUBDIR    = "cloud"
STRM_PATH      = "/data/strm/" + STRM_SUBDIR

# 服务子域名 → 本地端口。Emby 走 MediaWarp，不是 8096。
SUBDOMAINS = [
    ("home", 3000,           "homepage",   "首页入口"),
    ("emby", MEDIAWARP_PORT, "emby",       "Emby"),
    ("list", OPENLIST_PORT,  "openlist",   "OpenList"),
]

RST = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
CYAN = "\033[36m"


def info(m): print(f"{CYAN}>>>{RST} {m}", flush=True)
def ok(m):   print(f"{GREEN}✔{RST} {m}", flush=True)
def warn(m): print(f"{YELLOW}⚠{RST} {m}", flush=True)
def err(m):  print(f"{RED}✗{RST} {m}", file=sys.stderr, flush=True)
def hr():    print(f"{DIM}{'─' * 56}{RST}", flush=True)


# ============================================================================ 基础工具
def sh(cmd, check=False, timeout=None):
    """跑一条 shell 命令，返回 CompletedProcess。默认不因非 0 退出而抛异常 ——
       这个脚本里大量步骤是「尽力而为」，失败要能继续走到回滚/提示。"""
    return subprocess.run(cmd, shell=True, text=True, capture_output=True,
                          check=check, timeout=timeout)


def have(binary):
    return shutil.which(binary) is not None


def ask(prompt, default=""):
    """交互输入。优先读 /dev/tty，这样 curl|python3 管道下也能交互。"""
    hint = f" [{default}]" if default else ""
    try:
        with open("/dev/tty", "r") as t:
            print(f"{prompt}{hint}: ", end="", flush=True)
            line = t.readline()
            if line == "":
                raise EOFError
            v = line.rstrip("\n").strip()
    except (OSError, EOFError):
        try:
            v = input(f"{prompt}{hint}: ").strip()
        except EOFError:
            v = ""
    return v or default


def ask_yn(prompt, default=True):
    # 不把默认值当 ask 的 default 传进去 —— 那样会显示成
    # 「问题？ [Y/n] [Y]:」，同一个提示叠两遍，看着像要填别的东西。
    # 这里只给 [Y/n] 一个提示，空输入在下面自己兜。
    v = ask(f"{prompt} [{'Y/n' if default else 'y/N'}]")
    if not v:
        return default
    return v.lower().startswith("y")


def pad(s, width):
    """按终端显示宽度补空格。中文一个字占两列，直接用 len() 对齐会错位。"""
    w = sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)
    return s + " " * max(0, width - w)


def rand_pw(n=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def require_root():
    if os.geteuid() != 0:
        err("请用 root 运行：sudo python3 media-stack.py")
        sys.exit(1)


def public_ip():
    """取本机公网 IP，拿不到就退回内网地址，再不行给个占位值。
       只用于没有域名时拼 http://IP:端口 的展示链接。"""
    ip = sh("curl -fsS4 --max-time 5 https://api.ipify.org").stdout.strip()
    if ip:
        return ip
    parts = sh("hostname -I").stdout.split()
    return parts[0] if parts else "127.0.0.1"


def nginx_reload():
    """reload nginx。nginx -s reload 在用 systemd 管理时可能失败，兜一层。
       注意不能写成 sh(a) or sh(b) —— CompletedProcess 恒为真，短路不会生效。"""
    if sh("nginx -s reload").returncode != 0:
        sh("systemctl reload nginx")


# ============================================================================ 读节点状态（只读）
def node_state():
    """读 xy-installer.py 的 state.json。读不到就返回空 dict，不报错 ——
       本脚本要能在没装节点的干净机器上单独跑。"""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def ms_state():
    """读本脚本自己的状态文件；读不到就返回空 dict。"""
    try:
        with open(MS_STATE) as f:
            v = json.load(f)
            return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def save_ms_state(install_dir=None, **extra):
    """记住装在哪、以及那些从生成的配置里读不回来的选择（比如扫描路径是不是 auto）。

    合并写而不是整份覆盖：这个文件现在不止一个键了，某处只想更新其中一个的时候，
    整份覆盖会把别的键悄悄抹掉。
    """
    cur = ms_state()
    if install_dir:
        cur["install_dir"] = install_dir
    cur.update(extra)
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        with open(MS_STATE, "w") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def ms_install_dir():
    """取安装目录：先读自己的状态文件，没有就退回默认路径。"""
    try:
        with open(MS_STATE) as f:
            d = json.load(f).get("install_dir", "")
            if d:
                return d
    except Exception:
        pass
    return DEFAULT_DIR


def is_installed(install_dir=None):
    d = install_dir or ms_install_dir()
    return os.path.exists(os.path.join(d, "docker-compose.yml"))


def detect_nginx_https_port():
    """探测 nginx 内部 https 监听端口。

    节点开了 SNI 分流（--sni-split）时，443 上是 stream 模块按 SNI 不解密分流，
    真正的 https 站点绑在 127.0.0.1:8443。新站点必须监听同一个内部端口，
    才能被现有分流规则的 default 分支带进来。没开分流就是标准的 443。
    """
    r = sh("nginx -T 2>/dev/null")
    m = re.search(r"^\s*listen\s+127\.0\.0\.1:(\d+)\s+ssl", r.stdout or "", re.M)
    if m:
        return int(m.group(1))
    return 443


def nginx_worker_user():
    """nginx worker 跑在哪个用户下。

    auth_basic_user_file 是 worker 每次请求时读的，而 worker 不是 root
    （Debian 系 www-data、RHEL 系 nginx）。属主设成 root:root 会让 worker
    读不到，访问直接 500 —— 加了密码反而把服务打挂。
    """
    try:
        with open("/etc/nginx/nginx.conf") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] == "user":
                    return parts[1].rstrip(";")
    except OSError:
        pass
    for u in ("www-data", "nginx", "nobody"):
        if sh(f"id {u}").returncode == 0:
            return u
    return ""


def nginx_supports_http2_directive():
    """nginx 1.25.1 起 `listen ... http2` 被弃用，改用独立的 `http2 on;`。
       但 `http2 on;` 在旧版本上是未知指令、会让 nginx -t 直接失败，
       所以必须按版本二选一，不能一刀切。"""
    r = sh("nginx -v 2>&1")
    m = re.search(r"/(\d+)\.(\d+)\.(\d+)", (r.stdout or "") + (r.stderr or ""))
    if not m:
        return False
    return tuple(int(x) for x in m.groups()) >= (1, 25, 1)


def acme_bin():
    for p in (os.path.expanduser("~/.acme.sh/acme.sh"), "/root/.acme.sh/acme.sh"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def acme_has_cf_creds():
    """acme.sh 用过 Cloudflare DNS 验证的话，凭据会以 SAVED_CF_Token /
       SAVED_CF_Key 存进 account.conf。已经有就别再让用户去后台建一个。"""
    for c in (os.path.expanduser("~/.acme.sh/account.conf"),
              "/root/.acme.sh/account.conf"):
        try:
            with open(c) as f:
                if re.search(r"^SAVED_CF_(Token|Key)=.+", f.read(), re.M):
                    return True
        except OSError:
            continue
    return False


# ============================================================================ .env 读写
def read_env(path, key, fallback=""):
    """先读 path，没有就读 fallback（老版本把密码写在 .env 里，要能迁移过来）。"""
    for p in (path, fallback):
        if not p:
            continue
        try:
            with open(p) as f:
                for line in f:
                    if line.startswith(key + "="):
                        v = line.split("=", 1)[1].strip()
                        if v:
                            return v
        except OSError:
            continue
    return ""


def keep_or_new(path, key, n=16, fallback=""):
    """脚本是幂等的、也鼓励重跑（补 API Key、改配置），但重跑时把已经在用的
       密码冲掉很坑：用户手上记的、浏览器存的全失效，而且他未必意识到密码变了。"""
    return read_env(path, key, fallback) or rand_pw(n)


# ============================================================================ Docker
def ensure_docker():
    if have("docker"):
        v = sh("docker --version").stdout.strip()
        ok(f"Docker 已安装：{v}")
    else:
        warn("未检测到 Docker。")
        if not ask_yn("是否自动安装 Docker？", True):
            err("需要 Docker 才能继续。")
            sys.exit(1)
        info("正在安装 Docker（官方脚本）...")
        sh("curl -fsSL https://get.docker.com | sh", timeout=600)
        sh("systemctl enable --now docker")
        if not have("docker"):
            err("Docker 安装失败。")
            sys.exit(1)
        ok("Docker 安装完成")
    if sh("docker compose version").returncode != 0:
        err("缺少 docker compose 插件。请升级 Docker 或安装 compose 插件后重试。")
        sys.exit(1)
    ok("docker compose 可用")


# ============================================================================ 配置生成
def gen_compose(cfg):
    """生成 docker-compose.yml。

    反代模式下所有端口都收进 127.0.0.1：外面只能经 nginx 访问。
    Emby 的 8096 尤其重要 —— 直连它会绕过 MediaWarp 的 302 拦截，
    退化成服务器中转，带宽还是自己的。
    """
    bind = "127.0.0.1:" if cfg["has_domain"] else ""
    d = cfg["install_dir"]
    parts = ["# 由 media-stack.py 自动生成。可手动修改后 docker compose up -d 生效。",
             "name: mediastack", "services:"]

    parts.append(f"""  emby:
    image: emby/embyserver:latest
    container_name: emby
    restart: unless-stopped
    environment:
      - UID=${{PUID}}
      - GID=${{PGID}}
      - TZ=${{TZ}}
    volumes:
      - {d}/emby/config:/config
      - ${{DATA_ROOT}}:/data
    ports:
      - "127.0.0.1:8096:8096"
    networks: [mediastack]
""")

    parts.append(f"""  openlist:
    image: openlistteam/openlist:latest
    container_name: openlist
    restart: unless-stopped
    user: "${{PUID}}:${{PGID}}"
    environment:
      - UMASK=022
      - TZ=${{TZ}}
    volumes:
      - {d}/openlist/config:/opt/openlist/data
    ports:
      - "{bind}{OPENLIST_PORT}:5244"
    networks: [mediastack]

  autofilm:
    image: akimio/autofilm:latest
    container_name: autofilm
    restart: unless-stopped
    # AutoFilm 读不到 TZ 环境变量（启动日志里会打印「使用应用时区 timezone=UTC」），
    # 所以定时任务的时刻必须靠 --timezone 显式指定，否则 cron 里写的 05:15 会被当成
    # 05:15 UTC —— 对国内用户就是下午一点多，完全不是想要的"凌晨闲时"。
    # 钉死在北京时间而不是跟随服务器本地时区：网盘在国内，"闲时"是按北京时间定义的，
    # 跟这台 VPS 摆在日本还是美国没有关系。
    command: ["--timezone", "{AUTOFILM_TZ}"]
    environment:
      - TZ=${{TZ}}
    volumes:
      - {d}/autofilm/config:/config
      - {d}/autofilm/logs:/logs
      - ${{DATA_ROOT}}:/data
    depends_on: [openlist]
    networks: [mediastack]

  mediawarp:
    image: akimio/mediawarp:latest
    container_name: mediawarp
    restart: unless-stopped
    environment:
      - TZ=${{TZ}}
    volumes:
      - {d}/mediawarp/config:/config
      - {d}/mediawarp/logs:/logs
      - {d}/mediawarp/custom:/custom
    ports:
      - "{bind}{MEDIAWARP_PORT}:9000"
    depends_on: [emby]
    networks: [mediastack]
""")

    if cfg["homepage"]:
        parts.append(f"""  homepage:
    image: ghcr.io/gethomepage/homepage:latest
    container_name: homepage
    restart: unless-stopped
    environment:
      - PUID=${{PUID}}
      - PGID=${{PGID}}
      - TZ=${{TZ}}
      - HOMEPAGE_ALLOWED_HOSTS=*
    volumes:
      - {d}/homepage/config:/app/config
    ports:
      - "{bind}3000:3000"
    networks: [mediastack]
""")

    if cfg.get("metatube"):
        # 【不映射端口】：只有同一个 docker 网络里的 Emby 需要访问它，nginx 也不
        # 给它建站点。所以不设 -token —— 在这个拓扑下加了也拦不住任何人，代价却是
        # 用户要把一串随机字符抄进 Emby 的插件设置页。真要对外开放时再补。
        #
        # -dsn 要的是【纯文件路径】，不是 URL。实测：
        #   /data/metatube.db            ✔ 建表成功
        #   sqlite:///data/metatube.db   ✖ unable to open database file
        # 不带 -dsn 也能跑，但数据不落盘，重启后缓存全丢、每次都要重新抓站。
        parts.append(f"""  metatube:
    image: {METATUBE_IMAGE}
    container_name: metatube
    restart: unless-stopped
    environment:
      - TZ=${{TZ}}
    volumes:
      - {d}/metatube:/data
    command: ["-port", "{METATUBE_PORT}", "-bind", "0.0.0.0",
              "-dsn", "/data/metatube.db", "-db-auto-migrate"]
    networks: [mediastack]
""")

    parts.append("networks:\n  mediastack:\n    name: mediastack\n")
    return "\n".join(parts)


SCAN_AUTO = "auto"          # 扫描路径的「跟随 OpenList 已挂载存储」模式


def parse_scan_spec(s):
    """把用户输入归一成扫描路径规格。

    接受三种写法，因为这三种都是用户会自然打出来的：
      · y / Y / auto / 自动        → SCAN_AUTO，跟随 OpenList 里已挂载的存储
      · /quark                     → 单条路径
      · /quark,/aliyun  或空格分隔  → 多条路径

    返回 SCAN_AUTO 或去重后的路径列表；给不出有效内容时返回 None，由调用方决定兜底。
    """
    s = (s or "").strip()
    if not s:
        return None
    if s.lower() in ("y", "yes", "auto") or s in ("自动", "是"):
        return SCAN_AUTO
    out, seen = [], set()
    for p in re.split(r"[,，\s]+", s):
        p = p.strip().rstrip("/")
        if not p:
            continue
        if not p.startswith("/"):
            p = "/" + p
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out or None


def resolve_scan_paths(d, spec):
    """把规格展开成实际要扫的路径列表。

    auto 模式在【生成配置的那一刻】才去读 OpenList 已挂载的存储 —— 这样以后加了
    新网盘，不用回来改这里的设置，重新生成一次就自动带上。
    """
    if spec == SCAN_AUTO:
        return [mp for mp, _drv, _st, _root in openlist_storages(d) if mp and mp != "/"]
    return list(spec or [])


def scan_task_id(path):
    """由路径生成任务 id。AutoFilm 用它给状态文件命名，多任务时必须唯一。"""
    t = re.sub(r"[^\w一-鿿]+", "_", path.strip("/")) or "root"
    return t


def openlist_public_url(cfg):
    """strm 里要写的 OpenList 地址。

    必须是【播放器也能访问】的地址,不能用容器内网的 http://openlist:5244 ——
    Emby 的 ffprobe 在同一个 docker 网络里访问得了,但 MediaWarp 的 HTTPStrm
    是把这个地址 302 给播放器的,手机/电视解析不了 openlist 这个主机名。
    """
    sub = next(s for s, _p, c, _l in SUBDOMAINS if c == "openlist")
    if cfg["has_domain"]:
        return f"https://{sub}.{cfg['domain']}"
    # 没域名时 openlist 的端口是 0.0.0.0 绑定的(见 gen_compose 的 bind),
    # 直接用 IP:端口,和用户平时打开 OpenList 界面的地址一样
    return f"http://{cfg['host_ip']}:{OPENLIST_PORT}"


def gen_autofilm_conf(cfg):
    """AutoFilm：定时遍历 OpenList，把网盘里的视频写成 .strm 文本文件。

    mode 必须选 AlistURL,而且 public_url 必须填。这个结论是实测出来的,三种取值
    都试过,别照着「看起来合理」去改:

      · RawURL   —— 把网盘的临时直链写死进 strm。夸克直链几小时就过期,过期后
                    整个媒体库集体播放失败。

      · AlistPath —— 只写 OpenList 上的路径(/quark/电影/x.mp4)。302 能work,
                    但【Emby 拿不到时长】:它把 strm 内容当本地文件喂给 ffprobe,
                    日志里是 `file:/quark/...: No such file or directory`,
                    RunTimeTicks 停在 0。而 Emby 判断续播点是按时长算百分比的
                    (MinResumePct 2 / MaxResumePct 90),分母为 0 这套逻辑整个失效:
                    停止播放时直接判定「已看完」,续播点清零、打上已看标记,
                    下次点进去从头开始,进度条也拖不动(播放器以为总长是 0)。
                    补 .nfo 也没用 —— nfo 的 title 会生效,但 ffprobe provider
                    在 nfo 之后跑,失败后把媒体信息覆盖掉。

      · AlistURL  —— 写完整的 OpenList 下载地址。Emby 对 URL 形式的 strm 会在
                    第一次播放时探测出时长,但播放只能交给 MediaWarp 的 http_strm,
                    而那条路【没有直链缓存】:每次开播都要现调一次网盘接口换直链,
                    实测 7.5~47 秒,表现就是"隔一阵没看,点开要转半天"。

    所以两种形态都不适合常驻。最终方案是【平时用 AlistPath,只在补探测的那几秒
    临时切成 URL】—— 见 heal_media_info()。时长探出来之后存在 Emby 数据库里,
    跟 strm 里写什么再无关系,进度条和续播照常。

    早先这里写着「AlistURL 会让 MediaWarp 拼成 /http:/openlist:5244/... 然后
    storage not found」——那是把 URL 形式的 strm 交给了只认路径的 alist_strm。

    临时切 URL 时 sign 是必须的:OpenList 默认 sign_all=true,不带签名访问 /d/
    会 401;link_expiration 默认 0,签名永不过期。
    """
    paths = list(cfg.get("scan_paths") or [])
    head = f"""# 由 media-stack.py 自动生成，「更新」会重新生成本文件，别手改。
# 要改扫描哪些路径：emby → 3 后补参数 → 4 扫描路径
alist:
  - id: openlist
    # base_url 走容器内网:AutoFilm 自己调接口列目录,不必绕一圈公网。
    # public_url 才是写进 strm 的地址,必须是播放器能访问到的 —— 两者分开正好
    # 满足「内网调接口、公网给播放器」这个要求。
    base_url: http://openlist:5244
    public_url: {openlist_public_url(cfg)}
    username: {cfg['ol_user']}
    password: {cfg['ol_pass']}
    otp_code:
    token:
    wait_time: 0.2          # 每次请求间隔，夸克风控较严，别调成 0

alist2strm_tasks:"""
    if not paths:
        # 一条路径都没有时给个空列表:AutoFilm 启动会打印 scheduled_count=0 而不是报错,
        # 比塞一个 "/" 进去强 —— 那会让它去扫整个 OpenList 根目录
        return head + " []\n"
    # 所有任务共用同一个 target_dir：AutoFilm 会按源目录结构镜像出来，
    # 多个网盘的内容各自落在自己的子目录里，Emby 那边仍然只需要指向一个媒体库路径。
    return head + "\n" + "".join(_gen_strm_task(cfg, p) for p in paths)


def _gen_strm_task(cfg, path):
    """单个 alist2strm 任务。多网盘时每条扫描路径生成一个。"""
    # id 和路径都加引号：纯数字的挂载路径（比如 /115）不加引号会被 YAML 读成整数，
    # 路径里带冒号、井号之类的字符也会把这一行拆坏
    return f"""  - id: "{scan_task_id(path)}"
    cron: "{cfg['strm_cron']}"
    alist: openlist
    source_dir: "{path}"
    target_dir: "{STRM_PATH}"
    mode: AlistPath         # 见 gen_autofilm_conf 的注释，三种取值都实测过
    flatten_mode: false
    overwrite: false
    concurrency: 5          # 网盘限流，并发别开太高
    download:
      enable: true          # 顺带把字幕/封面/nfo 拉到本地（体积很小）
      subtitle: true
      image: true
      nfo: true
      other_ext: []
      concurrency: 5
    sync:
      # 默认【关】。同步删除的前提是"扫描结果可信"，而网盘扫描根本不满足这个前提：
      # 跨境线路上列目录超时是常态，AutoFilm 跳过该目录照样报"任务完成"，于是那
      # 一整个目录的文件就被判定为"远端已删除"，本地 strm 跟着清掉。
      # 实际后果：第一轮扫出 387 个文件、生成 56 个 strm；之后每轮只扫到一两个，
      # 每轮都删掉一批 —— 跑得越多剩得越少，用户看到的是"越搞越拉垮"。
      #
      # 先前试过 smart_protection.threshold 从 100 改成 1 来堵，堵不住：
      # 只要扫描本身不可靠，同步删除就是个错误的默认值。
      # 权衡很清楚 —— 网盘里删掉的文件在本地留个失效 strm，点开报错而已；
      # 而整库被清空是灾难，而且现象诡异到根本查不出原因。
      # 真要开就把 enabled 改成 true，下面的保护参数已经调好了。
      enabled: false
      ignore:
      smart_protection:
        enabled: true
        # threshold 是「待删除数量达到多少【才】启动保护」，不是「超过多少就不删」。
        # 填 1 表示「只要有文件要删就先进保护」，配合 grace_scans 连续确认 3 轮。
        threshold: 1
        grace_scans: 3
"""


def gen_mediawarp_conf(cfg):
    """MediaWarp：反代在 Emby 前面，拦截播放请求并 302 到网盘直链。"""
    return f"""# 由 media-stack.py 自动生成，「更新」会重新生成本文件，别手改。
port: 9000

server:
  type: Emby
  addr: http://emby:8096
  auth: {cfg['emby_api_key']}     # Emby API Key，留空则 MediaWarp 无法工作

log:
  access:
    console: true
    file: false
  service:
    console: true
    file: true

cache:
  enable: true
  # final_url: false 之后这一项其实用不上了(没有"最终地址"需要缓存),
  # 但留个长值兜底:万一以后谁把 final_url 打开,1 分钟的缓存等于每分钟都要重付
  # 一次连 CDN 的代价,那一跳实测能到 32 秒。
  http_strm_ttl: 2h
  # 默认 10m 太短。每次缓存过期,MediaWarp 就要让 OpenList 重新去网盘换一次直链;
  # 跨境线路上这个调用实测 0.3 秒到 44 秒不等,还经常直接超时 —— 日志里表现为
  #   404 | 30.3s | GET /emby/Videos/11/stream
  # 播放器等不到地址,画面就停在那儿。一部 93 分钟的电影按 10m 算要换约 9 次,
  # 等于把 9 次赌博串进一次观影。
  # 夸克直链的 auth_key 实测有效期约 30 小时,缓存 2 小时安全余量很足,
  # 一部片子只需要成功换一次。
  alist_api_ttl: 2h
  image_ttl: 10m
  subtitle_ttl: 2h

web:
  enable: false
  custom: false
  index: false
  crx: false
  actor_plus: true
  fanart_show: false
  external_player_url: false
  danmaku: false
  video_together: false

client:
  enable: false
  mode: BlackList
  list: []

# strm 平时是【路径形式】，所以播放走 alist_strm。它有 alist_api_ttl 那个 2 小时
# 直链缓存，命中时 3 毫秒就 302 走了。
#
# 为什么不用 http_strm + URL 形式的 strm：那条路能让 Emby 探测出时长，但
# 【没有直链缓存】—— 每次开播都要现调一次网盘接口换直链，实测 7.5~47 秒，
# 表现就是"隔一阵没看，点开要转半天"。时长的问题改用 heal_media_info() 解决：
# 只在补探测的那几秒钟把 strm 临时切成 URL，探完立刻切回来。
#
# 两个处理器【不能并存】：MediaWarp 按路径前缀选处理器，而 strm 全在同一个目录
# 下，prefix_list 只能是同一个值。都开的话 http_strm 抢先接管，然后把路径形式的
# 内容当 URL 去重定向，播放直接报「当前没有兼容的流」。
http_strm:
  enable: false
  proxy: false
  final_url: false
  compatibility_mode: false
  prefix_list: []

alist_strm:
  enable: true
  proxy: true
  # 必须是 true。false 的话 MediaWarp 会拿下面的 addr 拼重定向地址，把播放器
  # 302 到 http://openlist:5244/d/... —— 那是容器内部地址，手机/电视根本连不上。
  # true 表示 MediaWarp 自己调 OpenList API 换出网盘直链，直接把播放器 302 过去，
  # 视频流从网盘直达播放器，完全不经过本机带宽 —— 这正是这套东西存在的意义。
  raw_url: true
  list:
    - addr: http://openlist:5244
      username: {cfg['ol_user']}
      password: {cfg['ol_pass']}
      prefix_list:
        # strm 在 Emby 里的路径前缀，必须和 AutoFilm 的 target_dir 一致
        - {STRM_PATH}

subtitle:
  enable: true
  srt2ass: true
  ass_style:
    - "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    - "Style: Default,楷体,20,&H03FFFFFF,&H00FFFFFF,&H00000000,&H02000000,-1,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1"
"""


def gen_homepage_conf(cfg):
    def url_for(sub, port):
        if cfg["has_domain"]:
            return f"https://{sub}.{cfg['domain']}"
        return f"http://{cfg['host_ip']}:{port}"

    settings = """title: 我的媒体中心
theme: dark
color: slate
headerStyle: clean
layout:
  媒体:
    style: row
    columns: 3
"""
    # 健康检查要打「实际在监听的那个容器:端口」，不是卡片名字对应的容器。
    # Emby 的对外入口是 MediaWarp（9000），Emby 自己在 8096 —— 早先这里拼成了
    # emby:9000，那个端口上什么都没有，卡片就一直显示错误码。
    # 打专门的健康检查端点，别打根路径：Emby 的 / 会 302 跳到 /web/index.html，
    # MediaWarp 原样透传，Homepage 收到跳转就把卡片标成异常（显示 RESPONSE）。
    # /System/Info/Public 是 Emby 的公开信息接口，不需要认证、稳定返回 200 JSON。
    monitor = {
        "emby":     f"http://mediawarp:{MEDIAWARP_PORT}/System/Info/Public",
        # OpenList 打根路径就正常返回 200，别改成 /ping —— 现在是好的，不动它
        "openlist": f"http://openlist:{OPENLIST_PORT}",
        "homepage": "http://homepage:3000",
    }
    services = ["- 媒体:"]
    for sub, port, container, label in SUBDOMAINS:
        if sub == "home":
            continue
        # 用 openlist.png 而不是 alist.png：跑的是 OpenList，挂 Alist 的旧标不对。
        # 两个图标在 homarr-labs/dashboard-icons 里都在，确认过 HTTP 200。
        icon = {"emby": "emby.png", "openlist": "openlist.png"}.get(container, "")
        services.append(f"""    - {label}:
        icon: {icon}
        href: {url_for(sub, port)}
        description: {'影视播放' if container == 'emby' else '网盘挂载'}
        siteMonitor: {monitor.get(container, f'http://{container}:{port}')}""")
    # ⚠ 三样资源必须写在【同一个】 resources 块里，不要为了分别加中文标题拆开。
    # 试过拆成三块（label 是「给这一组起名」，一块只能有一个标题），结果是
    # 只有一块能拿到数据，另外两块常驻「-」，被挪走的那块还会显示「API 错误」——
    # 接口本身是好的（直接 curl /api/widgets/resources?type=memory 数据齐全），
    # 纯粹是多块并存渲染不出来。拿标题换掉数据，是笔亏本买卖。
    #
    # 标题里也【不要】写核数、内存总量这类硬件数字。每台机器配置不一样，而写进
    # widgets.yaml 的是生成那一刻的快照 —— 升配、换机之后不跑「更新」就一直是错的，
    # 面板上摆一个错数字比不摆更糟。核数尤其没法做成实时的：Homepage 的
    # /api/widgets/resources?type=cpu 只返回 usage 和 load，压根没有核数这个字段。
    #
    # expanded 让内存/硬盘除了「剩余」再显示「总量」，CPU 多显示 load。
    #
    # 盯 /app/config 而不是 / ：容器里的 / 是 overlay，Homepage 官方文档写明要监控的
    # 盘必须挂进容器。/app/config 是 {安装目录}/homepage/config 的 bind mount，
    # df 出来是真实的块设备（/dev/vda1），读的就是宿主机根分区的容量。
    widgets = """- resources:
    label: 系统
    expanded: true
    cpu: true
    memory: true
    disk: /app/config
- search:
    provider: duckduckgo
    target: _blank
"""
    return settings, "\n".join(services) + "\n", widgets


# ============================================================================ nginx 站点
def gen_nginx_site(cfg):
    listen_port = cfg["ngx_port"]
    listen = "443" if listen_port == 443 else f"127.0.0.1:{listen_port}"
    if cfg["http2_directive"]:
        listen_line = f"    listen {listen} ssl;\n    http2 on;"
    else:
        listen_line = f"    listen {listen} ssl http2;"

    auth_block = ""
    if cfg["basic_auth"]:
        auth_block = (f'    auth_basic           "media-stack";\n'
                      f"    auth_basic_user_file {HTPASSWD_FILE};\n")

    out = ["# 由 media-stack.py 自动生成，重跑会覆盖，别手改。",
           "# 本文件只新增站点，不改动节点(bgpeer)的任何 nginx 配置。"]
    for sub, port, container, _label in SUBDOMAINS:
        if sub == "home" and not cfg["homepage"]:
            continue
        # 自带账号体系的服务不加 Basic Auth：
        #   emby —— 有自己的用户系统，而且 App 客户端处理不了 Basic Auth，套上去连不上
        #   list —— OpenList 有 admin 账号，再套一层就变成连输两次不同的密码，
        #            实际使用中反复被这个绊住：弹框过了，又对着 OpenList 的登录页
        #            输弹框那对账号，然后以为"登不进去"。徒增困惑，安全上也没多拿到什么。
        # 只有 Homepage 是真的零认证，谁打开都能看到全部服务地址，那层必须保留。
        a = "" if sub in ("emby", "list") else auth_block
        out.append(f"""
server {{
{listen_line}
    server_name {sub}.{cfg['domain']};

    ssl_certificate     {cfg['crt']};
    ssl_certificate_key {cfg['key']};

    # 单独一份访问日志:和节点的日志混在一起就分不出是谁在敲媒体服务了。
    # 「5 链路体检」靠它统计有多少陌生外网 IP 访问过 —— 这几个服务是公网可达的,
    # 拿到域名就能敲门,总得有个地方能看见。logrotate 的默认规则匹配
    # /var/log/nginx/*.log,不用额外配置轮转。
    access_log {NGX_ACCESS_LOG};

    client_max_body_size 0;
{a}
    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        "upgrade";
        # MediaWarp 返回的 302 必须原样透传给播放器，
        # 被 nginx 改写或跟随的话直链就失效了，流量会退回服务器中转
        proxy_redirect  off;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}
}}""")
    return "\n".join(out) + "\n"


def apply_nginx_site(cfg):
    """写站点配置并生效。nginx -t 不过就还原备份并返回 False —— 这台机器上
       多半跑着用户的节点，绝不能因为本脚本让 nginx 起不来。"""
    os.makedirs("/etc/nginx/conf.d", exist_ok=True)
    backup = ""
    if os.path.exists(NGX_SITE):
        backup = f"{NGX_SITE}.bak.{int(time.time())}"
        shutil.copy2(NGX_SITE, backup)

    with open(NGX_SITE, "w") as f:
        f.write(gen_nginx_site(cfg))

    if sh("nginx -t").returncode == 0:
        nginx_reload()
        ok(f"nginx 配置已生效：{NGX_SITE}")
        if backup:
            os.remove(backup)
        if "ssl_preread" in (sh("nginx -T 2>/dev/null").stdout or ""):
            warn(f"节点用了 SNI 分流。新子域名靠分流表的 default 落到 "
                 f"127.0.0.1:{cfg['ngx_port']}，正常情况无需改节点配置。")
        return True

    err("nginx -t 未通过，已撤销本次改动（节点不受影响）：")
    for line in (sh("nginx -t").stderr or "").splitlines():
        print("     " + line)
    if backup:
        shutil.move(backup, NGX_SITE)
    else:
        os.remove(NGX_SITE)
    if sh("nginx -t").returncode == 0:
        warn("已回滚，nginx 当前配置正常，节点没事。")
    else:
        err("回滚后 nginx -t 仍不通过，请立刻检查上面的输出！")
    return False


def write_htpasswd(user, password):
    """生成 APR1 哈希的密码文件。用 openssl 而不是 htpasswd，
       免得为此去装 apache2-utils。"""
    # 不走 shell:密码可能含 $ " ! 等字符,拼进 shell 字符串会被二次解释。
    # 直接传参数列表,内核 execve 原样送达,没有任何解析环节。
    r = subprocess.run(["openssl", "passwd", "-apr1", password],
                       text=True, capture_output=True)
    if r.returncode != 0 or not r.stdout.strip():
        return False
    with open(HTPASSWD_FILE, "w") as f:
        f.write(f"{user}:{r.stdout.strip()}\n")
    worker = nginx_worker_user()
    if worker and sh(f"chown root:{worker} {HTPASSWD_FILE}").returncode == 0:
        os.chmod(HTPASSWD_FILE, 0o640)
    else:
        # 识别不出 worker 身份就退到 644：文件里存的是哈希不是明文，
        # 可读也好过 auth_basic 整个 500 挂掉
        os.chmod(HTPASSWD_FILE, 0o644)
        warn("没能识别 nginx worker 用户，密码文件按 644 处理（存的是哈希）。")
    return True


def issue_cert(domain, crt, key, cf_token):
    """用 acme.sh + Cloudflare DNS-01 签泛域名证书。
       DNS-01 不占任何端口，和节点已经占着的 80/443 完全不冲突。"""
    acme = acme_bin()
    if not acme:
        warn("没找到 acme.sh，正在安装...")
        sh("curl -fsSL https://get.acme.sh | sh", timeout=300)
        acme = acme_bin()
    if not acme:
        err("acme.sh 不可用，跳过证书签发。")
        return False

    os.makedirs(os.path.dirname(crt), exist_ok=True)
    env = dict(os.environ)
    if cf_token:
        env["CF_Token"] = cf_token
    # 指定 letsencrypt 是为了绕开 acme.sh 默认 CA(ZeroSSL) 要求先注册邮箱
    r = subprocess.run(
        f"{acme} --issue --dns dns_cf --keylength ec-256 -d '*.{domain}' --server letsencrypt",
        shell=True, text=True, capture_output=True, env=env, timeout=600)
    if r.returncode == 0:
        ok("证书签发成功")
    else:
        warn("签发未成功（可能证书已存在，或 Token 权限不足），继续尝试安装。")

    r = subprocess.run(
        f"{acme} --install-cert --ecc -d '*.{domain}' "
        f"--fullchain-file {crt} --key-file {key} --reloadcmd 'nginx -s reload'",
        shell=True, text=True, capture_output=True, env=env, timeout=300)
    if r.returncode == 0 and os.path.exists(crt) and os.path.exists(key):
        ok(f"证书已装到 {crt}（到期自动续，续完自动 reload nginx）")
        return True
    err("证书安装失败。")
    return False


# ============================================================================ 管理命令
CLI_TEMPLATE = '''#!/usr/bin/env bash
# media-stack 管理命令（由 media-stack.py 生成）
set -uo pipefail
D="${MEDIA_STACK_DIR:-__DIR__}"
C="${D}/docker-compose.yml"
b=$'\\e[1m'; r=$'\\e[0m'; y=$'\\e[33m'

# 以 emby 这个名字调用且不带参数时，默认就是「把面板地址甩出来」
[[ "$(basename "$0")" == "emby" && $# -eq 0 ]] && set -- panel

# help 不依赖安装目录，没装的机器上也要能看
case "${1:-info}" in
  help|-h|--help)
    cat <<'H'
用法: media-stack [命令]   (也可以敲 emby，不带参数时直接甩出面板地址)

  (无参数)        服务地址 + 账号密码 + 容器状态
  panel           只甩出面板入口地址(等同于直接敲 emby)
  url             只列出访问地址
  ps              容器状态
  logs <服务>     跟踪日志，如 media-stack logs mediawarp
  restart [服务]  重启，省略服务名则全部重启
  strm            立刻跑一次 strm 生成并跟日志
  302             跟踪 MediaWarp 日志，用来验证直链是否生效
  update          拉最新镜像并重启
  stop / start    停止 / 启动全部
H
    exit 0 ;;
esac

[[ -f "$C" ]] || { echo "找不到 ${C}"; echo "装在别的目录:MEDIA_STACK_DIR=/你的目录 media-stack"; exit 1; }
dc(){ docker compose -f "$C" --env-file "${D}/.env" "$@"; }

case "${1:-info}" in
  info)
    [[ -f "${D}/CREDENTIALS.txt" ]] && cat "${D}/CREDENTIALS.txt" || echo "${y}没有 CREDENTIALS.txt${r}"
    echo; echo "${b}容器状态${r}"; dc ps ;;
  panel)
    F="${D}/CREDENTIALS.txt"
    [[ -f "$F" ]] || { echo "找不到 ${F}"; exit 1; }
    echo; echo "  ${b}管理面板${r}"
    echo "  ────────────────────────────────────────────────"
    grep -E "首页入口|Emby|OpenList" "$F" | sed 's/^ */  /'
    echo "  ────────────────────────────────────────────────"
    echo "  ${y}浏览器打开即可;手机终端里长按链接一般能直接点开。${r}"
    echo "  完整地址和全部密码:${b}media-stack${r}"; echo ;;
  url)     grep -oE "https?://[^ ]+" "${D}/CREDENTIALS.txt" 2>/dev/null | sort -u ;;
  ps)      dc ps ;;
  logs)    shift; dc logs -f --tail 100 "$@" ;;
  restart) shift; dc restart "$@" && echo "已重启" ;;
  start|up)   dc up -d ;;
  stop|down)  dc down ;;
  update)
    # 走脚本的更新流程，而不是只 pull 镜像 —— 两条路必须做同一件事，
    # 否则从这里更新的人拿不到配置修复，只会以为"更新过了还是老样子"
    S=/etc/bgpeer/media-stack.py
    if [[ -f "$S" ]]; then python3 "$S" update; else dc pull && dc up -d; fi ;;
  strm)
    # AutoFilm v2 没有手动触发的入口:./autofilm --help 只有 --config/--log/--timezone
    # 这些开关,启动时也只注册 cron、不跑任务。所以这里临时把 cron 改成
    # "两分钟后的那一分钟"、只触发一次,跑完立刻还原成用户原来的定时设置。
    CFG="${D}/autofilm/config/config.yaml"
    [[ -f "$CFG" ]] || { echo "找不到 ${CFG}"; exit 1; }
    BAK="${CFG}.strmbak.$$"
    cp "$CFG" "$BAK" || exit 1
    LP=""
    restore() {
      [[ -n "$LP" ]] && { kill "$LP" 2>/dev/null; wait "$LP" 2>/dev/null; }
      # 还原是必须发生的,哪怕用户中途 Ctrl-C、哪怕 docker 命令失败
      if [[ -f "$BAK" ]]; then
        mv -f "$BAK" "$CFG"
        docker restart autofilm >/dev/null 2>&1
        echo; echo "${y}已还原原来的定时设置。${r}"
      fi
    }
    # INT/TERM 只负责退出:bash 跑完信号处理函数后默认【继续往下执行】,
    # 直接把 restore 挂在 INT 上的话,Ctrl-C 会还原配置然后接着轮询六分钟。
    # 还原统一交给 EXIT,正常结束和被中断走同一条路。
    trap 'exit 130' INT TERM
    trap restore EXIT

    # 定成"每分钟"是错的:一轮跑不完下一轮就压上来,几轮任务并发扫同一个目录、
    # 互相删对方刚写出去的 strm(实测跨境网络慢时一轮要 121 秒,同时跑了三轮,
    # 最后 io error: No such file or directory)。改成【指定时刻只触发一次】。
    # 时刻必须按【AutoFilm 调度器自己那套时钟】算。不能用 docker exec autofilm date:
    # 容器里的 date 认 TZ 环境变量返回本地时间,而 AutoFilm 启动时打印的是
    # 「使用应用时区 timezone=UTC」—— 它没读到 TZ、回落到了 UTC,两者差好几个小时。
    # 实测:date 说 19:57、AutoFilm 认为是 11:56,cron 写成 "0 57 19" 要等到次日
    # 凌晨才触发,表现就是"点了没反应、卡住不动"。
    # 日志时间戳末尾的偏移(+00:00 / +09:00 / Z)才是调度器真正用的时钟。
    OFF="$(docker logs --tail 80 autofilm 2>&1 \
           | grep -oE 'T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})' \
           | tail -1 | grep -oE '(Z|[+-][0-9]{2}:[0-9]{2})$')"
    case "$OFF" in
      ""|Z) OFFMIN=0 ;;
      *)    OFFMIN=$(( 10#${OFF:1:2} * 60 + 10#${OFF:4:2} ))
            [[ "${OFF:0:1}" == "-" ]] && OFFMIN=$(( -OFFMIN )) ;;
    esac
    NOW=$(( 10#$(date -u +%H) * 60 + 10#$(date -u +%M) ))
    T=$(( (NOW + OFFMIN + 2 + 1440) % 1440 ))      # +2 分钟,留足重启和对齐的余量
    FH="$(printf '%02d' $((T / 60)))"; FM="$(printf '%02d' $((T % 60)))"
    # 引号用变量带进去。这整段是 Python 的三引号字符串,在里面用反斜杠转义双引号
    # 会被 Python 自己吃掉,落到 bash 里就是引号错乱的一行,sed 静默不替换 ——
    # 而且不报错,只是 cron 没改成,人会以为是网盘慢。用变量就完全绕开这个坑。
    q='"'
    sed -i "s|cron: ${q}.*${q}|cron: ${q}0 $FM $FH * * *${q}|" "$CFG"

    TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    docker restart autofilm >/dev/null 2>&1 || { echo "autofilm 没在运行"; exit 1; }
    echo "${b}已安排在 ${FH}:${FM}(AutoFilm 调度器时间)触发一次,最多等 2 分钟开始。${r}"
    echo "${y}跑完会自己退出并还原定时设置 —— 中途别按 Ctrl-C,那会把任务掐断,"
    echo "strm 只生成一半,Emby 里就会报「找不到目录」。${r}"
    echo "${b}等到出现「Alist2Strm 任务完成」那一行为止,中间安静一阵是正常的。${r}"
    echo

    docker logs -f --since "$TS" autofilm 2>&1 &
    LP=$!
    # 轮询完成标记而不是死等日志:任务跑完后日志就安静了,
    # 只跟着 docker logs 的话会一直挂在那里,得靠人去按 Ctrl-C。
    # 封顶给到 15 分钟:网盘慢的时候一轮真的能跑十几分钟,提前收工会让人
    # 以为"又失败了",其实还在跑。
    for _ in $(seq 1 225); do           # 4s x 225 = 15 分钟封顶
      sleep 4
      if docker logs --since "$TS" autofilm 2>&1 | grep -q "Alist2Strm 任务完成"; then
        sleep 1; break
      fi
    done
    echo
    echo "${b}看上面那行「Alist2Strm 任务完成」里的 strm_created_count / strm_skipped_count。${r}"
    echo "${y}skipped 不是失败,是文件已经在了。之后去 Emby 扫一次媒体库。${r}"
    ;;
  302)
    echo "跟踪 MediaWarp 日志。现在去播放一集，看到 302 就说明直链生效:"
    docker logs -f --tail 20 mediawarp ;;
  *) echo "未知命令:$1。看 media-stack help"; exit 1 ;;
esac
'''


KEEPALIVE_CRON = "/etc/cron.d/media-stack-keepalive"
# 每 20 分钟一次 ≈ 72 次/天。别为了"让链路更热"去调小它 —— 实测耗时和空闲时间
# 不相关，理由见 do_keepalive() 的文档字符串。
KEEPALIVE_MIN  = 20


def keepalive_state(d):
    try:
        with open(os.path.join(d, "keepalive.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def do_keepalive():
    """定时做一次真实的目录列举。

    ⚠ 这个功能的原始理由已经被实测推翻,保留但别再照着旧理由加码。

    原本的说法是"最近调用过,链路就是热的,后面都是零点几秒",所以定时热一下能
    消掉第一次播放的转圈。实测不成立 —— 同一台机器同一条路径,刻意用 30/60/120/
    300/600 秒的不同空闲间隔各采样,耗时是:
        30s→1.7   30s→12.6   60s→4.9   60s→0.5   120s→3.4
        120s→3.0  300s→5.1   300s→2.6  600s→1.2   (秒)
    空闲 600 秒是全场第二快,空闲 30 秒出了最慢的一次。【耗时和空闲时间不相关】,
    波动来自跨境线路本身(晚高峰单次能飙到 120 秒,过了高峰又回到 3 秒)。

    结论:缩短间隔不会让播放变快,别改 KEEPALIVE_MIN。留着它是因为成本极低
    (72 次/天)、能给体检提供一个"链路最近通没通"的心跳,不是因为它能保温。

    输出写进 json 给体检读,不往日志里堆东西。
    """
    d = ms_install_dir()
    if not is_installed(d):
        return
    cfg = rebuild_cfg_from_disk(d)
    pw = read_env(os.path.join(d, ".secrets"), "OPENLIST_PASS",
                  fallback=os.path.join(d, ".env"))
    rec = {"ts": int(time.time()), "ok": False, "elapsed": 0.0, "error": ""}
    t0 = time.monotonic()
    try:
        tok = (_ol_api("/api/auth/login", {"username": "admin", "password": pw},
                       timeout=30).get("data") or {}).get("token", "")
        if not tok:
            raise RuntimeError("登录失败")
        # refresh: true 是必须的 —— 不加的话 OpenList 直接返回目录缓存、根本不联网,
        # 记录下来的耗时永远是 0.0 秒,当心跳用毫无意义(测的是本地缓存命中率)。
        p = (cfg["scan_paths"] or ["/"])[0]
        r = _ol_api("/api/fs/list", {"path": p, "password": "", "page": 1,
                                     "per_page": 1, "refresh": True},
                    tok, timeout=180)
        if r.get("code") != 200:
            raise RuntimeError(r.get("message", "list 失败")[:60])
        rec["ok"] = True
    except Exception as e:
        rec["error"] = _short_err(e)
    rec["elapsed"] = round(time.monotonic() - t0, 1)
    try:
        with open(os.path.join(d, "keepalive.json"), "w") as f:
            json.dump(rec, f, ensure_ascii=False)
    except OSError:
        pass


def install_keepalive(install_dir):
    """装保活定时任务。用 cron.d 而不是 crontab -e：这样卸载时删一个文件就干净了。"""
    try:
        txt = (f"# media-stack 网盘链路保活：每 {KEEPALIVE_MIN} 分钟做一次目录列举，\n"
               f"# 把 token 和连接热着，避免第一次播放卡在「换直链」上转圈。\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               # 指向当前正在跑的这份脚本：「更新」是原地替换这个文件的，
               # 所以 cron 会一直调到最新版，不用回来改这条
               f"*/{KEEPALIVE_MIN} * * * * root python3 {os.path.realpath(__file__)} "
               "keepalive >/dev/null 2>&1\n")
        with open(KEEPALIVE_CRON, "w") as f:
            f.write(txt)
        os.chmod(KEEPALIVE_CRON, 0o644)
        return True
    except OSError as e:
        warn(f"装保活定时任务失败（不影响使用）：{e}")
        return False


def install_cli(install_dir):
    with open(CLI_PATH, "w") as f:
        f.write(CLI_TEMPLATE.replace("__DIR__", install_dir))
    os.chmod(CLI_PATH, 0o755)
    # emby 是同一个脚本的软链，不带参数时直接打印面板入口（靠 basename 判断）
    if os.path.islink(CLI_ALIAS) or os.path.exists(CLI_ALIAS):
        os.remove(CLI_ALIAS)
    os.symlink(CLI_PATH, CLI_ALIAS)
    ok(f"已安装：敲 {BOLD}emby{RST} 甩出面板地址，敲 {BOLD}media-stack{RST} 看全部")


# ============================================================================ 总览
def build_summary(cfg, colored=True):
    def c(code, s):
        return f"{code}{s}{RST}" if colored else s

    lines = [f"服务地址一览（生成时间：{time.strftime('%F %T')}）",
             "  " + "-" * 58]
    if cfg["basic_auth"]:
        lines += [
            "  " + c(YELLOW + BOLD, "浏览器弹框") + c(DIM, "（只有首页入口会弹，它没有自己的账号）"),
            "      用户名  " + c(CYAN + BOLD, cfg["ba_user"]),
            "      密  码  " + c(CYAN + BOLD, cfg["ba_pass"]),
            "      " + c(DIM, "打开首页入口时会弹，输它。"),
            "  " + "-" * 58,
            "  " + c(GREEN + BOLD, "各服务自己的账号"),
        ]
    lines.append("  " + pad("服务", 14) + pad("访问地址", 36) + "账号/密码")
    lines.append("  " + "-" * 58)

    for sub, port, container, label in SUBDOMAINS:
        if sub == "home" and not cfg["homepage"]:
            continue
        url = (f"https://{sub}.{cfg['domain']}" if cfg["has_domain"]
               else f"http://{cfg['host_ip']}:{port}")
        if container == "openlist":
            cred = f"{cfg['ol_user']} / {cfg['ol_pass']}"
        elif container == "emby":
            cred = "首登自设"
        else:
            cred = "—"
        lines.append("  " + pad(label, 14) + pad(url, 36) + cred)

    lines.append("  " + "-" * 58)
    if cfg["basic_auth"]:
        lines.append("  " + c(DIM, "Emby 和 OpenList 不弹框，直接用它们自己的账号登录。"))
        lines.append("  " + "-" * 58)
    lines.append(f"  安装目录：{cfg['install_dir']}    媒体目录：{cfg['data_root']}")
    return "\n".join(lines)


def cron_human(cron):
    """把 6 位 cron 说成人话：'0 15 5 * * *' → '每天 05:15（北京时间）'。

    时刻按哪个时区解释，是这里最容易被误解的一点(我们把调度器钉在了北京时间,
    不是服务器本地时区),所以时区必须跟着时刻一起显示,不能只报一个 05:15。
    看不懂的格式就原样打出来,不猜。
    """
    p = cron.split()
    if len(p) == 6 and p[3] == p[4] == p[5] == "*" and all(x.isdigit() for x in p[:3]):
        return f"每天 {int(p[2]):02d}:{int(p[1]):02d}（北京时间）"
    return f"{cron}   {DIM}(6 位 cron，按北京时间){RST}"


def openlist_storages(d):
    """读 OpenList 的库，把挂好的网盘和几个关键参数列出来。

    只取挂载点、驱动、状态、根文件夹ID —— refresh_token / cookie 这些一概不碰，
    「使用信息」这段输出是会被截图发出来的。库不在、表名对不上就静默返回空，
    这只是锦上添花，不该让整个使用信息因为它报错。
    """
    db = os.path.join(d, "openlist", "config", "data.db")
    if not os.path.exists(db):
        return []
    try:
        # 只读方式打开，绝不因为看一眼信息而动到 OpenList 正在用的库
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("select mount_path, driver, status, addition "
                           "from x_storages order by mount_path").fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for mp, drv, status, add in rows:
        try:
            root = str(json.loads(add).get("root_folder_id", ""))
        except Exception:
            root = ""
        out.append((mp, drv, status, root))
    return out


def storage_token_days(d):
    """各网盘授权令牌还剩几天 [(挂载点, 天数), ...]。取不到就不返回那一条。

    为什么需要:令牌过期的表现和「网盘不通」一模一样 —— 列目录还行(读缓存)、
    一点开文件就转圈。实测过一次令牌只剩 5 天的情况,那天要是到期了,用户会
    以为又是跨境线路抽风,而这个是能修的(重新扫码授权),线路那个不能。
    静默过期是最坏的一种坏法,所以提前把天数摆出来。

    只解 JWT 的 payload 取 exp。【不返回令牌本身】—— 这个函数的输出会进体检,
    而体检结果是会被截图发出去的。
    """
    db = os.path.join(d, "openlist", "config", "data.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("select mount_path, addition from x_storages").fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for mp, add in rows:
        try:
            a = json.loads(add)
        except Exception:
            continue
        for k, v in a.items():
            if "token" not in k.lower() or not isinstance(v, str) or v.count(".") != 2:
                continue
            try:
                seg = v.split(".")[1]
                seg += "=" * (-len(seg) % 4)
                exp = json.loads(base64.urlsafe_b64decode(seg)).get("exp")
            except Exception:
                continue
            if exp:
                out.append((mp, (exp - time.time()) / 86400))
                break
    return out


# ============================================================================ 卸载
def show_info():
    """使用信息：把怎么进、用什么账号密码，全部从落盘的配置里读出来打印。
       不写死任何值 —— 用户改过、重跑过，这里显示的都得是当前真正生效的。"""
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return

    env_file = os.path.join(d, ".env")
    sec_file = os.path.join(d, ".secrets")
    domain   = read_env(env_file, "DOMAIN")
    ol_pass  = read_env(sec_file, "OPENLIST_PASS", fallback=env_file)
    ba_user  = read_env(sec_file, "BA_USER", fallback=env_file) or "media"
    ba_pass  = read_env(sec_file, "BA_PASS", fallback=env_file)
    data_root = read_env(env_file, "DATA_ROOT") or os.path.join(d, "media")

    print()
    print("=" * 60)
    print(f"  {BOLD}媒体栈使用信息{RST}")
    print("=" * 60)

    print(f"\n  {BOLD}▸ 访问地址{RST}")
    for sub, port, container, label in SUBDOMAINS:
        if container == "homepage" and not os.path.isdir(os.path.join(d, "homepage")):
            continue
        url = f"https://{sub}.{domain}" if domain else f"http://{public_ip()}:{port}"
        print(f"      {pad(label, 11)}{CYAN}{BOLD}{url}{RST}")
    if domain:
        print(f"      {DIM}首页入口就是导航面板，所有服务都能从那里点进去。{RST}")

    if ba_pass:
        print(f"\n  {YELLOW}{BOLD}▸ 浏览器弹框{RST}{DIM}（只有首页入口会弹）{RST}")
        print(f"      用户名   {CYAN}{BOLD}{ba_user}{RST}")
        print(f"      密  码   {CYAN}{BOLD}{ba_pass}{RST}")
        print(f"      {DIM}打开首页入口时会弹，输它。{RST}")

    print(f"\n  {GREEN}{BOLD}▸ 各服务自己的账号{RST}")
    print(f"      Emby       {DIM}首次打开自己设；不走弹框（App 客户端处理不了 Basic Auth）{RST}")
    print(f"      OpenList   {CYAN}{BOLD}admin{RST} / {CYAN}{BOLD}{ol_pass}{RST}")

    print(f"\n  {BOLD}▸ 常用命令{RST}")
    print(f"      {GREEN}{BOLD}emby{RST}                甩出面板地址")
    print(f"      {GREEN}{BOLD}media-stack{RST}         全部地址 + 密码 + 容器状态")
    print(f"      {GREEN}{BOLD}media-stack 302{RST}     播一集看有没有 302，验证直链是否真生效")
    print(f"      {GREEN}{BOLD}media-stack strm{RST}    立刻跑一次 strm 生成")
    print(f"      {GREEN}{BOLD}media-stack logs{RST} <服务>   跟踪日志")

    print(f"\n  {BOLD}▸ 路径{RST}")
    print(f"      安装目录   {d}")
    print(f"      媒体目录   {data_root}")
    print(f"      strm 目录  {os.path.join(data_root, 'strm', STRM_SUBDIR)}"
          f"   {DIM}(Emby 媒体库指向容器内的 {STRM_PATH}){RST}")
    print(f"      凭据存档   {os.path.join(d, 'CREDENTIALS.txt')}")
    if os.path.exists(NGX_SITE):
        print(f"      nginx 站点 {NGX_SITE}")

    # 这一屏是「地址 / 账号密码 / 怎么用」，不是诊断屏。所以这里只列【挂了哪些盘】,
    # 状态细节和修法全部交给「5 链路体检」——
    #   · status 字段装的是整条 Go 错误，里面带着 access_token。原样打印等于把网盘
    #     令牌摆在屏幕上，而这一屏恰恰是最常被截图发出去的
    #   · 而且它是【存储初始化那一刻】写进去的，之后恢复了也不会改回 work，
    #     拿它当实时状态用会把陈年记录报成当前故障
    stores = openlist_storages(d)
    if stores:
        print(f"\n  {BOLD}▸ 网盘挂载{RST}")
        for mp, drv, status, root in stores:
            print(f"      {pad(mp, 14)}{pad(drv, 12)}"
                  + (f"{DIM}根文件夹ID={root}{RST}" if root else ""))
        if any(s != "work" for _m, _d, s, _r in stores):
            print(f"      {YELLOW}有存储上次初始化时报过错{RST}"
                  f"{DIM} —— 跑「5 链路体检」看现在通不通、怎么修{RST}")

    # strm 数量是判断「Emby 里为什么是空的」最直接的指标，放在容器状态前面
    n = strm_count(d)
    print(f"\n  {BOLD}▸ 媒体库内容{RST}")
    if n:
        print(f"      已生成 {GREEN}{BOLD}{n}{RST} 个 strm")
        print(f"      Emby 媒体库指向 {CYAN}{BOLD}{STRM_PATH}{RST}   {DIM}(容器内路径){RST}")
        cron = read_yaml_scalar(os.path.join(d, "autofilm", "config", "config.yaml"), "cron")
        if cron:
            print(f"      自动生成 {cron_human(cron)}")
    else:
        print(f"      {YELLOW}{BOLD}0 个 strm —— Emby 里现在一定是空的{RST}")
        print(f"      {DIM}还差两步，按顺序做：{RST}")
        print(f"        1. 在 OpenList（上面的网盘挂载地址）里添加网盘存储")
        print(f"           {DIM}夸克类驱动的「根文件夹ID」必须填 {RST}{BOLD}0{RST}")
        print(f"        2. 回本菜单点 {GREEN}{BOLD}4 生成媒体库{RST}")
        print(f"      {DIM}生成完再去 Emby 添加媒体库，路径填 {STRM_PATH}{RST}")

    if metatube_on(d):
        print(f"\n  {BOLD}▸ MetaTube 番号刮削{RST}")
        print(f"      服务端地址 {CYAN}{BOLD}http://metatube:{METATUBE_PORT}{RST}"
              f"   {DIM}(容器内地址，填进 Emby 插件设置){RST}")
        print(f"      {DIM}用法：{RST}")
        print(f"        1. Emby → 设置 → 插件 → MetaTube → 服务器地址填上面那个")
        print(f"        2. 要用它的媒体库 → 编辑 → 刮削器勾上 {BOLD}MetaTube{RST} → 再扫一次")
        print(f"      {DIM}只对文件名是番号（ABC-123 这种）的片子有效；")
        print(f"      普通电影电视剧交给 Emby 自带的 TMDb，别在同一个库里混着开。{RST}")
        print(f"      {DIM}装/卸：3 后补参数 → 5{RST}")

    # 容器只报「几个在跑」。以前这里直接贴 docker compose ps 的原始输出,在手机上
    # 每行都折成三四行,IMAGE/COMMAND/PORTS 糊成一片,而真正要看的只有"跑没跑"。
    # 详细状态在「5 链路体检」里。
    print(f"\n  {BOLD}▸ 容器{RST}")
    want = ["emby", "openlist", "autofilm", "mediawarp"]
    if os.path.isdir(os.path.join(d, "homepage")):
        want.append("homepage")
    if metatube_on(d):
        want.append("metatube")
    r = sh("docker ps --format '{{.Names}}'", timeout=60)
    live = set((r.stdout or "").split())
    down = [n for n in want if n not in live]
    if not down:
        print(f"      {GREEN}{len(want)}/{len(want)} 在跑{RST}")
    else:
        print(f"      {YELLOW}{len(want) - len(down)}/{len(want)} 在跑"
              f"   没起来：{'、'.join(down)}{RST}")
        print(f"      {DIM}拉起来：media-stack start{RST}")

    # API Key 是空的话 302 根本不生效，这是最容易被忽略的一步，单独提醒
    try:
        with open(os.path.join(d, "mediawarp/config/config.yaml")) as f:
            if re.search(r"^\s*auth:\s*$", f.read(), re.M):
                print()
                warn("MediaWarp 的 Emby API Key 还是空的 —— 302 直链不会生效！")
                warn("去 Emby：设置 → 高级 → API 密钥 → 新建，然后重跑「1 安装」填进去。")
    except OSError:
        pass
    print()


def read_yaml_scalar(path, key, fallback=""):
    """从脚本自己生成的 yaml 里读一个标量值。

    故意不引 PyYAML —— 这脚本要能在只有 python3 标准库的裸机上跑。
    只认「key: value」这一种形态，读的又都是自己写出去的文件，够用。
    """
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                m = re.match(rf"^\s*{re.escape(key)}:\s*(.*)$", ln.split("#", 1)[0].rstrip())
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    return fallback


def read_yaml_all(path, key):
    """把 yaml 里所有 `key: value` 的值都读出来（多任务时 source_dir 会出现多次）。"""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                mm = re.match(rf"^\s*{re.escape(key)}:\s*(.*)$", ln.split("#", 1)[0].rstrip())
                if mm:
                    v = mm.group(1).strip().strip('"').strip("'")
                    if v and v not in out:
                        out.append(v)
    except OSError:
        pass
    return out


def rebuild_cfg_from_disk(d):
    """从落盘状态重建一份最小 cfg，够重新生成 nginx 站点和 Homepage 配置用。

    这样「更新」就能把脚本层面的改动（比如某个服务不再套 Basic Auth）应用上，
    不用重跑整轮安装问答 —— 那些问题的答案本来就都躺在磁盘上。
    """
    env_file = os.path.join(d, ".env")
    sec_file = os.path.join(d, ".secrets")
    cfg = {
        "install_dir": d,
        "domain":   read_env(env_file, "DOMAIN"),
        "data_root": read_env(env_file, "DATA_ROOT") or os.path.join(d, "media"),
        "ol_user":  "admin",
        "ol_pass":  read_env(sec_file, "OPENLIST_PASS", fallback=env_file),
        "ba_user":  read_env(sec_file, "BA_USER", fallback=env_file) or "media",
        "ba_pass":  read_env(sec_file, "BA_PASS", fallback=env_file),
        # 装了 Homepage 就有这个目录；容器可能被手动停掉，用目录判断更稳
        "homepage": os.path.isdir(os.path.join(d, "homepage", "config")),
        # MetaTube 用 compose 里有没有这个服务来判断，不看目录:关掉之后数据目录
        # 是留着的(里面是刮好的元数据缓存,再打开能直接接着用),看目录会误判成"装着"
        "metatube": metatube_on(d),
        "host_ip":  "",     # 只有无域名模式才用得上，下面按需取
    }
    # 这三个是装的时候用户填的，脚本没有别的地方存 —— 从上次生成的配置里读回来，
    # 「更新」才能重新生成 autofilm / mediawarp 的配置而不丢用户的输入。
    af = os.path.join(d, "autofilm", "config", "config.yaml")
    mw = os.path.join(d, "mediawarp", "config", "config.yaml")
    cfg["emby_api_key"] = read_yaml_scalar(mw, "auth")
    # 扫描路径：auto 这种「意图」从生成出来的 yaml 里读不回来（里面只有展开后的结果），
    # 所以存在状态文件里。老机器没有这个键，就退回去读 yaml 里已有的 source_dir。
    spec = ms_state().get("scan_spec")
    if spec == SCAN_AUTO:
        cfg["scan_spec"] = SCAN_AUTO
    elif isinstance(spec, list) and spec:
        cfg["scan_spec"] = spec
    else:
        cfg["scan_spec"] = read_yaml_all(af, "source_dir") or ["/quark"]
    cfg["scan_paths"] = resolve_scan_paths(d, cfg["scan_spec"])
    cfg["strm_cron"]    = read_yaml_scalar(af, "cron", DEFAULT_STRM_CRON)
    # 只迁移「没被动过的旧默认值」：以前默认 0 0 5 * * *，而 AutoFilm 当时按 UTC 解释，
    # 对国内用户等于下午一点多在跑。现在调度器钉在北京时间、默认值改成 05:15，
    # 老机器更新时顺手带过去。用户自己改过 cron 的一律保持原样，不越俎代庖。
    if cfg["strm_cron"] == OLD_STRM_CRON:
        cfg["strm_cron"] = DEFAULT_STRM_CRON
    cfg["has_domain"] = bool(cfg["domain"]) and have("nginx")
    cfg["basic_auth"] = bool(cfg["ba_pass"]) and os.path.exists(HTPASSWD_FILE)
    cfg["ngx_port"] = detect_nginx_https_port()
    cfg["http2_directive"] = nginx_supports_http2_directive()
    cfg["crt"] = f"/etc/nginx/certs/{cfg['domain']}.crt" if cfg["domain"] else ""
    cfg["key"] = f"/etc/nginx/certs/{cfg['domain']}.key" if cfg["domain"] else ""
    if not cfg["has_domain"]:
        cfg["host_ip"] = public_ip()
    return cfg


# 拉镜像失败时的兜底源。按顺序试,第一个成功就停。
# 故意【不】改 /etc/docker/daemon.json 的 registry-mirrors —— 那要重启 dockerd,
# 会把这台机器上跑着的节点容器一起带下来,为了拉个镜像不值当。
# 这里的做法是直接从镜像站 docker pull,再 docker tag 回原名,compose 就能在本地找到。
# 这些都是第三方公益站,会挂会换;挂了不影响正常流程,只是兜底失效。
IMAGE_MIRRORS = [
    ("ghcr.io/",  ["ghcr.nju.edu.cn/", "ghcr.m.daocloud.io/"]),
    ("",          ["docker.m.daocloud.io/", "dockerproxy.net/", "docker.1ms.run/"]),
]


def compose_images(compose):
    """从 compose 文件里抠出所有 image: 值。不引 PyYAML,只认自己生成的格式。"""
    out = []
    try:
        with open(compose, encoding="utf-8") as f:
            for ln in f:
                m = re.match(r"^\s*image:\s*(\S+)\s*$", ln.split("#", 1)[0])
                if m and m.group(1) not in out:
                    out.append(m.group(1))
    except OSError:
        pass
    return out


def pull_via_mirror(image):
    """从镜像站拉一个镜像并 tag 回原名。成功返回用到的镜像站前缀，失败返回 ''。"""
    for prefix, mirrors in IMAGE_MIRRORS:
        if prefix and not image.startswith(prefix):
            continue
        body = image[len(prefix):]                  # ghcr.io/a/b -> a/b
        for mi in mirrors:
            cand = mi + body
            if sh(f"docker pull {cand}", timeout=900).returncode != 0:
                continue
            if sh(f"docker tag {cand} {image}", timeout=60).returncode == 0:
                sh(f"docker rmi {cand}", timeout=120)   # 只留原名那份,别占两份空间
                return mi
        break                                       # 前缀匹配上了就只试它那组
    return ""


def pull_images(compose, env_file):
    """拉镜像：先走正常渠道，失败了再逐个走镜像站兜底。全部成功返回 True。"""
    info("拉取最新镜像...")
    if subprocess.run(f"docker compose -f {compose} --env-file {env_file} pull",
                      shell=True, timeout=1800).returncode == 0:
        return True
    warn("直连拉取失败，改从镜像站逐个试...")
    failed = []
    for img in compose_images(compose):
        if sh(f"docker pull {img}", timeout=900).returncode == 0:
            continue                                # 这个其实能拉，只是刚才某一个拖垮了整批
        used = pull_via_mirror(img)
        if used:
            ok(f"{img}  {DIM}←{RST} {used}")
        else:
            failed.append(img)
    if failed:
        err("这几个镜像所有源都拉不下来：")
        for f in failed:
            print(f"     {f}")
        print(f"  {DIM}镜像站是第三方公益服务，会挂会换。可以过一阵再试，"
              f"或者手动 docker pull 之后再跑更新。{RST}")
        return False
    ok("镜像已就绪（部分走了镜像站）")
    return True


def self_update():
    """把脚本自己换成仓库里的最新版；换过了返回 True，调用方应立刻 re-exec。

    没有这一步，「更新」只是按【这台机器上现有的那份脚本】重新生成一遍配置 ——
    仓库里修好的东西永远到不了机器上。之前 Homepage 的健康检查地址在仓库里改了
    两轮，用户那边 services.yaml 却一字未变，就是卡在这里。

    URL 上带时间戳绕开 CDN 缓存：raw.githubusercontent 和各家镜像都会缓存几分钟
    到几小时，不绕开的话「刚推的修复」拉下来还是旧的，看起来就像改了没用。
    """
    me = os.path.realpath(__file__)
    if not os.access(me, os.W_OK):
        return False
    try:
        req = urllib.request.Request(f"{SELF_URL}?_t={int(time.time())}",
                                     headers={"User-Agent": "media-stack"})
        body = urllib.request.urlopen(req, timeout=20).read().decode()
    except Exception as e:
        warn(f"拉取最新脚本失败（{e}）")
        print(f"  {DIM}继续用本机这份 v{SCRIPT_VERSION} 刷新配置。{RST}")
        return False
    # 只接受长得像本脚本的内容，别把一页 404/限流提示写进去，那会直接废掉这个文件
    if "SCRIPT_VERSION" not in body or len(body) < 10000:
        warn("拉到的内容不像 media-stack.py，已忽略，继续用本机这份。")
        return False
    try:
        if body == open(me, encoding="utf-8").read():
            return False
    except OSError:
        return False
    m = re.search(r'SCRIPT_VERSION\s*=\s*"([^"]+)"', body)
    tmp = me + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(tmp, 0o755)
    os.replace(tmp, me)          # 先写临时文件再原子替换，中途断网也不会留个半截脚本
    ok(f"脚本已更新：v{SCRIPT_VERSION} → v{m.group(1) if m else '?'}，用新版继续...")
    return True


def do_update():
    """更新：脚本自身 + 镜像 + 按新脚本重新生成配置。用户数据和密码都不动。"""
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return

    if self_update():
        me = os.path.realpath(__file__)
        os.execv(sys.executable, [sys.executable, me, "update"])

    compose = os.path.join(d, "docker-compose.yml")
    env_file = os.path.join(d, ".env")

    # 镜像拉不动就跳过，不再 return —— 跨境网络本来就时好时坏，
    # 而下面重刷配置才是修 bug 的那一步，不该被一次拉取失败连坐掉。
    if not pull_images(compose, env_file):
        warn("镜像没拉全，跳过换镜像，继续刷新配置。")
    else:
        info("用新镜像重启容器...")
        r = subprocess.run(f"docker compose -f {compose} --env-file {env_file} up -d",
                           shell=True, timeout=900)
        if r.returncode != 0:
            warn("用新镜像重启失败，看上面的报错；配置照常刷新。")
        else:
            ok("镜像已更新")
            sh("docker image prune -f", timeout=300)

    # 再把脚本生成的那几份配置按当前版本重刷一遍。
    #
    # mediawarp / autofilm 的配置以前是不碰的,理由是"里面有用户填的东西"。
    # 那个理由站不住:用户填的就三样(Emby API Key、网盘挂载路径、cron),
    # 全都能从上次生成的文件里读回来。代价却是配置层面的 bug 永远修不到已装的
    # 机器上 —— strm 的 mode 填错、raw_url 填错,都是改完仓库、用户更新完
    # 依旧原样。所以改成:读回用户的三个值,然后整份重新生成。
    cfg = rebuild_cfg_from_disk(d)
    mw_cfg = os.path.join(d, "mediawarp", "config", "config.yaml")
    af_cfg = os.path.join(d, "autofilm", "config", "config.yaml")
    for path, gen, svc in ((mw_cfg, gen_mediawarp_conf, "mediawarp"),
                           (af_cfg, gen_autofilm_conf, "autofilm")):
        if not os.path.exists(path):
            continue
        info(f"按当前版本重新生成 {svc} 配置...")
        with open(path, "w") as f:
            f.write(gen(cfg))
        subprocess.run(["docker", "restart", svc], capture_output=True)
    if os.path.exists(mw_cfg) and not cfg["emby_api_key"]:
        warn("MediaWarp 的 Emby API Key 是空的，302 直链不会生效。")
        warn("用「3 后补参数 → 添加 API 密钥」补上。")

    # CLI 也要跟着换:它是脚本生成的,里面的命令逻辑会随版本变
    # (比如 strm 从"只重启容器"改成"真的跑一次任务")。不重装一遍就拿不到。
    install_cli(d)
    install_keepalive(d)      # 保活定时任务也跟着换新（路径/频率可能变）

    if cfg["homepage"]:
        info("刷新 Homepage 导航配置...")
        hp = os.path.join(d, "homepage", "config")
        s, sv, w = gen_homepage_conf(cfg)
        open(os.path.join(hp, "settings.yaml"), "w").write(s)
        open(os.path.join(hp, "services.yaml"), "w").write(sv)
        open(os.path.join(hp, "widgets.yaml"), "w").write(w)
        subprocess.run(["docker", "restart", "homepage"], capture_output=True)
        ok("Homepage 配置已刷新")

    if cfg["has_domain"] and os.path.exists(NGX_SITE):
        if not (os.path.exists(cfg["crt"]) and os.path.exists(cfg["key"])):
            warn(f"证书文件不在 {cfg['crt']}，跳过 nginx 配置刷新。")
        else:
            info("按当前版本重新生成 nginx 站点配置...")
            apply_nginx_site(cfg)     # 内部已带 nginx -t + 失败回滚

    # 总览表也重刷一遍，免得 emby / media-stack 命令显示的还是旧版式
    cred = os.path.join(d, "CREDENTIALS.txt")
    if os.path.exists(cred):
        with open(cred, "w") as f:
            f.write(build_summary(cfg, colored=False) + "\n")
        os.chmod(cred, 0o600)

    # 更新过程重启了所有容器，这一步顺便验证网盘还通不通 —— 更新完立刻发现
    # 网盘挂了，总比晚上想看片时才发现强。
    # 注意：这【不是】"把链路热好"。实测耗时和空闲时间不相关（见 do_keepalive
    # 的文档字符串），所以别在这里承诺"现在去播不会卡"。
    info("顺便测一下网盘链路...")
    do_keepalive()
    ka = keepalive_state(d)
    if ka.get("ok"):
        ok(f"网盘链路正常（列目录 {ka.get('elapsed', 0)}s）")
    elif ka:
        warn(f"网盘暂时不通：{ka.get('error', '')}")
        print(f"  {DIM}和这次更新无关，是线路问题。保活每 {KEEPALIVE_MIN} 分钟会自己重试。{RST}")

    print()
    ok(f"更新完成（脚本 v{SCRIPT_VERSION}）：镜像、nginx 站点、导航面板都已是当前版本")
    print(f"  {DIM}Emby API Key、网盘挂载路径、cron 这些你填的东西没有被动过。{RST}")


def do_uninstall():
    require_root()
    install_dir = ms_install_dir()
    if not is_installed(install_dir):
        install_dir = ask("安装目录", install_dir)
    compose = os.path.join(install_dir, "docker-compose.yml")
    print()
    warn("将要删除：媒体栈的容器、nginx 站点配置、media-stack/emby 命令。")
    warn("节点(bgpeer)的任何文件都不会被碰。")
    print(f"  {DIM}输入 {RST}{RED}{BOLD}yes{RST}{DIM} 确认；回车 / n / 其它任何输入都是取消。{RST}")
    # 卸载是不可逆操作，故意不接受 y —— 必须完整打出 yes，避免手滑
    if ask("确认卸载？").strip().lower() != "yes":
        print("已取消，什么都没动。")
        return

    if os.path.exists(compose):
        sh(f"docker compose -f {compose} down", timeout=300)
    else:
        for c in ("emby", "openlist", "autofilm", "mediawarp", "homepage"):
            sh(f"docker rm -f {c}")
    ok("容器已删除")

    if os.path.exists(NGX_SITE):
        os.remove(NGX_SITE)
        if sh("nginx -t").returncode == 0:
            nginx_reload()
            ok("已移除 nginx 站点配置")
        else:
            err("移除站点后 nginx -t 不通过，请检查（这不该发生）。")
    for p in (HTPASSWD_FILE, CLI_PATH, CLI_ALIAS, MS_STATE, KEEPALIVE_CRON):
        if os.path.islink(p) or os.path.exists(p):
            os.remove(p)
    ok("已移除密码文件和管理命令")

    if ask_yn(f"删除 {install_dir}（配置和 strm 全部丢失，不可逆）？", False):
        if ask("再次确认：输入 DELETE 删除") == "DELETE":
            shutil.rmtree(install_dir, ignore_errors=True)
            ok(f"已删除 {install_dir}")
        else:
            warn("确认词不符，保留数据。")
    else:
        warn(f"保留 {install_dir}，以后可重新安装复用。")
    ok("卸载完成。节点未受影响。")


# ============================================================================ 主流程
def main():
    require_root()
    print()
    print("  ┌────────────────────────────────────────────────────┐")
    print("  │   自建 Emby · 网盘直链媒体服务器                   │")
    print("  │   Emby + OpenList + AutoFilm + MediaWarp（302）    │")
    print("  └────────────────────────────────────────────────────┘")
    print(f"  {DIM}v{SCRIPT_VERSION} · 与节点共存，只读节点配置，绝不修改{RST}")
    print()

    ensure_docker()
    hr()

    state = node_state()
    cfg = {}

    # ---- 域名：节点装过就直接用它的，不再问一遍 ----
    node_domain = (state.get("domain") or "").strip()
    if node_domain:
        ok(f"检测到节点域名：{node_domain}")
        cfg["domain"] = node_domain if ask_yn("媒体服务也用这个域名？", True) \
            else ask("你的域名（填 example.com 或 emby.example.com 都行）", "")
    else:
        cfg["domain"] = ask("你的域名（没有就留空，用 IP:端口 访问）", "")

    # 填了 emby.example.com 这种就静默归一 —— 用户填子域名本来就是合理表达
    if cfg["domain"]:
        first, _, rest = cfg["domain"].partition(".")
        if rest and first in ("emby", "list", "home"):
            cfg["domain"] = rest
            ok(f"按根域名 {rest} 处理")

    cfg["has_domain"] = bool(cfg["domain"]) and have("nginx")
    if cfg["domain"] and not have("nginx"):
        warn("没检测到 nginx，改用 IP:端口 模式。")

    # ---- 安装目录等 ----
    cfg["install_dir"] = ask("安装目录（存放配置）", DEFAULT_DIR)
    cfg["data_root"] = ask("媒体目录（存放 strm 和字幕）",
                           cfg["install_dir"] + "/media")
    cfg["tz"] = ask("时区", "Asia/Shanghai")
    cfg["puid"] = ask("PUID", "0")
    cfg["pgid"] = ask("PGID", "0")
    cfg["homepage"] = ask_yn("装 Homepage 导航面板（推荐）？", True)
    hr()

    # ---- 网盘 ----
    print(f"  {DIM}扫描路径：可以填一条（/quark）、多条用逗号隔开（/quark,/aliyun），{RST}")
    print(f"  {DIM}或者填 {RST}{BOLD}y{RST}{DIM} 表示自动跟随 OpenList 里已挂载的全部存储"
          f"（以后加网盘不用回来改）。{RST}")
    while True:
        cfg["scan_spec"] = parse_scan_spec(ask("扫描路径", "/quark"))
        if cfg["scan_spec"]:
            break
        warn("填一条路径、多条逗号隔开、或者 y（自动）。")
    # 这一步 OpenList 还没挂任何网盘,auto 现在展开必然是空的 —— 那不是错,
    # 装完加了存储再点「4 生成媒体库」就会带上。这里只是先把意图记下来。
    cfg["scan_paths"] = resolve_scan_paths(cfg["install_dir"], cfg["scan_spec"])
    print(f"  {DIM}下面这个时刻按【北京时间】算（调度器已钉在 {AUTOFILM_TZ}），"
          f"和服务器在哪无关。{RST}")
    cfg["strm_cron"] = ask("strm 生成 cron（6 位：秒 分 时 日 月 周）", DEFAULT_STRM_CRON)
    print()
    warn("MediaWarp 需要 Emby 的 API Key。首次部署时 Emby 还没初始化，这里可以留空；")
    warn("装完按提示去 Emby 生成，再重跑本脚本填上即可（幂等，重跑安全）。")
    cfg["emby_api_key"] = ask("Emby API Key（没有就直接回车）", "")
    hr()

    env_file = os.path.join(cfg["install_dir"], ".env")
    # 密码单独放,不进 .env —— docker compose 读 .env 时会做变量插值,
    # 密码里出现 $ 会被当成变量引用,轻则告警重则报 invalid interpolation
    secret_file = os.path.join(cfg["install_dir"], ".secrets")
    cfg["ol_user"] = "admin"
    cfg["ol_pass"] = keep_or_new(secret_file, "OPENLIST_PASS", fallback=env_file)
    cfg["ba_user"] = read_env(secret_file, "BA_USER", fallback=env_file) or "media"
    cfg["ba_pass"] = read_env(secret_file, "BA_PASS", fallback=env_file)
    cfg["basic_auth"] = False
    cfg["host_ip"] = public_ip()

    # ---- nginx / 证书 ----
    cfg["ngx_port"] = SNI_HTTPS_PORT_FALLBACK
    cfg["http2_directive"] = False
    cfg["crt"] = f"/etc/nginx/certs/{cfg['domain']}.crt" if cfg["domain"] else ""
    cfg["key"] = f"/etc/nginx/certs/{cfg['domain']}.key" if cfg["domain"] else ""
    cf_token = ""
    write_nginx = False

    if cfg["has_domain"]:
        cfg["ngx_port"] = detect_nginx_https_port()
        cfg["http2_directive"] = nginx_supports_http2_directive()
        if cfg["ngx_port"] != 443:
            ok(f"https 站点在 127.0.0.1:{cfg['ngx_port']}（节点的 SNI 分流架构），"
               f"新站点监听同一端口")
        write_nginx = ask_yn("要自动配好 nginx 反代和证书吗（只新增 conf.d 文件）？", True)

        if write_nginx:
            if acme_has_cf_creds():
                ok("acme.sh 里已有 Cloudflare 凭据，直接复用，不用再建 Token")
            else:
                print()
                print("  签证书需要一个 Cloudflare API Token（只用这一次，之后 acme.sh 自己记住）：")
                print("    CF 后台 → 我的个人资料 → API 令牌 → 创建令牌 → 「编辑区域 DNS」模板")
                print("    权限要有：区域→DNS→编辑、区域→区域→读取（少了第二条会失败）")
                cf_token = ask("Cloudflare API Token（可留空则手填已有证书路径）", "")
                if not cf_token:
                    cfg["crt"] = ask("证书 fullchain 路径", cfg["crt"])
                    cfg["key"] = ask("证书私钥路径", cfg["key"])
            print()
            warn("Homepage 是零认证的，挂上公网后知道域名的人就能看到你全部服务地址。")
            cfg["basic_auth"] = ask_yn("给它加一道密码？（Emby / OpenList 有自己的账号，不套）", True)
            if cfg["basic_auth"]:
                cfg["ba_user"] = ask("登录用户名", cfg["ba_user"])
                print(f"  {DIM}密码留空=自动生成随机的（更安全）；想自己定就直接输。{RST}")
                typed = ask("登录密码（留空则随机生成）", "")
                # 优先级：这次手填的 > 上次存下来的 > 新随机生成的
                if typed:
                    cfg["ba_pass"] = typed
                elif not cfg["ba_pass"]:
                    cfg["ba_pass"] = rand_pw(16)
        hr()

    # ---- 建目录 ----
    info("创建目录结构...")
    for svc in ("emby", "openlist", "autofilm", "mediawarp", "homepage"):
        os.makedirs(os.path.join(cfg["install_dir"], svc, "config"), exist_ok=True)
    for extra in ("autofilm/logs", "mediawarp/logs", "mediawarp/custom"):
        os.makedirs(os.path.join(cfg["install_dir"], extra), exist_ok=True)
    os.makedirs(os.path.join(cfg["data_root"], "strm", STRM_SUBDIR), exist_ok=True)
    # OpenList v4.1.0 起镜像不再认 PUID/PGID，改为固定 uid；用 user: 指定身份后
    # data 目录必须归它所有，否则 entrypoint 检测到无写权限会直接退出
    sh(f"chown -R {cfg['puid']}:{cfg['pgid']} "
       f"{os.path.join(cfg['install_dir'], 'openlist', 'config')}")
    ok(f"目录就绪：配置在 {cfg['install_dir']}，媒体在 {cfg['data_root']}")

    # ---- 写 .env / compose / 各服务配置 ----
    with open(env_file, "w") as f:
        f.write(f"""PUID={cfg['puid']}
PGID={cfg['pgid']}
TZ={cfg['tz']}
DATA_ROOT={cfg['data_root']}
DOMAIN={cfg['domain']}
""")
    os.chmod(env_file, 0o600)

    # 密码写进独立的 .secrets（不给 docker compose 读），重跑时靠它沿用
    with open(secret_file, "w") as f:
        f.write("# 由 media-stack.py 生成，供重跑时沿用已有密码。别手改。\n"
                f"OPENLIST_PASS={cfg['ol_pass']}\n"
                f"BA_USER={cfg['ba_user']}\n"
                f"BA_PASS={cfg['ba_pass']}\n")
    os.chmod(secret_file, 0o600)

    with open(os.path.join(cfg["install_dir"], "docker-compose.yml"), "w") as f:
        f.write(gen_compose(cfg))
    with open(os.path.join(cfg["install_dir"], "autofilm/config/config.yaml"), "w") as f:
        f.write(gen_autofilm_conf(cfg))
    with open(os.path.join(cfg["install_dir"], "mediawarp/config/config.yaml"), "w") as f:
        f.write(gen_mediawarp_conf(cfg))
    os.chmod(os.path.join(cfg["install_dir"], "mediawarp/config/config.yaml"), 0o600)
    os.chmod(os.path.join(cfg["install_dir"], "autofilm/config/config.yaml"), 0o600)
    if cfg["homepage"]:
        hp = os.path.join(cfg["install_dir"], "homepage/config")
        s, sv, w = gen_homepage_conf(cfg)
        open(os.path.join(hp, "settings.yaml"), "w").write(s)
        open(os.path.join(hp, "services.yaml"), "w").write(sv)
        open(os.path.join(hp, "widgets.yaml"), "w").write(w)
        for empty in ("bookmarks.yaml", "docker.yaml"):
            open(os.path.join(hp, empty), "w").close()
    ok("配置文件已生成")
    if not cfg["emby_api_key"]:
        warn("Emby API Key 为空，MediaWarp 暂时不会生效 —— 装完按提示补上再重跑。")

    # ---- 起容器 ----
    hr()
    info("拉取镜像并启动（首次较慢，取决于网络）...")
    r = subprocess.run(
        f"cd {cfg['install_dir']} && docker compose --env-file {env_file} up -d",
        shell=True, timeout=1800)
    if r.returncode != 0:
        err("容器启动失败。看上面的报错；内存/磁盘不足是常见原因。")
    else:
        ok("容器已启动")

    # ---- OpenList 密码 ----
    info("为 OpenList 设置管理员密码...")
    done = False
    for _ in range(20):
        if subprocess.run(["docker", "exec", "openlist", "./openlist",
                           "admin", "set", cfg["ol_pass"]],
                          capture_output=True).returncode == 0:
            done = True
            break
        time.sleep(3)
    if done:
        ok("OpenList 管理员密码已设置")
    else:
        warn("自动设置 OpenList 密码失败（容器可能还在初始化）。")
        warn("配置里已写入这个密码，手动跑下面一行就能对上：")
        print(f"     docker exec openlist ./openlist admin set '{cfg['ol_pass']}'")

    # ---- nginx + 证书 ----
    if cfg["has_domain"] and write_nginx:
        hr()
        if cf_token or acme_has_cf_creds():
            info(f"签发 *.{cfg['domain']} 泛域名证书...")
            issue_cert(cfg["domain"], cfg["crt"], cfg["key"], cf_token)
        if not (os.path.exists(cfg["crt"]) and os.path.exists(cfg["key"])):
            err(f"证书文件不存在（{cfg['crt']}），跳过 nginx 配置生成。")
        else:
            if cfg["basic_auth"] and not write_htpasswd(cfg["ba_user"], cfg["ba_pass"]):
                warn("生成密码文件失败（没有 openssl？），跳过统一密码。")
                cfg["basic_auth"] = False
            apply_nginx_site(cfg)

    # ---- 管理命令 ----
    install_cli(cfg["install_dir"])
    install_keepalive(cfg["install_dir"])
    # 记住装在哪（菜单里的 2/3/4 就不用再问），以及扫描路径的意图 ——
    # auto 从生成出来的 yaml 里读不回来，只能存在这
    save_ms_state(cfg["install_dir"], scan_spec=cfg["scan_spec"])

    # ---- 总览 ----
    cred_file = os.path.join(cfg["install_dir"], "CREDENTIALS.txt")
    with open(cred_file, "w") as f:
        f.write(build_summary(cfg, colored=False) + "\n")
    os.chmod(cred_file, 0o600)

    print()
    print(build_summary(cfg, colored=True))
    hr()
    ok(f"部署完成！凭据已存到 {BOLD}{cred_file}{RST}（chmod 600）")
    print()
    print(f"  以后敲 {BOLD}emby{RST} 甩出面板地址，敲 {BOLD}media-stack{RST} 看全部。")
    print()
    warn("接下来这几步脚本代劳不了（顺序不能乱）：")
    want = ("你在 OpenList 里挂的存储" if cfg["scan_spec"] == SCAN_AUTO
            else "、".join(cfg["scan_paths"]) or "你填的那条路径")
    print(f"  1) OpenList → 存储 → 添加，挂载路径填 {BOLD}{want}{RST}")
    print(f"     {DIM}阿里云盘 / 115 / 天翼这些 OpenList 支持的网盘都可以，本套东西不挑驱动。{RST}")
    print(f"     {YELLOW}夸克要选 {BOLD}QuarkTV{RST}{YELLOW}，不是 Quark{RST}"
          f"{DIM}（普通 Quark 驱动不支持 302，只能本机中转）{RST}")
    print(f"     {YELLOW}根文件夹ID 必须填 {BOLD}0{RST}{DIM}（填 / 或留空会返回空目录）{RST}")
    print(f"     保存 → 用网盘手机 App 扫码 → 扫完把该存储{BOLD}先禁用再启用{RST}，token 才生效")
    print("  2) 打开 Emby 完成首次安装向导 → 设置 → 高级 → API 密钥 → 新建并复制")
    print("  3) 重跑本脚本，在「Emby API Key」那步粘贴进去")
    print(f"  4) 重跑本脚本 → {BOLD}4 生成媒体库{RST}（也可以敲 media-stack strm）")
    print(f"  5) Emby 添加媒体库，路径指向 {BOLD}{STRM_PATH}{RST}")
    print(f"  6) {YELLOW}重要{RST}：该媒体库高级设置里关掉「章节图像提取」和「实时监控」，")
    print("     否则 Emby 会为了截图去拉整部影片，把网盘刷到限流。")
    print()
    print(f"  验证直链：{BOLD}media-stack 302{RST} 然后播一集，看到 302 就说明流量没走本机。")
    print()
    warn("Emby 的 8096 已收进 127.0.0.1，对外只能走 MediaWarp。直连 8096 会绕过 302。")


def mediawarp_conf_path(install_dir=None):
    return os.path.join(install_dir or ms_install_dir(), "mediawarp/config/config.yaml")


def read_emby_api_key(install_dir=None):
    """从 MediaWarp 配置里读当前的 API Key，读不到或为空都返回空串。"""
    try:
        with open(mediawarp_conf_path(install_dir)) as f:
            m = re.search(r"^\s*auth:[ \t]*(\S*)", f.read(), re.M)
            return m.group(1) if m and not m.group(1).startswith("#") else ""
    except OSError:
        return ""


def set_emby_api_key():
    """单独补 Emby API Key。

    这是装完之后唯一必须回头再填的东西（首次部署时 Emby 还没初始化，拿不到 Key），
    为它重跑一整轮安装问答太笨，所以单开一个入口。
    """
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    path = mediawarp_conf_path(d)
    if not os.path.exists(path):
        err(f"找不到 {path}，先跑一次「1 安装」。")
        return

    cur = read_emby_api_key(d)
    print()
    if cur:
        # 已经填过就先拦一道 —— 直接把光标丢给用户很容易手滑覆盖掉正在用的 Key
        print(f"  当前已填：{CYAN}{BOLD}{cur}{RST}")
        if not ask_yn("已经填过了，要更换吗？", False):
            print("保持不变。")
            return
    else:
        print(f"  {YELLOW}当前为空 —— 302 直链不会生效。{RST}")
    print(f"  {DIM}到哪拿：Emby → 设置 → 高级 → API 密钥 → 新建 → 复制那串字符{RST}")

    # 循环到拿到一个能用的值为止，输错不用从菜单重来
    key = ""
    while True:
        key = ask("Emby API Key（留空取消）").strip()
        if not key:
            print("已取消，保持原样。")
            return
        # Emby 的 Key 是 32 位十六进制。不符也放行（版本差异），但提醒一句，
        # 免得把「新建 API 密钥」旁边的应用名之类的东西粘进来还查不出原因。
        if re.fullmatch(r"[0-9a-fA-F]{32}", key):
            break
        warn(f"这串是 {len(key)} 个字符，不像 Emby 的 API Key（通常 32 位十六进制）。")
        if ask_yn("仍然使用它？", False):
            break
        print(f"  {DIM}那就重新贴一次，或直接回车取消。{RST}")

    lines = open(path).read().splitlines()
    hit = False
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*auth:)(.*)$", ln)
        if not m:
            continue
        # 保留原有缩进和行尾注释，只换值
        cm = re.search(r"(\s+#.*)$", m.group(2))
        lines[i] = f"{m.group(1)} {key}{cm.group(1) if cm else ''}"
        hit = True
        break
    if not hit:
        err(f"{path} 里没找到 auth: 这一行，没敢乱改。请手动填。")
        return
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    ok("API Key 已写入 MediaWarp 配置")

    info("重启 MediaWarp...")
    if subprocess.run(["docker", "restart", "mediawarp"],
                      capture_output=True).returncode != 0:
        err("重启失败，MediaWarp 可能没在跑。用「3 更新」或 media-stack start 拉起来。")
        return
    time.sleep(3)
    ok("MediaWarp 已重启")
    print()
    print(f"  验证：{BOLD}media-stack 302{RST} 然后播一集，日志出现 302 就说明直链生效了。")


def strm_root(d):
    return os.path.join(read_env(os.path.join(d, ".env"), "DATA_ROOT")
                        or os.path.join(d, "media"), "strm")


def strm_count(d):
    """本地已生成的 .strm 数量。0 就意味着 Emby 里一定是空的。"""
    n = 0
    for _dirpath, _dirnames, files in os.walk(strm_root(d)):
        n += sum(1 for f in files if f.endswith(".strm"))
    return n


def emby_scan_wait(key, timeout=600):
    """让 Emby 扫一次媒体库并【等它扫完】。返回是否确认扫完。

    必须等:迁移时要靠「先扫一次看到文件没了」来让 Emby 真正删掉旧条目,
    没等完就去重新生成的话,Emby 一次扫描里同时看到删和加,会当成没变过,
    旧条目的错误媒体信息就留下来了 —— 那正是这次要修的东西。
    """
    if not key:
        return False

    def scan_task():
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:8096/ScheduledTasks?api_key={key}", timeout=30) as r:
                return next((x for x in json.load(r)
                             if x.get("Key") == "RefreshLibrary"), None)
        except Exception:
            return None

    # 先记下上一次执行的结束时间。只靠「State 从 Running 变回 Idle」是不够的:
    # 小媒体库一两秒就扫完了,轮询第一次去看时任务早就 Idle,于是永远等不到那个
    # 状态跳变,只能干等到超时(实测 60 个文件的库就这样卡满 10 分钟)。
    # 结束时间变了同样说明这一轮跑完了,两个条件哪个先成立都算数。
    before = ((scan_task() or {}).get("LastExecutionResult") or {}).get("EndTimeUtc", "")
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:8096/Library/Refresh?api_key={key}", method="POST"),
            timeout=30).close()
    except Exception:
        return False

    deadline = time.time() + timeout
    seen_running = False
    while time.time() < deadline:
        time.sleep(3)
        t = scan_task()
        if t is None:
            continue
        if t.get("State") != "Idle":
            seen_running = True
            continue
        end = (t.get("LastExecutionResult") or {}).get("EndTimeUtc", "")
        if seen_running or (end and end != before):
            return True
    return False


def _emby(path, key, method="GET", timeout=60, body=None):
    u = f"http://127.0.0.1:8096{path}{'&' if '?' in path else '?'}api_key={key}"
    req = urllib.request.Request(
        u, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = r.read()
    return json.loads(out) if out.strip() else {}


def items_without_duration(key):
    """Emby 里还没探测出时长的影视条目 [(id, 名字), ...]。

    枚举方式是照抄 Emby 网页端自己发的请求:ParentId 填【媒体库条目 id】
    (VirtualFolders 里的 ItemId)、带 Recursive 和 IncludeItemTypes。
    少了 IncludeItemTypes 的话 Emby 会把媒体库节点本身当结果返回,看起来就像
    "库里只有一个条目",排查时会被带到沟里去。
    """
    out = []
    try:
        libs = _emby("/Library/VirtualFolders", key)
    except Exception:
        return out
    uid = ""
    try:
        users = _emby("/Users", key)
        uid = (users[0] or {}).get("Id", "") if users else ""
    except Exception:
        pass
    if not uid:
        return out
    for lb in libs:
        pid = lb.get("ItemId")
        if not pid:
            continue
        try:
            d = _emby(f"/Users/{uid}/Items?ParentId={pid}&Recursive=true"
                      f"&IncludeItemTypes=Movie,Episode,Video&Fields=Path", key)
        except Exception:
            continue
        for i in d.get("Items") or []:
            if not (i.get("RunTimeTicks") or 0):
                out.append((uid, i.get("Id"), i.get("Name") or "?"))
    return out


RESUME_MIN_SECONDS = 10     # 播够多少秒才值得记续播点
RESUME_MIN_PCT     = 1      # 播到百分之几才值得记


def tune_resume_thresholds(key):
    """把媒体库的续播门槛调到对短片子也公平的值。

    Emby 每个媒体库有三个续播参数,默认值是按"电影"设计的:
        MinResumeDurationSeconds  120   播放位置不到 120 秒就不记
        MinResumePct                2   不到总时长 2% 不记
        MaxResumePct               90   超过 90% 判定看完,清掉续播点
    网盘库里什么长度都有。一个 1 分多钟的片子,播放位置【永远】到不了 120 秒,
    于是永远没有续播记忆 —— 用户看到的是"长的能记住、短的记不住",像是坏了,
    其实是规则如此。而这三个值 Emby 的网页设置里不给改,只能走接口。

    只动指向本脚本 strm 目录的媒体库:用户自己另外建的库(本地电影、音乐之类)
    不该被这个脚本碰。MaxResumePct 也不动 —— 那条是按比例算的,长短本来就公平。
    """
    try:
        libs = _emby("/Library/VirtualFolders", key)
    except Exception:
        return
    for lb in libs:
        if not any(STRM_PATH in p or p in STRM_PATH for p in (lb.get("Locations") or [])):
            continue
        o = lb.get("LibraryOptions") or {}
        cur_s = o.get("MinResumeDurationSeconds")
        cur_p = o.get("MinResumePct")
        if cur_s == RESUME_MIN_SECONDS and cur_p == RESUME_MIN_PCT:
            continue
        o["MinResumeDurationSeconds"] = RESUME_MIN_SECONDS
        o["MinResumePct"] = RESUME_MIN_PCT
        try:
            _emby("/Library/VirtualFolders/LibraryOptions",
                  key, method="POST",
                  body={"Id": lb.get("ItemId"), "LibraryOptions": o})
        except Exception as e:
            warn(f"改「{lb.get('Name')}」的续播门槛失败：{_short_err(e)}")
            continue
        ok(f"媒体库「{lb.get('Name')}」续播门槛：{cur_s}秒/{cur_p}% → "
           f"{RESUME_MIN_SECONDS}秒/{RESUME_MIN_PCT}%")
        print(f"  {DIM}默认的 120 秒是按电影长度定的，短片子永远够不到，"
              f"表现为「长的记得住、短的记不住」。{RST}")


def _strm_host_path(d, item_path):
    """Emby 报的容器内路径 → 宿主机上的 strm 文件路径。"""
    if not item_path or not item_path.startswith(STRM_PATH):
        return ""
    return os.path.join(strm_root(d), STRM_SUBDIR, item_path[len(STRM_PATH):].lstrip("/"))


def heal_media_info(d, key):
    """给没有时长的条目补上媒体信息。进度条、续播、已看标记全靠这一步。

    背景：Emby 拿不到时长时，续播逻辑整个失效 —— 它按时长的百分比判断存不存续播点
    （MinResumePct / MaxResumePct），分母为 0 就直接判定「已看完」，续播点清零、
    进度条也拖不动（播放器以为总长是 0）。

    而 strm 的两种形态各有各的死穴：
      · 路径形式 /quark/…/x.mp4  —— 播放快（MediaWarp 的 alist_strm 有 2 小时直链
        缓存，命中时 3 毫秒 302），但 Emby 把它当本地文件喂 ffprobe，必然
        No such file or directory，探不出时长
      · URL 形式 https://…/d/…   —— Emby 能探测（播放时现拉一段文件头），但
        MediaWarp 只能用 http_strm 接管，那条路【没有直链缓存】，每次开播都要现
        换一次直链，实测 7.5~47 秒

    所以两种都不能常驻。这里的做法是「只在探测那几秒钟切过去」：

        ① 把该条目的 strm 临时写成带签名的 URL
        ② 发一次 IsPlayback=true 的 PlaybackInfo，逼 Emby 现在就探测
        ③ 确认时长真的入库
        ④ 立刻写回路径形式

    第 ④ 步之所以不会把时长弄丢，靠的是一个实测过的行为：Emby 对【已经存在的
    条目】不会重新探测 —— 改内容、全量刷新元数据、甚至重新播放三分钟都不会。
    当初这是拦路虎（老条目改了 strm 也自愈不了），现在正好拿来当依靠：媒体信息
    已经在 Emby 的数据库里，跟 strm 里写什么再无关系。

    副作用是这套东西自带修复能力：以后哪个条目时长丢了（比如手动点了「替换所有
    元数据」），再跑一次「生成媒体库」就会把它挑出来重探，用户不用知道发生过什么。
    """
    pend = items_without_duration(key)
    if not pend:
        return
    print()
    info(f"给 {len(pend)} 个条目补媒体信息（时长、编码）...")
    print(f"  {DIM}没有时长的话进度条拖不动、看一半退出会被当成看完。")
    print(f"  每个最多等 3 分钟，慢是正常的 —— 要真的去网盘拉一段文件头。{RST}")

    token = ""
    pw = read_env(os.path.join(d, ".secrets"), "OPENLIST_PASS",
                  fallback=os.path.join(d, ".env"))
    try:
        token = (_ol_api("/api/auth/login", {"username": "admin", "password": pw},
                         timeout=30).get("data") or {}).get("token", "")
    except Exception:
        pass
    if not token:
        warn("OpenList 登不上，没法生成带签名的地址，这一步跳过。")
        return

    cfg = rebuild_cfg_from_disk(d)
    base = openlist_public_url(cfg)
    done = 0
    for uid, iid, name in pend:
        try:
            it = _emby(f"/Users/{uid}/Items/{iid}", key, timeout=30)
        except Exception:
            continue
        host = _strm_host_path(d, it.get("Path") or "")
        if not host or not os.path.exists(host):
            continue
        try:
            original = open(host, encoding="utf-8").read()
        except OSError:
            continue
        p = strm_target_path(original)
        if not p:
            continue
        try:
            sign = ((_ol_api("/api/fs/get", {"path": p, "password": ""},
                             token, timeout=120).get("data") or {}).get("sign", ""))
        except Exception as e:
            print(f"  {DIM}·{RST} {name[:26]}  {YELLOW}取签名失败：{_short_err(e)}{RST}")
            continue
        url = base + "/d" + urllib.parse.quote(p) + (f"?sign={sign}" if sign else "")
        mins = 0
        try:
            # 临时切成 URL 形式 —— 只在这几秒钟里是这个样子
            with open(host, "w", encoding="utf-8") as f:
                f.write(url)
            try:
                _emby(f"/Items/{iid}/PlaybackInfo?UserId={uid}&IsPlayback=true"
                      f"&AutoOpenLiveStream=true&MediaSourceId=mediasource_{iid}"
                      f"&StartTimeTicks=0&MaxStreamingBitrate=200000000",
                      key, method="POST", timeout=200)
            except Exception:
                pass          # 探测本身超时也要走到 finally 把文件还原
            try:
                mins = (_emby(f"/Users/{uid}/Items/{iid}", key,
                              timeout=30).get("RunTimeTicks") or 0) / 6e8
            except Exception:
                mins = 0
        finally:
            # 还原必须发生：留在 URL 形式上的话，这个条目的播放就绕过了直链缓存，
            # 每次开播都要现换一次直链（实测 7.5~47 秒）
            try:
                with open(host, "w", encoding="utf-8") as f:
                    f.write(original if original.strip().startswith("/") else p)
            except OSError as e:
                err(f"{name[:26]} 的 strm 没还原成路径形式：{e}")
        if mins:
            done += 1
            print(f"  {GREEN}\u2714{RST} {name[:26]}  {mins:.0f} 分钟")
        else:
            print(f"  {DIM}\u00b7{RST} {name[:26]}  {YELLOW}没探到，下次生成媒体库会再试{RST}")
    if done == len(pend):
        ok(f"{done} 个条目补齐，进度条和续播可用")
    elif done:
        warn(f"{done}/{len(pend)} 个成功")
        print(f"  {DIM}没成功的多半是当时网盘那条线在抖。再点一次「4 生成媒体库」")
        print(f"  会只补没探到的那些，已经好的不重来。{RST}")
    else:
        warn("一个都没探到 —— 网盘接口现在多半不通，跑「5 链路体检」看看。")


def autofilm_clock():
    """AutoFilm 调度器当前认为的 (时, 分)。取不到返回 None。

    **不能用 `docker exec autofilm date`**：容器里的 date 认 TZ 环境变量，返回的是
    用户设的本地时间；而 AutoFilm 自己启动时打印的是「使用应用时区 timezone=UTC」
    —— 它没读到 TZ，回落到了 UTC。两者能差好几个小时。

    实测踩过：date 说 19:57，AutoFilm 认为现在是 11:56，于是 cron 写成
    "0 57 19 * * *" 要等到 19:57 UTC（次日凌晨）才触发，表现就是「点了没反应、
    卡住不动」。

    它日志时间戳末尾那个偏移量（+00:00 / +09:00 / Z）才是调度器真正用的那套时钟，
    从那里取偏移，再加到宿主机的 UTC 时间上，才和 cron 的解释方式对得上。
    """
    out = sh("docker logs --tail 80 autofilm", timeout=30)
    text = (out.stdout or "") + (out.stderr or "")
    off = None
    for m in re.finditer(r"T\d{2}:\d{2}:\d{2}(?:\.\d+)?(Z|[+-]\d{2}:\d{2})", text):
        off = m.group(1)                       # 取最后一条，最新
    if off is None:
        return None
    if off == "Z":
        mins = 0
    else:
        mins = (int(off[1:3]) * 60 + int(off[4:6])) * (1 if off[0] == "+" else -1)
    t = time.gmtime(time.time() + mins * 60)
    return t.tm_hour, t.tm_min


def do_strm():
    """立刻跑一次 strm 生成，跑完顺手让 Emby 扫一次媒体库。

    为什么必须有这个按钮：装完的那一刻网盘还没挂上 —— OpenList 里的存储得用户自己
    在网页里添加。所以安装流程里跑 strm 一定是空的，等用户挂好网盘之后，必须再触发
    一次生成，Emby 里才会出现片子。

    以前这一步只有命令行 `media-stack strm`。不看文档、不敲命令的人到这里就死局了：
    OpenList 里文件明明都在，Emby 里永远刷不出来，界面上没有任何东西提示还差一步。
    这是把人挡在门外的设计，不是用户的问题。

    触发方式和 CLI 那条一致：AutoFilm v2 没有手动执行的入口(--help 里只有
    --config/--log/--timezone 这类开关)，启动时也只注册 cron、不跑任务。所以临时把
    cron 改成两分钟后只触发一次，跑完立刻还原。不用「每分钟」是因为网盘慢的时候一轮
    要一两分钟，几轮压在一起会并发扫同一个目录、互相删对方刚写出的 strm。
    """
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    cfg_path = os.path.join(d, "autofilm", "config", "config.yaml")
    if not os.path.exists(cfg_path):
        warn(f"找不到 {cfg_path}，AutoFilm 可能没装。")
        return

    before = strm_count(d)
    print(f"\n  当前本地已有 {BOLD}{before}{RST} 个 strm 文件。")

    original = open(cfg_path, encoding="utf-8").read()
    hm = autofilm_clock()
    if hm is None:
        g = time.gmtime()
        hm = (g.tm_hour, g.tm_min)
        warn("读不到 AutoFilm 的时区，按 UTC 估算触发时刻。")
    t = (hm[0] * 60 + hm[1] + 2) % 1440
    fire = f"0 {t % 60:02d} {t // 60:02d} * * *"

    patched = re.sub(r'(?m)^(\s*cron:\s*)".*"$', lambda m: f'{m.group(1)}"{fire}"',
                     original, count=1)
    if patched == original:
        # 还没动过文件就退出，别进 try —— 否则 finally 会白重启一次容器
        err("没能改写 cron 那一行，为安全起见没有继续。")
        return

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(patched)
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if subprocess.run(["docker", "restart", "autofilm"],
                          capture_output=True).returncode != 0:
            err("重启 AutoFilm 失败，它可能没在跑。")
            return
        info(f"已安排在 {fire.split()[2]}:{fire.split()[1]}（容器时间）触发一次，最多等 2 分钟开始。")
        print(f"  {DIM}扫描期间日志会安静一阵，正常。整个过程最长 15 分钟。{RST}")

        done = ""
        for i in range(225):                       # 4s x 225 = 15 分钟封顶
            # 合并 stdout 和 stderr：不同版本的 AutoFilm/Docker 日志落在哪一路
            # 并不一致，只读 stdout 会漏掉统计数字（表现是最后那行全是问号）
            r = sh(f"docker logs --since {since} autofilm", timeout=60)
            # 先剥掉 ANSI 颜色码：AutoFilm 默认 --colorful-log，日志里的字段名被
            # 转义序列包着(strm_created_count 前后各有一段 \x1b[..m)，直接拿正则
            # 找 xxx_count=数字 一个都匹配不到，最后只能打出一排问号。
            # 这个坑很隐蔽：粘贴到聊天/文档里时终端把颜色码剥掉了，看起来完全干净。
            out = ANSI_RE.sub("", (r.stdout or "") + (r.stderr or ""))
            for ln in out.splitlines():
                if "Alist2Strm 任务完成" in ln:
                    done = ln                      # 不 break：取最后一条，也就是最新那轮
            if done:
                break
            time.sleep(4)
            if i % 15 == 14:
                print(f"  {DIM}...已等待 {(i + 1) * 4 // 60} 分钟{RST}")
        if not done:
            warn("15 分钟内没等到「任务完成」。网盘可能一直超时，稍后再试一次。")
    finally:
        # 还原是必须发生的：中途报错、Ctrl-C 都不能把用户的定时设置留在临时值上
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(original)
        subprocess.run(["docker", "restart", "autofilm"], capture_output=True)

    after = strm_count(d)
    print()
    if done:
        nums = dict(re.findall(r"([a-z_]+_count)=(\d+)", done))
        if nums:
            ok(f"生成完成：新增 {nums.get('strm_created_count', '?')}，"
               f"已存在跳过 {nums.get('strm_skipped_count', '?')}，"
               f"失败 {nums.get('failed_path_count', '?')}")
            # 扫到的目录数同样关键：网盘目录列不出来时它是 0,而"新增 0"看起来
            # 和"本来就没有新文件"一模一样,不把这两个数摆出来根本分不清
            print(f"  {DIM}扫描目录 {nums.get('scanned_dir_count', '?')} 个"
                  f"（跳过 {nums.get('skipped_dir_count', '?')} 个），"
                  f"发现文件 {nums.get('discovered_file_count', '?')} 个{RST}")
            if nums.get("skipped_dir_count", "0") != "0":
                warn(f"有 {nums['skipped_dir_count']} 个目录没列出来就被跳过了 —— "
                     f"网盘那边超时了，里面的文件这轮不会生成。再跑一次通常能补上。")
        else:
            # 解析不出来就把原始那行摆出来，别拿一排问号糊弄人
            ok("生成完成，AutoFilm 的统计行：")
            print(f"  {DIM}{done.strip()[-400:]}{RST}")
    print(f"  本地 strm：{before} → {BOLD}{after}{RST}")

    if after == 0:
        print()
        warn("一个 strm 都没生成，说明 OpenList 那边没读到网盘文件。检查：")
        print(f"  {DIM}·{RST} OpenList 里的存储状态是不是 work（看「2 使用信息」的网盘挂载那段）")
        print(f"  {DIM}·{RST} 夸克类驱动的根文件夹ID 必须填 {BOLD}0{RST}，填 / 或留空都会返回空目录")
        print(f"  {DIM}·{RST} AutoFilm 扫的是 {BOLD}{read_yaml_scalar(cfg_path, 'source_dir', '/')}{RST}，"
              f"这个路径在 OpenList 里点得开吗")
        return

    # 生成完顺手让 Emby 扫一遍，省得用户还要再进 Emby 后台找「扫描媒体库」
    key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"), "auth")
    if key:
        info("通知 Emby 扫描媒体库...")
        if emby_scan_wait(key, timeout=900):
            ok("Emby 已扫完")
        else:
            ok("已通知 Emby 扫描（后台进行，稍等片刻刷新 Emby 页面）")
        tune_resume_thresholds(key)
        heal_media_info(d, key)
    else:
        warn("没有 Emby API Key，没法自动触发扫描。去 Emby 后台手动扫一次媒体库。")
        print(f"  {DIM}填 API Key：「3 后补参数 → 添加 API 密钥」{RST}")

    print()
    print(f"  {BOLD}Emby 媒体库要指向的路径{RST}（容器内路径，不是宿主机路径）：")
    print(f"      {CYAN}{BOLD}{STRM_PATH}{RST}")
    print(f"  {DIM}Emby → 设置 → 媒体库 → 添加媒体库 → 内容类型选「电影」→ 文件夹填上面这个{RST}")
    # 刮不出海报的两个高频原因,都在建库那一屏,建完再回头改很麻烦
    print(f"  {YELLOW}同一屏里还要改两处，不然刮不出海报：{RST}")
    # 这里【只是提示】,脚本不碰 Emby 的媒体库设置(体检那段也只读不写)。
    # 语言不是硬规定:它决定刮回来的标题/简介用哪种语言显示,不限制能刮哪国的片子。
    # 会出事的只有「文件名是中文 + 语言按英文搜」这一种组合。
    print(f"  {DIM}·{RST} 首选语言{BOLD}别留空{RST}"
          f"{DIM} —— 留空按服务器默认（通常英文）去 TMDb 搜，中文片名一条都搜不到。{RST}")
    print(f"    {DIM}片名是中文就选中文/中国。这只影响标题简介显示成哪种语言，"
          f"不限制能刮哪国的片子，随时能在 Emby 里改。{RST}")
    print(f"  {DIM}·{RST} 片子文件名要像 {BOLD}流浪地球 (2019).mkv{RST}"
          f"{DIM} —— 带发布组标记的（[BT]xxx.1080p.WEB-DL-YYY）Emby 解析不出片名{RST}")


def write_secret(path, key, value):
    """在 .secrets 里就地改一个键，没有就追加。整文件重写会丢掉别的键。"""
    lines = []
    hit = False
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    for i, ln in enumerate(lines):
        if ln.startswith(key + "="):
            lines[i] = f"{key}={value}"
            hit = True
            break
    if not hit:
        lines.append(f"{key}={value}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def patch_credentials_file(install_dir, user, password):
    """同步 CREDENTIALS.txt 里的两行 —— emby / media-stack 命令都读它，
       不改的话敲 emby 看到的还是旧密码，比不显示更误导。"""
    p = os.path.join(install_dir, "CREDENTIALS.txt")
    try:
        with open(p) as f:
            txt = f.read()
    except OSError:
        return
    txt = re.sub(r"(?m)^(\s*用户名\s+).*$", lambda m: m.group(1) + user, txt)
    txt = re.sub(r"(?m)^(\s*密\s+码\s+).*$", lambda m: m.group(1) + password, txt)
    with open(p, "w") as f:
        f.write(txt)
    os.chmod(p, 0o600)


def set_web_credentials():
    """改浏览器弹框那层的账号密码（首页入口用的那个）。"""
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    sec = os.path.join(d, ".secrets")
    env = os.path.join(d, ".env")
    cur_user = read_env(sec, "BA_USER", fallback=env) or "media"
    cur_pass = read_env(sec, "BA_PASS", fallback=env)

    if not cur_pass or not os.path.exists(HTPASSWD_FILE):
        print()
        warn("当前没有启用统一密码（Homepage / OpenList 是裸奔的）。")
        warn("要启用：跑「1 安装」，在「给它们加一道统一密码」那步选是。")
        return

    print()
    print(f"  当前用户名：{CYAN}{BOLD}{cur_user}{RST}")
    print(f"  当前密码　：{CYAN}{BOLD}{cur_pass}{RST}")
    print(f"  {DIM}这是首页入口的弹框；Emby / OpenList 用它们自己的账号，不受影响。{RST}")
    if not ask_yn("要修改吗？", False):
        print("保持不变。")
        return

    new_user = ask("新用户名（回车＝不改）", cur_user).strip() or cur_user
    print(f"  {DIM}密码：回车＝不改；输 r ＝生成一个随机的；也可以直接输你要的。{RST}")
    typed = ask("新密码（回车不改）").strip()
    if typed == "r":
        new_pass = rand_pw(16)
    elif typed:
        new_pass = typed
    else:
        new_pass = cur_pass

    if new_user == cur_user and new_pass == cur_pass:
        print("没有任何变化。")
        return

    if not write_htpasswd(new_user, new_pass):
        err("生成密码文件失败（没有 openssl？），没有改动。")
        return
    write_secret(sec, "BA_USER", new_user)
    write_secret(sec, "BA_PASS", new_pass)
    patch_credentials_file(d, new_user, new_pass)
    nginx_reload()

    ok("已更新")
    print()
    print(f"      用户名  {CYAN}{BOLD}{new_user}{RST}")
    print(f"      密  码  {CYAN}{BOLD}{new_pass}{RST}")
    print(f"  {DIM}浏览器多半记着旧密码，下次弹框可能不弹 —— 用无痕窗口试，"
          f"或清一下该站点的登录状态。{RST}")


# ============================================================================ MetaTube
# 按番号刮削的 Emby 插件。跟这套网盘直链没有任何关系，纯粹是「Emby 拿到文件之后
# 怎么刮信息」那一层的补充，默认不装。
#
# 它是【两件东西】,少一件就是"装了但不工作":
#   · MetaTube Server —— 独立后端,插件自己不抓站,所有请求都转给它
#   · Emby 插件本体   —— 放进 Emby 的 plugins 目录
METATUBE_IMAGE = "ghcr.io/metatube-community/metatube-server:latest"
METATUBE_PORT  = 8080
METATUBE_API   = ("https://api.github.com/repos/metatube-community"
                  "/jellyfin-plugin-metatube/releases/latest")
# 下载地址在运行时查 Releases,不写死:写死的话人家发新版就断了。
# 同一个 release 里 Emby 和 Jellyfin 各一个包,按前缀挑 —— 实测资产名形如
#   Emby.MetaTube@v2025.1102.2200.0.zip
#   Jellyfin.MetaTube@v2025.1102.2200.0.zip
METATUBE_ASSET_PREFIX = "Emby."


def metatube_dir(d):
    return os.path.join(d, "metatube")


def emby_plugin_dir(d):
    """Emby 容器里的 /config/plugins 对应的宿主机目录。"""
    return os.path.join(d, "emby", "config", "plugins")


def metatube_on(d):
    """compose 里有没有 metatube 这个服务。"""
    try:
        with open(os.path.join(d, "docker-compose.yml"), encoding="utf-8") as f:
            return "container_name: metatube" in f.read()
    except OSError:
        return False


LINK_METHODS = {
    "download":  ("原画直链", "画质最好（网盘里是什么就播什么），但码率高；"
                            "跨境线路上 4K 原盘经常拉不动"),
    "streaming": ("转码流",   "网盘自己转码后的流，码率低一个量级，卡的时候选它；"
                            "转码在网盘那边做，不吃本机 CPU"),
}


def link_method_storages(d):
    """列出支持切换直链方式的存储：(id, 挂载点, 驱动, 当前值)。

    只有夸克/UC 的 TV 驱动有 link_method 这个字段，所以按「addition 里有没有这个键」
    来筛，而不是按驱动名硬编 —— 以后多几个驱动支持也能自动认出来。
    """
    db = os.path.join(d, "openlist", "config", "data.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("select id, mount_path, driver, addition "
                           "from x_storages order by mount_path").fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for sid, mp, drv, add in rows:
        try:
            a = json.loads(add)
        except Exception:
            continue
        if "link_method" in a:
            out.append((sid, mp, drv, str(a.get("link_method") or "")))
    return out


def _compose_up(d):
    compose = os.path.join(d, "docker-compose.yml")
    env_file = os.path.join(d, ".env")
    return subprocess.run(
        f"docker compose -f {compose} --env-file {env_file} up -d --remove-orphans",
        shell=True, timeout=900).returncode == 0


def _metatube_fetch_plugin(d):
    """下载并解压 Emby 版插件，返回落地的文件名列表。"""
    req = urllib.request.Request(
        METATUBE_API,
        headers={"Accept": "application/vnd.github+json",
                 # GitHub 的 API 不带 UA 会直接 403
                 "User-Agent": "media-stack"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rel = json.load(r)
    asset = next((a for a in rel.get("assets") or []
                  if str(a.get("name", "")).startswith(METATUBE_ASSET_PREFIX)
                  and str(a.get("name", "")).endswith(".zip")), None)
    if not asset:
        names = "、".join(a.get("name", "?") for a in (rel.get("assets") or [])) or "（空）"
        raise RuntimeError(f"这个 release 里没有 Emby 版插件包。现有：{names}")
    info(f"下载 {asset['name']}（{rel.get('tag_name', '')}）...")
    dst = emby_plugin_dir(d)
    os.makedirs(dst, exist_ok=True)
    tmp = os.path.join(dst, ".metatube.zip.part")
    req = urllib.request.Request(asset["browser_download_url"],
                                 headers={"User-Agent": "media-stack"})
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    written = []
    try:
        with zipfile.ZipFile(tmp) as z:
            for n in z.namelist():
                # 只取压缩包根部的普通文件,挡掉 ../ 之类的路径穿越
                base = os.path.basename(n)
                if not base or n.endswith("/"):
                    continue
                with z.open(n) as src, open(os.path.join(dst, base), "wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(base)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not written:
        raise RuntimeError("压缩包里没有文件")
    return written


def toggle_metatube():
    """装 / 卸 MetaTube（服务端容器 + Emby 插件），一个按钮来回切。

    两件东西必须一起动:只装插件不装服务端的话,插件什么都刮不出来,而界面上
    看起来"装好了" —— 那种状态最难排查。
    """
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    on = metatube_on(d)
    files = ms_state().get("metatube_files") or []

    print()
    print(f"  {BOLD}MetaTube{RST} {DIM}—— 按番号刮削的 Emby 插件{RST}")
    print(f"  {DIM}给文件名是番号（ABC-123 这种）的片子刮标题、封面、演员。")
    print(f"  普通电影电视剧用不上它 —— 那些 Emby 自带的 TMDb 就够了。{RST}")
    print(f"  {DIM}要求 Emby 4.9 及以上。{RST}")
    print()
    print(f"  当前：{(GREEN + BOLD + '已安装' + RST) if on else (DIM + '未安装' + RST)}")
    print()

    if on:
        print(f"  {DIM}卸载会：删掉 metatube 容器、删掉插件文件、重启 Emby。{RST}")
        print(f"  {DIM}已经刮好的元数据缓存（{metatube_dir(d)}）留着不删，")
        print(f"  以后再装回来能直接接着用。{RST}")
        if not ask_yn("现在卸载吗？", False):
            print("没有改动。")
            return
        for n in files:
            try:
                os.remove(os.path.join(emby_plugin_dir(d), n))
            except OSError:
                pass
        cfg = rebuild_cfg_from_disk(d)
        cfg["metatube"] = False
        with open(os.path.join(d, "docker-compose.yml"), "w") as f:
            f.write(gen_compose(cfg))
        subprocess.run(["docker", "rm", "-f", "metatube"], capture_output=True)
        _compose_up(d)
        subprocess.run(["docker", "restart", "emby"], capture_output=True)
        save_ms_state(metatube_files=[])
        ok("已卸载（元数据缓存保留）")
        return

    print(f"  {DIM}安装会新增一个容器（几十 MB），并下载插件到 Emby 的插件目录。{RST}")
    print(f"  {DIM}服务端不映射端口，只有同网络的 Emby 连得到。{RST}")
    if not ask_yn("现在安装吗？", False):
        print("没有改动。")
        return

    try:
        files = _metatube_fetch_plugin(d)
    except Exception as e:
        err(f"插件下载失败：{_short_err(e)}")
        print(f"  {DIM}容器还没动，什么都没改。网络好了再试一次。{RST}")
        return
    ok(f"插件已就位：{'、'.join(files)}")

    cfg = rebuild_cfg_from_disk(d)
    cfg["metatube"] = True
    os.makedirs(metatube_dir(d), exist_ok=True)
    with open(os.path.join(d, "docker-compose.yml"), "w") as f:
        f.write(gen_compose(cfg))
    info("启动 MetaTube 服务端...")
    if not _compose_up(d):
        err("容器启动失败，看上面的报错。")
        return
    subprocess.run(["docker", "restart", "emby"], capture_output=True)
    save_ms_state(metatube_files=files)
    ok("装好了")
    print()
    print(f"  {YELLOW}还有两步要你在 Emby 里手动做{RST}{DIM}（脚本代劳不了，"
          f"那是插件自己的设置页）：{RST}")
    print(f"  {DIM}1.{RST} Emby → 设置 → 插件 → MetaTube → 服务器地址填 "
          f"{CYAN}{BOLD}http://metatube:{METATUBE_PORT}{RST}")
    print(f"  {DIM}2.{RST} 要用它的那个媒体库 → 编辑 → 刮削器里勾上 "
          f"{BOLD}MetaTube{RST}，再扫一次")
    print(f"  {DIM}插件没出现的话，Emby 可能还没加载完，等一会儿刷新设置页。{RST}")


def set_link_method():
    """切换网盘直链的获取方式：原画直链 ↔ 转码流。

    这个开关在跨境线路上是决定性的：同一个 4K 文件，原画直链只能跑几百 KB/s、
    每两秒卡一次，换成转码流立刻 5 MB/s 流畅。而两者的差别只是 OpenList 存储里
    的一个字段。以前只能自己去改 sqlite 或者在网页表单里翻，所以做成按钮。

    先显示当前值再问要不要换，和「添加 API 密钥」那个按钮一个路子 —— 点进来
    先告诉你现在是什么状态，而不是上来就改。
    """
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    stores = link_method_storages(d)
    if not stores:
        warn("没有找到支持切换的网盘存储。")
        print(f"  {DIM}只有夸克 / UC 的 TV 版驱动（QuarkTV、UCTV）有这个选项。{RST}")
        print(f"  {DIM}还没在 OpenList 里添加网盘的话，先去加一个。{RST}")
        return

    print()
    for _sid, mp, drv, cur in stores:
        name, why = LINK_METHODS.get(cur, (cur or "未知", ""))
        print(f"  {BOLD}{mp}{RST}  {DIM}({drv}){RST}   当前：{CYAN}{BOLD}{name}{RST} "
              f"{DIM}[{cur}]{RST}")
        if why:
            print(f"      {DIM}{why}{RST}")
    print()
    for k, (name, why) in LINK_METHODS.items():
        print(f"  {DIM}·{RST} {BOLD}{name}{RST} {DIM}[{k}]{RST}：{why}")
    print()

    # 全部存储当前值一致时直接给出"换到另一个"，否则让用户明确选一个
    curs = {c for _s, _m, _d, c in stores}
    if len(curs) == 1 and curs.pop() in LINK_METHODS:
        cur = stores[0][3]
        target = "streaming" if cur == "download" else "download"
        if not ask_yn(f"切换成「{LINK_METHODS[target][0]}」？", True):
            print("没有改动。")
            return
    else:
        print("  1. 原画直链（download）")
        print("  2. 转码流（streaming）")
        c = ask("要切换成哪个？（回车取消）").strip()
        target = {"1": "download", "2": "streaming"}.get(c, "")
        if not target:
            print("没有改动。")
            return

    # OpenList 把存储缓存在内存里，改完必须重启才生效；写库前先停，避免锁冲突
    info("停止 OpenList...")
    subprocess.run(["docker", "stop", "openlist"], capture_output=True, timeout=120)
    db = os.path.join(d, "openlist", "config", "data.db")
    bak = db + ".bak"
    try:
        shutil.copy2(db, bak)
        con = sqlite3.connect(db)
        for sid, mp, _drv, cur in stores:
            row = con.execute("select addition from x_storages where id=?", (sid,)).fetchone()
            a = json.loads(row[0])
            a["link_method"] = target
            con.execute("update x_storages set addition=? where id=?",
                        (json.dumps(a, ensure_ascii=False), sid))
            ok(f"{mp}: {cur or '空'} → {target}")
        con.commit()
        con.close()
    except Exception as e:
        err(f"写入失败：{e}")
        if os.path.exists(bak):
            shutil.copy2(bak, db)
            warn("已从备份还原。")
    finally:
        subprocess.run(["docker", "start", "openlist"], capture_output=True, timeout=120)
        info("OpenList 已重启")

    print()
    print(f"  {DIM}strm 文件不用重新生成 —— 里面存的是网盘路径，{RST}")
    print(f"  {DIM}清晰度是播放那一刻才决定的。直接播一次就能看出区别。{RST}")


def scan_spec_human(spec, paths):
    """把扫描路径的设置说成人话，auto 要把当前展开的结果一起显示出来 ——
       只说「自动」看不出实际在扫什么，加了网盘没生效也发现不了。"""
    if spec == SCAN_AUTO:
        return (f"自动（跟随 OpenList 已挂载的存储）" +
                (f"　→ 当前 {len(paths)} 条：{'、'.join(paths)}" if paths
                 else "　→ 当前还没挂任何网盘"))
    return "、".join(paths) if paths else "未设置"


def set_scan_paths():
    """改 AutoFilm 要扫哪些路径，改完立刻重新生成配置并重启。

    做成按钮而不是「重跑安装」：加一个网盘就要把整轮安装问答再走一遍太重，
    而且重跑安装还会顺带碰 nginx、证书、密码这些完全无关的东西。
    """
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    cfg = rebuild_cfg_from_disk(d)
    print()
    print(f"  当前：{CYAN}{BOLD}{scan_spec_human(cfg['scan_spec'], cfg['scan_paths'])}{RST}")

    mounted = [mp for mp, _drv, _st, _root in openlist_storages(d) if mp and mp != "/"]
    if mounted:
        print(f"  {DIM}OpenList 里已挂载：{'、'.join(mounted)}{RST}")
    else:
        print(f"  {DIM}OpenList 里还没挂任何网盘。{RST}")
    print()
    print(f"  {DIM}填一条（/quark/电影）、多条逗号隔开（/quark/电影,/quark/剧集）、"
          f"或 {RST}{BOLD}y{RST}{DIM} 自动跟随已挂载的存储。回车不改。{RST}")
    print(f"  {YELLOW}填到影视目录那一层，别填网盘根目录{RST}{DIM} —— 原因见确认前的提示。{RST}")
    spec = parse_scan_spec(ask("扫描路径", ""))
    if not spec:
        print("没有改动。")
        return

    paths = resolve_scan_paths(d, spec)
    if not paths:
        warn("展开后一条路径都没有（OpenList 里还没挂网盘？），没有改动。")
        return
    print(f"  将扫描：{BOLD}{'、'.join(paths)}{RST}")
    # 提前把「填了但没挂上」挑出来:AutoFilm 扫不存在的目录只会在日志里留一行 WARN,
    # 用户看到的是"点了生成但什么都没多",很难联想到是路径打错了
    if mounted:
        unknown = [p for p in paths if not any(p == mp or p.startswith(mp.rstrip("/") + "/")
                                               for mp in mounted)]
        if unknown:
            warn(f"这些路径不在已挂载的存储里：{'、'.join(unknown)}")
            print(f"  {DIM}拼错了或者还没在 OpenList 里挂上，扫的时候会被跳过。{RST}")

    # 扫网盘根目录是个大坑,而且是「看起来成功了」的那种坑:
    #   1. 整个盘都会被镜像成 strm —— 手机备份、微信截图、扫描件、壁纸全堆进媒体库,
    #      Emby 把每个目录当成一部电影去 TMDb 搜,搜不到,于是"有条目没海报"
    #   2. download.image 会把网盘里【所有】图片真的下载到 VPS 本地(不只是影视封面),
    #      小盘 VPS 会被自己的相册备份撑爆
    # 实测扫 /quark 生成 61 个 strm,其中只有 3 个跟影视沾边。
    bare = [p for p in paths if p.strip("/").count("/") == 0]
    if bare:
        warn(f"{'、'.join(bare)} 是网盘根目录 —— 整个盘都会被扫进来")
        print(f"  {DIM}手机备份、截图、扫描件都会变成 strm 堆在媒体库里，Emby 拿这些去")
        print(f"  刮削当然搜不到，表现就是「有一堆条目、全都没有海报」。{RST}")
        print(f"  {DIM}而且图片会{RST}真的下载到 VPS 本地{DIM}占硬盘，不只是影视封面。{RST}")
        print(f"  {DIM}建议填到影视目录那一层，比如 {RST}{BOLD}{bare[0]}/电影{RST}")

    if not ask_yn("确认改成上面这些？", True):
        print("没有改动。")
        return

    cfg["scan_spec"], cfg["scan_paths"] = spec, paths
    af = os.path.join(d, "autofilm", "config", "config.yaml")
    with open(af, "w", encoding="utf-8") as f:
        f.write(gen_autofilm_conf(cfg))
    save_ms_state(scan_spec=spec)
    subprocess.run(["docker", "restart", "autofilm"], capture_output=True)
    ok(f"已改成 {len(paths)} 条扫描路径，AutoFilm 已重启")
    print(f"  {DIM}回菜单点「4 生成媒体库」立刻扫一次，或等每天定时任务。{RST}")


def params_menu():
    """后补参数：装完之后才拿得到、需要回头再填的东西。"""
    while True:
        d = ms_install_dir()
        cur = read_emby_api_key(d) if is_installed(d) else ""
        print("\n" + "-" * 60)
        print(f"  {BOLD}后补参数{RST}{DIM}（装完之后再填的东西）{RST}")
        print("-" * 60)
        state = (f"{GREEN}已填{RST}" if cur else f"{YELLOW}空 · 302 不生效{RST}")
        # 顺带把「有没有启用统一密码」也显示出来,省得进去才发现没开
        sec = os.path.join(d, ".secrets")
        has_ba = bool(read_env(sec, "BA_PASS", fallback=os.path.join(d, ".env")))
        ba_state = (f"{GREEN}已启用{RST}" if has_ba else f"{DIM}未启用{RST}")
        # 直链方式在跨境线路上是决定成败的开关，当前值直接摆在菜单上
        lms = link_method_storages(d) if is_installed(d) else []
        if lms:
            vals = {c for _s, _m, _d, c in lms}
            lm_state = (f"{CYAN}{LINK_METHODS.get(lms[0][3], (lms[0][3],))[0]}{RST}"
                        if len(vals) == 1 else f"{YELLOW}各存储不一致{RST}")
        else:
            lm_state = f"{DIM}未挂网盘{RST}"
        print(f"  1. 添加 API 密钥（Emby API Key）   当前：{state}")
        print(f"  2. 修改用户名 / 密码（浏览器弹框那层）  当前：{ba_state}")
        print(f"  3. 直链方式：原画 / 转码流（卡就换这个）  当前：{lm_state}")
        if is_installed(d):
            c0 = rebuild_cfg_from_disk(d)
            sp_state = scan_spec_human(c0["scan_spec"], c0["scan_paths"])
        else:
            sp_state = f"{DIM}未安装{RST}"
        print(f"  4. 扫描路径（加网盘 / 换目录）")
        print(f"     {DIM}当前：{sp_state}{RST}")
        mt_state = ((f"{GREEN}已安装{RST}" if metatube_on(d) else f"{DIM}未安装{RST}")
                    if is_installed(d) else f"{DIM}未安装{RST}")
        print(f"  5. MetaTube 刮削插件（番号识别）  当前：{mt_state}")
        print("  0. 返回")
        print("-" * 60)
        c = ask("请选择").strip()
        if c in ("0", ""):
            return
        if c == "1":
            set_emby_api_key()
        elif c == "2":
            set_web_credentials()
        elif c == "3":
            set_link_method()
        elif c == "4":
            set_scan_paths()
        elif c == "5":
            toggle_metatube()
        else:
            print("无效选择。")
            continue
        ask("\n按回车返回...")


# ============================================================================ 链路体检
def _hc(label, state, detail=""):
    icon = {"ok": f"{GREEN}✔{RST}", "warn": f"{YELLOW}⚠{RST}",
            "bad": f"{RED}✖{RST}", "skip": f"{DIM}—{RST}"}[state]
    print(f"\r\x1b[2K    {pad(label, 20)}{icon}  {detail}")


def _hc_wait(label, secs):
    """慢检查开始前先把行占上，让人看得见它在等什么、要等多久。

    体检恰恰是「东西已经坏了」的时候才会跑的：网盘接口不通时，列目录会一直等到
    超时，屏幕上却停在上一行不动。用户看到的是「体检自己卡死了」——最需要它说话
    的时刻，它反而一声不吭。
    这里先打一行不换行的占位，_hc 用 \\r + 清行覆盖掉它。
    """
    print(f"    {pad(label, 20)}{DIM}测试中…最多 {secs} 秒{RST}", end="", flush=True)


def _short_err(s):
    """把 OpenList 存回来的错误信息压成一行能看的。

    存储的 status 字段在出错时装的是整条 Go 错误,里面带着请求 URL —— 而那个 URL 的
    query 里有 access_token。体检的输出是会被截图发出去的,原样打印等于把网盘令牌
    公开。所以先把每个 URL 砍到问号前,再压掉换行、截断。
    """
    s = re.sub(r'(https?://[^\s"?]+)\?[^\s"]*', r"\1?…", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    # 常见网络错误给个人话结论,原文太长且对定位没有额外帮助
    for pat, msg in (("context deadline exceeded", "网盘接口超时"),
                     ("i/o timeout", "网盘接口超时"),
                     ("TLS handshake timeout", "网盘接口 TLS 握手超时"),
                     ("connection refused", "连接被拒绝"),
                     ("no such host", "域名解析失败")):
        if pat in s:
            return msg
    return s[:70] + ("…" if len(s) > 70 else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向 —— 要的就是那个 302 本身和它的 Location。"""
    def redirect_request(self, *_a, **_kw):
        return None


def strm_target_path(content):
    """从 strm 内容里还原出它在 OpenList 上的路径。两种形态都要认:

      · 路径形式(旧)   /quark/电影/x.mp4
      · URL 形式(新)   https://list.<域名>/d/quark/电影/x.mp4?sign=…

    体检里有几项要按「挂载点」给文件分组,只认路径形式的话,升级之后会得出
    「还没有 strm 文件」这种和上一行「strm 文件 3 个」直接打架的结论。
    """
    c = (content or "").strip()
    if not c:
        return ""
    if c.startswith("/"):
        return c
    if not c.lower().startswith("http"):
        return ""
    try:
        p = urllib.parse.unquote(urllib.parse.urlsplit(c).path)
    except Exception:
        return ""
    # OpenList 的下载端点是 /d/<路径>，也见过带签名前缀的 /dav/ 形态
    for pre in ("/d/", "/dav/"):
        if p.startswith(pre):
            return "/" + p[len(pre):]
    return p or ""


def probe_302(key, own_host=""):
    """真的发一次播放请求，看 MediaWarp 到底回不回 302、302 到哪。

    这是整套东西唯一的端到端证明。前面那些检查(存储 work、能换到直链)都只说明
    "零件是好的",而播放实际走哪条路要看这一下:
      · 302 → 内部地址     客户端根本连不上
      · 200 不是 302       MediaWarp 没拦住,视频要经过本机中转,吃你的带宽

    strm 改成 URL 形式之后这里【多了一跳】,只看第一跳会得出错误结论:
    MediaWarp 现在 302 到的是 OpenList 的公网地址(own_host),播放器要再跟一次
    才到网盘 CDN。所以拿到第一跳之后还要再跟一次,确认后半段也是通的 ——
    否则「302 → 自己的域名」看起来像成功,实际上可能第二跳就死了。

    返回 (state, 说明)。
    """
    if not key:
        return "skip", "没有 Emby API Key"
    try:
        u = (f"http://127.0.0.1:8096/Items?Recursive=true&Limit=8"
             f"&IncludeItemTypes=Movie,Episode&Fields=Path&api_key={key}")
        with urllib.request.urlopen(u, timeout=20) as resp:
            items = (json.load(resp).get("Items") or [])
    except Exception as e:
        return "skip", f"取不到媒体条目（{_short_err(e)}）"
    item = next((i for i in items if STRM_PATH in str(i.get("Path", ""))), None)
    if not item:
        return "skip", "媒体库里还没有网盘条目"

    iid = item.get("Id")
    url = (f"http://127.0.0.1:{MEDIAWARP_PORT}/Videos/{iid}/stream"
           f"?MediaSourceId=mediasource_{iid}&Static=true&api_key={key}")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=90) as resp:
            # 没抛异常就是没重定向
            return "bad", f"返回 {resp.status}，不是 302 —— 视频会经过本机中转"
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            return "bad", f"HTTP {e.code}（换直链失败时就是这样）"
        loc = e.headers.get("Location", "")
        host = loc.split("/")[2] if "://" in loc else loc[:40]
        bare = host.split(":")[0]
        internal = (bare in ("openlist", "emby", "mediawarp", "localhost")
                    or _is_private_ip(bare))
        if internal:
            return "bad", f"302 → {host}  {RED}内部地址，客户端连不上{RST}"

        # 第一跳落在自己的 OpenList 公网域名上 —— 这是 URL 形式 strm 的正常形态,
        # 但还没到网盘。必须再跟一跳才知道后半段通不通:只看第一跳的话,
        # 「302 → 自己的域名」看起来像成功,实际可能第二跳就死了。
        if own_host and bare == own_host:
            try:
                with opener.open(loc, timeout=60) as r2:
                    return "bad", (f"302 → {bare} 之后没有再跳转（HTTP {r2.status}）"
                                   f"  {RED}视频会经过本机{RST}")
            except urllib.error.HTTPError as e2:
                if e2.code not in (301, 302, 303, 307, 308):
                    return "bad", f"{bare} 返回 HTTP {e2.code}  {RED}换直链失败{RST}"
                loc = e2.headers.get("Location", "")
                host = loc.split("/")[2] if "://" in loc else loc[:40]
                bare = host.split(":")[0]
                if bare in ("openlist", "emby", "mediawarp", "localhost") or _is_private_ip(bare):
                    return "bad", f"第二跳 → {host}  {RED}内部地址，客户端连不上{RST}"
                two_hop = True
            except Exception as ex:
                return "bad", f"{bare} 那一跳失败：{_short_err(ex)}"
        else:
            two_hop = False

        # 判断实际拿到的是原画还是转码流。光看 .m3u8 会判错:夸克的转码流地址是
        # video-play-*.drive.quark.cn,并不一定以 m3u8 结尾,而原画是 dl-*。
        # 实际就出现过「302 那行说原画、直链方式那行说转码流」自相矛盾。
        # 认不出来的主机名就不瞎标 —— 报错的标签比没有标签更坏。
        if ".m3u8" in loc or bare.startswith(("video-play", "play-")):
            kind = "转码流"
        elif bare.startswith(("dl-", "download")):
            kind = "原画"
        else:
            kind = ""
        head = f"302 →{' ' + own_host + ' →' if two_hop else ''} {host}"
        tail = (f"（{kind}，" if kind else "（") + "视频直达网盘，不经过本机）"
        return "ok", f"{head}  {DIM}{tail}{RST}"
    except Exception as e:
        return "bad", _short_err(e)


def _is_private_ip(ip):
    """内网 / 本机地址。这些是自己人（nginx 回环、docker 网段、家里局域网），不算外部访问。"""
    if ip.startswith(("127.", "10.", "192.168.", "169.254.", "::1", "fc", "fd")):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def public_visitors(limit=20000):
    """从媒体服务自己那份 nginx 访问日志里，统计非内网来源的 IP。

    为什么值得看:emby / list / home 这三个子域是公网可达的,任何人拿到域名就能敲门。
    Homepage 有 Basic Auth,但 Emby 和 OpenList 走的是它们自己的登录页 —— 有没有人在
    外面试,只有日志知道。

    返回 (总请求数, [(ip, 次数), ...] 按次数降序)；日志不存在返回 (0, [])。
    """
    hits = {}
    total = 0
    for path in (NGX_ACCESS_LOG, NGX_ACCESS_LOG + ".1"):
        try:
            with open(path, errors="replace") as f:
                lines = f.readlines()[-limit:]      # 只看最近的，日志可能很大
        except OSError:
            continue
        for ln in lines:
            ip = ln.split(" ", 1)[0].strip()
            if not ip or _is_private_ip(ip):
                continue
            total += 1
            hits[ip] = hits.get(ip, 0) + 1
    return total, sorted(hits.items(), key=lambda kv: -kv[1])


def _ol_api(path, body, token=None, timeout=60):
    req = urllib.request.Request(
        f"http://127.0.0.1:{OPENLIST_PORT}{path}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": token} if token else {})})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def do_healthcheck():
    """把整条链路挨个打一遍，每项报耗时和结论。

    为什么需要这个:这套东西的失败模式几乎全是「看起来正常、实际是废的」——
    存储状态 work 但根目录ID 填错所以目录是空的;strm 生成了但 mode 错了所以 302
    永远失败;302 成功了但换一次直链要 30 秒所以播放卡在开头。每一个都只能靠翻
    容器日志一层层挖,而日志里那句真正的报错往往被几百行访问日志淹掉。

    这里的每一项都对应一个真实踩过的坑,不是凭空设计的检查表。
    """
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    cfg = rebuild_cfg_from_disk(d)
    todo = []                      # 收集「发现的问题 + 该怎么办」，最后统一打印

    print("\n" + "=" * 60)
    print(f"  {BOLD}链路体检{RST}   {DIM}每一项都对应一个真实卡过的地方{RST}")
    print("=" * 60)

    # ---- 容器 ----
    want = ["emby", "openlist", "mediawarp", "autofilm"]
    if cfg["homepage"]:
        want.append("homepage")
    running = (sh("docker ps --format '{{.Names}}'", timeout=30).stdout or "").split()
    dead = [c for c in want if c not in running]
    if dead:
        _hc("容器", "bad", f"{len(want) - len(dead)}/{len(want)} 在跑，"
                           f"{RED}没起来：{' '.join(dead)}{RST}")
        todo.append(("容器没起全", f"docker compose -f {d}/docker-compose.yml up -d"))
    else:
        _hc("容器", "ok", f"{len(want)}/{len(want)} 在跑")

    # ---- OpenList 登录 ----
    pw = read_env(os.path.join(d, ".secrets"), "OPENLIST_PASS",
                  fallback=os.path.join(d, ".env"))
    token = ""
    t0 = time.monotonic()
    try:
        r = _ol_api("/api/auth/login", {"username": "admin", "password": pw}, timeout=30)
        token = (r.get("data") or {}).get("token", "")
        _hc("OpenList 登录", "ok" if token else "bad",
            f"{time.monotonic() - t0:.1f} 秒" if token else f"{r.get('message', '没拿到 token')}")
    except Exception as e:
        _hc("OpenList 登录", "bad", _short_err(e))
    if not token:
        todo.append(("OpenList 登不上，后面几项没法测",
                     "看 docker logs --tail 50 openlist"))

    # ---- 存储 ----
    stale_status = {}          # 挂载点 → 上次初始化时的报错（等实测结果出来再定性）
    stores = openlist_storages(d)
    if not stores:
        _hc("网盘存储", "bad", "一个都没有")
        todo.append(("OpenList 里还没挂网盘",
                     "浏览器打开 OpenList → 存储 → 添加"))
    for mp, drv, st, root in stores:
        bad_root = drv.lower().startswith("quark") or drv.lower().startswith("uc")
        if st != "work":
            # status 是【存储初始化那一刻】写进去的,之后成功了也不会自动改回 work。
            # 拿它当实时状态用,会把一条陈年记录报成当前故障 —— 实际就出现过
            # 「存储 ✖ 网盘接口超时」和下一行「列目录 ✔ 22 项」自相矛盾。
            # 所以这里只报为「上次初始化的记录」,真正的结论交给下面的实测,
            # 并把它记下来,等实测结果出来再决定要不要进问题清单。
            brief = _short_err(st)
            _hc(f"存储 {mp}", "warn", f"{drv}  {DIM}上次初始化时：{brief}{RST}")
            stale_status[mp] = brief
        elif bad_root and (not root or "/" in root):
            _hc(f"存储 {mp}", "bad", f"{drv}  work  {RED}根文件夹ID={root or '空'}{RST}")
            todo.append((f"{mp} 的根文件夹ID 是 {root or '空'}，夸克要的是文件夹 ID",
                         "OpenList → 存储 → 编辑 → 根文件夹ID 填 0 → 全部重新加载"))
        else:
            _hc(f"存储 {mp}", "ok", f"{drv}  work" + (f"  根目录ID={root}" if root else ""))

    # ---- 列目录 ----
    listed_ok = []
    for p in (cfg["scan_paths"] or [])[:5]:            # 最多测 5 条，别把体检拖太久
        if not token:
            _hc(f"列目录 {p}", "skip", "OpenList 没登上")
            continue
        _hc_wait(f"列目录 {p}", 120)
        t0 = time.monotonic()
        try:
            # refresh: true —— 不加的话 OpenList 直接返回目录缓存,这一行永远是 0.0 秒,
            # 那个数字对判断链路好坏毫无意义。实测同一台机器三次体检:列目录
            # 0.0 / 47.0 / 0.0 秒,而慢的那次恰好是缓存刚被清空 —— 说明快的两次
            # 根本没联网。体检要量的是真实链路,不是缓存命中率。
            r = _ol_api("/api/fs/list", {"path": p, "password": "", "page": 1,
                                         "per_page": 0, "refresh": True},
                        # 120 秒封顶,不是 180:体检本来就是"东西坏了"才跑的,
                        # 网盘不通时每条路径干等 3 分钟,人只会以为体检自己死了。
                        # 实测最慢的一次真实列目录是 119.8 秒,120 刚好盖住。
                        token, timeout=120)
            el = time.monotonic() - t0
            items = (r.get("data") or {}).get("content")
            if r.get("code") != 200:
                _hc(f"列目录 {p}", "bad", f"{el:.1f} 秒  {r.get('message', '')[:40]}")
                todo.append((f"{p} 列不出来：{r.get('message', '')[:40]}", "见上面的存储检查"))
            elif not items:
                _hc(f"列目录 {p}", "warn", f"{el:.1f} 秒  空目录")
                todo.append((f"{p} 是空的", "路径写错了？或者网盘里这个目录本来就没东西"))
            else:
                # 列目录也要看耗时。以前只给「换直链」设了阈值,列目录无论多慢都判绿,
                # 结果出现过「列目录 47.0 秒」和结论「全部正常」同屏 —— 那比不体检更误导。
                # 而且这一项直接决定「生成媒体库」跑不跑得完:AutoFilm 每个目录都要列一次,
                # 47 秒一个目录,几十个目录就必然半路超时(用户那些"扫到一半"就是这么来的)。
                # 阈值按实测重标过,不是拍脑袋的。同一台机器同一条路径采样 9 次
                # (刻意用 30/60/120/300/600 秒的不同空闲间隔):
                #   1.7 / 12.6 / 4.9 / 0.5 / 3.4 / 3.0 / 5.1 / 2.6 / 1.2 秒
                # 中位数 ~3 秒,正常波动到 12 秒。原来的「< 3 秒才算绿」等于让一条
                # 健康的跨境线路永远飘黄 —— 一直报警就等于没报警。
                # 30 秒这条线的依据是后果:超过它,几十个目录的扫描必然半路超时。
                st = "ok" if el < 5 else ("warn" if el <= 30 else "bad")
                extra = "" if st == "ok" else ("  偏慢" if st == "warn" else "  太慢（正常 < 5 秒）")
                _hc(f"列目录 {p}", st, f"{el:.1f} 秒  {len(items)} 项{extra}")
                listed_ok.append((p, items))
                if st == "bad":
                    todo.append((f"列 {p} 用了 {el:.0f} 秒，「生成媒体库」很可能扫到一半就超时",
                                 "网盘接口到本机的线路问题。把扫描路径收窄到具体的媒体目录"
                                 "（3 后补参数 → 4 扫描路径），目录少了成功率高很多"))
        except Exception as e:
            _hc(f"列目录 {p}", "bad", _short_err(e))

    # 存储 status 是陈旧记录，只有当【实测也失败】时才算真故障
    live_ok = {p for p, _ in listed_ok}
    for mp, brief in stale_status.items():
        if any(p == mp or p.startswith(mp.rstrip("/") + "/") for p in live_ok):
            continue                                   # 实际能列出来，那条记录已经过期了
        todo.append((f"{mp} 连不上网盘接口（{brief}）",
                     "线路问题，不是配置错了。等几分钟再跑一次体检；"
                     "已生成的 strm 不受影响"))

    # ---- 换直链：整套东西最关键的一项 ----
    # 播放卡顿的根因几乎都在这:MediaWarp 每次要新地址都得让 OpenList 去网盘换一次,
    # 这个调用慢或超时,播放器就停在那儿等
    # 按网盘分组各测一个:多盘的时候一个好一个坏,只测一个文件根本看不出来
    # (以前取的是所有 strm 里字典序第一个,属于哪个盘全看运气)
    by_mount = {}
    for f in sorted(glob.glob(os.path.join(
            read_env(os.path.join(d, ".env"), "DATA_ROOT") or os.path.join(d, "media"),
            "strm", "**", "*.strm"), recursive=True)):
        try:
            raw = open(f, encoding="utf-8").read()
        except OSError:
            continue
        # strm 有两种形态(路径 / URL),统一还原成 OpenList 上的路径再分组。
        # 只认路径形式的话,升级之后这里会空掉,体检就会打出「还没有 strm 文件」
        # 而上一行明明写着「strm 文件 3 个」—— 自相矛盾比不检查更误导。
        fp = strm_target_path(raw)
        if not fp.startswith("/"):
            continue
        mount = "/" + fp.lstrip("/").split("/")[0]      # /quark/电影/x.mp4 → /quark
        by_mount.setdefault(mount, fp)
    if not token:
        _hc("换直链", "skip", "OpenList 没登上")
    elif not by_mount:
        _hc("换直链", "skip", "还没有 strm 文件，无从测起")
    for mount, fp in list(by_mount.items())[:3]:       # 封顶 3 个,每个最坏要等半分钟
        label = "换直链" if len(by_mount) == 1 else f"换直链 {mount}"
        _hc_wait(label, 60)
        t0 = time.monotonic()
        try:
            r = _ol_api("/api/fs/get", {"path": fp, "password": ""}, token, timeout=60)
            el = time.monotonic() - t0
            raw = (r.get("data") or {}).get("raw_url", "")
            if not raw:
                _hc(label, "bad", f"{el:.1f} 秒  {r.get('message', '没拿到 raw_url')[:40]}")
                todo.append((f"{mount} 换不到直链，302 不可能生效",
                             "看 docker logs --tail 50 openlist"))
            elif el > 8:
                _hc(label, "bad",
                    f"{RED}{el:.1f} 秒{RST}  太慢（正常 < 2 秒）  →  {raw.split('/')[2]}")
                todo.append((f"{mount} 换一次直链要 {el:.0f} 秒，播放会卡在开头甚至超时",
                             "网盘接口到本机的线路问题，服务端改不了；"
                             "缓存已开 2h，同一部片只慢第一次"))
            elif el > 2:
                _hc(label, "warn",
                    f"{el:.1f} 秒  偏慢（正常 < 2 秒）  →  {raw.split('/')[2]}")
            else:
                _hc(label, "ok", f"{el:.1f} 秒  →  {raw.split('/')[2]}")
        except Exception as e:
            _hc(label, "bad", _short_err(e))
            todo.append((f"{mount} 换直链失败，此刻这个盘的片子会卡在开头",
                         "网盘接口不通（线路问题，不是配置错了）。"
                         "已生成的 strm 不受影响，等几分钟再跑一次体检"))

    # ---- MediaWarp ----
    key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"), "auth")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{MEDIAWARP_PORT}/System/Info/Public", timeout=20) as resp:
            el = time.monotonic() - t0
            _hc("MediaWarp→Emby", "ok" if resp.status == 200 else "bad",
                f"{el:.1f} 秒  HTTP {resp.status}")
    except Exception as e:
        _hc("MediaWarp→Emby", "bad", _short_err(e))
        todo.append(("MediaWarp 打不通 Emby", "docker logs --tail 30 mediawarp"))
    # ---- 302 端到端 ----
    own_host = urllib.parse.urlsplit(openlist_public_url(cfg)).hostname or ""
    _hc_wait("302 直链", 90)
    st302, msg302 = probe_302(key, own_host)
    _hc("302 直链", st302, msg302)
    if st302 == "bad":
        if "不是 302" in msg302:
            todo.append(("MediaWarp 没有拦截播放请求，视频会经过本机中转",
                         "检查 mediawarp/config.yaml 的 http_strm.enable 是不是 true、"
                         f"prefix_list 是不是 {STRM_PATH}；「6 更新」会重新生成"))
        elif "内部地址" in msg302:
            todo.append(("302 指向了容器内部地址，播放器连不上",
                         "autofilm 的 public_url 要填播放器能访问的地址；"
                         "「6 更新」会重新生成这份配置"))
        else:
            todo.append(("302 没生成", "多半是上一行的换直链失败，等线路恢复再试"))

    if key:
        _hc("Emby API Key", "ok", "已填")
    else:
        _hc("Emby API Key", "bad", "空 —— 302 不会生效")
        todo.append(("MediaWarp 没有 Emby API Key", "3 后补参数 → 1 添加 API 密钥"))

    # ---- strm / 媒体库 ----
    n = strm_count(d)
    _hc("strm 文件", "ok" if n else "bad", f"{n} 个" if n else "0 个 —— Emby 里一定是空的")
    if not n:
        todo.append(("一个 strm 都没有", "先确认上面的列目录正常，再点「4 生成媒体库」"))
    if key:
        try:
            u = (f"http://127.0.0.1:8096/Library/VirtualFolders?api_key={key}")
            with urllib.request.urlopen(u, timeout=20) as resp:
                libs = json.load(resp)
            paths = [p for lb in libs for p in (lb.get("Locations") or [])]
            hit = any(STRM_PATH in p or p in STRM_PATH for p in paths)
            _hc("Emby 媒体库", "ok" if hit else "warn",
                f"{len(libs)} 个库" + ("" if hit else f"  {YELLOW}没有指向 {STRM_PATH}{RST}"))
            if not hit:
                todo.append((f"Emby 里没有指向 {STRM_PATH} 的媒体库",
                             f"Emby → 设置 → 媒体库 → 添加，路径填 {STRM_PATH}"))

            # 元数据语言留空 = 跟服务器默认走(通常是 en)。中文片名拿去 TMDb 的英文
            # 索引里搜是搜不到的,表现为「条目都在、一张海报都没有」,而且这个设置藏在
            # 建库那一屏里,建完之后基本没人会回去看,所以单独拎出来报一条。
            noln = [lb.get("Name") or "?" for lb in libs
                    if not (lb.get("LibraryOptions") or {}).get("PreferredMetadataLanguage")]
            if noln:
                _hc("刮削语言", "warn", f"{'、'.join(noln)} 没设语言  {YELLOW}中文片名搜不到{RST}")
                todo.append((f"媒体库「{noln[0]}」的元数据语言是空的，会按服务器默认（通常英文）搜",
                             "Emby → 设置 → 媒体库 → 点该库 → 首选语言按片名的语种选"
                             "（中文片名就选中文/中国），再「扫描媒体库文件」"))
            else:
                _hc("刮削语言", "ok", "都设了")
        except Exception as e:
            _hc("Emby 媒体库", "warn", _short_err(e))

    # ---- 网盘授权令牌 ----
    # 令牌过期的表现和「网盘不通」一模一样：列目录还行（读缓存），一点开文件就转圈。
    # 但这两件事一个能修（重新扫码）、一个只能等（跨境线路），混在一起会让人一直
    # 往线路方向找，找不到根子。所以把天数明摆出来。
    for mp, days in storage_token_days(d):
        if days <= 0:
            _hc(f"授权 {mp}", "bad", f"{RED}已过期 {-days:.0f} 天{RST}")
            todo.append((f"{mp} 的网盘授权已过期 —— 目录还列得出来（读缓存），"
                         f"但点开任何文件都会转圈",
                         "OpenList → 存储 → 编辑该存储 → 重新扫码授权"))
        elif days < 3:
            _hc(f"授权 {mp}", "warn", f"{YELLOW}还剩 {days:.1f} 天{RST}")
            todo.append((f"{mp} 的网盘授权 {days:.1f} 天后到期",
                         "到期当天会突然打不开任何文件。"
                         "OpenList → 存储 → 编辑该存储 → 重新扫码授权"))
        else:
            _hc(f"授权 {mp}", "ok", f"还剩 {days:.0f} 天")

    # ---- 直链方式 / 证书 ----
    lms = link_method_storages(d)
    if lms:
        cur = lms[0][3]
        _hc("直链方式", "ok", f"{LINK_METHODS.get(cur, (cur,))[0]}"
                             f"{DIM}（卡顿就去 3 后补参数 → 3 切换）{RST}")
    # ---- 保活 ----
    ka = keepalive_state(d)
    if not os.path.exists(KEEPALIVE_CRON):
        _hc("链路保活", "warn", "没装 —— 冷启动第一次播放会转圈几十秒")
        todo.append(("保活定时任务没装",
                     "跑一次「6 更新」会自动补上"))
    elif not ka:
        _hc("链路保活", "skip", f"已装，还没跑过（每 {KEEPALIVE_MIN} 分钟一次）")
    else:
        mins = int((time.time() - ka.get("ts", 0)) / 60)
        if ka.get("ok"):
            _hc("链路保活", "ok", f"{mins} 分钟前成功，耗时 {ka.get('elapsed', 0)} 秒")
        else:
            _hc("链路保活", "warn",
                f"{mins} 分钟前失败：{ka.get('error', '')[:40]}")

    # ---- 公网访问 ----
    if cfg["has_domain"]:
        if os.path.exists(NGX_ACCESS_LOG):
            total, tops = public_visitors()
            if not tops:
                _hc("公网访问", "ok", "最近没有外网 IP 访问过")
            else:
                head = "、".join(f"{ip}({n}次)" for ip, n in tops[:3])
                # 只是提示，不判错:自己在外面用手机看片也会记在这里
                _hc("公网访问", "warn" if len(tops) > 3 else "ok",
                    f"{len(tops)} 个外网 IP / {total} 次请求")
                print(f"    {pad('', 20)}{DIM}最多的：{head}{RST}")
                if len(tops) > 3:
                    todo.append((f"有 {len(tops)} 个不同的外网 IP 访问过媒体服务",
                                 "自己在外面用手机看也会记在这里；"
                                 "认不出来的话去 Emby/OpenList 改密码，"
                                 f"完整日志 {NGX_ACCESS_LOG}"))
        else:
            _hc("公网访问", "skip", f"还没有日志（下次「6 更新」刷新 nginx 配置后就有）")

    if cfg["has_domain"] and os.path.exists(cfg["crt"]):
        r = sh(f"openssl x509 -enddate -noout -in {cfg['crt']}", timeout=20)
        m = re.search(r"notAfter=(.+)", r.stdout or "")
        if m:
            try:
                exp = time.mktime(time.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z"))
                days = int((exp - time.time()) / 86400)
                # 14 天这条线不是"快到期了提醒一下"，是"续期已经失败了"：
                # acme.sh 默认剩 30 天就自动重签，还能看到 14 天说明那一步没跑成。
                # 常见三种断法都写进 todo，省得用户以为只是还没到时候。
                _hc("证书", "ok" if days > 14 else "bad", f"还有 {days} 天")
                if days <= 14:
                    todo.append((f"证书只剩 {days} 天 —— acme.sh 剩 30 天就该自动续了，"
                                 f"还能看到这个数说明续期已经失败",
                                 "依次查：crontab -l | grep acme（cron 在不在）、"
                                 "/root/.acme.sh/*/*.conf 里的 Le_ReloadCmd 和 "
                                 "Le_RealFullChainPath、以及 Cloudflare Token 是不是换过了"))
            except ValueError:
                _hc("证书", "skip", m.group(1).strip()[:40])

    print("=" * 60)
    if todo:
        print(f"\n  {YELLOW}{BOLD}发现 {len(todo)} 个问题{RST}")
        for i, (what, how) in enumerate(todo, 1):
            print(f"  {i}. {what}")
            print(f"     {DIM}→ {how}{RST}")
    else:
        print(f"\n  {GREEN}{BOLD}全部正常。{RST}"
              f"{DIM}播放仍然卡的话，多半是播放设备到网盘那条线，不在服务器这边。{RST}")
    print()


def main_menu():
    require_root()
    while True:
        installed = is_installed()
        print("\n" + "=" * 60)
        print(f"  {BOLD}自建 Emby · 网盘直链媒体服务器{RST}   "
              f"{DIM}v{SCRIPT_VERSION}{RST}")
        print(f"  {DIM}Emby + OpenList + AutoFilm + MediaWarp（302 直链）{RST}")
        print("=" * 60)
        print(f"  状态：" + (f"{GREEN}已安装{RST}  {DIM}{ms_install_dir()}{RST}"
                            if installed else f"{YELLOW}未安装{RST}"))
        print("-" * 60)
        print("  1. 安装" + ("（已装，重跑可改配置）" if installed else ""))
        print("  2. 使用信息（地址 / 账号密码 / 常用命令）")
        print("  3. 后补参数（Emby API Key 等装完才拿得到的东西）")
        # 装完网盘还没挂，所以这一步只能等用户在 OpenList 里挂好之后自己点。
        # 没有这个按钮的话，不敲命令的人就卡在「OpenList 里有文件、Emby 里空的」
        print("  4. 生成媒体库（网盘挂好后点这个，Emby 才看得到片子）")
        print("  5. 链路体检（卡住 / 不出片子时先跑这个）")
        print("  6. 更新（拉最新镜像 + 按新版本刷新配置）")
        print("  7. 卸载")
        print("  0. 返回")
        print("-" * 60)
        c = ask("请选择").strip()
        if c in ("0", ""):
            return
        if c == "1":
            main()
        elif c == "2":
            show_info()
        elif c == "3":
            params_menu()
            continue          # 子菜单自己管停顿，回来别再多按一次回车
        elif c == "4":
            do_strm()
        elif c == "5":
            do_healthcheck()
        elif c == "6":
            do_update()
        elif c == "7":
            do_uninstall()
        else:
            print("无效选择。")
            continue
        ask("\n按回车返回菜单...")


if __name__ == "__main__":
    try:
        arg = sys.argv[1] if len(sys.argv) > 1 else ""
        if arg in ("uninstall", "remove"):
            do_uninstall()
        elif arg == "info":
            show_info()
        elif arg in ("check", "doctor", "healthcheck"):
            do_healthcheck()
        elif arg == "keepalive":          # cron 调的，安静跑，结果写 json
            do_keepalive()
        elif arg == "update":
            do_update()
        elif arg in ("apikey", "key"):
            require_root(); set_emby_api_key()
        elif arg in ("passwd", "password"):
            require_root(); set_web_credentials()
        elif arg == "install":
            main()
        else:
            main_menu()
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
