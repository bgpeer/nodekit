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
import signal
import sqlite3
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# 版本号：改了代码就 +1，让「7 更新」能显示 vX → vY。
# 仓库主人定的规矩：只动最后一位，1.5.0 一路加到 1.5.999，前两位不要自己动。
SCRIPT_VERSION = "1.5.56"

# 本脚本在仓库里的地址，「更新」时用它把自己换成最新版
SELF_URL = "https://raw.githubusercontent.com/bgpeer/nodekit/main/media-stack.py"
# 媒体库关键词规则也放仓库里，和脚本一起更新。用户点的名：想在 GitHub 上直接改，
# 不用登服务器 —— 在手机上尤其明显，终端里改 yaml 基本没法用。
RULES_URL = "https://raw.githubusercontent.com/bgpeer/nodekit/main/library-rules.yaml"

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


def has_tty():
    """现在是不是真的有人坐在终端前。

    不能靠调用方传 interactive：ask() 读不到 /dev/tty 会退回 input()，cron 下
    拿到 EOF 被兜成空字符串，ask_yn 再当成"用了默认值"—— 定时任务替用户答了 Y，
    而问句从来没出现在屏幕上。
    """
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        pass
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


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
    """记住装在哪、以及生成的配置里读不回来的选择（比如扫描路径是不是 auto）。

    合并写而不是整份覆盖 —— 覆盖会把别的键悄悄抹掉。
    """
    cur = ms_state()
    if install_dir:
        cur["install_dir"] = install_dir
    cur.update(extra)
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        write_atomic(MS_STATE, json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
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

    节点开了 SNI 分流时 443 上是 stream 模块按 SNI 分流，真正的 https 站绑在
    127.0.0.1:8443；新站点要监听同一个内部端口才能被 default 分支带进来。
    """
    r = sh("nginx -T 2>/dev/null")
    m = re.search(r"^\s*listen\s+127\.0\.0\.1:(\d+)\s+ssl", r.stdout or "", re.M)
    if m:
        return int(m.group(1))
    return 443


def nginx_worker_user():
    """nginx worker 跑在哪个用户下。

    auth_basic_user_file 是 worker 每次请求时读的，而 worker 不是 root
    （Debian 系 www-data、RHEL 系 nginx）。属主设成 root 会让它读不到，访问 500。
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
def write_atomic(path, text, mode=0o644):
    """写文件：先写同目录的临时文件、落盘，再原子改名顶上去。

    直接 open(path, "w") 的毛病是它【先把文件清成 0 字节】再写 —— 中间那一瞬间进程要是
    没了（cron 的 timeout 杀、被 OOM 杀、断电、磁盘满），磁盘上留下的就是一个空文件。
    而读它的那一方多半会把"读不出来"当成"本来就没有"，于是安静地按默认值继续跑。
    这个仓库为这一条踩过两次：mediawarp 的配置被写空、观看进度的备份表被清掉。

    改名是原子的：要么还是旧的那份，要么是完整的新的，没有中间态。
    fsync 也不能省 —— 只 write 不 flush 的话，改名可能先于数据落盘，断电后同样是空文件。
    mode 在【打开的时候】就定死：.secrets 里是密码和 API Key，不能先落地成 0644 再 chmod，
    那个时间窗里谁都读得到。
    """
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode),
                       "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
# Docker 默认的 json-file 日志没有上限，六个容器一直往里写，磁盘和页缓存会被慢慢
# 吃掉，而脚本自己也靠 docker logs 做诊断，文件越大读得越慢。每个容器封顶 3 个 10 MB。
LOG_LIMIT = """    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}
"""


def gen_compose(cfg):
    """生成 docker-compose.yml。

    反代模式下所有端口都收进 127.0.0.1。Emby 的 8096 尤其重要 —— 直连它会绕过
    MediaWarp 的 302 拦截，退化成服务器中转，带宽还是自己的。
    """
    bind = "127.0.0.1:" if cfg["has_domain"] else ""
    d = cfg["install_dir"]
    parts = ["# 由 media-stack.py 自动生成。可手动修改后 docker compose up -d 生效。",
             "name: mediastack", "services:"]

    parts.append(f"""  emby:
    image: emby/embyserver:latest
    container_name: emby
    restart: unless-stopped
{LOG_LIMIT}    environment:
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
{LOG_LIMIT}    user: "${{PUID}}:${{PGID}}"
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
{LOG_LIMIT}    # AutoFilm 读不到 TZ 环境变量（启动日志里会打印「使用应用时区 timezone=UTC」），
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
{LOG_LIMIT}    environment:
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
{LOG_LIMIT}    environment:
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
{LOG_LIMIT}    environment:
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

      · y / auto / 自动    → SCAN_AUTO，跟随 OpenList 里已挂载的存储
      · /quark             → 单条路径
      · /quark,/aliyun     → 多条路径，【只认逗号】

    【不能拿空格当分隔符】目录名里带空格很常见（/quark/My Movies、/115/4K REMUX），
    按空格切会把一条路径切成两条不存在的。而扫描路径决定了哪些主目录算孤儿，
    切歪之后下游那个清理会认为本地所有 strm 都不该留 —— 那次删了 39786 个。

    返回 SCAN_AUTO 或去重后的路径列表；给不出有效内容时返回 None。
    """
    s = (s or "").strip()
    if not s:
        return None
    if s.lower() in ("y", "yes", "auto") or s in ("自动", "是"):
        return SCAN_AUTO
    out, seen = [], set()
    for p in re.split(r"[,，]+", s):
        p = p.strip().rstrip("/")
        if not p:
            continue
        if not p.startswith("/"):
            p = "/" + p
        if p not in seen:
            seen.add(p)
            out.append(p)
    return merge_scan_paths(out) or None


def _paths_under(paths, mp):
    """paths 里属于挂载点 mp 的那些。/aliyun2 不算 /aliyun 底下的。"""
    root = (mp or "").rstrip("/")
    if not root:
        return []
    return [p for p in paths if p == root or p.startswith(root + "/")]


def merge_scan_paths(paths):
    """扫描路径列表规范化：去重、去掉末尾斜杠、【子路径被父路径吃掉】。

    /网盘 和 /网盘/mov/电影 同时在表里，实际效果和只写 /网盘 一模一样 —— 扫整个盘的时候
    本来就包含那条子路径。可屏上是两条，人会以为"限定了只扫电影"，而实际扫的是整个盘，
    手机备份和截图照样进媒体库。规矩说出来就一句：【有最大的就扫最大的】。

    AutoFilm 那边还要更糟一点：两个任务范围重叠，同一部片被扫两遍、strm 写两次。

    排序保证父目录一定排在自己的子目录前面（"/网盘" < "/网盘/mov"），所以一趟就够。
    """
    out = []
    for p in sorted({(x or "").rstrip("/") for x in paths if x}):
        if any(p == q or p.startswith(q + "/") for q in out):
            continue
        out.append(p)
    return out


def explicit_scan_paths():
    """用户在「挂载路径」里给某个盘明确指定的那些路径。

    【读出来就先归并一次】老配置里可能已经躺着"整个盘 + 它底下的子目录"这种组合，
    那是上一版没有拦住留下的。在这里归并，菜单、体检、生成配置看到的就是同一份。
    """
    sp = ms_state().get("scan_spec")
    return [] if sp == SCAN_AUTO else merge_scan_paths(sp or [])


def auto_rest_on():
    """「剩余网盘（自动）」开着没有。

    老配置里 scan_spec == SCAN_AUTO 语义上等同于"没有盘单独设过 + 剩余全开"，认成开。
    """
    st = ms_state()
    if "auto_rest" in st:
        return bool(st.get("auto_rest"))
    return st.get("scan_spec") == SCAN_AUTO


def effective_scan_paths(d):
    """真正要交给 AutoFilm 去扫的路径。

    【单独设过的盘优先，剩余的才归"自动"管】某个盘一旦被单独指了目录，自动就不再
    往它身上加整盘 —— 否则明明只想扫 /aliyun/电影，开了自动又把整个盘塞回去。
    """
    exp = explicit_scan_paths()
    out = list(exp)
    if auto_rest_on():
        for mp, _drv, _st, _root, _m in openlist_storages(d):
            if mp and mp != "/" and not _paths_under(exp, mp):
                out.append(mp)
    return order_scan_paths(d, out)


def resolve_scan_paths(d, spec):
    """把规格展开成实际要扫的路径列表。

    auto 在【生成配置的那一刻】才去读 OpenList 已挂载的存储 —— 以后加了新网盘，
    不用回来改设置，重新生成一次就自动带上。
    """
    if spec == SCAN_AUTO:
        paths = [mp for mp, _drv, _st, _root, _m in openlist_storages(d)
                 if mp and mp != "/"]
    else:
        paths = list(spec or [])
    return order_scan_paths(d, merge_scan_paths(paths))


def order_scan_paths(d, paths):
    """小盘先扫。已经扫过的盘按 strm 数升序，没扫过的一律排在后面。

    任务按配置顺序跑，一个两万文件的盘排前面，后面的小盘就得等，而等待循环撑不到
    那么久就放弃了 —— 表现是"夸克永远扫不到"。

    【没扫过的排最后】新挂的盘本地 strm 计 0，只按数量排会让它排第一，而它恰恰是
    唯一不知道有多大的那个。新盘之间用顶层目录数粗排，列不出来的垫底。
    """
    base = os.path.join(strm_root(d), STRM_SUBDIR)

    def local_strm(p):
        n = 0
        try:
            for _r, _ds, fs in os.walk(os.path.join(base, *strm_subpath(p).split("/"))):
                n += sum(1 for f in fs if f.endswith(".strm"))
        except OSError:
            pass
        return n

    known = [(local_strm(p), p) for p in paths]
    scanned = sorted((n, p) for n, p in known if n > 0)
    fresh = [p for n, p in known if n == 0]
    if len(fresh) > 1:
        # 新盘之间才值得花这一次列举；只有一个新盘的话排哪儿都一样
        tok = ""
        try:
            pw = read_env(os.path.join(d, ".secrets"), "OPENLIST_PASS",
                          fallback=os.path.join(d, ".env"))
            tok = (_ol_api("/api/auth/login",
                           {"username": "admin", "password": pw},
                           timeout=20).get("data") or {}).get("token", "")
        except Exception:
            tok = ""

        def top_count(p):
            if not tok:
                return 1 << 30
            names = _dir_names(p, tok)
            return (1 << 30) if names is None else len(names)
        fresh = [p for _n, p in sorted((top_count(p), p) for p in fresh)]
    return [p for _n, p in scanned] + fresh


def strm_subpath(scan_path):
    """扫描路径 → strm 树里对应的相对目录。就是网盘全路径去掉开头的斜杠。

        /quark        → quark
        /quark/电影   → quark/电影

    【strm 树是网盘树的镜像】一一对应，换来的是扫描路径怎么改都不会让已有的 strm
    搬家。老规则会把扫描路径自己那几段吃掉，于是 cloud/quark/电影 在扫 /quark 时和
    在扫 /quark/电影 时指的不是同一个目录 —— 而 Emby 媒体库的路径是用户手填的，
    脚本改扫描路径碰不到它，覆盖范围就这么悄悄变了（7 个 strm 掉出去 6 个，
    Emby 不报任何错）。已有的 strm 由 migrate_strm_layout() 挪过去，续播点跟着搬。
    """
    return "/".join(x for x in (scan_path or "").split("/") if x)


def strm_mount_dir(scan_path):
    """扫描路径 → 它在 strm 树里的【顶层】目录名。/quark/电影 → quark；/115 → 115

    只在"这个网盘还要不要"这种整盘级别的判断里用（比如清理孤儿主目录）。落点用
    strm_subpath()，别拿这个去拼路径 —— 那正是上面记的那个坑。
    """
    segs = [x for x in (scan_path or "").split("/") if x]
    return segs[0] if segs else ""


def scan_task_id(path):
    """由路径生成任务 id。AutoFilm 用它给状态文件命名，多任务时必须唯一。"""
    t = re.sub(r"[^\w一-鿿]+", "_", path.strip("/")) or "root"
    return t


def openlist_public_url(cfg):
    """strm 里要写的 OpenList 地址。

    必须是【播放器也能访问】的地址，不能用容器内网的 http://openlist:5244 ——
    MediaWarp 的 HTTPStrm 是把它 302 给播放器的，手机/电视解析不了这个主机名。
    """
    sub = next(s for s, _p, c, _l in SUBDOMAINS if c == "openlist")
    if cfg["has_domain"]:
        return f"https://{sub}.{cfg['domain']}"
    # 没域名时 openlist 的端口是 0.0.0.0 绑定的(见 gen_compose 的 bind),
    # 直接用 IP:端口,和用户平时打开 OpenList 界面的地址一样
    return f"http://{cfg['host_ip']}:{OPENLIST_PORT}"


# OpenList 里那个「网站 URL」在不同版本上叫不同的键。不猜 —— 去设置列表里找哪个真的在。
SITE_URL_KEYS = ("api_url", "site_url")


def ensure_openlist_site_url(d, cfg=None, quiet=False):
    """把 OpenList 的「网站 URL」设成【播放器也能访问】的那个地址。返回改没改。

    【这一项决定了 WebDAV / 本地目录这类盘在 Emby 里能不能播】它们是"代理型"存储：
    网盘那侧根本没有 CDN 直链，OpenList 只能把自己的 /d/ 地址当成直链回给你。而那个
    地址的主机名是【谁来问就按谁用的主机名拼】—— MediaWarp 在容器里用
    http://openlist:5244 问，OpenList 就回 http://openlist:5244/d/…，MediaWarp 原样
    302 给播放器。手机、电视根本解析不了 openlist 这个名字，报的是
    "Name or service not known"，在客户端上就是一句 load fail。

    整条链每一步都"成功"了：strm 是对的、OpenList 认得这个文件、MediaWarp 也确实
    302 了 —— 只有最后那个地址是内网的。这就是为什么它极难自己看出来。

    修法是 OpenList 自己的设置：填上「网站 URL」之后，它拼 /d/ 地址就不再看请求里的
    主机名，一律用这个对外地址。这是那个设置项存在的全部意义。

    【键名不硬编】不同版本里它叫 api_url 或 site_url。去设置列表里找哪个真的存在 ——
    写一个不存在的键进去，OpenList 会存着然后完全忽略，而屏上写着"已设置"。
    """
    cfg = cfg or rebuild_cfg_from_disk(d)
    want = (openlist_public_url(cfg) or "").rstrip("/")
    if not want or "127.0.0.1" in want or "localhost" in want:
        return False           # 连我们自己都算不出一个对外地址，就别乱写
    tok = _ol_token(d)
    if not tok:
        return False
    try:
        r = _ol_api("/api/admin/setting/list", {}, tok, timeout=30, method="GET")
    except Exception:
        return False
    items = (r.get("data") or []) if r.get("code") == 200 else []
    item = next((x for x in items if x.get("key") in SITE_URL_KEYS), None)
    if item is None:
        if not quiet:
            # 【把带 url 的键名摆出来】不然这就是个死胡同：屏上只说"没有这一项"，
            # 而这一项决定了 WebDAV 那类盘能不能播。换个版本它可能改了名字，
            # 印出来才知道该往 SITE_URL_KEYS 里加哪一个。
            near = [str(x.get("key")) for x in items if "url" in str(x.get("key")).lower()]
            print(f"  {DIM}这个 OpenList 版本没有「网站 URL」这一项，跳过"
                  + (f"（带 url 的设置项：{'、'.join(near[:6])}）" if near else "") + f"{RST}")
        return False
    if str(item.get("value") or "").rstrip("/") == want:
        return False
    old = str(item.get("value") or "")
    item = dict(item, value=want)
    try:
        # 【整条 item 原样发回去】只发 key/value 的话，help、type、group 这些元信息
        # 会被 upsert 覆盖成空 —— 设置页面上那一项就变成没有说明的裸输入框。
        _ol_api("/api/admin/setting/save", [item], tok, timeout=30)
    except Exception as e:
        if not quiet:
            warn(f"设不上 OpenList 的「网站 URL」：{_short_err(e)}")
        return False
    if not quiet:
        ok(f"OpenList 的「网站 URL」已设成 {want}"
           + (f"{DIM}（原来是{'空的' if not old else ' ' + old}）{RST}"))
        print(f"  {DIM}WebDAV 源、本地目录这类盘在网盘侧没有 CDN 直链，OpenList 只能回"
              f"自己的地址 —— 不填这一项它回的是容器内网名（openlist:5244），"
              f"手机电视解析不了，点开就是 load fail。{RST}")
        print(f"  {DIM}已经缓存过的旧地址最多 2 小时后自动换过来；等不及就"
              f"docker restart mediawarp。{RST}")
    return True


def openlist_api_addr(cfg):
    """MediaWarp 该用【哪个地址】去调 OpenList 的接口。

    【这一项决定了代理型存储（WebDAV 源、本地目录）能不能在 Emby 里播】那类驱动在网盘侧
    没有 CDN 直链，OpenList 只能把自己的 /d/ 地址当直链回给 MediaWarp。而 OpenList v4
    【没有「网站 URL」这个设置】—— v4.2.6 的设置列表里带 url 的只有 site_title 和
    qbittorrent_url，那一项在 v4 里被去掉了。它一律【按请求里的 Host 头】拼那个地址：
    用 http://openlist:5244 去问，回来的就是 http://openlist:5244/d/…，MediaWarp 原样
    302 给播放器，而手机、电视解析不了这个容器名，客户端上就是一句 load fail。

    所以只能从这头改：让 MediaWarp 用【对外地址】去问。nginx 那边 Host 和
    X-Forwarded-Proto 都是原样透传的（见 gen_nginx_conf），OpenList 拼出来的自然就是
    对外地址。代价只有一跳本机 nginx —— 换直链不是热路径，而且上面还有 alist_api_ttl
    那层缓存。非代理型的盘完全不受影响：它们的 raw_url 是网盘 CDN 给的，跟这个地址无关。

    【连不通就退回内网地址】域名在容器里解析不了、证书没签好的话，用对外地址会让
    【所有】盘都换不到直链 —— 那比"WebDAV 那个盘播不了"严重得多。所以先探一次再决定。
    """
    internal = f"http://openlist:{OPENLIST_PORT}"
    pub = (openlist_public_url(cfg) or "").rstrip("/")
    if not pub or "127.0.0.1" in pub or "localhost" in pub:
        return internal
    try:
        req = urllib.request.Request(pub + "/api/public/settings",
                                     headers={"User-Agent": "media-stack"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return pub if r.status == 200 else internal
    except Exception:
        return internal


def gen_autofilm_conf(cfg):
    """AutoFilm：定时遍历 OpenList，把网盘里的视频写成 .strm 文本文件。

    mode 必须是 AlistURL，而且 public_url 必须填。三种取值都试过：

      · RawURL    把网盘的临时直链写死进 strm，过期后整库集体播放失败
      · AlistPath 只写 OpenList 上的路径。302 能用，但【Emby 拿不到时长】—— 它把
                  strm 内容当本地文件喂 ffprobe，RunTimeTicks 停在 0。而续播点是
                  按时长算百分比的，分母为 0 这套逻辑整个失效：停止播放直接判「已
                  看完」，续播点清零、进度条也拖不动。补 .nfo 没用，ffprobe
                  provider 在 nfo 之后跑，失败后把媒体信息覆盖掉
      · AlistURL  写完整下载地址，Emby 第一次播放能探出时长，但播放只能交给
                  MediaWarp 的 http_strm，那条路【没有直链缓存】，每次开播都要现换
                  一次直链（实测 7.5~47 秒）

    所以两种形态都不适合常驻。最终方案是平时用路径形式、只在补探测那几秒临时切成
    URL —— 见 heal_media_info()。切 URL 时 sign 必须带：OpenList 默认 sign_all=true，
    不带签名访问 /d/ 会 401。
    """
    paths = list(cfg.get("scan_paths") or [])
    head = f"""# 由 media-stack.py 自动生成，「更新」会重新生成本文件，别手改。
# 要改扫描哪些路径：emby → 4 挂载路径
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
    target_dir: "{STRM_PATH}/{strm_subpath(path)}"
    mode: AlistPath         # 见 gen_autofilm_conf 的注释，三种取值都实测过
    flatten_mode: false
    overwrite: false
    concurrency: 5          # 网盘限流，并发别开太高
    download:
      # 【只下字幕】。原来 image / nfo 也开着，两个都踩过坑：
      #
      # image: 扫描阶段会把网盘里每一张图片【一张张拉到本地】。实测一个 WebDAV
      #   源里带着敦煌壁画摄影集，光下 DSC_xxxx.jpg 就跑了 38 分钟还没完 ——
      #   而海报本来就该由 Emby 去 TMDb 刮，网盘里那些图跟影视条目毫无关系。
      #   关掉之后扫描退化成纯列目录，快一个数量级。
      #
      # nfo: 更糟，它不只是慢。nfo 里带着 tmdbid，Emby 读到就把刮削身份按它回填，
      #   于是两个文件被认成同一部片、共用一份续播记录 —— 这正是之前花了好几天
      #   修的那个"进度条串台"。strip_nfo_ids() 这个函数存在的唯一原因就是它。
      #   源头关掉，比事后一遍遍去擦干净合理得多。
      #
      # 字幕留着：体积小，而且 Emby 没法替你从网盘取字幕，关了就真没有了。
      enable: true
      subtitle: true
      image: false
      nfo: false
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
    """MediaWarp：反代在 Emby 前面，拦截播放请求并 302 到网盘直链。

    【直链缓存时长在函数里现算，不要求调用方传】cfg 是好几条路各自拼出来的，每加
    一个键就得每条路都记得填，漏一条就是 KeyError —— 而这个函数一炸，整个「7 更新」
    就断在半路。
    """
    ttl = cfg.get("link_ttl") or link_ttl_of(cfg.get("install_dir")
                                             or ms_install_dir())[0]
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
  #
  # 【但这条只对夸克成立 —— 阿里的直链只活 15 分钟】阿里发的地址里带
  # x-oss-expires=900,过期后阿里直接拒。缓存比直链本身还长,后果是
  # MediaWarp 把一条【已经死掉】的地址 302 给播放器,播放器报"load fail",
  # 而日志里照样是一次漂亮的 302、体检也全绿 —— 因为体检每次都现换一条新的。
  # 表现就是"刚挂好能放,过一会儿就放不了了","有的片能放有的不能放"。
  # 所以这个值不是常数,要按【进了 Emby 的那些盘】取最短的那家,见 link_ttl_of()。
  # 注意是"进了 Emby"不是"挂在 OpenList 上"—— 没被扫进媒体库的盘不会被换直链,
  # 让它去压别人的缓存,只会把夸克那种能撑 30 小时的盘一起拖慢。
  alist_api_ttl: {ttl}
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
    # 【这里必须是【对外地址】，不能是 http://openlist:5244】WebDAV 源、本地目录这类
    # 代理型存储在网盘侧没有 CDN 直链，OpenList 只能回自己的 /d/ 地址 —— 而它是按
    # 【请求里的 Host】拼的（OpenList v4 把「网站 URL」那个设置去掉了，没有别的地方能改）。
    # 用容器内网名去问，拿回来的就是内网名，302 给手机电视就是解析不了。
    # 探不通时会自动退回内网地址，见 openlist_api_addr()。
    - addr: {openlist_api_addr(cfg)}
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
    # 「6 链路体检」靠它统计有多少陌生外网 IP 访问过 —— 这几个服务是公网可达的,
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
CLI_TEMPLATE = r'''#!/usr/bin/env bash
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
  selfupdate      只把脚本换成仓库里的最新版(不动镜像、不重启容器)
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
  selfupdate)
    # 【必须有一个手动入口】脚本自动更新本来只有每天一次的 cron。可是最需要
    # 换新脚本的时刻,恰恰是"刚发现一个 bug、修好了、想马上拿到"—— 那时候
    # 让人等到明天凌晨,或者去背 python3 /etc/bgpeer/media-stack.py selfupdate,
    # 都不合理。实测就撞上了:我让用户敲 media-stack selfupdate,而它根本不存在。
    S=/etc/bgpeer/media-stack.py
    [[ -f "$S" ]] || { echo "找不到 ${S}"; exit 1; }
    V0="$(grep -m1 '^SCRIPT_VERSION' "$S" | cut -d'"' -f2)"
    python3 "$S" selfupdate
    V1="$(grep -m1 '^SCRIPT_VERSION' "$S" | cut -d'"' -f2)"
    if [[ "$V0" == "$V1" ]]; then
      echo "脚本已是 v${V1}(没有变化)"
    else
      echo "脚本 v${V0} → ${b}v${V1}${r}"
      echo "上一版留在 ${S}.prev,出事可以换回去"
    fi ;;
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
SYNC_CRON      = "/etc/cron.d/media-stack-sync"
WARM_CRON      = "/etc/cron.d/media-stack-warm"
SELFUP_CRON    = "/etc/cron.d/media-stack-selfupdate"
# 每几小时热一次「继续观看」的直链。必须【小于】MediaWarp 的 alist_api_ttl(2h)，
# 否则缓存会在两次预热之间过期，等于白跑。1 小时留了一倍余量。
WARM_EVERY_H   = 1
# 单步的 socket 超时。注意它【不是总时限】：urllib 的 timeout 管的是单次读写等待，
# 只要对端还在断续地回数据就不会触发 —— 实测有一部花了 100 秒。真正兜底的是
# WARM_BUDGET 那个整轮预算
WARM_STEP_T    = 45     # 后台跑，等久点没关系 —— 热不成才是白跑
WARM_BUDGET    = 600    # 整轮封顶（秒）。用满就收工，剩下的交给一小时后那轮
WARM_RETRY     = 2      # 每部最多试几次。跨境超时多是偶发，隔一轮再试往往就成了
WARM_LIMIT     = 10     # 优先批热几部。「继续观看」里靠前的那几部才是真会被点开的
# 【优先批之外还要轮全库】「继续观看」+ 最近新加盖不到【看完过的老片】：有播放记录
# 所以不在「继续观看」，不是新加的所以不在 Latest 里，于是永远是冷的。
# 按顺序轮着热，下一轮接着上一轮的位置往后走，转到头再从头开始。
WARM_REST      = 20
# MediaWarp 缓存直链的时长（小时）。改这个要同时改 gen_mediawarp_conf 里的
# alist_api_ttl —— 下面那个门槛就是拿它算的。
LINK_TTL_H     = 2
# 【各家直链自己能活多久】缓存绝对不能比这个长，长了就是把死地址 302 给播放器。
# 数字读自直链地址本身：阿里 x-oss-expires=900 → 15 分钟；夸克 auth_key 约 30 小时。
# 没列进来的驱动按夸克那档算（2 小时，这是这套东西一直在跑的值）。
LINK_LIFE_MIN = {"aliyundriveopen": 15}
# 取最短那家之后还要再打个折 —— 缓存正好等于有效期的话，边界上那一次必死。
LINK_TTL_SAFE = 0.6
# 【轮转全库只在小库上成立】有效覆盖 = 每小时热几部 × 缓存能活几小时 = 20 × 2，
# 也就是任何时刻只有 40 部是热的。一万部的库轮一圈要 500 小时，等轮回来第一批早凉了
# 249 次 —— 覆盖率 0.4%，代价却是一天 480 次真实的换直链请求，全打在夸克那个风控较
# 严的接口上，还会把真正想看的那一部挤慢。
# 所以门槛就是"一圈能不能在缓存过期前跑完"，超了整批不做，只留优先批。
WARM_ROTATE_MAX = WARM_REST * (LINK_TTL_H // WARM_EVERY_H)
# 每部之间歇一下。AutoFilm 的配置里为同一个理由留了 wait_time: 0.2（"夸克风控较严"）。
# 连着打十几个换直链请求容易被风控盯上，那会连累列目录、播放一起超时。
WARM_GAP       = 2
WARM_BYTES     = 65536  # 每部拉多少字节 —— 够让网盘把那一段准备好，又不占带宽
# 每天对齐一次的时刻（北京时间），钉在 AutoFilm 生成 strm 之后半小时 —— 先有
# 文件，再去清失效、补时长。和 DEFAULT_STRM_CRON 一起改
SYNC_HOUR_CST  = "05:45"
# 刷目录缓存的时刻。必须【早于】AutoFilm 生成 strm 的 05:15（见 DEFAULT_STRM_CRON），
# 又不必太早 —— 刷完到开扫之间隔得越久，这中间新加的片子越可能又赶不上。5 分钟够。
PRECACHE_HOUR_CST = "05:10"
# 自动更新【只换脚本，不动容器、不重生成配置】。放在每日对齐之前一点，
# 换完那一轮对齐就是新版脚本在跑。见 do_selfupdate 里为什么只换脚本。
SELFUP_HOUR_CST = "05:15"
# 每 20 分钟一次 ≈ 72 次/天。别为了"让链路更热"去调小它 —— 实测耗时和空闲时间
# 不相关，理由见 do_keepalive() 的文档字符串。
KEEPALIVE_MIN  = 20

# ---- 定时任务的互斥和超时 ----------------------------------------------------
# cron 的规矩是"到点就起，不管上一轮跑完没有"。三条任务原来都是裸命令，于是任何一轮
# 卡住都会开始叠罗汉：实测现场十个 python3 进程、每个 ~145 M，合计 1.35 G，swap 吃满、
# 视频放不了 —— 而表现是"看片卡"，没有任何一行日志会说是自己造成的。
#
# 两道闸【必须都有】：
#   flock -n   同类任务同时只跑一个，后来的直接退出（任务本来就是幂等的，不用排队）
#   timeout    卡死的那个自己会被杀掉。只有 flock 的话，第一个吊死之后锁永远不放，
#              从"叠罗汉"换成"全停摆"，一样糟
HEAL_BG_BUDGET_T = 1800 + 600   # do_heal 的预算 + 余量，见 CRON_TIMEOUT["heal"]
CRON_LOCK_DIR  = "/run/lock"
# 超时都压在【下一次触发之前】：宁可这轮少做点，也不能和下一轮撞上。
# 三条任务本身都是幂等 + 带预算的，砍掉的部分下一轮会接着做。
CRON_TIMEOUT   = {
    "keepalive": KEEPALIVE_MIN * 60 - 120,   # 20 分钟一次 → 18 分钟
    "warm":      WARM_EVERY_H * 3600 - 300,  # 每小时一次 → 55 分钟
    "sync":      3 * 3600,                   # 每天一次，给足
    # 后台补时长。比自己的预算多留一截 —— timeout 是防吊死的最后一道，
    # 不该在任务正常收尾之前把它砍了
    "heal":      HEAL_BG_BUDGET_T,
    "selfupdate": 300,                       # 就一次 HTTPS 下载 + 语法自检
    "precache":   300,                       # 每个盘两个 HTTP 请求，不该跑这么久
}


def mediawarp_token_broken():
    """MediaWarp 手里的 OpenList 令牌是不是已经作废了。返回 (是不是, 日志原文)。

    MediaWarp 只在【启动那一刻】登录 OpenList 拿一个令牌，之后一直用那一个。
    OpenList 一重启（更新、切直链方式、改目录缓存、宿主机重启、被 OOM 杀）旧令牌
    就作废，而 MediaWarp 毫不知情 —— 换直链时拿到 401，整个请求以 404 收场。

    【不会立刻暴露】已经缓存了直链的片子照样能放（命中缓存 3 毫秒，根本不问
    OpenList），只有缓存里没有的才失败。等缓存陆续过期才慢慢变成"全都打不开"，
    而那时 OpenList 自己是好的（挂载页面里点开能播），最容易怀疑到 Emby 头上去。

    【只看本次启动之后的日志】docker restart 不清旧日志，固定 --since 6h 会把重启
    之前那些 401 一起读进来 —— 修好了还报故障，用户再重启一次，还是报。
    """
    try:
        r = sh("docker inspect -f '{{.State.StartedAt}}' mediawarp", timeout=30)
        since = (r.stdout or "").strip().strip("'") or "6h"
        r = sh(f"docker logs --since {since} mediawarp", timeout=60)
        log = ANSI_RE.sub("", (r.stdout or "") + (r.stderr or ""))
    except Exception:
        return False, ""
    return ("token is invalidated" in log or "响应状态码: 401" in log), log


def heal_mediawarp_token():
    """发现 MediaWarp 令牌作废就重启它。返回有没有重启。

    这个故障原来只有体检报得出来，而体检是【已经发现出事了】才会去跑的东西 ——
    偏偏它坏的那一刻什么都看不出来，等直链缓存全过期，人才发现"Emby 全放不了、
    挂载里却能播"，中间那几天没有任何提醒。挂在每小时的保活上，最多坏一小时。
    重启的代价是清空直链缓存，下面顺手预热补回来。
    """
    bad, _log = mediawarp_token_broken()
    if not bad:
        return False
    r = subprocess.run(["docker", "restart", "mediawarp"],
                       capture_output=True, timeout=120)
    return r.returncode == 0


def keepalive_state(d):
    try:
        with open(os.path.join(d, "keepalive.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def do_keepalive():
    """定时做一次真实的目录列举。

    ⚠ 这个功能的原始理由已经被实测推翻，保留但别再照着旧理由加码。

    原本的说法是"最近调用过，链路就是热的"，所以定时热一下能消掉第一次播放的转圈。
    不成立 —— 同一条路径按 30/60/120/300/600 秒的空闲间隔各采样，空闲 600 秒是全场
    第二快，空闲 30 秒出了最慢的一次：耗时和空闲时间【不相关】，波动来自跨境线路
    本身（晚高峰单次能飙到 120 秒，过了高峰又回到 3 秒）。

    所以别改 KEEPALIVE_MIN。留着它是因为成本极低（72 次/天）、能给体检提供一个
    "链路最近通没通"的心跳，不是因为它能保温。输出写 json，不往日志里堆东西。
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
    append_hist(d, rec)
    # 【顺手把 MediaWarp 的令牌管了】见 heal_mediawarp_token 的注释：这个故障
    # 会安静地烂好几天，而检测只要读一次容器日志，挂在这条已经每小时都在跑的
    # 任务上不多花任何代价。重启会清空直链缓存，紧接着预热一遍补回来。
    try:
        if heal_mediawarp_token():
            key = read_yaml_scalar(os.path.join(d, "mediawarp", "config",
                                                "config.yaml"), "auth")
            if key and wait_openlist_ready(d):
                time.sleep(5)          # 等 MediaWarp 起来并重新登录 OpenList
                warm_links(d, key)
    except Exception:
        pass


# 保活每 KEEPALIVE_MIN 分钟都在测同一条路径，可它一直把结果【覆盖】掉 —— 等于每次
# 都把证据扔了，「列目录到底是偶尔慢还是一直慢」只能靠翻聊天记录里的截图来吵。
# 改成追加一行 jsonl，探测本来就在跑，白得一条连续 24 小时的曲线。
KEEPALIVE_HIST = "keepalive-history.jsonl"
HIST_KEEP      = 1000            # 每 15 分钟一条 ≈ 10 天，足够看趋势又不会撑大


def append_hist(d, rec):
    """把一次探测结果追加进历史，顺便把老记录裁掉。"""
    path = os.path.join(d, KEEPALIVE_HIST)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # 裁剪不必每次都做：文件小的时候读一遍再写一遍纯属浪费
        if os.path.getsize(path) > HIST_KEEP * 120:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()[-HIST_KEEP:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except OSError:
        pass


def keepalive_history(d, hours=24):
    """读最近 hours 小时的探测记录。"""
    cut = time.time() - hours * 3600
    out = []
    try:
        with open(os.path.join(d, KEEPALIVE_HIST), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue                   # 写了一半的行，跳过就是
                if r.get("ts", 0) >= cut:
                    out.append(r)
    except OSError:
        pass
    return out


def _count_episodes(recs, gap=1):
    """失败分成几阵。相邻两次失败之间成功不超过 gap 次，就算同一阵。"""
    eps, since = 0, None                    # since = 距上一次失败过了几次成功
    for r in recs:
        if not r.get("ok"):
            if since is None or since > gap:
                eps += 1
            since = 0
        elif since is not None:
            since += 1
    return eps


def hist_stats(recs, recent_h=3):
    """把一串探测记录压成数字。没有样本返回 None。

    耗时分布【只统计成功的那些】—— 失败的耗时量的是超时设置（120 秒封顶），混进
    中位数会得出「一直很慢」这种和事实相反的结论。失败单独计数。

    另外分出「最近 recent_h 小时」：一次已经过去的故障会在 24 小时窗口里留下一堆
    失败，拿整窗失败率去报警等于让旧故障连报一整天 —— 而一直报警就等于没报警。
    """
    if not recs:
        return None
    now = time.time()
    ok = sorted(r.get("elapsed", 0) for r in recs if r.get("ok"))
    rec_n = [r for r in recs if r.get("ts", 0) >= now - recent_h * 3600]
    bads = [r for r in recs if not r.get("ok")]
    ts = [r.get("ts", 0) for r in recs]
    s = {"n": len(recs), "ok_n": len(ok), "bad": len(recs) - len(ok),
         "span_h": (max(ts) - min(ts)) / 3600,
         "recent_n": len(rec_n), "recent_bad": sum(1 for r in rec_n if not r.get("ok")),
         "last_bad_h": (now - max(r.get("ts", 0) for r in bads)) / 3600 if bads else None,
         # 失败【分成几阵】：一次掉线是一整阵，长期抽风是散开的好几阵。两者失败次数
         # 可以完全一样，该做的事却相反 —— 前者已经过去了，后者得去挖。
         #
         # 中间夹着一两次成功【不算断开】：!.XXXXX!X!X.!.......... 明明是同一阵掉线
         # （后面二十多次全通），按"碰到成功就断"去数是 3 段，就被判成长期抽风了。
         # 容差取 1 —— 再大的话，真正均匀散布的 .X..X..X. 也会被并成一阵。
         "bad_runs": _count_episodes(recs, gap=1),
         "med": 0.0, "p90": 0.0, "mx": 0.0, "slow": 0}
    if ok:
        s["med"] = ok[len(ok) // 2]
        s["p90"] = ok[min(len(ok) - 1, int(len(ok) * 0.9))]
        s["mx"] = ok[-1]
        s["slow"] = sum(1 for e in ok if e >= 5)
    return s


def hist_block(s, recs):
    """把历史排成几行，一眼能看清。返回 (第一行, [后续行])。

    全挤在一行里会在手机终端上折断，图例被劈成两截 —— 数据再准，看不清等于没有。
    拆成「耗时 / 失败 / 探测」三行，每行 ~50 字符内，探测图每 12 个分一组好数。
    """
    if not s["ok_n"]:
        return f"{s['n']} 次探测　{RED}全部失败{RST}", []
    rows = [f"{DIM}耗时{RST}  中位 {s['med']:.1f} 秒　九成 {s['p90']:.1f} 秒"
            f"　最慢 {s['mx']:.1f} 秒"
            + (f"　{s['slow']} 次 ≥5 秒" if s["slow"] else "")]
    if s["bad"]:
        when = (f"　最近一次 {s['last_bad_h']:.0f} 小时前"
                if s["last_bad_h"] is not None else "")
        rows.append(f"{DIM}失败{RST}  {RED}{s['bad']} 次{RST}／{s['n']} 次"
                    f"　分 {s['bad_runs']} 阵{when}")
    else:
        rows.append(f"{DIM}失败{RST}  {GREEN}没有{RST}／{s['n']} 次")
    spark = hist_spark(recs)
    for i, line in enumerate(spark):
        rows.append(f"{DIM}探测{RST}  {line}" if i == 0 else f"      {line}")
    rows.append(f"{DIM}      左旧右新　. 快　: 偏慢　! 很慢　X 失败{RST}")
    return f"最近 {s['span_h']:.0f} 小时　{s['n']} 次探测", rows


def hist_spark(recs, per_row=36, rows=3, group=12):
    """把最近的探测画成图，让人一眼看出失败是【扎堆】还是【一直在冒】。

    扎堆 = 一次已经结束的故障，什么都不用做；散布 = 链路长期抽风，那才要去挖。
    光给一个「7 次失败」的总数，这两种情况长得一模一样。

    每 group 个空一格，方便数。返回若干行（左旧右新，最后一行最新）。
    """
    cells = []
    for r in recs[-(per_row * rows):]:
        if not r.get("ok"):
            cells.append(f"{RED}X{RST}")
        else:
            e = r.get("elapsed", 0)
            cells.append(f"{GREEN}.{RST}" if e < 5 else
                         (f"{YELLOW}:{RST}" if e <= 30 else f"{RED}!{RST}"))
    out = []
    for i in range(0, len(cells), per_row):
        chunk = cells[i:i + per_row]
        out.append(" ".join("".join(chunk[j:j + group])
                            for j in range(0, len(chunk), group)))
    return out


def hist_verdict(s):
    """历史该不该报警。返回 (图标状态, 待办 或 None)。

    出过这么一屏：列目录 ✔ 3.2 秒、换直链 ✔ 1.0 秒、302 ✔，结论「全部正常」，而同屏
    的历史那行写着「31 次探测 … 7 次失败」。体检只看得见跑它那一瞬间，用户过的是
    那 9 个小时 —— 结论必须把历史算进去。
    """
    if not s or s["n"] < 8:
        return "ok", None                      # 样本太少，任何比例都是噪声
    rate = s["bad"] / s["n"]
    rec_rate = (s["recent_bad"] / s["recent_n"]) if s["recent_n"] >= 4 else None
    # 最近这几小时还在失败 —— 是【正在坏】
    if rec_rate is not None and rec_rate >= 0.2:
        return "bad", (f"网盘列目录最近 {s['recent_n']} 次探测失败了 {s['recent_bad']} 次"
                       f"（24 小时内共 {s['bad']}/{s['n']}）—— 播放器那边表现为"
                       f"「有时候点开打不开」",
                       "先看下面那行探测图：X 扎堆在一段 = 那会儿出过一次事、已经过去了；"
                       "均匀散布 = 网盘在对【列目录】这个接口限流。"
                       "存储真掉线了才去 OpenList 里停用再启用 —— 那一下会清掉这个盘的"
                       "目录缓存，正在被限流的时候反而更糟")
    # 失败散成好几段 —— 不是一次掉线，是长期抽风。哪怕此刻是通的也必须报：它的表现
    # 就是「有时候点开打不开、过一会儿又好了」，而每次去体检又都正常。
    # 【必须带上"最近还在坏"这个条件】少了它，一次已经过去的抽风会连报一整天。
    if rate >= 0.1 and s["bad_runs"] >= 3 and (s["last_bad_h"] or 99) < 4:
        return "bad", (f"24 小时内列目录失败 {s['bad']}/{s['n']} 次，而且散成 "
                       f"{s['bad_runs']} 阵（不是一次掉线，是长期抽风）",
                       "坏的是【列目录】这一个接口 —— 同屏的「换直链」要是绿的，就跟线路"
                       "无关，是网盘按账号在限流它。三件事有用：目录缓存别调短"
                       "（命中缓存的列目录根本不碰网盘接口）；少重启 OpenList"
                       "（缓存在内存里，一重启全清，之后每个目录第一次列都要走真实接口）；"
                       "别在凌晨 AutoFilm 扫库那会儿去翻挂载。探测是带 refresh 的，"
                       "平时在网页里翻目录多半比这个数好看")
    # 整窗有失败但连成一段、最近也干净 —— 坏过，已经好了，只提醒别报警
    if rate >= 0.1:
        ago = f"{s['last_bad_h']:.0f} 小时前" if s["last_bad_h"] is not None else ""
        return "warn", None if (s["last_bad_h"] or 0) >= 3 else (
            f"24 小时内列目录失败过 {s['bad']} 次，最近一次 {ago}",
            "最近几小时没再失败，多半是那会儿出过一次事。留意就行")
    if s["p90"] >= 30:
        return "warn", None
    return "ok", None


def cron_cmd(sub):
    """拼一条 cron 用的命令：超时 + 调本脚本的某个子命令。

    指向 os.path.realpath(__file__)：「更新」是原地替换这个文件的，所以 cron 会一直
    调到最新版，不用回来改 cron.d。

    【这里不能再套一层 flock(1)】外层 flock(1) 已经拿着那个文件的独占锁，它 exec
    出来的 python 又去 take_task_lock() 抢【同一个文件】。flock 锁跟着 open file
    description 走，python 是重新 open 的，属于另一个描述，于是必然冲突 —— LOCK_NB
    直接失败，子命令一次都没跑成。症状极隐蔽：cron 照常起、退出码还是 0（被 flock
    吞掉了），日志一个字没有，体检那行还是绿的。

    互斥交给进程内那把 fcntl 锁就够了，而且更好：不依赖 util-linux 装没装；进程无论
    怎么死内核都会自动放锁。timeout 留着 —— 它管的是"卡死的自己会被杀掉"。
    """
    cmd = f"python3 {os.path.realpath(__file__)} {sub}"
    if shutil.which("timeout"):
        cmd = f"timeout {CRON_TIMEOUT[sub]} {cmd}"
    return cmd


_TASK_LOCK_FH = None          # 必须活到进程结束：句柄一关，锁就放了


def take_task_lock(sub):
    """抢这个任务的互斥锁。拿到返回 True，已经有一轮在跑就返回 False。

    用 fcntl 而不是再 fork 一个 flock：锁跟着进程走，进程无论怎么死（被 timeout 杀、
    OOM、断电）内核都会自动放锁，不会留下一把没人认领的锁挡住后面所有轮。
    """
    global _TASK_LOCK_FH
    d = CRON_LOCK_DIR
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = "/tmp"
    try:
        import fcntl
        fh = open(os.path.join(d, f"media-stack-{sub}.lock"), "w")
    except (ImportError, OSError):
        return True                     # 建不了锁文件就别拦 —— 拦错了等于任务全停
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False                    # 锁被占着 —— 上一轮还在跑，安静退出
    _TASK_LOCK_FH = fh
    return True


def running_tasks():
    """列出当前在跑的本脚本 cron 进程：[(pid, 子命令, 已跑秒数), ...]。

    直接读 /proc，不为这一个功能引入 psutil。启动时刻取自 /proc/<pid>/stat 的第 22 个
    字段（时钟嘀嗒），配 /proc/uptime 换成秒。只认【本脚本 + 那三个 cron 子命令】并
    排除自己 —— 手点的那一份不算后台任务，误报会让人去杀自己正在用的进程。
    """
    me, self_path = os.getpid(), os.path.realpath(__file__)
    try:
        hz = os.sysconf("SC_CLK_TCK") or 100
        up = float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError):
        return []
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
            args = [a.decode("utf-8", "replace") for a in argv if a]
            if len(args) < 3 or self_path not in args:
                continue
            sub = args[-1]
            if sub not in CRON_TIMEOUT:
                continue
            # 【只认 python 本体】flock 和 timeout 是它的父进程，它们的 cmdline
            # 里也带着脚本路径和子命令 —— 不滤掉的话一个任务会被数成三个，
            # 体检当场报"叠了 3 个"，而实际一个都没叠。
            if not os.path.basename(args[0]).startswith("python"):
                continue
            # stat 的进程名里可能带空格和括号，只能从最后一个 ')' 之后切
            st = open(f"/proc/{pid}/stat").read()
            fields = st[st.rindex(")") + 2:].split()
            age = up - float(fields[19]) / hz          # 第 22 字段 = 下标 19
            out.append((int(pid), sub, max(0, int(age))))
        except (OSError, ValueError, IndexError):
            continue                    # 进程刚好退出了 —— 正常，跳过
    return sorted(out, key=lambda x: -x[2])


def reap_stale_tasks():
    """把还吊着的旧 cron 进程杀掉，返回杀掉的个数。

    只在「更新」时跑一次。装了 flock 之后新的不会再叠，但【已经堆在内存里的那些不会
    自己走】—— 它们是在没有 timeout 的年代起来的，会一直吊到重启。
    先 TERM 后 KILL：这些任务大多卡在网络读上，TERM 能让 Python 正常收尾。
    """
    pids = [p for p, _s, _a in running_tasks()]
    found = len(pids)
    if not found:
        return 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in pids:
            try:
                os.kill(p, sig)
            except OSError:
                pass                    # 已经没了
        if sig is signal.SIGTERM:
            time.sleep(3)
            pids = [p for p, _s, _a in running_tasks()]
            if not pids:
                break
    return found


def install_keepalive(install_dir):
    """装保活定时任务。用 cron.d 而不是 crontab -e：这样卸载时删一个文件就干净了。"""
    try:
        txt = (f"# media-stack 网盘链路保活：每 {KEEPALIVE_MIN} 分钟做一次目录列举，\n"
               f"# 把 token 和连接热着，避免第一次播放卡在「换直链」上转圈。\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               f"*/{KEEPALIVE_MIN} * * * * root {cron_cmd('keepalive')} "
               ">/dev/null 2>&1\n")
        with open(KEEPALIVE_CRON, "w") as f:
            f.write(txt)
        os.chmod(KEEPALIVE_CRON, 0o644)
        return True
    except OSError as e:
        warn(f"装保活定时任务失败（不影响使用）：{e}")
        return False


def cst_to_local_cron(hhmm):
    """北京时间 HH:MM → 本机时区下的 cron "分 时"。

    三个时钟必须对齐：AutoFilm 的调度器钉死在 Asia/Shanghai；cron 用的是【宿主机
    时区】（VPS 默认多半 UTC，也见过跟机房走的）；而对齐任务必须跑在生成之后，
    否则每天都在拿昨天的文件对齐。

    所以偏移只能【运行时从本机读】。tm_gmtoff 拿的是当前实际生效的偏移，夏令时也算
    在里面。按分钟算而不是按小时 —— 存在 +5:30 这种时区。
    """
    h, m = (int(x) for x in hhmm.split(":"))
    off = time.localtime().tm_gmtoff or 0            # 本机相对 UTC 的偏移（秒）
    total = (h * 60 + m + (off - 8 * 3600) // 60) % 1440
    return total % 60, total // 60


def do_selfupdate():
    """定时任务调的自动更新：【只把脚本换成仓库里的最新版】。结果写 json 给体检读。

    修好的东西到不了机器上，等于没修。

    【但只换脚本，不碰镜像也不重新生成配置】：
      · 镜像是 :latest，上游什么时候推破坏性改动我们管不了 —— 半夜自动拉一个坏版本
        下来，第二天整套停摆，而没人在场
      · 重新生成配置要重启容器，会打断正在看的人，也会作废 MediaWarp 的 OpenList 令牌
      · 而脚本这一层本来就不需要重启任何东西：cron 指向的就是这个文件

    【自动更新必须有回滚】语法层面的坏（下载被截断、拉到一页 HTML）能当场查出来：
    换上去之前先 compile 一遍，不过就整个放弃，一个字节都不动本机这份。
    """
    d = ms_install_dir()
    if not is_installed(d):
        return
    me = os.path.realpath(__file__)
    rec = {"ts": int(time.time()), "ok": False, "changed": False,
           "from": SCRIPT_VERSION, "to": "", "error": ""}
    try:
        if not os.access(me, os.W_OK):
            raise RuntimeError("脚本文件不可写")
        req = urllib.request.Request(f"{SELF_URL}?_t={int(time.time())}",
                                     headers={"User-Agent": "media-stack"})
        body = urllib.request.urlopen(req, timeout=60).read().decode()
        # 和 self_update 同一道门槛：别把 404 页面/限流提示写进去，那会废掉文件
        if "SCRIPT_VERSION" not in body or len(body) < 10000:
            raise RuntimeError("拉到的内容不像 media-stack.py")
        cur = open(me, encoding="utf-8").read()
        if body == cur:
            rec["ok"] = True                  # 已是最新，什么都不用做
        else:
            # 【换上去之前先 compile】语法坏掉的版本换进去，等于把这台机器上
            # 所有定时任务一起废掉 —— 而且下一轮自动更新也跑不起来，救不回来。
            compile(body, me, "exec")
            m = re.search(r'SCRIPT_VERSION\s*=\s*"([^"]+)"', body)
            with open(me + ".prev", "w", encoding="utf-8") as f:
                f.write(cur)                  # 留一份上一版，出事能手动换回去
            tmp = me + ".new"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(tmp, 0o755)
            os.replace(tmp, me)               # 原子替换，中途断电不会留半截脚本
            rec["ok"] = rec["changed"] = True
            rec["to"] = m.group(1) if m else "?"
    except Exception as e:
        rec["error"] = _short_err(e)
    try:
        with open(os.path.join(d, "selfupdate.json"), "w") as f:
            json.dump(rec, f, ensure_ascii=False)
    except OSError:
        pass


def install_selfupdate_cron(install_dir):
    """装每天一次的脚本自动更新。见 do_selfupdate。"""
    m, h = cst_to_local_cron(SELFUP_HOUR_CST)
    try:
        txt = (f"# media-stack 每天自动把脚本换成仓库最新版（只换脚本，\n"
               f"# 不拉镜像、不重生成配置、不重启任何容器）。\n"
               f"# 北京时间 {SELFUP_HOUR_CST}（本机 {h:02d}:{m:02d}），"
               f"排在每日对齐之前，换完那一轮就是新版在跑。\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               f"{m} {h} * * * root {cron_cmd('selfupdate')} >/dev/null 2>&1\n")
        with open(SELFUP_CRON, "w") as f:
            f.write(txt)
        os.chmod(SELFUP_CRON, 0o644)
        return True
    except OSError as e:
        warn(f"装自动更新任务失败（不影响使用）：{e}")
        return False


def install_sync_cron(install_dir):
    """装每天一次的自动对齐任务。

    清失效 strm、调续播门槛、补时长这三件事以前只在手点「5 生成媒体库」时才跑，而
    AutoFilm 每天那次定时【只生成、不做后面三步】，于是有两个洞：

      · 网盘里删掉/挪走的片子，Emby 里一直留着点不开的条目
      · 新建的媒体库续播门槛是默认的 120 秒 —— 短片子永远没有记忆。门槛是每个库
        各自一份的，而加库的动作在 Emby 里做，脚本这边毫无感知
    """
    m, h = cst_to_local_cron(SYNC_HOUR_CST)
    pm, ph = cst_to_local_cron(PRECACHE_HOUR_CST)
    try:
        txt = (f"# media-stack 每天两件事，顺序不能反：\n"
               f"# 北京时间 {PRECACHE_HOUR_CST}（本机 {ph:02d}:{pm:02d}）"
               f"刷目录缓存 —— 必须排在 AutoFilm 生成 strm【之前】，否则它列到的\n"
               f"# 是一份旧目录，新片一个都扫不进来，而任务照样报完成。\n"
               f"# 北京时间 {SYNC_HOUR_CST}（本机 {h:02d}:{m:02d}）对齐：清失效 strm、\n"
               f"# 给新媒体库调续播门槛、补时长、通知 Emby 扫描 —— 排在生成【之后】。\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               f"{pm} {ph} * * * root {cron_cmd('precache')} >/dev/null 2>&1\n"
               f"{m} {h} * * * root {cron_cmd('sync')} >/dev/null 2>&1\n")
        with open(SYNC_CRON, "w") as f:
            f.write(txt)
        os.chmod(SYNC_CRON, 0o644)
        return True
    except OSError as e:
        warn(f"装每日对齐任务失败（不影响使用）：{e}")
        return False


def do_precache():
    """AutoFilm 开扫【之前】几分钟，把要扫的那几个盘的目录缓存刷掉。安静跑。

    【补的是自动那条路上的一个洞】手点「5 生成媒体库」第一步就清缓存，所以点了就能
    把新片扫进来；而每天凌晨那条自动的路【从来不清】—— AutoFilm 到点就去列目录，
    OpenList 手里要是还压着一份旧的，它当然一个新文件都看不见，任务照样报"完成"。
    用户看到的就是"我记得设过每天自动扫描，怎么新片没进来"，而且查不出所以然：
    定时任务跑了、日志干净、strm 数没变，每一环看着都对。

    缓存设得越长这个洞越大：12 小时的话，凌晨 5 点那次看到的是前一天下午的目录。
    现在默认 30 分钟，洞小了但没堵上 —— 而且用户随时可以把它调回长的。

    代价几乎为零：每个盘两个 HTTP 请求（停用再启用），不重启容器，也不动别的盘。
    """
    d = ms_install_dir()
    if not is_installed(d):
        return
    mounts = {"/" + m for m in
              (strm_mount_dir(p) for p in effective_scan_paths(d)) if m}
    reload_storages(d, mounts)


def install_warm_cron(install_dir):
    """装定时预热。

    不能挂在每日对齐（05:45）里：MediaWarp 的直链缓存只有 2 小时，05:45 热完 07:45
    就过期了，而用户起床看片多半在那之后 —— 热在错的时间等于没热。所以单独一条、
    每小时一次，封顶 10 部（「继续观看」+ 最近新加），那正是最可能被点开的。

    这条任务后来还兼了 align_library()：新内容的库选项/时长/片名/身份也按小时跟上。
    """
    try:
        txt = (f"# media-stack 每 {WARM_EVERY_H} 小时跟一次新内容：\n"
               f"#   1. 把所有 strm 媒体库和条目拉到脚本认定的状态（续播门槛、\n"
               f"#      多版本合并、时长、片名、进度条身份）—— 新建的库/新加的片\n"
               f"#      最多一小时就和老片一样\n"
               f"#   2. 给「继续观看」和最近新加的片子提前换好直链，\n"
               f"#      省掉点播放时那几秒到几十秒的跨境等待\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               f"0 */{WARM_EVERY_H} * * * root {cron_cmd('warm')} "
               ">/dev/null 2>&1\n")
        with open(WARM_CRON, "w") as f:
            f.write(txt)
        os.chmod(WARM_CRON, 0o644)
        return True
    except OSError as e:
        warn(f"装预热定时任务失败（不影响使用）：{e}")
        return False


def do_warm():
    """cron 每小时调的：先把新内容拉齐，再预热直链。安静跑。

    对齐管「有没有进度条记忆」，预热管「第一次点开快不快」，缺哪个用户都会说
    "新片不行"，所以放在同一个小时级任务里，一起跟上。
    """
    d = ms_install_dir()
    if not is_installed(d):
        return
    key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"),
                           "auth")
    if not key:
        return
    align_library(d, key)
    warm_links(d, key)
    # 【跑完必须留个时间戳】否则体检没办法分辨"在跑"和"装了但从没跑成"。
    # 写在最后：中途炸了就不该留下"跑过了"的痕迹。
    try:
        with open(os.path.join(d, "warm.json"), "w") as f:
            json.dump({"ts": int(time.time())}, f)
    except OSError:
        pass


def follow_new_storages(d):
    """扫描路径设成「自动」时，把新挂的网盘补进 AutoFilm 的配置。

    auto 模式只在【重新生成配置那一刻】才去读 OpenList 已挂载的存储，而那一刻只发生
    在装机、改设置、点「5 生成媒体库」的时候。用户在 OpenList 里挂上一个新网盘之后，
    AutoFilm 的 source_dir 里根本没有它，那个盘里的片子永远不会变成 strm —— 而且不会
    有任何报错。

    只在【集合真的变了】的时候才写配置和重启，所以按小时跑没有代价。
    固定路径模式不碰 —— 那是用户明确指定的范围，替他扩大不是帮忙。
    """
    cfg = rebuild_cfg_from_disk(d)
    if cfg.get("scan_spec") != SCAN_AUTO:
        return []
    af = os.path.join(d, "autofilm", "config", "config.yaml")
    now = set(cfg.get("scan_paths") or [])
    old = set(read_yaml_all(af, "source_dir") or [])
    if not now or now == old:
        return []
    try:
        with open(af, "w") as f:
            f.write(gen_autofilm_conf(cfg))
        os.chmod(af, 0o600)
        sh("docker restart autofilm", timeout=120)
    except OSError as e:
        warn(f"更新 AutoFilm 扫描路径失败：{_short_err(e)}")
        return []
    added = sorted(now - old)
    if added:
        info(f"检测到新挂的网盘，已加入扫描：{'、'.join(added)}")
    gone = sorted(old - now)
    if gone:
        info(f"这些存储在 OpenList 里没有了，已移出扫描：{'、'.join(gone)}")
    return added


def planned_strm_path(d, netdisk_path, scan_paths):
    """按当前扫描配置，这个网盘文件的 strm 【应该】落在宿主机的哪儿。"""
    best = ""
    for sp in scan_paths:
        if _under(netdisk_path, sp) and len(sp) > len(best):
            best = sp                       # 取最深的那条，扫描路径可能嵌套
    if not best:
        return ""
    rel = netdisk_path[len(best.rstrip("/")):].lstrip("/")
    if not rel:
        return ""
    return os.path.join(strm_root(d), STRM_SUBDIR, *strm_subpath(best).split("/"),
                        os.path.splitext(rel)[0] + ".strm")


def _progress_by_target(d, key):
    """{网盘路径: (续播位置, 是否看完)}。

    【按网盘路径归档，不按条目 id】条目 id 跟着文件路径走，strm 一挪位 Emby 就当成新
    条目、id 全变；而 strm 指向的那个网盘文件是不变的，只有它能把两边对上号。
    """
    out = {}
    try:
        users = _emby("/Users", key, timeout=20) or []
        libs = _emby("/Library/VirtualFolders", key, timeout=20) or []
    except Exception:
        return out
    uid = (users[0] or {}).get("Id", "") if users else ""
    if not uid:
        return out
    for lb in libs:
        pid = lb.get("ItemId")
        if not pid or not is_strm_lib(lb):
            continue
        try:
            r = _emby(f"/Users/{uid}/Items?ParentId={pid}&Recursive=true"
                      f"&IncludeItemTypes=Movie,Episode,Video"
                      f"&Fields=Path,UserData", key, timeout=60)
        except Exception:
            continue
        for i in r.get("Items") or []:
            ud = i.get("UserData") or {}
            pos, played = ud.get("PlaybackPositionTicks") or 0, bool(ud.get("Played"))
            if not pos and not played:
                continue                    # 没看过的不用记
            host = _strm_host_path(d, i.get("Path") or "")
            if not host or not os.path.exists(host):
                continue
            try:
                t = strm_target_path(open(host, encoding="utf-8").read())
            except OSError:
                continue
            if t:
                out[t] = (pos, played)
    return out


def _restore_progress(d, key, saved):
    """迁移后把续播点按网盘路径贴回新条目。返回贴回几个。"""
    if not saved:
        return 0
    try:
        users = _emby("/Users", key, timeout=20) or []
        libs = _emby("/Library/VirtualFolders", key, timeout=20) or []
    except Exception:
        return 0
    uid = (users[0] or {}).get("Id", "") if users else ""
    n = 0
    for lb in libs:
        pid = lb.get("ItemId")
        if not pid or not is_strm_lib(lb) or not uid:
            continue
        try:
            r = _emby(f"/Users/{uid}/Items?ParentId={pid}&Recursive=true"
                      f"&IncludeItemTypes=Movie,Episode,Video&Fields=Path",
                      key, timeout=60)
        except Exception:
            continue
        for i in r.get("Items") or []:
            host = _strm_host_path(d, i.get("Path") or "")
            if not host or not os.path.exists(host):
                continue
            try:
                t = strm_target_path(open(host, encoding="utf-8").read())
            except OSError:
                continue
            if t not in saved:
                continue
            pos, played = saved[t]
            try:
                _emby(f"/Users/{uid}/Items/{i.get('Id')}/UserData", key,
                      method="POST",
                      body={"PlaybackPositionTicks": pos, "Played": played},
                      timeout=30)
                n += 1
            except Exception:
                pass
    return n


# ---- 按关键词自动建媒体库 -----------------------------------------------------
# 匹配到关键词的文件夹，底下的视频整合成一个媒体库；成人库自动带上 MetaTube。
# 这补的是整套东西里最后一段手工活 —— 以前「到 Emby → 添加媒体库 → 选内容类型 →
# 填路径 → 选语言 → 再去另一个页面勾 MetaTube」全靠人记，每挂一个新盘都要重来一遍。
# 忘一步的后果都不小：内容类型选错，剧集的每一集会变成独立电影；语言留空，中文片名
# 一张海报都刮不出来；MetaTube 忘了关，它会跑去动画库里配 JAV 封面。
#
# 三条取舍：【按文件夹名匹配，不按路径】（同一个关键词散在好几个盘里，全收进同一个
# 库才是"整合"）；【匹配到就不再往下钻】；【只碰自己建的库】—— 名字撞上了也不动，
# 动别人的库可能把观看记录连根拔了。
LIB_RULES_DEFAULT = [
    {"name": "电影",   "kw": ["电影", "movies", "movie"],           "type": "movies",  "mt": False, "lang": "zh", "country": "CN"},
    {"name": "电视剧", "kw": ["电视剧", "剧集", "连续剧", "tv"],     "type": "tvshows", "mt": False, "lang": "zh", "country": "CN"},
    {"name": "动漫",   "kw": ["动漫", "动画", "番剧", "anime"],      "type": "tvshows", "mt": False, "lang": "zh", "country": "CN"},
    {"name": "动漫电影", "kw": ["动漫电影", "剧场版"],               "type": "movies",  "mt": False, "lang": "zh", "country": "CN"},
    {"name": "纪录片", "kw": ["纪录片", "纪录", "documentary"],      "type": "movies",  "mt": False, "lang": "zh", "country": "CN"},
    # 【兜底往安全的方向倒】private 默认开着。这份内置名单是拉不到仓库那份规则文件
    # 时才用的。那种时候如果成人库对所有账号可见，用户多半还不知道有这回事 ——
    # 而这个疏忽是当场、当着人暴露的。想让所有账号都能看，规则里显式写 private: false。
    {"name": "AV影片", "kw": ["av", "写真", "番号"],                 "type": "movies",  "mt": True,  "lang": "ja", "country": "JP", "private": True},
]
LIB_TYPES = {"movies": "电影", "tvshows": "电视剧", "homevideos": "家庭影像", "": "混合"}


# 仓库拉下来的那份。【别手改】—— 每次「更新」都会被仓库版覆盖
LIB_RULES_FILE = "library-rules.yaml"
# 老版本菜单里 a / d 写下的本机覆盖。【现在的脚本不再写它】—— 要自定义就填链接，在
# 仓库/gist 里改。但装过老版本的机器上可能有这个文件，还认它（否则人家的规则会突然
# 消失），而且会在菜单和体检里明写出来 + 给删除命令，不让它继续当一层看不见的东西。
LIB_RULES_LOCAL = "library-rules.local.yaml"
LIB_RULES_CUSTOM = "library-rules.custom.yaml"


def lib_rules_path(d, local=False, custom=False):
    """规则文件在本机的落点。

    【作者的和自定义的各存一份，互不覆盖】切回作者时不用重新联网。只留一份的话，
    切一次就得联网拉一次，网那边一抽风就切不回去了。
    """
    if custom:
        return os.path.join(d, LIB_RULES_CUSTOM)
    return os.path.join(d, LIB_RULES_LOCAL if local else LIB_RULES_FILE)


def rules_source():
    """当前用哪一套规则："author"（作者的）或 "custom"（自定义链接）。

    【没填链接就一律算作者的】只记了 custom 却把链接删了，等于没有来源。
    """
    st = ms_state()
    if st.get("rules_src") == "custom" and (st.get("rules_url") or "").strip():
        return "custom"
    return "author"


def rules_url_of(src=None):
    src = src or rules_source()
    return (ms_state().get("rules_url") or "").strip() if src == "custom" else RULES_URL


def set_rules_source(src):
    save_ms_state(rules_src="custom" if src == "custom" else "author")


def set_rules_url(url):
    """存自定义链接。传空串 = 删掉，并且【同步切回作者的】。

    链接没了还留在 custom 上，就成了"选了一个不存在的来源"。
    """
    url = (url or "").strip()
    if url:
        save_ms_state(rules_url=url)
    else:
        save_ms_state(rules_url="", rules_src="author")


def fetch_lib_rules(d, src=None, url=None):
    """把【当前这套】规则拉到本机。返回 True 表示拉到了新内容。

    带时间戳绕开 raw.githubusercontent 的 CDN 缓存 —— 不绕的话"刚推的改动"拉下来还是
    旧的，看起来就像改了没用。

    拉失败【不是错误】：机器上还留着上一次那份。而【解析不出规则就一个字都不写】：
    拉到一页 404 或者限流提示照写进去的话，下一轮所有媒体库会当场消失。
    """
    src = src or rules_source()
    u = url if url is not None else rules_url_of(src)
    if not u:
        return False
    try:
        req = urllib.request.Request(f"{u}{'&' if '?' in u else '?'}_t={int(time.time())}",
                                     headers={"User-Agent": "media-stack"})
        body = urllib.request.urlopen(req, timeout=20).read().decode()
    except Exception:
        return False
    if not parse_lib_rules(body):
        return False
    path = lib_rules_path(d, custom=(src == "custom"))
    try:
        if os.path.exists(path) and open(path, encoding="utf-8").read() == body:
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return True
    except OSError:
        return False


def lib_rules(d=None):
    """当前生效的关键词规则，以及它是从哪来的。返回 (规则列表, 来源说明)。

    【来源是一个显式的单选，不是隐式优先级】作者的 / 自定义链接，选哪个用哪个；
    「5 生成媒体库」「每小时对齐」「体检」「7 更新」全走这个函数，所以自动跟着走。

    本机覆盖文件还认（老版本的 a/d 菜单写过它）。它仍然盖过链接，但会在菜单和体检里
    【明写出来】并给出删除命令，不再是一层看不见的东西。

    【解析不出规则时不覆盖用户的文件】文件在但一条都没读出来，多半是手改坏了。
    """
    d = d or ms_install_dir()
    src = rules_source()
    tries = [(lib_rules_path(d, local=True), "本机覆盖文件（盖过链接）"),
             (lib_rules_path(d, custom=(src == "custom")),
              "自定义链接" if src == "custom" else "作者的")]
    for path, label in tries:
        try:
            if not os.path.exists(path):
                continue
            got = parse_lib_rules(open(path, encoding="utf-8").read())
        except OSError:
            continue
        if got:
            return got, f"{label} {path}"
        warn(f"{path} 里没解析出规则（格式改坏了？），这次先用默认的。")
        print(f"  {DIM}原文件没有被改动。{RST}")
        break
    return ([dict(r) for r in LIB_RULES_DEFAULT],
            "内置默认（还没拉到" + ("自定义那份）" if src == "custom" else "仓库那份）"))


def parse_lib_rules(text):
    """解析规则文件。返回规则列表；解析不出来就返回空。

    【不用 YAML 库】这脚本一路下来都没有第三方依赖，而且用户机器上装没装是未知数，
    import 失败的话整个菜单就打不开了。只认自己写出来的那个形状（一条规则一个 - 块，
    底下 key: value），它同时也是合法 YAML。

    解析【只跳过看不懂的行，不抛异常】：配置是给人手改的，改坏一个字符就崩太脆。
    """
    rules, cur = [], None
    for raw in (text or "").splitlines():
        ln = raw.split("#", 1)[0].rstrip()
        if not ln.strip():
            continue
        if ln.lstrip().startswith("- "):
            if cur and cur.get("name"):
                rules.append(cur)
            cur = {"name": "", "kw": [], "type": "movies", "mt": False,
                   "lang": "zh", "country": "CN", "scr": [], "img": [],
                   "private": False, "epnum": None}
            ln = ln.lstrip()[2:]
        if cur is None or ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        k, v = k.strip().lower(), v.strip().strip('"').strip("'")
        if k == "name":
            cur["name"] = v
        elif k == "type":
            cur["type"] = v if v in ("movies", "tvshows") else "movies"
        elif k == "metatube":
            cur["mt"] = v.lower() in ("true", "yes", "y", "on", "1")
        elif k == "private":
            cur["private"] = v.lower() in ("true", "yes", "y", "on", "1")
        elif k in ("episode_number", "episode_numbers", "ep_number"):
            # 【三态，不是布尔】没写 = None = "按开关问一次"；写了就以规则文件为准，
            # 一句都不问。写 false 的库还会把脚本写过的编号文件清掉。
            cur["epnum"] = v.lower() in ("true", "yes", "y", "on", "1")
        elif k in ("scrapers", "metadata"):
            cur["scr"] = [x.strip() for x in re.split(r"[,，]", v.strip("[]")) if x.strip()]
        elif k in ("image_scrapers", "images"):
            cur["img"] = [x.strip() for x in re.split(r"[,，]", v.strip("[]")) if x.strip()]
        elif k in ("language", "lang"):
            cur["lang"] = v or "zh"
        elif k == "country":
            cur["country"] = v or "CN"
        elif k == "keywords":
            v = v.strip("[]")
            cur["kw"] = [x.strip() for x in re.split(r"[,，]", v) if x.strip()]
    if cur and cur.get("name"):
        rules.append(cur)
    return [r for r in rules if r["name"] and r["kw"]]


def _kw_hit(dirname, kw):
    """文件夹名命不命中这个关键词。【要一模一样，不是包含】

    子串匹配一路踩出三个坑：「av」命中 Java、Savage、上海AVI，这些文件夹会被塞进成人
    库还自动开 MetaTube；「电影」命中「动漫电影」，用户新建的规则怎么跑都不生效；
    补丁摞补丁之后规则本身变得难预测 —— 用户得先想清楚自己的关键词是不是别人的子串、
    谁排前面、谁更长，而他要的只是"这个文件夹归这个库"。

    一模一样才算，规则的含义只剩一句话。代价是 4K电影、我的电影 不再命中「电影」，
    想收就把它写进 keywords。只做两件宽容：去首尾空白、大小写不敏感。
    """
    return bool(kw) and (dirname or "").strip().lower() == kw.strip().lower()


def plan_libraries(d, rules=None):
    """按关键词规则算出每个库该收哪些路径：{库名: (类型, [容器路径...], 要不要 MetaTube)}。

    只收【底下真的有 strm 的】目录 —— 空壳目录建进去只会在 Emby 里多一个空条目。
    """
    rules = rules or lib_rules(d)[0]
    base = os.path.join(strm_root(d), STRM_SUBDIR)
    has_strm = {}

    def _count(path):
        """这个目录（含子目录）有几个 strm。算过就记下来，别重复走。"""
        if path in has_strm:
            return has_strm[path]
        n = 0
        for _r, _ds, fs in os.walk(path):
            n += sum(1 for f in fs if f.endswith(".strm"))
        has_strm[path] = n
        return n

    plan = {}
    stack = [base]
    while stack:
        cur = stack.pop()
        try:
            subs = sorted(x for x in os.listdir(cur)
                          if os.path.isdir(os.path.join(cur, x)))
        except OSError:
            continue
        for name in subs:
            full = os.path.join(cur, name)
            # 关键词是完全匹配，所以不存在"一个文件夹同时像两条规则"这种事 ——
            # 除非两条规则写了同一个关键词，那种情况按顺序取第一条就好
            hit = next((r for r in rules
                        if any(_kw_hit(name, k) for k in r["kw"])), None)
            if hit and _count(full):
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                plan.setdefault(hit["name"],
                                (hit["type"], [], hit["mt"],
                                 hit.get("lang") or "zh",
                                 hit.get("country") or "CN"))[1].append(
                    f"{STRM_PATH}/{rel}")
                continue            # 命中了就不再往下钻 —— 底下都归它
            stack.append(full)
        # 【浅的先匹配】用栈会先走到深处，而 /quark/电影/电影 这种嵌套下，
        # 应该是外层那个「电影」收走整棵子树。所以每层按名字排序压栈、
        # 命中即停，天然是自顶向下的。
    return plan


def _emby_add_library(key, name, ctype, paths, lang="zh", country="CN"):
    """在 Emby 里建一个媒体库。成功返回 ""，失败返回错误说明。

    顺手把【首选语言】一起设成中文。建库时留空的话 Emby 按服务器默认（通常是英文）
    去 TMDb 搜，中文片名一条都搜不到 —— 表现是"条目都在、一张海报都没有"。
    """
    # 【建库时就把刮削器带上】不带的话 TypeOptions 是空的，后面任何一次
    # "往里加 MetaTube" 都会把整份名单坐实成"只用这一个"。一开始就给它一份
    # 正确的，后面加减都是在正确的基础上改。
    tos = good_type_options(key, ctype)
    opts = {
        "PreferredMetadataLanguage": lang,
        "MetadataCountryCode": country,
        "PreferredImageLanguage": lang,
        "EnableRealtimeMonitor": False,   # strm 是脚本批量增删的，实时监控只会
                                          # 让 Emby 在扫库中途反复触发重扫
        "PathInfos": [{"Path": x} for x in paths],
    }
    if tos:
        opts["TypeOptions"] = tos
    q = (f"/Library/VirtualFolders?name={urllib.parse.quote(name)}"
         f"&refreshLibrary=false")
    if ctype:
        q += f"&collectionType={ctype}"
    for x in paths:
        q += f"&paths={urllib.parse.quote(x)}"
    try:
        _emby(q, key, method="POST", body=opts, timeout=90)
        return ""
    except Exception as e:
        return _short_err(e)


def _emby_add_path(key, name, path, lib_id=""):
    """给已有媒体库加一条路径。成功返回 ""，失败返回把几种调法都试过之后的说明。

    【一种调法不够，而且不能只看返回码】这个接口在 Emby 各版本里认的字段不一样，
    而 500 不告诉你是哪种原因（实测：往已有的库里加一条新挂的网盘路径，直接 500）。
    所以挨个试，每试一次【回读确认路径在不在库里】—— Emby 这套接口一贯是"收下请求"
    和"做了这件事"两回事：200 不代表加上了，500 也不代表没加上。
    """
    def _has():
        try:
            for lb in (_emby("/Library/VirtualFolders", key, timeout=20) or []):
                if (lb.get("Name") or "") == name:
                    return path in (lb.get("Locations") or [])
        except Exception:
            pass
        return False
    if _has():
        return ""
    q = urllib.parse.quote(str(name))
    qp = urllib.parse.quote(str(path))
    tries = (
        # Emby 4.9 的形状：只要 Name + PathInfo，不要 Id
        ("POST", "/Library/VirtualFolders/Paths?refreshLibrary=false",
         {"Name": name, "PathInfo": {"Path": path}}),
        # 带库 id 的形状（老版本认这个）
        ("POST", "/Library/VirtualFolders/Paths?refreshLibrary=false",
         {"Id": lib_id or name, "Name": name, "PathInfo": {"Path": path}}),
        # 更老的扁平写法
        ("POST", "/Library/VirtualFolders/Paths?refreshLibrary=false",
         {"Name": name, "Path": path}),
        # 全部塞 query
        ("POST", f"/Library/VirtualFolders/Paths?name={q}&path={qp}"
                 f"&refreshLibrary=false", None),
    )
    errs = []
    for method, ep, body in tries:
        try:
            _emby(ep, key, method=method, body=body, timeout=60)
        except Exception as e:
            errs.append(_emby_err(e))
        if _has():                 # 【以路径在不在库里为准】，不看上面那次的返回
            return ""
    return "；".join(dict.fromkeys(errs)) or "调用都成功了但路径没加上"


def _ol_token(d):
    """登 OpenList 拿一个 token。拿不到返回 ""。"""
    try:
        pw = read_env(os.path.join(d, ".secrets"), "OPENLIST_PASS",
                      fallback=os.path.join(d, ".env"))
        return (_ol_api("/api/auth/login",
                        {"username": "admin", "password": pw},
                        timeout=20).get("data") or {}).get("token", "")
    except Exception:
        return ""


def _emby_err(e):
    """把 Emby 的报错压成一行，带上它自己给的原因。

    HTTPError 直接 str() 只有「HTTP Error 500」，对定位没用。真正的原因在
    X-Application-Error-Code 头和响应体里，读出来才知道是哪种。
    """
    detail = ""
    try:
        detail = (e.headers.get("X-Application-Error-Code") or "").strip()
    except Exception:
        pass
    if not detail:
        try:
            detail = (e.read() or b"")[:200].decode("utf-8", "replace").strip()
        except Exception:
            detail = ""
    detail = re.sub(r"\s+", " ", detail)[:120]
    return f"{_short_err(e)}{('：' + detail) if detail else ''}"


def _emby_del_library(key, lb):
    """删掉一个媒体库。成功返回 ""，失败返回把几种调法都试过之后的错误说明。

    【一种调法不够，而且不能只看返回码】这个接口在 Emby 各版本里认的参数不一样
    （name / id、放 query 还是放 body），而 500 不告诉你是哪种原因。所以挨个试，每试
    一次【回读确认库是不是真没了】—— 200 不代表删掉了，500 也不代表没删掉（实测过
    500 之后东西其实没了的接口）。以库还在不在为准。
    """
    nm, iid = lb.get("Name") or "", lb.get("ItemId")
    q = urllib.parse.quote(str(nm))
    tries = (
        ("DELETE", f"/Library/VirtualFolders?name={q}&refreshLibrary=false", None),
        ("DELETE", f"/Library/VirtualFolders?id={iid}&refreshLibrary=false", None),
        ("DELETE", "/Library/VirtualFolders?refreshLibrary=false",
         {"Name": nm, "Id": iid, "RefreshLibrary": False}),
        ("POST", "/Library/VirtualFolders/Delete",
         {"Name": nm, "Id": iid, "RefreshLibrary": False}),
    )
    errs = []
    for method, path, body in tries:
        try:
            _emby(path, key, method=method, body=body, timeout=60)
        except Exception as e:
            errs.append(_emby_err(e))
        try:                       # 【以库还在不在为准】，不看上面那次的返回
            still = {x.get("Name") for x in
                     (_emby("/Library/VirtualFolders", key, timeout=20) or [])}
        except Exception:
            still = {nm}
        if nm not in still:
            return ""
    return "；".join(dict.fromkeys(errs)) or "调用都成功了但库还在"


def _netdisk_empty(d, lb, token):
    """网盘那边这个媒体库的目录是不是【确认】没东西了。

    三态，和 _dir_names 一脉相承：True 确认空 / False 还有东西 / None 问不出来。
    strm 树是网盘树的镜像，去掉 STRM_PATH 前缀就是网盘全路径。

    【为什么要问网盘，而不是数几轮 strm】"本地没有 strm"有两种完全不同的原因：网盘
    里那个目录真没了，或者 AutoFilm 还没扫到、扫失败了。只看本地分不出来。
    """
    if not token:
        return None
    got = []
    for p in (lb.get("Locations") or []):
        if not _under(p, STRM_PATH):
            return None
        names = _dir_names(p[len(STRM_PATH):] or "/", token)
        if names is None:
            return None          # 有一条问不出来，整个不算数
        got.append(bool(names))
    return (not any(got)) if got else None


def _lib_strm_count(d, lb):
    """这个媒体库所有路径底下一共有几个 strm。有路径不在 strm 树里就返回 -1。

    -1 的含义是"不归这儿管"：混着本地目录的库不是纯粹的网盘库，数字说明不了问题。
    """
    n = 0
    for p in (lb.get("Locations") or []):
        if not _under(p, STRM_PATH):
            return -1
        rel = [x for x in p[len(STRM_PATH):].split("/") if x]
        for _dp, _dn, fs in os.walk(os.path.join(strm_root(d), STRM_SUBDIR, *rel)):
            n += sum(1 for f in fs if f.endswith(".strm"))
    return n


def drop_empty_auto_libraries(d, key):
    """脚本自己建的库，底下 strm 全没了就删掉。返回删了几个。

    补上"能建不能删"这一半：在网盘里把「动漫电影」改名成「动漫剧场版」，脚本按新名字
    建了新库，旧的那个空壳却一直杵在 Emby 里 —— 首页轮播、「继续观看」照样推它的片，
    点开必然放不了。

    删的判据是【网盘的肯定回答】，不是"连着几轮没看见"：

      本地没 strm + 网盘说这儿没东西  → 删。证据齐了，不需要冷静期
      本地没 strm + 网盘说还有东西    → 不删。那是 AutoFilm 没扫到，库是好的
      本地没 strm + 网盘问不出来      → 不删，留着等下一轮

    另外三道闸：只删【脚本自己建的】（用户手建的一律不碰 —— 动别人的库可能把观看
    记录连根拔了）；库的每一条路径都得在 strm 树里；整棵 strm 树是空的时候整个不做
    （那说明上游出了问题，不是这些库该没了）。
    """
    mine = set(ms_state().get("lib_auto") or [])
    if not mine:
        return 0
    base = os.path.join(strm_root(d), STRM_SUBDIR)
    total = 0
    for _dp, _dn, fs in os.walk(base):
        total += sum(1 for f in fs if f.endswith(".strm"))
    if not total:
        return 0                       # 整棵树空 = 上游的事，一个库都别动
    try:
        libs = _emby("/Library/VirtualFolders", key, timeout=20) or []
    except Exception:
        return 0
    token, gone, waiting, lagging = "", [], [], []
    for lb in libs:
        nm = lb.get("Name") or "?"
        if nm not in mine:
            continue
        n = _lib_strm_count(d, lb)
        if n != 0:                     # 有片子，或者 -1（混了本地目录）
            continue
        # 到这儿才登 OpenList —— 没有空库的时候一个请求都不发
        token = token or _ol_token(d)
        empty = _netdisk_empty(d, lb, token)
        if empty is None:
            waiting.append(nm)
            continue
        if not empty:
            lagging.append(nm)
            continue
        # _emby_del_library 内部已经逐次回读确认了，返回 "" 就是真的没了
        err = _emby_del_library(key, lb)
        if err:
            warn(f"删空媒体库「{nm}」失败：{err}")
            print(f"  {DIM}几种调法都试过了。到 Emby → 设置 → 媒体库 → "
                  f"「{nm}」→ 删除，手动删一次就行；这个库本来就是空的，"
                  f"删掉不影响别的。{RST}")
            continue
        gone.append(nm)
    if gone:
        for nm in gone:
            mine.discard(nm)
        save_ms_state(lib_auto=sorted(mine))
    if waiting:
        print(f"  {DIM}这些库底下没有 strm 了，但网盘那边问不出来"
              f"（超时 / 存储掉线），先留着：{'、'.join(waiting)}{RST}")
    if lagging:
        warn(f"这些库底下一个 strm 都没有，可网盘里还有东西："
             f"{'、'.join(lagging)}")
        print(f"  {DIM}不是该删的库，是 strm 没生成出来 —— 多半 AutoFilm 那一轮"
              f"扫失败了。跑「6 链路体检」看网盘通不通。{RST}")
    if gone:
        ok(f"删掉 {len(gone)} 个空媒体库：{'、'.join(gone)}")
        print(f"  {DIM}本地一个 strm 都没有，网盘也明确回答那儿没东西了"
              f"（多半是在网盘里改了文件夹名）。留着的话首页轮播和"
              f"「继续观看」还会推这些片，点开必然放不了。{RST}")
        print(f"  {DIM}这些条目的观看记录跟着一起没了 —— 文件都不在了，"
              f"记录也接不回去。只删脚本自己建的库，你手建的不碰。{RST}")
    return len(gone)


# 文件名里已经有季集编号的样子：S01E01 / s1e1 / 1x01。有这个就别动。
EP_HAS_SE = re.compile(r"(?i)(?:s\d{1,2}[\s._-]*e\d{1,4}|\b\d{1,2}x\d{1,4}\b)")
# 父目录里的季号：Season 01 / S01 / 第 2 季
EP_SEASON = re.compile(r"(?i)^(?:season[\s._-]*|s)(\d{1,2})$|^第\s*(\d{1,2})\s*季$")


def episode_no(name):
    """从文件名里抠出集数。抠不出返回 0。

    只认【第一段独立的数字】：「231 4K」→ 231，「第154集」→ 154。分辨率那种数字必须
    躲开 —— 「4K」「1080p」「2160p」先删掉再找，否则「231 4K」会被抠成 4。
    """
    s = re.sub(r"(?i)\b\d{3,4}[pi]\b|\b[248]k\b", " ", name)
    m = re.search(r"\d{1,4}", s)
    return int(m.group()) if m else 0


def host_strm_path(d, p):
    """把 Emby 报的【容器内】strm 路径换算成【宿主机】上的路径。换不了返回 ""。

    Emby 在容器里看到的是 /data/strm/cloud/…，而这个脚本跑在宿主机上，同一个文件在
    <DATA_ROOT>/strm/cloud/…。直接拿 Emby 给的路径去 open() 必然 FileNotFoundError，
    而如果那个 except 是 continue，整件事就【一个字都不报】地什么都不做 —— 实测就是
    这么翻的：开关明明开着、跑起来也不报错，就是一个文件都不改。
    """
    if not _under(p, STRM_PATH):
        return ""
    rel = [x for x in p[len(STRM_PATH):].split("/") if x]
    return os.path.join(strm_root(d), STRM_SUBDIR, *rel)


def _episode_items(key):
    """Emby 里所有剧集条目（翻页拉完）。问不到返回 None。

    None 和 [] 差别很大：None 是"问不出来"，一个文件都不该动；[] 是"确实没有"。半路
    失败也返回 None —— 拿半份名单去判断"哪些没认出来"，会把没拉到的全当成不存在。
    """
    out, start, page = [], 0, 500
    while True:
        try:
            r = _emby(f"/Items?Recursive=true&IncludeItemTypes=Episode"
                      f"&Fields=ProviderIds,Path,IndexNumber,ParentIndexNumber,Name"
                      f"&StartIndex={start}&Limit={page}", key, timeout=60) or {}
        except Exception:
            return None
        items = r.get("Items") or []
        out.extend(items)
        total = int(r.get("TotalRecordCount") or 0)
        start += len(items)
        if not items or start >= total:
            return out


def cloud_name_stem(hp):
    """这个 strm 指向的网盘文件，去掉扩展名的那个名字。读不出来返回 ""。"""
    try:
        with open(hp, encoding="utf-8") as f:
            tgt = strm_target_path(f.read())
    except OSError:
        return ""
    return os.path.splitext(os.path.basename(tgt or ""))[0]


# 网盘文件名里除了集数就只剩这些的话，等于没有名字 —— 这些是画质/编码/字幕
# 标记，不是剧集名。「233 4K」去掉 233 和 4K 就什么都不剩了。
EP_JUNK = re.compile(
    r"(?i)^(4k|8k|2160p?|1440p?|1080p?|720p?|480p?|hd|fhd|uhd|sd|hdr10?|dv|"
    r"x26[45]|h\.?26[45]|hevc|avc|aac\d*|flac|ddp?\d?(\.\d)?|dts(-hd)?|truehd|"
    r"web-?dl|web-?rip|blu-?ray|bd-?rip|hd-?tv|remux|repack|\d{2,3}fps|"
    r"国语|日语|粤语|英语|双语|中字|中英|内封|内嵌|简体|繁体|外挂|字幕|无字|"
    r"高清|超清|蓝光|全集|完结|未删减|v\d)$")
# 【别把 · 当分隔符】U+00B7 那个点在中文人名/译名里是【名字的一部分】：
# 「断东河·吴」拆开就毁了。而 U+2022 的 • 是真的当分隔符用的
# （实测网盘里的「仙逆 [第154集•4K]」）。两个字符长得像，作用相反。
EP_SPLIT = re.compile(r"[\s._\-\[\]()（）【】•｜|/／]+")
# 集号本身的各种写法。名字里这一段要整个去掉 —— 留着就成了「仙逆 第154集•4K」
EP_MARK = re.compile(r"(?i)^(?:第\s*0*%d\s*[集话話]|e?p?0*%d|s\d{1,2}e0*%d)$")
# 带连字符的成组标记要【先整条去掉】，不能等切开再逐个认 —— WEB-DL 切成
# WEB 和 DL 之后哪个都不在上面那张表里，结果「初露锋芒」后面挂着个 WEB DL。
EP_JUNK_RUN = re.compile(r"(?i)\b(web[\s._-]?(dl|rip)|blu[\s._-]?ray|bd[\s._-]?rip|"
                         r"hd[\s._-]?tv|dvd[\s._-]?rip|h[\s._-]?26[45])\b")


def _xml_esc(s):
    """写进 nfo 之前转义。网盘文件名里出现 & 或 < 的话，不转义会让整个 nfo
    变成一个语法坏掉的 XML —— Emby 读不出来就当没有，编号和名字一起白写。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def episode_title_from_name(stem, ep, show=""):
    """给这一集起个显示用的名字。stem 是网盘文件名（不含扩展名）。

    【为什么要脚本自己起名】把集号按网盘的连续编号写死（S01E237）之后，刮削器那边根本
    没有"第 1 季第 237 集"这一集 —— 它的库里 237 是第 5 季第 29 集。对不上就给不出名字，
    标题栏只好一直挂着文件名「237 4K」。要真名就得把编号交还给刮削器，那样又会被拆成
    好几季，而用户要的正好相反。两个不能同时要，所以名字这一半自己来：

        「236.断东河·吴」        → 断东河·吴
        「237 4K」               → 第237集
        「仙逆 [第154集•4K]」     → 第154集   （剧名和画质标记都去掉）

    show 传剧名的话，名字里重复的剧名也去掉 —— Emby 本来就在剧集页里显示。
    """
    rest = EP_JUNK_RUN.sub(" ", stem)
    mark = re.compile(EP_MARK.pattern % (int(ep), int(ep), int(ep)), re.I)
    sh = (show or "").strip().lower()
    words = []
    for w in EP_SPLIT.split(rest):
        w = w.strip(" .、．_-–—:：")
        if not w or EP_JUNK.match(w) or mark.match(w):
            continue
        if sh and w.lower() == sh:
            continue                  # 剧名重复，去掉
        words.append(w)
    got = " ".join(words).strip(" -_.")
    return got or f"第{int(ep)}集"


def untitled_episodes(d, rules, key, items=None):
    """标题栏里放的还是文件名、刮削器给的真剧集名一直没进来的条目。

    返回 {strm 宿主机路径: 条目 id}；问不到返回 None。

    【为什么会卡在这儿】Emby 建条目时没有标题就拿文件名顶上，于是标题栏【不是空的】。
    后面再刮，ReplaceAllMetadata=false 的含义是"只补缺失的字段"—— 标题栏有东西 = 不缺
    = 不去问刮削器要。简介、海报都刮回来了，唯独名字永远停在「233 4K」。

    只认【和文件名一模一样】的。带着我们自己写的编号 nfo 的条目不算在内 —— 那些的名字
    由 nfo 给，交给刮削器重新识别只会把季集编号一起换掉。
    """
    if items is None:
        items = _episode_items(key)
    if items is None:
        return None
    roots = lib_strm_dirs(d, rules, [r["name"] for r in rules
                                     if (r.get("type") or "movies") == "tvshows"])
    out = {}
    for i in items:
        p = str(i.get("Path") or "")
        if not (p.endswith(".strm") and _under(p, STRM_PATH)):
            continue
        hp = host_strm_path(d, p)
        # roots 为空 = 问不出媒体库的路径，那就不按库筛（宁可多修，别一个不修）
        if not hp or (roots and not any(_under(hp, q) for q in roots)):
            continue
        if has_episode_nfo(hp):
            continue                  # 名字由 nfo 给，绝不能交给刮削器重新识别
        name = str(i.get("Name") or "").strip()
        if not name:
            continue
        stems = {os.path.splitext(os.path.basename(p))[0].strip(),
                 cloud_name_stem(hp).strip()}
        if name in stems:
            out[hp] = i.get("Id")
    return out


def misparsed_strm_names(d, key, items=None):
    """Emby 把集号认错了、或者压根没认出来的剧集。

    返回 {strm 宿主机路径: 条目 id}；问不到返回 None。

    【键是完整路径，不是文件名】剧集的文件名撞车是常态：「剧A/01.strm」和「剧B/01.strm」
    的 basename 一模一样，拿文件名当键，一部剧认错了会让另一部剧也被当成认错的。

    【判据是"Emby 认的集号和文件名里的数字对不对得上"】只挑"一个刮削源都没认出来"的
    太窄：实测「231 4K.mp4」被 Emby 认成【第 2 季第 31 集】（把三位数当成首位是季、后
    两位是集），条目刮出来了、有海报有简介，只有集号是错的 —— 而它恰恰最该改。所以拿
    网盘文件名里的数字当标准答案，和 IndexNumber 比，对不上就是认错了。

    None 是"问不出来"，一个文件都不该动；{} 是"确认没有认错的"。
    """
    if items is None:
        items = _episode_items(key)
    if items is None:
        return None
    out = {}
    for i in items:
        p = str(i.get("Path") or "")
        if not (p.endswith(".strm") and _under(p, STRM_PATH)):
            continue
        # 【名字里已经写着 SxxExx 的一律不算"认错"】写成 S01E238 之后，Emby 会拿 238
        # 去刮削器换算成"第 5 季第 30 集"，IndexNumber 变成 30 —— 那是【对的】，季是
        # TheTVDB 分的。不排除的话每轮都会把它算进"集号不对"，报出来的数字纯属吓人。
        if EP_HAS_SE.search(os.path.splitext(os.path.basename(p))[0]):
            continue
        has_id = bool({k: v for k, v in (i.get("ProviderIds") or {}).items()
                       if v and k.lower() != "trakt"})
        hp = host_strm_path(d, p)
        if not hp:
            continue
        want = episode_no(cloud_name_stem(hp))
        got = i.get("IndexNumber")
        if not has_id:
            out[hp] = i.get("Id")           # 谁都没认出来
        elif want and got is not None and int(got) != want:
            out[hp] = i.get("Id")           # 认出来了，但集号和文件名对不上
    return out


def plan_episode_renames(d, rules, broken=None):
    """给剧集库里【Emby 认不出来】的 strm 想一个它认得的名字。

    返回 [(strm 路径, 季, 集, 这个库在规则文件里是怎么声明的)]。不改任何东西，只算。
    最后那一项：True = 规则文件写了 episode_number: true（做，不问）；None = 没写
    （听全局开关的）。写了 false 的库根本不会出现在结果里。

    【判据是"Emby 到底认没认出来"，不是"文件名长什么样"】拿"文件名里没有 SxxExx"当
    条件太宽：Emby 本来就能从「236.断东河·吴.mp4」里认出集号，这种改了是净亏 —— 观看
    进度断掉，标题还变成更难看的「剧名 - S01E236」。所以 broken 传进来的是"Emby 确认
    没认出来的那些"；broken 是 None（问不到）时一个都不动。

    【为什么改 strm 而不是改网盘】Emby 解析季集靠【strm 的文件名】，播放靠【strm 的
    内容】，两者互不相干。改网盘则是动用户的东西，还要重扫、旧 strm 变废、观看记录
    一起断。另外 AutoFilm 下一轮会按原名再生成一个 strm（它只跳过同名的），所以这里还
    要认出重复品交给调用方删掉，否则同一集在 Emby 里会有两个条目。
    """
    out = []
    if not broken:
        return out                    # None=问不到、set()=没有坏的，都不动
    base = os.path.join(strm_root(d), STRM_SUBDIR)
    # 【三态跟着每一条走】规则文件里 episode_number 写了什么，就带到每个待办上；
    # 没写是 None，交给全局开关。写 false 的库这里【一个都不收】。
    tv = {r["name"]: r.get("epnum") for r in rules
          if (r.get("type") or "movies") == "tvshows" and r.get("epnum") is not False}
    if not tv:
        return out
    try:
        plan = plan_libraries(d, rules)
    except Exception:
        return out
    for name, val in plan.items():
        if name not in tv:
            continue
        decided = tv[name]
        for cpath in val[1]:
            rel = [x for x in cpath[len(STRM_PATH):].split("/") if x]
            for dp, _dn, fs in os.walk(os.path.join(base, *rel)):
                # 剧名取【strm 所在目录】的名字；在 Season 目录里就再往上一层
                parts = [x for x in dp.split(os.sep) if x]
                sea, si = 1, len(parts) - 1
                m = EP_SEASON.match(parts[si]) if parts else None
                if m:
                    sea = int(m.group(1) or m.group(2) or 1)
                    si -= 1
                show = parts[si] if si >= 0 else ""
                if not show:
                    continue
                for f in fs:
                    if not f.endswith(".strm"):
                        continue
                    if os.path.join(dp, f) not in broken:
                        continue          # Emby 认对了，别碰
                    stem = f[:-5]
                    if EP_HAS_SE.search(stem):
                        continue          # 名字里已经写着编号，Emby 还认错就不是改名能解决的
                    # 【集数从网盘文件名抠，不是从 strm 名】strm 可能被旧版改成过
                    # 「剧名 - S01E231」，从它抠第一段数字会得到 01。
                    # 网盘那个名字才是原始事实，而这条路从不动 strm 的内容。
                    ep = episode_no(cloud_name_stem(os.path.join(dp, f)))
                    if not ep:
                        continue          # 抠不出集数就别猜
                    out.append((os.path.join(dp, f), sea, ep, decided))
    return out


def _strm_items(key, uid):
    """{strm 完整路径: 条目}。覆盖【所有媒体库、所有网盘】，不按库名或类型挑。

    【键必须是完整路径，不能用文件名】剧集的文件名撞车是常态，「剧A/01.strm」和
    「剧B/01.strm」的 basename 都是 01.strm，拿它当键后一个会把前一个顶掉。

    【必须翻页】这个接口一次只给一批。写死 Limit=4000 的话，片子超过这个数就有一批
    永远轮不到，而且不会有任何报错。按 StartIndex 翻到底。
    """
    out, start, page = {}, 0, 500
    while True:
        try:
            r = _emby(f"/Users/{uid}/Items?Recursive=true"
                      f"&IncludeItemTypes=Movie,Episode,Video"
                      f"&Fields=Path,UserData"
                      f"&StartIndex={start}&Limit={page}", key, timeout=60) or {}
        except Exception:
            return out                # 半路失败就用已经拿到的，别整批丢掉
        items = r.get("Items") or []
        for i in items:
            p = str(i.get("Path") or "")
            if p.endswith(".strm") and _under(p, STRM_PATH):
                out[p] = i
        total = int(r.get("TotalRecordCount") or 0)
        start += len(items)
        if not items or start >= total:
            return out


# 观看进度按【网盘文件路径】存一份。
#
# Emby 把观看记录挂在条目上，而条目是按 strm 的【路径】认的，路径一变就是另一个条目，
# 进度接不回去 —— 而这套东西里路径变化是家常便饭：补季集编号会改名、AutoFilm 按新配置
# 重新生成会换目录、用户在网盘里挪一下文件也会。
# strm 的【内容】那条网盘路径才是稳定的，拿它当键，进度就跟着"哪个视频"走。
PROGRESS_MAP = "progress-map.json"


def _progress_map(d):
    try:
        with open(os.path.join(d, PROGRESS_MAP), encoding="utf-8") as f:
            v = json.load(f)
            return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _save_progress_map(d, m):
    try:
        write_atomic(os.path.join(d, PROGRESS_MAP), json.dumps(m, ensure_ascii=False))
    except OSError:
        pass


def _has_progress(ud):
    ud = ud or {}
    return bool(ud.get("PlaybackPositionTicks") or ud.get("Played")
                or ud.get("PlayCount"))


def sync_progress_map(d, key, just_zeroed=()):
    """记录 + 补回观看进度，按网盘路径认。返回补回了几条。

    一趟过，两个方向：
      · 条目【有】进度  → 记下来（连同当时的条目 id）
      · 条目【没有】进度 → 记录里有的话看情况补回去

    【补回去之前要分清"新条目"和"用户自己清的"】两者现在都是"没进度"，
    但该做的事正好相反。判据是条目 id：
      · id 和记录里的【一样】 = 同一个条目，进度从有变没 = 用户自己标了未播放。
        这时候要把记录删掉，否则下一轮又给他补回去，他怎么清都清不掉。
      · id 【不一样】 = 路径变了、Emby 重新建的条目。这才是要补的。

    just_zeroed 是【本轮脚本自己刚清零的条目 id】。它们看起来和"用户标了未播放"
    一模一样，但绝不能按那条走 —— 见下面那段注释。
    """
    just_zeroed = {str(x) for x in (just_zeroed or ())}
    if not key:
        return 0
    try:
        users = _emby("/Users", key, timeout=20) or []
    except Exception:
        return 0
    m, n, dirty = _progress_map(d), 0, False
    for u in users:
        uid = u.get("Id")
        if not uid:
            continue
        for path, it in _strm_items(key, uid).items():
            hp = host_strm_path(d, path)       # Emby 给的是容器路径，得换算
            if not hp:
                continue
            try:
                with open(hp, encoding="utf-8") as f:
                    tgt = strm_target_path(f.read())
            except OSError:
                continue
            if not tgt:
                continue
            iid, ud = it.get("Id"), (it.get("UserData") or {})
            slot = (m.get(tgt) or {}).get(uid) or {}
            if _has_progress(ud):
                cur = {"id": iid, "ts": int(time.time()), "d": {
                    "PlaybackPositionTicks": int(ud.get("PlaybackPositionTicks") or 0),
                    "PlayCount": int(ud.get("PlayCount") or 0),
                    "Played": bool(ud.get("Played")),
                    "IsFavorite": bool(ud.get("IsFavorite")),
                }}
                if slot.get("d") != cur["d"] or slot.get("id") != iid:
                    m.setdefault(tgt, {})[uid] = cur
                    dirty = True
                continue
            if not slot:
                continue
            if slot.get("id") == iid:
                if str(iid) in just_zeroed:
                    # 【这个零是脚本自己刚打上去的，不是用户清的】同一轮里
                    # clear_impossible_progress 清掉了"位置比片长还大"的条目，而那种
                    # 脏数据几乎全是身份串台串过来的，串台最爱找剧集下手。当成"用户标了
                    # 未播放"就会连备份记录一起删掉 —— 这一集连底稿都没了。
                    continue
                # 同一个条目、进度没了 = 用户自己清的，尊重他
                m[tgt].pop(uid, None)
                if not m[tgt]:
                    m.pop(tgt, None)
                dirty = True
                continue
            try:
                _emby(f"/Users/{uid}/Items/{iid}/UserData", key, method="POST",
                      body=slot.get("d") or {}, timeout=30)
                slot["id"] = iid
                m[tgt][uid] = slot
                dirty = True
                n += 1
            except Exception:
                pass
    # 网盘上已经没有的文件，记录也该跟着走 —— 否则这个文件只会越长越大。
    #
    # 【但"这次没扫到"绝不能当成"文件没了"】判据是本地 strm 目录，而它恰恰在最需要备份
    # 的那一刻最不可信：AutoFilm 正在重新生成、目录没挂上、权限不对、磁盘满了，每一种
    # 都会让这趟只数到零星几个，整张表被判成"全没了"。而多留几条废记录只是 json 大一点，
    # 删错一条是用户几百集的观看记录再也回不来。
    if len(m) > 200:
        root = os.path.join(strm_root(d), STRM_SUBDIR)
        counted = os.path.isdir(root)
        alive = set()
        if counted:
            def _walk_failed(_e):
                # os.walk 默认把出错的目录【静悄悄跳过】—— 那正是最危险的形状：
                # 少数了一批，却看不出少数过。出一次错这趟就整个不算数。
                nonlocal counted
                counted = False
            for dp, _dn, fs in os.walk(root, onerror=_walk_failed):
                for f in fs:
                    if not f.endswith(".strm"):
                        continue
                    try:
                        with open(os.path.join(dp, f), encoding="utf-8") as fh:
                            alive.add(strm_target_path(fh.read()))
                    except OSError:
                        counted = False      # 读不出来的那份，不能算它"没了"
        # alive 是【所有】strm 的目标，m 只装看过的那些，正常情况下 alive 远大于 m。
        # 反过来说明这趟数得不对（目录空了一半、正在重建），宁可不删。
        if counted and len(alive) >= len(m):
            gone = [k for k in m if k not in alive]
            if gone:
                for k in gone:
                    m.pop(k, None)
                dirty = True
    if dirty:
        _save_progress_map(d, m)
    if n:
        ok(f"{n} 条观看进度按网盘文件补回来了{DIM}（条目换了但视频还是那个）{RST}")
    return n


# 「补季集编号」这个设置的语义版本。问过的答案只对【当时那个问题】有效，机制一换就得
# 重新问 —— 否则等于拿旧答案回答新问题。
#   v1 问的是"要不要按文件名有没有 SxxExx 来改名"，理由还是我自己编的
#   v2 判据换成"Emby 认的集号 vs 网盘文件名里的数字"，对不上才改
#   v3 机制从"改 strm 文件名"换成"旁挂 .nfo"（不再丢进度、不再和 AutoFilm 打架）
#   v4 nfo 里多写一个【脚本自己起的】剧集名 —— 编号写死之后刮削器给不出名字，
#      要么名字自己起、要么把编号交回去让它拆季，这是个新的取舍
EP_FIX_V = 4


def ep_fix_setting():
    """"补季集编号"的当前设置。没问过、或者问的是老版本的问题，都返回 None。"""
    st = ms_state()
    if st.get("ep_fix_v") != EP_FIX_V:
        return None
    v = st.get("ep_fix")
    return bool(v) if v is not None else None


# 一轮最多叫 Emby 重新识别多少个条目。每一个都是一次联网刮削，
# 整库几千集一次性发出去，Emby 会排队排到天亮，还可能被刮削源限流。
EP_REIDENT_MAX = 120
# 已经叫它重新识别过、刮削器还是没给出真名字的那些。记下来别再重复要 ——
# 有些片子的剧集名在 TheTVDB/TMDB 里本来就是空的，那是要不到的，
# 不记账就会每小时对着同一批条目再发一轮，永远停不下来。
EP_TRIED_MAX = 3000


def refresh_items(key, ids):
    """让 Emby 重新读一遍这些条目的【本地文件】。返回成功发出去几个。

    【这里绝对不能用 ReplaceAllMetadata=true】那个的意思是"把本地这份扔了，全部按刮削
    器来"，季集编号也在其中。实测：nfo 写的是 S01E237，重新识别之后被 TheTVDB 按它自己
    的库改成 S5:E29，一部剧当场拆成 2 季 —— 而用户要的是分集不分季。
    false 才是对的：读 nfo、补空着的字段，编号和名字都以 nfo 为准。
    """
    n = 0
    for iid in ids:
        if not iid:
            continue
        try:
            _emby(f"/Items/{iid}/Refresh?MetadataRefreshMode=FullRefresh"
                  f"&ImageRefreshMode=Default"
                  f"&ReplaceAllMetadata=false&ReplaceAllImages=false",
                  key, method="POST", timeout=30)
            n += 1
        except Exception:
            continue
    return n


def reidentify_items(key, ids):
    """让 Emby 对这些条目【重新识别】。返回成功发出去几个。

    【只给"编号本来就归刮削器管"的条目用】true 的含义是"把本地这份扔了，全部按刮削器
    来"，季集编号也算在内 —— 带我们自己写的编号 nfo 的条目走了这条路就会被重排季集，
    一部剧当场拆成好几季。untitled_episodes 已经把那些排除掉了。

    为什么这些非得用 true：它们的标题栏里【已经有东西了】—— 就是那个文件名。false 只补
    缺失的字段，Emby 一看不缺，根本不会去问刮削器要真正的剧集名。

    图像不动：缩略图早刮好了，重下一遍是白花流量。观看进度不受影响（UserData 是另一套）。
    """
    n = 0
    for iid in ids:
        if not iid:
            continue
        try:
            _emby(f"/Items/{iid}/Refresh?MetadataRefreshMode=FullRefresh"
                  f"&ImageRefreshMode=Default"
                  f"&ReplaceAllMetadata=true&ReplaceAllImages=false",
                  key, method="POST", timeout=30)
            n += 1
        except Exception:
            continue
    return n


def episodes_without_image(d, rules, key, items=None):
    """自己没有缩略图的剧集。返回 {strm 宿主机路径: 条目 id}；问不到返回 None。

    【Emby 会拿剧集海报顶上，所以"看起来有图"】没有自己那张图的一集，界面上显示的是
    整部剧的封面 —— 一眼看过去每集都有图，只有挨着比才发现是同一张。
    ImageTags.Primary 有没有，就是"这一集有没有自己的图"。
    """
    if items is None:
        items = _episode_items(key)
    if items is None:
        return None
    roots = lib_strm_dirs(d, rules, [r["name"] for r in rules
                                     if (r.get("type") or "movies") == "tvshows"])
    out = {}
    for i in items:
        p = str(i.get("Path") or "")
        if not (p.endswith(".strm") and _under(p, STRM_PATH)):
            continue
        hp = host_strm_path(d, p)
        if not hp or (roots and not any(_under(hp, q) for q in roots)):
            continue
        if not (i.get("ImageTags") or {}).get("Primary"):
            out[hp] = i.get("Id")
    return out


def fix_episode_images(d, rules, key, items=None):
    """给没有自己缩略图的剧集补图。返回发出去几个。

    【只碰图，一个元数据字段都不动】MetadataRefreshMode=None —— 季集编号是脚本直接写进
    条目的，顺带刷元数据有把它按刮削器改回去的风险。

    【发过就记账】有些剧的分集图刮削源那边本来就没有，不记账就会每小时对着同一批再发
    一轮，永远停不下来。
    """
    if not key:
        return 0
    got = episodes_without_image(d, rules, key, items)
    if not got:
        return 0
    tried = set(ms_state().get("ep_img_tried") or [])
    todo = [(hp, iid) for hp, iid in got.items() if hp not in tried][:EP_REIDENT_MAX]
    if not todo:
        return 0
    n = 0
    for _hp, iid in todo:
        if not iid:
            continue
        try:
            _emby(f"/Items/{iid}/Refresh?MetadataRefreshMode=None"
                  f"&ImageRefreshMode=FullRefresh&ReplaceAllImages=false",
                  key, method="POST", timeout=30)
            n += 1
        except Exception:
            continue
    if not n:
        return 0
    tried.update(hp for hp, _i in todo)
    save_ms_state(ep_img_tried=sorted(tried)[-EP_TRIED_MAX:])
    ok(f"{n} 个剧集没有自己的缩略图（界面上顶着剧集海报），已去刮"
       f"{DIM}（只刮图，不碰编号和片名；后台跑）{RST}")
    if len(got) > len(todo):
        print(f"  {DIM}还有 {len(got) - len(todo)} 个排下一轮。{RST}")
    return n


def fix_episode_titles(d, rules, key, skip=(), items=None):
    """把标题栏还停在文件名上的剧集，叫 Emby 重新识别一次。返回发出去几个。

    【只管没有编号 nfo 的那些】写了 nfo 的条目名字从 nfo 来，交给刮削器重新识别会连季集
    编号一起换掉。

    skip 是这一轮刚刚单独动过的那些：Emby 是后台跑的，这会儿它们看起来仍然"没名字"，
    重复发还会把它们记进 ep_title_tried，等于把一次都没跑完的尝试当成"试过了"。
    """
    if not key:
        return 0
    got = untitled_episodes(d, rules, key, items)
    if not got:
        return 0
    tried = set(ms_state().get("ep_title_tried") or []) | set(skip)
    todo = [(hp, iid) for hp, iid in got.items() if hp not in tried]
    if not todo:
        return 0
    todo = todo[:EP_REIDENT_MAX]
    n = reidentify_items(key, [iid for _hp, iid in todo])
    if not n:
        return 0
    # 【发出去就记账，不等结果】重新识别是后台跑的，这里拿不到结论。
    # 只发一次就够：真有名字的下一轮自然就不在 untitled 里了；
    # 要不到名字的那些，记了账才不会每小时再来一遍。
    tried.update(hp for hp, _i in todo)
    save_ms_state(ep_title_tried=sorted(tried)[-EP_TRIED_MAX:])
    ok(f"{n} 个剧集的名字还是文件名，已叫 Emby {BOLD}重新识别{RST}"
       f"{DIM}（后台跑，名字过一会儿变）{RST}")
    if len(got) > len(todo):
        print(f"  {DIM}还有 {len(got) - len(todo)} 个排下一轮 —— "
              f"一次发太多会把 Emby 和刮削源都堵住。{RST}")
    return n


# 我们自己写的 .nfo 上的记号。只删带这一行的，绝不碰用户/AutoFilm 从网盘
# 下下来的那些 nfo。
EP_NFO_MARK = "<!-- media-stack: 只给 Emby 补季集编号，别的一概不写 -->"


def _show_of(strm_path):
    """这个 strm 属于哪部剧 —— 取它所在目录名；在 Season 目录里就再往上一层。

    只用来把标题里重复的剧名去掉，取错了最多是没去掉，不会写错编号。
    """
    parts = [x for x in os.path.dirname(strm_path).split(os.sep) if x]
    if not parts:
        return ""
    i = len(parts) - 1
    if EP_SEASON.match(parts[i]):
        i -= 1
    return parts[i] if i >= 0 else ""


def write_episode_nfo(strm_path, season, ep, show=""):
    """在 strm 旁边写一个只含季集编号的 .nfo。成功返回 True。

    【为什么改成写 nfo，不再改 strm 的文件名】改名这条路在跟 AutoFilm 打架，而且每轮都会
    输一次：

        AutoFilm 每次扫描 → 按网盘原名重新生成 231 4K.strm
        Emby 扫到         → 建一个新条目
        脚本下一轮        → 认出是重复品，删掉
        Emby 再扫         → 那个条目没文件了，删掉

    条目就这么反复出现和消失。用户正在放的时候撞上，播放器弹「找不到条目」；观看进度也
    活不过一轮 —— 两个症状同一个来源。nfo 没有这个问题：文件名不动，AutoFilm 认得它、
    不会重新生成，路径不变 = 条目不变 = 进度不丢。

    【写编号和名字，不写身份】不写 uniqueid/plot/演员 —— nfo 会成为刮削身份的第二份存档，
    从数据库里清掉多少次，下次扫描又灌回去。名字是例外，见 episode_title_from_name。
    """
    nfo = os.path.splitext(strm_path)[0] + ".nfo"
    title = _xml_esc(episode_title_from_name(
        cloud_name_stem(strm_path), ep, show or _show_of(strm_path)))
    body = (f'<?xml version="1.0" encoding="utf-8"?>\n'
            f"{EP_NFO_MARK}\n"
            f"<episodedetails>\n"
            f"  <title>{title}</title>\n"
            f"  <season>{int(season)}</season>\n"
            f"  <episode>{int(ep)}</episode>\n"
            f"</episodedetails>\n")
    try:
        if os.path.exists(nfo):
            with open(nfo, encoding="utf-8", errors="replace") as f:
                cur = f.read()
            if EP_NFO_MARK not in cur:
                return False          # 别人的 nfo，不动
            if cur == body:
                return False          # 已经是这样了，别白写
        with open(nfo, "w", encoding="utf-8") as f:
            f.write(body)
        return True
    except OSError:
        return False


def read_episode_nfo(strm_path):
    """读我们自己写的那个 nfo，返回 (季, 集)。不是我们的、或读不出来返回 None。"""
    try:
        with open(os.path.splitext(strm_path)[0] + ".nfo",
                  encoding="utf-8", errors="replace") as f:
            cur = f.read()
    except OSError:
        return None
    if EP_NFO_MARK not in cur:
        return None
    m1 = re.search(r"<season>(\d+)</season>", cur)
    m2 = re.search(r"<episode>(\d+)</episode>", cur)
    return (int(m1.group(1)), int(m2.group(1))) if m1 and m2 else None


def episode_number_mismatch(d, items):
    """nfo 里写的季集编号和 Emby 实际显示的对不上的那些。

    返回 [(strm 路径, 条目 id, 该是第几季, 该是第几集, 现在是第几季, 现在是第几集)]。

    【写下 nfo ≠ Emby 采纳了它】实测踩过：库的「元数据下载器（集）」名单被规则文件里的
    scrapers 覆盖成只有 TheTVDB/TheMovieDb，本地的 Nfo 读取器被挤了出去，脚本写的
    S01E231 一眼都没被看过。脚本一路打印"已补上编号"，外面看到的却是"还是没变"。
    """
    out = []
    for i in (items or []):
        p = str(i.get("Path") or "")
        if not (p.endswith(".strm") and _under(p, STRM_PATH)):
            continue
        hp = host_strm_path(d, p)
        want = read_episode_nfo(hp) if hp else None
        if not want:
            continue
        got_s, got_e = i.get("ParentIndexNumber"), i.get("IndexNumber")
        if got_s is None or got_e is None:
            continue                      # 还没扫出来，不算对不上
        if (int(got_s), int(got_e)) != want:
            out.append((hp, i.get("Id"), want[0], want[1], int(got_s), int(got_e)))
    return out


def count_episode_nfo(d):
    """脚本自己写下的季集编号 nfo 有几个。"""
    n = 0
    for dp, _dn, fs in os.walk(os.path.join(strm_root(d), STRM_SUBDIR)):
        for f in fs:
            if f.endswith(".nfo") and has_episode_nfo(
                    os.path.join(dp, os.path.splitext(f)[0] + ".strm")):
                n += 1
    return n


def has_episode_nfo(strm_path):
    """这个 strm 旁边有没有【我们自己写的】那种 nfo。"""
    try:
        with open(os.path.splitext(strm_path)[0] + ".nfo",
                  encoding="utf-8", errors="replace") as f:
            return EP_NFO_MARK in f.read()
    except OSError:
        return False


def upgrade_episode_nfo(d):
    """给早先写下的、只有编号没有名字的 nfo 补上名字。返回补过的 strm 路径。

    这些条目的编号已经补对了，从此不会再被判成"集号认错"，也就再不会进
    plan_episode_renames —— 光靠那条路，它们的名字永远补不上。
    """
    out = []
    for dp, _dn, fs in os.walk(os.path.join(strm_root(d), STRM_SUBDIR)):
        for f in fs:
            if not f.endswith(".nfo"):
                continue
            q = os.path.join(dp, f)
            try:
                with open(q, encoding="utf-8", errors="replace") as fh:
                    cur = fh.read()
            except OSError:
                continue
            if EP_NFO_MARK not in cur or "<title>" in cur:
                continue
            strm = os.path.splitext(q)[0] + ".strm"
            m1 = re.search(r"<season>(\d+)</season>", cur)
            m2 = re.search(r"<episode>(\d+)</episode>", cur)
            if not (m1 and m2 and os.path.exists(strm)):
                continue
            if write_episode_nfo(strm, int(m1.group(1)), int(m2.group(1))):
                out.append(strm)
    return out


def drop_episode_nfo(d, roots=None):
    """删掉【我们自己写的】那种 .nfo。返回删了几个。

    roots 给了就只清这几棵子树 —— 用在"某个库在规则文件里写了
    episode_number: false，别的库还开着"的时候。不给就是整棵 strm 树。
    """
    n = 0
    for root in (roots if roots is not None
                 else [os.path.join(strm_root(d), STRM_SUBDIR)]):
        for dp, _dn, fs in os.walk(root):
            for f in fs:
                if not f.endswith(".nfo"):
                    continue
                q = os.path.join(dp, f)
                try:
                    with open(q, encoding="utf-8", errors="replace") as fh:
                        if EP_NFO_MARK not in fh.read():
                            continue
                    os.remove(q)
                    n += 1
                except OSError:
                    continue
    return n


def lib_strm_dirs(d, rules, names):
    """这几个媒体库在【宿主机】上对应的 strm 目录。问不出来就返回空。"""
    if not names:
        return []
    try:
        plan = plan_libraries(d, rules)
    except Exception:
        return []
    out = []
    for name in names:
        for cpath in (plan.get(name) or (None, []))[1]:
            q = host_strm_path(d, cpath)
            if q and os.path.isdir(q):
                out.append(q)
    return out


def restore_strm_names(d):
    """把改过名的 strm 全部还原成网盘里那个文件名。返回还原了几个。

    【不需要记账就能还原】strm 的内容就是网盘全路径，取它的文件名换上 .strm 就是原来的
    名字 —— 改名从来没动过内容。

    什么时候用：用户把 ep_fix 关掉的时候。只是"以后不改了"的话，已经改坏的那些还留在
    那儿，等于关不掉。还原之后观看进度会自己回来（进度按网盘路径记，见 PROGRESS_MAP）。
    """
    n = 0
    for dp, _dn, fs in os.walk(os.path.join(strm_root(d), STRM_SUBDIR)):
        for f in fs:
            if not f.endswith(".strm"):
                continue
            src = os.path.join(dp, f)
            try:
                with open(src, encoding="utf-8") as fh:
                    tgt = strm_target_path(fh.read())
            except OSError:
                continue
            if not tgt:
                continue
            want = os.path.splitext(os.path.basename(tgt))[0] + ".strm"
            if want == f or not want.strip():
                continue
            dst = os.path.join(dp, want)
            try:
                if os.path.exists(dst):
                    os.remove(src)      # 原名那个已经在了（AutoFilm 重新生成的）
                else:
                    os.replace(src, dst)
                n += 1
            except OSError:
                continue
    return n


def fix_episode_strm_names(d, rules, key, interactive=True):
    """给 Emby 认错集号的剧集旁挂一个只写季集编号的 .nfo。返回 (补了几个, 0)。

    每个库先看规则文件里的 episode_number；没写的库才听全局开关的，开关第一次问一句，
    答案记在 ms_state["ep_fix"] 里。

    【为什么必须写死 S01】要的是分集不是分季。网盘里是一个扁平目录、集号连续到 200 多，
    而 Emby 会把「231 4K.mp4」里的三位数读成【第 2 季第 31 集】（首位当季、后两位当集）
    —— 实测现场就是这么歪的：231→31、232→32。写成 S01E231 才对得上。

    答 n 会把已经写下的编号文件【全部删掉】，不是"以后不补"而已 —— 那样等于关不掉。

    【规则文件里写了就不问】剧集库要不要补编号是【库的属性】，跟语言、刮削器一样，本来
    就该跟着库走。开关只管【没写的那些库】。
    """
    # 【改名那套整个撤了】任何被改过名的 strm 都是旧版残留：AutoFilm 每次扫描
    # 都会按原名再生成一个，两边永远在打架，条目反复生灭 —— 播放中「找不到条目」
    # 和进度记不住都是它。不管开关是什么状态，先无条件改回网盘原名。
    back = restore_strm_names(d)
    if back:
        ok(f"{back} 个 strm 改回网盘原名{DIM}（改名换成 nfo 了，不再和 AutoFilm 打架）{RST}")

    st = ep_fix_setting()
    tv = [r for r in rules if (r.get("type") or "movies") == "tvshows"]
    # 【关掉就要清干净】规则文件里写了 false 的库，加上"没写、而全局开关是关"的库，
    # 都要把脚本写过的编号文件删掉。没写 false 的库不受影响 —— 一个库的关不该
    # 把别的库一起关了，这正是"又只修好个别库"的反面。
    clean = [r["name"] for r in tv if r.get("epnum") is False
             or (r.get("epnum") is None and st is False)]
    if clean:
        _n = drop_episode_nfo(d, lib_strm_dirs(d, rules, clean))
        if _n:
            ok(f"清掉 {_n} 个季集编号文件（{'、'.join(clean)}），恢复成网盘原样")

    # 【一轮只拉一次条目表】下面三处都要用它：判集号、找没名字的、拿条目 id。
    all_items = _episode_items(key) if key else None
    bad = misparsed_strm_names(d, key, all_items) if key else None
    ids = ({host_strm_path(d, str(i.get("Path") or "")): i.get("Id")
            for i in (all_items or [])} if all_items else {})
    # 【早先只写了编号、没写名字的那批要补上】它们的编号已经对了，从此不会
    # 再被判成"集号认错"，光靠下面那条路永远轮不到它们 —— 外面看到的就是
    # "新加的好了，之前那批还是文件名"。
    up = upgrade_episode_nfo(d)
    if up:
        ok(f"{len(up)} 个条目补上了剧集名{DIM}（早先只写了编号）{RST}")
        refresh_items(key, [ids.get(p) for p in up])
    todo = plan_episode_renames(d, rules, bad)      # 写了 false 的库不在里面
    on = [t for t in todo if t[3] is True]          # 规则文件说做
    und = [t for t in todo if t[3] is None]         # 规则文件没说，听开关的
    if not todo:
        if bad is None:
            warn("剧集编号：问不到 Emby 的条目列表，这轮跳过")
        else:
            # 【编号没得补，不代表已经生效】这两件事是分开的：写下 nfo 之后
            # 条目就不再进 todo，可 Emby 认没认是另一回事 —— 早一版在这里直接
            # return，于是"写了没生效"这种失败从外面一点都看不见。
            verify_episode_numbers(d, rules, key, all_items)
            fix_episode_titles(d, rules, key, skip=up, items=all_items)
            fix_episode_images(d, rules, key, all_items)
            _names = [r["name"] for r in tv]
            print(f"  {DIM}剧集编号：Emby 判定集号不对的有 {len(bad)} 个，"
                  f"需要处理的 0 个（剧集库：{'、'.join(_names) or '一个都没有'}）{RST}")
        return 0, 0
    # 【只有"规则文件没写、开关也没答过"的库才问】规则文件里写了 true 的库照做，
    # 一句都不问 —— 用户已经在配置里明确表过态了，再拦一次是重复要答案。
    if und and st is None:
        if not (interactive and has_tty()):
            print(f"  {DIM}剧集编号：有 {len(und)} 个可以补，但这是后台在跑、没法问你。"
                  f"在规则文件里给这些库写一行 episode_number: true 就不用管了，"
                  f"或者到「3 后补参数 → 7」开一下{RST}")
        else:
            print()
            info(f"剧集库里有 {len(und)} 个条目，Emby 认错了集号或者没认出来。")
            print(f"  {DIM}可以在 strm 旁边放一个只写季集编号的 .nfo，让 Emby 认对。"
                  f"【文件名一个字不改，网盘也不动】—— 所以观看进度不会丢。{RST}")
            for src, sea, ep, _dc in und[:5]:
                print(f"    {DIM}{os.path.basename(src)}{RST}  →  "
                      f"{BOLD}S{sea:02d}E{ep:02d}{RST}")
            if len(und) > 5:
                print(f"    {DIM}…还有 {len(und) - 5} 个{RST}")
            print(f"  {DIM}季固定写 1，集号用网盘文件名里那个完整的数 —— Emby 会把"
                  f"「231 4K」的三位数读成【第 2 季第 31 集】，写死 S01E231 才对得上。"
                  f"分几季由刮削器决定，脚本不管。{RST}")
            print(f"  {DIM}不想每台机器都答一遍：在 library-rules.yaml 的剧集库上"
                  f"写一行 episode_number: true，以后一句都不问。{RST}")
            st = ask_yn("补吗？（以后不再问）", True)
            save_ms_state(ep_fix=bool(st), ep_fix_v=EP_FIX_V)
            if not st:
                return fix_episode_strm_names(d, rules, key, interactive=False)
    do = on + (und if st else [])
    n, wrote = 0, []
    for src, sea, ep, _dc in do:
        if write_episode_nfo(src, sea, ep):
            n += 1
            wrote.append(src)
    if n:
        ok(f"{n} 个条目补上了季集编号和剧集名"
           f"{DIM}（旁挂 .nfo，文件名和网盘都没动）{RST}")
        # 【刚写完的这批要当场叫 Emby 重读一遍】文件是脚本在 Emby 背后放的，
        # 不重读它就还按旧的来。用 refresh_items（ReplaceAllMetadata=false）——
        # true 会把编号一起交还给刮削器，那正是"一部剧被拆成 2 季"的来源。
        refresh_items(key, [bad.get(p) for p in wrote])
    verify_episode_numbers(d, rules, key, all_items)
    fix_episode_titles(d, rules, key, skip=list(wrote) + list(up),
                       items=all_items)
    fix_episode_images(d, rules, key, all_items)
    return n, 0


def _admin_uid(key):
    """随便一个管理员账号的 id。问不到返回 ""。"""
    try:
        for u in (_emby("/Users", key, timeout=20) or []):
            if (u.get("Policy") or {}).get("IsAdministrator"):
                return str(u.get("Id") or "")
    except Exception:
        pass
    return ""


def set_episode_number(key, uid, iid, season, ep):
    """直接把季集编号写进 Emby 的条目里。成功返回 True。

    【为什么不能靠 .nfo】实测在 Emby 4.9 上，「元数据下载器（集）」的可选项只有 TheTVDB、
    TheMovieDb、The Open Movie Database —— 【没有 Nfo】。这个版本压根不提供 nfo 读取器，
    写在片子旁边的 .nfo 从头到尾没有任何人读过。

    这条路和用户在界面上「编辑元数据 → 填季号/集号」是同一个接口。

    【不锁定条目】锁了简介、海报也不再更新，代价太大。改用"每轮核对、对不上就再写一次"。
    """
    try:
        it = _emby(f"/Users/{uid}/Items/{iid}", key, timeout=20) if uid else None
    except Exception:
        it = None
    if not isinstance(it, dict) or not it.get("Id"):
        return False
    it["ParentIndexNumber"] = int(season)
    it["IndexNumber"] = int(ep)
    try:
        _emby(f"/Items/{iid}", key, method="POST", body=it, timeout=30)
        return True
    except Exception:
        return False


def verify_episode_numbers(d, rules, key, items):
    """核对编号 Emby 到底是不是那个数，不是就直接写进去。返回改了几个。

    【写下 ≠ 生效】早几版每轮都理直气壮地打印"已补上季集编号"，而 Emby 那边纹丝不动 ——
    用户连着几轮回来说"还是没变"。核对这一步就是补这个洞。

    【为什么改成直接写】这个 Emby 版本的「元数据下载器（集）」里【没有 Nfo】，写在片子
    旁边的 .nfo 从头到尾没人读，所以不能再指望"写文件 + 重刮"。

    .nfo 仍然写：它是【这一集该是第几集】的落盘记录，核对时拿它当标准答案，换机器、重装、
    Emby 数据库重建都还在。不锁定条目 —— 刮削器哪天改回去，下一轮自己修回来。
    """
    if not key:
        return 0
    off = episode_number_mismatch(d, items)
    if not off:
        return 0
    _s, _i, ws, we, gs, ge = off[0]
    warn(f"{len(off)} 个条目的季集编号和应有的对不上 —— "
         f"该是 S{ws:02d}E{we:02d}，Emby 显示的是第 {gs} 季第 {ge} 集")
    uid = _admin_uid(key)
    if not uid:
        print(f"  {DIM}问不到管理员账号，这轮改不了。{RST}")
        return 0
    n = 0
    for hp, iid, wsx, wex, _gs, _ge in off[:EP_REIDENT_MAX]:
        if set_episode_number(key, uid, iid, wsx, wex):
            n += 1
    if n:
        ok(f"{n} 个条目的季集编号已直接写进 Emby"
           f"{DIM}（走的是「编辑元数据」那个接口，不经过刮削器；"
           f"下轮还会再核对一次）{RST}")
        # 【同一轮里刚发过整库重新识别的话，这次写多半白写】那个是后台跑的，
        # 跑完会按刮削器重排季集，把刚写进去的盖掉。说清楚，别让用户以为
        # 是写失败了 —— 上一轮就是这么"写成功了、界面上没变"的。
        if time.time() - int(ms_state().get("reident_ts") or 0) < 600:
            warn("这一轮还给整个库发过一次重新识别（后台跑）")
            print(f"  {DIM}它跑完会把编号按刮削器改回去 —— 下一轮对齐"
                  f"（一小时内）会再写一次，那次才作数。{RST}")
    if n < len(off):
        warn(f"还有 {len(off) - n} 个没写进去")
        print(f"  {DIM}到 Emby 里点这一集 → 编辑元数据，手工填「季号 / 集号」；"
              f"或者把这个库改成 episode_number: false，编号交回给刮削器"
              f"（那样会按刮削器的库分季）。{RST}")
    return n


def episode_nfo_reader_on(key, rules):
    """剧集库的「元数据下载器（集）」里有没有 Nfo。问不到就当有（不误报）。"""
    tv = {r["name"] for r in rules if (r.get("type") or "movies") == "tvshows"}
    try:
        libs = _emby("/Library/VirtualFolders", key, timeout=20) or []
    except Exception:
        return True
    seen = False
    for lb in libs:
        if lb.get("Name") not in tv:
            continue
        for t in ((lb.get("LibraryOptions") or {}).get("TypeOptions") or []):
            if (t.get("Type") or "") != "Episode":
                continue
            seen = True
            names = t.get("MetadataFetcherOrder") or t.get("MetadataFetchers") or []
            if not any(_is_local_fetcher(x, "MetadataFetchers") for x in names):
                return False
    return True if seen else True


def apply_libraries(d, key, plan):
    """把规划落到 Emby 上。返回 (建了几个库, 加了几条路径)。

    【只碰自己建的库】同名的库如果不是脚本建的，一律不动，只把该加的路径印出来让用户
    自己决定 —— 动别人的库可能把观看记录连根拔了。
    """
    mine = set(ms_state().get("lib_auto") or [])
    exist = {n: (ps, t) for n, ps, t in emby_libs(key)}
    # 库的 ItemId：加路径那个接口有的版本按 id 认，光有名字不够
    try:
        ids = {(lb.get("Name") or ""): lb.get("ItemId")
               for lb in (_emby("/Library/VirtualFolders", key, timeout=20) or [])}
    except Exception:
        ids = {}
    made, added, skipped, overlap = [], 0, [], []
    for name, (ctype, paths, _mt, lang, country) in sorted(plan.items()):
        paths = sorted(set(paths))
        # 【已经被别的库盖住的，绝对不能再建一个】否则同一个文件在 Emby 里会变成
        # 两个条目 —— 而 Emby 的观看记录是按刮削身份存的，两个条目会共用一份
        # 续播点，一个看过另一个也变成看过。那正是之前花了好几天修的"进度条串台"。
        # 实测现场：用户手工建了一个「夸克」库指向整棵树，规则又要按分类建三个库。
        dup = sorted({n for n, (ps, _t) in exist.items() if n != name
                      for x in paths if any(_under(x, q) for q in ps)})
        if dup:
            overlap.append((name, dup, paths))
            continue
        if name not in exist:
            err = _emby_add_library(key, name, ctype, paths, lang, country)
            if err:
                warn(f"建媒体库「{name}」失败：{err}")
                skipped.append((name, paths, f"建库失败：{err}"))
                continue
            made.append(name)
            mine.add(name)
            continue
        have = exist[name][0]
        # 已经被这个库（或它更浅的某条路径）盖住的就不用再加
        want = [x for x in paths if not any(_under(x, h) for h in have)]
        if not want:
            continue
        if name not in mine:
            # 用户自己建的，不擅自动
            skipped.append((name, want, "已有同名库，但不是脚本建的"))
            continue
        for x in want:
            err = _emby_add_path(key, name, x, ids.get(name) or "")
            if err:
                warn(f"往「{name}」加路径失败：{err}")
                skipped.append((name, [x], f"加路径失败：{err}"))
            else:
                added += 1
    if made:
        save_ms_state(lib_auto=sorted(mine))
    if overlap:
        print()
        warn("这些库没建 —— 要收的路径已经被别的媒体库盖住了：")
        for name, dup, paths in overlap:
            print(f"  {DIM}·{RST} {BOLD}{name}{RST}{DIM} 想收 "
                  f"{'、'.join(x.split('/')[-1] for x in paths)}，"
                  f"但已经在「{'」「'.join(dup)}」里了{RST}")
        print(f"  {YELLOW}硬建的话同一部片会有两个条目，而它们共用一份观看进度"
              f"{RST}{DIM}（一个看过另一个也变成看过，续播点互相串）。{RST}")
        print(f"  {DIM}想按规则分类的话，先去 Emby 把上面那个大库删掉，再回来跑一次。"
              f"删库不影响 strm 文件。{RST}")
    if skipped:
        print()
        warn("下面这些没有自动处理，需要你到 Emby 里手动加：")
        for name, paths, why in skipped:
            print(f"  {DIM}·{RST} {BOLD}{name}{RST}{DIM}（{why}）{RST}")
            for x in paths:
                print(f"      {x}")
        print(f"  {DIM}Emby → 设置 → 媒体库 → 添加媒体库 / 编辑文件夹{RST}")
    return len(made), added


def auto_libraries_apply(d, key, quiet=False):
    """按规则把该建的媒体库建上。不问，不交互 —— 给「5 生成媒体库」用。

    规则原来只在「3 后补参数 → 4」按 y 的时候才跑，于是用户改完网盘文件夹名、点「5」，
    期待的"扫完顺手把库建好"什么都没发生。

    不问是对的：只建【不存在的】库，不动用户已有的任何东西，重叠的直接跳过并说明。
    """
    # 【这里也要拉一次仓库版】用户在 GitHub 上改完规则，接着点的多半是
    # 「5 生成媒体库」而不是「7 更新」—— 他刚整理完网盘，想的是"扫一遍把库建好"。
    # 只在更新里拉的话，他会看到规则没生效，以为改的地方不对。
    # fetch 失败不影响后面：用本机现有的那份跑。
    try:
        fetch_lib_rules(d)
    except Exception:
        pass
    try:
        rules, _src = lib_rules(d)
        plan = plan_libraries(d, rules)
    except Exception:
        return
    # 【建和删是一件事的两面】放在 plan 之后、那两个提前 return 之前：
    # 网盘里改完文件夹名，往往【没有】新库要建（新名字还没扫出 strm），
    # 于是下面那两个 return 会先走掉，旧空壳永远轮不到被清理。
    drop_empty_auto_libraries(d, key)
    if not plan:
        if not quiet:
            print(f"  {DIM}关键词规则：{len(rules)} 条，没有文件夹匹配上。{RST}")
        return
    # 【不能只看"库名在不在"】以前这里是 any(n not in exist)，于是"库都在、
    # 但某个库少了一条路径"这种情况直接 return —— 而那恰恰是最常见的：
    # 用户网盘里新加一个文件夹，它归属的库早就建好了，缺的只是一条路径。
    # 实测那次：新文件夹被划给已有的「电影」库，三个库名都在，整步静默跳过。
    libs = {n: ps for n, ps, _t in emby_libs(key)}
    todo_any = False
    for name, (_ct, paths, _mt, _lg, _co) in plan.items():
        if name not in libs:
            todo_any = True
            break
        if any(not any(_under(x, h) for h in libs[name]) for x in set(paths)):
            todo_any = True
            break
    if not todo_any:
        if not quiet:
            print(f"  {DIM}媒体库和关键词规则已经对齐（{len(plan)} 个库），"
                  f"没有要建的。{RST}")
        return
    print()
    info("按关键词规则建媒体库...")
    made, added = apply_libraries(d, key, plan)
    if not (made or added):
        # 走到这儿说明确实有事该做，却一件都没做成 —— apply_libraries 里
        # 已经打印了原因（重叠 / 接口失败），这里补一句结论，别让人以为没跑
        warn("一个库都没建成，原因见上面几行。")
    if made or added:
        ok(f"新建 {made} 个媒体库，补了 {added} 条路径")
        mt_libs = [n for n, (_t, _p, m, _l, _c) in plan.items() if m]
        if metatube_on(d):
            ids = [i for n, i, _on, _o in metatube_libraries(key) if n in set(mt_libs)]
            if set_metatube_libraries(key, ids):
                ok(f"MetaTube 只在 {'、'.join(mt_libs) or '（无）'} 生效")
        # 新库要扫一次才有内容。刚扫过就不会真去扫（见 emby_scan_wait 的去重）
        emby_scan_wait(key, timeout=900, label="扫描新建的媒体库")
    if not quiet:
        print(f"  {DIM}规则文件：{lib_rules_path(d)}"
              f"（改仓库里那份，「7 更新」会拉下来）{RST}")


def auto_libraries():
    """按关键词自动建媒体库。菜单项。"""
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"),
                           "auth")
    if not key:
        warn("没有 Emby API Key，先填「1 添加 API 密钥」。")
        return
    while True:
        rules, rule_src = lib_rules(d)
        src = rules_source()
        cust = (ms_state().get("rules_url") or "").strip()
        local = lib_rules_path(d, local=True)
        print()
        print(f"  {BOLD}关键词规则{RST}{DIM}　文件夹名匹配到关键词，"
              f"就把它整个收进对应的媒体库{RST}")
        print()
        # 【当前用哪套必须一眼看见】以前只印一行"来自哪个文件"，看不出还有别的
        # 选择、也看不出怎么切。这三行照节点脚本那个「▸ 当前生效」排的。
        print(f"  ▸ {BOLD}当前生效{RST}　"
              + (f"{CYAN}【自定义链接】{RST}" if src == "custom"
                 else f"{GREEN}【作者的】{RST}"))
        print(f"    作者链接　{DIM}{RULES_URL}{RST}")
        print(f"    自定义　　{(CYAN + cust + RST) if cust else DIM + '(未设置)' + RST}")
        print(f"    共 {len(rules)} 条　{DIM}{'、'.join(r['name'] for r in rules)}{RST}")
        print(f"    {DIM}规则文件 {rule_src}{RST}")
        # 老版本 a/d 菜单写下的覆盖文件还认，但绝不让它继续藏着
        if os.path.exists(local):
            warn(f"本机有覆盖文件，它【盖过】上面选的链接：{local}")
            print(f"  {DIM}要让链接生效就删掉它：{BOLD}rm {local}{RST}")
        print(f"  {DIM}想改规则：到链接指向的文件里改（手机上开 GitHub 就能编辑），"
              f"改完回来按 4 重新拉。{RST}")
        plan = plan_libraries(d, rules)
        print()
        if not plan:
            print(f"  {YELLOW}按这些关键词，一个文件夹都没匹配上。{RST}")
            print(f"  {DIM}你的文件夹叫什么名字，关键词就得写什么 —— "
                  f"比如网盘里那个「某个分类目录」，"
                  f"得把它的名字加进对应那条的 keywords 里才收得进去。{RST}")
        else:
            print(f"  {BOLD}会这样建{RST}{DIM}（还没动手）{RST}")
            for name, (ctype, paths, mt, lang, _c) in sorted(plan.items()):
                mt_s = f"　{CYAN}+MetaTube{RST}" if mt else ""
                print(f"    {BOLD}{name}{RST}"
                      f"{DIM}（{LIB_TYPES.get(ctype, ctype)}，刮削语言 {lang}，"
                      f"{len(set(paths))} 个文件夹）{RST}{mt_s}")
                for x in sorted(set(paths)):
                    print(f"        {DIM}{x}{RST}")
        print()
        print(f"  {DIM}1 用作者的　2 用自定义　3 自定义链接（填/换/删）　"
              f"4 重新拉一次　{RST}{BOLD}y 按上面建库{RST}{DIM}　回车退出{RST}")
        c = ask("请选择").strip().lower()
        if c in ("", "0", "q"):
            return
        if c == "1":
            if src == "author":
                print(f"  {DIM}本来就是作者的。{RST}")
                continue
            set_rules_source("author")
            ok("已切到【作者的】")
            # 缓存还在就不联网 —— 断网也切得回去，这正是各存一份的用处
            if not os.path.exists(lib_rules_path(d)):
                fetch_lib_rules(d, "author")
            print(f"  {DIM}「5 生成媒体库」和每小时的对齐任务从现在起都用它。{RST}")
        elif c == "2":
            if not cust:
                warn("还没填自定义链接（先按 3 填）")
                continue
            if src == "custom":
                print(f"  {DIM}本来就是自定义的。{RST}")
                continue
            set_rules_source("custom")
            ok("已切到【自定义链接】")
            if not os.path.exists(lib_rules_path(d, custom=True)):
                if not fetch_lib_rules(d, "custom"):
                    warn("这条链接拉不下来、或者内容里解析不出规则")
                    print(f"  {DIM}先按 4 重试；一直不行就按 3 换一条。"
                          f"在切回作者的之前，用的是内置默认那份。{RST}")
            print(f"  {DIM}「5 生成媒体库」和每小时的对齐任务从现在起都用它。{RST}")
        elif c == "3":
            if cust:
                print()
                print(f"  当前自定义链接：{CYAN}{cust}{RST}")
                print(f"  {DIM}1 更换　2 删除（并切回作者的）　0 返回{RST}")
                t = ask("请选择").strip()
                if t == "2":
                    set_rules_url("")
                    ok("已删掉自定义链接，切回【作者的】")
                    continue
                if t != "1":
                    continue
            print(f"  {DIM}填 raw 链接（GitHub raw / gist raw 都行），"
                  f"格式和作者那份一样。回车放弃。{RST}")
            u = ask("自定义链接").strip()
            if not u:
                continue
            if not u.lower().startswith(("http://", "https://")):
                warn("要 http:// 或 https:// 开头的链接")
                continue
            # 【先验再存】存一条拉不动的链接，等于把用户切到一个空来源上
            if not fetch_lib_rules(d, "custom", u):
                warn("这条链接拉不下来、或者内容里解析不出规则 —— 没有保存")
                print(f"  {DIM}检查一下：是不是 raw 链接（不是网页那个地址）、"
                      f"仓库是不是私有的、格式对不对。{RST}")
                continue
            set_rules_url(u)
            set_rules_source("custom")
            _n = len(parse_lib_rules(
                open(lib_rules_path(d, custom=True), encoding="utf-8").read()))
            ok(f"已保存并切到【自定义链接】（解析出 {_n} 条）")
        elif c == "4":
            if fetch_lib_rules(d):
                ok(f"已重新拉取（{'自定义' if src == 'custom' else '作者的'}）")
            else:
                print(f"  {DIM}没有变化，或者这次没拉到"
                      f"（拉不动时保留机器上原来那份，不会把规则弄没）。{RST}")
        elif c == "y":
            if not plan:
                warn("没有可建的，先加关键词。")
                continue
            print()
            print(f"  {DIM}建库不会动你已有的库；同名但不是脚本建的，只会印出来"
                  f"让你自己加。{RST}")
            mt_libs = [n for n, (_t, _p, m, _l, _c) in plan.items() if m]
            if metatube_on(d):
                print(f"  {DIM}MetaTube 会【只】在 "
                      f"{('「' + '」「'.join(mt_libs) + '」') if mt_libs else '（无）'}"
                      f" 生效，其它库一律摘掉 —— 它会把动画认成 JAV。{RST}")
            if not ask_yn("按上面建？", False):
                continue
            made, added = apply_libraries(d, key, plan)
            ok(f"新建 {made} 个媒体库，补了 {added} 条路径")
            if metatube_on(d):
                ids = [i for n, i, _on, _o in metatube_libraries(key)
                       if n in set(mt_libs)]
                n_mt = set_metatube_libraries(key, ids)
                if n_mt:
                    ok(f"MetaTube 生效范围已调整（{n_mt} 个库有变化）")
            print(f"  {DIM}回菜单点「5 生成媒体库」让 Emby 扫一次，"
                  f"新库里的片子才会出来。{RST}")
            return
        else:
            warn("不认识这个选项。")


def library_targets(d, key):
    """能拿去建 Emby 媒体库的路径清单：[(容器内路径, 层级, strm 个数, 已被库覆盖)]。

    结构改成「每个网盘一条主路径」之后，可选的落点不再只有一个根，而生成完只印一句
    /data/strm/cloud 等于什么都没说。正确用法本来就是「脚本把主路径摆出来，人到 Emby 里
    挑子路径」。只列【直接装着 strm 的目录】和它们的祖先。
    """
    base = os.path.join(strm_root(d), STRM_SUBDIR)
    cnt = {}
    for root, _dirs, files in os.walk(base):
        n = sum(1 for f in files if f.endswith(".strm"))
        if not n:
            continue
        cur = root                       # 把数量累加到自己和每一层祖先上
        while True:
            cnt[cur] = cnt.get(cur, 0) + n
            if os.path.abspath(cur) == os.path.abspath(base):
                break
            cur = os.path.dirname(cur)
    if not cnt:
        return []
    covered = [p for _n, ps in emby_lib_locations(key) for p in ps] if key else []
    out = []
    for host in sorted(cnt):
        rel = os.path.relpath(host, base)
        cpath = STRM_PATH if rel == "." else f"{STRM_PATH}/{rel}"
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        out.append((cpath, depth, cnt[host],
                    any(_under(cpath, L) for L in covered)))
    return out


def print_library_targets(d, key, max_rows=14):
    """把可选路径打成一行一条：几部片、建没建过库、完整路径。

    一行一条而不是画树：用户要做的是【把路径复制到 Emby 的文件夹框里】，路径必须完整
    可见。树形 + 单独一行放完整路径行数翻倍，在手机终端上一屏都装不下。
    """
    rows = [r for r in library_targets(d, key) if r[1] <= 3]
    if not rows:
        print(f"  {BOLD}Emby 媒体库要指向的路径{RST}（容器内路径，不是宿主机路径）：")
        print(f"      {CYAN}{BOLD}{STRM_PATH}{RST}")
        return
    print(f"  {BOLD}Emby 媒体库可以指向这些路径{RST}"
          f"{DIM}（容器内路径，不是宿主机路径）{RST}")
    for cpath, depth, n, cov in rows[:max_rows]:
        mark = f"{GREEN}已建库{RST}" if cov else f"{DIM}未建库{RST}"
        print(f"    {pad(f'{n} 部', 7)}{mark}  {'  ' * depth}{CYAN}{cpath}{RST}")
    if len(rows) > max_rows:
        print(f"    {DIM}...另外 {len(rows) - max_rows} 个更深的目录没列{RST}")
    print(f"  {DIM}指向 {BOLD}{STRM_PATH}{RST}{DIM} = 全都要，以后新挂的网盘自动进；"
          f"指向子路径 = 只要那一块，分类清楚但每加一块要建一次库。{RST}")


# strm 树里除了 .strm 就只有 AutoFilm 下的这些附属文件，全都是能重新生成的。
# 这棵树整个由脚本产生，用户的东西不会放在这儿，所以清掉孤儿元数据是安全的。
SIDECAR_EXT = (".nfo", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".ass", ".ssa", ".sub")


def _sweep_empty_dirs(root_dir):
    """收掉 strm 树里空的、以及只剩孤儿元数据的目录。返回删了几个。

    「只剩孤儿元数据」也得收：迁移之后旧目录里可能留着没有对应 strm 的 nfo/海报，留着
    它们目录就非空 —— Emby 的文件夹选择器里那个旧目录一直在，而脚本自己的列表只数
    .strm，看不见，两边对不上。

    【要按磁盘实际内容判空，不能用 os.walk 给的 dirs/files】那是进目录时抓的快照。
    """
    base = os.path.join(root_dir, STRM_SUBDIR)
    gone = 0
    # 根目录自己不删，但里面的孤儿元数据要清 —— 用户那里就躺着一个 2MB 的
    # 「…•…4K-poster.png」，是旧布局留下的，没有对应 strm，永远不会被搬走
    try:
        for n in os.listdir(base):
            f = os.path.join(base, n)
            if os.path.isfile(f) and n.lower().endswith(SIDECAR_EXT):
                os.remove(f)
                gone += 1
    except OSError:
        pass
    for cur, _dirs, _files in os.walk(base, topdown=False):
        if os.path.abspath(cur) == os.path.abspath(base):
            continue
        try:
            names = os.listdir(cur)
        except OSError:
            continue
        if any(n.endswith(".strm") for n in names):
            continue                      # 还有正片，留着
        # 只剩附属文件（或者本来就空）才动手；出现别的东西一律不碰
        if any(not n.lower().endswith(SIDECAR_EXT)
               and os.path.isfile(os.path.join(cur, n)) for n in names):
            continue
        if any(os.path.isdir(os.path.join(cur, n)) for n in names):
            continue                      # 底下还有子目录没清完，这轮先放着
        try:
            for n in names:
                os.remove(os.path.join(cur, n))
            os.rmdir(cur)
            gone += 1
        except OSError:
            pass
    return gone


def migrate_strm_layout(d, key):
    """把已有的 strm 挪到它按当前规则【应该】在的位置。返回挪了几个。

    为什么必须由脚本来挪、而不是让 AutoFilm 在新位置重新生成一遍：旧位置的 strm 不会被
    prune 清掉（prune 只删网盘上确认没有的，而这些文件在网盘上好好的），结果是新旧并存，
    每部片在 Emby 里两个条目，还可能撞回「刮削身份」那个老问题。

    续播点靠网盘路径搬家（见 _progress_by_target）：条目 id 随路径变，网盘路径不变。
    """
    cfg = rebuild_cfg_from_disk(d)
    sps = cfg.get("scan_paths") or []
    if not sps:
        return 0
    moves = []
    for host, target in strm_inventory(d):
        want = planned_strm_path(d, target, sps)
        if want and os.path.abspath(want) != os.path.abspath(host):
            moves.append((host, want))
    if not moves:
        # 【没得挪也要清一遍】。strm 早就挪好了的话 moves 是空的，可上一轮遗留的
        # 空壳目录、孤儿 nfo/海报还在 —— 它们不含 .strm，脚本的路径列表看不见，
        # Emby 的文件夹选择器却照列。用户看到的就是"清扫说做完了，Emby 里还在"，
        # 而且怎么点刷新都不变。实测卡在这儿好几轮。
        _sweep_empty_dirs(strm_root(d))
        return 0
    print()
    info(f"{len(moves)} 个 strm 要挪位置（strm 目录树要和网盘目录树对上）")
    print(f"  {DIM}不挪的话新旧两份并存，每部片在 Emby 里会变成两个条目。{RST}")
    saved = _progress_by_target(d, key) if key else {}
    if saved:
        print(f"  {DIM}已记下 {len(saved)} 个条目的续播点，挪完贴回去。{RST}")
    n = 0
    for src, dst in moves:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            # 【附属文件必须一起搬】AutoFilm 会把 nfo/海报/字幕下到 strm 旁边。只搬
            # .strm 的话它们留在原地，旧目录就【非空】—— 清不掉，于是 Emby 的文件夹
            # 选择器里那几个旧目录一直在（脚本的列表只数 .strm，看不见它们）。
            # 必须在 strm 挪走【之前】列，_strm_sidecars 是按同目录同名前缀找的。
            old_stem = os.path.basename(src)[:-len(".strm")]
            new_stem = os.path.basename(dst)[:-len(".strm")]
            for sc in _strm_sidecars(src):
                tail = os.path.basename(sc)[len(old_stem):]   # 保住 .nfo / .zh.srt
                try:
                    shutil.move(sc, os.path.join(os.path.dirname(dst),
                                                 new_stem + tail))
                except OSError:
                    pass
            shutil.move(src, dst)
            n += 1
        except OSError as e:
            warn(f"挪不动 {os.path.basename(src)}：{_short_err(e)}")
    # 顺手把空掉的旧目录收干净，否则 Emby 里会留一堆空文件夹条目
    _sweep_empty_dirs(strm_root(d))
    ok(f"{n} 个 strm 已挪好，strm 目录树和网盘对上了")
    if key:
        # 【挪完必须让 Emby 知道】不然旧条目留着、新条目不出现，一部片两个入口。
        # 先走快车道：挪了哪几条我们【一清二楚】，没必要为十几条让它重扫两千多个。
        moved = [(_strm_container_path(d, src), "Deleted") for src, _dst in moves]
        moved += [(_strm_container_path(d, dst), "Created") for _src, dst in moves]
        if not emby_notify_changes(key, moved):
            info("让 Emby 看一眼挪过的位置...")
            emby_scan_wait(key, timeout=900, label="重扫挪过位置的条目", force=True)
        back = _restore_progress(d, key, saved)
        if back:
            ok(f"{back} 个条目的续播点已贴回")
        elif saved:
            warn(f"{len(saved)} 个续播点没能贴回 —— 条目可能还没扫出来，"
                 f"等下一轮对齐或再点一次「5 生成媒体库」")
    return n


def do_heal():
    """后台补时长：一轮一轮走，中间歇几分钟，直到没得补或者用满预算。

    单独一个子命令而不是塞进 warm，是因为触发时机不同：warm 是每小时的例行，这个是
    【用户刚扫完盘】那一下 —— 那时候新条目最多、最需要赶紧补上。

    失败的隔几分钟再试：失败几乎全是当时网盘那条线在抖，同一个条目下一轮往往就成了。
    heal_media_info 自己带游标，所以反复调它就是"只补没探到的"。
    """
    d = ms_install_dir()
    if not is_installed(d):
        return
    key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"),
                           "auth")
    if not key:
        return
    t0, seen = time.monotonic(), 0
    while time.monotonic() - t0 < HEAL_BG_BUDGET and seen < HEAL_BG_MAX:
        pend = items_without_duration(key)
        if not pend:
            return                      # 全补齐了
        heal_media_info(d, key)
        seen += HEAL_LIMIT
        if not items_without_duration(key):
            return
        # 【歇一下再来】隔太密只会连着撞同一段抽风的线路，还容易被网盘限流
        if time.monotonic() - t0 + HEAL_RETRY_MIN * 60 >= HEAL_BG_BUDGET:
            return
        time.sleep(HEAL_RETRY_MIN * 60)


def align_library(d, key, heal=True):
    """把【所有】指向 strm 的媒体库和它们里面的条目，拉到脚本认定的状态。

    这一坨原本散在 do_strm / do_sync 里各抄一遍，而 do_sync 一天只跑一次(05:45)，后果就是
    "新加进来的片全都没有进度条记忆"。这些从来就不该是某几部片子享有的待遇。收成一个
    函数、按小时跑，新内容不管从哪儿冒出来（手动点、凌晨 AutoFilm、用户自己新建一个库）
    最多一小时就跟上。

    每一步都是【幂等 + 无事不发请求】的，所以按小时跑没有代价：
      · tune       选项已经对的库，一个请求都不发
      · heal       只挑没探出时长的条目
      · title      只改和策略不符的片名
      · identity   只拆真的撞了身份的
      · impossible 只清位置 > 片长的脏数据

    【不含 prune】它是破坏性的、而且要跨境列目录，代价高，留给每日对齐。
    【但要管 Emby 扫描】strm 数一变就通知 Emby 扫一次，数没变一个请求都不发。
    """
    follow_new_storages(d)            # 新挂的网盘要先进扫描范围，否则后面全是空的

    # 【必须在这儿也来一遍】strm 不是只有点「5 生成媒体库」才会产生 —— AutoFilm 自己的
    # 定时任务也会按新配置生成。实测翻过车：「7 更新」重写了 autofilm 配置，它的 cron
    # 到点按新布局生成了 cloud/quark/…，而旧的那批没人搬，两份并存。
    migrate_strm_layout(d, key)
    if not key:
        return
    # 【改了就得让 Emby 重扫】库选项对【已经建好的条目】不会追溯生效 —— 把多版本合并
    # 关掉，已经被并成「版本」的那几个条目还是合的，得重扫才拆开。tune 自己说"下一次
    # 扫描会拆开"，可那次扫描没人发起。
    n_tuned = tune_strm_libraries(key)   # 库级：续播门槛、多版本合并
    # heal=False 是「5 生成媒体库」用的：那条路把补时长扔后台单独跑，
    # 不能在这儿再跑一遍（会撞锁、也会让用户白等一次）
    try:                              # 库级：刮削器/语言按规则文件对齐
        _r = lib_rules(d)[0]
        sync_library_options(d, key, _r)
        # 私密库的权限也一起对 —— 新加的用户、新建的库都得自动跟上，
        # 否则用户得记着每次回 Emby 后台改一遍勾（而漏一次就是内容暴露在电视上）
        sync_private_libraries(d, key, _r)
    except Exception as e:
        # 【别静默吞】原来这里是 except: pass。规则文件解析炸了、Emby 没起来、
        # 哪个字段对不上，全都一声不吭 —— 用户看到的是"跑完了，Emby 里没变化"，
        # 根本没法判断是没跑还是跑了没用。这一步失败不该拦住后面的对齐，
        # 但必须让人知道它失败了。
        warn(f"按规则文件对齐媒体库的刮削器/语言失败：{_short_err(e)}")
    if heal:
        heal_media_info(d, key)       # 条目级：补时长
    normalize_strm_files(d)           # heal 中途被打断的兜底
    # 剧集 strm 改名成带季集编号的。【必须排在 scan_if_grown 之前】——
    # 改完要让 Emby 重扫才认得出来。interactive 跟着 heal 走：heal=True 的那条
    # 路是用户手点「4」在看着的，可以问；cron 那条路不问，用记住的答案。
    try:
        # 【别拿 heal 当"有没有人在看"】它俩没关系，而且正好反了：
        # 「5 生成媒体库」是用户手点、看着的，传的却是 heal=False；
        # cron 那轮没人看，传的是 heal=True。真正的判据是 has_tty()。
        _nr, _nd = fix_episode_strm_names(d, lib_rules(d)[0], key,
                                          interactive=has_tty())
        if _nr or _nd:
            n_tuned += 1              # 借它触发一次重扫
    except Exception as e:
        warn(f"给剧集 strm 补季集编号失败：{_short_err(e)}")
    apply_title_policy(d, key)        # 条目级：片名跟着网盘文件走
    split_shared_identities(d, key)   # 条目级：进度条身份互相独立
    # 条目级：清掉位置 > 片长的脏数据。清了哪些要往下传 —— 下面那步得知道
    # 这些"没进度"是脚本自己刚打的零，不是用户标的未播放
    zeroed = clear_impossible_progress(key)
    # 库选项改过就必须重扫（见上），哪怕文件数一个没变
    scan_if_grown(d, key, force=bool(n_tuned))
    # 【必须排在扫描之后】路径变过的条目要等 Emby 重新扫出来才找得到。
    # 这一趟同时做两件事：把现有进度记下来，把新条目缺的补回去。
    try:
        sync_progress_map(d, key, just_zeroed=zeroed)
    except Exception as e:
        warn(f"同步观看进度失败：{_short_err(e)}")


def scan_if_grown(d, key, force=False):
    """strm 数和上次记的不一样就让 Emby 扫一次。返回有没有扫。

    只在【变了】的时候扫：全库扫描在两万条目的库上不便宜，按小时无脑扫就是
    白烧 CPU。而变没变本地就能数出来，不用问任何人。
    """
    mark = os.path.join(d, "strm_seen.txt")
    now = strm_count(d)
    try:
        was = int(open(mark).read().strip())
    except (OSError, ValueError):
        was = -1
    if not key or (now == was and not force):
        return False
    emby_scan_wait(key, timeout=600, label="扫描媒体库")
    try:
        with open(mark, "w") as f:
            f.write(str(now))
    except OSError:
        pass
    return True


def do_sync():
    """每天自动跑的对齐：把 Emby 的状态和网盘、和当前媒体库配置拉齐。

    就是「5 生成媒体库」末尾那几步，减掉触发 AutoFilm 那一段（那个有它自己的定时任务）。
    全程不问任何问题 —— 没人在终端前面。

    顺序是有讲究的：
      1. normalize  URL 形式的 strm 归一回路径形式（heal 被打断时的残留）
      2. prune      删掉网盘上确认没有的（三态判据，超时一律当成还在）
      3. tune       给【所有】指向 strm 的媒体库调续播门槛，新建的库在这里被兜住
      4. scan       让 Emby 看到增删
      5. heal       给没时长的条目补探测 —— 必须在 scan 之后，新条目才存在

    【结果必须落盘】cron 里跑的东西输出全进了 /dev/null，用户第二天看到问题还在，没法
    判断是"任务没跑"还是"跑了但没修好"。所以写进 sync.json 给体检读。
    """
    d = ms_install_dir()
    if not is_installed(d):
        return
    rec = {"ts": int(time.time()), "ok": False, "pruned": 0, "bluray": 0,
           "bluray_stuck": 0, "nodur_before": 0,
           "nodur_after": 0, "missing": 0, "error": ""}
    try:
        key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"),
                               "auth")
        normalize_strm_files(d)
        # 原盘目录压成单个 strm。放这儿是因为上一轮"问不到片段大小"的那些要有机会重试，
        # 而且新扫进来的原盘不必等到用户下次手动点「5」才可播。
        rec["bluray"], rec["bluray_stuck"] = collapse_bluray_folders(d, quiet=True)
        rec["pruned"] = prune_dead_strm(d)   # 每日对齐不设预算：凌晨没人等
        if not key:
            rec["error"] = "没有 Emby API Key"    # 本地那一层已经做完了
        else:
            rec["nodur_before"] = len(items_without_duration(key))
            tune_strm_libraries(key)   # 扫描前先调好，新条目一进来就是对的
            emby_scan_wait(key, timeout=900, label="每日对齐的扫描")
            align_library(d, key)      # 和小时级那轮同一份，不会飘
            rec["nodur_after"] = len(items_without_duration(key))
            rec["missing"] = len(strm_not_in_emby(d, key))
            rec["ok"] = True
    except Exception as e:
        rec["error"] = _short_err(e)
    try:
        with open(os.path.join(d, "sync.json"), "w") as f:
            json.dump(rec, f, ensure_ascii=False)
    except OSError:
        pass


def warm_state(d):
    """上一次直链预热是什么时候。取不到就返回空。

    【这一行原来只查 cron 文件在不在】—— 文件在就打绿勾。可 cron 文件在不等于任务在跑：
    那次双层锁把三条任务全锁死，预热这行是纯粹的"装了"，坏成什么样都是绿的。
    """
    try:
        with open(os.path.join(d, "warm.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def sync_state(d):
    """上一次每日对齐做了什么。取不到就返回空 —— 体检那边按"还没跑过"处理。"""
    try:
        with open(os.path.join(d, "sync.json")) as f:
            return json.load(f)
    except Exception:
        return {}


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

    时刻按哪个时区解释最容易被误解（调度器钉在北京时间，不是服务器本地时区），所以时区
    必须跟着时刻一起显示，不能只报一个 05:15。看不懂的格式就原样打出来，不猜。
    """
    p = cron.split()
    if len(p) == 6 and p[3] == p[4] == p[5] == "*" and all(x.isdigit() for x in p[:3]):
        return f"每天 {int(p[2]):02d}:{int(p[1]):02d}（北京时间）"
    return f"{cron}   {DIM}(6 位 cron，按北京时间){RST}"


def openlist_storages(d):
    """读 OpenList 的库，把挂好的网盘和几个关键参数列出来。

    取挂载点、驱动、状态、根文件夹ID，外加"走哪套接口"那个开关（阿里的 alipan_type /
    夸克的 link_method）—— refresh_token / cookie 一概不碰，「使用信息」这段输出是会被
    截图发出来的。库不在、表名对不上就静默返回空。
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
            a = json.loads(add)
            root = str(a.get("root_folder_id", ""))
            # 【只取这一个开关，不碰任何凭据】refresh_token / cookie 一概不读 ——
            # 这些值会进体检输出，而体检是会被截图发出去的
            mode = str(a.get("alipan_type") or a.get("link_method") or "")
        except Exception:
            root, mode = "", ""
        out.append((mp, drv, status, root, mode))
    return out


def link_ttl_of(d):
    """MediaWarp 该把直链缓存多久。返回 ("10m", 10) 这样的 (写进配置的值, 分钟)。

    规矩只有一条：【缓存不能比直链本身活得长】。长了，MediaWarp 就会把一条已经过期的地址
    302 给播放器 —— 播放器报 load fail，而这边日志里是一次正常的 302、体检也全绿（体检
    每次都现换一条新的，永远碰不到这个坑）。这个值是全局的，只能取最短的那家。

    【但只看"进了 Emby 的盘"，不看"挂在 OpenList 上的盘"】挂着但没被扫进媒体库的盘，
    MediaWarp 根本不会为它换直链。按"挂了什么"算的话，光是把阿里挂在 OpenList 上（哪怕
    压根没往 Emby 里放）就会把夸克的缓存从 2 小时压到 9 分钟 —— 夸克本来靠这 2 小时做到
    "点开就播"，压短之后每次开播都要现换一次直链，表现就是转圈。
    """
    mins = LINK_TTL_H * 60
    scanned = read_yaml_all(os.path.join(d, "autofilm", "config", "config.yaml"),
                            "source_dir") or []
    for mp, drv, _st, _root, _mode in openlist_storages(d):
        life = LINK_LIFE_MIN.get(str(drv).lower())
        if not life or not mp:
            continue
        root = mp.rstrip("/")
        if not any(p == root or p.startswith(root + "/") for p in scanned):
            continue          # 挂着但没进 Emby —— 不该让它拖累别的盘
        mins = min(mins, int(life * LINK_TTL_SAFE))
    if mins >= 60 and mins % 60 == 0:
        return f"{mins // 60}h", mins
    return f"{mins}m", mins


def storage_token_days(d):
    """各网盘授权令牌还剩几天 [(挂载点, 天数), ...]。取不到就不返回那一条。

    为什么需要：令牌过期的表现和「网盘不通」一模一样 —— 列目录还行（读缓存）、一点开文件
    就转圈。而这两件事一个能修（重新扫码授权）、一个只能等（跨境线路），分不清就会一直
    往线路方向找。静默过期是最坏的一种坏法。

    读的是【长期凭据】，不是请求 URL 里那个 access_token，两者差别很大：
      · access_token   短期票据，只活在内存里。驱动检测到它失效会自己换新的再重试，用户
                       完全感知不到；它的 exp 只有几天，拿这个报警等于天天喊狼来了
      · refresh_token  扫码那次拿到的长期凭据，存在 addition 里，实测 exp 是 362 天
    addition 里只有它是 JWT 形态，所以按"挑 JWT"这个规则取到的正是它。

    只解 JWT 的 payload 取 exp。【不返回令牌本身】—— 体检结果是会被截图发出去的。
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
def emby_users(key):
    """Emby 里有哪些账号。返回 [(名字, 是不是管理员)]。

    密码读不出来，也不该读 —— Emby 存的是哈希。这里只列名字，让用户知道
    该拿哪个账号登客户端；密码是他自己设的，脚本从来没经手过。
    """
    try:
        return [(u.get("Name") or "?",
                 bool((u.get("Policy") or {}).get("IsAdministrator")))
                for u in (_emby("/Users", key, timeout=15) or [])]
    except Exception:
        return []


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
    ip = "" if domain else public_ip()
    emby_url = emby_port = ""
    for sub, port, container, label in SUBDOMAINS:
        if container == "homepage" and not os.path.isdir(os.path.join(d, "homepage")):
            continue
        # 有域名时对外是 nginx 的 443，容器端口只开在 127.0.0.1，外面连不到 ——
        # 所以这里要报【外面真正能连的那个端口】，不是 compose 里写的那个
        url, shown = ((f"https://{sub}.{domain}", "443") if domain
                      else (f"http://{ip}:{port}", str(port)))
        print(f"      {pad(label, 11)}{CYAN}{BOLD}{pad(url, 34)}{RST}"
              f"{DIM}端口 {shown}{RST}")
        if container == "emby":
            emby_url, emby_port = url, shown
    if domain:
        print(f"      {DIM}首页入口就是导航面板，所有服务都能从那里点进去。{RST}")

    # 外部播放器（Hills / Infuse / Emby 官方 App…）单独列一段，因为有那个【必须填
    # MediaWarp 地址】的坑：Emby 自己的 8096 只绑 127.0.0.1，外面本来就连不到，但内网里
    # 直接连它是连得上的 —— 而那样会整个绕过 302，视频改由本机中转，又慢又烧流量，
    # 表面上还"能播放"，属于最典型的「看起来正常、实际是废的」。
    if emby_url:
        print(f"\n  {BOLD}▸ 外部播放器{RST}"
              f"{DIM}（Hills / Infuse / Emby 官方 App 等，填这个地址）{RST}")
        print(f"      服务器地址 {CYAN}{BOLD}{emby_url}{RST}")
        print(f"      端  口     {CYAN}{BOLD}{emby_port}{RST}"
              + (f"   {DIM}https，nginx 转到 MediaWarp{RST}" if domain
                 else f"   {DIM}MediaWarp{RST}"))
        us = emby_users(read_emby_api_key(d) or "")
        if us:
            print("      账  号     " + "、".join(
                f"{CYAN}{BOLD}{n}{RST}" + (f"{DIM}(管理员){RST}" if a else "")
                for n, a in us))
        else:
            print(f"      账  号     {DIM}第一次打开 Emby 网页时自己创建的那个{RST}")
        print(f"      密  码     {DIM}你在 Emby 里自己设的，脚本不保存也读不到"
              f"（Emby 存的是哈希）{RST}")
        print(f"      {YELLOW}必须填上面这个地址{RST}"
              f"{DIM} —— Emby 自己的 8096 只开在本机。绕开它直连 8096 会让 302 失效，"
              f"视频全部改走本机中转{RST}")

    if ba_pass:
        print(f"\n  {YELLOW}{BOLD}▸ 浏览器弹框{RST}{DIM}（只有首页入口会弹）{RST}")
        print(f"      用户名   {CYAN}{BOLD}{ba_user}{RST}")
        print(f"      密  码   {CYAN}{BOLD}{ba_pass}{RST}")
        print(f"      {DIM}打开首页入口时会弹，输它。{RST}")

    print(f"\n  {GREEN}{BOLD}▸ 各服务自己的账号{RST}")
    print(f"      Emby       {DIM}首次打开自己设；不走弹框（App 客户端处理不了 Basic Auth）{RST}")
    print(f"      OpenList   {CYAN}{BOLD}admin{RST} / {CYAN}{BOLD}{ol_pass}{RST}")

    # 【挂网盘要的令牌不在这套东西里，得去外面取】而"去哪取"是最容易卡住的一步：
    # OpenList 的驱动表单里【没有】这个链接，文档也在另一个域名下，
    # 不写在这儿的话每次挂新盘都要重新去搜一遍。
    print(f"\n  {BOLD}▸ 挂网盘要的令牌去哪取{RST}"
          f"{DIM}（OpenList 表单里没有这个链接）{RST}")
    print(f"      {CYAN}{BOLD}https://api.oplist.org/{RST}"
          f"{DIM}   打不开就换 https://api.oplist.org.cn/（同一个工具的国内站）{RST}")
    print(f"      {DIM}页面顶上那个下拉框选哪一项，要和 OpenList 里的"
          f"「阿里盘账户类型」{RST}对上{DIM}：{RST}")
    print(f"        {DIM}alipan_type = {RST}default{DIM}   → 选"
          f"{RST}阿里云盘 (OAuth2) 扫码登录")
    print(f"        {DIM}alipan_type = {RST}alipanTV{DIM}  → 选"
          f"{RST}阿里云盘 (Client) TV版扫码")
    # 【为什么把这条配对关系写在取令牌这里】OpenList 拿 alipan_type 决定向官方 API 报哪个
    # 驱动标识（default→alicloud_qr，alipanTV→alicloud_tv），拿一种流程取的令牌去另一种
    # 那边换，只会得到 empty token returned from official API。而类型那个下拉框在表单里、
    # 令牌却要去另一个网站取 —— 两件事离得远，改一个忘一个是最容易踩的坑。
    print(f"      {YELLOW}两边不配对就挂不上{RST}"
          f"{DIM}，报 empty token returned from official API。"
          f"换类型 = 连令牌一起换{RST}")
    print(f"      {DIM}授权时把「备份盘」的勾去掉 —— 那里面是手机相册，"
          f"挂进来会变成一堆刮不出海报的条目{RST}")
    print(f"      {DIM}只要{RST}刷新令牌{DIM}，访问令牌不用填"
          f"（那是几小时就过期的短期票据，OpenList 自己会换）{RST}")

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

    # 这一屏是「地址 / 账号密码 / 怎么用」，不是诊断屏，所以只列【挂了哪些盘】：
    #   · status 字段装的是整条 Go 错误，里面带着 access_token —— 而这一屏最常被截图
    #   · 而且它是【存储初始化那一刻】写进去的，之后恢复了也不会改回 work，拿它当实时
    #     状态用会把陈年记录报成当前故障
    stores = openlist_storages(d)
    if stores:
        print(f"\n  {BOLD}▸ 网盘挂载{RST}")
        for mp, drv, status, root, mode in stores:
            print(f"      {pad(mp, 14)}{pad(drv, 12)}"
                  + (f"{DIM}根文件夹ID={root}{RST}" if root else "")
                  + (f"{DIM}　接口 {mode}{RST}" if mode else ""))
        if any(s != "work" for _m, _d, s, _r, _x in stores):
            print(f"      {YELLOW}有存储上次初始化时报过错{RST}"
                  f"{DIM} —— 跑「6 链路体检」看现在通不通、怎么修{RST}")

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
        print(f"        2. 回本菜单点 {GREEN}{BOLD}5 生成媒体库{RST}")
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
        print(f"      {DIM}装/卸：3 后补参数 → 3{RST}")

    # 容器只报「几个在跑」。以前这里直接贴 docker compose ps 的原始输出,在手机上
    # 每行都折成三四行,IMAGE/COMMAND/PORTS 糊成一片,而真正要看的只有"跑没跑"。
    # 详细状态在「6 链路体检」里。
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
    # 【别只信 mediawarp 那一份】它是「更新」自己重写的文件，坏了/被写空了的话，
    # 从它读回来的就是空值，然后更新会拿这个空值把配置再生成一遍 —— Key 就此丢掉。
    # .secrets 里的备份就是为这一刻留的。见 save_emby_api_key。
    cfg["emby_api_key"] = (read_yaml_scalar(mw, "auth")
                           or read_env(sec_file, "EMBY_API_KEY"))
    # 扫描路径：auto 这种「意图」从生成出来的 yaml 里读不回来（里面只有展开后的结果），
    # 所以存在状态文件里。老机器没有这个键，就退回去读 yaml 里已有的 source_dir。
    spec = ms_state().get("scan_spec")
    if spec == SCAN_AUTO:
        cfg["scan_spec"] = SCAN_AUTO
    elif isinstance(spec, list) and spec:
        cfg["scan_spec"] = spec
    else:
        cfg["scan_spec"] = read_yaml_all(af, "source_dir") or ["/quark"]
    # 【用 effective 而不是 resolve】单独设过的盘 + 剩余自动，见 effective_scan_paths。
    # 生成 autofilm 配置的就是这个值，所以"剩余网盘"开关必须在这里生效。
    cfg["scan_paths"] = effective_scan_paths(d)
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


# 拉镜像失败时的兜底源，按顺序试，第一个成功就停。
# 故意【不】改 /etc/docker/daemon.json 的 registry-mirrors —— 那要重启 dockerd，会把这台
# 机器上跑着的节点容器一起带下来。这里是直接从镜像站 pull 再 tag 回原名。
# 这些都是第三方公益站，会挂会换；挂了不影响正常流程，只是兜底失效。
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

    没有这一步，「更新」只是按【这台机器上现有的那份脚本】重新生成一遍配置 —— 仓库里修好
    的东西永远到不了机器上。

    URL 上带时间戳绕开 CDN 缓存：raw.githubusercontent 和各家镜像都会缓存几分钟到几小时，
    不绕开的话「刚推的修复」拉下来还是旧的，看起来就像改了没用。
    """
    me = os.path.realpath(__file__)
    if not os.access(me, os.W_OK):
        # 同上：静默返回会让人以为"检查过了、是最新的"，而实际上根本没检查
        warn(f"脚本文件不可写（{me}），这次跳过检查更新。")
        print(f"  {DIM}继续用本机这份 v{SCRIPT_VERSION} 刷新配置。{RST}")
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
            # 【没得升也要说一声】原来这里是静默 return，于是"没打那行"同时意味着三件
            # 事：已是最新、拉取失败、脚本不可写 —— 用户没法从屏幕上分辨，只会以为
            # "更新功能不见了"。所以照着有更新时的形状打，只是箭头两头一样。
            ok(f"脚本 v{SCRIPT_VERSION} → v{SCRIPT_VERSION}"
               f"{DIM}（已是最新，仓库里没有更新的版本）{RST}")
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


# 一次性善后。v1.1.0 有过一个「节点分流规则」的开关，会往节点脚本的三份成品配置里插一段。
# 那个功能撤掉了，但撤掉功能【不等于】撤掉它已经写进去的东西：装过那一版的机器上还留着
# 那段，而移除它的按钮已经没了 —— 用户手上就是一段无人认领、也关不掉的规则。所以按标记
# 清一次。清干净之后 node_rule.json 一删，以后每次更新它就是个空转的 if。
NODE_RULE_JSON = "node_rule.json"
_NR_MARK_IN  = "# >>> media-stack 媒体分流 >>>"
_NR_MARK_OUT = "# <<< media-stack 媒体分流 <<<"
_NR_GROUP    = "📺媒体走CDN"


def purge_node_rule(d):
    """把上一版写进节点配置的那段清掉。没写过就什么都不做。"""
    flag = os.path.join(d, NODE_RULE_JSON)
    if not os.path.exists(flag):
        return
    hit = []
    for path in (BGP_DIR + "/mihomo.yaml", BGP_DIR + "/singbox.json",
                 BGP_DIR + "/shadowrocket.conf"):
        if not os.path.exists(path):
            continue
        try:
            raw = open(path, encoding="utf-8").read()
            if path.endswith(".json"):
                if _NR_GROUP not in raw:
                    continue
                obj = json.loads(raw)
                obj["outbounds"] = [o for o in obj.get("outbounds", [])
                                    if not (isinstance(o, dict)
                                            and o.get("tag") == _NR_GROUP)]
                rt = obj.get("route") or {}
                rt["rules"] = [r for r in (rt.get("rules") or [])
                               if not (isinstance(r, dict)
                                       and r.get("outbound") == _NR_GROUP)]
                out = json.dumps(obj, ensure_ascii=False, indent=2)
            else:
                if _NR_MARK_IN not in raw:
                    continue
                out = re.sub(r"(?ms)^[ \t]*%s\n.*?^[ \t]*%s\n"
                             % (re.escape(_NR_MARK_IN), re.escape(_NR_MARK_OUT)),
                             "", raw)
            open(path, "w", encoding="utf-8").write(out)
            hit.append(os.path.basename(path))
        except (OSError, ValueError):
            continue                        # 清不掉就算了，别把更新搞挂
    try:
        os.remove(flag)
    except OSError:
        pass
    if hit:
        info(f"顺手清掉了旧版「节点分流规则」写进节点配置的那段："
             f"{'、'.join(hit)}{DIM}（这个功能已撤销）{RST}")


def do_update(from_menu=False):
    """更新：脚本自身 + 镜像 + 按新脚本重新生成配置。用户数据和密码都不动。

    from_menu 只影响自我更新之后 re-exec 的走法：菜单里进来的，跑完要回菜单。
    """
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return

    # 【换过脚本之后别再查一次】更新的走法是：老脚本发现有新版 → 换掉自己 → re-exec，
    # 新脚本从头再跑一遍 do_update，于是 self_update 被调用两次，屏幕上连着出现
    #     ✔ 脚本已更新：v1.5.29 → v1.5.30，用新版继续...
    #     ✔ 脚本 v1.5.30 → v1.5.30（已是最新，仓库里没有更新的版本）
    # 看着像更新了两次。用环境变量把"我是被 re-exec 起来的"带过去（execv 继承当前
    # environ），顺手也省掉第二次那趟没用的网络请求。
    if not os.environ.get("MS_SELF_UPDATED") and self_update():
        me = os.path.realpath(__file__)
        # 【把"我是从菜单进来的"这件事带过去】re-exec 会把当前进程整个换掉，调用栈
        # 连同"跑完该回哪儿"一起没了。用命令行那条 update 参数的话，新进程做完就
        # 退出 —— 用户按回车直接被弹回外层的 bgpeer 主菜单，而他明明是在
        # 「15 自建 Emby」里面点的更新，本该回到那个子菜单。
        os.environ["MS_SELF_UPDATED"] = "1"
        os.execv(sys.executable,
                 [sys.executable, me, "update-menu" if from_menu else "update"])

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
    # mediawarp / autofilm 的配置以前是不碰的，理由是"里面有用户填的东西"。那个理由站不住：
    # 用户填的就三样（Emby API Key、网盘挂载路径、cron），全都能从上次生成的文件里读回来。
    # 代价却是配置层面的 bug 永远修不到已装的机器上。
    cfg = rebuild_cfg_from_disk(d)
    mw_cfg = os.path.join(d, "mediawarp", "config", "config.yaml")
    af_cfg = os.path.join(d, "autofilm", "config", "config.yaml")
    for path, gen, svc in ((mw_cfg, gen_mediawarp_conf, "mediawarp"),
                           (af_cfg, gen_autofilm_conf, "autofilm")):
        if not os.path.exists(path):
            continue
        info(f"按当前版本重新生成 {svc} 配置...")
        # 【先生成，再开文件】open(path,"w") 是【立刻清空】的：如果 gen() 在这之后
        # 抛异常，磁盘上留下的是一份【空配置】—— 容器这次没重启所以还看不出来，
        # 等下次重启才整个起不来，而那时候早忘了是哪一步弄的。实测栽过一次
        # （cfg 少一个键 → KeyError），所以顺序必须是这个。
        try:
            text = gen(cfg)
        except Exception as e:
            err(f"生成 {svc} 配置失败：{e}")
            warn(f"{path} 保持原样没动，更新继续往下走。")
            continue
        write_atomic(path, text)
        subprocess.run(["docker", "restart", svc], capture_output=True)
    # 【老版本 OpenList（alist v3）还有「网站 URL」这个设置，有就顺手填上】
    # v4 已经把它去掉了，那边靠的是上面重新生成的 mediawarp 配置里那个 addr。
    ensure_openlist_site_url(d, cfg, quiet=True)
    if os.path.exists(mw_cfg) and not cfg["emby_api_key"]:
        warn("MediaWarp 的 Emby API Key 是空的，302 直链不会生效。")
        warn("用「3 后补参数 → 添加 API 密钥」补上。")

    # CLI 也要跟着换:它是脚本生成的,里面的命令逻辑会随版本变
    # (比如 strm 从"只重启容器"改成"真的跑一次任务")。不重装一遍就拿不到。
    install_cli(d)
    # 先收尸再装新的。老版本的 cron 没有 flock/timeout，卡住的那些会一直吊着
    # 占内存（实测叠到十个、1.35 G、swap 吃满）—— 光把 cron.d 换成带锁的版本
    # 治不了已经堆在那里的，用户会以为更新没用。
    stale = reap_stale_tasks()
    if stale:
        ok(f"清掉 {stale} 个卡住的后台任务进程{DIM}（老版本没装互斥锁，"
           f"卡住的那轮会一直吊着占内存）{RST}")
    # 规则文件和脚本一样住在仓库里，不主动拉就永远到不了机器上 —— 用户在
    # GitHub 上改完，机器这边一点变化都没有，看起来就像改了没用。
    _src = rules_source()
    if fetch_lib_rules(d):
        _lr, _ = lib_rules(d)
        ok(f"媒体库关键词规则已更新"
           f"{DIM}（{'自定义链接' if _src == 'custom' else '作者的'}）{RST}"
           f"（{len(_lr)} 条：{'、'.join(r['name'] for r in _lr)}）")
        if os.path.exists(lib_rules_path(d, True)):
            print(f"  {DIM}注意：本机有覆盖文件 {lib_rules_path(d, True)}，"
                  f"实际生效的是它，不是刚拉下来的这份。"
                  f"要用链接就删掉它。{RST}")
    # 一次性把早先版本自动设的 720 分钟迁回默认。见 dir_cache_auto_apply。
    # 放在这里是因为它要停一次 OpenList，而更新本来就在重启容器。
    try:
        dir_cache_auto_apply(d)
    except Exception as e:
        warn(f"调整目录缓存失败（不影响使用）：{_short_err(e)}")
    install_keepalive(d)      # 保活定时任务也跟着换新（路径/频率可能变）
    install_sync_cron(d)      # 老用户也补上每日对齐（这个版本才有）
    install_warm_cron(d)      # 定时预热同上
    # 【自动更新】用户的原话："我不可能每过几天点更新一次吧"。只换脚本，
    # 不拉镜像也不重生成配置 —— 理由见 do_selfupdate 的文档字符串。
    install_selfupdate_cron(d)

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

    # 更新会按当前版本重写 mediawarp 配置（切回 alist_strm）。磁盘上要是还留着
    # 老版本写的 URL 形式 strm，那些片子立刻就播不了了 —— 而且症状很误导：
    # 挂载里点开好好的，只有 Emby 转圈。所以配置刷完顺手把 strm 归一化。
    fixed = normalize_strm_files(d)
    if fixed:
        ok(f"{fixed} 个 strm 从 URL 形式改回路径形式（老版本留下的）")

    # 这里【不测网盘】。曾经在这一步顺手列一次目录"验证网盘还通不通"，但那是跨境调用，
    # 慢的时候实测 66 秒 —— 而更新本身早就做完了，人却被钉在屏幕前等一个和更新毫无关系的
    # 结果。而且这个位置天生容易误报：上面刚把容器全重启，OpenList 起来还要几秒初始化
    # 存储，在那之前列目录会报 object not found，看着像网盘挂了，其实只是问得太早。
    # 网盘通不通归「6 链路体检」管，那边测得更细，而且是用户主动去问的时候才跑。
    print()
    ok(f"更新完成（脚本 v{SCRIPT_VERSION}）：镜像、nginx 站点、导航面板都已是当前版本")
    purge_node_rule(d)
    # 新建的媒体库拿的是 Emby 出厂默认（续播门槛 5 分钟、多版本合并开着），
    # 表现就是"新加的库没有进度条记忆"。更新时顺手调一次，用户当场能看到结果。
    _k2 = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"), "auth")
    if _k2:
        tune_strm_libraries(_k2)
        # 【刮削器/语言也要在这儿对一次】库选项的对齐一直分在两处：续播门槛和多版本合并
        # （tune_strm_libraries）就在上面这行，更新时会跑；刮削器和语言
        # （sync_library_options）只挂在「5 生成媒体库」和定时任务上，更新这条路一次都不
        # 经过 —— 而用户的预期是"更新 = 新逻辑在我机器上生效"。同样是库选项，没有道理分开。
        try:
            _r2 = lib_rules(d)[0]
            sync_library_options(d, _k2, _r2)
            sync_private_libraries(d, _k2, _r2)
        except Exception as e:
            warn(f"按规则文件对齐媒体库的刮削器/语言失败：{_short_err(e)}")
    print(f"  {DIM}Emby API Key、网盘挂载路径、cron 这些你填的东西没有被动过。{RST}")
    print(f"  {DIM}想确认网盘通不通：跑「6 链路体检」。{RST}")
    # 上面 docker compose up -d 把容器全重启了，MediaWarp 的直链缓存随之清空 —— 不热的话，
    # 用户更新完顺手去点一部片子，等的就是那几秒到几十秒的跨境换直链，而他刚做的是"更新"，
    # 不会想到是这造成的。【后台跑】：热不热得上跟这次更新成没成功毫无关系，没道理让人
    # 对着它干等。
    _k = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"),
                          "auth")
    if _k:
        try:
            subprocess.Popen(
                [sys.executable, os.path.realpath(__file__), "warm"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            print(f"  {DIM}已在后台给「继续观看」+ 最近新加的片子预热线路"
                  f"（换直链，最多几分钟）—— 不用等它，直接回车就行。{RST}")
        except Exception as e:
            warn(f"后台预热没起来（不影响更新）：{_short_err(e)}")


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
    for p in (HTPASSWD_FILE, CLI_PATH, CLI_ALIAS, MS_STATE,
              KEEPALIVE_CRON, SYNC_CRON, WARM_CRON, SELFUP_CRON):
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
    # 装完加了存储再点「5 生成媒体库」就会带上。这里只是先把意图记下来。
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
    write_atomic(env_file, f"""PUID={cfg['puid']}
PGID={cfg['pgid']}
TZ={cfg['tz']}
DATA_ROOT={cfg['data_root']}
DOMAIN={cfg['domain']}
""", 0o600)

    # 密码写进独立的 .secrets（不给 docker compose 读），重跑时靠它沿用
    write_atomic(secret_file,
                 "# 由 media-stack.py 生成，供重跑时沿用已有密码。别手改。\n"
                 f"OPENLIST_PASS={cfg['ol_pass']}\n"
                 f"BA_USER={cfg['ba_user']}\n"
                 f"BA_PASS={cfg['ba_pass']}\n", 0o600)
    # 【这一句必须在整份重写 .secrets 之后】上面是 "w"，会把备份的 Key 一起冲掉；
    # 重跑安装的人本来就有 Key，冲掉就等于让他再去 Emby 里翻一次。
    save_emby_api_key(cfg["install_dir"], cfg.get("emby_api_key", ""))

    write_atomic(os.path.join(cfg["install_dir"], "docker-compose.yml"),
                 gen_compose(cfg))
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
    install_sync_cron(cfg["install_dir"])
    install_warm_cron(cfg["install_dir"])
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
    print(f"  4) 重跑本脚本 → {BOLD}5 生成媒体库{RST}（也可以敲 media-stack strm）")
    print(f"  5) Emby 添加媒体库，路径指向 {BOLD}{STRM_PATH}{RST}")
    print(f"  6) {YELLOW}重要{RST}：该媒体库高级设置里关掉「章节图像提取」和「实时监控」，")
    print("     否则 Emby 会为了截图去拉整部影片，把网盘刷到限流。")
    print()
    print(f"  验证直链：{BOLD}media-stack 302{RST} 然后播一集，看到 302 就说明流量没走本机。")
    print()
    warn("Emby 的 8096 已收进 127.0.0.1，对外只能走 MediaWarp。直连 8096 会绕过 302。")


def mediawarp_conf_path(install_dir=None):
    return os.path.join(install_dir or ms_install_dir(), "mediawarp/config/config.yaml")


def save_emby_api_key(install_dir, key):
    """把 API Key 另存一份到 .secrets。

    【为什么要存两份】这个 Key 以前只活在 mediawarp 的配置文件里，而那份配置是「7 更新」
    每次整份重写的，重写用的值又是从它自己上一版里读回来的 —— 一条自己咬自己的链：那个
    文件一旦损坏或被写空，Key 就没了，而更新还会理直气壮地拿一个空值再生成一遍。
    """
    if not key:
        return
    sec = os.path.join(install_dir, ".secrets")
    try:
        lines = [ln for ln in (open(sec, encoding="utf-8").read().splitlines()
                               if os.path.exists(sec) else [])
                 if not ln.startswith("EMBY_API_KEY=")]
        lines.append(f"EMBY_API_KEY={key}")
        write_atomic(sec, "\n".join(lines) + "\n", 0o600)
    except OSError:
        pass          # 存不下就算了，主存储仍然是 mediawarp 的配置


def read_emby_api_key(install_dir=None):
    """当前的 API Key，读不到或为空都返回空串。

    先读 mediawarp 配置（那是真正生效的那一份），空了再退回 .secrets 的备份。
    """
    key = ""
    try:
        with open(mediawarp_conf_path(install_dir)) as f:
            m = re.search(r"^\s*auth:[ \t]*(\S*)", f.read(), re.M)
            key = m.group(1) if m and not m.group(1).startswith("#") else ""
    except OSError:
        key = ""
    if key:
        return key
    d = install_dir or ms_install_dir()
    return read_env(os.path.join(d, ".secrets"), "EMBY_API_KEY")


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
    save_emby_api_key(d, key)      # 另存一份，配置文件出事时还能捞回来
    ok("API Key 已写入 MediaWarp 配置")

    info("重启 MediaWarp...")
    if subprocess.run(["docker", "restart", "mediawarp"],
                      capture_output=True).returncode != 0:
        err("重启失败，MediaWarp 可能没在跑。用「7 更新」或 media-stack start 拉起来。")
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


SCAN_DEDUP_SEC = 300      # 刚扫完这么久之内、本地又没变，就不再扫一遍


# 上一次全库扫描是什么时候扫完的、那一刻本地有多少个 strm。
_scan_state = {"done_at": 0.0, "strm": -1}


def emby_scan_wait(key, timeout=600, label="扫描媒体库", force=False):
    """让 Emby 扫一次媒体库并【等它扫完】。返回是否确认扫完。

    必须等：迁移时要靠"先扫一次看到文件没了"来让 Emby 真正删掉旧条目。没等完就去重新生成
    的话，Emby 一次扫描里同时看到删和加，会当成没变过，旧条目的错误媒体信息就留下来了。

    【同一趟里不重复扫】「5 生成媒体库」这条路上原来最多扫三遍：挪完 strm 扫一次、生成
    流程自己扫一次、按规则建了新库再扫一次。每一遍都是让 Emby 把两千多个文件重新过一遍，
    而中间本地【一个文件都没变】—— 第二遍第三遍纯属让人干等。所以：距上一次扫完不到
    SCAN_DEDUP_SEC 秒、且这中间本地 strm 数没变，就直接返回。
    （盲点是"数目没变但文件挪过位置"。挪文件的只有 migrate_strm_layout，而它挪完自己
    就扫，扫完才记下新的数 —— 所以这个盲点在现有流程里落不到实处。）

    【等的时候要说话】原来 migrate 挪完 strm 之后直接进这里干等，最长十五分钟，屏上
    一个字都没有 —— 用户看到的就是"卡在「14 个 strm 已挪好」不动了"。
    """
    if not key:
        return False
    now_n = -1
    try:
        now_n = strm_count(ms_install_dir())
    except Exception:
        pass
    if (not force and _scan_state["strm"] == now_n >= 0
            and time.time() - _scan_state["done_at"] < SCAN_DEDUP_SEC):
        print(f"  {DIM}刚扫过一遍，本地也没变，跳过这次扫描{RST}")
        return True

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

    t0 = time.time()
    deadline = t0 + timeout
    nudge = t0 + 20
    seen_running = False
    while time.time() < deadline:
        time.sleep(3)
        if time.time() >= nudge:
            el = int(time.time() - t0)
            # 【必须报出在等什么】两千多个 strm，Emby 要挨个 stat 一遍，几分钟很正常。
            # 不说话的话这几分钟和"卡死了"在屏上长得一模一样。
            print(f"  {DIM}...Emby 还在{label}（{now_n if now_n >= 0 else '?'} 个条目），"
                  f"已等 {el // 60} 分 {el % 60} 秒{RST}")
            nudge = time.time() + 30
        t = scan_task()
        if t is None:
            continue
        if t.get("State") != "Idle":
            seen_running = True
            continue
        end = (t.get("LastExecutionResult") or {}).get("EndTimeUtc", "")
        if seen_running or (end and end != before):
            _scan_state["done_at"], _scan_state["strm"] = time.time(), now_n
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


def shared_identity_items(key):
    """strm 媒体库里刮到了【同一个身份】的条目，按身份分组返回 {身份: [(id, 名字)]}。

    Emby 的观看记录不是按条目 id 存的，而是按一组"用户数据键"，其中就包含刮削到的外部 id
    （TMDb/IMDb 那些）。两个文件被刮成同一部片，它们就【共用一份观看进度】—— 看了 A，B 也
    跟着变成看过；A 的续播点会出现在 B 上，哪怕 B 根本没那么长（实测：一集 17 分钟的动画和
    一部 93 分钟的剧场版，两个条目的续播点都是 38:21）。

    网盘库里这种误撞非常容易发生：文件名带 [第154集•4K] 这类标记，Emby 解析不出片名，拿去
    搜就是碰运气。而它坏掉的是【观看记录】—— 最不该出错、也最难恢复的那样东西。
    """
    groups = {}
    try:
        libs = _emby("/Library/VirtualFolders", key)
        users = _emby("/Users", key)
    except Exception:
        return {}
    uid = (users[0] or {}).get("Id", "") if users else ""
    if not uid:
        return {}
    for lb in libs:
        pid = lb.get("ItemId")
        if not pid or not is_strm_lib(lb):
            continue
        try:
            r = _emby(f"/Users/{uid}/Items?ParentId={pid}&Recursive=true"
                      f"&IncludeItemTypes=Movie,Episode,Video"
                      f"&Fields=Path,ProviderIds", key)
        except Exception:
            continue
        for i in r.get("Items") or []:
            pids = i.get("ProviderIds") or {}
            # 只认真正能当身份用的那几个外部库 id。Emby 自己的内部 id 每个条目都不同，
            # 算进来就永远分不到一组
            sig = tuple(sorted((k, v) for k, v in pids.items()
                               if k.lower() in ("tmdb", "imdb", "tvdb")))
            if not sig:
                continue
            groups.setdefault(sig, []).append((i.get("Id"),
                                               str(i.get("Name") or "?")))
    return {k: v for k, v in groups.items() if len(v) > 1}


def clear_impossible_progress(key):
    """把「续播点比片长还大」的记录清零。返回被清零的条目 id 集合。

    【返回 id 而不是个数】清零之后这些条目在 Emby 眼里就是"没有进度"，和用户自己标未播放
    长得一模一样。紧跟着跑的 sync_progress_map 必须能把两者分开，否则它会把备份记录也一起
    删掉 —— 见那边 just_zeroed 那段。

    这种记录物理上不可能，一定是别的条目串过来的（见 split_shared_identities）。界面上的
    表现是「剩余 -35 分钟」，恢复播放会跳到一个根本不存在的位置。

    【为什么必须由脚本清】断开身份只让以后不再串，已经写进库里的那个数不会自己消失；而
    Emby 的界面里没有"清除续播点"这个操作。判据只用一条【客观不可能】：位置 > 片长，留 2%
    余量 —— 刮削回填的片长和文件实际长度常有零点几分钟的出入。
    """
    try:
        libs = _emby("/Library/VirtualFolders", key)
        users = _emby("/Users", key)
    except Exception:
        return set()
    uid = (users[0] or {}).get("Id", "") if users else ""
    if not uid:
        return set()
    n, failed, zeroed = 0, [], set()
    for lb in libs:
        pid = lb.get("ItemId")
        if not pid or not is_strm_lib(lb):
            continue
        try:
            r = _emby(f"/Users/{uid}/Items?ParentId={pid}&Recursive=true"
                      f"&IncludeItemTypes=Movie,Episode,Video&Fields=UserData", key)
        except Exception:
            continue
        for i in r.get("Items") or []:
            run = i.get("RunTimeTicks") or 0
            pos = (i.get("UserData") or {}).get("PlaybackPositionTicks") or 0
            if not run or pos <= run * 1.02:
                continue
            iid, nm = i.get("Id"), str(i.get("Name") or "?")
            try:
                _emby(f"/Users/{uid}/Items/{iid}/UserData", key, method="POST",
                      body={"PlaybackPositionTicks": 0, "Played": False},
                      timeout=30)
            except Exception as e:
                failed.append((nm[:22], _short_err(e)))
                continue
            # 回读确认 —— 这个接口在不同 Emby 版本上的行为不一致，
            # 不核对的话会打出"已清零"而库里纹丝不动
            try:
                back = _emby(f"/Users/{uid}/Items/{iid}", key, timeout=30)
                still = (back.get("UserData") or {}).get(
                    "PlaybackPositionTicks") or 0
            except Exception:
                still = 0
            if still > run * 1.02:
                failed.append((nm[:22], f"清了但回读还是 {still / 6e8:.1f} 分"))
                continue
            n += 1
            zeroed.add(str(iid))
            print(f"  {DIM}·{RST} {nm[:26]}  续播点 {pos / 6e8:.1f} 分 → 0"
                  f"{DIM}（片长只有 {run / 6e8:.1f} 分）{RST}")
    if failed:
        warn(f"{len(failed)} 个条目的脏续播点没清掉：")
        for nm, why in failed[:5]:
            print(f"  {DIM}·{RST} {nm}  {why}")
        print(f"  {DIM}可以在 Emby 里点开那个条目，标记成「已播放」再取消一次，"
              f"多数版本会连带把位置清掉。{RST}")
    if n:
        ok(f"清掉 {n} 个不可能的续播点（位置比片长还大）")
    return zeroed


# 三种写法都要认。只写成对标签的话，自闭合和空标签会漏网 —— 实测漏掉一行之后
# grep 还能在文件里搜到 uniqueid，而"清干净了没有"这种事不该靠肉眼去数。
_NFO_ID_TAGS = "tmdbid|imdbid|tvdbid|uniqueid|tmdbcolid"
NFO_ID_RE = re.compile(
    r"[ \t]*(?:"
    rf"<({_NFO_ID_TAGS})\b[^>]*/>"          # <uniqueid type="tmdb"/>
    rf"|<({_NFO_ID_TAGS})\b[^>]*>.*?</\2>"  # <tmdbid>123</tmdbid>
    r")[ \t]*\r?\n?",
    re.IGNORECASE | re.DOTALL)
# 抠完回头自查用：文件里还能搜到这些词，就说明有形态没覆盖到
NFO_ID_LEFT_RE = re.compile(rf"<(?:{_NFO_ID_TAGS})\b", re.IGNORECASE)


def strip_nfo_ids(strm_host_path):
    """把 strm 同名 .nfo 里的外部 id 标签抠掉。返回有没有改动。

    只删 id，标题、简介、演职人员原样留着 —— 要断的是"两个文件是同一部片"这个关联。
    改文件而不是删文件：.nfo 有可能是网盘里自带、由 AutoFilm 下载下来的，那是用户的东西。
    """
    if not strm_host_path or not strm_host_path.endswith(".strm"):
        return False
    nfo = strm_host_path[:-len(".strm")] + ".nfo"
    try:
        txt = open(nfo, encoding="utf-8").read()
    except OSError:
        return False
    out = NFO_ID_RE.sub("", txt)
    if out == txt:
        return False
    try:
        with open(nfo, "w", encoding="utf-8") as f:
            f.write(out)
    except OSError as e:
        warn(f"改 {os.path.basename(nfo)} 失败：{e}")
        return False
    # 【自查】写完再搜一遍。漏掉一种写法的后果不是"少清了一行"，而是刮削身份
    # 下次扫描又被灌回去 —— 也就是这个函数存在的全部意义落空，而且从输出上
    # 完全看不出来。宁可吵一句，不要静悄悄地留个尾巴
    if NFO_ID_LEFT_RE.search(out):
        warn(f"{os.path.basename(nfo)} 里还残留 id 标签，可能是没见过的写法。")
        print(f"  {DIM}这个条目的刮削身份可能会被扫描重新读回去。"
              f"看一眼：grep -n 'uniqueid\\|tmdbid' '{nfo}'{RST}")
        return False
    print(f"  {DIM}·{RST} 已从 {os.path.basename(nfo)} 里抠掉刮削 id")
    return True


def split_shared_identities(d, key):
    """把撞在一起的刮削身份清掉，让每个视频文件各有各的观看记录。返回改了几个条目。

    Emby 把观看记录挂在【刮削到的身份】上，不是挂在文件上 —— 这是它的有意设计：同一部电影
    放在两个媒体库里，看过一个另一个也该显示看过。但前提是那个身份【认对了】，而网盘库里
    认错是常态（文件名带 [第154集•4K] 这类标记，搜索就是碰运气）。

    【为什么整组都清，不留一个】留哪个都是猜。这一组里至少有一个是认错的，多半两个都错。
    全清之后每个条目回落到用自己的内部 id 做记录键，观看进度立刻各归各。

    已经下载好的海报、简介不受影响（那些在 Emby 自己的元数据库里）。想重新认一部片，随时
    可以用 Emby 的「识别」指定。
    """
    dup = shared_identity_items(key)
    if not dup:
        return 0
    try:
        users = _emby("/Users", key)
        uid = (users[0] or {}).get("Id", "") if users else ""
    except Exception:
        return 0
    if not uid:
        return 0
    n, stuck = 0, []
    for sig, group in dup.items():
        names = "、".join(nm for _i, nm in group)
        warn(f"这几个条目刮到了同一部片（{dict(sig)}），"
             f"Emby 会让它们共用观看进度：")
        print(f"  {DIM}{names}{RST}")
        for iid, nm in group:
            try:
                full = _emby(f"/Users/{uid}/Items/{iid}", key, timeout=30)
            except Exception as e:
                warn(f"读不到「{nm[:20]}」：{_short_err(e)}")
                continue
            # 先拆掉文件里那份存档，再改数据库。顺序反了的话，Emby 随时可能
            # 因为一次刷新把 .nfo 里的 id 重新读进来，前脚清完后脚就回来了
            strip_nfo_ids(_strm_host_path(d, full.get("Path") or ""))
            full["ProviderIds"] = {}
            # 【关键的一步：连锁定一起设】只清 id 是拦不住的 —— 清完之后条目就成了"没有
            # 身份的条目"，下一次刮削刷新会拿【片名】重新去搜，而片名没变，于是搜回同一部
            # 片、写回同一个 id（实测：验证时是 {}，过一阵又变回原来那个）。
            # LockData 让 Emby 跳过这个条目的元数据刷新，身份才停得住。代价是海报和简介也不
            # 再自动更新 —— 但这些条目的身份本来就是认错的。「识别」照样能覆盖锁定。
            full["LockData"] = True
            try:
                _emby(f"/Items/{iid}", key, method="POST", body=full, timeout=30)
            except Exception as e:
                stuck.append((nm[:22], _short_err(e)))
                continue
            # 【回读】HTTP 200 不代表改进去了。这一项实测就栽过：脚本打出"已清掉"，
            # 接口里两个条目的 Tmdb id 一个没少，用户照着那行绿勾以为修好了，
            # 白等一晚上。写操作一律回读，没有例外。
            try:
                back = _emby(f"/Users/{uid}/Items/{iid}?Fields=ProviderIds",
                             key, timeout=30)
                left = {k: v for k, v in (back.get("ProviderIds") or {}).items()
                        if k.lower() in ("tmdb", "imdb", "tvdb")}
                locked = bool(back.get("LockData"))
            except Exception:
                left, locked = {}, True
            if left:
                stuck.append((nm[:22], f"回读还是 {left}"))
                continue
            if not locked:
                # 没锁住就等于没修：id 清掉了，但下次刷新会照着片名再搜回来
                stuck.append((nm[:22], "id 清掉了但没锁住，刷新后会再长回来"))
                continue
            n += 1
    if stuck:
        err(f"{len(stuck)} 个条目的刮削身份【没能清掉】：")
        for nm, why in stuck:
            print(f"  {DIM}·{RST} {nm}  {why}")
        # Emby 收下了请求却没生效，最常见的原因是身份被写在了 .nfo 里：
        # SaveLocalMetadata 开着时 Emby 会把 tmdbid 存进媒体文件旁边的 .nfo，
        # 每次扫描再读回来 —— 从数据库里清掉多少次都会被文件重新灌进去。
        print(f"  {YELLOW}Emby 收下了请求但值没变。多半是身份被写进了 .nfo 文件，"
              f"每次扫描又读回来。{RST}")
        print(f"  {DIM}排查：ls {os.path.join(strm_root(d), STRM_SUBDIR)} 下面那几个"
              f"同名的 .nfo，里面会有 <tmdbid>。{RST}")
        print(f"  {DIM}这两个条目会继续共用观看进度，进度条数值仍然不可信。{RST}")
    if n:
        ok(f"{n} 个条目的刮削身份已清掉并锁定，观看进度从此各归各")
        print(f"  {DIM}锁定是必须的：只清 id 的话，下次刷新会拿片名重新搜，"
              f"搜回同一部片、写回同一个 id。{RST}")
        print(f"  {DIM}已经刮到的海报和简介原样保留，只是不再自动更新 —— "
              f"这些条目的身份本来就认错了，自动更新的也是错的东西。{RST}")
        print(f"  {DIM}想给某部片重新认一个正确的，用条目里的「识别」指定，"
              f"那个操作会覆盖锁定。{RST}")
    return n


def items_without_duration(key):
    """Emby 里还没探测出时长的影视条目 [(id, 名字), ...]。

    枚举方式照抄 Emby 网页端自己发的请求：ParentId 填【媒体库条目 id】、带 Recursive 和
    IncludeItemTypes。少了 IncludeItemTypes 的话 Emby 会把媒体库节点本身当结果返回，看起来
    就像"库里只有一个条目"，排查时会被带到沟里去。

    【判据看 MediaSource，不看条目】条目的 RunTimeTicks 有两个来源：文件探测，以及刮削
    （TMDb 给的片长）。探测失败的条目照样能从 TMDb 拿到一个片长填在条目上，而 MediaSource
    那边还是 0 —— 只看条目的话这个函数会认为它"已经有时长了"而跳过，补时长那步永远不会再
    试它。用户看到的是"明明显示 17 分钟，进度条还是记不住"，而体检也跟着报「时长 都有」。
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
        # 和其它几处保持一致：只看指向本脚本 strm 目录的媒体库。用户自己建的本地库
        # 不归这个脚本管 —— 本地文件 Emby 自己就能探到时长，报出来只是噪音，而且
        # heal 那边拿到非 strm 路径也只会跳过，等于报了一堆修不了的东西
        if not pid or not is_strm_lib(lb):
            continue
        try:
            d = _emby(f"/Users/{uid}/Items?ParentId={pid}&Recursive=true"
                      f"&IncludeItemTypes=Movie,Episode,Video"
                      f"&Fields=Path,MediaSources", key)
        except Exception:
            continue
        for i in d.get("Items") or []:
            srcs = i.get("MediaSources") or []
            # 有源就以源为准（源才是文件的真实探测结果）；一个源都没有时退回看条目
            need = (any(not (s.get("RunTimeTicks") or 0) for s in srcs) if srcs
                    else not (i.get("RunTimeTicks") or 0))
            if need:
                out.append((uid, i.get("Id"), i.get("Name") or "?"))
    return out


# 指向 strm 的媒体库要改的选项。默认值全是按【本地整理好的片库】设计的，
# 用在网盘库上每一条都会出事，而且 Emby 的网页设置里这些都不给改，只能走接口。
STRM_LIB_OPTIONS = {
    # 续播门槛：默认 120 秒 / 2%。网盘库里什么长度都有，一个 1 分多钟的片子播放位置永远到
    # 不了 120 秒，于是永远没有续播记忆 —— 表现为"长的记得住、短的记不住"。
    # 两个值要一起改：百分比那条是【按时长算】的，1% 对一部 94 分钟的电影就是 56 秒，秒数
    # 设再小也会被它卡住。所以百分比设 0，让秒数说了算。
    "MinResumeDurationSeconds": 2,
    "MinResumePct": 0,
    # 多版本合并：默认开，本意是伺候「流浪地球 4K.mkv + 流浪地球 1080p.mkv」这种同一部电影
    # 的不同画质。但它是按【清理后的文件名】和【刮削到的元数据】分组的，不是按文件 —— 网盘
    # 里名字相近的两部片（去掉方括号后前缀一样）会被强行并成一个条目，后果有两层：
    #   · 少一部片 —— Emby 里只剩一个条目，用户以为文件没扫出来
    #   · 进度条坏掉 —— 合并后的条目挂着两个源，探测失败的那个时长是 0，Emby 拿它算续播
    #     百分比就判定"看完了"，续播点存不下来。这层尤其难查：能播，只有进度条不对
    # 网盘库里几乎不存在"同一部片多个画质放同一个文件夹"的用法，两个都关掉。
    "EnableMultiVersionByFiles": False,
    "EnableMultiVersionByMetadata": False,
    # 别把元数据写回 strm 目录。默认开着时 Emby 会在每个媒体文件旁边生成 .nfo，里面带
    # <uniqueid type="tmdb">。那个文件成了刮削身份的【第二份存档】：从数据库里清掉多少次，
    # 下一次扫描读 .nfo 又灌回去。何况 strm 目录本来就是脚本生成、脚本清理的镜像目录 ——
    # 网盘里自带、由 AutoFilm 下载过来的那份才是用户的东西，不受这个影响。
    "SaveLocalMetadata": False,
}
# 体检那边要单独引用，避免两处各写一份魔法数字
RESUME_MIN_SECONDS = STRM_LIB_OPTIONS["MinResumeDurationSeconds"]
RESUME_MIN_PCT     = STRM_LIB_OPTIONS["MinResumePct"]

# 【会让 Emby 跨境去拉视频文件的那几个开关】，一律关掉。
#
# 本地片库上它们都是好东西：章节点、预览缩略图、提前下好的图，代价只是读一遍本地磁盘。而
# 这里的"文件"是 strm —— Emby 要生成章节点就得真的把视频拉过来看，一个几 MB 起步。这台机器
# 已经为同一类操作（补时长去读 MP4 的 moov）跑掉过 80 GB，不能再开第二个口子。
#
# 【为什么是候选名而不是一个名字】Emby 各版本里这些字段改过名，而
# /Library/VirtualFolders/LibraryOptions 对【不认识的字段是静默忽略】的 —— 猜错了不报错，
# 只是什么都不发生，然后脚本照样打印"已设好"。
STRM_LIB_TOGGLES = (
    (("EnableChapterImageExtraction", "ExtractChapterImagesDuringLibraryScan",
      "EnableAutomaticChapters", "EnableChapterExtraction"), False, "章节生成/章节图像"),
    (("ThumbnailImagesIntervalSeconds", "VideoPreviewThumbnails",
      "EnableVideoPreviewThumbnails"), 0, "视频预览缩略图"),
    (("DownloadImagesInAdvance",), False, "预先下载图像"),
    (("SaveImagesInMediaFolders", "SaveLocalImagesInMediaFolders"), False,
     "把图片写回媒体文件夹"),
    (("AutomaticRefreshIntervalDays",), 0, "初次导入后自动联网刷新"),
)


def _pick_opt_key(opts, names):
    """候选名里挑一个【这个库真的有】的键。一个都没有返回 ""。

    见 STRM_LIB_TOGGLES 的注释：这个接口静默忽略不认识的字段，所以"写了个不存在的键"和
    "写成功了"从返回值上分不出来。只认已经存在的键 —— 猜错就等于没做。
    """
    for n in names:
        if n in (opts or {}):
            return n
    return ""

# MaxResumePct 不动 —— 那条是按比例算的，长短本来就公平。


def tune_strm_libraries(key):
    """把指向 strm 的媒体库选项调成适合网盘库的值。见 STRM_LIB_OPTIONS。

    只动指向本脚本 strm 目录的媒体库：用户自己另外建的库（本地电影、音乐之类）不该被碰。
    这个 Emby 版本没有的那几个选项，名字收进 miss_all 写到状态里给体检读，不在这里打印。
    """
    try:
        libs = _emby("/Library/VirtualFolders", key)
    except Exception:
        return 0
    n_changed, miss_all = 0, set()
    for lb in libs:
        if not is_strm_lib(lb):
            continue
        o = lb.get("LibraryOptions") or {}
        diff = {k: v for k, v in STRM_LIB_OPTIONS.items() if o.get(k) != v}
        # 会跨境拉视频的那几个开关，按【这个库真的有的键名】来关（见 STRM_LIB_TOGGLES）
        missing = []
        for names, val, human in STRM_LIB_TOGGLES:
            k = _pick_opt_key(o, names)
            if not k:
                missing.append(human)
                continue
            if o.get(k) != val:
                diff[k] = val
        # 【不在这儿打印】上一版对每个库打一行"没找到这几项的设置字段"，每次更新都刷四五
        # 行一模一样的话，而它【不是故障也没法处理】。更要命的是它把库名（用户自己起的分类
        # 名）印在了每一次更新的输出里，而那些输出是会被截图发出去的。
        # 字段名对不上是【版本属性】不是某个库的属性，所有库的结果必然一样 —— 收集起来交给
        # 体检报一行就够了。
        miss_all.update(missing)
        if not diff:
            continue
        was = {k: o.get(k) for k in diff}
        o.update(diff)
        try:
            _emby("/Library/VirtualFolders/LibraryOptions",
                  key, method="POST",
                  body={"Id": lb.get("ItemId"), "LibraryOptions": o})
        except Exception as e:
            warn(f"改「{lb.get('Name')}」的媒体库选项失败：{_short_err(e)}")
            continue
        # 【必须回读确认】这个接口对不认识的字段是静默忽略的：HTTP 200 不代表改进去了。
        # 不回读的话，脚本会年复一年地打印"已改成 2 秒"，而 Emby 那边纹丝不动 ——
        # 用户照着这行字排除掉这个方向，真正的原因反而永远查不到。
        try:
            back = _emby("/Library/VirtualFolders", key)
            now = next((x.get("LibraryOptions") or {} for x in back
                        if x.get("ItemId") == lb.get("ItemId")), {})
        except Exception:
            now = {}
        # 【拿 diff 比，不是拿 STRM_LIB_OPTIONS 比】diff 里现在还混着
        # STRM_LIB_TOGGLES 那几个键，去常量表里查会直接 KeyError
        bad = [k for k in diff if now.get(k) != diff[k]]
        if bad:
            warn(f"「{lb.get('Name')}」有选项没改动成功："
                 f"{'、'.join(f'{k}={now.get(k)}' for k in bad)}")
            print(f"  {DIM}Emby 收下了请求但没生效，这个版本的接口可能不吃这些字段。{RST}")
            continue
        n_changed += 1
        name = lb.get("Name")
        if "MinResumeDurationSeconds" in diff or "MinResumePct" in diff:
            ok(f"媒体库「{name}」续播门槛：{was.get('MinResumeDurationSeconds')}秒/"
               f"{was.get('MinResumePct')}% → {RESUME_MIN_SECONDS}秒/{RESUME_MIN_PCT}%")
            print(f"  {DIM}默认的 120 秒是按电影长度定的，短片子永远够不到，"
                  f"表现为「长的记得住、短的记不住」。{RST}")
        if "EnableMultiVersionByFiles" in diff or "EnableMultiVersionByMetadata" in diff:
            ok(f"媒体库「{name}」已关闭多版本自动合并")
            print(f"  {DIM}Emby 默认会把名字相近的文件并成同一部片的多个「版本」。"
                  f"网盘库里那基本都是误判 —— 少一部片，而且进度条会坏。{RST}")
            print(f"  {DIM}关掉之后有几个文件就有几个条目。已经并在一起的，"
                  f"下一次扫描会拆开。{RST}")
        _tog = [h for names, _v, h in STRM_LIB_TOGGLES
                if _pick_opt_key(was, names) in diff]
        if _tog:
            ok(f"媒体库「{name}」关掉了会拉视频的选项：{'、'.join(_tog)}")
            print(f"  {DIM}这些在本地片库上是好东西，代价只是读一遍本地磁盘；"
                  f"而这里的文件是 strm，Emby 得把视频从网盘拉过来才能做。{RST}")
    save_ms_state(lib_opt_missing=sorted(miss_all))
    return n_changed


def title_policy():
    """片名用哪个来源的【默认值】。"filename" = 网盘文件名，"scrape" = 刮削结果。"""
    return ms_state().get("title_policy") or "scrape"


def title_policy_of(mount):
    """某个网盘的片名来源。没单独设过就跟默认值走。

    【为什么要分盘设】同一台机器上，夸克里是规规矩矩的「片名 (年份).mkv」，刮削结果更好；
    另一个盘里全是「仙逆 [第154集•4K]」这种，刮削器只会乱撞，文件名反而准。
    """
    if not mount:
        return title_policy()
    return (ms_state().get("title_by_drive") or {}).get(mount) or title_policy()


def set_title_policy_of(mount, val):
    """给某个盘单独定片名来源；val 传 None 表示"跟默认值走"。"""
    by = dict(ms_state().get("title_by_drive") or {})
    if val is None:
        by.pop(mount, None)
    else:
        by[mount] = val
    save_ms_state(title_by_drive=by)


def drive_of_strm(path):
    """从 strm 文件路径反推它属于哪个网盘挂载点。取不到返回空串。

        /data/strm/cloud/quark/电影/x.strm  →  /quark

    靠得住的原因见 strm_subpath()：strm 树是网盘树的【镜像】，cloud/ 底下第一层就是挂载点名。
    """
    marker = f"/strm/{STRM_SUBDIR}/"
    i = (path or "").find(marker)
    if i < 0:
        return ""
    rest = path[i + len(marker):].split("/")
    return "/" + rest[0] if rest and rest[0] else ""


def apply_title_policy(d, key):
    """按当前设置把 strm 条目的片名改成文件名，或者放回给刮削。返回改了几个。

    网盘里的文件名常常带 [第154集•4K] 这类标记，Emby 解析不出片名，拿去 TMDb 就是乱撞 ——
    实测一集动画被刮成了同名剧场版，两个不同的文件还刮出了一模一样的标题。

    【关键是只锁 Name 这一个字段】Emby 的 LockedFields 是按字段锁的。锁掉 Name 之后刮削照常
    跑、海报简介照常更新，只有标题不再被覆盖 —— 整条目锁死会把海报一起冻住，那不是用户要的。

    切回 scrape 时把 Name 从锁定列表里去掉就行，不去动标题本身：下一次刮削会自然覆盖回去。
    """
    # 【按盘决定，不是一刀切】want_filename 挪到循环里按条目所属的盘算，
    # 见 title_policy_of()。夸克用刮削结果、另一个盘用文件名，这两件事要能并存。
    try:
        libs = _emby("/Library/VirtualFolders", key)
        users = _emby("/Users", key)
    except Exception:
        return 0
    uid = (users[0] or {}).get("Id", "") if users else ""
    if not uid:
        return 0
    # 【两个方向要分开数】片名来源现在是按盘设的，一轮里可能既有"改成文件名"
    # 又有"交回刮削"。原来只有一个 n，末尾拿循环变量 want_filename 去决定怎么报，
    # 报的是【最后一个条目】的方向 —— 分盘之后那就是错的。
    n, n_file, n_scrape, seen, failed = 0, 0, 0, 0, []
    for lb in libs:
        pid = lb.get("ItemId")
        if not pid or not is_strm_lib(lb):
            continue
        try:
            r = _emby(f"/Users/{uid}/Items?ParentId={pid}&Recursive=true"
                      f"&IncludeItemTypes=Movie,Episode,Video&Fields=Path", key)
        except Exception:
            continue
        for i in r.get("Items") or []:
            path = str(i.get("Path") or "")
            if not path.endswith(".strm"):
                continue
            iid = i.get("Id")
            seen += 1
            # 取单个条目必须走 /Users/{uid}/Items/{iid}。裸的 /Items/{iid} 在 Emby
            # 上没有 GET 实现，会 404 —— 而这里原本 except 掉就 continue，于是每个
            # 条目都被静默跳过，整个功能一声不吭地什么都不做。改片名【是】写操作，
            # 失败必须看得见，不能和"本来就不用改"长得一样。
            try:
                full = _emby(f"/Users/{uid}/Items/{iid}", key, timeout=30)
            except Exception as e:
                failed.append((str(i.get("Name") or "?")[:20], _short_err(e)))
                continue
            locked = list(full.get("LockedFields") or [])
            stem = os.path.splitext(os.path.basename(path))[0]
            want_filename = title_policy_of(drive_of_strm(path)) == "filename"
            if want_filename:
                if full.get("Name") == stem and "Name" in locked:
                    continue
                full["Name"] = stem
                if "Name" not in locked:
                    locked.append("Name")
            else:
                if "Name" not in locked:
                    continue
                locked = [x for x in locked if x != "Name"]
            full["LockedFields"] = locked
            # 更新走 POST /Items/{id}，这个是 Emby 网页端改元数据时用的那个
            try:
                _emby(f"/Items/{iid}", key, method="POST", body=full, timeout=30)
                n += 1
                if want_filename:
                    n_file += 1
                else:
                    n_scrape += 1
            except Exception as e:
                failed.append((str(i.get("Name") or "?")[:20], _short_err(e)))
    if failed:
        warn(f"{len(failed)} 个条目的片名没改成：")
        for nm, why in failed[:5]:
            print(f"  {DIM}·{RST} {nm}  {why}")
    if n_file:
        ok(f"{n_file} 个条目的片名已改成网盘文件名（并锁定，刮削不再覆盖）")
        print(f"  {DIM}只锁了标题这一个字段，海报和简介照常跟着刮削更新。{RST}")
    if n_scrape:
        ok(f"{n_scrape} 个条目的片名解锁，交回给刮削")
        print(f"  {DIM}标题会在下一次刮削时被覆盖回去。{RST}")
    if not n and seen and not failed:
        ok(f"{seen} 个条目的片名已经是想要的样子，没有需要改的")
    elif not seen:
        warn("没找到 strm 条目 —— Emby 媒体库可能还没建或还没扫。")
    return n


def _strm_host_path(d, item_path):
    """Emby 报的容器内路径 → 宿主机上的 strm 文件路径。"""
    if not item_path or not item_path.startswith(STRM_PATH):
        return ""
    return os.path.join(strm_root(d), STRM_SUBDIR, item_path[len(STRM_PATH):].lstrip("/"))


def _strm_container_path(d, host_path):
    """宿主机上的 strm 文件路径 → Emby 看到的容器内路径。_strm_host_path 的反向。"""
    base = os.path.join(strm_root(d), STRM_SUBDIR)
    rel = os.path.relpath(host_path, base)
    if rel.startswith(".."):
        return ""
    return STRM_PATH + "/" + rel.replace(os.sep, "/")


EMBY_TARGETED_MAX = 300     # 变动超过这么多条，就老实全库扫一遍


def emby_strm_paths(key):
    """Emby 现在收录了哪些 strm（容器内路径）。问不出来返回 None。

    【None 和空集合不是一回事】问不出来（库没建、接口失败）和"一个都没收录"处置相反，
    前者只能放弃判断，后者是实打实的结论。
    """
    try:
        libs = _emby("/Library/VirtualFolders", key)
        users = _emby("/Users", key)
    except Exception:
        return None
    uid = (users[0] or {}).get("Id", "") if users else ""
    if not uid:
        return None
    known = set()
    for lb in libs:
        pid = lb.get("ItemId")
        if not pid or not is_strm_lib(lb):
            continue
        try:
            r = _emby(f"/Users/{uid}/Items?ParentId={pid}&Recursive=true"
                      f"&Fields=Path,MediaSources", key)
        except Exception:
            return None                # 有一个库问不到就整个放弃，别报假阳性
        for i in r.get("Items") or []:
            # 条目自己的 Path 【不一定】是那个 strm：片子单独放一个文件夹时，Emby 把整个
            # 文件夹当成这部电影，条目的 Path 是【文件夹】，真正的文件在 MediaSources 里。
            # 只看 Path 的话，凡是按"一部片一个文件夹"摆的片子会全部被误报成"没收录"——
            # 而那个摆法恰恰是本脚本推荐的，等于谁照着建议做谁中招。两处都收。
            for p in [str(i.get("Path") or "")] + \
                     [str(s.get("Path") or "") for s in (i.get("MediaSources") or [])]:
                if p.endswith(".strm"):
                    known.add(p)
    return known


def emby_notify_changes(key, changes, timeout=90, quiet=False):
    """把【具体变了哪几条路径】告诉 Emby，而不是让它重扫整个媒体库。

    changes 是 [(容器内路径, "Created" | "Deleted")]。

    【为什么值得单独走这条路】全库扫描是让 Emby 把两千多个 strm 挨个过一遍，几分钟起步。
    而点一次「5 生成媒体库」真正变动的常常只有十几条 —— 为了这十几条让人等几分钟，还每次
    都等。Emby 自己有这个接口（它的媒体库监视器用的就是它），给一份变动清单，它只碰这几条。

    【返回 True 必须是"确认收进去了"】不确认就报成功是这里最坏的做法：新片没进库，而屏上
    写着已完成，用户回 Emby 里找不到，而且没有任何线索指向这一步。所以发完清单要回头问
    Emby 要一次收录列表，确认到了才算数；确认不了就返回 False，调用方退回全库扫描。

    【接口不一定有】这条 API 在不同 Emby 版本上不保证存在。发不出去就是 False，
    照样退回全库扫描 —— 这条路是【快车道】，不是唯一的路。
    """
    changes = [(p, k) for p, k in changes if p]
    if not changes or len(changes) > EMBY_TARGETED_MAX or not key:
        return False
    try:
        _emby("/Library/Media/Updated", key, method="POST", timeout=60,
              body={"Updates": [{"Path": p, "UpdateType": k} for p, k in changes]})
    except Exception as e:
        if not quiet:
            print(f"  {DIM}没法只更新变动的那几条（{_short_err(e)}），改成全库扫描{RST}")
        return False
    want = {p for p, k in changes if k != "Deleted"}
    gone = {p for p, k in changes if k == "Deleted"}
    if not quiet:
        info(f"只让 Emby 过一遍变动的 {len(changes)} 条，不重扫整个库...")
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        have = emby_strm_paths(key)
        if have is None:
            return False
        if want <= have and not (gone & have):
            if not quiet:
                el = int(time.time() - t0)
                ok(f"Emby 已收到这 {len(changes)} 条变动（{el} 秒）")
            return True
    if not quiet:
        print(f"  {DIM}等了 {timeout} 秒 Emby 还没认下这几条，改成全库扫描{RST}")
    return False


def strm_not_in_emby(d, key):
    """本地有 strm、Emby 却没收进去的文件。返回容器内路径列表。

    这是整套东西里最难自查的一类失败：文件在网盘上、strm 生成了、媒体库路径也没填错，Emby
    就是不认它 —— 而界面上【一个字的提示都没有】，用户看到的只是"我明明放了两部，只出来
    一部"。原因基本都在 Emby 自己的电影库布局规则上（一个文件夹被当成一部电影、同名文件被
    并成"版本"、文件名带标记解析不出片名），这些规则 Emby 从不解释，出问题也不报错。

    枚举那一段在 emby_strm_paths() 里（问不出来返回 None，那是"放弃判断"，
    不是"一个都没有"）。
    """
    known = emby_strm_paths(key)
    if known is None or not known:
        return []                      # 一个都没有多半是库还没建，那是另一回事
    missing = []
    for hp, _tgt in strm_inventory(d):
        cp = _strm_container_path(d, hp)
        if cp and cp not in known:
            missing.append(cp)
    return sorted(missing)


DISC_DIRS = ("bdmv", "certificate")     # 蓝光原盘那棵目录树的两个顶层目录


def _bluray_discs(root):
    """strm 树里的蓝光原盘：{原盘根目录: [BDMV 底下所有 strm 的路径]}。

    认法就是"这一层底下有没有 BDMV 目录"—— 那是蓝光原盘的固定结构，不是命名习惯，
    不会认错。文件名里写着 BluRay 的普通 mkv 不受影响（它没有 BDMV 目录）。
    """
    discs = {}
    for dirpath, dirnames, _files in os.walk(root):
        low = {n.lower(): n for n in dirnames}
        if "bdmv" not in low:
            continue
        strms = []
        for dp2, _dn2, fs2 in os.walk(os.path.join(dirpath, low["bdmv"])):
            strms += [os.path.join(dp2, f) for f in fs2 if f.endswith(".strm")]
        discs[dirpath] = strms
        # BDMV 上面已经自己走过一遍了，别让 os.walk 再钻一次
        dirnames[:] = [n for n in dirnames if n.lower() not in DISC_DIRS]
    return discs


def _bluray_main_stream(strms, tok):
    """这套原盘的正片：BDMV/STREAM 里【最大】的那个片段。拿不到大小就返回空串。

    【拿不到大小宁可不做】随便挑一个的话，用户点开看到的是三十秒的厂标或菜单动画 ——
    那比"播不了"更难受，因为它看起来是成功的，人会以为片源就是坏的。

    一套原盘问一次 fs/list 就够（几十个片段都在同一个 STREAM 目录里）。
    """
    by_dir = {}
    for p in strms:
        try:
            tgt = open(p, encoding="utf-8").read().strip()
        except OSError:
            continue
        if tgt.startswith("/"):
            by_dir.setdefault(os.path.dirname(tgt), {})[os.path.basename(tgt)] = tgt
    best, best_size = "", -1
    for dirn, files in by_dir.items():
        try:
            r = _ol_api("/api/fs/list", {"path": dirn, "password": "", "page": 1,
                                         "per_page": 0, "refresh": False},
                        tok, timeout=60)
        except Exception:
            continue
        if r.get("code") != 200:
            continue
        for x in ((r.get("data") or {}).get("content") or []):
            n = x.get("name")
            if n in files and int(x.get("size") or 0) > best_size:
                best, best_size = files[n], int(x.get("size") or 0)
    return best


def collapse_bluray_folders(d, quiet=False):
    """把蓝光原盘目录压成一个 strm。返回 (处理了几套, 还没处理的几套)。

    【为什么这件事必须做，而不是提醒一下就算】原盘不是一个文件，是一整棵目录树：
    BDMV/STREAM 里几十个 .m2ts，外加播放列表和菜单索引。Emby 一看见 BDMV 就把整个
    文件夹认成一个"蓝光原盘"条目（容器写 Bluray），播放时要按索引在多个片段之间跳 ——
    那需要它拿得到【本地目录】。而 strm 里只装得下一条指向【单个文件】的地址。

    所以原盘条目在 Emby 里必定是：大小 0B、码率 0bps、点播放 load fail。这跟网盘是
    哪一家、线路好不好、直链方式怎么选都没有关系 —— 换哪个盘都一样。

    【做法：把正片挑出来，其余整棵树删掉】STREAM 里最大的那个片段就是正片（预告、
    菜单动画、花絮通常小一到两个数量级）。给它在原盘目录里生成一个同名 strm，Emby
    就当成一部普通电影去刮削和播放，302 照常生效。

    【为什么连 strm 一起删】留着的话 Emby 还是会看见 BDMV 目录，还是会把这一套认成
    原盘条目 —— 那等于白做。删的只是本地那几十个几十字节的文本文件，网盘上的原盘
    一个字节都没动，什么时候想还原重新扫一次就有。
    """
    root = strm_root(d)
    if not os.path.isdir(root):
        return 0, 0
    discs = _bluray_discs(root)
    if not discs:
        return 0, 0
    tok = _ol_token(d)
    done, stuck = 0, []
    for disc, strms in sorted(discs.items()):
        name = os.path.basename(disc.rstrip("/")) or "BluRay"
        main = _bluray_main_stream(strms, tok) if strms else ""
        if not main:
            stuck.append(name)
            continue
        try:
            write_atomic(os.path.join(disc, name + ".strm"), main)
        except OSError as e:
            stuck.append(f"{name}（写不进去：{_short_err(e)}）")
            continue
        for sub in os.listdir(disc):
            if sub.lower() in DISC_DIRS:
                shutil.rmtree(os.path.join(disc, sub), ignore_errors=True)
        done += 1
    if not quiet and (done or stuck):
        if done:
            ok(f"{done} 套蓝光原盘目录已压成单个 strm（取 BDMV 里最大的那个片段＝正片）")
            print(f"  {DIM}原盘是一整棵目录树，strm 只装得下一条指向单个文件的地址 ——"
                  f"不压的话 Emby 里就是 0B / 0bps、点播放 load fail。{RST}")
        if stuck:
            warn(f"{len(stuck)} 套原盘没能处理（问不到片段大小，多半是列目录超时）：")
            print(f"  {DIM}{'、'.join(stuck[:5])}"
                  f"{' …' if len(stuck) > 5 else ''}{RST}")
            print(f"  {DIM}下一轮对齐会再试；宁可不动，也不能随便挑一个片段 ——"
                  f"挑错了点开是厂标或菜单动画，看着像成功了。{RST}")
    return done, len(stuck)


def normalize_strm_files(d):
    """把所有 strm 统一成【路径形式】。返回改了几个。

    路径形式是常态，URL 形式只该在 heal_media_info() 补探测的那几秒里存在。但有两种情况会留
    下残留：老版本（strm 一律写 URL 那一版）生成的文件 —— heal 只处理【没有时长】的条目，
    已经探到时长的会被跳过，于是它们永远停在 URL 形式；以及 heal 中途被 Ctrl-C 或杀掉。

    后果：MediaWarp 用的是 alist_strm，而它【只认路径】。拿到 URL 会当成路径去查 OpenList，
    查不到就不 302，播放器一直转圈 —— 而挂载那边点开却是好的，因为那条路不经过 MediaWarp。
    这个"挂载能播、Emby 转圈"的组合极具迷惑性。

    所以每次生成媒体库、每次更新都无条件扫一遍。只读写本地几十字节的文本，成本可以忽略。
    """
    n = 0
    for dirpath, _dirnames, files in os.walk(strm_root(d)):
        for fn in files:
            if not fn.endswith(".strm"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                cur = open(p, encoding="utf-8").read().strip()
            except OSError:
                continue
            if not cur or cur.startswith("/"):
                continue
            want = strm_target_path(cur)
            if not want or not want.startswith("/"):
                continue
            try:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(want)
                n += 1
            except OSError:
                pass
    return n


def _under(path, root):
    """path 在不在 root 底下（含相等）。

    按【路径分段】比，不按字符串前缀 —— /data/strm/cloudX 不能算在 /data/strm/cloud 底下。
    体检里原来那句 `STRM_PATH in p` 就是字符串前缀，它正是让「库只覆盖了一个子目录」被判成
    合格的原因。
    """
    a = [x for x in path.rstrip("/").split("/") if x]
    b = [x for x in root.rstrip("/").split("/") if x]
    return a[:len(b)] == b


def is_strm_lib(lb):
    """这个媒体库是不是指向本脚本的 strm 目录（含只指向其中某个子目录的）。

    判据统一放这儿：原来同一句字符串前缀匹配在八个地方各写了一遍，而 /data/strm/cloudX 会被
    误当成 /data/strm/cloud 的一部分。八处散着改必然漏。
    """
    return any(_under(p, STRM_PATH) or _under(STRM_PATH, p)
               for p in (lb.get("Locations") or []))


def emby_lib_locations(key):
    """Emby 各媒体库实际覆盖的路径：[(库名, [路径...])]。"""
    return [(n, ps) for n, ps, _t in emby_libs(key)]


def emby_libs(key):
    """[(库名, [路径...], 内容类型)]。内容类型是 movies / tvshows / 空。"""
    try:
        return [(lb.get("Name") or "?", list(lb.get("Locations") or []),
                 (lb.get("CollectionType") or ""))
                for lb in (_emby("/Library/VirtualFolders", key, timeout=20) or [])]
    except Exception:
        return []


# 「片名 + 集号」的常见写法。中文的「第N集」和西文的 SxxExx 各来一条 ——
# 只认这两种，认得越杂误报越多，而误报会让用户去动本来没问题的文件。
EP_PATTERNS = (
    re.compile(r"^(?P<stem>.+?)[\s\-_.\[（(]*第\s*(?P<n>\d{1,4})\s*[集话話]"),
    re.compile(r"^(?P<stem>.+?)[\s\-_.]+[Ss](?P<s>\d{1,2})[Ee](?P<n>\d{1,3})\b"),
)


def _ep_key(name):
    """文件名 → (剧名, 集号)；不像剧集就返回 None。"""
    base = os.path.splitext(name)[0]
    for rx in EP_PATTERNS:
        m = rx.match(base)
        if m:
            stem = re.sub(r"[\s\-_.\[（(]+$", "", m.group("stem")).strip()
            if stem:
                return stem, int(m.group("n"))
    return None


def episode_like_dirs(d, min_eps=2):
    """看起来是剧集的文件夹：[(容器内路径, 剧名, 集数)]。

    判据刻意保守：同一个文件夹里至少 min_eps 个文件，能解析出【同一个剧名】和【不同的集号】。
    名字对不上的一律不算 —— 用户的「某个分类目录」里是四部互不相干的片子，那种绝不能被当成
    剧集。

    【只报不改】库的内容类型是 Emby 的库级设置，一个库要么全电影要么全剧集，脚本没法按文件
    夹区分；而且靠文件名猜身份正是当初坑了好几天的那类启发式。
    """
    base = os.path.join(strm_root(d), STRM_SUBDIR)
    out = []
    for cur, _dirs, files in os.walk(base):
        eps = {}
        for f in files:
            if not f.endswith(".strm"):
                continue
            k = _ep_key(f[:-len(".strm")])
            if k:
                eps.setdefault(k[0], set()).add(k[1])
        for stem, nums in eps.items():
            if len(nums) >= min_eps:
                rel = os.path.relpath(cur, base)
                out.append((STRM_PATH if rel == "." else f"{STRM_PATH}/{rel}",
                            stem, len(nums)))
    return out


def strm_dirs_uncovered(d, key):
    """strm 根目录下有哪些文件夹【不在任何媒体库范围内】。返回 [文件夹名]。

    这是「新加的片子死活扫不进来」的头号原因，而且它没有任何症状可循：文件在、权限对、容器
    里看得见、Emby 日志里一个字都没有 —— 因为 Emby 压根不知道该去扫它。

    【但反过来也不能只看顶层目录在不在库里】那样会误报，而且是很难堪的误报：同一屏上
    「Emby 收录 ✔ 7 个 strm 都收进去了」，紧挨着「✖ 1 个文件夹没被任何库覆盖：quark」。
    原因是库指向 cloud/quark/电影 而不是 cloud/quark —— strm 树改成镜像网盘目录之后，库指向
    更深的一层是常态，按顶层判必错。

    所以判据只能是【有没有 strm 文件落在所有库范围之外】，再把这些文件归到它所在的顶层目录
    报出来。没有这种文件就是绿的，和「Emby 收录」那行天然一致。
    """
    locs = [p for _n, ps in emby_lib_locations(key) for p in ps]
    if not locs:
        return []
    if any(_under(STRM_PATH, L) for L in locs):
        return []                       # 有库覆盖了根目录，底下全都在范围内
    data_root = read_env(os.path.join(d, ".env"), "DATA_ROOT") \
        or os.path.join(d, "media")
    base = os.path.join(data_root, "strm", STRM_SUBDIR)
    try:
        subs = sorted(x for x in os.listdir(base)
                      if os.path.isdir(os.path.join(base, x)))
    except OSError:
        return []
    out = []
    for top in subs:
        for dp, _dn, fs in os.walk(os.path.join(base, top)):
            if not any(f.endswith(".strm") for f in fs):
                continue                # 空目录不算 —— 里面没有片子会被漏掉
            rel = os.path.relpath(dp, base).replace(os.sep, "/")
            cpath = STRM_PATH if rel == "." else f"{STRM_PATH}/{rel}"
            if not any(_under(cpath, L) for L in locs):
                out.append(top)
                break                   # 这个顶层目录已经有片子在库外，够了
    return out


def report_not_in_emby(d, key):
    """把 Emby 没收录的 strm 摆出来，并说清楚该怎么改。

    单独一个函数是因为「5 生成媒体库」和「6 链路体检」都要用，而这段话的价值全在措辞上 ——
    只说"少了 1 个"等于没说，得指名道姓 + 给出可执行的改法。

    【必须先看文件是不是独占一个文件夹】原来无条件按「同一个文件夹里放了多部片子」去讲，可
    实测撞到的那次恰恰是独占的 —— 人家早就一片一个文件夹了，脚本还在教他"把这几个挪进各自
    的单独文件夹"，照着做只会白折腾。两种情形的成因和改法完全不同，得分开说。
    """
    missing = strm_not_in_emby(d, key)
    if not missing:
        return 0
    # 【先问最基本的那个问题】这个文件在不在任何媒体库的范围内。不在的话，后面讲布局规则、
    # 讲名字解析全是废话 —— Emby 根本没去看过它。实测踩的就是这个：新建的目录落在两个库之
    # 外，于是文件在、权限对、容器里看得见、日志里一个字没有，而脚本还在建议人家拆文件夹。
    libs = emby_lib_locations(key)
    locs = [p for _n, ps in libs for p in ps]
    outside = [p for p in missing
               if locs and not any(_under(p, L) for L in locs)]
    rest = [p for p in missing if p not in outside]
    # 剩下的按「这个 strm 的文件夹里还有没有别的视频」分两拨
    shared, alone = [], []
    for p in rest:
        sibs = _strm_siblings(d, p)
        (shared if sibs > 1 else alone).append((p, sibs))
    print()
    warn(f"有 {len(missing)} 个 strm 生成了，但 Emby 里没有对应的独立条目：")

    if outside:
        for p in outside[:8]:
            print(f"  {DIM}·{RST} {p}")
        if len(outside) > 8:
            print(f"  {DIM}...另外 {len(outside) - 8} 个{RST}")
        print(f"  {YELLOW}这些文件不在任何媒体库的范围内 —— Emby 根本不会去扫，"
              f"所以既没有条目，日志里也不会有记录。{RST}")
        print(f"  {DIM}现在的媒体库只覆盖这些路径：{RST}")
        for name, ps in libs:
            for lp in ps:
                print(f"      {pad(name, 14)}{DIM}{lp}{RST}")
        print(f"  {YELLOW}两种改法：{RST}")
        print(f"  {DIM}  · 把其中一个库的路径改成 {BOLD}{STRM_PATH}{RST}"
              f"{DIM}（覆盖全部）—— 以后新加的片子{BOLD}自动进库{RST}"
              f"{DIM}，代价是所有片子混在一个库里{RST}")
        print(f"  {DIM}  · 或者给这个文件夹单独加一个媒体库 —— 保持分类，"
              f"但每加一个新文件夹都要手动加一次{RST}")
        print(f"  {DIM}Emby → 设置 → 媒体库 → 选中库 → 编辑文件夹。"
              f"改完回来点一次「5 生成媒体库」。{RST}")
        if not (shared or alone):
            return len(missing)
        print()

    for p, sibs in (shared + alone)[:8]:
        print(f"  {DIM}·{RST} {p}"
              + (f"   {DIM}(同目录 {sibs} 个视频){RST}" if sibs > 1 else ""))
    if len(shared) + len(alone) > 8:
        print(f"  {DIM}...另外 {len(shared) + len(alone) - 8} 个{RST}")

    if shared:
        print(f"  {DIM}文件和 strm 都没问题，是 Emby 的电影库布局规则把它吃掉了：{RST}")
        print(f"  {DIM}同一个文件夹里放了多部片子时，Emby 可能只认其中一部，"
              f"另一部要么被忽略，要么被并成前一部的一个「版本」。{RST}")
        print(f"  {DIM}并成「版本」还会连累进度条：那个条目挂着两个源，探测失败的那个"
              f"时长是 0，续播点就存不下来。{RST}")
        print(f"  {YELLOW}先看「6 链路体检」的「媒体库选项」那一行。{RST}"
              f"{DIM} 本脚本会自动关掉多版本合并，"
              f"关掉之后有几个文件就有几个条目，这一条通常就不会再出现。{RST}")
        print(f"  {DIM}如果那一行是打勾的、这里还在报，才需要动文件：把这几个挪进"
              f"各自的单独文件夹。{RST}")
        # 拆文件夹是【下策】，两个代价必须说在前面，否则用户照做完会遇到新的困惑：
        #   · Emby 的规则是"文件夹里只有一个视频 → 用文件夹名去刮削"。拆完之后
        #     刮削依据从文件名变成文件夹名，而用户要的往往正是按文件刮
        #   · 扫描耗时取决于目录个数不是片子个数，无差别拆等于成倍增加跨境列目录
        print(f"  {YELLOW}但拆之前知道两件事：{RST}")
        print(f"  {DIM}  · 一个文件夹里只剩一个视频时，Emby 会改用{BOLD}文件夹名{RST}"
              f"{DIM}去刮削，不再看文件名{RST}")
        print(f"  {DIM}  · 目录越多，每次扫描要跨境列的次数越多；名字差别大的片子"
              f"平铺在一起本来就没问题，别全拆{RST}")

    if alone:
        print(f"  {YELLOW}上面这些已经是一片一个文件夹了，所以不是布局问题，"
              f"别去动目录结构。{RST}")
        print(f"  {DIM}这种情况下 Emby 用【文件夹名】刮削。常见的三个原因：{RST}")
        print(f"  {DIM}  1. 扫描还没真跑完 —— 触发的是全库扫描，大库可能要几分钟。"
              f"先去 Emby 后台看「计划任务」里扫描是不是还在跑{RST}")
        print(f"  {DIM}  2. 文件夹名里的额外标记（4K、60帧、高码、压制组…）"
              f"让 Emby 解析不出片名。理想是 {BOLD}某电影 (2004){RST}"
              f"{DIM}，多余的挪到文件名里去{RST}")
        print(f"  {DIM}  3. Emby 把它当成了「特典/花絮」—— 文件夹或文件名里带"
              f"trailer、sample、extras 这类词就会{RST}")
        print(f"  {DIM}想知道到底是哪个，去 Emby 日志里搜这个文件名：{RST}")
        print(f"      {CYAN}docker logs --tail 400 emby 2>&1 | grep -i "
              f"'{os.path.basename(alone[0][0])[:28]}'{RST}")
        print(f"  {DIM}日志里没有它 = Emby 压根没扫到（原因 1）；"
              f"有它但报解析失败 = 名字问题（原因 2/3）。{RST}")
    print(f"  {DIM}改完回来点一次「5 生成媒体库」。{RST}")
    return len(missing)


def _strm_siblings(d, strm_path):
    """这个 strm 所在的文件夹里一共有几个 strm。1 = 它独占一个文件夹。

    传进来的是 Emby 视角的容器内路径，得先换算回宿主机路径才能去数 —— 直接拿容器路径去
    listdir 只会数出 0，然后每一条都被误判成「独占」，这个分支就白加了。
    """
    data_root = read_env(os.path.join(d, ".env"), "DATA_ROOT") \
        or os.path.join(d, "media")
    host = strm_path
    if strm_path.startswith(STRM_PATH):
        host = os.path.join(data_root, "strm", STRM_SUBDIR,
                            strm_path[len(STRM_PATH):].lstrip("/"))
    try:
        folder = os.path.dirname(host)
        return sum(1 for f in os.listdir(folder) if f.endswith(".strm"))
    except OSError:
        return 1                    # 数不出来就当独占，宁可少给一段用不上的建议


def strm_inventory(d):
    """本地每个 strm 和它在 OpenList 上的目标路径 [(本地文件, 网盘路径), ...]。"""
    out = []
    for dirpath, _dirnames, files in os.walk(strm_root(d)):
        for fn in files:
            if not fn.endswith(".strm"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                tgt = strm_target_path(open(p, encoding="utf-8").read())
            except OSError:
                continue
            if tgt.startswith("/"):
                out.append((p, tgt))
    return out


def _strm_sidecars(strm_path):
    """跟这个 strm 同名的附属文件（nfo / 海报 / 字幕）。

    必须带上那个点再比前缀：拿 `第01集` 直接比的话，`第01集完整版.nfo` 也会中枪。
    另一个 .strm 无论如何不碰 —— 那是另一部片子，它的死活单独判断。
    """
    dirn, fn = os.path.split(strm_path)
    stem = fn[:-len(".strm")] + "."
    out = []
    try:
        for f in os.listdir(dirn):
            if f == fn or not f.startswith(stem) or f.endswith(".strm"):
                continue
            out.append(os.path.join(dirn, f))
    except OSError:
        pass
    return out


# 「5 生成媒体库」里核对失效 strm 最多花这么久。超了就记下游标，下次接着走。
# 每日对齐那次不设限 —— 凌晨跑，没人等。
PRUNE_BUDGET = 60


def _dir_names(path, token):
    """列一个网盘目录，返回 set(文件名)；问不出来返回 None。

    三态是这个函数的全部意义：有 / 明确没有 / 问不出来。AutoFilm 自带的同步删除是两态的
    （不在扫描结果里就算删了），而跨境线路上列目录超时是常态，于是"没扫到"被当成"已删除"，
    整个目录的 strm 被清掉 —— 所以那个开关在 gen_autofilm_conf 里是【关】的，改由这里来判。

    按目录列举而不是一个文件一次 fs/get：两万文件的网盘就是 21509 次跨境请求，而 fs/get 还会
    顺带换直链，是最贵的那种调用。

    【故意不带 refresh】读缓存对"核对存活"来说是安全方向：最坏是这轮没删掉一个废 strm，下轮
    再说。宁可少删，不能误删。
    """
    try:
        r = _ol_api("/api/fs/list", {"path": path, "password": "", "page": 1,
                                     "per_page": 0, "refresh": False},
                    token, timeout=60)
    except Exception:
        return None
    if r.get("code") != 200:
        msg = (r.get("message") or "").lower()
        # 目录本身没了 = 里面的文件确实都没了。但 storage not found 是存储掉线，
        # 那一刻整个挂载点每个目录都会这么答 —— 认成"已删除"就是清空整个媒体库
        if "object not found" in msg and "storage not found" not in msg:
            return set()
        return None
    return {x.get("name") for x in ((r.get("data") or {}).get("content") or [])
            if x.get("name")}


def prune_dead_strm(d, budget=None):
    """删掉网盘上【确认已经不存在】的 strm。返回删了几个。

    用户在网盘里整理片子（新建文件夹、分类、改名）之后，AutoFilm 会在新路径下生成一批新的
    strm，但旧路径那批不会消失 —— 同步删除是关着的。表现就是 Emby 里同一部片出现两次，一个
    能放、一个点开报错，而且整理得越勤长得越多。

    【不问，直接删】本地就该是网盘的镜像。之所以敢不问，是因为判据是逐个问 OpenList 要来的
    【肯定回答】，而不是"不在扫描结果里就算删了"—— 后者在跨境线路上会把超时当成删除，整库
    清空。三态判断见 _dir_names。

    唯一保留的刹车是"整个挂载点全判死"：那更像存储掉线、根目录 ID 填错之类的配置问题，第一
    轮只记账不删。strm 随时能重新生成，真正删不回来的是 Emby 那边的观看记录 —— 刹车为它踩。
    """
    inv = strm_inventory(d)
    if not inv:
        return 0
    pw = read_env(os.path.join(d, ".secrets"), "OPENLIST_PASS",
                  fallback=os.path.join(d, ".env"))
    try:
        token = (_ol_api("/api/auth/login", {"username": "admin", "password": pw},
                         timeout=30).get("data") or {}).get("token", "")
    except Exception:
        token = ""
    if not token:
        warn("OpenList 登不上，跳过失效 strm 清理（不影响已生成的片子）。")
        return 0

    print()
    info(f"核对 {len(inv)} 个 strm 在网盘上还在不在...")
    print(f"  {DIM}只删网盘明确回答「对象不存在」的。超时、报错、存储掉线一律"
          f"当成还在 —— 宁可留废文件，不能误删。{RST}")

    # 按目录归拢，一个目录问一次。两万个文件常常只分布在几百个目录上
    by_dir = {}
    for local, tgt in inv:
        by_dir.setdefault(os.path.dirname(tgt), []).append((local, tgt))
    print(f"  {DIM}按目录核对：{len(inv)} 个文件分布在 {len(by_dir)} 个目录里，"
          f"一个目录问一次。{RST}")

    # 【时间预算 + 游标】34231 个文件分布在 2716 个目录上，一个目录一次跨境列举，
    # 全跑完要一小时 —— 而这是在"已经把扫完的盘推给 Emby、用户以为可以走了"之后
    # 发生的，等于把刚省下的时间又还回去。实测用户就是在这儿 Ctrl-C 的。
    # 改成每次只花 budget 秒，从上次停下的地方接着走，绕一圈算一遍。
    # 少删一轮的代价只是废 strm 多留一会儿；而让人干等一小时是实打实的。
    order = sorted(by_dir)
    cur = 0
    mark = os.path.join(d, "prune_cursor.txt")
    if budget:
        try:
            last = open(mark).read().strip()
            # 【从停下的那个目录本身开始，不是它后面】stopped_at 记的是"没轮到
            # 就超预算了"的那个目录，+1 会把它永远跳过去 —— 绕多少圈都核对不到。
            cur = order.index(last) if last in order else 0
        except (OSError, ValueError):
            cur = 0
        order = order[cur:] + order[:cur]        # 从游标处绕一圈
    t0 = time.monotonic()
    stopped_at = ""
    dead, unknown, seen = [], 0, 0
    for i, dirpath in enumerate(order):
        items = by_dir[dirpath]
        if budget and time.monotonic() - t0 > budget:
            stopped_at = dirpath
            print(f"  {DIM}这轮先核对到这儿（{i}/{len(order)} 个目录），"
                  f"下次从这里接着走 —— 没核对的一律当成还在。{RST}")
            break
        names = _dir_names(dirpath, token)
        seen += len(items)
        if names is None:
            unknown += len(items)            # 这个目录问不出来，里面的一律当还在
        else:
            for local, tgt in items:
                if os.path.basename(tgt) not in names:
                    dead.append((local, tgt))
        if (i + 1) % 20 == 0 and i + 1 < len(order):
            print(f"  {DIM}...已核对 {seen}/{len(inv)} 个文件"
                  f"（{i + 1}/{len(order)} 个目录）{RST}")
    if budget:
        try:
            with open(mark, "w") as f:
                f.write(stopped_at or "")        # 跑完一圈就清空，下次从头
        except OSError:
            pass

    hold_path = os.path.join(d, "prune_hold.json")

    if not dead:
        # 一个都不判死，说明上一轮记下的刹车（如果有）是存储抖了一下造成的误判，
        # 现在盘已经恢复 —— 账必须销掉。留着的话下次真出配置问题时，那条陈年记录
        # 会和新结论对上，刹车直接被跳过，等于白装
        if os.path.exists(hold_path):
            try:
                os.remove(hold_path)
            except OSError:
                pass
        if unknown:
            # 【把后果说出来】只报"N 个没问出结果"，用户猜不到这跟他看到的现象
            # 有什么关系。而现象很具体：他刚在网盘里改完名字，Emby 里就多出一个
            # 点不开的旧条目 —— 因为旧文件确实没了，但那一刻问不到答案，
            # 按"宁可留废文件不能误删"的规矩留下了。
            warn(f"{unknown} 个没问出结果（超时或报错），这轮不动它们。")
            print(f"  {DIM}刚在网盘里改过名字或挪过位置的话，旧条目会和新条目"
                  f"一起留在 Emby 里 —— 旧的点开是打不开的。{RST}")
            print(f"  {DIM}等这些目录问得通了再点一次「4」就会清掉；"
                  f"一直问不通就去 OpenList 把存储停用再启用。{RST}")
        else:
            ok("没有失效的 strm，本地和网盘对得上。")
        return 0

    def mount_of(p):
        return "/" + p.lstrip("/").split("/")[0]

    counts, totals = {}, {}
    for _l, t in dead:
        counts[mount_of(t)] = counts.get(mount_of(t), 0) + 1
    for _l, t in inv:
        totals[mount_of(t)] = totals.get(mount_of(t), 0) + 1

    # ---- 唯一的刹车：整个挂载点全判死，第一轮只记账不删 ----
    try:
        hold = json.load(open(hold_path, encoding="utf-8"))
    except Exception:
        hold = {}
    held, new_hold = {}, {}
    for m, n in counts.items():
        # 少于 5 个的挂载点不设刹车：那个规模上"全没了"本来就很正常
        # （比如只放了两部片的盘，删掉一部就是 50%，删掉两部就是全部）
        if n == totals.get(m, 0) and totals.get(m, 0) >= 5:
            if hold.get(m, {}).get("n") == n:
                pass                       # 上一轮同样的结论，这轮照删
            else:
                held[m] = n
                new_hold[m] = {"n": n, "ts": int(time.time())}
    # 只在结论变了才落盘。写的是"本轮仍然全判死"的挂载点，所以上一轮记过、这一轮
    # 恢复正常的会自动从账上消失 —— 刹车不会一直留着
    if new_hold != hold:
        try:
            with open(hold_path, "w", encoding="utf-8") as f:
                json.dump(new_hold, f, ensure_ascii=False)
        except OSError:
            pass

    if held:
        dead = [(l, t) for l, t in dead if mount_of(t) not in held]
        print()
        for m, n in sorted(held.items()):
            warn(f"{m} 下面 {n} 个文件【全部】判定为已删除 —— 这轮先不动。")
        print(f"  {DIM}整个盘都判死，更像是存储掉线或根文件夹ID 填错，而不是你真把它清空了。")
        print(f"  先去 OpenList 点一下这个挂载点确认还列得出东西。真是你删的话，")
        print(f"  下次再点「5 生成媒体库」结论一样就会删掉，无非晚一轮。{RST}")
        if not dead:
            return 0

    print()
    info(f"网盘上已经没有的 strm：{len(dead)} 个，跟着删掉。")
    for m, n in sorted(counts.items()):
        if m in held:
            continue
        print(f"  {DIM}·{RST} {m}  {n}/{totals.get(m, 0)} 个")
    for _l, t in dead[:10]:
        print(f"    {DIM}{t}{RST}")
    if len(dead) > 10:
        print(f"    {DIM}...另外 {len(dead) - 10} 个{RST}")
    if unknown:
        print(f"  {DIM}另有 {unknown} 个没问出结果（超时或报错），当成还在，没删。{RST}")

    n = 0
    for local, _t in dead:
        for f in [local] + _strm_sidecars(local):
            try:
                os.remove(f)
            except OSError:
                pass
        n += 1
    # 顺手收掉空目录：整理之后旧的目录层级会整层空下来，留着 Emby 里就是一排空文件夹。
    # 深的先删，这样父目录轮到自己时看到的已经是子目录删完之后的状态
    root = strm_root(d)
    for dirpath, _dn, _f in sorted(os.walk(root), key=lambda x: -len(x[0])):
        if os.path.abspath(dirpath) == os.path.abspath(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass
    ok(f"删掉 {n} 个失效 strm，本地和网盘对齐")
    print(f"  {DIM}Emby 里那些点不开的条目会在下一次扫描时消失。移动过位置的片子"
          f"算新条目，观看进度不跟过去 —— Emby 按路径认片子，这条改不了。{RST}")
    return n


# 补时长最多重试几轮、每轮之间隔多久。
# 一轮一次机会不够：探测要跨境换直链 + 让网盘准备文件头，实测同一条路径的耗时能从
# 0.5 秒飘到 100 秒以上，单次成败基本是抽签。
#
# 【这一步是要真的从网盘下载数据的，必须封顶】实测吃过一次大亏：挂上一个两万多文件的
# 网盘之后有 32599 个条目没有时长，而 Emby 探测就是从网盘拉一段视频文件头（几 MB 一个，
# moov 在文件尾的还要再 seek 一次）。这个循环原来没有上限、没有预算，还挂在每小时的
# cron 上跑两轮 —— 当天账单：VPS 下行 80.4 GB、上行 1.0 GB。
#
# 所以每轮只探一批，转着来。宁可多花几天补完，也不能一个晚上把人家的流量包打光。
HEAL_LIMIT  = 50         # 每轮至少探几个条目
# 【积压多的时候要能自己提速】上限写死 50 的话，一个刚扫进来两千多条的库要按每小时
# 50 个补，光补时长就得跑两天多 —— 而这两天里那些片子看一半退出全被当成看完。
# 真正该管住的是【时间】不是【个数】：慢的时候 HEAL_BUDGET 600 秒自己就刹住了，
# 快的时候（缓存命中、线路好，一个两秒）没道理还卡在 50。所以按积压量放大，
# 再用这个上限兜住 —— 探一个要从网盘拉一段文件头，不设顶会把流量打光（实测一天 80 GB）。
HEAL_LIMIT_MAX = 300
HEAL_BUDGET = 600        # 整轮封顶（秒）。用满就收工，剩下的下一轮接着
HEAL_ROUNDS = 2
HEAL_GAP    = 8          # 隔开一点，别撞夸克的频率限制（和预热同一个理由）
# 【补时长必须后台跑】这一步天生慢（每个条目要跨境换直链 + 让 Emby 去网盘拉文件头，
# 一个最坏 3 分钟），而它跟"生成媒体库成没成功"毫无关系 —— 没道理把最慢的一步钉在用户
# 面前。而且失败的多半是当时线路在抖，隔几分钟再试往往就成了。
# 所以「5」把它扔后台：一轮一轮走，中间隔 HEAL_RETRY_MIN 分钟，直到没有待探的、
# 或者用满 HEAL_BG_BUDGET。
HEAL_RETRY_MIN = 3       # 后台两轮之间隔几分钟。太密会撞网盘限流，反而更难成
HEAL_BG_BUDGET = 1800    # 后台整体封顶（秒）。用满收工，剩下的交给每小时那轮
HEAL_BG_MAX    = 200     # 后台一次最多探几个条目 —— 拉文件头是要走流量的
HEAL_PRE_T  = 40         # 预检超时：只是确认线路此刻通不通，不必等满


def _netdisk_head_ok(raw_url, timeout=HEAL_PRE_T):
    """先自己去网盘拉一小段文件头，确认这条线此刻真的能出数据。

    为什么要多这一步：探测那一发是【发给 Emby】的，Emby 再经 MediaWarp、OpenList 去换
    直链，整条链任何一环卡住都只表现为"等满 200 秒然后没探到"。自己先拉一次的好处是：
    线路此刻不通就别去烧那 200 秒；而拉过一次之后 OpenList 那边的直链是热的，紧接着的
    探测更容易在超时内跑完。

    返回 (能不能, 说明)。
    """
    if not raw_url:
        return False, "没拿到直链"
    try:
        req = urllib.request.Request(
            raw_url, headers={"User-Agent": "Mozilla/5.0",
                              "Range": f"bytes=0-{WARM_BYTES - 1}"})
        n = len(urllib.request.urlopen(req, timeout=timeout).read(WARM_BYTES))
        return (n > 0), (f"{n // 1024}KB" if n else "网盘返回了 0 字节")
    except Exception as e:
        return False, _short_err(e)


def heal_media_info(d, key):
    """给没有时长的条目补上媒体信息。进度条、续播、已看标记全靠这一步。

    Emby 拿不到时长时续播逻辑整个失效 —— 它按时长的百分比判断存不存续播点，分母为 0 就
    直接判定「已看完」，续播点清零、进度条也拖不动（播放器以为总长是 0）。

    而 strm 的两种形态各有各的死穴：
      · 路径形式  播放快（alist_strm 有直链缓存，命中时 3 毫秒 302），但 Emby 把它当本地
                  文件喂 ffprobe，必然 No such file or directory，探不出时长
      · URL 形式  Emby 能探测，但播放只能走 http_strm，那条路没有直链缓存，每次开播都要
                  现换一次直链（实测 7.5~47 秒）

    所以两种都不能常驻。做法是「只在探测那几秒钟切过去」：
        ① 把该条目的 strm 临时写成带签名的 URL
        ② 发一次 IsPlayback=true 的 PlaybackInfo，逼 Emby 现在就探测
        ③ 确认时长真的入库
        ④ 立刻写回路径形式

    第 ④ 步不会把时长弄丢，靠的是一个实测过的行为：Emby 对【已经存在的条目】不会重新
    探测。媒体信息已经在它的数据库里，跟 strm 里写什么再无关系。
    """
    allpend = items_without_duration(key)
    if not allpend:
        return
    # 【轮转取一批】。全量探的代价见 HEAL_LIMIT 那段注释。用游标是因为总有一批
    # 条目怎么探都探不出来（网盘上是残缺文件、格式 Emby 不认），每轮都从头取
    # 的话它们会把名额永远占死，后面的条目一辈子轮不到。
    cur = int(ms_state().get("heal_cursor") or 0) % max(1, len(allpend))
    # 【取多少要先夹到总数】不夹的话 (allpend+allpend) 在待探数少于 HEAL_LIMIT 时
    # 会把同一批切出来两遍 —— 7 个待探切成 14 个，每个条目探两次、流量翻倍。
    # 而这一步的全部意义就是省流量。
    take = min(max(HEAL_LIMIT, len(allpend) // 8), HEAL_LIMIT_MAX, len(allpend))
    pend = (allpend + allpend)[cur:cur + take]
    save_ms_state(heal_cursor=(cur + take) % len(allpend))
    print()
    if len(allpend) > len(pend):
        info(f"给 {len(pend)} 个条目补媒体信息（时长、编码）"
             f"{DIM}，这批之外还有 {len(allpend) - len(pend)} 个排队{RST}")
        print(f"  {DIM}每轮只探一批：探一个要从网盘拉一段文件头，几 MB 起步 ——"
              f"几万个条目一次全探会把流量打光（实测一天 80 GB）。{RST}")
        print(f"  {DIM}剩下的每小时那轮接着往后探，不用管它。{RST}")
    else:
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
    todo_items = list(pend)
    t_all = time.monotonic()
    for rnd in range(1, HEAL_ROUNDS + 1):
        if not todo_items or time.monotonic() - t_all > HEAL_BUDGET:
            break
        if rnd > 1:
            print(f"  {DIM}...{len(todo_items)} 个没探到，隔 {HEAL_GAP} 秒再试一轮"
                  f"（第 {rnd}/{HEAL_ROUNDS} 轮）{RST}")
            time.sleep(HEAL_GAP)
        again = []
        done += _heal_round(d, key, todo_items, base, token, again, t_all)
        todo_items = again
    _heal_summary(done, len(pend))


def _heal_round(d, key, pend, base, token, again, t_all=None):
    """探一轮。探不到的塞进 again 供下一轮再试。返回这一轮成功几个。

    【每个条目要先把行占上】探一个条目最坏要等 3 分钟，而结果是【探完才打印】的 ——
    等待的那三分钟屏幕上一个字都不动，从屏幕上看和死机没有区别。
    """
    done = 0
    total = len(pend)
    for idx, (uid, iid, name) in enumerate(pend, 1):
        # 预算是【这一轮里也要看】的：一个条目最多等 3 分钟，50 个就是两个多小时，
        # 光靠外层每轮之间那次判断根本刹不住 —— 而这任务是挂在每小时的 cron 上的。
        if t_all is not None and time.monotonic() - t_all > HEAL_BUDGET:
            print(f"\r  {DIM}这轮时间用完了，剩下的下一轮接着探。{RST}\033[K")
            break
        print(f"\r  {DIM}·{RST} {pad(name[:26], 28)}"
              f"{DIM}探测中… {idx}/{total}，最多 3 分钟{RST}\033[K",
              end="", flush=True)
        try:
            it = _emby(f"/Users/{uid}/Items/{iid}", key, timeout=30)
        except Exception:
            print("\r\033[K", end="")
            again.append((uid, iid, name))     # 问 Emby 失败可能只是这一下，值得再试
            continue
        # 下面几种是【问题在本地，重试也没用】：路径对不上、文件读不了、
        # strm 里没有可用目标。不进 again，免得白跑一轮还刷一屏同样的话
        host = _strm_host_path(d, it.get("Path") or "")
        if not host or not os.path.exists(host):
            print("\r\033[K", end="")      # 占位行要擦掉，不然下一条盖在上面
            continue
        try:
            original = open(host, encoding="utf-8").read()
        except OSError:
            print("\r\033[K", end="")
            continue
        p = strm_target_path(original)
        if not p:
            print("\r\033[K", end="")
            continue
        try:
            got0 = (_ol_api("/api/fs/get", {"path": p, "password": ""},
                            token, timeout=120).get("data") or {})
            sign, raw = got0.get("sign", ""), got0.get("raw_url", "")
        except Exception as e:
            print(f"\r  {DIM}·{RST} {name[:26]}  {YELLOW}换直链失败：{_short_err(e)}{RST}\033[K")
            again.append((uid, iid, name))
            continue
        # 先自己拉一段文件头。不通就别去烧 Emby 那 200 秒了，而且拉过之后
        # 直链是热的，紧接着的探测更容易在超时内跑完
        good, why = _netdisk_head_ok(raw)
        if not good:
            print(f"\r  {DIM}·{RST} {name[:26]}  {YELLOW}网盘没给出文件头（{why}）{RST}\033[K")
            again.append((uid, iid, name))
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
            # 【核对源的时长，不是条目的】条目的 RunTimeTicks 可能是刮削回填的
            # （TMDb 给的片长）。拿它当探测结果的话，探测明明失败了也会报"✔ 18 分钟"
            # —— 谎报成功比报失败坏得多：这个条目从此被当成已修好，再也不会重试，
            # 而进度条依然是坏的。判据必须和 items_without_duration 保持一致。
            try:
                got = _emby(f"/Users/{uid}/Items/{iid}?Fields=MediaSources",
                            key, timeout=30)
                srcs = got.get("MediaSources") or []
                ticks = (min((s.get("RunTimeTicks") or 0) for s in srcs) if srcs
                         else (got.get("RunTimeTicks") or 0))
                mins = ticks / 6e8
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
            print(f"\r  {GREEN}\u2714{RST} {name[:26]}  {mins:.0f} 分钟\033[K")
        else:
            print(f"\r  {DIM}\u00b7{RST} {name[:26]}  {YELLOW}Emby 没探出时长{RST}\033[K")
            again.append((uid, iid, name))
    return done


def _heal_summary(done, total):
    if done == total:
        ok(f"{done} 个条目补齐，进度条和续播可用")
    elif done:
        warn(f"{done}/{total} 个成功")
        # 【别再让用户去点菜单】每小时的对齐任务本来就会重跑这一步，而且只挑
        # 没探到的。原来那句"再点一次「5 生成媒体库」"是在让人干本来会自动发生
        # 的事，还会让他以为不点就永远不修。
        print(f"  {DIM}没成功的多半是当时网盘那条线在抖。每小时的对齐任务会自动重试，")
        print(f"  只补没探到的那些，已经好的不重来 —— 不用管它。{RST}")
    else:
        warn(f"{total} 个都没探到 —— 网盘接口现在多半不通，跑「6 链路体检」看看。")
        print(f"  {DIM}每小时的对齐任务会自动重试，线路恢复后会自己补上。{RST}")


def autofilm_clock():
    """AutoFilm 调度器当前认为的 (时, 分)。取不到返回 None。

    **不能用 `docker exec autofilm date`**：容器里的 date 认 TZ 环境变量，返回的是用户设的
    本地时间；而 AutoFilm 启动时打印的是「使用应用时区 timezone=UTC」—— 它没读到 TZ、
    回落到了 UTC，两者能差好几个小时。实测踩过：date 说 19:57，AutoFilm 认为是 11:56，
    cron 写成 "0 57 19 * * *" 要等到次日凌晨才触发，表现就是「点了没反应」。

    它日志时间戳末尾那个偏移量才是调度器真正用的那套时钟。
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

    为什么必须有这个按钮：装完的那一刻网盘还没挂上 —— OpenList 里的存储得用户自己在网页
    里添加，所以安装流程里跑 strm 一定是空的。以前这一步只有命令行 `media-stack strm`，
    不看文档、不敲命令的人到这里就是死局：OpenList 里文件明明都在，Emby 里永远刷不出来，
    界面上没有任何提示还差一步。

    触发方式和 CLI 那条一致：AutoFilm v2 没有手动执行的入口，启动时也只注册 cron、不跑
    任务，所以临时把 cron 改成两分钟后只触发一次。不用「每分钟」是因为网盘慢的时候一轮要
    一两分钟，几轮压在一起会并发扫同一个目录、互相删对方刚写出的 strm。

    【为什么还原不等到最后】AutoFilm 是启动时把 config.yaml 读进内存注册 cron 的，之后再改
    磁盘上那份不影响已经排好的这一轮。所以容器一起来就立刻还原 —— 用户可以随时 Ctrl-C
    走人，不会留下临时定时值。代价是那条临时 cron 以每天一次的形式留在内存里直到下次重启，
    无害：overwrite 是 false、同步删除是关的，重复跑一轮只是白扫一遍。
    """
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    cfg_path = os.path.join(d, "autofilm", "config", "config.yaml")
    if not os.path.exists(cfg_path):
        warn(f"找不到 {cfg_path}，AutoFilm 可能没装。")
        return

    fixed = normalize_strm_files(d)
    if fixed:
        ok(f"{fixed} 个 strm 从 URL 形式改回路径形式")
        print(f"  {DIM}URL 形式只该在补探测的那几秒存在。留在磁盘上的话 MediaWarp")
        print(f"  的 alist_strm 认不出来，表现是「挂载能播、Emby 一直转圈」。{RST}")

    before = strm_count(d)
    # 【记下这一趟开工前有哪些】收尾时拿它一减，就知道到底新增/删除了哪几条 ——
    # 有了这份清单才能只让 Emby 过这几条，而不是重扫整个库（见 emby_notify_changes）。
    snap0 = {_strm_container_path(d, hp) for hp, _t in strm_inventory(d)}
    print(f"\n  当前本地已有 {BOLD}{before}{RST} 个 strm 文件。")

    # 【有人在看片就先问一声】扫库和播放抢的是同一个网盘账号，而夸克风控很严。
    # 实测撞过：AutoFilm 在扫的那两分钟里，同一条路径列目录要 20.5 秒，
    # 扫描过去之后立刻再打是 0.4 秒 —— 快 50 倍。对正在看片的人来说，
    # 这就是"好好看着突然卡住转圈"，而且他完全不知道是有人点了「4」。
    # 定时那轮排在凌晨 05:15 正是为了避开，手动点这一下绕过了那个安排。
    _k0 = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"),
                           "auth")
    if _k0:
        try:
            if now_playing_ids(_k0):
                print()
                warn("现在有人在看片 —— 扫库和播放抢的是同一个网盘账号。")
                print(f"  {DIM}实测扫描时同一条路径列目录要 20 秒，平时是 0.4 秒；"
                      f"对方会感觉到卡顿甚至转圈打不开。{RST}")
                print(f"  {DIM}不急的话等他看完，或者交给每天凌晨那轮自动扫"
                      f"（新片最多晚一天进库）。{RST}")
                if not ask_yn("还是现在就扫？", False):
                    print("没有扫描。")
                    return
        except Exception:
            pass            # 问不到就别拦着，这只是个提醒

    # 先清缓存再扫。放在"有人在看片"那一问之后 —— 那一问可能直接退出，
    # 没必要为一次被取消的扫描去重启两个容器。
    try:
        # 只清【这次真要扫的那几个盘】。别的盘的缓存没有理由跟着遭殃 ——
        # 缓存命中的列目录不碰网盘接口，而列目录正是被限流的那一个。
        clear_dir_cache(d, {"/" + m for m in
                            (strm_mount_dir(p) for p in effective_scan_paths(d)) if m})
    except Exception as e:
        warn(f"清目录缓存失败（不影响扫描，但刚加的片子可能看不见）：{_short_err(e)}")

    original = open(cfg_path, encoding="utf-8").read()
    hm = autofilm_clock()
    if hm is None:
        g = time.gmtime()
        hm = (g.tm_hour, g.tm_min)
        warn("读不到 AutoFilm 的时区，按 UTC 估算触发时刻。")
    t = (hm[0] * 60 + hm[1] + 2) % 1440
    fire = f"0 {t % 60:02d} {t // 60:02d} * * *"

    # 【每个任务都要改，不能只改第一个】原来这里写死 count=1。单网盘时只有一条
    # cron，看不出问题；一个网盘一个任务之后，只有排在最前面那个任务被改成
    # "两分钟后触发"，其余的还是原来的凌晨定时 —— 于是永远停在「已完成 1/3」，
    # 剩下两个根本没启动，而界面上看着像是它们卡住了。
    patched, n_fire = re.subn(r'(?m)^(\s*cron:\s*)".*"$',
                              lambda m: f'{m.group(1)}"{fire}"', original)
    if not n_fire:
        # 还没动过文件就退出，别进 try —— 否则 finally 会白写一次文件
        err("没能改写 cron 那一行，为安全起见没有继续。")
        return

    def autofilm_log(since):
        r"""合并 stdout 和 stderr 并剥掉颜色码。

        两路都要读：不同版本的 AutoFilm/Docker 日志落在哪一路并不一致，只读 stdout 会漏掉
        统计数字（表现是最后那行全是问号）。
        颜色码必须先剥：AutoFilm 默认 --colorful-log，字段名被转义序列包着，直接拿正则找
        xxx_count=数字 一个都匹配不到 —— 这个坑很隐蔽，粘进聊天框时颜色码已经没了。
        """
        r = sh(f"docker logs --since {since} autofilm", timeout=60)
        return ANSI_RE.sub("", (r.stdout or "") + (r.stderr or ""))

    done = ""
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(patched)
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if subprocess.run(["docker", "restart", "autofilm"],
                          capture_output=True).returncode != 0:
            err("重启 AutoFilm 失败，它可能没在跑。")
            return

        # 等它把配置读进内存（启动会打印 scheduled_count），然后【立刻】还原磁盘。
        # 见函数开头的说明：还原之后这一轮照跑，而脚本从此可以随时被打断。
        for _ in range(20):
            if "scheduled_count" in autofilm_log(since):
                break
            time.sleep(1)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(original)
        base_lines = len(autofilm_log(since).splitlines())

        info(f"已安排在 {fire.split()[2]}:{fire.split()[1]}（容器时间）触发，最多等 2 分钟开始。")
        print(f"  {GREEN}这一步不用守着{RST}{DIM}：定时设置已经还原，扫描在容器里跑。"
              f"按 Ctrl-C 随时可以走人，不会打断它。{RST}")
        # 【这句话以前是错的】原来写"走人的话这三步要下次再点"。那是 align_library
        # 还没有的时候。现在补时长、清失效、通知扫描、调库选项、预热全都挂在每小时
        # 的对齐任务上，走人只是晚一小时，不是不做。
        # 网盘大的时候扫描能跑几十分钟，让用户守着看日志纯属浪费他的时间。
        print(f"  {DIM}补时长、清失效条目、通知 Emby 扫描这些收尾，"
              f"{BOLD}每小时的对齐任务都会做{RST}{DIM} —— 走人只是晚一小时，不是不做。"
              f"网盘大的时候扫描要跑几十分钟，没必要守着。{RST}")

        # last  = 日志里最新那行；shown = 上次报进度时已经打出去过的那行。两个都要留：
        # 日志安静的时候把同一行反复打出来，看着像任务在原地打转，而实际上是【一行都没新增】。
        #
        # 【为什么不是固定 15 分钟】扫描耗时取决于【目录个数】不是片子个数 —— AutoFilm 每个
        # 目录列一次，同一个目录里放 3 部还是 300 部，列目录的次数一样。线路好的时候 100 个
        # 目录几分钟就完了，线路烂的时候 3 个目录也能耗掉一刻钟。所以按"还在不在动"判断：
        # 只要日志还在往前走就一直等，真正卡死（长时间一行不出）才放弃。
        # 已经有盘扫完、又等了这么久还没等齐，就先把扫完的推给 Emby，剩下的留给容器继续跑 ——
        # 没道理让 60 个文件的盘陪着 2 万个文件的盘一起等。后面几步对"只扫了一部分"是安全的：
        # 清失效的判据是问网盘要的，不是看这轮扫描结果；其余都是幂等的。
        SOFT_WAIT = 300
        QUIET_GIVEUP = 360         # 连续这么久没有新日志才认定卡死（单次列目录最长也就两分多钟）
        NOSTART_GIVEUP = 300       # 一直等不到开始：cron 最多 2 分钟就该触发，5 分钟还没动就是没触发
        HARD_CAP = 3600            # 兜底总时限，防止异常情况下无限等下去
        # 【一个扫描路径 = AutoFilm 的一个任务 = 一行「任务完成」】看到第一行就 break 的话，
        # 多网盘时会在第一个任务刚完成就往下走 —— 统计只是那一个任务的，而 prune / 迁移 /
        # 通知 Emby 扫描全在其余任务还在生成的时候执行，Emby 看到的是半成品。
        # 任务数从 AutoFilm 【自己的配置】数，不从脚本的 cfg 猜 —— 那才是它真正会跑几个任务，
        # 而且 do_strm 这个位置根本没有 cfg。
        want_tasks = max(1, len(read_yaml_all(cfg_path, "source_dir")))
        done_lines, early = [], False
        started, last, shown, quiet_since = False, "", "", time.monotonic()
        t_started = 0.0                  # AutoFilm 真正动起来那一刻，用来拆时间
        slow_warned = False
        t_start = time.monotonic()
        give_up = ""
        while True:
            out = autofilm_log(since)
            lines = out.splitlines()
            for ln in lines:
                if "Alist2Strm 任务完成" in ln and ln not in done_lines:
                    done_lines.append(ln)
            if len(done_lines) >= want_tasks:
                done = "\n".join(done_lines)
                break
            # 有盘扫完了、又等够了软截止 —— 先走，别让小盘的片子陪着大盘等
            if done_lines and time.monotonic() - t_start > SOFT_WAIT:
                done = "\n".join(done_lines)
                early = True
                break
            # 日志行数超过"刚启动"那一刻，就说明任务真的动起来了。用行数而不是认
            # 某句中文：AutoFilm 各版本的措辞不一样，认死了会一直显示"还没开始"
            if len(lines) > base_lines:
                if not started:
                    t_started = time.monotonic()
                started = True
                newest = next((l.strip() for l in reversed(lines) if l.strip()), "")
                if newest and newest != last:
                    last, quiet_since = newest, time.monotonic()
            time.sleep(4)
            el = int(time.monotonic() - t_start)
            quiet = time.monotonic() - quiet_since
            if started and quiet > QUIET_GIVEUP:
                give_up = (f"日志已经 {quiet / 60:.0f} 分钟没动静，判定卡住了"
                           + (f"（{want_tasks} 个网盘只完成了 {len(done_lines)} 个）"
                              if want_tasks > 1 else ""))
                done = "\n".join(done_lines)   # 完成了几个就先按几个报，别当成一个都没跑
                break
            if not started and el > NOSTART_GIVEUP:
                give_up = "一直没等到任务开始，AutoFilm 可能没接到这次触发"
                break
            if el > HARD_CAP:
                give_up = f"已经等了 {el // 60} 分钟，先收工"
                break
            if el % 32 < 4:                        # 每 32 秒报一次，别让人以为卡死了
                phase = "正在扫描网盘" if started else "等 AutoFilm 到点触发"
                # 多网盘时把进度报出来，否则用户看到"已等 8 分钟"完全不知道
                # 是卡住了还是第 3 个盘正在扫
                prog = (f"（已完成 {len(done_lines)}/{want_tasks} 个网盘）"
                        if want_tasks > 1 else "")
                print(f"  {DIM}...{phase}{prog}，已等 {el // 60} 分 {el % 60} 秒{RST}")
                # 【按盘列出来，不能只给个 2/3】三个任务是并行跑的（AutoFilm 把
                # 它们排在同一分钟，调度器一次性全启动），而文件最多的那个盘话最密，
                # 日志里滚的全是它 —— 用户看到的是"一直在扫别人的盘"，
                # 其实自己的盘早跑完了。把每个盘的状态摊开，这个误会就没了。
                if want_tasks > 1:
                    fin = set(re.findall(r"task_id=(\S+)", "\n".join(done_lines)))
                    run = [t for t in re.findall(r"task_id=(\S+)", out) if t not in fin]
                    seen_run = list(dict.fromkeys(run))[:4]
                    bits = [f"{GREEN}✔{RST}{DIM}{t}{RST}" for t in sorted(fin)]
                    bits += [f"{YELLOW}…{RST}{DIM}{t}{RST}" for t in seen_run]
                    if bits:
                        print("       " + "  ".join(bits))
                if last and last != shown:
                    print(f"  {DIM}   {last[-88:]}{RST}")
                    shown = last
                elif last:
                    print(f"  {DIM}   日志 {quiet:.0f} 秒没有新内容，还停在上面那行{RST}")
                # 跨境列目录慢是常态，但安静两分钟以上通常是某个目录在超时重试。
                # 说一句免得用户以为脚本死了 —— 这时候它确实什么都做不了，只能等
                if quiet > 120 and started and not slow_warned:
                    slow_warned = True
                    print(f"  {YELLOW}   网盘那边在超时重试，这是跨境线路的老毛病。"
                          f"AutoFilm 会跳过列不出来的目录继续往下走。{RST}")
        # 【看 give_up，不看 done】卡住时我们也会把已完成的那几行填进 done，
        # 于是 `if not done` 永远为假 —— 卡住被报成"✔ 生成完成"，
        # 正是这套东西最不该有的那种谎报。判据必须是"有没有放弃"。
        if early:
            fin = sorted(set(re.findall(r"task_id=(\S+)", done)))
            info(f"{len(fin)}/{want_tasks} 个网盘已扫完，先把它们推给 Emby")
            print(f"  {DIM}完成的：{'、'.join(fin)}{RST}")
            print(f"  {DIM}还没扫完的在容器里继续跑，不受影响；它们生成出来的 strm "
                  f"由每小时的对齐任务接手推进 Emby。{RST}")
        elif give_up:
            if done_lines:
                got = sorted(re.findall(r"task_id=(\S+)", done))
                warn(f"{want_tasks} 个网盘只完成了 {len(done_lines)} 个：{give_up}")
                print(f"  {DIM}完成的：{'、'.join(got) or '?'}{RST}")
                print(f"  {DIM}没完成的那几个还在容器里跑，不会因为这里不等了就停；"
                      f"它们的 strm 生成完就有了，下次点「4」会接上后面几步。{RST}")
            else:
                warn(f"没等到「任务完成」：{give_up}。")
                print(f"  {DIM}扫描本身还在容器里跑，不会因为这里不等了就停。"
                      f"已经写出来的 strm 照常生效。{RST}")
    except KeyboardInterrupt:
        print()
        warn("不等了 —— 扫描在容器里继续跑，strm 会照常生成。")
        print(f"  {DIM}没跑的是后面三步：补时长（进度条要靠它）、清失效条目、"
              f"通知 Emby 扫描。{RST}")
        print(f"  {DIM}过十来分钟再点一次「5 生成媒体库」：那一次文件已经在了，"
              f"生成会秒过，这三步在那次补上。{RST}")
        return
    finally:
        # 兜底：中途报错也不能把临时定时值留在用户的配置里。正常路径上面已经还原过，
        # 这里读一眼、不一样才写，省掉一次无谓的磁盘写入
        try:
            if open(cfg_path, encoding="utf-8").read() != original:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(original)
        except OSError:
            pass

    after = strm_count(d)
    print()
    if done:
        # 【累加，不是覆盖】dict(findall) 会让最后一个任务的数字盖掉前面的，
        # 多网盘时报出来的就只是最后一个盘的量
        nums = {}
        for _k, _v in re.findall(r"([a-z_]+_count)=(\d+)", done):
            nums[_k] = nums.get(_k, 0) + int(_v)
        if nums:
            _tag = ("生成完成" if not (give_up or early)
                    else f"已完成 {len(done_lines)}/{want_tasks} 个网盘")
            ok(f"{_tag}：新增 {nums.get('strm_created_count', '?')}，"
               f"已存在跳过 {nums.get('strm_skipped_count', '?')}，"
               f"失败 {nums.get('failed_path_count', '?')}")
            # 扫到的目录数同样关键：网盘目录列不出来时它是 0,而"新增 0"看起来
            # 和"本来就没有新文件"一模一样,不把这两个数摆出来根本分不清
            print(f"  {DIM}扫描目录 {nums.get('scanned_dir_count', '?')} 个"
                  f"（跳过 {nums.get('skipped_dir_count', '?')} 个），"
                  f"发现文件 {nums.get('discovered_file_count', '?')} 个{RST}")
            # 【把这几分钟花在哪儿说清楚】用户看到"7 个片跑了 7 分钟"会以为脚本慢，
            # 而实测那次的构成是：2 分钟在等 AutoFilm 的定时触发（固定开销），
            # 5 分钟里 3 个目录一直在等夸克接口、等到超时被跳过。
            # 这两件事该做的处理完全不同 —— 前者是设计如此，后者要去 OpenList
            # 重新加载存储。数字不摊开的话，用户只会得出"这脚本慢"这一个结论。
            _tot = int(time.monotonic() - t_start)
            _wait = int((t_started or time.monotonic()) - t_start)
            def _mmss(n):
                return f"{n // 60} 分 {n % 60} 秒" if n >= 60 else f"{n} 秒"
            print(f"  {DIM}用时 {_mmss(_tot)}：等 AutoFilm 到点触发 {_mmss(_wait)}"
                  f"（固定开销）+ 实际扫描 {_mmss(max(0, _tot - _wait))}{RST}")
            if nums.get("skipped_dir_count", 0):
                warn(f"有 {nums['skipped_dir_count']} 个目录没列出来就被跳过了 —— "
                     f"网盘那边超时了，里面的文件这轮不会生成。")
                print(f"  {DIM}扫描慢也多半是这个：跳过一个目录之前要先等它超时，"
                      f"几个目录就是几分钟。{RST}")
                print(f"  {DIM}刚在网盘里改过目录名的话，先去 OpenList 把这个存储"
                      f"停用再启用（清掉旧的目录缓存），再点一次「4」。{RST}")
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

    # 【在通知 Emby 之前压原盘】不然 Emby 先把它们建成一批 0B 的"蓝光原盘"条目，
    # 之后再删再建，中间那段时间用户点进去就是 load fail。
    collapse_bluray_folders(d)

    # 生成只会【加】不会【减】。用户在网盘里整理过片子的话，旧路径那批 strm
    # 还留在本地，Emby 里就是同一部片子两个条目、一个点不开。放在扫描之前收尾，
    # 让 Emby 这一趟同时看到"新的多了"和"旧的没了"。
    prune_dead_strm(d, budget=PRUNE_BUDGET)

    # 生成完顺手让 Emby 扫一遍，省得用户还要再进 Emby 后台找「扫描媒体库」
    key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"), "auth")
    # 放在通知 Emby 扫描【之前】：让 Emby 这一趟同时看到"旧位置没了、新位置有了"，
    # 不然它会先建一批旧路径的条目，下一轮再删，中间那段时间库里是双份。
    # （注意这里跑在 AutoFilm 生成【之后】—— 所以新旧可能已经并存，migrate 会把
    # 旧的覆盖到新位置上去，结果一样，只是多搬一次。真正的兜底在 align_library
    # 里，那条每小时都跑，不管这批 strm 是谁生成的。）
    migrate_strm_layout(d, key)
    if key:
        snap1 = {_strm_container_path(d, hp) for hp, _t in strm_inventory(d)}
        diff = ([(p, "Created") for p in sorted(snap1 - snap0) if p]
                + [(p, "Deleted") for p in sorted(snap0 - snap1) if p])
        # 变动少就只报这几条（几秒）。多了、没变动、或者这条路没走通，都退回全库扫描 ——
        # 【"没变动"也要退回去扫】本地没变不等于 Emby 里就是对的：媒体库可能是刚建的，
        # 里头一个条目都没有。emby_scan_wait 自己有去重，刚扫过就不会真扫。
        if not emby_notify_changes(key, diff):
            info("通知 Emby 扫描媒体库...")
            if emby_scan_wait(key, timeout=900, label="扫描媒体库"):
                ok("Emby 已扫完")
            else:
                ok("已通知 Emby 扫描（后台进行，稍等片刻刷新 Emby 页面）")
        # 【补时长不在这儿跑】它是整条流程里最慢的一步，而且跟"生成成没成功"
        # 无关。扔后台之后用户扫完就能走人，缺多少时长看体检那行「条目时长」。
        align_library(d, key, heal=False)   # 库选项 + 片名 + 身份 + 脏进度
        auto_libraries_apply(d, key)  # 按关键词规则把该建的库建上
        report_not_in_emby(d, key)
        # 【后台跑】跟「7 更新」那边同一个理由：预热要跨境换直链，慢的时候一部
        # 几十秒，而生成媒体库本身早就做完了。热不热得上跟这次生成成没成功毫无
        # 关系，没道理让用户对着它干等。
        _nodur = len(items_without_duration(key))
        try:
            for _sub in ("warm", "heal"):
                subprocess.Popen(
                    [sys.executable, os.path.realpath(__file__), _sub],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            print(f"  {DIM}已在后台预热线路{RST}"
                  + (f"{DIM}，并给 {RST}{BOLD}{_nodur}{RST}{DIM} 个条目补时长"
                     f"（每 {HEAL_RETRY_MIN} 分钟一轮，没探到的会自动再试）{RST}"
                     if _nodur else f"{DIM}（时长都齐了）{RST}"))
            if _nodur:
                print(f"  {DIM}不用等：补到哪儿了看「6 链路体检」的"
                      f"「条目时长」那一行。{RST}")
        except Exception as e:
            warn(f"后台任务没起来（不影响本次生成）：{_short_err(e)}")
    else:
        warn("没有 Emby API Key，没法自动触发扫描。去 Emby 后台手动扫一次媒体库。")
        print(f"  {DIM}填 API Key：「3 后补参数 → 添加 API 密钥」{RST}")

    print()
    print_library_targets(d, key)
    print(f"  {DIM}Emby → 设置 → 媒体库 → 添加媒体库 → 选内容类型 → 文件夹填上面某一条{RST}")
    print(f"  {YELLOW}内容类型别选错{RST}{DIM}：剧集要选「电视剧」，"
          f"用「电影」类型去刮剧集，每一集会变成一部独立电影。{RST}")
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
# 按番号刮削的 Emby 插件。跟这套网盘直链没有任何关系，纯粹是「Emby 拿到文件之后怎么刮
# 信息」那一层的补充，默认不装。它是【两件东西】，少一件就是"装了但不工作"：
#   · MetaTube Server —— 独立后端，插件自己不抓站，所有请求都转给它
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


METATUBE_FETCHER = "MetaTube"


def _fetcher_names(opts):
    """这个库启用了哪些元数据刮削器，【按优先级】去重返回。

    顺序就是优先级 —— 排前面的先查、查到就赢，后面的只用来填空缺。
    用 set 去重会把这一点丢掉，所以走 dict.fromkeys。
    """
    return list(dict.fromkeys(
        f for t in ((opts or {}).get("TypeOptions") or [])
        for f in (t.get("MetadataFetcherOrder") or t.get("MetadataFetchers") or [])))


def metatube_libraries(key):
    """每个媒体库有没有启用 MetaTube。返回 [(名字, ItemId, 是否启用, LibraryOptions)]。

    Emby 的刮削器是【每个媒体库、每种内容类型】各一份名单，存在
    LibraryOptions.TypeOptions 里。装插件这个动作本身不写这些名单 —— 是 Emby
    在遇到没见过的刮削器时，默认把它当成"启用"。
    """
    out = []
    try:
        libs = _emby("/Library/VirtualFolders", key)
    except Exception:
        return out
    for lb in libs:
        o = lb.get("LibraryOptions") or {}
        on = any(METATUBE_FETCHER in (t.get("MetadataFetchers") or [])
                 or METATUBE_FETCHER in (t.get("ImageFetchers") or [])
                 for t in (o.get("TypeOptions") or []))
        # ContentType 在库这一层，而补 TypeOptions 时要按它定 Type，
        # 所以塞进 LibraryOptions 一起带出去
        o.setdefault("ContentType", lb.get("CollectionType") or "")
        out.append((lb.get("Name") or "?", lb.get("ItemId"), on, o))
    return out


# 自动重刮的条目上限。超过这个数就不自动来了 —— 整库重刮要把每个条目的海报
# 重新下一遍，几百 KB 一张，几万个条目就是几十 GB。这台机器已经因为一次
# 无节制的网盘拉取跑掉过 80 GB（见 HEAL_LIMIT），不能再犯同一个错。
REFRESH_AUTO_MAX = 500


def _lib_item_count(key, iid):
    """这个媒体库里有多少个条目。问不到返回 0（当成小库，允许自动重刮）。"""
    try:
        r = _emby(f"/Items?Recursive=true&ParentId={iid}"
                  f"&IncludeItemTypes=Movie,Episode&Limit=1", key, timeout=20) or {}
        return int(r.get("TotalRecordCount") or 0)
    except Exception:
        return 0


def sync_private_libraries(d, key, rules):
    """标了 private: true 的媒体库，从【非管理员】用户的可见范围里摘掉。返回改了几个用户。

    【Emby 没有"给一个库设密码"这回事】它的模型是按用户分权限：每个用户各自有一份"能看
    哪些库"的名单。所以正确形状是两个账号 —— 你自己的（全都能看）和电视上那个（看不到
    私密库）。密码是账号的密码，不是库的密码。

    【为什么按"是不是管理员"来分】总得有个办法区分"我"和"别人"，而让用户再去某个配置里
    写一遍用户名等于多一处会写错、会过期的东西。管理员本来就能进后台看到一切，对他遮挡
    没有意义 —— 所以规则是【管理员不动，其余一律摘掉】。

    只在【本来就能看见】的时候才发请求；一个非管理员用户都没有的话什么都不做。
    """
    priv = {r["name"] for r in rules if r.get("private")}
    if not priv:
        return 0
    try:
        libs = _emby("/Library/VirtualFolders", key, timeout=20) or []
        users = _emby("/Users", key, timeout=20) or []
    except Exception:
        return 0
    # ItemId 才是策略里认的东西，名字只用来对规则
    hide = {lb.get("ItemId") for lb in libs if (lb.get("Name") or "") in priv}
    allids = {lb.get("ItemId") for lb in libs if lb.get("ItemId")}
    hide = {x for x in hide if x}
    if not hide:
        return 0                      # 规则里标了，但 Emby 里还没这个库
    n, done = 0, []
    for u in users:
        pol = u.get("Policy") or {}
        if pol.get("IsAdministrator"):
            continue                  # 管理员 = "我"，不动
        cur = set(pol.get("EnabledFolders") or [])
        if pol.get("EnableAllFolders"):
            want = allids - hide      # 原来"全都能看" → 换成显式名单
        else:
            want = cur - hide         # 原来就是名单 → 只做减法，别扩权
        if not pol.get("EnableAllFolders") and want == cur:
            continue                  # 本来就看不见，不用发请求
        pol["EnableAllFolders"] = False
        pol["EnabledFolders"] = sorted(x for x in want if x)
        try:
            _emby(f"/Users/{u.get('Id')}/Policy", key, method="POST",
                  body=pol, timeout=30)
            n += 1
            done.append(u.get("Name") or "?")
        except Exception as e:
            warn(f"给用户「{u.get('Name')}」收权限失败：{_emby_err(e)}")
    if done:
        ok(f"私密媒体库已对 {len(done)} 个非管理员账号隐藏："
           f"{'、'.join(sorted(priv))}")
        print(f"  {DIM}受影响的账号：{'、'.join(done)}。管理员账号不受影响 ——"
              f"电视上登非管理员那个就看不到了。{RST}")
    return n


def sync_library_options(d, key, rules):
    """把规则文件里的刮削器/语言设置同步到 Emby。返回改了几个库。

    这才是这份规则文件该有的样子 —— 改一行 yaml、跑一次「5」，Emby 那边跟着变，不用进
    六个页面挨个勾。（实测现场：动漫库的剧集/播出季/集三层、元数据和图像六个列表，一个
    刮削器都没勾上，只有本地 Nfo 是开的。）

    【写了就照办，没写就只修坏的】这条边界很重要：
      · 规则里写了 scrapers/image_scrapers → 以 yaml 为准，覆盖 Emby 里的
      · 没写 → 只在【名单没配好】时才动手（一个刮削器都没有、或只剩 MetaTube），用户
        自己在 Emby 里精简过的名单不碰 —— 只要还留着一个正经刮削器就算数
    不这样分的话，用户在 Emby 界面上的任何调整都会被下一次「5」抹掉。
    """
    by_name = {r["name"]: r for r in rules}
    try:
        libs = _emby("/Library/VirtualFolders", key, timeout=20) or []
    except Exception:
        return 0
    changed, changed_ids, adult_want = [], [], {}
    for lb in libs:
        nm = lb.get("Name") or "?"
        if nm not in by_name:
            continue                     # 不是规则管的库，一律不碰
        if not any(_under(p, STRM_PATH) for p in (lb.get("Locations") or [])):
            continue
        rule = by_name[nm]
        o = lb.get("LibraryOptions") or {}
        tos = o.get("TypeOptions") or []
        fs = sorted({f for t in tos for f in (t.get("MetadataFetchers") or [])})
        # 【"空名单 = 用默认"这个说法要作废，空的就得把默认值明写进去】理由站不住：
        #   · Emby 的库编辑界面是照着 TypeOptions 画勾的，空名单画出来就是【六个列表一个
        #     都没勾】—— 用户看到的就是没配
        #   · "空到底算不算默认"是 Emby 内部行为，版本一变就可能不一样，从外面没法验证
        # 写进去的内容就是 Emby 自己 AvailableOptions 给的 DefaultEnabled，不多不少 ——
        # 不是我们凭空塞一份名单，只是把隐式的变成显式的。
        #
        # 【图像那半边也要算进来】一个库完全可以元数据勾了、图像一个没勾 —— 那正好就是
        # "简介年份都有、就是没有海报"，而这一项恰恰是用户最先看出来的。
        ims = sorted({f for t in tos for f in (t.get("ImageFetchers") or [])})
        broken = (not fs) or fs == [METATUBE_FETCHER] or (not ims)
        explicit = bool(rule.get("scr") or rule.get("img"))
        ct = (lb.get("CollectionType") or "").lower()
        # 【语言是无条件同步的，不能挂在刮削器那个条件下面】原来写的是"既没写 scrapers、
        # 刮削器也没坏 → 直接 continue"，于是没写 scrapers 的库连语言都不会被设：三个库里
        # 只有写了 scrapers 的那个语言是「Chinese」，另外两个全是空的。
        # 语言和刮削器不是一回事：language/country 每条规则都有，是这份 yaml 最基本的字段；
        # 刮削器是可选的、而且要尊重用户的手动调整。两者的写入条件本来就该分开。
        want = _wanted_options(key, ct, rule) if (explicit or broken) else None
        # 【metatube: true 也是明确意图，和 language 一样无条件生效】
        # 名单本来是好的、yaml 里又没写 scrapers 时，want 是 None、整段跳过 ——
        # 于是 AV 库明明标着 metatube: true，MetaTube 却一直没挂上。
        # 这时候拿现有名单当底，只往里补一个 MetaTube，别的一律不动。
        if want is None and rule.get("mt") and metatube_on(d):
            # 【空名单也要处理，这是最常见的情况】通过 API 建出来的库【默认就是空的】，
            # 而带 "and tos" 的条件会把它跳过，于是：
            #
            #     空名单 + metatube: true  =  永远挂不上 MetaTube
            #
            # 空名单时从 Emby 的默认值起一份底，再把 MetaTube 放前面。
            # 【"在名单里"不够，还得在【最前面】】"MetaTube 不在名单里才补"会漏掉"在、但排
            # 在最后"这种情况 —— 而它恰恰最常见（set_metatube_libraries 是往末尾追加的）。
            # 实测现场体检打出来的就是：AV影片 → TheMovieDb、TheTVDB、MetaTube。挂是挂上了，
            # 可 Emby 按顺序查，TheMovieDb 先查到就赢，用户看到的是"配了等于没配"。
            _cur = [f for t in tos
                    for f in (t.get("MetadataFetcherOrder")
                              or t.get("MetadataFetchers") or [])]
            if not _cur or _cur[0] != METATUBE_FETCHER:
                want = (json.loads(json.dumps(tos)) if tos
                        else _wanted_options(key, ct, rule))
        if want and rule.get("mt") and metatube_on(d):
            _put_metatube_first(want)
        if want is not None and not want:
            # 【问不出默认值时宁可不动，但必须出声】_wanted_options 两种内容类型都问不出
            # 名单时返回空。把空的写进去就是真的把这个库的刮削器全关掉，比现状糟得多，
            # 所以这轮只同步语言。但【不能闷声跳过】：上一版默默 want = None，于是同一份
            # yaml、同样两条规则，一个库勾上了、另一个没勾，而脚本从头到尾一个字都没说。
            # 哪个库、什么内容类型、为什么没做，全说出来。
            warn(f"「{nm}」问不到刮削器默认值，这轮只同步语言 —— "
                 f"内容类型 {ct or '(空)'}，规则里写的是 {rule.get('type') or '(没写)'}")
            print(f"  {DIM}Emby 的 /Libraries/AvailableOptions 对这个内容类型"
                  f"没给出可选项，同类型的其它媒体库也没有现成名单可抄。{RST}")
            want = None
        # 语言也一起对齐 —— 它和刮削器是同一件事的两面，分开同步只会让人困惑
        lang, country = rule.get("lang") or "zh", rule.get("country") or "CN"
        # 【图像语言也要设】用户截图里「首选图像下载语言」那一栏三个库全是空的 ——
        # 我只写了元数据语言。空着的后果和元数据语言留空一样：海报按服务器默认
        # 语言挑，中文片子会拿到英文版海报，或者干脆挑不出来。
        # 它跟元数据语言用同一个值，没有分开填的道理。
        img_lang = rule.get("img_lang") or lang
        # 【成人元数据跟着 metatube 走，不另开一个字段】Emby 的「允许成人元数据」默认是
        # 关的，关着时联网搜元数据【不匹配成人标题】。而一个标了 metatube: true 的库就是
        # 成人库 —— 特意挂了按番号刮成人片的插件、却又不许搜成人标题，这两件事是矛盾的。
        # 让用户在 yaml 里再写一遍 adult: true 是多余的。
        adult_k = _pick_opt_key(o, ("EnableAdultMetadata", "AllowAdultMetadata",
                                    "EnableAdultContent"))
        adult_v = bool(rule.get("mt"))
        same = ((want is None or _same_fetchers(o.get("TypeOptions"), want))
                and o.get("PreferredMetadataLanguage") == lang
                and o.get("MetadataCountryCode") == country
                and o.get("PreferredImageLanguage") == img_lang
                and (not adult_k or o.get(adult_k) == adult_v))
        if same:
            continue
        if want is not None:
            o["TypeOptions"] = _merge_type_options(o.get("TypeOptions"), want)
        o["PreferredMetadataLanguage"] = lang
        o["MetadataCountryCode"] = country
        o["PreferredImageLanguage"] = img_lang
        if adult_k:
            o[adult_k] = adult_v
        try:
            _emby("/Library/VirtualFolders/LibraryOptions", key, method="POST",
                  body={"Id": lb.get("ItemId"), "LibraryOptions": o}, timeout=30)
            changed.append(nm)
            # 【区分"只改了语言"和"换了刮削器"】两者要的重刮强度不一样，
            # 见下面 _full 那段。语言变了补一遍缺失字段就够；刮削器换了必须
            # 重新识别，否则等于没换。
            changed_ids.append((lb.get("ItemId"), nm,
                                want is not None
                                and not _same_fetchers(tos, want)))
            if adult_k:
                adult_want[lb.get("ItemId")] = (nm, adult_k, adult_v)
        except Exception as e:
            warn(f"「{nm}」的刮削器设置写不进去：{_short_err(e)}")
    # 【必须回读确认】这个接口对不认识的字段是【静默忽略】的：HTTP 200 只说明请求收到了，
    # 不说明写进去了。不回读的话，脚本理直气壮地打印"4 个媒体库已按规则文件设好"，而用户
    # 点进 Emby 的库编辑页，六个列表还是一个勾都没有 —— 报成功比不报更糟，他会照着这行字
    # 把这个方向排除掉。一次拉回全部库比每个库拉一次省得多。
    if changed:
        try:
            back = {x.get("ItemId"): (x.get("LibraryOptions") or {})
                    for x in (_emby("/Library/VirtualFolders", key, timeout=20) or [])}
        except Exception:
            back = {}
        bad = []
        for iid, nm, _full in list(changed_ids):
            now = back.get(iid)
            if now is None:
                continue                 # 回读本身失败，不当成写失败
            if not _fetcher_names(now):  # 名单还是空的 = 这次写根本没进去
                bad.append(nm)
        # 成人元数据那一项单独核对：它和刮削器名单是两个独立的写入，
        # 名单进去了不代表它也进去了
        for iid, (nm, ak, av) in adult_want.items():
            now = back.get(iid)
            if now is not None and now.get(ak) != av and av:
                warn(f"「{nm}」的「允许成人元数据」没能打开 —— "
                     f"到 Emby 里手动开一下（媒体库设置 → 合集 底下那一项）")
                print(f"  {DIM}关着的话联网搜元数据不匹配成人标题，"
                      f"MetaTube 排第一也可能刮不出来。{RST}")
        if bad:
            for nm in bad:
                changed.remove(nm)
            changed_ids[:] = [x for x in changed_ids if x[1] not in bad]
            warn(f"{len(bad)} 个媒体库的刮削器名单写完回读还是空的："
                 f"{'、'.join(bad)}")
            print(f"  {DIM}Emby 收下了请求但没存 —— 这一项得到界面上手动勾："
                  f"设置 → 媒体库 → 点该库 → 影片 元数据下载器 / 图像获取器。{RST}")
    if changed:
        ok(f"{len(changed)} 个媒体库的语言/刮削器已按规则文件设好（已回读确认）："
           f"{'、'.join(changed)}")
    else:
        print(f"  {DIM}媒体库的语言/刮削器已经和规则文件一致，没有要改的。{RST}")
    # 【这一段原来缩在上面那个 else 里】也就是"一个库都没改"的时候才跑，
    # 而那时 changed_ids 必然是空的 —— 循环一次都不会转。真正改了刮削器的时候
    # 反而不重刮，于是名单写进去了、条目一个都没按新刮削器重新识别过。
    # 外面看到的就是"脚本说设好了，Emby 里还是老样子"。
    for iid, nm, _full in changed_ids:
        n_item = _lib_item_count(key, iid)
        if n_item > REFRESH_AUTO_MAX:
            warn(f"「{nm}」有 {n_item} 个条目，没有自动重刮 —— 挑个时间自己来")
            continue
        # 【换了刮削器就必须 ReplaceAllMetadata=true】否则改了等于没改：false 的含义是"只补
        # 缺失的字段"，而一个已经刮全了的条目什么都不缺 —— Emby 扫一眼发现没什么要补的，
        # 【根本不会重新做识别】，新的刮削器排第几都轮不到它出手。
        # 只改了语言的库仍然用 false：那种情况要的就是"把缺的补上"，没必要把整库的元数据
        # 推倒重来（那是几十 GB 的下载）。Emby 会保留锁定的字段，脚本设的片名不会被冲掉。
        try:
            _emby(f"/Items/{iid}/Refresh?Recursive=true"
                  f"&MetadataRefreshMode=FullRefresh"
                  f"&ImageRefreshMode=FullRefresh"
                  f"&ReplaceAllMetadata={'true' if _full else 'false'}"
                  f"&ReplaceAllImages={'true' if _full else 'false'}",
                  key, method="POST", timeout=30)
            ok(f"「{nm}」已通知"
               + (f"{BOLD}重新识别{RST}（换了刮削器，"
                  f"{n_item} 个条目，后台进行）" if _full
                  else f"重刮（{n_item} 个条目，后台进行）"))
        except Exception:
            pass
    return len(changed)




def _emby_default_fetchers(key, ctype):
    """问 Emby：这个内容类型【默认】该启用哪些刮削器。返回 TypeOptions，问不到返回 []。

    这是 Emby 自己在「添加媒体库」对话框里调的那个接口 —— 它按当前版本、当前装了哪些插件
    给出可选项和默认值，所以拿到的名字一定对得上这台机器，比在代码里硬写一串可靠得多。

    为什么非要它不可：通过 API 建出来的库 TypeOptions 是空的。空名单在 Emby 那边等于"用
    默认"，可一旦我们为了加 MetaTube 往里写一份，含义就变成"只用我列的这些"。
    """
    try:
        r = _emby(f"/Libraries/AvailableOptions?libraryContentType={ctype or ''}"
                  f"&isNewLibrary=true", key, timeout=20) or {}
    except Exception:
        return []
    out = []
    for t in (r.get("TypeOptions") or []):
        def _pick(field):
            # DefaultEnabled 就是 Emby 自己勾好的那几个；一个都没标就全要 ——
            # 全要也比一个都不要强，后者等于这个库没有刮削器
            items = t.get(field) or []
            names = [x.get("Name") for x in items
                     if isinstance(x, dict) and x.get("DefaultEnabled")]
            if not names:
                names = [x.get("Name") for x in items if isinstance(x, dict)]
            return [n for n in names if n]
        md, im = _pick("MetadataFetchers"), _pick("ImageFetchers")
        if not md and not im:
            continue
        out.append({"Type": t.get("Type") or "",
                    "MetadataFetchers": md, "MetadataFetcherOrder": list(md),
                    "ImageFetchers": im, "ImageFetcherOrder": list(im)})
    return out


def _fetcher_map(tos):
    """{内容类型: (元数据刮削器顺序, 图像刮削器顺序)}。只取我们管的那几项。"""
    out = {}
    for t in (tos or []):
        out[t.get("Type") or ""] = (
            list(t.get("MetadataFetcherOrder") or t.get("MetadataFetchers") or []),
            list(t.get("ImageFetcherOrder") or t.get("ImageFetchers") or []))
    return out


def _same_fetchers(cur, want):
    """Emby 存的和我们要的，【在我们管的那部分上】是不是已经一样。

    【绝对不能整份 dict 比】Emby 存的 TypeOptions 每一项带一堆我们不认识、也没打算管的
    字段，而我们构造的只有刮削器那四个键 —— 两边永远不相等，于是每一轮都判成"改了"、
    每一轮都发一次【整库全量重新识别】：机器被啃住（播放一卡一卡），而且
    ReplaceAllMetadata=true 会把脚本刚写进去的季集编号又按刮削器改回去。
    "写进去了却没变"和"突然开始卡"是同一个来源。
    """
    c, w = _fetcher_map(cur), _fetcher_map(want)
    return all(c.get(ty) == v for ty, v in w.items())


def _merge_type_options(cur, want):
    """把要的刮削器名单【并进】Emby 现有的那份，其它字段一个不动。

    整份替换会把 Emby 自己存在里面的其它设置一起抹掉 —— 那些是它的东西，
    我们既不认识也没理由动。
    """
    out = [dict(t) for t in (cur or [])]
    idx = {(t.get("Type") or ""): t for t in out}
    for t in (want or []):
        ty = t.get("Type") or ""
        if ty not in idx:
            out.append(dict(t))
            idx[ty] = out[-1]
            continue
        for k in ("MetadataFetchers", "MetadataFetcherOrder",
                  "ImageFetchers", "ImageFetcherOrder"):
            if t.get(k) is not None:
                idx[ty][k] = list(t[k])
    return out


def desired_type_options(key, ctype, rule=None):
    """这个库【应该】用哪些刮削器。返回 TypeOptions。

    规则里没写 scrapers/image_scrapers 就用 Emby 自己的默认值；写了就按写的来。两者都由
    AvailableOptions 兜底：只保留【这台机器上真实存在】的刮削器名字 —— 用户拼错一个、或者
    写了个没装的插件，不该把整份名单带歪。

    顺序按用户写的来 —— Emby 的名单是有优先级的，排在前面的先用。
    """
    base = _emby_default_fetchers(key, ctype) or _borrow_type_options(key, ctype)
    if not base:
        return []
    want_md = [x.lower() for x in ((rule or {}).get("scr") or [])]
    want_im = [x.lower() for x in ((rule or {}).get("img") or [])]
    avail = _emby_avail_names(key, ctype)
    out = []
    for t in base:
        t = dict(t)
        for names, fk, ok_ in ((want_md, "MetadataFetchers", "MetadataFetcherOrder"),
                               (want_im, "ImageFetchers", "ImageFetcherOrder")):
            if not names:
                continue                 # 没指定 → 保留 Emby 默认那一份
            pool = avail.get((t.get("Type") or "", fk)) or []
            picked = [p for n in names for p in pool if p.lower() == n]
            if not picked:               # 一个都对不上就别动，留默认的
                continue
            # 【本地读取器绝对不能被挤掉，而且要排在最前】用户在 scrapers 里写的是"上哪个
            # 网站刮"，他不会想到这一行还管着"读不读本地的 .nfo"。而这份名单的含义是"只用我
            # 列的这些"—— 没列 Nfo 就等于把它关了，于是脚本写下的季集编号 Emby 一眼都不看。
            # 排最前是因为本地那份是我们【确知正确】的，网站只该拿来填空缺。
            local = [p for p in pool
                     if p not in picked and _is_local_fetcher(p, fk)]
            t[fk] = local + picked
            t[ok_] = list(t[fk])
        out.append(t)
    return out


# 本地元数据/图片读取器：读的是文件旁边的 .nfo、poster.jpg 这些，不联网。
# 【不含 Image Capture / Screen Grabber 那类】那个是从视频里抓一帧当封面，
# 而这套东西的片子都在网盘上，抓一帧等于跨境把视频拉下来 —— 规则文件的
# 注释里专门交代过不要它，这里更不能偷偷替用户加回去。
_LOCAL_MD = ("nfo",)
_LOCAL_IM = ("local", "embedded", "folder")
_NOT_LOCAL_IM = ("capture", "grab", "extract", "screen", "thumb")


def _is_local_fetcher(name, field):
    n = (name or "").lower()
    if field == "MetadataFetchers":
        return any(k in n for k in _LOCAL_MD)
    return (any(k in n for k in _LOCAL_IM)
            and not any(k in n for k in _NOT_LOCAL_IM))


def _wanted_options(key, ctype, rule):
    """这个库该用的刮削器名单。库自己的内容类型问不出来就拿规则里的 type 再问。

    【为什么不能只认库的 CollectionType】它有可能是空的或者对不上：在 Emby 界面上手工建库
    时选了「混合内容」、或者建的时候压根没选，CollectionType 就是空字符串。拿空串去问
    AvailableOptions 什么都问不到，于是这个库永远配不上刮削器 —— 实测就是这么翻的：同一份
    yaml、两条形状完全一样的规则，一个勾上了、一个没勾，差别只在库的 CollectionType。

    规则里的 type 是用户【声明的意图】（movies / tvshows），拿它去问最合适。
    """
    for ct in (ctype, (rule or {}).get("type") or ""):
        if not ct:
            continue
        got = desired_type_options(key, ct, rule)
        if got:
            return got
    return []


def _emby_avail_names(key, ctype):
    """{(Type, 字段): [这台机器上真实可选的刮削器名]}。问不到返回空。"""
    try:
        r = _emby(f"/Libraries/AvailableOptions?libraryContentType={ctype or ''}"
                  f"&isNewLibrary=true", key, timeout=20) or {}
    except Exception:
        return {}
    out = {}
    for t in (r.get("TypeOptions") or []):
        for fk in ("MetadataFetchers", "ImageFetchers"):
            out[(t.get("Type") or "", fk)] = [x.get("Name") for x in (t.get(fk) or [])
                                              if isinstance(x, dict) and x.get("Name")]
    return out


def good_type_options(key, ctype):
    """这个内容类型该用的刮削器名单。问不到就从同类型的其它库抄，都不行返回 []。"""
    return _emby_default_fetchers(key, ctype) or _borrow_type_options(key, ctype)


def _put_metatube_first(tos):
    """把 MetaTube 放到刮削器名单的【最前面】。原地改 tos，返回它。

    【顺序决定谁说了算】Emby 的刮削器名单是有优先级的：排在前面的先查，查到了就用它的
    结果，后面的只用来填空缺。所以把 MetaTube 追加在末尾等于让它永远排在 TheMovieDb 后面
    —— 而 TheMovieDb 对着一个成人片的文件名也会自信地匹配上一部同名的正经片子，一旦它先
    认领，MetaTube 根本没机会插手（实测：一个中文名的文件被认成同名国产剧情片，海报和简介
    全是那部片的）。
    """
    for t in tos:
        for fk, ok_ in (("MetadataFetchers", "MetadataFetcherOrder"),
                        ("ImageFetchers", "ImageFetcherOrder")):
            lst = [x for x in (t.get(fk) or []) if x != METATUBE_FETCHER]
            t[fk] = [METATUBE_FETCHER] + lst
            order = [x for x in (t.get(ok_) or []) if x != METATUBE_FETCHER]
            t[ok_] = [METATUBE_FETCHER] + order
    return tos


def _borrow_type_options(key, ctype):
    """从同内容类型的其它媒体库抄一份刮削器名单。抄不到返回 []。

    新建的库 TypeOptions 是空的 —— Emby 要扫过一次才填。而空名单的含义是"用默认"，一旦
    我们写进去一份，含义就变成"只用这几个"。所以要往里加 MetaTube 时，得先有一份真实的
    名单打底。抄同类型的库是最稳的来源：它是这台 Emby 上真实生效过的配置。
    """
    try:
        libs = _emby("/Library/VirtualFolders", key, timeout=20) or []
    except Exception:
        return []
    for lb in libs:
        if (lb.get("CollectionType") or "").lower() != (ctype or "").lower():
            continue
        tos = ((lb.get("LibraryOptions") or {}).get("TypeOptions") or [])
        if any(t.get("MetadataFetchers") or t.get("ImageFetchers") for t in tos):
            return json.loads(json.dumps(tos))       # 深拷贝，别改到人家的
    return []


def set_metatube_libraries(key, enable_ids):
    """只在 enable_ids 里的媒体库启用 MetaTube，其余一律摘掉。返回改了几个库。

    为什么必须由脚本兜底：MetaTube 是【按番号刮日本成人片】的刮削器，装上之后 Emby 默认把
    它加进每个媒体库的刮削器名单，于是它会去动画库、家庭录像库里乱认（实测一集国产动画被
    配上了 JAV 封面）。用户根本没在那个库勾选过它，也不会想到要去每个库挨个取消。
    """
    n = 0
    for name, iid, on, o in metatube_libraries(key):
        want = iid in enable_ids
        if want == on:
            continue
        tos = o.get("TypeOptions") or []
        # 【刚建出来的库 TypeOptions 是空的】—— Emby 要等第一次扫描才把它填上。
        # 空列表进下面那个 for 循环等于什么都不做，于是 set_metatube_libraries
        # 报"改了 0 个"、MetaTube 一个库都没开上。实测就是这么翻的车：规则里
        # AV影片 标着 metatube: true，库也建出来了，插件却没戴上。
        # 要开的时候自己补一条，按库的内容类型定 Type。
        if want and not tos:
            # 【造名单时绝不能只放 MetaTube】那等于把 TheMovieDb 那些默认刮削器从这个库里
            # 删掉 —— 海报、简介、年份全没了，而用户看到的只是"刮不出图"。空名单在 Emby 那边
            # 是"用默认"，一旦写进去就变成"只用我列的这些"。
            # 所以从【同类型的其它库】抄一份现成的名单，再往里加 MetaTube。抄不到就【不动】
            # —— 宁可 MetaTube 这次没戴上，也不能把整个库的刮削器清空。
            ct = (o.get("ContentType") or "").lower()
            tos = good_type_options(key, ct)
            if not tos:
                warn(f"「{name}」还没有刮削器名单（Emby 要扫过一次才会生成），"
                     f"这次跳过 MetaTube")
                print(f"  {DIM}扫完之后再跑一次「5 生成媒体库」就会戴上；"
                      f"急的话在 Emby 的媒体库设置里手动勾 MetaTube。{RST}")
                continue
        for t in tos:
            for fk, ok_ in (("MetadataFetchers", "MetadataFetcherOrder"),
                            ("ImageFetchers", "ImageFetcherOrder")):
                lst = list(t.get(fk) or [])
                order = list(t.get(ok_) or [])
                if want:
                    if METATUBE_FETCHER not in lst:
                        lst.append(METATUBE_FETCHER)
                    if METATUBE_FETCHER not in order:
                        order.append(METATUBE_FETCHER)
                else:
                    lst = [x for x in lst if x != METATUBE_FETCHER]
                    order = [x for x in order if x != METATUBE_FETCHER]
                t[fk] = lst
                t[ok_] = order
        o["TypeOptions"] = tos
        try:
            _emby("/Library/VirtualFolders/LibraryOptions", key, method="POST",
                  body={"Id": iid, "LibraryOptions": o}, timeout=30)
            n += 1
            print(f"  {DIM}·{RST} {name}：MetaTube "
                  f"{GREEN + '已启用' + RST if want else DIM + '已移除' + RST}")
        except Exception as e:
            warn(f"改「{name}」的刮削器名单失败：{_short_err(e)}")
    return n


def choose_metatube_libraries(key):
    """让用户选 MetaTube 在哪些媒体库生效。

    【这是一次性的初始设置，不是脚本要接管这份名单】刚装好插件的那一刻，名单是 Emby 替用户
    填的（遇到没见过的刮削器默认全部启用），用户从没表过态 —— 一个按番号刮成人片的插件就
    这么进了动画库。在这个时点问一句，是补上那次缺失的选择。

    问完就不再管：之后用户在 Emby 里怎么改都算数，新建的库也照 Emby 自己的默认走。
    """
    libs = metatube_libraries(key)
    if not libs:
        warn("读不到媒体库列表，MetaTube 的适用范围没能设置。")
        print(f"  {DIM}稍后可以在 Emby → 媒体库 → 某个库 → 刮削器里自己勾。{RST}")
        return
    print()
    print(f"  {BOLD}MetaTube 要在哪些媒体库生效？{RST}")
    print(f"  {DIM}它是按番号刮日本成人片的。留在动画库、电影库里会乱认 ——"
          f"实测一集国产动画被配上了 JAV 封面。{RST}")
    print()
    for i, (name, _iid, on, _o) in enumerate(libs, 1):
        print(f"    {i}) {name}   {DIM}当前：{'启用' if on else '未启用'}{RST}")
    print()
    print(f"  {DIM}输入编号，多个用逗号隔开（比如 1,3）；{RST}{BOLD}a{RST}"
          f"{DIM} = 全部启用；直接回车 = 全部不启用。{RST}")
    print(f"  {DIM}这只是这一次的初始设置 —— 之后你在 Emby 的「媒体库 → 刮削器」"
          f"里怎么勾都算数，脚本不会再动它；{RST}")
    print(f"  {DIM}以后新建的媒体库也按 Emby 自己的默认来，不受这里影响。{RST}")
    raw = ask("选哪些").strip().lower()
    if raw == "a":
        picked = {iid for _n, iid, _on, _o in libs}
    else:
        picked = set()
        for x in re.split(r"[,，\s]+", raw):
            if x.isdigit() and 1 <= int(x) <= len(libs):
                picked.add(libs[int(x) - 1][1])
    if set_metatube_libraries(key, picked) == 0:
        print(f"  {DIM}刮削器名单本来就是这样，没有改动。{RST}")


# OpenList 的驱动名 → 中文名。菜单第一列显示的是它，不是挂载路径。
# 挂载路径是给机器看的（/aliyun、/115），一屏全是斜杠开头的短词，扫一眼分不出哪个是哪个盘。
# 挂载路径仍然跟在后面显示：同一种网盘可以挂两个账号（/aliyun1、/aliyun2），光有中文名就
# 分不清了，而下面配路径用的又正是它。
DRIVER_CN = {
    "115 cloud":        "115 网盘",
    "115 open":         "115 网盘（Open）",
    "115share":         "115 分享",
    "aliyundriveopen":  "阿里云盘（Oauth2）",
    "aliyundrive":      "阿里云盘",
    "aliyundriveshare": "阿里云盘分享",
    "quarktv":          "Quark TV",
    "quark":            "夸克网盘",
    "quarkopen":        "夸克网盘（Open）",
    "uctv":             "UC TV",
    "uc":               "UC 网盘",
    "baidunetdisk":     "百度网盘",
    "baiduphoto":       "百度相册",
    "thunder":          "迅雷云盘",
    "thunderbrowser":   "迅雷浏览器",
    "pikpak":           "PikPak",
    "webdav":           "WebDAV",
    "local":            "本地目录",
    "onedrive":         "OneDrive",
    "onedrive app":     "OneDrive（应用）",
    "googledrive":      "Google Drive",
    "s3":               "S3 对象存储",
    "ftp":              "FTP",
    "sftp":             "SFTP",
    "smb":              "SMB 共享",
    "teambition":       "Teambition",
    "lanzou":           "蓝奏云",
    "189cloud":         "天翼云盘",
    "189cloudpc":       "天翼云盘（PC）",
    "mopan":            "移动云盘",
    "chaoxing":         "超星",
    "crypt":            "加密层",
}


def driver_cn(drv):
    """驱动的中文名。没收录的就原样返回驱动名 —— 认不出来也不能显示成空白。"""
    return DRIVER_CN.get(str(drv or "").strip().lower(), str(drv or "?"))


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


# x_storages 这张表上、跟"视频字节走哪条路"有关的列。它们【不在 addition 里】——
# addition 是各驱动自己的字段，这几个是 OpenList 存储表自己的列，每个盘都有。
STORAGE_COLS = ("web_proxy", "webdav_policy", "proxy_range", "down_proxy_url")


def _truthy(v):
    """sqlite 里的布尔值。gorm 存 0/1，但换个版本存 "true" 也不奇怪，都认。"""
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _storage_rows(d):
    """读 OpenList 的存储表：[(id, 挂载点, 驱动, addition 字典, 表列字典)]。

    【为什么要连表上的列一起读】"直链方式"有两个存放位置：驱动自己的开关在 addition 这个
    JSON 里（夸克的 link_method、百度的 download_api……），而"视频字节过不过 VPS"是存储表
    自己的列（web_proxy / webdav_policy），跟驱动无关、每个盘都有。只读 addition 的话后面
    这一类永远看不见 —— 那正是"这个盘只有原画直链一种、什么都不能调"的由来。

    【列名先问再取】OpenList 版本之间这几列会变。select 一个不存在的列是整条语句报错，
    会把整屏功能连坐掉；先 pragma 问一遍，只取真的有的。
    """
    db = os.path.join(d, "openlist", "config", "data.db")
    if not os.path.exists(db):
        return []
    base = ["id", "mount_path", "driver", "addition"]
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        have = {r[1] for r in con.execute("pragma table_info(x_storages)")}
        extra = [c for c in STORAGE_COLS if c in have and c not in base]
        rows = con.execute("select " + ", ".join(base + extra) +
                           " from x_storages order by mount_path").fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            a = json.loads(r[3])
        except Exception:
            a = {}
        out.append((r[0], r[1], r[2], a, dict(zip(extra, r[4:]))))
    return out


# 驱动自己的「直链方式」开关。一行一个字段：
#   (键名, 屏上标题, ((值, 名字, 一句说明), ...))
#
# 【判断标准只有一条：这个键【已经在那个盘的 addition 里】】OpenList 每个驱动的字段各不
# 相同，同一件事在夸克叫 link_method、在百度叫 download_api、PikPak 又是另一个名字。而
# 写一个这个驱动没有的键进去，OpenList 会原样存着然后完全忽略 —— 屏上凭空多一个看着能用
# 的开关，切完什么都没变，那比"没有这个开关"更坏。所以宁可少给，绝不给一个假的。
#
# 【也正因为如此，这张表可以放心地往下加】猜错的行不会显示（没有哪个盘的 addition 里有
# 那个键），代价是零；猜对了就白捡一个开关。以后 OpenList 支持了新驱动、或者哪个驱动新
# 加了字段，来这里加一行就行，别的地方一个字都不用改。
LINK_SWITCHES = (
    ("link_method", "画质",
     (("download",  "原画直链", "画质最好（网盘里是什么就播什么），但码率高；"
                               "跨境线路上 4K 原盘经常拉不动"),
      ("streaming", "转码流",   "网盘自己转码后的流，码率低一个量级，卡的时候选它；"
                               "转码在网盘那边做，不吃本机 CPU"))),
    ("download_api", "取直链的接口",
     (("official",    "官方接口",   "网盘官方的下载接口，最稳；有的账号会被它限速"),
      ("crack",       "非官方接口", "绕开官方那条，速度常常快一截；网盘一改就失效"),
      ("crack_video", "非官方·视频", "同上，取的是视频那条地址"))),
    ("use_transcoding_address", "画质",
     ((False, "原画直链", "发原始文件的下载地址"),
      (True,  "转码流",   "发网盘转码后的地址，码率低、卡的时候选它"))),
)

# 上面哪些是【画质】开关（"播出来是什么"），其余的是【通道】开关（"从哪儿取这条地址"）。
# 分开是为了那一列的措辞：一个盘没有画质开关，它走的就是原画 —— 屏上得写「原画直链」，
# 不能只写「开放平台接口」，那句话没回答"清晰度是什么"。
QUALITY_KEYS = ("link_method", "use_transcoding_address")


# 【这一项每个盘都有】上面那些是某些驱动特有的字段，这个不是 —— 它是存储表自己的列。
# 它管的不是画质，是【视频的字节从哪儿走】：
#   302 直链  播放器直接连网盘。不吃 VPS 带宽 —— 这套东西存在的理由就是这个
#   本机代理  每个字节先到 VPS 再转给播放器，来回两份流量，还更慢
#
# 【那为什么还要留"本机代理"】有的网盘发的直链绑 IP、绑 UA、或者签名只对取它的那台机器
# 有效，播放器自己去连就是 403 —— 这类盘不代理【根本播不了】。这不是理论：WebDAV 源和
# 本地目录这两类驱动在网盘侧压根没有 CDN 直链，OpenList 只能回自己的地址。
# 判断不用猜，跑 tools/dav-check.sh，每个盘现在实际走哪条一目了然。
#
# 两个字段一起写：web_proxy 管网页和 /d/ 那条（Emby 的 302 走这条），webdav_policy 管
# /dav/ 那条（Infuse / VidHub 直连 OpenList 走这条）。只改一个的话，同一个盘在 Emby 里
# 和在播放器里走的是两条不同的路，出了问题根本对不上。
SOURCE_MODES = (
    ("direct", "302 直链",
     "视频从播放器直连网盘，不经过 VPS 带宽（默认；这套东西的意义就在这儿）",
     {"web_proxy": 0, "webdav_policy": "302_redirect"}),
    ("proxy", "本机代理",
     "每个字节先过你的 VPS 再转给播放器：吃双份带宽、也更慢。"
     "但直链绑 IP / 绑 UA 的盘只有这条路能播",
     {"web_proxy": 1, "webdav_policy": "native_proxy"}),
)


def drive_links(d, mp, drv=""):
    """这个盘在「直链方式」这一屏上能调的开关，按屏上顺序。

    每一项：(键名, 放哪儿, 屏上标题, 选项表, 当前值)
      放哪儿  "addition" 驱动自己的字段 / "column" 存储表的列 /
              "source" 回源方式（一次写两列）/ "alipan" 要连令牌一起换
      选项表  ((值, 名字, 一句说明), ...)

    盘不在库里就返回空 —— 空和"没有可调的"是同一种处置，调用方不用分。
    """
    row = next((r for r in _storage_rows(d) if r[1] == mp), None)
    if row is None:
        return []
    _sid, _mp, drv2, add, cols = row
    out = []
    for key, title, opts in LINK_SWITCHES:
        if key not in add:
            continue
        cur = add.get(key)
        if all(cur != v for v, _n, _w in opts):
            # 【收录之外的取值也要显示出来】显示成"未知"等于把人蒙在鼓里，
            # 而且他一旦切走就再也切不回来了
            opts = tuple(opts) + ((cur, f"保持原样（{cur}）",
                                   "这套脚本没收录的取值，原样保留"),)
        out.append((key, "addition", title, tuple(opts), cur))
    if str(drv2 or drv or "").lower() == "aliyundriveopen":
        # 阿里的通道不能在这里直接写：类型和令牌必须一起换，见 _alipan_channel_menu
        out.append(("alipan_type", "alipan", "接口通道",
                    tuple((k, n, w) for k, (n, w, _p) in ALIPAN_TYPES.items()),
                    str(add.get("alipan_type") or "default")))
    if any(k in cols for k in ("web_proxy", "webdav_policy")):
        out.append(("__source__", "source", "回源方式",
                    tuple((k, n, w) for k, n, w, _u in SOURCE_MODES),
                    "proxy" if _truthy(cols.get("web_proxy")) else "direct"))
    return out


def _opt_name(opts, cur):
    """选项表里 cur 对应的显示名。认不出来就把值本身摆出来。"""
    return next((n for v, n, _w in opts if v == cur), str(cur))


# OpenList 每个存储的目录缓存时长（分钟）。默认 30。
#
# 【为什么这个值值得单独摆出来调】实测过一台机器：同一条网盘路径，第一次列目录 12.7 秒，
# 紧接着再列 0.3 秒 —— 差的四十倍就是"走没走真实接口"。而同一时段换直链只要 0.2 秒、一次
# 没失败：坏的只有目录列举这一个接口，夸克对它限流（24 小时 81 次探测失败 20 次）。
#
# 缓存命中的列目录不吃网盘接口，也就不会被限流 —— 这是对着"列目录慢/失败"最直接的一招，
# 比调预热频率对症得多（预热走的是换直链，那条路本来就没问题）。
# 代价：网盘里新增或改名的文件最多要等这么久才被看见，而 AutoFilm 有自己的定时扫描。
DIR_CACHE_PRESETS = ((1, "基本实时　新片点进去就看得见"), (30, "OpenList 默认"),
                     (120, "2 小时"), (720, "12 小时　少打网盘接口"))
# 【这一项脚本【不再】替用户拿主意】曾经自动把所有存储调到 720 分钟，理由是"少打被限流
# 的列目录接口"。那个理由本身没错，但它把代价算漏了：网盘里刚加的片子，OpenList 自己
# 【整整半天看不见】—— 点进去没有、刷新也没有，而用户记忆里这套东西一直是"点进去就能看到
# 新片"的。用户的原话：「实在不行就不要了，让他实时连着」。
#
# 这不是一个"正确答案唯一"的设置，是个取舍，而且两头都很实在：
#   短 → 新片马上看得见，代价是列目录多走真实接口，赶上限流就慢/失败
#   长 → 列目录几乎不碰网盘接口，代价是新片要等缓存过期才看得见
# 取舍归用户，脚本只把两头的代价说清楚。默认跟 OpenList 自己的 30 分钟走。
DIR_CACHE_DEFAULT = 30
# 我曾经自动写下去的那个值。只用来认出"这台机器上的 720 是脚本设的、不是用户设的"，
# 好把它迁回默认 —— 用户自己选的 720 不会被碰（他一选就有 dir_cache_manual）。
DIR_CACHE_WAS_AUTO = 720


def _apply_dir_cache(d, stores, want, quiet=False):
    """把这些存储的目录缓存写成 want 分钟。返回改了几个。

    OpenList 把存储缓存在内存里，改完必须重启才生效；写库前先停容器，
    避免和它自己的写入撞锁。菜单和自动应用共用这一段。
    """
    todo = [(sid, mp, ce) for sid, mp, _drv, ce in stores if ce != want]
    if not todo:
        return 0
    if not quiet:
        info("停止 OpenList...")
    subprocess.run(["docker", "stop", "openlist"], capture_output=True, timeout=120)
    db = os.path.join(d, "openlist", "config", "data.db")
    bak = db + ".bak"
    n = 0
    try:
        shutil.copy2(db, bak)
        con = sqlite3.connect(db)
        for sid, mp, ce in todo:
            con.execute("update x_storages set cache_expiration=? where id=?",
                        (want, sid))
            ok(f"{mp}: 目录缓存 {ce} → {want} 分钟")
            n += 1
        con.commit()
        con.close()
    except Exception as e:
        err(f"写入失败：{e}")
        if os.path.exists(bak):
            shutil.copy2(bak, db)
            warn("已从备份还原。")
        n = 0
    finally:
        subprocess.run(["docker", "start", "openlist"], capture_output=True, timeout=120)
        # 【MediaWarp 必须跟着重启，而且要等 OpenList 先就绪】它只在启动时登录
        # 一次，OpenList 重启后旧令牌作废，换直链会 401 —— 而且只有缓存里没有
        # 的片子才失败，表现是"有的能放有的不能放"，根本联想不到是这一步。
        restart_mediawarp_when_ready(d, quiet=quiet)
    return n


def reload_storages(d, mounts):
    """把这几个挂载点的存储【停用再启用】—— 等于只清掉它们的目录缓存。返回清了几个。

    这是 OpenList 网页上「重新加载存储」那个操作的接口版。比 docker restart openlist
    好在三处：
      · 不重启容器，MediaWarp 手里的 OpenList 令牌照样有效 —— 也就不用跟着重启它，
        更不会再出现"等了 90 秒没等到 → 一整批片子全 404"那种连锁
      · 只动指定的盘。别的盘缓存留着，而缓存命中的列目录根本不碰网盘接口，
        列目录恰恰是被限流的那一个
      · 快：两个 HTTP 请求，不用等容器起来

    接口路径各版本不一定一样，所以任何一步不成就返回 0，让调用方退回重启那条路。
    """
    want = {m for m in mounts if m}
    if not want:
        return 0
    ids = [(sid, mp) for sid, mp, _drv, _min in dir_cache_storages(d) if mp in want]
    if not ids:
        return 0
    pw = read_env(os.path.join(d, ".secrets"), "OPENLIST_PASS",
                  fallback=os.path.join(d, ".env"))
    try:
        tok = (_ol_api("/api/auth/login", {"username": "admin", "password": pw},
                       timeout=15).get("data") or {}).get("token", "")
    except Exception:
        return 0
    if not tok:
        return 0
    n = 0
    for sid, mp in ids:
        try:
            for act in ("disable", "enable"):
                r = _ol_api(f"/api/admin/storage/{act}?id={sid}", {}, tok, timeout=60)
                if r.get("code") != 200:
                    raise RuntimeError(r.get("message", act)[:60])
            n += 1
        except Exception as e:
            warn(f"重新加载 {mp} 失败：{_short_err(e)}")
    return n


def clear_dir_cache(d, mounts=()):
    """把目录缓存清掉，让刚加的片子当场看得见。返回成没成。

    【为什么"生成媒体库"必须先做这一步】缓存没过期之前，OpenList 手里还是旧那份目录，
    AutoFilm 去列目录当然看不见新文件 —— 于是点了「生成媒体库」，跑得好好的，一个新片子
    都没多。缓存设得越长这一步越要紧。

    优先只重新加载【要扫的那几个盘】（见 reload_storages）。整个重启 OpenList 是退路：
    它会把所有盘的缓存一起清掉，还会作废 MediaWarp 的登录令牌、逼它跟着重启。
    """
    info("清一次目录缓存（不然刚加的片子要等缓存过期才看得见）...")
    n = reload_storages(d, mounts)
    if n:
        ok(f"{n} 个网盘的目录缓存已清{DIM}（只重新加载了这几个盘，"
           f"没重启容器，别的盘不受影响）{RST}")
        return True
    subprocess.run(["docker", "restart", "openlist"], capture_output=True, timeout=120)
    if not restart_mediawarp_when_ready(d, quiet=True):
        warn("缓存是清掉了，但 MediaWarp 没重启 —— 见上一行。")
        return False
    ok("目录缓存已清，OpenList 和 MediaWarp 都已重启")
    return True


def dir_cache_auto_apply(d):
    """把【脚本当初自动写下的】720 分钟迁回默认值。返回改了几个。

    只做这一次迁移，做完就记账，以后再不碰这个值 —— 它是个取舍，归用户。
    条件卡得很紧，宁可不做也不能改掉用户自己的选择：
      · 手动设过（dir_cache_manual）→ 不碰
      · 迁移过一次（dir_cache_migrated）→ 不碰
      · 当前值不是 720 → 不碰。720 才是我当初写下去的那个数
    """
    st = ms_state()
    if st.get("dir_cache_manual") or st.get("dir_cache_migrated"):
        return 0
    stores = dir_cache_storages(d)
    auto = [x for x in stores if x[3] == DIR_CACHE_WAS_AUTO]
    if not auto:
        save_ms_state(dir_cache_migrated=True)   # 没什么可迁的，也别年年再看一遍
        return 0
    warn(f"目录缓存从 {DIR_CACHE_WAS_AUTO} 分钟改回 {DIR_CACHE_DEFAULT} 分钟。")
    print(f"  {DIM}{DIR_CACHE_WAS_AUTO} 分钟是早先版本自动设的，为的是少打被限流的"
          f"列目录接口 —— 但代价是网盘里刚加的片子要等半天才看得见，而这一条"
          f"当时没跟你说。{RST}")
    n = _apply_dir_cache(d, auto, DIR_CACHE_DEFAULT)
    save_ms_state(dir_cache_migrated=True)
    if n:
        print(f"  {DIM}想调回长的（列目录老超时的话有用）：3 后补参数 → 6。"
              f"手动设过之后脚本就再也不碰它。{RST}")
    return n


def dir_cache_storages(d):
    """列出各存储的目录缓存时长：(id, 挂载点, 驱动, 分钟)。取不到返回 []。

    cache_expiration 是 x_storages 的一个【列】，不在 addition 里 —— 和
    link_method 不一样，别照着那边写。列名先查 PRAGMA 确认存在再用：
    OpenList 换版本改过表结构的话，宁可这一项不显示，也不要整个菜单打不开。
    """
    db = os.path.join(d, "openlist", "config", "data.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cols = {r[1] for r in con.execute("PRAGMA table_info(x_storages)")}
        if "cache_expiration" not in cols:
            con.close()
            return []
        rows = con.execute("select id, mount_path, driver, cache_expiration "
                           "from x_storages order by mount_path").fetchall()
        con.close()
    except Exception:
        return []
    return [(sid, mp, drv, int(ce or 0)) for sid, mp, drv, ce in rows]


def set_episode_fix():
    """开/关"给剧集 strm 补季集编号"。关掉会把已经改过的名字还原回去。"""
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    cur = ep_fix_setting()
    _rules = lib_rules(d)[0]
    _tv = [r for r in _rules if (r.get("type") or "movies") == "tvshows"]
    _said = [(r["name"], r["epnum"]) for r in _tv if r.get("epnum") is not None]
    print()
    print(f"  当前：{'开' if cur else ('关' if cur is False else '还没问过')}")
    # 【规则文件写了的库不归这个开关管】不说清楚的话，用户在这里关掉、
    # 发现某个库照旧在补，只会以为开关坏了。
    if _said:
        print(f"  {DIM}下面这几个库在规则文件里写死了，不受这个开关影响："
              f"{'、'.join(n + ('=开' if v else '=关') for n, v in _said)}{RST}")
        print(f"  {DIM}这个开关只管没写 episode_number 的库。{RST}")
    print(f"  {DIM}开：Emby 认错集号（或者没认出来）的剧集，在它的 strm 旁边"
          f"放一个只写季集编号的 .nfo。文件名一个字不改，网盘也不动。{RST}")
    print(f"  {DIM}季固定写 1，集号用网盘文件名里那个完整的数 —— Emby 会把"
          f"「231 4K」读成【第 2 季第 31 集】，写死 S01E231 才对得上。{RST}")
    print(f"  {DIM}网盘里本来就按季分了目录的话，关掉。{RST}")
    print(f"  {DIM}关：不再补，并且把【脚本写过的 .nfo 删掉】、"
          f"被旧版改过名的 strm 也改回网盘原名。{RST}")
    print(f"  {DIM}不想每台机器都来点一次：在 library-rules.yaml 的剧集库上写一行"
          f" episode_number: true，那个库就永远按它办，跟着规则文件走。{RST}")
    print()
    c = ask("1 开 / 2 关（回车不改）").strip()
    want = {"1": True, "2": False}.get(c)
    if want is None:
        print("没有改动。")
        return
    save_ms_state(ep_fix=want, ep_fix_v=EP_FIX_V)
    ok(f"已改成：{'开' if want else '关'}")
    # 【开关一拨就当场做完，别让人再去点「4」】上一版只存了个设置就回菜单，
    # 改名要等下一次「5 生成媒体库」或者下一轮定时任务。用户开完立刻去 Emby 看，
    # 看到的还是旧名字，只会以为开关没用 —— 实测就是这么被问回来的。
    # 「7 片名用哪个」那边早就是当场套用的，这里照它办。
    key = (read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"),
                            "auth") if is_installed(d) else "")
    # 【开也好关也好，都走同一条路】关掉不能一把梭 drop_episode_nfo(d)：那会把
    # 规则文件里写了 episode_number: true 的库也一起清了。fix_ 里按库分得清清楚楚。
    if not key:
        warn("没有 Emby API Key，这次改动要等下次点「5 生成媒体库」才落地。")
        return
    try:
        n, _dup = fix_episode_strm_names(d, _rules, key, interactive=False)
    except Exception as e:
        warn(f"处理失败：{_short_err(e)}")
        return
    if want and not n:
        print(f"  {DIM}没有需要补的 —— Emby 认的集号和网盘文件名都对得上。{RST}")
        return
    # 【必须让 Emby 重扫】nfo 是扫描的时候才读的，不重扫集号还是旧的
    try:
        _emby("/Library/Refresh", key, method="POST", timeout=60)
        ok("已通知 Emby 重扫，集号过一会儿就更新")
        print(f"  {DIM}扫描在后台跑，片子多的话要等几分钟。{RST}")
    except Exception as e:
        warn(f"通知 Emby 扫描失败：{_short_err(e)}")
        print(f"  {DIM}点一次「5 生成媒体库」也会扫。{RST}")


def set_dir_cache():
    """改各存储的目录缓存时长。见 DIR_CACHE_PRESETS。"""
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    stores = dir_cache_storages(d)
    if not stores:
        warn("读不到存储的缓存设置。")
        print(f"  {DIM}还没在 OpenList 里添加网盘，或者这个版本的表结构变了 ——"
              f"可以去 OpenList → 存储 → 编辑，那一项叫「缓存过期时间」。{RST}")
        return
    print()
    for _sid, mp, drv, ce in stores:
        print(f"  {BOLD}{mp}{RST}  {DIM}({drv}){RST}   当前："
              f"{CYAN}{BOLD}{ce} 分钟{RST}")
    print()
    print(f"  {DIM}缓存命中的列目录不走网盘接口（实测 0.3 秒 vs 12.7 秒），"
          f"也就不会被限流。{RST}")
    print(f"  {DIM}代价：网盘里新增/改名的文件最多等这么久才被看见 —— "
          f"改完目录去 OpenList 把存储停用再启用就立刻清掉。{RST}")
    print()
    for i, (m, why) in enumerate(DIR_CACHE_PRESETS, 1):
        print(f"  {i}. {BOLD}{m} 分钟{RST} {DIM}（{why}）{RST}")
    print()
    c = ask("选一个，或直接输分钟数（回车取消）").strip()
    if not c:
        print("没有改动。")
        return
    if c.isdigit() and 1 <= int(c) <= len(DIR_CACHE_PRESETS):
        want = DIR_CACHE_PRESETS[int(c) - 1][0]
    elif c.isdigit():
        want = int(c)
    else:
        print("没有改动。")
        return
    if want <= 0:
        warn("得是个正数。没有改动。")
        return
    if want < DIR_CACHE_PRESETS[0][0]:
        # 【序号和分钟数在同一个输入框里，会撞】上面 1-4 当序号，别的当分钟数。
        # 用户想选第 5 项（没有第 5 项）敲个 5，落下来就是"5 分钟" —— 比默认的
        # 30 还短，正好和他要的相反。比默认短的值一律先问一句。
        if not ask_yn(f"{want} 分钟比 OpenList 默认的 "
                      f"{DIR_CACHE_PRESETS[0][0]} 分钟还短，确定？"
                      f"（想选第 N 项的话，只有 1-{len(DIR_CACHE_PRESETS)} 项）",
                      False):
            print("没有改动。")
            return
    # 【记下"用户手动设过"】设过之后自动应用就不再碰它 —— 他明确表达过的
    # 选择，脚本不能在下一次更新时悄悄改回去。
    save_ms_state(dir_cache_manual=True)
    if not _apply_dir_cache(d, stores, want):
        print(f"  {DIM}没有需要改的 —— 每个存储都已经是 {want} 分钟。{RST}")
    print()
    print(f"  {DIM}观察一天，再看「6 链路体检」里「列目录历史」那张探测图，"
          f"X（失败）应该变少。{RST}")


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


# ============================================================================ 115 扫码
# OpenList 的「115 网盘」驱动要一个【已经拿到的】二维码令牌，它自己不生成二维码 —— 源码
# drivers/115/util.go 里是 if d.QRCodeToken != "" 直接拿去兑换，没有任何生成流程。于是用户
# 在界面上只看到一个空输入框和一句「需要二维码令牌和 Cookie 其中之一」。取 Cookie 又要开发
# 者工具，手机上做不了。所以这里把 115 的扫码流程做成按钮，接口地址来自 SheltonZhu/115driver。
QR115_TOKEN  = "https://qrcodeapi.115.com/api/1.0/web/1.0/token"
QR115_IMAGE  = "https://qrcodeapi.115.com/api/1.0/mac/1.0/qrcode?uid={}"
QR115_STATUS = "https://qrcodeapi.115.com/get/status/?uid={}&time={}&sign={}&_={}"


def _qr115_get(url, timeout=35):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _qr115_show(uid, at=0):
    """把令牌、二维码地址、OpenList 该怎么填，一次性摊开。

    这些要【在等待扫码之前】就显示出来:用户是拿着手机在操作,等的过程中正好
    可以先把 OpenList 那几栏填好,确认完直接点保存。藏到成功之后才给,等于逼人干等。
    """
    print()
    age = ""
    if at:
        m = (time.time() - at) / 60
        age = (f"{m:.0f} 分钟前" if m < 60 else
               f"{m / 60:.0f} 小时前" if m < 1440 else f"{m / 1440:.0f} 天前")
        age = f"   {DIM}（{age}生成）{RST}"
    print(f"  {BOLD}当前令牌{RST}   {GREEN}{BOLD}{uid}{RST}{age}")
    print(f"  {BOLD}二维码{RST}     {CYAN}{QR115_IMAGE.format(uid)}{RST}")
    print()
    print(f"  {DIM}OpenList → 管理 → 存储 → 添加 → 驱动选「115 网盘」，然后：{RST}")
    print(f"     Cookie       {DIM}留空{RST}")
    print(f"     二维码令牌   {GREEN}上面那串{RST}")
    print(f"     二维码源     {GREEN}{BOLD}网页{RST}"
          f"   {DIM}← 必须是网页，选安卓/TV 会报「系统已下架」{RST}")
    print(f"     根文件夹ID   {GREEN}{BOLD}0{RST}")
    print(f"     挂载路径     {GREEN}{BOLD}/115{RST}   {DIM}（自己定，别和已有的重名）{RST}")


def _qr115_new():
    """申请一个新二维码，并等扫码确认。"""
    info("正在向 115 申请二维码...")
    try:
        d = (_qr115_get(QR115_TOKEN).get("data") or {})
        uid, tm, sign = d.get("uid"), d.get("time"), d.get("sign")
    except Exception as e:
        err(f"申请失败：{_short_err(e)}")
        return
    if not uid:
        err("115 没有返回二维码信息，稍后再试。")
        return
    # time/sign 也存下来：下次进菜单要拿它们回查这个令牌还有没有效
    save_ms_state(qr115_uid=uid, qr115_tm=tm, qr115_sign=sign,
                  qr115_at=int(time.time()))

    _qr115_show(uid)
    print()
    print(f"  {BOLD}怎么扫{RST}：手机浏览器打开上面那个二维码地址 → 长按存图 →")
    print(f"          115 App → 扫一扫 → {BOLD}从相册{RST}选那张图 → 确认登录")
    print(f"  {DIM}（二维码和手机是同一台设备，扫不了自己的屏幕，只能走相册）{RST}")
    print()
    info("等你扫码确认，最多 5 分钟。Ctrl-C 可以中断，令牌已经存下来了。")

    state, last_err = None, ""
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            state = (_qr115_get(QR115_STATUS.format(
                uid, tm, sign, int(time.time() * 1000))).get("data") or {}).get("status")
            last_err = ""
        except Exception as e:
            # 以前这里把异常吞掉只显示「状态 None」，用户完全看不出发生了什么。
            # 状态接口是长轮询，超时是常态，照实说出来比装作没事强。
            state, last_err = None, _short_err(e)
        if state in (2, -1, -2):
            break
        tip = {0: "等待扫码…", 1: "已扫到，请在手机上点「确认登录」…"}.get(state)
        if tip is None:
            tip = f"重试中（{last_err[:24]}）" if last_err else f"状态 {state}"
        left = int(deadline - time.time())
        print(f"\r    {DIM}{pad(tip, 40)}还剩 {left // 60}:{left % 60:02d}{RST}",
              end="", flush=True)
        time.sleep(2)
    print("\r\x1b[2K", end="")

    if state == 2:
        ok("扫码确认成功")
        print(f"  {YELLOW}现在就去 OpenList 保存{RST}{DIM} —— 这个会话是一次性的，"
              f"保存那一下才真正去兑换。{RST}")
    elif state == -1:
        warn("二维码过期了，重新制作一个。")
    elif state == -2:
        warn("你在手机上取消了。")
    else:
        warn("没等到确认。令牌还在，扫完码可以直接去 OpenList 保存试试。")
        if last_err:
            print(f"  {DIM}最后一次查询状态失败：{last_err}{RST}")


def qr115_status(uid, tm, sign):
    """回查一个令牌现在还有没有效。返回 (状态码, 人话)。

    为什么要查：扫码会话是【分钟级】的，115 的状态接口专门有个 -1 已过期。而菜单只显示
    "几分钟前生成"，两天前那个和刚生成的看起来一模一样 —— 猜错的代价是拿一串废令牌去填，
    然后对着 OpenList 那句完全不相干的报错发懵。

    状态接口是长轮询：没变化时它会挂住，所以给 8 秒就够 —— 超时本身就说明状态没变化。
    """
    try:
        st = (_qr115_get(QR115_STATUS.format(uid, tm, sign, int(time.time() * 1000)),
                         timeout=8).get("data") or {}).get("status")
    except Exception:
        return None, "还没扫码"          # 长轮询超时 = 状态没变 = 仍在等扫码
    return st, {2: "已确认，可以拿去填",
                1: "扫了但还没在手机上点确认",
                0: "还没扫码",
                -1: "已过期，要重新制作",
                -2: "手机上取消过了"}.get(st, f"状态 {st}")


def qr115_login():
    """115 扫码登录：先摊开当前令牌，要不要重做由用户决定。

    进来就自动申请是错的 —— 用户可能只是想回来看一眼上次那串令牌是什么
    （比如 OpenList 那边填错了要重填），结果反而把旧的作废了。
    """
    while True:
        st = ms_state()
        uid, at = st.get("qr115_uid") or "", st.get("qr115_at") or 0
        print()
        print("-" * 60)
        print(f"  {BOLD}115 网盘扫码登录{RST}")
        print("-" * 60)
        print(f"  {DIM}OpenList 的「115 网盘」驱动要一个「二维码令牌」，但它自己不生成")
        print(f"  二维码，界面上也没说这串东西从哪来。这个按钮替你走完 115 的扫码流程。{RST}")
        if uid:
            _qr115_show(uid, at)
            print()
            print(f"    {DIM}正在确认这串还有没有效...{RST}", end="", flush=True)
            st_code, st_txt = qr115_status(uid, st.get("qr115_tm"), st.get("qr115_sign"))
            col = {2: GREEN, -1: RED, -2: RED}.get(st_code, YELLOW)
            print(f"\r\x1b[2K  {BOLD}状态{RST}       {col}{st_txt}{RST}")
            if st_code == 2:
                print(f"  {DIM}直接拿去填 OpenList 就行。{RST}")
            elif st_code in (-1, -2):
                print(f"  {DIM}这串已经没用了，选 1 重新制作。{RST}")
            else:
                print(f"  {DIM}扫码会话是分钟级的，隔一段时间没扫就会失效 —— "
                      f"拿不准就选 1 重做一个，很快。{RST}")
        else:
            print()
            print(f"  {DIM}还没生成过令牌。{RST}")
        print()
        # 一个按钮按当前状态走两条路:没有就直接做,有了先问 —— 刷新会让旧令牌作废,
        # 而用户很可能只是回来看一眼那串东西,不该顺手把它废掉
        print(f"  1. 制作二维码" + (f"{DIM}（已有一个，会先问要不要刷新）{RST}" if uid else ""))
        print(f"  0. 返回")
        print("-" * 60)
        c = ask("请选择").strip()
        if c in ("0", ""):
            return
        if c != "1":
            print("无效选择。")
            continue
        if uid:
            print()
            warn("已经有一个令牌了，重新制作会让上面那串立刻作废。")
            print(f"  {DIM}OpenList 里如果已经用它挂好了存储，那个存储不受影响"
                  f"（它早换成 cookie 了）。{RST}")
            if not ask_yn("确定要重新制作吗？", False):
                continue
        _qr115_new()
        ask("\n按回车继续...")


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
        write_atomic(os.path.join(d, "docker-compose.yml"), gen_compose(cfg))
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
    write_atomic(os.path.join(d, "docker-compose.yml"), gen_compose(cfg))
    info("启动 MetaTube 服务端...")
    if not _compose_up(d):
        err("容器启动失败，看上面的报错。")
        return
    subprocess.run(["docker", "restart", "emby"], capture_output=True)
    save_ms_state(metatube_files=files)
    ok("装好了")

    # 装完【必须】立刻圈定适用范围。Emby 遇到没见过的刮削器默认当成启用，于是
    # 一个按番号刮成人片的插件会自动对所有媒体库生效 —— 用户没在那些库勾过它，
    # 也不会想到要去每个库里挨个取消。这个默认值不该由用户来擦屁股。
    key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"),
                           "auth")
    if key:
        time.sleep(8)          # 等 Emby 重启完，不然媒体库列表读不到
        choose_metatube_libraries(key)
    else:
        warn("没有 Emby API Key，没法自动圈定 MetaTube 的适用范围。")
        print(f"  {DIM}请手动去每个不需要它的媒体库里取消勾选 MetaTube，"
              f"否则它会去动画库、电影库里乱认。{RST}")

    print()
    print(f"  {YELLOW}还有一步要你在 Emby 里手动做{RST}{DIM}（脚本代劳不了，"
          f"那是插件自己的设置页）：{RST}")
    print(f"  {DIM}·{RST} Emby → 设置 → 插件 → MetaTube → 服务器地址填 "
          f"{CYAN}{BOLD}http://metatube:{METATUBE_PORT}{RST}")
    print(f"  {DIM}插件没出现的话，Emby 可能还没加载完，等一会儿刷新设置页。{RST}")




def _write_addition(d, targets, updates, quiet_keys=()):
    """把 updates 合进这些存储的 addition。薄封装，见 _write_storage。"""
    _write_storage(d, targets, addition=updates, quiet_keys=quiet_keys)


def _write_storage(d, targets, addition=None, columns=None, quiet_keys=()):
    """改这些存储，然后重启 OpenList 和 MediaWarp。

    targets 是 [(存储 id, 挂载点)]。这段（停容器、备份、写、还原、重启顺序）每一步都是踩
    出来的，所有要动存储的入口都走这里，不能各抄一份。

    addition 合进 addition 那个 JSON（驱动自己的字段）；columns 直接写表上的列
    （web_proxy / webdav_policy 这类每个盘都有的开关）。两者可以一起给。

    【列要先问再写】表上没有的列直接 update 是整条语句报错 —— 而那时候容器已经停了、
    库已经动过一半，比一开始就不写危险得多。写之前 pragma 问一遍，只写真的有的。

    quiet_keys 里的键【新旧两个值都不打】，只报"已更新"—— 旧值也是一串还在有效期内的
    刷新令牌，而这一屏正是用户会截图发出来的。换令牌这件事，前后两个都是秘密。
    """
    addition = dict(addition or {})
    columns = dict(columns or {})
    if not addition and not columns:
        return
    # OpenList 把存储缓存在内存里，改完必须重启才生效；写库前先停，避免锁冲突
    info("停止 OpenList...")
    subprocess.run(["docker", "stop", "openlist"], capture_output=True, timeout=120)
    db = os.path.join(d, "openlist", "config", "data.db")
    bak = db + ".bak"
    mw_ok = False
    try:
        shutil.copy2(db, bak)
        con = sqlite3.connect(db)
        have = {r[1] for r in con.execute("pragma table_info(x_storages)")}
        cols = {k: v for k, v in columns.items() if k in have}
        for k in columns:
            if k not in have:
                print(f"  {DIM}这个 OpenList 版本的存储表没有 {k} 这一列，跳过{RST}")
        for sid, mp in targets:
            row = con.execute("select addition from x_storages where id=?", (sid,)).fetchone()
            a = json.loads(row[0])
            shown = []
            for k, v in addition.items():
                if k in quiet_keys:
                    shown.append(f"{k}: {'（原来的）' if a.get(k) else '空'} → （已更新）")
                else:
                    shown.append(f"{k}: {a.get(k) if a.get(k) not in (None, '') else '空'} → {v}")
                a[k] = v
            sets, vals = [], []
            if addition:
                sets.append("addition=?")
                vals.append(json.dumps(a, ensure_ascii=False))
            for k, v in cols.items():
                sets.append(f"{k}=?")
                vals.append(v)
                shown.append(f"{k} → {v}")
            con.execute(f"update x_storages set {', '.join(sets)} where id=?",
                        (*vals, sid))
            ok(f"{mp}  " + "　".join(shown))
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
        # 【MediaWarp 必须跟着重启】理由见 restart_mediawarp_when_ready()：它只在启动那一刻
        # 登录 OpenList，OpenList 一重启旧令牌就作废，换直链会 401、整个请求以 404 收场。
        # 而且【不会立刻暴露】—— 已经缓存了直链的片子照样能播，只有缓存里没有的才失败，
        # 用户看到的是"有的能放有的不能放"，完全联想不到是刚才那次切换造成的。
        mw_ok = restart_mediawarp_when_ready(d)

    print()
    print(f"  {DIM}strm 文件不用重新生成 —— 里面存的是网盘路径，{RST}")
    print(f"  {DIM}清晰度是播放那一刻才决定的。{RST}")
    # 刚重启完 MediaWarp，缓存是空的：这时候用户去点播放，每部片子都要等一次
    # 跨境换直链（实测 0.3～27 秒），转码流还要等网盘准备切片 —— 表现就是一直转圈。
    # 趁这里替他热一遍，切完就能直接看
    key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"),
                           "auth")
    if not mw_ok:
        # 【MediaWarp 没重启成就别热】它手里是废令牌，这十几部一定【全部】404 ——
        # 白等十分钟、白打一轮网盘接口，末尾还会打出"网盘接口多半在抖"，
        # 把人往完全错的方向带。实测就是这么发生的。
        print(f"  {DIM}这次不预热了：MediaWarp 还握着旧令牌，现在热必定一部都热不上。{RST}")
        print(f"  {DIM}按上面那条命令重启完，下一轮（{WARM_EVERY_H} 小时内）会自动补热；"
              f"想马上热就再进一次这个菜单。{RST}")
    elif key:
        # 【放后台】预热要一部一部跨境换直链，慢的时候整轮要几分钟。通道已经切完、
        # 配置已经落盘，热不热跟这次切换成没成功毫无关系 —— 没道理让人对着它干等。
        # 更新那条路早就是后台跑的，这里跟上。
        try:
            subprocess.Popen(
                [sys.executable, os.path.realpath(__file__), "warm"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            print(f"  {DIM}已在后台给「继续观看」+ 最近新加的片子预热线路"
                  f"（换直链，最多几分钟）—— 不用等它，直接回车就行。{RST}")
        except Exception as e:
            warn(f"后台预热没起来（不影响切换）：{_short_err(e)}")
    else:
        print(f"  {DIM}没有 Emby API Key，没法提前接线路 —— "
              f"第一次播放会等一会儿换直链。{RST}")




def _ol_subdirs(path, token=None):
    """列出这个路径下的子目录名。返回 (名字列表, 没列成的原因)。

    给「挂载路径」那一屏挑目录用 —— 在 ssh 里手打 /quark/夸克挂载/电影 又长又
    容易错，列出来点编号才是能用的交互。

    【必须三态，不能两态】"没有子目录"有两个截然不同的原因，处置也相反：
      · 这一层底下真的只有文件  → 正常，按 . 选中它就行
      · 列目录根本没通          → 那才是故障，而且往往就是要查的那件事
    上一版两种都返回 []，屏上只有一行光秃秃的提示语 —— 用户看到的是"这个盘什么都没有"，
    而实际上是【没登录】：这里调 fs/list 一直没带令牌，OpenList 的游客默认是关的，
    它回的是 Guest user is disabled。整整一屏都是这么来的，还一个字都不解释。

    【没给令牌就自己去登】列目录这条接口认令牌，而这一屏的调用方手里未必有。
    """
    if token is None:
        token = _ol_token(ms_install_dir())
    try:
        r = _ol_api("/api/fs/list", {"path": path, "password": "", "page": 1,
                                     "per_page": 0, "refresh": False},
                    token, timeout=60)
    except Exception as e:
        return [], _short_err(e)
    if r.get("code") != 200:
        return [], (r.get("message") or f"OpenList 回了 {r.get('code')}")
    return sorted(x.get("name") for x in ((r.get("data") or {}).get("content") or [])
                  if x.get("is_dir") and x.get("name")), ""


def _apply_scan_paths(d, why=""):
    """把当前设置（各盘的路径 + 剩余自动）落到 AutoFilm 配置上并重启它。

    每次改完立刻应用，不再攒一批最后统一保存 —— 一屏里能改的东西多了之后，
    "改了但没保存"就成了新的坑：退出去以为设好了，其实一个字没写。
    """
    cfg = rebuild_cfg_from_disk(d)
    paths = cfg["scan_paths"]
    af = os.path.join(d, "autofilm", "config", "config.yaml")
    with open(af, "w", encoding="utf-8") as f:
        f.write(gen_autofilm_conf(cfg))
    subprocess.run(["docker", "restart", "autofilm"], capture_output=True)
    ok(f"{why}已应用：现在扫 {len(paths)} 个目录，AutoFilm 已重启")
    drop_orphan_strm_dirs(d, paths, [])
    return paths


def _pick_dirs(d, mp):
    """在这个盘里挑扫描路径。返回挑中的路径（空 = 取消）。

    【一个盘挂多少条路径都行】上层是 scan_spec 那个列表，加进去就是追加一条，删也是按条
    删 —— 一直都支持。真正卡住的是【挑不到】：上一版只列挂载点【下面一层】，而按字母分类
    的盘（/网盘/mov/电影/A、/B、/C……）想扫到字母那一层只能选「m 手打」把整条路径敲
    进去 —— 在手机 ssh 上手打长中文路径，正是当初做这个菜单要避免的事。
    改成能一层一层往下走。

    【单个编号 = 进去看看，多个编号 = 就选它们】这条规矩不用解释也猜得到，而且两件事都
    做得到：想选中你正站着的这一层，按 . 就行。全要就按 *（十几个字母目录一个个点太蠢）。

    【直接贴一条路径也认】实测有人在"要扫哪个"这里直接把整条路径打进去了 —— 那是完全合理
    的反应，可上一版只回一句"没认出有效的编号"。斜杠开头的一律当路径收。
    """
    root = mp.rstrip("/")
    cur = root
    tok = _ol_token(d)
    while True:
        print(f"\n  {DIM}正在列 {cur} 下面的目录...{RST}")
        subs, err = _ol_subdirs(cur, tok)
        print(f"  {BOLD}{cur}{RST}")
        for j, name in enumerate(subs, 1):
            print(f"    {j:>2}. {name}")
        if err:
            # 【列不动是故障，不能和"底下没目录"长一样】前者要去查，后者按 . 选中就完事
            print(f"    {YELLOW}列不出来：{err}{RST}")
            print(f"    {DIM}还是可以直接把路径贴进来（下面那行）{RST}")
        elif not subs:
            print(f"    {DIM}（这一层底下没有目录了）{RST}")
        # 【不用字母键】上一版是 a 整个盘 / m 手打路径 —— 字母是脚本自己发明的暗号，
        # 而编号、. 、* 、.. 这几个在文件路径里本来就是这个意思，不用记。
        tips = ["编号 进这一层", "多个编号（逗号隔开）一次选好几个",
                ". 就选整个盘" if cur == root else ". 就选这一层"]
        if subs:
            tips.append("* 这一层全部")
        if cur != root:
            tips.append(".. 上一层")
        tips += ["或者直接把路径贴进来", "回车取消"]
        print(f"  {DIM}{'　'.join(tips)}{RST}")
        pick = ask("要扫哪个").strip()
        if not pick:
            return []
        if pick == "..":
            cur = cur.rsplit("/", 1)[0]
            if len(cur) < len(root):
                cur = root
            continue
        if pick == ".":
            return [cur]
        if pick == "*":
            if not subs:
                print("这一层底下没有目录。")
                continue
            return [f"{cur}/{n}" for n in subs]
        if pick.startswith("/"):
            raw = pick.rstrip("/")
            if not raw:
                return []
            if not raw.startswith(root + "/") and raw != root:
                warn(f"这条不在 {mp} 底下，没有加。")
                continue
            return [raw]
        toks = [t.strip() for t in pick.replace("，", ",").split(",") if t.strip()]
        nums = [int(t) for t in toks
                if t.isdigit() and subs and 1 <= int(t) <= len(subs)]
        # 【认不出来就整条不算】一串编号里有一个越界就照样加剩下的，等于悄悄少加一条，
        # 而屏上写的是"已应用"—— 要么全对要么重来
        if not nums or len(nums) != len(toks):
            print("没认出有效的编号。")
            continue
        if len(nums) == 1:
            cur = f"{cur}/{subs[nums[0] - 1]}"      # 单个 = 往下走一层
            continue
        return [f"{cur}/{subs[n - 1]}" for n in nums]


def _drive_paths_menu(d, mp):
    """一个盘的「路径」子菜单：加 / 换 / 删。一个盘想挂几条挂几条。"""
    while True:
        exp = explicit_scan_paths()
        mine = _paths_under(exp, mp)
        print("\n" + "-" * 60)
        print(f"  {BOLD}{mp}{RST} 的扫描路径")
        print("-" * 60)
        if mine:
            for i, p in enumerate(mine, 1):
                tag = f"  {DIM}整个盘{RST}" if p.rstrip("/") == mp.rstrip("/") else ""
                print(f"  {i:>2}. {p}{tag}")
        else:
            print(f"  {DIM}{'整个盘（自动）' if auto_rest_on() else '未加路径'}{RST}")
        print("-" * 60)
        # 【和别的每一屏一样，编号，不用字母】原来是 a 添加 / d 删除，整套菜单里只有这一屏
        # 是字母键 —— 别的屏全是 1/2/0。同一套东西两种规矩，每次进来都得先认一遍。
        print("  1. 添加")
        print("  2. 删除")
        print("  0. 返回")
        print("-" * 60)
        c = ask("请选择").strip().lower()
        if c in ("0", "", "q"):
            return
        if c in ("2", "d"):
            if not mine:
                print("没有可删的。")
                continue
            # 挂了十几条（按字母分类的那种）时，一条一条删要重进十几次这一屏
            t = ask("删第几条？（逗号隔开多条，* 全删，回车取消）").strip()
            if t == "*":
                gone = list(mine)
            else:
                toks = [x.strip() for x in t.replace("，", ",").split(",") if x.strip()]
                nums = [int(x) for x in toks
                        if x.isdigit() and 1 <= int(x) <= len(mine)]
                if not nums or len(nums) != len(toks):
                    print("没有改动。")
                    continue
                gone = [mine[n - 1] for n in sorted(set(nums))]
            save_ms_state(scan_spec=[p for p in exp if p not in gone])
            _apply_scan_paths(d, f"删掉 {'、'.join(gone)}，")
            continue
        if c not in ("1", "a"):
            print("无效选择。")
            continue

        got = [p for p in _pick_dirs(d, mp) if p]
        if not got:
            continue
        bare = [p for p in got if p.strip("/").count("/") == 0]
        if bare:
            # 扫整个盘是这套东西里最容易踩、又最像"成功了"的坑：手机备份、截图
            # 全变成条目，刮削搜不到，表现是"一堆条目全都没有海报"。详见 README。
            warn(f"整个盘会把手机备份、截图也扫进来")
            if not ask_yn("仍然要整个盘？", False):
                continue
        # 【有最大的就扫最大的】加了父目录，它底下那些子路径就该消失 —— 留着既没用又骗人：
        # 屏上写着"只扫电影"，实际扫的是整个盘。反过来，整个盘已经在扫了还想加它底下的
        # 一条，那条同样是白加 —— 这时候要说清楚"没加"，不能报一句"已应用"了事。
        merged = merge_scan_paths(exp + got)
        eaten = [p for p in exp if p not in merged]
        added = [p for p in merged if p not in exp]
        if not added:
            print(f"  {DIM}{'、'.join(got)} 已经在正在扫的路径底下了 ——"
                  f" 父目录扫的时候本来就包含它，没有加。{RST}")
            continue
        save_ms_state(scan_spec=merged)
        if eaten:
            print(f"  {DIM}去掉 {'、'.join(eaten)}：它在刚加的路径底下，"
                  f"留着也是扫同一批文件{RST}")
        _apply_scan_paths(d, f"加了 {'、'.join(added)}，")




def _title_menu(d, mp=None):
    """片名用哪个。mp=None 时改的是【默认值】（「剩余网盘」那一屏用）。

    合成一个函数：单盘和"剩余"要问的是同一件事，只是落到哪个键上不一样。
    """
    names = {"scrape": "刮削结果", "filename": "网盘文件名"}
    dflt = title_policy()
    print()
    if mp:
        own = (ms_state().get("title_by_drive") or {}).get(mp)
        print(f"  {mp} 当前："
              + (f"{CYAN}{BOLD}{names.get(own, own)}{RST}{DIM}（单独设的）{RST}" if own
                 else f"{DIM}跟默认走 → {names.get(dflt, dflt)}{RST}"))
    else:
        print(f"  默认（没单独设过的盘都用它）当前："
              f"{CYAN}{BOLD}{names.get(dflt, dflt)}{RST}")
    print(f"  1. 刮削结果")
    print(f"  2. 网盘文件名")
    if mp:
        print(f"  3. 跟默认走{DIM}（{names.get(dflt, dflt)}）{RST}")
    print(f"  0. 返回")
    c = ask("请选择").strip()
    if c in ("0", "", "q"):
        return
    val = {"1": "scrape", "2": "filename"}.get(c, "x")
    if c == "3" and mp:
        val = None
    if val == "x":
        print("无效选择。")
        return
    if mp:
        set_title_policy_of(mp, val)
        ok(f"{mp} 的片名来源："
           + (f"跟默认走（{names.get(dflt, dflt)}）" if val is None else names[val]))
    else:
        save_ms_state(title_policy=val)
        ok(f"默认片名来源：{names[val]}")
    key = read_emby_api_key(d)
    if key:
        apply_title_policy(d, key)
    else:
        print(f"  {DIM}没有 Emby API Key，改不到已有条目上 —— "
              f"先去「3 后补参数 → 1」填上。{RST}")


# 阿里云盘的「接口通道」。它没有 link_method（那是夸克/UC 的字段），
# 但有一个作用类似、而且决定播放快慢的开关：alipan_type。
# 取值和取令牌页面上的入口一一对应，两边必须配对，见 gen 使用信息那段。
ALIPAN_TYPES = {
    "default":  ("开放平台接口", "第三方 Open API。阿里对它限速 —— 实测约 0.8 Mbps，"
                                "大码率的片子放不动",
                 "阿里云盘 (OAuth2) 扫码登录"),
    "alipanTV": ("TV 客户端接口", "走 TV 版客户端那条通道，不吃上面那个限速；"
                                 "但要另外扫 TV 版二维码取令牌",
                 "阿里云盘 (Client) TV版扫码"),
}


def drive_channel(d, mp, drv):
    """这个盘的「直链走哪条路」：(一句话, 能不能在这里切)。

    【别把"没有 link_method"说成"这个盘没有直链方式"】阿里当然有 302 直链，MediaWarp 照样
    把播放器重定向到 dl1-v6.aliyundrive.cloud。没有的只是"原画/转码流"这个【选择】：那是
    夸克/UC 的 TV 驱动特有的字段。说"没有开关"和说"没有直链"，差得远。

    而且【每个盘至少还有"回源方式"能调】（走 302 还是本机代理，见 SOURCE_MODES）——
    以前这一屏对着 115、WebDAV 这类盘只会写一句"只有这一种"，用户看到的就是
    "这个盘什么都不能做"，可它明明有一个决定能不能播的开关。

    【正常状态不占屏，反常状态必须显眼】回源方式是 302 时一个字都不写（默认，人人如此）；
    切成本机代理才挂到后面 —— 那是会吃双份带宽的状态，不该藏在子菜单里才看得见。
    """
    sw = drive_links(d, mp, drv)
    if not sw:
        return "原画直链", False
    names = [_opt_name(o, c) for k, _w, _t, o, c in sw if k in QUALITY_KEYS] or ["原画直链"]
    names += [_opt_name(o, c) for k, w, _t, o, c in sw
              if k not in QUALITY_KEYS and w != "source"]
    names += [_opt_name(o, c) for _k, w, _t, o, c in sw
              if w == "source" and c != "direct"]
    return " · ".join(names), True


def _jwt_field(tok, name):
    """从 JWT 的 payload 里取一个字段。不是 JWT 或取不到就返回空串。

    只读 payload，不验签 —— 这里要的只是"这串是谁发的"，不是"这串有效吗"。
    """
    try:
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return str(json.loads(base64.urlsafe_b64decode(seg)).get(name) or "")
    except Exception:
        return ""


def _alipan_channel_menu(d, mp):
    """阿里的接口通道：default（开放平台，被限速）↔ alipanTV（TV 客户端）。

    【类型和令牌必须一起换】OpenList 拿 alipan_type 决定向官方 API 报哪个驱动标识
    （default→alicloud_qr，alipanTV→alicloud_tv），拿一种流程取的令牌去另一种那边换，只会
    得到 empty token returned from official API。所以这里【不给只翻类型的按钮】。
    """
    sid = cur = None
    shape = ""
    for s2, m2, _drv2, t2, sh2 in _ali_storages(d):
        if m2 == mp:
            sid, cur, shape = s2, t2 or "default", sh2
    if sid is None:
        warn(f"读不到 {mp} 的存储记录。")
        return
    other = "alipanTV" if cur == "default" else "default"
    # 【库里这份令牌配不配得上要换的那个类型】配得上就不用重新扫码 ——
    # 最常见的一次就是这样：类型被改成了 alipanTV、令牌还是 OAuth2 的那份，
    # 结果整个盘挂不上。这时要做的只是把类型改回 default，令牌一个字都不用动。
    # 上一版不管三七二十一都要人贴一份新令牌，等于让他为了修一个手误再去扫一次码。
    tok_fits = ((shape == "jwt" and other == "default")
                or (shape == "other" and other == "alipanTV"))
    print()
    for k, (name, why, page) in ALIPAN_TYPES.items():
        star = f"  {GREEN}← 现在{RST}" if k == cur else ""
        print(f"  {DIM}·{RST} {BOLD}{name}{RST} {DIM}[{k}]{RST}{star}")
        print(f"      {DIM}{why}{RST}")
    print()
    print(f"  1. 换成「{ALIPAN_TYPES[other][0]}」")
    print("  0. 返回")
    if ask("请选择").strip() != "1":
        print("没有改动。")
        return

    if tok_fits:
        print()
        print(f"  {DIM}库里现有的令牌正好是「{ALIPAN_TYPES[other][2]}」那条路取的，"
              f"不用重新扫码。{RST}")
        if not ask_yn(f"直接换成「{ALIPAN_TYPES[other][0]}」？", True):
            print("没有改动。")
            return
        _write_addition(d, [(sid, mp)], {"alipan_type": other})
        print(f"  {DIM}挂上没有：跑「6 链路体检」，或者 bash tools/ali-token.sh{RST}")
        return

    # 【先把令牌要到手，再动类型】只翻类型 = 必定挂不上，见函数开头
    print()
    warn("类型和令牌必须一起换，只改类型这个盘会挂不上。")
    print(f"  取令牌：{CYAN}{BOLD}https://api.oplist.org/{RST}"
          f"　选 {BOLD}{ALIPAN_TYPES[other][2]}{RST}　只要{BOLD}刷新令牌{RST}")
    # 【那个网站是第三方的，会挂】实测点「获取 Token」直接弹「获取秘钥失败」——
    # 那是它自己的接口返回了非 200，跟这边的配置无关，重试或换国内站有时就好了。
    # 不写这一句的话，人会以为是自己哪一步做错了，在配置里反复折腾。
    print(f"  {DIM}它弹「获取秘钥失败」= 那个网站自己的接口没通，不是你填错了。"
          f"换 https://api.oplist.org.cn/ 或者过一会儿再试{RST}")
    print()
    tok = ask("把刷新令牌粘在这里（留空取消）").strip()
    if not tok:
        print("已取消，一个字都没改。")
        return
    # JWT 形态 = OAuth2 扫码那条路取的；TV 客户端那条给的不是 JWT。
    # 形态和要换的通道对不上就是刚才那个坑，当场拦住比事后报错强。
    looks_jwt = tok.count(".") == 2 and tok.startswith("ey")
    if (other == "alipanTV") == looks_jwt:
        warn(f"这串看着不像「{ALIPAN_TYPES[other][2]}」取的令牌。")
        # 【把证据摆出来，别只说"看着不像"】JWT 里的 aud 就是发它的那个应用 id，而开放平台
        # 发的直链里那个 ap= 参数是同一个值。两边一样，就说明这串还是开放平台那条路取的。
        # 【为什么必须拦住】这种配错【当场是好的】：刚换完能换直链、能播，一小时内访问令牌
        # 到期、拿它去续，官方 API 回空，整个盘掉线 —— 已经发生过两次。
        aud = _jwt_field(tok, "aud")
        if aud:
            print(f"  {DIM}它的 aud（发证应用）= {aud}{RST}")
            print(f"  {DIM}开放平台发的直链里 ap= 就是这个值 —— 同一个应用，"
                  f"也就是说这串是「{ALIPAN_TYPES['default'][2]}」那条路取的{RST}")
        print(f"  {YELLOW}配错的表现很骗人：现在能用，一小时内令牌一续期就整个盘掉线{RST}")
        # 【多半不是你选错了】看过 api.oplist.org 的前端源码（public/static/login.js）：
        # 扫完码去兑换令牌那一步调的是 /alicloud/callback，参数里【没有 driver_txt】——
        # 扫 TV 码和扫 OAuth2 码走的是同一个兑换接口。所以扫了 TV 的二维码、
        # 拿回来的照样可能是开放平台的令牌。实测就是这样。
        # 不写这一句，人会以为是自己选错了下拉框，反复重扫、甚至把存储删掉重建。
        if other == "alipanTV":
            print(f"  {DIM}顺带：那个站扫完码去兑换的那一步（/alicloud/callback）"
                  f"参数里不带类型，扫 TV 码也可能拿回开放平台的令牌 —— "
                  f"多半不是你选错了。重扫、删存储重建都改不了这一点{RST}")
        if not ask_yn("仍然用它？", False):
            print("已取消，一个字都没改。")
            return
    _write_addition(d, [(sid, mp)],
                    {"alipan_type": other, "refresh_token": tok},
                    quiet_keys=("refresh_token",))
    print(f"  {DIM}挂上没有：跑「6 链路体检」，或者 bash tools/ali-token.sh{RST}")


def _ali_storages(d):
    """阿里云盘存储：(id, 挂载点, 驱动, alipan_type, 令牌是不是 JWT 形态)。

    最后那个是【形态】不是令牌本身："jwt" = OAuth2 扫码那条路发的（ey 开头、两个点），
    "other" = TV 客户端那条发的，"" = 根本没有令牌。靠它就知道库里现有的令牌配不配得上某个
    类型 —— 而这决定了换通道时用不用重新扫码。【空串必须和两种形态都区分开】：没有令牌时
    不管换成哪个类型都得重新取。
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
        if str(drv or "").lower() != "aliyundriveopen":
            continue
        try:
            a = json.loads(add)
            cur = str(a.get("alipan_type") or "default")
            tok = str(a.get("refresh_token") or "")
        except Exception:
            cur, tok = "default", ""
        shape = ("" if not tok else
                 "jwt" if (tok.count(".") == 2 and tok.startswith("ey")) else "other")
        out.append((sid, mp, drv, cur, shape))
    return out


def _link_method_menu(d, mounts, who):
    """直链方式。mounts 是要看的挂载点，who 只用于打印。

    单盘传一个，「剩余网盘」传它管的那一批 —— 两边共用这一段，免得同一个开关有两套行为。

    【一批盘不再合并成一个开关】原来的做法是：这批里凡是有 link_method 的盘一起切，其余
    的盘【一个字都不提】。混着夸克、阿里、115、WebDAV 的时候，屏上只看得见夸克那一个开关，
    另外三个盘等于不存在；而它们各有各的开关（阿里的接口通道、每个盘都有的回源方式）。
    用户的原话是「这个里面可能混合各种各样的网盘而且连接方式也应该有很多不同」。
    改成先把这批盘各自现在是什么、能调几项列出来，再进某一个盘去改。

    【也不再一起写】不同驱动的字段本来就不一样，"一起切"这件事只在它们碰巧同驱动时成立。
    """
    ms = [m for m in mounts if m]
    if not ms:
        print(f"\n  {who} 底下没有盘。")
        return
    if len(ms) == 1:
        _one_drive_link_menu(d, ms[0])
        return
    while True:
        print("\n" + "-" * 60)
        print(f"  {BOLD}{who}{RST} 的直链方式")
        print("-" * 60)
        for i, m in enumerate(ms, 1):
            ch, _sw = drive_channel(d, m, "")
            n = len(drive_links(d, m))
            print(f"  {i:>2}. {pad(m, 24)}{CYAN}{ch}{RST}"
                  + (f"  {DIM}{n} 项可调{RST}" if n else f"  {DIM}没有可调的{RST}"))
        print("   0. 返回")
        print("-" * 60)
        c = ask("改哪个盘").strip()
        if c in ("0", "", "q"):
            return
        if not (c.isdigit() and 1 <= int(c) <= len(ms)):
            print("无效选择。")
            continue
        _one_drive_link_menu(d, ms[int(c) - 1])


def _one_drive_link_menu(d, mp):
    """一个盘的直链方式：把它【真的有】的开关列出来，选一项改。

    开关表是 drive_links() 给的，这里只管画屏和写库 —— 加一种新开关不用碰这个函数。
    """
    while True:
        sw = drive_links(d, mp)
        print("\n" + "-" * 60)
        print(f"  {BOLD}{mp}{RST} 的直链方式")
        print("-" * 60)
        if not sw:
            print(f"  {DIM}读不到这个盘的存储记录（OpenList 的库里没有它）。{RST}")
            ask("按回车返回...")
            return
        for i, (_k, _w, title, opts, cur) in enumerate(sw, 1):
            print(f"  {i:>2}. {pad(title, 20)}当前：{CYAN}{_opt_name(opts, cur)}{RST}")
        print("   0. 返回")
        print("-" * 60)
        c = ask("改哪一项").strip()
        if c in ("0", "", "q"):
            return
        if not (c.isdigit() and 1 <= int(c) <= len(sw)):
            print("无效选择。")
            continue
        key, where, _title, opts, cur = sw[int(c) - 1]
        if where == "alipan":
            # 类型和令牌必须一起换，那一屏有它自己的一整套拦截，不能在这里直接写
            _alipan_channel_menu(d, mp)
            continue
        print()
        for v, name, why in opts:
            star = f"  {GREEN}← 现在{RST}" if v == cur else ""
            print(f"  {DIM}·{RST} {BOLD}{name}{RST}{star}")
            print(f"      {DIM}{why}{RST}")
        print()
        for j, (_v, name, _w) in enumerate(opts, 1):
            print(f"  {j}. 换成「{name}」")
        print("  0. 返回")
        t = ask("请选择").strip()
        if not (t.isdigit() and 1 <= int(t) <= len(opts)):
            print("没有改动。")
            continue
        val = opts[int(t) - 1][0]
        if val == cur:
            print("本来就是这个，没有改动。")
            continue
        sid = next((r[0] for r in _storage_rows(d) if r[1] == mp), None)
        if sid is None:
            warn(f"读不到 {mp} 的存储记录。")
            continue
        if where == "source":
            if val == "proxy":
                warn("本机代理：视频的每个字节都要经过你的 VPS，来回两份流量。")
                if not ask_yn("确定换成本机代理？", False):
                    print("没有改动。")
                    continue
            _write_storage(d, [(sid, mp)],
                           columns=next(u for k, _n, _w, u in SOURCE_MODES if k == val))
        else:
            _write_storage(d, [(sid, mp)], addition={key: val})


def _scan_of(mp):
    """这个盘现在扫什么，一句话。给菜单那一列用。

    【多了就不全列】一个盘挂十几条路径是正常用法（按字母分类的盘），全列出来会把外层
    那一屏撑成一坨 —— 那一屏是一行一个盘。要看全部就进这个盘的「1 扫描路径」。
    """
    mine = _paths_under(explicit_scan_paths(), mp)
    if not mine:
        return "整个盘（自动）" if auto_rest_on() else "未加路径"
    if len(mine) <= 2:
        return "、".join(mine)
    return f"{len(mine)} 条：{mine[0]} …"


def _drive_menu(d, mp, drv):
    """单个网盘的设置。"""
    names = {"scrape": "刮削结果", "filename": "网盘文件名"}
    while True:
        ch, switchable = drive_channel(d, mp, drv)
        tp = title_policy_of(mp)
        print("\n" + "=" * 60)
        print(f"  {BOLD}{driver_cn(drv)}{RST} {BOLD}{mp}{RST}   {CYAN}{_scan_of(mp)}{RST}")
        print("=" * 60)
        print(f"  1. 扫描路径          {CYAN}{_scan_of(mp)}{RST}")
        n = len(drive_links(d, mp, drv)) if switchable else 0
        print(f"  2. 直链方式          当前：{CYAN}{ch}{RST}"
              + (f"  {DIM}{n} 项可调{RST}" if n else f"  {DIM}（只有这一种）{RST}"))
        print(f"  3. 片名用哪个        当前：{CYAN}{names.get(tp, tp)}{RST}")
        has115 = "115" in str(drv)
        if has115:
            print(f"  4. 网盘扫码登录")
        print("  0. 返回")
        print("-" * 60)
        c = ask("请选择").strip()
        if c in ("0", "", "q"):
            return
        if c == "1":
            _drive_paths_menu(d, mp)
        elif c == "2":
            _link_method_menu(d, [mp], driver_cn(drv))
        elif c == "3":
            _title_menu(d, mp)
        elif c == "4" and has115:
            qr115_login()
        else:
            print("无效选择。")


def _rest_menu(d):
    """「剩余网盘（自动）」：没被单独设过的盘归它管。"""
    names = {"scrape": "刮削结果", "filename": "网盘文件名"}
    while True:
        exp = explicit_scan_paths()
        rest = [mp for mp, _drv, _st, _r, _m in openlist_storages(d)
                if mp and mp != "/" and not _paths_under(exp, mp)]
        on = auto_rest_on()
        # 【这一行只是概览，别替它们编一个统一答案】剩余组里可能混着夸克、阿里、115、
        # WebDAV，各有各的开关。以前这里拿组里第一个有 link_method 的盘的值当整组的值，
        # 屏上写「原画直链」而组里另外三个盘根本没有这个字段 —— 那是个假的统一。
        chs = [drive_channel(d, m, "")[0] for m in rest]
        uniq = sorted(set(chs))
        ch = uniq[0] if len(uniq) == 1 else (f"各盘不同（{len(rest)} 个盘）"
                                             if rest else "没有剩余的盘")
        print("\n" + "=" * 60)
        print(f"  {BOLD}♻ 剩余网盘（自动）{RST}   "
              + (f"{GREEN}开{RST}" if on else f"{DIM}关{RST}")
              + (f"   {DIM}{'、'.join(rest)}{RST}" if rest else
                 f"   {DIM}没有剩余的盘{RST}"))
        print("=" * 60)
        print(f"  1. 自动路径开关      当前："
              + (f"{GREEN}开{RST}" if on else f"{DIM}关{RST}"))
        print(f"  2. 直链方式          当前：{CYAN}{ch}{RST}")
        print(f"  3. 片名用哪个        当前："
              f"{CYAN}{names.get(title_policy())}{RST}")
        print("  0. 返回")
        print("-" * 60)
        c = ask("请选择").strip()
        if c in ("0", "", "q"):
            return
        if c == "2":
            _link_method_menu(d, rest, "这些剩余的盘")
            continue
        if c == "3":
            _title_menu(d, None)
            continue
        if c != "1":
            print("无效选择。")
            continue
        if not on and rest:
            warn(f"这些盘会按整个盘扫进来：{'、'.join(rest)}")
            if not ask_yn("确定打开？", False):
                continue
        save_ms_state(auto_rest=(not on))
        _apply_scan_paths(d, "自动" + ("打开" if not on else "关掉") + "，")


def mount_paths_menu():
    """挂载路径：一个盘一屏，各管各的。

    【为什么不是一个输入框】原来扫描路径埋在「后补参数」里，要把所有路径用逗号连成一串手
    打进去 —— 加一个盘得把已有的全部重打一遍，在手机 ssh 上根本没法编辑。
    【为什么一个盘一个子菜单】每个盘要管的有四件事：扫哪些目录、直链方式、片名用哪个、
    115 还要扫码登录，而它们全都是一个盘一个样的东西。
    【屏上不写解释】原委只留在注释和 README，菜单上只放：叫什么、现在是什么。
    """
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    while True:
        stores = [(mp, drv, st) for mp, drv, st, _r, _m in openlist_storages(d)
                  if mp and mp != "/"]
        print("\n" + "=" * 60)
        print(f"  {BOLD}挂载路径{RST}{DIM}（哪些网盘要进 Emby，各自扫哪些目录）{RST}")
        print("=" * 60)
        if not stores:
            print(f"  {YELLOW}OpenList 里还没挂任何网盘。{RST}")
            ask("\n按回车返回...")
            return
        # 【这一屏只管设置，不报状态】存储通不通归「6 链路体检」——
        # 那边是真去列一次目录、换一次直链，比这里读一个陈年字段准得多。
        # 【必须带挂载点，光有驱动名认不出是哪个盘】驱动名不唯一：挂两个 WebDAV
        # （一个公益源、一个自己的）时，这一列就是两行一模一样的「WebDAV」，
        # 点进去才知道是哪个。挂载点是用户自己在 OpenList 里起的名字，那才是他认得的那个。
        for i, (mp, drv, _st) in enumerate(stores, 1):
            where = _scan_of(mp)
            col = CYAN if where != "未加路径" else DIM
            print(f"  {i:>2}. {pad(f'{driver_cn(drv)} {mp}', 30)}{col}{where}{RST}")
        print(f"  {len(stores) + 1:>2}. {pad('♻ 剩余网盘（自动）', 24)}"
              + (f"{GREEN}开{RST}" if auto_rest_on() else f"{DIM}关{RST}"))
        # 返回也要占一行。这一屏原来只在提示里写「0 = 返回」，而别的每一屏
        # 都是列成 "0. 返回" —— 同一套菜单里两种写法，回车能不能退出还得试。
        # 编号宽度跟上面的条目对齐（上面用的是 {i:>2}）。
        print(f"  {0:>2}. 返回")
        print("-" * 60)
        c = ask("请选择").strip()
        if c in ("0", "", "q"):
            return
        if not c.isdigit() or not 1 <= int(c) <= len(stores) + 1:
            print("无效选择。")
            continue
        if int(c) == len(stores) + 1:
            _rest_menu(d)
        else:
            mp, drv, _st = stores[int(c) - 1]
            _drive_menu(d, mp, drv)


def drop_orphan_strm_dirs(d, paths, unknown=()):
    """把不再属于任何扫描路径的主目录清掉（问过之后）。

    【缩小扫描范围原来是个没有效果的按钮】去掉一个网盘之后，它那两万多个 strm 一个都不会
    少。prune 也救不了 —— 它只删「网盘上明确不存在」的文件，而那个盘还好好地挂在 OpenList
    上，只是不该再进媒体库了。于是 Emby 继续刮削这几万个条目、体检那些数字继续涨、每轮核对
    失效 strm 的预算全花在已经不要的盘上。用户以为自己做了减法，实际什么都没减。

    删之前【必须问】，而且默认否：这是整个脚本里少数几个真的删用户文件的地方（删的只是本地
    生成的 strm 和刮削缓存，网盘上的片子一个都不碰）。

    【这个函数第一版把用户的 39786 个 strm 全删了】，两个错叠在一起：
      1. 挂载目录在 <DATA_ROOT>/strm/cloud/<盘名>，而这里列的是 <DATA_ROOT>/strm —— 那一层
         只有一个子目录 "cloud"，它当然不在 keep 里，一刀下去连 115 和 quark 一起没了
      2. 没有下限保护。keep 一个都对不上时，正确的反应是「我算错了，什么都别删」

    所以下面那道 keep 检查不是冗余：宁可漏删，也绝不能因为算错就把整棵树端了。
    """
    # 【有一条路径填歪就整个不删】删除的依据是「这些盘不在扫描范围里了」，
    # 而这个推理的前提是那几条路径本身是对的。有路径对不上已挂载的存储时，
    # 前提就已经塌了 —— 那次事故里 /影 就是这么来的，它旁边还立着一行
    # 「这些路径不在已挂载的存储里」的警告，而清理照删不误。
    if unknown:
        print(f"  {DIM}有路径对不上已挂载的存储，本地 strm 一个都不动 ——"
              f"先把路径填对，再回来改一次就会问。{RST}")
        return
    keep = {m for m in (strm_mount_dir(p) for p in paths) if m}
    root = os.path.join(strm_root(d), STRM_SUBDIR)     # 盘名在 cloud/ 这一层
    try:
        dirs = sorted(x for x in os.listdir(root)
                      if os.path.isdir(os.path.join(root, x)))
    except OSError:
        return
    orphans = [x for x in dirs if x not in keep]
    if dirs and not (set(dirs) & keep):
        # 一个都没留下 = keep 算错了（路径填歪了、或者盘名对不上）。这时候
        # 「全都是孤儿」是推理错误，不是事实 —— 闭嘴，什么都别删。
        warn(f"扫描路径算出来的主目录（{'、'.join(sorted(keep)) or '空'}）"
             f"和本地已有的（{'、'.join(dirs)}）一个都对不上。")
        print(f"  {DIM}不动任何文件 —— 这种情况多半是扫描路径填歪了。"
              f"确认一下上面那几条路径对不对。{RST}")
        return
    if not orphans:
        return
    sizes = {}
    for x in orphans:
        n = 0
        for _dp, _dn, fs in os.walk(os.path.join(root, x)):
            n += sum(1 for f in fs if f.endswith(".strm"))
        sizes[x] = n
    print()
    warn(f"这 {len(orphans)} 个主目录已经不在扫描范围里了，但它们的 strm 还在本地：")
    for x in orphans:
        print(f"  {DIM}·{RST} {x}　{BOLD}{sizes[x]}{RST} 个 strm")
    # 【把留下的也列出来】只报要删的，用户没有参照物。第一版就是这样把
    # 「cloud　39786 个」摆在人面前，看不出那其实是整棵树。
    kept = [x for x in dirs if x in keep]
    print(f"  {DIM}会留下：{RST}{'、'.join(kept) if kept else f'{RED}没有{RST}'}")
    print(f"  {DIM}留着的话 Emby 会继续刮削它们、继续占内存，体检里那些"
          f"「没时长」「没收录」的数字也会一直挂着。{RST}")
    print(f"  {DIM}删掉的只是本机生成的 strm 和刮削缓存，{RST}"
          f"{BOLD}网盘里的片子一个都不碰{RST}{DIM}。{RST}")
    if not ask_yn(f"把这 {sum(sizes.values())} 个 strm 从本地删掉？", False):
        print(f"  {DIM}留着。以后想清，再进这里改一次扫描路径就会再问。{RST}")
        return
    gone = 0
    for x in orphans:
        try:
            shutil.rmtree(os.path.join(root, x))
            gone += sizes[x]
        except OSError as e:
            warn(f"{x} 没删干净：{_short_err(e)}")
    ok(f"删掉 {gone} 个 strm")
    print(f"  {DIM}Emby 那边的条目要等它扫一次才会消失 —— 「5 生成媒体库」"
          f"最后会通知扫描，每小时的对齐任务也会做。{RST}")
    print(f"  {YELLOW}媒体库本身还在 Emby 里{RST}{DIM}，路径指向的目录现在是空的。"
          f"不想要就去 Emby 的「媒体库」里把它删掉。{RST}")




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
        print(f"  1. 添加 API 密钥（Emby API Key）   当前：{state}")
        print(f"  2. 修改用户名 / 密码（浏览器弹框那层）  当前：{ba_state}")
        # 【直链方式 / 片名用哪个 / 115 扫码登录 都搬去「4 挂载路径」了】
        # 它们全是【一个盘一个样】的东西：直链方式只有夸克/UC 的驱动才有，
        # 片名该跟文件名还是跟刮削也是每个盘各不相同 —— 放在这里当全局开关，
        # 本身就是把它们摆错了地方。扫描路径同理。
        mt_state = ((f"{GREEN}已安装{RST}" if metatube_on(d) else f"{DIM}未安装{RST}")
                    if is_installed(d) else f"{DIM}未安装{RST}")
        print(f"  3. MetaTube 刮削插件（番号识别）  当前：{mt_state}")
        # 【这里只报"用哪份"，一个字都不多】规则文件路径、几条、哪几个库名 ——
        # 这些进到菜单里会随库数一起长，7 条就要折两行，几十条整屏都是它。
        # 点进第 4 项那一屏本来就全列着，重复一遍只是把菜单撑丑。
        _rsrc = (f"{CYAN}自定义{RST}" if rules_source() == "custom"
                 else f"{DIM}作者的{RST}") if is_installed(d) else ""
        print(f"  4. 按关键词自动建媒体库（规则用哪份链接）  当前：{_rsrc}")
        if metatube_on(d):
            mtl = [n for n, _i, on, _o in metatube_libraries(
                read_emby_api_key(d) or "") if on]
            print(f"  5. MetaTube 在哪些库生效  当前："
                  + (f"{CYAN}{'、'.join(mtl)}{RST}" if mtl else f"{DIM}都不启用{RST}"))
        # 目录缓存直接决定「列目录」快不快 —— 命中缓存 0.3 秒，走真实接口十几秒
        # 还会被限流。当前值摆在菜单上，和直链方式一个道理。
        dcs = dir_cache_storages(d) if is_installed(d) else []
        if dcs:
            _vals = {ce for _s, _m, _d2, ce in dcs}
            dc_state = (f"{CYAN}{dcs[0][3]} 分钟{RST}" if len(_vals) == 1
                        else f"{YELLOW}各存储不一致{RST}")
        else:
            dc_state = f"{DIM}未挂网盘{RST}"
        _dcm = "" if ms_state().get("dir_cache_manual") else f"{DIM}　脚本自动维护{RST}"
        print(f"  6. 目录缓存时长{DIM}（列目录老超时就调大这个）{RST}  "
              f"当前：{dc_state}{_dcm}")
        _ef = ep_fix_setting()
        ef_state = (f"{DIM}没问过{RST}" if _ef is None else
                    (f"{CYAN}开{RST}" if _ef else f"{DIM}关{RST}"))
        print(f"  7. 给剧集补季集编号{DIM}（旁挂 .nfo，不改文件名、不动网盘）"
              f"{RST}  当前：{ef_state}")
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
            toggle_metatube()
        elif c == "4":
            auto_libraries()
        elif c == "5":
            d0 = ms_install_dir()
            k0 = (read_yaml_scalar(os.path.join(d0, "mediawarp", "config",
                                                "config.yaml"), "auth")
                  if is_installed(d0) else "")
            if not metatube_on(d0):
                warn("MetaTube 没装，先在「5」里装。")
            elif not k0:
                warn("没有 Emby API Key，先填「1」。")
            else:
                choose_metatube_libraries(k0)
        elif c == "6":
            set_dir_cache()
        elif c == "7":
            set_episode_fix()
        else:
            print("无效选择。")
            continue
        ask("\n按回车返回...")


# ============================================================================ 链路体检
def _hc(label, state, detail=""):
    icon = {"ok": f"{GREEN}✔{RST}", "warn": f"{YELLOW}⚠{RST}",
            "bad": f"{RED}✖{RST}", "skip": f"{DIM}—{RST}"}[state]
    print(f"\r\x1b[2K    {pad(label, 20)}{icon}  {detail}")


def _hc_group(title, why):
    """体检的分组标题。

    二十多项平铺成一长条，扫到一半就不知道自己在看什么了 —— 而这些项的轻重差得很远：
    「列目录失败」是片子放不了，「证书还有 79 天」是三个月后的事。分组之后顺序也有了意思：
    先「能不能放」，再「片子对不对」，最后才是背景信息。
    """
    print(f"\n  {BOLD}{title}{RST}  {DIM}{why}{RST}")


def _stale_note(elapsed_min, every_min, what, late=3):
    """定时任务「早就该跑了」的判定。返回 (状态, 补充说明)。

    【只报"上次几点跑的"是不够的】实测吃过一次大亏：cron 里多套了一层 flock，三条定时任务
    全被锁死、一次都没跑成，而体检那行写的是「链路保活 ✔ 719 分钟前成功」—— 绿的。
    "719 分钟前"这个数字明明摆在那儿，可 ✔ 让人一眼扫过去就跳过了。

    late = 允许迟到几轮，【按任务的性质分别定，不能一刀切】：统一用 3 倍时，每日对齐停了
    39 小时照样是绿的（24×3 = 72 小时才判）。
      · 高频任务（保活 20 分钟）→ 3 倍。被负载挤晚一两轮很正常，连丢三轮才是真不跑了
      · 每日任务 → 1.5 倍（36 小时）。一天跑一次的东西，迟到半天就是丢了一整轮
    """
    if elapsed_min <= every_min * late:
        return "ok", ""
    return "bad", (f"　{RED}每 {what} 该跑一次，已经 "
                   f"{elapsed_min // 60} 小时没跑了{RST}")


def _hc_wait(label, secs):
    """慢检查开始前先把行占上，让人看得见它在等什么、要等多久。

    体检恰恰是「东西已经坏了」的时候才会跑的：网盘接口不通时列目录会一直等到超时，屏幕却
    停在上一行不动 —— 最需要它说话的时刻，它反而一声不吭。
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
                     # 【canceled 和 deadline exceeded 不是一回事，别混成一句】
                     # deadline exceeded = 等到我们自己设的时限，网盘没回话；
                     # canceled = 【发起方主动撤了】—— 网页上就是浏览器等不下去先断了，
                     # OpenList 顺手把上游那个请求也取消掉。前者要去查网盘，
                     # 后者说明请求慢到了人先放弃，慢的根子仍在网盘，但不是同一种证据。
                     ("context canceled", "请求被中途取消（多半是页面等太久先断了）"),
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
    """从 strm 内容里还原出它在 OpenList 上的路径。两种形态都要认：

      · 路径形式(旧)   /quark/电影/x.mp4
      · URL 形式(新)   https://list.<域名>/d/quark/电影/x.mp4?sign=…

    体检里有几项要按「挂载点」给文件分组，只认路径形式的话会得出「还没有 strm 文件」这种
    和上一行「strm 文件 3 个」直接打架的结论。
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


def now_playing_ids(key):
    """当前正在播放的条目 id 集合。取不到就返回空集（宁可不热，也不误判）。"""
    try:
        return {str((s.get("NowPlayingItem") or {}).get("Id"))
                for s in _emby("/Sessions", key, timeout=20)
                if s.get("NowPlayingItem")}
    except Exception:
        return set()


def resume_items(key, uid, limit=10):
    """「继续观看」里的前 N 部（只要 strm 的）。返回 [(id, 名字, 续播点ticks, 源)]。

    Emby 的正规入口是 /Users/{uid}/Items/Resume。万一某个版本没有这个路由，
    退回用 Filters=IsResumable 自己筛 —— 这个文件里已经因为猜错接口路径栽过两次，
    带个兜底比赌它一定在强。
    """
    q = (f"?Limit={limit * 3}&MediaTypes=Video&Recursive=true"
         f"&Fields=Path,MediaSources")
    for path in (f"/Users/{uid}/Items/Resume{q}",
                 f"/Users/{uid}/Items{q}&Filters=IsResumable"):
        try:
            r = _emby(path, key, timeout=30)
        except Exception:
            continue
        out = []
        for i in r.get("Items") or []:
            if not str(i.get("Path") or "").endswith(".strm"):
                continue
            srcs = i.get("MediaSources") or []
            pos = (i.get("UserData") or {}).get("PlaybackPositionTicks") or 0
            out.append((i.get("Id"), str(i.get("Name") or "?"), pos,
                        srcs[0] if srcs else {}))
            if len(out) >= limit:
                break
        if out:
            return out
    return []


def wait_openlist_ready(d, timeout=90):
    """等 OpenList 能正常登录。返回是否等到了。

    只用「登录成功」作判据，不去列目录：登目录要跨境，慢的时候要一两分钟，而这里要问的只是
    "OpenList 自己起好了没有"，那是本机的事，答得很快。

    【为什么要把秒数打出来】屏幕上如果只有一句"等 OpenList 起好…"，那么"正在正常地等"和
    "卡死了"长得一模一样。所以滚动刷新已用秒数和上限，看得见在走、也看得见还要多久。
    """
    pw = read_env(os.path.join(d, ".secrets"), "OPENLIST_PASS",
                  fallback=os.path.join(d, ".env"))
    t0 = time.monotonic()
    said = False
    while True:
        el = time.monotonic() - t0
        if el >= timeout:
            break
        try:
            tok = (_ol_api("/api/auth/login",
                           {"username": "admin", "password": pw},
                           timeout=10).get("data") or {}).get("token", "")
            if tok:
                el = time.monotonic() - t0
                if said:
                    print(f"\r\x1b[2K  {DIM}OpenList 就绪（等了 {el:.0f} 秒）{RST}")
                return True
        except Exception:
            pass
        said = True
        print(f"\r\x1b[2K  {DIM}等 OpenList 起好再重启 MediaWarp…"
              f"{el:.0f}/{timeout} 秒{RST}", end="", flush=True)
        time.sleep(2)
    if said:
        print("\r\x1b[2K", end="")      # 占位行擦掉，让调用方的 ⚠ 从行首开始
    return False


def restart_mediawarp_when_ready(d, quiet=False):
    """等 OpenList 起好，再重启 MediaWarp。返回【有没有真的重启成】。

    MediaWarp 只在启动那一刻登录 OpenList，之后一直用那一个令牌。OpenList 一重启旧令牌就
    作废，所以它必须跟着重启；而且顺序不能反 —— OpenList 还没起好就重启它，那次登录直接
    失败，它照样握着一个废令牌。

    【返回值不是给日志看的】没重启成，此后每一次换直链都会被拒（对 Emby 表现为 404、点开
    一直转圈）。拿到 False 的调用方必须【停下所有还要换直链的后续动作】—— 尤其是预热：
    明知每一部都会 404 还照跑十分钟，最后还会得出"网盘接口在抖"这种完全相反的结论。
    """
    if wait_openlist_ready(d):
        subprocess.run(["docker", "restart", "mediawarp"], capture_output=True,
                       timeout=120)
        if not quiet:
            info("MediaWarp 已重启（换新令牌，否则换直链会 401）")
        return True
    warn("等了 90 秒 OpenList 还没起来，没有重启 MediaWarp。")
    print(f"  {DIM}它手里还是旧令牌，换直链会被拒 —— Emby 里表现为点开一直转圈。{RST}")
    print(f"  {DIM}等 OpenList 好了敲：{RST}{BOLD}docker restart mediawarp{RST}"
          f"{DIM}，再去播一部片子。{RST}")
    return False


def _fetch_text(url, timeout, limit=1 << 20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def warm_hls(loc, at_sec, timeout):
    """转码流的预热：把播放列表读开，去拉【真正的那个分片】。返回一句说明。

    这是之前预热"热了等于没热"的根因。转码流的 302 给的是 .m3u8 —— 那是一份文本播放列表，
    拉它的前 64KB 只是把目录读了一遍，网盘完全没被要求去准备任何视频数据。表现就是：明明
    热过了，点开还是先跑几 KB/s，一两分钟后才提速 —— 那正是网盘在现做分片。

    所以要顺着列表往下走一层：主列表先挑一路码率，媒体列表按 #EXTINF 累加时长找到续播点
    落在哪个分片，然后去拉那个分片。
    """
    try:
        txt = _fetch_text(loc, timeout)
    except Exception:
        return "播放列表没读到"
    # 主列表（多码率）→ 先下钻到第一路
    if "#EXT-X-STREAM-INF" in txt:
        nxt = next((l.strip() for l in txt.splitlines()
                    if l.strip() and not l.startswith("#")), "")
        if not nxt:
            return "主列表里没有可用码率"
        loc = urllib.parse.urljoin(loc, nxt)
        try:
            txt = _fetch_text(loc, timeout)
        except Exception:
            return "媒体列表没读到"
    # 按 #EXTINF 累加，找出续播点落在第几个分片
    segs, acc, pick, dur = [], 0.0, 0, 0.0
    for ln in txt.splitlines():
        ln = ln.strip()
        if ln.startswith("#EXTINF:"):
            try:
                dur = float(ln.split(":", 1)[1].split(",")[0])
            except ValueError:
                dur = 0.0
        elif ln and not ln.startswith("#"):
            segs.append(ln)
            if acc < at_sec:
                pick = len(segs) - 1
            acc += dur
    if not segs:
        return "播放列表里没有分片"
    pick = min(pick, len(segs) - 1)
    try:
        req = urllib.request.Request(urllib.parse.urljoin(loc, segs[pick]),
                                     headers={"User-Agent": "Mozilla/5.0"})
        n = len(urllib.request.urlopen(req, timeout=timeout).read(WARM_BYTES))
        return f"已拉第 {pick + 1}/{len(segs)} 个分片 {n // 1024}KB"
    except Exception as e:
        return f"分片没拉到（{_short_err(e)}）"


def rest_items(key, uid, limit, cursor):
    """全库里从 cursor 开始的一段（只要 strm）。返回 (四元组列表, 下一个 cursor)。

    补上预热的最后一块：「继续观看」和「最近新加」两批合起来盖不到【看完过的老片】——
    它有播放记录所以不在「继续观看」，又不是新加的所以不在 Latest 里。

    【必须轮转，不能每轮都热前 N 部】否则库一大，靠后的片子永远轮不到。用 StartIndex 取一个
    窗口、记下位置，下一轮接着往后走，到头了从 0 开始。

    用 StartIndex 而不是"取全部再切片"：四万条目的库那样取一次就是几十兆 JSON。
    续播点一律给 0：这批片子要么没看过、要么看完了，从头热就是待会儿要播的那段。
    """
    if limit <= 0:
        return [], cursor, 0
    try:
        r = _emby(f"/Users/{uid}/Items?Recursive=true"
                  f"&IncludeItemTypes=Movie,Episode&Fields=Path,MediaSources"
                  f"&SortBy=SortName&SortOrder=Ascending"
                  f"&StartIndex={max(0, int(cursor))}&Limit={limit}", key, timeout=30)
    except Exception:
        return [], cursor, 0
    items = r.get("Items") or []
    total = int(r.get("TotalRecordCount") or 0)
    # 【库太大就整批不做】理由见 WARM_ROTATE_MAX 那段。这里返回空但把 total 带出去，
    # 让调用方能说清楚"为什么不热"—— 静悄悄地什么都不做，下次没人知道是设计还是坏了。
    if total > WARM_ROTATE_MAX:
        return [], cursor, total
    nxt = max(0, int(cursor)) + limit
    if not total or nxt >= total:
        nxt = 0                         # 转完一圈（或者库是空的），从头再来
    out = []
    for i in items:
        if not str(i.get("Path") or "").endswith(".strm"):
            continue
        srcs = i.get("MediaSources") or []
        out.append((i.get("Id"), str(i.get("Name") or "?"), 0,
                    srcs[0] if srcs else {}))
    return out, nxt, total


def latest_items(key, uid, limit=5):
    """最近加进库的前 N 部（只要 strm）。返回和 resume_items 同样的四元组。

    为什么必须单独取这一批：预热原来只热「继续观看」，而【新片从来没播过，永远进不了那个
    列表】—— 于是"刚加的片子第一次点开特别慢"成了预热盖不到的真空区，而这恰恰是最常发生
    的场景。续播点一律给 0：新片没有进度，从头热正是待会儿要播的那一段。
    """
    try:
        r = _emby(f"/Users/{uid}/Items/Latest"
                  f"?Limit={limit * 3}&MediaTypes=Video"
                  f"&Fields=Path,MediaSources&IsPlayed=false", key, timeout=30)
    except Exception:
        return []
    # /Items/Latest 直接返回数组，不是 {"Items": [...]}
    items = r if isinstance(r, list) else (r.get("Items") or [])
    out = []
    for i in items:
        if not str(i.get("Path") or "").endswith(".strm"):
            continue
        srcs = i.get("MediaSources") or []
        out.append((i.get("Id"), str(i.get("Name") or "?"), 0,
                    srcs[0] if srcs else {}))
        if len(out) >= limit:
            break
    return out


def warm_links(d, key, limit=None):
    """给「继续观看」和最近新加的片子提前接好线路。返回 (成功数, 总数)。

    范围只取前 10 部左右，这个取舍是对的：
      · 每热一个都要跨境换一次直链（实测 0.3～27 秒）。整库热的话，片子一多就从"省时间"变成
        "耗时间"，而且绝大多数根本不会在缓存有效期内被点开
      · 「继续观看」里的那几部恰恰是最可能被点开的
      · 但只热这一批有个真空区：【新片从没播过，永远进不了「继续观看」】，而"刚放完片子回来
        就想看"是最常见的场景。所以剩下的名额补给最近新加的

    热的做法和真实播放【完全一样】：走 MediaWarp 的 /Videos/{id}/stream 拿 302，再从【续播点
    那个位置】拉一小段字节 —— 位置对得上才有意义，从头拉反而热错了地方。
    """
    try:
        users = _emby("/Users", key)
    except Exception:
        return 0, 0
    uid = (users[0] or {}).get("Id", "") if users else ""
    if not uid:
        return 0, 0
    limit = limit or WARM_LIMIT
    # 【网盘正忙就这轮不热】预热本身就是在敲同一个网盘账号换直链。AutoFilm 扫库或者 Emby
    # 全库扫描的时候再插进去是往拥堵里加车：热出来的多半是超时（白打一轮还占着重试名额），
    # 更糟的是把用户此刻真正想看的那一部挤慢了 —— 实测撞过：同一条路径 20.5 秒，扫描过去之后
    # 立刻再打是 0.4 秒。预热是每小时一轮的锦上添花，让一轮毫无代价。
    busy = netdisk_load(d, key)
    if busy:
        print()
        info(f"网盘这会儿正忙（{busy}），这轮预热跳过 —— "
             f"{WARM_EVERY_H} 小时后那轮会补上")
        print(f"  {DIM}预热和扫库用的是同一个网盘账号，挤在一起只会两边都慢。{RST}")
        return 0, 0
    # 「继续观看」优先 —— 看了一半的东西最可能被接着点。剩下的名额给新加的片子：
    # 它们从没播过，进不了「继续观看」，而"刚放完片子回来就想看"恰恰是最常见的
    # 场景。两批合起来仍然封顶 limit 部，不会因为多热一类就把整轮拖长。
    cut = resume_items(key, uid, limit)
    seen = {str(i) for i, _n, _p, _s in cut}
    for it in latest_items(key, uid, max(0, limit - len(cut))):
        if str(it[0]) not in seen:
            cut.append(it)
            seen.add(str(it[0]))
    # 第三批：按顺序轮全库。上面两批盖不到「看完过的老片」—— 有播放记录所以不在
    # 「继续观看」，不是新加的所以不在 Latest 里，于是永远是冷的。
    # 排在最后是因为优先级最低：真正会被点开的还是前两批，这一批是兜底。
    # 轮转位置存在状态文件里，下一轮接着走 —— 每轮都热前 N 部的话，库一大
    # 靠后的永远轮不到，等于没热。
    cur = int(ms_state().get("warm_cursor") or 0)
    more, nxt, total = rest_items(key, uid, WARM_REST, cur)
    if total > WARM_ROTATE_MAX:
        print(f"  {DIM}库里 {total} 部，超过轮转的意义范围（{WARM_ROTATE_MAX} 部）——"
              f"只热「继续观看」和新加的。{RST}")
        print(f"  {DIM}再多热也是白打网盘接口：热一部只能管 {LINK_TTL_H} 小时，"
              f"轮一圈要 {total // max(1, WARM_REST)} 小时，轮回来早凉了。{RST}")
    for it in more:
        if str(it[0]) not in seen:
            cut.append(it)
            seen.add(str(it[0]))
    if nxt != cur:
        save_ms_state(warm_cursor=nxt)
    if not cut:
        return 0, 0
    # 【正在播的一律不碰】这是回退逻辑的致命处：光看"续播点动了没有"，分不清是预热推的还是
    # 【用户此刻正在看】。整点那次预热要是撞上他在看片，回退就会把真实进度抹掉。
    # 而正在播的片子本来也不需要热：它的直链早就在缓存里了。跳过它，两个问题一起没。
    playing = now_playing_ids(key)
    if playing:
        skip = [n for i, n, _p, _s in cut if str(i) in playing]
        cut = [x for x in cut if str(x[0]) not in playing]
        for n in skip:
            print(f"  {DIM}·{RST} {n[:24]}  {DIM}正在播，跳过（本来就是热的）{RST}")
    if not cut:
        return 0, 0

    print()
    info(f"提前接好线路（共 {len(cut)} 部）："
         f"「继续观看」+ 最近新加 + 轮到的老片...")
    print(f"  {DIM}只换直链、拉 {WARM_BYTES // 1024}KB，不上报播放进度；"
          f"热完回读一遍，被推动了就改回原值。{RST}")
    # 【把上限和"能不能中途走"说出来】每部片子跑完才打一行，而单部最多要等
    # WARM_STEP_T 秒 —— 于是第一行出来之前屏幕是死的，看着就是卡住了。
    # 配置在进这个函数之前就已经写完落盘了，Ctrl-C 走人不会留下半截状态。
    print(f"  {DIM}最多跑 {WARM_BUDGET // 60} 分钟；不想等就 Ctrl-C，"
          f"配置已经改好了，热不热只影响第一次点开快不快。{RST}")
    opener = urllib.request.build_opener(_NoRedirect)
    done, dead = 0, []
    t_all = time.monotonic()
    # 【多轮重试】跨境超时绝大多数是偶发的：同一部片这一秒超时、下一秒 0.3 秒就回来。
    # 一轮打完就走的话，热成率完全看运气 —— 用户实测有一轮 4 部只热上 1 部。
    # 失败的攒起来再来一遍，中间隔几秒让接口喘口气，比一次性打完靠谱得多。
    todo_q, attempt = list(cut), 0

    def wipe():
        """把"接线中…"那行占位擦掉，再打别的。

        占位行是用 \\r 顶着的、没有换行。后面凡是【不以 \\r 开头】的输出直接打，
        就会接在它屁股后面，两句话糊成一行（"…接线中… 最多 45 秒...3 部没热上"）。
        所以每一处多行提示前面都先擦一次。
        """
        print("\r\033[K", end="", flush=True)

    while todo_q and attempt < WARM_RETRY:
        attempt += 1
        if attempt > 1:
            wipe()
            print(f"  {DIM}...{len(todo_q)} 部没热上，隔 5 秒再试一轮"
                  f"（第 {attempt}/{WARM_RETRY} 轮）{RST}")
            time.sleep(5)
        again = []
        for iid, name, pos, src in todo_q:
            # 【总时长封顶】跨境慢的时候一个能耗掉半分钟。这是后台任务，跑太久没意义
            # —— 一小时后还会再来，剩下的留给那一轮
            if time.monotonic() - t_all > WARM_BUDGET:
                wipe()
                print(f"  {DIM}...已用满 {WARM_BUDGET // 60} 分钟，剩下的交给下一轮{RST}")
                again = []
                todo_q = []
                break
            t0 = time.monotonic()
            if done or again:            # 第一个不等，之后每个之间歇一下
                time.sleep(WARM_GAP)
            # 【先打一行占位再去等】不然这一部跑完之前屏幕一动不动（单部最多
            # WARM_STEP_T 秒），和卡死长得一样。下面成功/失败那行用 \r 盖掉它。
            print(f"\r  {DIM}·{RST} {pad(name[:24], 26)}"
                  f"{DIM}接线中… 最多 {WARM_STEP_T} 秒{RST}\033[K",
                  end="", flush=True)
            url = (f"http://127.0.0.1:{MEDIAWARP_PORT}/Videos/{iid}/stream"
                   f"?MediaSourceId=mediasource_{iid}&Static=true&api_key={key}")
            loc, why = "", ""
            try:
                opener.open(url, timeout=WARM_STEP_T)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location", "")
                else:
                    why = f"HTTP {e.code}"
            except Exception as e:
                why = _short_err(e)
            if not loc and attempt < WARM_RETRY:
                again.append((iid, name, pos, src))
                continue
            if not loc:
                # 【只报事实，不猜原因】上一版这里写死了"多半是网盘里已经删了"，
                # 而实测打出来的是 timed out —— 超时只说明接口那几十秒没回话，
                # 跟文件在不在毫无关系。文件到底还在不在，由每日对齐那步的三态
                # 判据说了算（明确回答"对象不存在"才算删）。
                dead.append((name[:24], why))
                tip = ("网盘接口一直没回话，线路慢，下一轮再试"
                       if ("timed out" in why or "timeout" in why.lower() or not why)
                       else f"{why} —— 下一轮再试；一直这样就跑「6 链路体检」")
                print(f"\r  {DIM}·{RST} {name[:24]}  {YELLOW}没热上{RST}"
                      f"{DIM}（{tip}）{RST}\033[K")
                continue
            # 按续播点估算字节位置。转码流是 m3u8（整份播放列表），没有位置可言，
            # 直接拉开头就行；原画是完整文件，才需要跳到那一段去
            size = src.get("Size") or 0
            run = src.get("RunTimeTicks") or 0
            at = ""
            if ".m3u8" in loc.lower() or (src.get("Container") or "").lower() == "hls":
                # 转码流：必须下钻到分片，拉播放列表等于没热（见 warm_hls 的说明）
                at = f"  {DIM}（{warm_hls(loc, pos / 6e8 * 60, WARM_STEP_T)}）{RST}"
            else:
                # 原画：完整文件，直接跳到续播点那个字节位置
                head = {"User-Agent": "Mozilla/5.0"}
                if size and run and pos:
                    off = min(int(size * pos / run), max(size - WARM_BYTES, 0))
                    head["Range"] = f"bytes={off}-{off + WARM_BYTES - 1}"
                    at = f"  {DIM}（从 {pos / 6e8:.0f} 分处）{RST}"
                else:
                    head["Range"] = f"bytes=0-{WARM_BYTES - 1}"
                try:
                    urllib.request.urlopen(urllib.request.Request(loc, headers=head),
                                           timeout=WARM_STEP_T).read(WARM_BYTES)
                except Exception:
                    at = f"  {DIM}（字节没拉到，直链已进缓存）{RST}"
            done += 1
            print(f"\r  {GREEN}\u2714{RST} {name[:24]}  "
                  f"{time.monotonic() - t0:.1f} 秒{at}\033[K")
        todo_q = again
    wipe()      # 最后一部要是进了重试队列，占位行还顶在屏幕上，下面的总结会糊上去

    # 【自查：续播点有没有被推着走】用户担心的正是这个 —— "热着热着一天下来那个
    # 继续播放进度条都跑完了"。理论上不会：预热只请求流、从不调 Emby 的播放上报
    # 接口，而且 MediaWarp 直接 302 掉、Emby 根本看不见这次请求。
    # 但"理论上不会"这句话今天已经被打脸好几次了，所以实测一遍：对不上就改回去。
    moved = []
    # 回退前【重新】取一次正在播的集合：预热这几分钟里用户完全可能刚点开播放。
    # 那种情况下续播点是他自己推的，绝不能改回去
    playing = now_playing_ids(key)
    for iid, name, pos, _src in cut:
        if str(iid) in playing:
            print(f"  {DIM}·{RST} {name[:24]}  {DIM}期间开始播放了，续播点归你，不动{RST}")
            continue
        try:
            now = ((_emby(f"/Users/{uid}/Items/{iid}", key, timeout=20)
                    .get("UserData") or {}).get("PlaybackPositionTicks") or 0)
        except Exception:
            continue
        if abs(now - pos) < 6e8 / 60:          # 1 秒以内的抖动不算
            continue
        moved.append((name[:24], pos, now))
        try:
            _emby(f"/Users/{uid}/Items/{iid}/UserData", key, method="POST",
                  body={"PlaybackPositionTicks": pos}, timeout=20)
        except Exception:
            pass
    if moved:
        warn(f"预热把 {len(moved)} 个条目的续播点推动了，已改回原值：")
        for nm, was, now in moved:
            print(f"  {DIM}·{RST} {nm}  {now / 6e8:.1f} → 改回 {was / 6e8:.1f} 分")
        print(f"  {DIM}这不该发生，说明预热的请求被当成了真实播放。{RST}")

    if done:
        ok(f"{done}/{len(cut)} 部已接好，点「继续播放」不用等换直链")
    elif dead:
        # 【全军覆没要按报错原文分岔，不能一律说"网盘在抖"】两种的处置完全相反：
        #   · 一部一部超时 = 真的是线路在抖，等下一轮就行，人什么都不用做
        #   · 齐刷刷 404   = MediaWarp 换直链被拒，十有八九是它手里的 OpenList 令牌废了。
        #                    这个不会自愈，下一轮照样全 404 —— 得重启 MediaWarp
        # 说反了的代价是实打实的：用户等了一小时，回来还是一部都放不了。
        four = [w for _n, w in dead if w.startswith("HTTP 4")]
        warn(f"{len(dead)} 部都没换到直链。")
        if len(four) >= max(2, len(dead) - 1):
            code = max(set(four), key=four.count)      # 取占多数的那个码，别拿第一条顶
            print(f"  {DIM}全是 {code} —— 这不是网盘慢，是 MediaWarp 换直链被拒了。"
                  f"最常见的原因：OpenList 重启过，它手里的令牌作废了。{RST}")
            print(f"  {DIM}敲这一条再试：{RST}{BOLD}docker restart mediawarp{RST}")
            print(f"  {DIM}还是不行就跑「6 链路体检」。{RST}")
        else:
            print(f"  {DIM}网盘接口这会儿多半在抖，下一轮（{WARM_EVERY_H} 小时后）"
                  f"会自动再试 —— 你什么都不用做。{RST}")
    else:
        warn("一个都没接上 —— 网盘接口可能正好在抖，跑「6 链路体检」看看。")
    return done, len(cut)


def probe_302(key, own_host="", want_kind=""):
    """真的发一次播放请求，看 MediaWarp 到底回不回 302、302 到哪。

    这是整套东西唯一的端到端证明。前面那些检查（存储 work、能换到直链）都只说明"零件是好
    的"，而播放实际走哪条路要看这一下：
      · 302 → 内部地址     客户端根本连不上
      · 200 不是 302       MediaWarp 没拦住，视频要经过本机中转，吃你的带宽

    strm 改成 URL 形式之后这里【多了一跳】：MediaWarp 302 到的是 OpenList 的公网地址，播放器
    要再跟一次才到网盘 CDN，所以拿到第一跳之后还要再跟一次 —— 否则「302 → 自己的域名」看起来
    像成功，实际上可能第二跳就死了。返回 (state, 说明)。
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
    item = next((i for i in items if _under(str(i.get("Path", "")), STRM_PATH)), None)
    if not item:
        return "skip", "媒体库里还没有网盘条目"

    # 这一部属于哪个挂载点。多盘的时候必须报出来 —— 见下面「内部地址」那一支
    rel = str(item.get("Path", ""))[len(STRM_PATH):].lstrip("/")
    probed_mount = rel.split("/")[0] if rel else ""

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
        if _is_internal_host(host):
            # 【必须报是哪个盘】这条以前只说「302 → 内部地址」，用户在多盘环境里
            # 无从知道是哪一个 —— 而它测的只是媒体库里排在前面的那一部，
            # 换个盘的片子可能完全正常。实测那次三个盘里只有 WebDAV 那个是坏的，
            # 光看这行会以为整套 302 都废了。
            return "bad", (f"302 → {host}  {RED}内部地址，客户端连不上{RST}"
                           f"{DIM}（测的是 {probed_mount or '?'} 里的片子）{RST}")

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
                if _is_internal_host(host):
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
        # 实际拿到的形态和设置里的直链方式对不上，几乎一定是【直链缓存还没过期】：
        # MediaWarp 的 alist_api_ttl 是 2 小时，刚切换完，老地址还在缓存里。
        # 不说明的话，用户会以为切换没生效 —— 而其实只是还没轮到这个文件换。
        if kind and want_kind and kind != want_kind:
            tail += (f"  {YELLOW}← 设置里是{want_kind}，这条是切换前缓存的地址，"
                     f"最多 2 小时后自动换过来{RST}")
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


def _is_internal_host(host):
    """这个 host（可能带端口）是不是「只有本机能连」的地址。

    直链和 302 两处都要判，判法必须一致 —— 曾经各写各的，结果同一个
    127.0.0.1:5244 在「换直链」那行是绿的、在「302 直链」那行是红的。
    """
    bare = host.split(":")[0]
    return (bare in ("openlist", "emby", "mediawarp", "localhost")
            or _is_private_ip(bare))


def public_visitors(limit=20000):
    """从媒体服务自己那份 nginx 访问日志里，统计非内网来源的 IP。

    为什么值得看：emby / list / home 这三个子域是公网可达的，任何人拿到域名就能敲门。
    Homepage 有 Basic Auth，但 Emby 和 OpenList 走的是它们自己的登录页 —— 有没有人在外面试，
    只有日志知道。返回 (总请求数, [(ip, 次数), ...] 按次数降序)；日志不存在返回 (0, [])。
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


def _ol_api(path, body, token=None, timeout=60, method="POST"):
    """OpenList 的接口。method="GET" 时不带 body —— 它的几条查询接口只认 GET，
    发成 POST 会回 405，而那种失败长得像"接口不存在"。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{OPENLIST_PORT}{path}",
        data=json.dumps(body).encode() if method != "GET" else None,
        method=method,
        headers={"Content-Type": "application/json",
                 **({"Authorization": token} if token else {})})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def stack_versions(key=""):
    """各组件的版本。取不到的返回空串 —— 宁可不显示，也不编一个。

    能问出真版本号的只有 Emby 和 OpenList（它们有自己的接口）。MediaWarp 和
    AutoFilm 没有版本接口，退而取【镜像的构建日期】：这套东西全用 :latest 标签，
    构建日期就是"你手上这份有多新"最实在的答案，比没有强。
    """
    # 【脚本自己排第一】这套东西每次修的都是脚本，可 stack_versions 一直只报
    # Emby/OpenList/MediaWarp/AutoFilm —— 唯独漏了它。结果是"修好推上去了"和
    # "这台机器上跑的是哪一版"之间没有任何一条能对上的信息，排查时只能靠猜
    # 用户到底更新没更新。这一行早就该有。
    out = {"脚本": f"v{SCRIPT_VERSION}"}
    if key:
        try:
            out["Emby"] = str(_emby("/System/Info", key, timeout=20).get("Version") or "")
        except Exception:
            pass
    try:
        r = _ol_api("/api/public/settings", {}, timeout=15)
        # OpenList 把 commit、前端版本、构建时间全塞在这一个字段里，原样打出来
        # 能顶掉整行。只取开头那个版本号
        v = str((r.get("data") or {}).get("version") or "")
        out["OpenList"] = re.split(r"[\s(]", v.strip(), 1)[0]
    except Exception:
        pass
    for name, img in (("MediaWarp", "akimio/mediawarp:latest"),
                      ("AutoFilm", "akimio/autofilm:latest")):
        r = sh(f"docker image inspect {img} -f '{{{{.Created}}}}'", timeout=30)
        v = (r.stdout or "").strip().strip("'")[:10]
        if v and v[:4].isdigit():
            out[name] = f"{v} 构建"
    return {k: v for k, v in out.items() if v}


def _up_minutes(status):
    """把 docker 的「Up 12 minutes」换成分钟。不在跑返回 -1。"""
    s = status or ""
    if not s.startswith("Up"):
        return -1
    m = re.search(r"(\d+)\s*(second|minute|hour|day|week|month)", s)
    if not m:
        return 60 if "hour" in s else 1440      # 「Up About an hour」这种没有数字
    return int(m.group(1)) * {"second": 0, "minute": 1, "hour": 60,
                              "day": 1440, "week": 10080, "month": 43200}[m.group(2)]


def container_health(names):
    """每个容器：(名字, 起来多少分钟, 重启过几次, 是不是被 OOM 杀过)。取不到就不返回那条。

    RestartCount 和 OOMKilled 是【累计】的，容器不重建就一直留着 —— 正好是"经常断开"
    这种间歇故障需要的证据：事后去看，进程是好好跑着的，只有这两个数字记得发生过什么。
    """
    if not names:
        return []
    st = {}
    r = sh("docker ps --format '{{.Names}}\t{{.Status}}'", timeout=30)
    for line in (r.stdout or "").splitlines():
        p = line.split("\t")
        if len(p) == 2:
            st[p[0].strip()] = p[1].strip()
    out = []
    r = sh("docker inspect --format '{{.Name}} {{.RestartCount}} {{.State.OOMKilled}}' "
           + " ".join(names), timeout=30)
    for line in (r.stdout or "").splitlines():
        p = line.split()
        if len(p) != 3:
            continue
        nm = p[0].lstrip("/")
        try:
            cnt = int(p[1])
        except ValueError:
            cnt = 0
        out.append((nm, _up_minutes(st.get(nm, "")), cnt, p[2].lower() == "true"))
    return out


def mem_pressure():
    """(可用内存 MB, swap 已用 MB, swap 总量 MB)。读不到返回 (0, 0, 0)。

    看 MemAvailable 不看 MemFree：Linux 会把空闲内存全拿去当页缓存，MemFree 常年很小，
    拿它判断"够不够"必然误报。MemAvailable 才是"还能给新进程用多少"。
    """
    v = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                try:
                    v[k] = int(rest.split()[0])          # kB
                except (ValueError, IndexError):
                    continue
    except OSError:
        return 0, 0, 0
    total = v.get("SwapTotal", 0) // 1024
    return v.get("MemAvailable", 0) // 1024, total - v.get("SwapFree", 0) // 1024, total


def netdisk_load(d, key=""):
    """此刻还有谁在同时敲网盘。列目录慢的时候，先看这个再去怪线路。

    为什么需要：体检报「列目录 52.1 秒」的那台机器，对【同一条路径】手工连打 8 次带 refresh
    的 fs/list，全部在 1.3 秒以内 —— 那 52 秒不是跨境往返延迟，是【那一次请求排在了别人后面】。
    夸克对同一个账号有并发和频率限制，而 Emby 扫库、AutoFilm 生成 strm 都会把同一个接口打满。
    同一路径采到的 0.8 → 12.9 → 52 → 79 → 97 → 106 秒也是这个形状：单调爬升是排队/退避的
    曲线，不是线路抖动的曲线（抖动会上下跳）。

    这件事必须报出来，因为体检原来的结论是「线路问题，服务端改不了」—— 用户照着这句话只会去
    折腾一条根本没毛病的网络，而真正的原因再等几分钟就自己没了。没查到并发就返回空串。
    """
    busy = []
    # Emby 扫库是最凶的那个：一次全库扫描会把每个 strm 都读一遍，每读一个
    # MediaWarp 就要向 OpenList 要一次直链，全压在同一个网盘账号上。
    if key:
        try:
            for t in _emby("/ScheduledTasks", key, timeout=15) or []:
                if t.get("State") != "Running":
                    continue
                pct = t.get("CurrentProgressPercentage")
                busy.append(f"Emby 在跑「{t.get('Name') or '?'}」"
                            + (f" {pct:.0f}%" if isinstance(pct, (int, float)) else ""))
        except Exception:
            pass
    # AutoFilm 空闲时是不写日志的（它在等下一次定时），所以近两分钟有日志
    # 基本就等于它正在跑。只陈述观察到的东西，不替它下判断。
    r = sh("docker logs --since 2m autofilm", timeout=20)
    n = len([x for x in ((r.stdout or "") + (r.stderr or "")).splitlines() if x.strip()])
    if n:
        busy.append(f"AutoFilm 近 2 分钟有 {n} 行日志（在生成 strm）")
    return "；".join(busy[:3])


def do_healthcheck():
    """把整条链路挨个打一遍，每项报耗时和结论。

    这套东西的失败模式几乎全是「看起来正常、实际是废的」—— 存储状态 work 但根目录 ID 填错所以
    目录是空的；strm 生成了但 mode 错了所以 302 永远失败；302 成功了但换一次直链要 30 秒所以
    播放卡在开头。每一个都只能靠翻容器日志一层层挖，而真正的报错往往被几百行访问日志淹掉。
    这里的每一项都对应一个真实踩过的坑，不是凭空设计的检查表。
    """
    # 体检当场数出来的"没时长"个数。下面「每日对齐」那一行要拿它跟
    # 上次跑完时记下的数字对一下 —— 不对一下就会出现「条目时长 ✔ 都有」
    # 和「还有 8 个没时长」同屏打架，用户没法判断该信哪个。
    live_nodur = None
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    cfg = rebuild_cfg_from_disk(d)
    # 早点读出来：列目录那一项慢的时候要拿它去问 Emby 是不是正在扫库
    key = read_yaml_scalar(os.path.join(d, "mediawarp", "config", "config.yaml"), "auth")
    todo = []                      # 收集「发现的问题 + 该怎么办」，最后统一打印

    print("\n" + "=" * 60)
    print(f"  {BOLD}链路体检{RST}   {DIM}每一项都对应一个真实卡过的地方{RST}")
    print("=" * 60)

    _hc_group("能不能放", "这一组红了，片子就打不开")

    # ---- 容器 ----
    # 这份名单必须和 compose 里实际起了哪些服务一致，否则「5/5 在跑」会在
    # 6 个容器的机器上打出来 —— 一个漏掉的容器（metatube 死了）永远不会被报出来，
    # 而用户看到的是一片绿。装了 metatube 却不数它，就是这个下场。
    want = ["emby", "openlist", "mediawarp", "autofilm"]
    if cfg["homepage"]:
        want.append("homepage")
    if metatube_on(d):
        want.append("metatube")
    running = (sh("docker ps --format '{{.Names}}'", timeout=30).stdout or "").split()
    dead = [c for c in want if c not in running]
    if dead:
        _hc("容器", "bad", f"{len(want) - len(dead)}/{len(want)} 在跑，"
                           f"{RED}没起来：{' '.join(dead)}{RST}")
        todo.append(("容器没起全", f"docker compose -f {d}/docker-compose.yml up -d"))
    else:
        _hc("容器", "ok", f"{len(want)}/{len(want)} 在跑")

    # ---- 容器稳定性 ----
    # 【"在跑"和"一直在跑"是两回事】上面那行只看此刻有没有进程。而"挂载页面经常连不上、
    # 过一会儿又好"最典型的成因恰恰是【容器被反复重启】—— 尤其是内存不够被系统 OOM 杀掉。
    # 每被杀一次，OpenList 的存储要重新初始化、MediaWarp 手里的登录令牌作废，表现就是
    # 时好时坏；而等用户想起来去体检，进程早就又跑起来了，上面那行是绿的。
    # RestartCount / OOMKilled 是累计值，是这种间歇故障事后唯一还留着的证据。
    ch = container_health([c for c in want if c in running])
    avail, sw_used, sw_total = mem_pressure()
    oom = [c[0] for c in ch if c[3]]
    many = [(c[0], c[2]) for c in ch if c[2] >= 3]
    fresh = [(c[0], c[1]) for c in ch if 0 <= c[1] < 15]
    mem = ""
    if avail:
        mem = f"内存可用 {avail} MB"
        if sw_total:
            mem += f"，swap {sw_used}/{sw_total} MB"
    tight = avail and avail < 200 and sw_total and sw_used > sw_total * 0.9
    if oom:
        _hc("容器稳定性", "bad",
            f"{RED}{'、'.join(oom)} 被系统 OOM 杀过{RST}"
            + (f"　{DIM}{mem}{RST}" if mem else ""))
        todo.append(("内存不够，容器被系统杀过（挂载时好时坏就是它）",
                     "加 swap，或把不用的容器停掉：docker stop metatube"))
    elif many:
        _hc("容器稳定性", "warn",
            "　".join(f"{n} 重启过 {c} 次" for n, c in many)
            + (f"　{DIM}{mem}{RST}" if mem else ""))
        todo.append(("容器反复重启 —— 挂载时断时续多半是它",
                     f"看它为什么退出：docker logs --tail 100 {many[0][0]}"))
    elif tight:
        _hc("容器稳定性", "warn",
            f"{YELLOW}内存吃紧{RST}{DIM}（{mem}）—— 还没被杀，但快了{RST}")
        todo.append(("内存快用完了", "加 swap，或把不用的容器停掉"))
    elif fresh:
        _hc("容器稳定性", "warn",
            "　".join(f"{n} {m} 分钟前刚起来" for n, m in fresh)
            + f"　{DIM}刚重启过的话是正常的{RST}")
    else:
        _hc("容器稳定性", "ok",
            "没有重启记录，也没被 OOM 杀过" + (f"　{DIM}{mem}{RST}" if mem else ""))

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
    for mp, drv, st, root, mode in stores:
        bad_root = drv.lower().startswith("quark") or drv.lower().startswith("uc")
        if st != "work":
            # status 是【存储初始化那一刻】写进去的，之后成功了也不会自动改回 work。拿它当
            # 实时状态用会把陈年记录报成当前故障 —— 出现过「存储 ✖ 网盘接口超时」和下一行
            # 「列目录 ✔ 22 项」自相矛盾。所以这里只报为「上次初始化的记录」，真正的结论交给
            # 下面的实测。
            # 图标用「—」不用「⚠」：打印这一行的时候脚本【还不知道】现在通不通。用告警图标
            # 表达一个"还不知道"的东西，结果就是每次体检都先亮一个黄灯，而下面全绿 —— 用户
            # 学会忽略它之后，真出事那次也会被跳过去。
            brief = _short_err(st)
            _hc(f"存储 {mp}", "skip", f"{drv}  {DIM}上次初始化时：{brief}"
                                      f"（当前状态看下面的实测）{RST}")
            stale_status[mp] = brief
            # 【类型和令牌必须配对】阿里的 alipan_type 决定向官方 API 报的驱动
            # 标识：default → alicloud_qr，alipanTV → alicloud_tv。拿一种流程
            # 取的令牌去另一种那边换，官方 API 返回空，就是这句报错。
            # 只改了类型没换令牌是最容易踩的 —— 那个下拉框就在表单里，
            # 而令牌要去另一个网站重取，两件事离得远。
            if "empty token" in str(st).lower() and drv.lower() == "aliyundriveopen":
                todo.append((
                    f"{mp} 挂不上：类型（alipan_type={mode or 'default'}）和"
                    f"刷新令牌不配对 —— 令牌是按另一种流程取的",
                    "两条路二选一：① 把「阿里盘账户类型」改回 default，"
                    "原来的令牌立刻就能用（速度还是被限）；"
                    "② 保持 alipanTV，去 https://api.oplist.org/ 选下拉框里的"
                    "【阿里云盘 (Client) TV版扫码】重新扫码取一个新令牌，"
                    "把类型和令牌【一起】换掉"))
        elif bad_root and (not root or "/" in root):
            _hc(f"存储 {mp}", "bad", f"{drv}  work  {RED}根文件夹ID={root or '空'}{RST}")
            todo.append((f"{mp} 的根文件夹ID 是 {root or '空'}，夸克要的是文件夹 ID",
                         "OpenList → 存储 → 编辑 → 根文件夹ID 填 0 → 全部重新加载"))
        else:
            _hc(f"存储 {mp}", "ok", f"{drv}  work"
                + (f"  根目录ID={root}" if root else "")
                + (f"  {DIM}接口 {mode}{RST}" if mode else ""))
        # 【走哪套接口，决定的是速度，不是通不通】—— 而"通不通"上面已经报绿了，所以这一条
        # 不看状态、单独判。实测：阿里云盘 alipan_type=default 走的是开放平台的【下载】接口，
        # 被限到 0.5 MB/s（≈4 Mbps）；而挂载页面放的是阿里的转码流（1 Mbps 上下），所以
        # "挂载能放、Emby 卡死"—— 两边根本不是同一路流。17 Mbps 的原盘在 4 Mbps 上必卡。
        if drv.lower() == "aliyundriveopen" and (mode or "default") == "default":
            _hc(f"接口 {mp}", "warn",
                f"alipan_type=default{DIM}　开放平台的下载接口，"
                f"阿里限速到 0.5 MB/s 左右{RST}")
            todo.append((
                f"{mp} 走的是阿里开放平台【下载】接口，被限速到 0.5 MB/s 上下 —— "
                f"码率高的片子必卡（挂载页面不卡是因为那边放的是转码流，不是原片）",
                "OpenList → 存储 → 这个盘 → 编辑 → 「阿里盘账户类型」改成 "
                "alipanTV（TV 接口，和夸克必须选 QuarkTV 是一个道理）。"
                "多半要按新类型重新取一次刷新令牌，不限速通常还要超级会员。"
                "不想折腾就把大码率的片子放夸克，阿里放压过的"))

    # ---- 列目录 ----
    listed_ok = []

    def _list_once(path, tmo=120):
        """列一次目录并计时，顺手把这一次记进历史。返回 (耗时, code, 报错, 条数)。

        refresh: true 是必须的 —— 不加的话 OpenList 直接返回目录缓存、根本不联网，记下来的
        耗时永远是 0.0 秒，量的是缓存命中率不是链路。
        per_page 跟保活那边对齐成 1。注意【这不是提速】—— 实测两者没有差别（OpenList 本来就是
        把整个目录从网盘拉回来再本地分页）。对齐的理由是【可比】：保活和体检并排打在同一屏上，
        两行必须量的是同一件事。条数从 data.total 取，OpenList 无论 per_page 多少都给全量总数。
        """
        t = time.monotonic()
        try:
            # 120 秒封顶，不是 180：体检本来就是「东西坏了」才跑的，网盘不通时
            # 每条路径干等 3 分钟，人只会以为体检自己死了。实测最慢的一次真实
            # 列目录是 119.8 秒，120 刚好盖住。
            rr = _ol_api("/api/fs/list", {"path": path, "password": "", "page": 1,
                                          "per_page": 1, "refresh": True},
                         token, timeout=tmo)
            e = time.monotonic() - t
            dd = rr.get("data") or {}
            n = dd.get("total")
            if not isinstance(n, int):
                n = len(dd.get("content") or [])
            code = rr.get("code")
            msg = "" if code == 200 else str(rr.get("message") or "list 失败")[:60]
        except Exception as ex:
            e, code, msg, n = time.monotonic() - t, 0, _short_err(ex), 0
        append_hist(d, {"ts": int(time.time()), "ok": code == 200,
                        "elapsed": round(e, 1), "error": msg, "src": "体检"})
        return e, code, msg, n

    for p in (cfg["scan_paths"] or [])[:5]:            # 最多测 5 条，别把体检拖太久
        if not token:
            _hc(f"列目录 {p}", "skip", "OpenList 没登上")
            continue
        _hc_wait(f"列目录 {p}", 120)
        el, code, msg, n_items = _list_once(p)

        # 慢【或者失败】都紧跟着对同一条路径再打一发。这一发是判因用的，不是重试 —— 一次读数
        # 区分不了「这条路不通」和「这一下赶上了一次性开销」，而这两种情况该做的事完全相反。
        # 逼出这个设计的实测：体检报 55.2 秒的同一屏上，换直链（同一个存储、同一分钟）只用
        # 0.6 秒；手工对同一条路径连打 8 次全部 ≤1.3 秒。线路要是坏的，这两样不可能快。
        # 【失败也要再打一发】：只在「成功但慢」的分支里加第二发的话，真出故障那次反而没采到
        # 第二个数据点 —— 最需要判因的那一次，判因的手段没跑。
        el2 = code2 = msg2 = None
        if code != 200 or el >= 5:
            _hc_wait(f"列目录 {p}", 60)
            el2, code2, msg2, n2 = _list_once(p, 60)
            if code2 == 200:
                n_items = n2
        second = ""
        if el2 is not None:
            second = (f"  →  再打一次 {el2:.1f} 秒"
                      + (f" {msg2[:24]}" if code2 != 200 else ""))

        if code != 200 and (el2 is None or code2 != 200):
            # 两发都失败 —— 是真故障，不是慢
            _hc(f"列目录 {p}", "bad", f"{el:.1f} 秒  {msg}{second}")
            todo.append((f"{p} 列不出来：{msg}",
                         "两次都失败。多半是这个存储在 OpenList 里没初始化成功"
                         "（看上面「存储」那行）：OpenList → 存储 → 找到它 → "
                         "先停用再启用，重新加载一次；还不行就 docker restart openlist"))
        elif code != 200:
            # 第一发失败、第二发就通了 —— 间歇性，别当成故障报
            _hc(f"列目录 {p}", "warn",
                f"{el:.1f} 秒 {msg}{second}  第一发失败第二发就通了，间歇性")
            listed_ok.append((p, n_items))
            todo.append((f"{p} 第一次列失败（{msg}），紧接着再列就通了 —— 间歇性抽风",
                         "不是配置错了。要是「生成媒体库」老扫到一半停，"
                         "错开后台扫库的时间再点一次"))
        elif not n_items:
            _hc(f"列目录 {p}", "warn", f"{el:.1f} 秒  空目录")
            todo.append((f"{p} 是空的", "路径写错了？或者网盘里这个目录本来就没东西"))
        else:
            # 列目录也要看耗时。以前只给「换直链」设了阈值，列目录无论多慢都判绿，出现过
            # 「列目录 47.0 秒」和结论「全部正常」同屏 —— 那比不体检更误导。而且这一项直接决定
            # 「生成媒体库」跑不跑得完：AutoFilm 每个目录都要列一次。
            # 阈值按实测重标过：同一条路径采样 9 次，中位数 ~3 秒、正常波动到 12 秒。原来的
            # 「< 3 秒才算绿」等于让一条健康的跨境线路永远飘黄 —— 一直报警就等于没报警。
            st = "ok" if el < 5 else ("warn" if el <= 30 else "bad")
            # 第二发就快了，说明路是通的。降级成提醒，别再打红叉 ——「列目录 ✖」
            # 配上「换直链 ✔ 0.6 秒」同屏出现，是这个体检自己在自相矛盾。
            if el2 is not None and code2 == 200 and el2 < 5:
                st = "warn"
                second += "  第一次是一次性开销，路是通的"
            elif el2 is not None and code2 == 200:
                second += "  两次都慢，这条路真有问题"
            # 还要看有没有人在同时敲网盘。原来这里无条件写「线路问题，服务端改不了」，
            # 会把用户支去折腾一条没毛病的网络，而真凶（后台在扫库）反而没人提，
            # 再等几分钟它自己就消失了。
            load = netdisk_load(d, key) if st != "ok" else ""
            extra = ("" if st == "ok" or second
                     else ("  偏慢" if st == "warn" else "  太慢（正常 < 5 秒）"))
            # 27 = 前导 4 空格 + pad(label,20) + 图标 1 + 2 空格
            _hc(f"列目录 {p}", st, f"{el:.1f} 秒  {n_items} 项{extra}{second}"
                                   + (f"\n{' ' * 27}{DIM}同时在敲网盘：{load}{RST}"
                                      if load else ""))
            listed_ok.append((p, n_items))
            if el2 is not None and code2 == 200 and el2 < 5:
                todo.append((f"列 {p} 第一次用了 {el:.0f} 秒，紧接着再列只要 {el2:.1f} 秒"
                             f" —— 线路是通的，慢在第一次的一次性开销"
                             + (f"（同一刻 {load}）" if load else ""),
                             "不用改网络。「生成媒体库」要是扫到一半停了，"
                             "错开后台扫库的时间再点一次"))
            elif st == "bad" and load:
                todo.append((f"列 {p} 用了 {el:.0f} 秒 —— 但同一刻 {load}，"
                             f"这个数字量的是排队，不一定是线路",
                             "等后台那件事跑完再体检一次。两次都慢才是线路问题"))
            elif st == "bad":
                todo.append((f"列 {p} 用了 {el:.0f} 秒，「生成媒体库」很可能扫到一半就超时",
                             "此刻没有别的东西在占网盘，两次都慢，是接口到本机的线路问题。"
                             "把扫描路径收窄到具体的媒体目录"
                             "（主菜单 4 挂载路径），目录少了成功率高很多"))

    # 单次读数说服不了任何人。「列目录 55 秒」到底是这一下赶上了，还是它就没快过？
    # 保活每 KEEPALIVE_MIN 分钟本来就在测同一条路径，把结果攒起来，这个问题就不用
    # 猜了 —— 直接看分布。体检自己的探测也记在同一本账上。
    recs = keepalive_history(d, 24)
    hstat = hist_stats(recs)
    if hstat:
        hst, htodo = hist_verdict(hstat)
        head, rows = hist_block(hstat, recs)
        _hc("列目录历史", hst, head)
        for line in rows:
            print(f"      {line}")
        if htodo:
            todo.append(htodo)
    else:
        _hc("列目录历史", "skip", "还没攒够记录（保活每跑一次记一条）")

    # 【紧跟在列目录后面】这一项直接决定上面那两行好不好看：命中缓存的列目录
    # 不走网盘接口（实测 0.3 秒 vs 12.7 秒），也就不会被限流。列目录老失败的
    # 时候，这是最对症的一个旋钮，得让人一眼看见现在设的是多少。
    _dcs = dir_cache_storages(d)
    if _dcs:
        _low = [f"{mp} {ce} 分钟" for _s, mp, _dv, ce in _dcs if ce and ce <= 30]
        _txt = "　".join(f"{mp} {ce} 分钟" for _s, mp, _dv, ce in _dcs)
        if _low and hstat and hstat.get("bad"):
            # 【区分"脚本还没来得及设"和"用户自己设成这样"】前者不是待办，
            # 跑一次更新就好了；后者才需要提醒他这个值和他的线路对不上。
            # 【这里只摆事实，不再劝人调长】缓存调长确实能让列目录少碰网盘接口，
            # 但代价是网盘里刚加的片子要等这么久才看得见 —— 而"点进去就能看到新片"
            # 恰恰是用户对这套东西最基本的预期。两头都是实实在在的代价，
            # 该由他自己选，体检的活是把两头说清楚，不是替他决定。
            _hc("目录缓存", "warn",
                f"{_txt}  {YELLOW}短缓存 + 列目录老超时{RST}"
                f"{DIM}（大部分列目录都在走真实接口，而网盘对它限流）{RST}")
            print(f"      {DIM}调长能少碰这个接口（3 后补参数 → 6），"
                  f"代价是网盘里新加的片子要等这么久才看得见。{RST}")
            print(f"      {DIM}想两头都要：缓存留短，别在凌晨扫库那会儿翻挂载。{RST}")
        else:
            _hc("目录缓存", "ok", _txt)

    # 存储 status 是陈旧记录，只有当【实测也失败】时才算真故障
    live_ok = {p for p, _ in listed_ok}
    for mp, brief in stale_status.items():
        if any(p == mp or p.startswith(mp.rstrip("/") + "/") for p in live_ok):
            continue                                   # 实际能列出来，那条记录已经过期了
        # 【别一律说成"线路问题"】这句原来是无条件加的，于是同一个故障会同时
        # 出现两条互相打架的待办：上面刚说完"类型和令牌不配对，去改配置"，
        # 这里紧接着说"线路问题，不是配置错了，等几分钟"。用户照后面那条等，
        # 等多久都不会好。能从报错原文认出来的，就别猜。
        _b = brief.lower()
        if "empty token" in _b or "refresh token" in _b:
            continue          # 令牌/类型的事，上面已经给过确切的修法
        if "not init" in _b or "storage not found" in _b:
            todo.append((f"{mp} 这个存储没初始化成功（{brief}）",
                         "OpenList → 存储 → 找到它 → 先停用再启用；"
                         "还不行就看它的令牌是不是过期或者填错了"))
            continue
        todo.append((f"{mp} 连不上网盘接口（{brief}）",
                     "多半是线路，不是配置。等几分钟再跑一次体检；"
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
            elif _is_internal_host(raw.split("/")[2]):
                # 【快 ≠ 通】实测撞过：一屏上「换直链 ✔ 0.0 秒 → 127.0.0.1:5244」和「302 直链
                # ✖ 内部地址，客户端连不上」并排。0.0 秒恰恰是症状 —— 它根本没去网盘换，直接把
                # OpenList 自己的地址回来了。代理型存储（WebDAV、本地盘这些没有 CDN 直链的驱动）
                # 就是这样：视频要经 OpenList 中转，外网客户端拿到这个地址只会连不上。
                _hc(label, "bad", f"{el:.1f} 秒  →  {raw.split('/')[2]}  "
                                  f"{RED}本机地址，外网放不了{RST}")
                todo.append((
                    f"{mount} 换出来的「直链」是本机地址（{raw.split('/')[2]}）——"
                    f"这个盘的片子在外网点开会一直连不上",
                    "这类驱动（WebDAV、本地目录等）在网盘那边没有 CDN 直链，"
                    "OpenList 只能自己中转，302 也就没法指向外部。"
                    "和 public_url 无关，其它盘不受影响"))
            elif el > 2:
                _hc(label, "warn",
                    f"{el:.1f} 秒  偏慢（正常 < 2 秒）  →  {raw.split('/')[2]}")
            else:
                _hc(label, "ok", f"{el:.1f} 秒  →  {raw.split('/')[2]}")
        except Exception as e:
            _msg = _short_err(e)
            _hc(label, "bad", _msg)
            # 同上：能从报错原文认出是配置的，别说成线路 —— 说错了用户就会去等，
            # 而配置问题等多久都不会自己好。
            _m = _msg.lower()
            if "empty token" in _m or "refresh token" in _m:
                _fix = ("令牌和「阿里盘账户类型」不配对。"
                        "4 挂载路径 → 选那个盘 → 2 直链方式，按提示换")
            elif "not init" in _m or "storage not found" in _m:
                _fix = ("这个存储没初始化成功。OpenList → 存储 → 找到它 → "
                        "先停用再启用；还不行就查它的令牌")
            else:
                _fix = ("多半是网盘接口不通，不是配置。"
                        "已生成的 strm 不受影响，等几分钟再跑一次体检")
            todo.append((f"{mount} 换直链失败，此刻这个盘的片子会卡在开头", _fix))

    # ---- MediaWarp ----（key 在函数开头就读好了，列目录那一项要用）
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
    # ---- MediaWarp 用哪个地址问 OpenList ----
    # 【代理型存储（WebDAV 源、本地目录）能不能在 Emby 里播，全看这一项】它们在网盘侧
    # 没有 CDN 直链，OpenList 只能回自己的 /d/ 地址，而那个地址是【按请求里的 Host 拼】的
    # ——OpenList v4 已经没有「网站 URL」那个设置了，改不了它，只能改问它的人。
    # MediaWarp 拿 http://openlist:5244 去问，拿回来的就是内网名，302 给手机电视解析不了。
    # 【不能用 read_yaml_scalar("addr")】配置里有两个 addr：server.addr 是 Emby
    # （http://emby:8096），alist_strm 那个才是 OpenList。取第一个就永远读成 Emby，
    # 这一项会一直报错还指错方向。只在 alist_strm: 之后那一段里找。
    _mw_addr = ""
    try:
        _txt = open(os.path.join(d, "mediawarp", "config", "config.yaml"),
                    encoding="utf-8").read()
        _seg = _txt.split("alist_strm:", 1)
        if len(_seg) == 2:
            _m = re.search(r"^\s*-?\s*addr:\s*(\S+)", _seg[1], re.M)
            _mw_addr = _m.group(1).strip('"\'') if _m else ""
    except OSError:
        pass
    _proxy_drives = [mp for _sid, mp, drv, _add, cols in _storage_rows(d)
                     if _truthy(cols.get("web_proxy"))
                     or str(drv or "").lower() in ("webdav", "local", "crypt")]
    _want_addr = (openlist_public_url(cfg) or "").rstrip("/")
    if not _proxy_drives:
        pass                      # 没有代理型的盘，这一项与它无关，不占屏
    elif _mw_addr.rstrip("/") == _want_addr and _want_addr:
        _hc("MediaWarp→OpenList", "ok",
            f"{_mw_addr}{DIM}（代理型的盘发得出对外地址）{RST}")
    else:
        _hc("MediaWarp→OpenList", "bad",
            f"{_mw_addr or '?'}  {YELLOW}内网地址 —— "
            f"{'、'.join(_proxy_drives[:2])} 这类盘的 302 播放器连不上{RST}")
        todo.append((f"MediaWarp 用 {_mw_addr or '内网地址'} 去问 OpenList，"
                     f"而 {'、'.join(_proxy_drives[:2])} 是代理型存储（WebDAV 源、"
                     f"本地目录）—— 它们在网盘侧没有 CDN 直链，OpenList 只能回自己的"
                     f"地址，而那个地址是按请求里的 Host 拼的，于是 302 出去的是容器"
                     f"内网名，手机电视解析不了，表现为 load fail",
                     f"跑一次「7 更新」：它会把这个地址改成 {_want_addr or '对外地址'}"
                     f"（探不通会自动退回内网地址，不会把别的盘搞坏）"))

    # ---- 302 端到端 ----
    own_host = urllib.parse.urlsplit(openlist_public_url(cfg)).hostname or ""
    _hc_wait("302 直链", 90)
    # 把设置里的直链方式传进去，好让 302 那行能指出"拿到的和设的不一致"
    _lm = link_method_storages(d) if is_installed(d) else []
    _want = ({"download": "原画", "streaming": "转码流"}.get(_lm[0][3], "")
             if len({c for _s, _m, _d, c in _lm}) == 1 and _lm else "")
    # MediaWarp 的 OpenList 令牌失效时，302 那一项【测不出来】：它挑到的条目只要命中了直链
    # 缓存就照样 302 成功（3 毫秒，根本不问 OpenList），而缓存里没有的片子全部 404。用户看到的
    # 是"有的能放有的不能放"，体检却一片绿。所以直接去日志里找那句话。
    # 【必须只看本次启动之后的日志】docker restart 不清旧日志，固定 --since 6h 会把重启之前那些
    # 401 一起读进来 —— 修好了还报故障，用户照着待办再重启一次，还是报。
    bad_tok, mwlog = mediawarp_token_broken()
    if bad_tok:
        _hc("MediaWarp 令牌", "bad",
            f"{YELLOW}对 OpenList 的令牌失效了，没缓存的片子会打不开{RST}")
        todo.append(("MediaWarp 拿着一个作废的 OpenList 令牌，换直链时被拒（401）。"
                     "已缓存直链的片子还能放，其余的报错 —— 表现是「有的能放有的不能放」",
                     "docker restart mediawarp　（它启动时会重新登录换新令牌）。"
                     "每小时的保活任务现在也会自己发现并重启它"))
    elif mwlog.strip():
        _hc("MediaWarp 令牌", "ok", "本次启动以来没有换直链被拒的记录")

    # ---- 直链缓存时长 vs 直链自己能活多久 ----
    # 【这一项体检以前测不出来，正因为体检自己碰不到它】302 那一项每次都现换一条
    # 新地址，新地址当然是好的；坏的是【放了一阵子的那条】。缓存比直链活得长，
    # MediaWarp 就会把死地址 302 出去，播放器报 load fail，而这边日志里是一次
    # 正常的 302。所以只能靠比对配置值，测是测不出来的。
    _ttl_want, _ttl_min = link_ttl_of(d)
    _mw_yaml = os.path.join(d, "mediawarp", "config", "config.yaml")
    _ttl_cur = read_yaml_scalar(_mw_yaml, "alist_api_ttl")
    # 【空配置要单独报】容器是启动时读配置的，文件被清空之后它照常跑（内存里
    # 还是老的一份），要等下一次重启才整个起不来 —— 那时候没人会联想到是几天前
    # 某次更新写坏的。所以文件本身先看一眼，别只看某个值读没读到。
    if os.path.exists(_mw_yaml) and os.path.getsize(_mw_yaml) < 200:
        _hc("MediaWarp 配置", "bad",
            f"{RED}文件是空的（{os.path.getsize(_mw_yaml)} 字节）{RST}")
        todo.append((
            "mediawarp 的配置文件被写空了 —— 容器现在还在跑（配置在内存里），"
            "但只要它重启一次就整个起不来，表现是所有片子都放不了",
            "跑一次「7 更新」重新生成。生成失败的话看那一步的报错"))
    _short = {drv: LINK_LIFE_MIN[str(drv).lower()]
              for _mp, drv, _s, _r, _m in openlist_storages(d)
              if str(drv).lower() in LINK_LIFE_MIN}
    _shortest = sorted(_short, key=lambda k: _short[k])
    if _ttl_cur and _ttl_cur != _ttl_want:
        _hc("直链缓存", "bad",
            f"{RED}缓存 {_ttl_cur}，但{'、'.join(_shortest) or '已挂的盘'}的直链"
            f"只活 {min(_short.values()) if _short else '?'} 分钟{RST}")
        todo.append((
            f"直链缓存（{_ttl_cur}）比直链本身的有效期还长 —— MediaWarp 会把"
            f"【已经过期】的地址 302 给播放器，播放器报 load fail / 打不开。"
            f"刚放过的片子能再放（地址还新），搁一阵子的就不行 —— "
            f"表现正是「刚挂好能放，过一会儿又放不了」",
            f"跑一次「7 更新」，它会按已挂的盘把这个值重算成 {_ttl_want}"))
    elif _ttl_cur:
        _hc("直链缓存", "ok", f"{_ttl_cur}"
            + (f"  {DIM}按 {'、'.join(_shortest)} 的直链有效期定的{RST}"
               if _shortest else f"  {DIM}没有短命直链的盘{RST}"))

    st302, msg302 = probe_302(key, own_host, _want)
    _hc("302 直链", st302, msg302)
    if st302 == "bad":
        if "不是 302" in msg302:
            todo.append(("MediaWarp 没有拦截播放请求，视频会经过本机中转",
                         "检查 mediawarp/config.yaml 的 http_strm.enable 是不是 true、"
                         f"prefix_list 是不是 {STRM_PATH}；「7 更新」会重新生成"))
        elif "内部地址" in msg302:
            # 【别一口咬定 public_url】这条以前只有那一个说法，而实测撞到的是
            # 另一个原因：被测的那部片在 WebDAV 挂载上，那类驱动在网盘侧根本
            # 没有 CDN 直链，OpenList 只能回自己的地址 —— 改多少次 public_url
            # 都没用，而其它盘（115、夸克）当时是好的。所以先看上面「换直链」
            # 那几行：某个盘单独红，就是那个盘的驱动决定的，不是配置错了。
            todo.append(("302 指向了本机地址，播放器连不上",
                         "先对照上面「换直链」那几行：如果只有某一个盘的直链是"
                         "本机地址，那是那个盘的驱动（WebDAV、本地目录这类）没有"
                         "CDN 直链，改配置没用；如果所有盘都这样，才是 autofilm 的"
                         "public_url 填成了内部地址，「7 更新」会重新生成"))
        else:
            todo.append(("302 没生成", "多半是上一行的换直链失败，等线路恢复再试"))

    # ---- 直链方式 / 证书 ----
    lms = link_method_storages(d)
    if lms:
        cur = lms[0][3]
        _hc("直链方式", "ok", f"{LINK_METHODS.get(cur, (cur,))[0]}"
                             f"{DIM}（卡顿就去 4 挂载路径 → 选那个盘 → 2 切换）{RST}")
    if key:
        _hc("Emby API Key", "ok", "已填")
    else:
        _hc("Emby API Key", "bad", "空 —— 302 不会生效")
        todo.append(("MediaWarp 没有 Emby API Key", "3 后补参数 → 1 添加 API 密钥"))

    # 【负载和内存要一起看】实测撞过一次，而我第一时间判断错了：看到"扫库 + 卡死"
    # 就归因给 Emby 刮削吃内存，可 docker stats 摆出来是 emby 280 MiB、
    # mediawarp 969 MiB、六个容器合计才 1.6 GB —— 内存不是被 Emby 吃的，
    # 而 CPU 那会儿是 100%(2 核)。CPU 打满时什么都卡，跟内存够不够无关。
    # 所以这两个必须并排报，只报一个会把人引向错的方向。
    try:
        _la = os.getloadavg()[0]
        _nc = os.cpu_count() or 1
        _r = _la / _nc
        _st = "ok" if _r < 1.0 else ("warn" if _r < 2.0 else "bad")
        _hc("负载", _st, f"{_la:.2f} / {_nc} 核（每核 {_r:.2f}）"
                        + ("" if _st == "ok" else
                           f"   {YELLOW}CPU 打满时播放会卡，和内存无关{RST}"))
        if _st == "bad":
            todo.append((f"负载 {_la:.2f}、只有 {_nc} 个核 —— 这时候播放卡顿、"
                         f"界面转圈都是 CPU 排队造成的，不是链路问题",
                         "docker stats 看是谁在烧 CPU。刮削和扫描是常见来源，"
                         "等它跑完就会回落；一直不回落才需要查"))
    except (OSError, AttributeError):
        pass

    # 【内存要单独报】实测撞过：库涨到 3 万多条目后 Emby 刮削把 3.8 GB 吃到只剩
    # 300 MB，OpenList 和 MediaWarp 跟着挨饿，表现是「视频都看不了」—— 而链路那
    # 一组全是绿的，因为链路本身没坏，是整台机器没内存了。
    # 这正是这个体检要抓的「看起来正常、实际是废的」，而且它只在大库上才出现。
    try:
        _mi = {}
        for _ln in open("/proc/meminfo"):
            _k, _, _v = _ln.partition(":")
            _mi[_k] = int(_v.split()[0]) // 1024          # MiB
        _tot = _mi.get("MemTotal", 0)
        _av = _mi.get("MemAvailable", 0)
        # 【必须报 MemAvailable，不能报 used】面板（GreenCloud 那种）算的是
        # used，把 buff/cache 也算进去，于是常年显示 80%+ 吓人；反过来也踩过 ——
        # 我拿"缓存假象"解释过一次真的爆内存，结果 buff/cache 只有 339 MB。
        # MemAvailable 是内核自己算的"现在还能拿去用多少"，缓存能回收的部分
        # 已经算在里面了，只认它。
        _swt, _swf = _mi.get("SwapTotal", 0), _mi.get("SwapFree", 0)
        if _tot:
            _pct = _av * 100 // _tot
            _n_strm = strm_count(d)
            _st = "ok" if _pct >= 25 else ("warn" if _pct >= 12 else "bad")
            # swap 吃满是个独立的坏信号：真到这一步，机器已经在拿硬盘当内存用，
            # 播放卡顿是必然的 —— 哪怕 MemAvailable 看着还有一点余量
            _sw = ""
            if _swt and _swf * 100 // _swt <= 10:
                _st = "bad"
                _sw = f"   {YELLOW}swap 也吃满了（{_swt} MiB）{RST}"
            _hc("内存", _st, f"可用 {_av} MiB / {_tot} MiB（{_pct}%）"
                             + (f"   {DIM}{_n_strm} 个 strm{RST}" if _n_strm else "")
                             + _sw)
            if _st != "ok":
                todo.append((f"内存只剩 {_pct}%（{_av} MiB）—— 这时候播放会失败，"
                             f"而链路各项还是绿的：不是链路坏了，是整台机器没内存了",
                             # 【别先入为主怪 Emby】实测那次就是这么判断错的：
                             # 一看"扫库 + 卡死"就归因给刮削，而 ps 摆出来真凶是
                             # 十个叠在一起的 cron 进程。所以这里给的是分辨的办法，
                             # 不是结论 —— 容器和宿主机进程都要看一眼。
                             "先看下面「后台在跑」里那行「任务并发」。然后两条一起跑，"
                             "谁大谁是凶手：docker stats --no-stream 看容器，"
                             "ps -eo rss,comm --sort=-rss | head 看宿主机进程"))
    except (OSError, ValueError):
        pass

    _hc_group("片子对不对", "链路是通的，但库里的东西可能不对")

    # ---- strm / 媒体库 ----
    n = strm_count(d)
    _hc("strm 文件", "ok" if n else "bad", f"{n} 个" if n else "0 个 —— Emby 里一定是空的")
    if not n:
        todo.append(("一个 strm 都没有", "先确认上面的列目录正常，再点「5 生成媒体库」"))
    if key:
        try:
            u = (f"http://127.0.0.1:8096/Library/VirtualFolders?api_key={key}")
            with urllib.request.urlopen(u, timeout=20) as resp:
                libs = json.load(resp)
            hit = any(is_strm_lib(lb) for lb in libs)
            # 【光"沾边"不够】。原来这一行用字符串前缀判"有没有库和 strm 根目录
            # 沾边"，于是只指向子目录的库照样打勾 —— 实测那台机器两个库分别指向
            # /data/strm/cloud/<某剧> 和 …/<某目录>，新加的「某电影 (2004) …」
            # 落在两库之外，Emby 从没扫过它，而这一行从头到尾是绿的。
            # 「看起来正常、实际是废的」，正是这个体检要防的东西。
            uncov = strm_dirs_uncovered(d, key) if hit else []
            if not hit:
                _hc("Emby 媒体库", "warn",
                    f"{len(libs)} 个库  {YELLOW}没有指向 {STRM_PATH}{RST}")
                todo.append((f"Emby 里没有指向 {STRM_PATH} 的媒体库",
                             f"Emby → 设置 → 媒体库 → 添加，路径填 {STRM_PATH}"))
            elif uncov:
                _hc("Emby 媒体库", "bad",
                    f"{len(libs)} 个库  {RED}{len(uncov)} 个文件夹没被任何库覆盖{RST}"
                    f"\n{' ' * 27}{DIM}{'、'.join(uncov[:4])}"
                    f"{'…' if len(uncov) > 4 else ''}{RST}")
                todo.append((f"strm 根目录下有 {len(uncov)} 个文件夹不在任何媒体库范围内"
                             f"（{'、'.join(uncov[:3])}）—— 里面的片子 Emby 永远扫不到，"
                             f"而且不会有任何报错",
                             f"库的路径指的是子目录。要么把某个库改成 {STRM_PATH} "
                             f"（以后新片自动进库），要么给这些文件夹各加一个库"))
            else:
                _hc("Emby 媒体库", "ok", f"{len(libs)} 个库")

            # ---- 库里有 strm，Emby 却一个条目都没认出来 ----
            # 【和"空壳库"是相反的两回事】空壳库是目录真空了；这个是目录里有片子，但 Emby 按
            # 这个库的内容类型解析不出任何条目，库在界面上显示「未找到项目」。
            # 实测这一例：动漫库是 tvshows 类型，而网盘里是「动漫/某剧 [第12集].mp4」这样两个
            # 文件散在库根目录。tvshows 要的是「剧名/Season 01/剧名 - S01E01.mp4」，散着放它一个
            # 都不认 —— 而 strm 文件、路径、权限全都对，体检其它每一项都是绿的。
            noitem = []
            for _lb in libs:
                _ls = [p for p in (_lb.get("Locations") or []) if _under(p, STRM_PATH)]
                if not _ls:
                    continue
                n_strm = n_item = 0
                for p in _ls:
                    rel = [x for x in p[len(STRM_PATH):].split("/") if x]
                    for _dp, _dn, _fs in os.walk(
                            os.path.join(strm_root(d), STRM_SUBDIR, *rel)):
                        n_strm += sum(1 for f in _fs if f.endswith(".strm"))
                if not n_strm:
                    continue                     # 归下面「空壳媒体库」管
                try:
                    _r = _emby(f"/Items?Recursive=true&ParentId={_lb.get('ItemId')}"
                               f"&IncludeItemTypes=Movie,Episode&Limit=1", key,
                               timeout=20) or {}
                    n_item = int(_r.get("TotalRecordCount") or 0)
                except Exception:
                    continue
                if not n_item:
                    noitem.append((_lb.get("Name") or "?",
                                   (_lb.get("CollectionType") or ""), n_strm))
            if noitem:
                _nm, _ct, _ns = noitem[0]
                _hc("库里认不出片子", "bad",
                    "、".join(f"{n}（{_ns2} 个 strm）" for n, _c, _ns2 in noitem)
                    + f"  {RED}Emby 一个条目都没认出来{RST}")
                todo.append((
                    f"媒体库「{_nm}」底下有 {_ns} 个 strm，但 Emby 里一个条目都没有 —— "
                    f"界面上显示「未找到项目」",
                    ("这个库是【电视剧】类型，而电视剧要求网盘里是"
                     "「剧名/Season 01/剧名 - S01E01.mp4」这种结构；"
                     "几个文件散在根目录 Emby 一个都不认。"
                     "要么在网盘里按这个结构整理，要么把这个库改成【电影】类型"
                     "（规则文件里把 type 改成 movies）")
                    if _ct == "tvshows" else
                    ("文件名 Emby 解析不出片名。改成「片名 (年份).mp4」这种，"
                     "带发布组标记的（[BT]xxx.1080p.WEB-DL-YYY）它认不出来")))

            # ---- 刮削器名单 ----
            # 【把每个库勾了什么【列出来】，不要只报"有问题/没问题"】这一项原来只在两种明确
            # 坏掉的形态下说话，别的时候一句"各库都配了刮削器"带过。而用户真正想确认的是"我这个
            # 库到底挂没挂上 MetaTube"—— 这个问题它答不了。
            #
            # 【赋予】是配置，【刮出来】是结果。我之前一直拿条目详情里的「数据库链接」当证据，
            # 那是结果不是配置：一个库明明勾了 MetaTube，只要文件名查不到，那一行就不会出现它。
            # 所以这一行现在直接摊开配置：每个库勾了哪几个，一眼可查。
            _rows, nofetch, mtwrong = [], [], []
            for _lb in libs:
                if not any(_under(p, STRM_PATH) for p in (_lb.get("Locations") or [])):
                    continue
                _nm = _lb.get("Name") or "?"
                # 顺序就是优先级，排前面的先查 —— 去重时不能打乱
                _fs = _fetcher_names(_lb.get("LibraryOptions"))
                if not _fs:
                    # 【别再区分"名单为空"和"名单在但没勾"了】前者曾被写成"跟随 Emby 默认"并
                    # 报绿，而用户点进 Emby 的库编辑页，两种情况看到的是同一屏：六个列表一个勾
                    # 都没有。对用户来说它们就是一回事，照实说。
                    # 【把内容类型一起打出来】刮削器配不上的头号原因就是它：手工建库选了「混合
                    # 内容」或者没选，CollectionType 是空的，按它问不到任何默认名单。
                    nofetch.append(_nm)
                    _ct2 = (_lb.get("CollectionType") or "")
                    _rows.append((_nm, f"{RED}一个都没勾{RST}"
                                       f"{DIM}（内容类型 {_ct2 or '空 —— 多半就是它'}）{RST}"))
                else:
                    if _fs == [METATUBE_FETCHER]:
                        mtwrong.append(_nm)
                    _rows.append((_nm, "、".join(_fs)))
            if _rows:
                _st = "bad" if (nofetch or mtwrong) else "ok"
                _hc("刮削器", _st, f"{len(_rows)} 个库{DIM}（按优先级，"
                                   f"排前面的先查）{RST}")
                for _nm, _txt in _rows:
                    print(f"      {pad(_nm[:12], 14)}{_txt}")
            if nofetch or mtwrong:
                _bad = nofetch + mtwrong
                todo.append((
                    f"媒体库「{_bad[0]}」的刮削器名单不对 —— "
                    f"{'一个刮削器都没启用' if _bad[0] in nofetch else '只剩 MetaTube，TheMovieDb 被挤掉了'}，"
                    f"表现就是「条目都在、一张海报都没有」",
                    "跑一次「5 生成媒体库」会按规则文件把默认刮削器写进去 —— "
                    "只对规则文件里有名字的库生效。自己在 Emby 里另建的库归你自己管："
                    "Emby → 设置 → 媒体库 → 点该库 → 手动勾"))

            # ---- 空壳媒体库 ----
            # 【删了 strm 不等于删了条目】Emby 的条目活在它自己的数据库里，只有扫描时发现文件
            # 没了才会删 —— 而扫描要库还在、路径还在。把一个网盘从扫描范围里去掉、strm 清光
            # 之后，那个库指着一个空目录杵在 Emby 里，首页轮播和「继续观看」照样推它的片，海报
            # 缓存也还占着盘。用户能看见的只有"垃圾还在"，看不出它在哪儿、为什么清不掉。
            empty = []
            for _lb in libs:
                _ls = [p for p in (_lb.get("Locations") or []) if _under(p, STRM_PATH)]
                if not _ls:
                    continue                # 用户自己的本地库，不归这儿管
                n_strm = 0
                for p in _ls:
                    rel = [x for x in p[len(STRM_PATH):].split("/") if x]
                    for _dp, _dn, _fs in os.walk(
                            os.path.join(strm_root(d), STRM_SUBDIR, *rel)):
                        n_strm += sum(1 for f in _fs if f.endswith(".strm"))
                if not n_strm:
                    empty.append(_lb.get("Name") or "?")
            if empty:
                _hc("空壳媒体库", "warn",
                    f"{'、'.join(empty)}  {YELLOW}目录里一个 strm 都没有{RST}")
                todo.append((
                    f"媒体库「{empty[0]}」指向的目录已经空了，但 Emby 里的条目还在 —— "
                    f"首页轮播、「继续观看」还会推这些片，点开必然放不了",
                    f"脚本自己建的库，跑「5 生成媒体库」时会去问网盘 —— "
                    f"网盘明确回答那儿没东西了就自动删掉，问不出来（超时、"
                    f"存储掉线）就留着等下一轮。你手建的不碰，"
                    f"到 Emby → 设置 → 媒体库 → 「{empty[0]}」→ 删除。"
                    f"要是这个库还想留着，就把对应的网盘加回扫描路径"))

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

            # ---- 进度条记忆 ----
            # 这是本项目最容易复发、也最难自查的一项：用户看到的永远是"看完退出来，下次点进去
            # 从头开始"，而背后是两个完全不同的原因，光看现象分不出来。
            #   ① 条目没有时长（RunTimeTicks=0）。Emby 按时长的百分比判断续播点，分母为 0 整套
            #      逻辑失效 —— 直接判定看完、清掉续播点。新生成的条目都是这个状态
            #   ② 媒体库的续播门槛还是默认值（120 秒）。一分多钟的片子播放位置永远到不了 120 秒，
            #      于是永远没有记忆 —— 长的记得住、短的记不住
            # ② 尤其阴险：门槛是【每个媒体库】各自一份的，用户新建一个库它就是默认值，之前调好
            # 的那次不会自动惠及后来建的库。
            slibs = [lb for lb in libs
                     if is_strm_lib(lb)]
            stale = {}
            for lb in slibs:
                o = lb.get("LibraryOptions") or {}
                off = [k for k, v in STRM_LIB_OPTIONS.items() if o.get(k) != v]
                if off:
                    stale[lb.get("Name") or "?"] = off
            if slibs and stale:
                # 两类选项分开报：一类只影响短片子的记忆，一类会让片子少掉、
                # 顺带把进度条弄坏。混成一句"设置不对"用户不知道自己中的是哪个
                names = "、".join(stale)
                allk = {k for v in stale.values() for k in v}
                what = []
                if allk & {"MinResumeDurationSeconds", "MinResumePct"}:
                    what.append("续播门槛还是默认值（短片子不会有记忆）")
                if allk & {"EnableMultiVersionByFiles", "EnableMultiVersionByMetadata"}:
                    what.append("多版本合并没关（名字相近的片子会被并成一部，进度条也会坏）")
                _hc("媒体库选项", "bad", f"{names}  {YELLOW}{'；'.join(what)}{RST}")
                todo.append((f"媒体库「{names.split('、')[0]}」的选项还是 Emby 默认值，"
                             f"对网盘库不合适",
                             "点「5 生成媒体库」或「7 更新」会立刻调好；"
                             "不管的话每小时的预热任务也会跟上（最多等 1 小时）"))
            elif slibs:
                # 【这个 Emby 版本没有的选项，在这儿报一次就够】原来是每次更新
                # 对每个库各打一行，刷四五行一模一样的话，而且把库名印进了会被
                # 截图的输出里。它不是故障、也没法处理，属于"知道一下"级别的
                # 信息 —— 体检才是看这种东西的地方，附在已有的行后面，不占篇幅。
                _miss = ms_state().get("lib_opt_missing") or []
                _tail = (f"{DIM}　这个 Emby 版本没有：{'、'.join(_miss)}{RST}"
                         if _miss else "")
                _hc("媒体库选项", "ok",
                    f"续播 {RESUME_MIN_SECONDS} 秒/{RESUME_MIN_PCT}%、"
                    f"多版本合并已关{_tail}")

            # 【私密库要能一眼验证】"以为遮住了、其实没遮"是这个功能唯一会真出事
            # 的失败方式，而它不会自己冒出来 —— 用户得亲自拿另一个账号登一次才
            # 知道。所以这里直接把"谁还能看见"摊开。
            _pr = {r["name"] for r in (lib_rules(d)[0] if is_installed(d) else [])
                   if r.get("private")}
            if _pr:
                _pids = {lb.get("ItemId") for lb in libs
                         if (lb.get("Name") or "") in _pr}
                _leak = []
                try:
                    for _u in (_emby("/Users", key, timeout=20) or []):
                        _po = _u.get("Policy") or {}
                        if _po.get("IsAdministrator"):
                            continue
                        if _po.get("EnableAllFolders") or (
                                set(_po.get("EnabledFolders") or []) & _pids):
                            _leak.append(_u.get("Name") or "?")
                except Exception:
                    _leak = None
                if _leak is None:
                    _hc("私密媒体库", "skip", f"{'、'.join(sorted(_pr))}　问不到用户列表")
                elif _leak:
                    _hc("私密媒体库", "bad",
                        f"{'、'.join(sorted(_pr))}  {RED}这些账号还能看见："
                        f"{'、'.join(_leak)}{RST}")
                    todo.append((
                        f"标了 private 的媒体库，非管理员账号「{_leak[0]}」还能看见",
                        "跑一次「7 更新」或「5 生成媒体库」会自动收权限；"
                        "还不行就 Emby → 设置 → 用户 → 点该用户 → 取消勾选那个库"))
                else:
                    _hc("私密媒体库", "ok",
                        f"{'、'.join(sorted(_pr))}　只有管理员账号能看见")

            # 刮到同一个身份 = 共用观看进度。这个坏的是观看记录，比刮错标题严重得多，
            # 而且现象极具迷惑性（A 的续播点出现在 B 上，甚至超过 B 的总长）
            dup = shared_identity_items(key)
            if dup:
                names = "、".join(n for g in dup.values() for _i, n in g)[:60]
                _hc("刮削身份", "bad",
                    f"{len(dup)} 组条目刮到了同一部片  "
                    f"{YELLOW}它们共用观看进度{RST}")
                print(f"     {DIM}{names}{RST}")
                todo.append(("多个条目被刮成了同一部片，Emby 会让它们共用观看进度"
                             "（一个看过另一个也变成看过，续播点互相串）",
                             "进那些条目 → ⋯ → 识别 → 各自指到正确的片子；"
                             "网盘片子常常在 TMDb 上没有对应条目，那就把刮削身份清掉"))
            else:
                _hc("刮削身份", "ok", "没有条目撞身份")

            # ---- 一个刮削源都没认出来的条目 ----
            # 【和"撞身份"是相反的一头】那个是认得太狠、几个文件认成同一部；这个是谁都不认，
            # 条目上一个 ProviderId 都没有 —— 没有海报、没有简介、没有年份，界面上就是个灰方块。
            # 必须报出来，因为现象会把人引到错的地方：同一个文件夹里有的片有封面、有的没有，
            # 自然会怀疑"插件是不是只对一部分生效"。实际上刮削器是【整个库共用】的，差别在于
            # 文件名能不能被查到 —— TMDb 靠片名、MetaTube 靠番号，自己起的名字两边都查不到。
            try:
                _r = _emby("/Items?Recursive=true&IncludeItemTypes=Movie,Episode"
                           "&Fields=ProviderIds,Path&Limit=2000", key, timeout=30) or {}
                _noid = [i for i in (_r.get("Items") or [])
                         if _under(str(i.get("Path") or ""), STRM_PATH)
                         and not {k: v for k, v in (i.get("ProviderIds") or {}).items()
                                  if v and k.lower() != "trakt"}]
            except Exception:
                _noid = []
            if _noid:
                _names = [str(i.get("Name") or "?")[:16] for i in _noid[:3]]
                _hc("刮削结果", "warn",
                    f"{'、'.join(_names)}{'…' if len(_noid) > 3 else ''} 等 "
                    f"{len(_noid)} 个  {YELLOW}没有任何刮削源认出来{RST}")
                # 【剧集和电影的改名建议是【相反】的，不能一句话打发】
                # 上一版对所有条目都说"改成「片名 (年份).mp4」"。对剧集那是错的，
                # 而且会把事情弄得更糟：一集叫「231 4K.mp4」的动画，按电影的规矩
                # 改名之后 Emby 更认不出它属于哪部剧、第几集。
                # 剧集要的是季集编号，电影要的是片名和年份 —— 按条目类型分开说。
                _ep = sum(1 for i in _noid if i.get("Type") == "Episode")
                _mv = len(_noid) - _ep
                _how = []
                if _ep:
                    _how.append(
                        f"其中 {_ep} 个是【剧集】：Emby 靠季集编号认它们，"
                        "文件名里没有 SxxExx 就谁都认不出来。"
                        "网盘里理想的结构是「剧名/Season 01/剧名 - S01E01.mp4」；"
                        "不想建季文件夹的话，至少把文件名改成"
                        "「剧名 - S01E231.mp4」这种")
                if _mv:
                    _how.append(
                        f"其中 {_mv} 个是【电影】：改成「片名 (年份).mp4」，"
                        "成人片用番号命名（字母-数字那种）最准")
                todo.append((
                    f"{len(_noid)} 个条目一个刮削源都没匹配上 —— 没有海报、简介和年份，"
                    f"界面上是灰方块。刮削器是整个库共用的，所以这跟"
                    f"「有的片有封面有的没有」不矛盾：差别在文件名能不能被查到",
                    "TMDb 按片名查、MetaTube 按番号查，自己起的名字两边都没有对应"
                    "条目，配多少刮削器都查不出来。"
                    + "；".join(_how) + "。改完点「5 生成媒体库」"))
            elif key:
                _hc("刮削结果", "ok", "条目都刮到了信息")

            # ---- 剧集编号 ----
            # 【写下 ≠ 生效，必须在体检里看得见】用户几轮回来说"还是没变"，
            # 而脚本那边一路打印"已补上季集编号" —— 两边对不上话，因为
            # 核对只在「5 生成媒体库」里做，而用户看的是这里。
            if key:
                _eps = _episode_items(key)
                _nfo = count_episode_nfo(d)
                if _nfo and _eps is not None:
                    _off = episode_number_mismatch(d, _eps)
                    _rd = episode_nfo_reader_on(key, lib_rules(d)[0])
                    if not _off:
                        _hc("剧集编号", "ok",
                            f"{_nfo} 个条目的季集编号和应有的一致")
                    else:
                        _p, _i, _ws, _we, _gs, _ge = _off[0]
                        _hc("剧集编号", "bad",
                            f"{len(_off)}/{_nfo} 个 Emby 没采纳"
                            f"{DIM}（nfo 写 S{_ws:02d}E{_we:02d}，"
                            f"Emby 显示第 {_gs} 季第 {_ge} 集）{RST}")
                        # 【把 Emby 自己给出的可选项打出来】判断"本地读取器被
                        # 挤掉了"要有依据：这台机器上「元数据下载器（集）」到底
                        # 有哪些可选，只有 Emby 自己知道。没有这一行，这个结论
                        # 就永远停在推测上 —— 这一串问题上已经猜错过两次了。
                        _av = (_emby_avail_names(key, "tvshows")
                               .get(("Episode", "MetadataFetchers")) or [])
                        if _av:
                            print(f"      {DIM}Emby 给出的可选项（集）："
                                  f"{'、'.join(_av)}{RST}")
                        print(f"      {DIM}本地 Nfo 读取器："
                              + ("开着" if _rd else
                                 "这个 Emby 版本不提供 —— 所以编号改成由脚本"
                                 "直接写进条目，不走 .nfo")
                              + f"{RST}")
                        todo.append((
                            f"{len(_off)} 个剧集的季集编号不对 —— "
                            f"该是 S{_ws:02d}E{_we:02d}，"
                            f"界面上显示的是第 {_gs} 季第 {_ge} 集",
                            "点一次「5 生成媒体库」：脚本会走「编辑元数据」那个"
                            "接口把编号直接写进 Emby，不经过刮削器。"
                            "写完还是不对就到 Emby 里点这一集 → 编辑元数据手工填，"
                            "或者把这个库改成 episode_number: false"
                            "（编号交回刮削器，代价是按它的库分季）"))

            # ---- 关键词规则 ----
            # 【来源必须看得见】这一串问题上反复吃亏的就是"设置在哪、有没有
            # 生效"看不见。规则决定了建哪些库、用什么刮削器、什么语言，
            # 用错一份是全局性的，而从 Emby 界面上一点都看不出来。
            _rs = rules_source()
            _ru = rules_url_of(_rs)
            _rl, _rsrc = lib_rules(d)
            _lov = lib_rules_path(d, local=True)
            if os.path.exists(_lov):
                _hc("关键词规则", "warn",
                    f"本机覆盖文件盖过了链接{DIM}（{len(_rl)} 条）{RST}")
                print(f"      {DIM}{_lov}{RST}")
                todo.append((
                    "媒体库关键词规则用的是本机覆盖文件，不是「3 → 8」里选的链接",
                    f"老版本的 a/d 菜单写下的。要让链接生效就删掉它：rm {_lov}"))
            else:
                _hc("关键词规则", "ok",
                    ("自定义链接" if _rs == "custom" else "作者的")
                    + f"　{len(_rl)} 条")
                print(f"      {DIM}{_ru}{RST}")

            # ---- 剧集缩略图 ----
            # 【看起来"每集都有图"是假象】没有自己那张图的一集，Emby 拿整部剧
            # 的封面顶上 —— 不挨着比根本看不出来。所以这一行必须数出来。
            if key and _eps:
                _noimg = episodes_without_image(d, lib_rules(d)[0], key, _eps)
                _tot = sum(1 for i in _eps
                           if str(i.get("Path") or "").endswith(".strm"))
                if _noimg and _tot:
                    _hc("剧集缩略图", "warn",
                        f"{len(_noimg)}/{_tot} 个没有自己的图"
                        f"{DIM}（界面上顶着剧集海报，看着像有图）{RST}")
                    todo.append((
                        f"{len(_noimg)} 个剧集没有自己的缩略图 —— "
                        f"界面上显示的是整部剧的封面",
                        "点一次「5 生成媒体库」会去刮分集图（只刮图，"
                        "不碰编号和片名）。刮削源那边本来就没有分集图的剧，"
                        "刮不回来也正常"))
                elif _tot:
                    _hc("剧集缩略图", "ok", f"{_tot} 个都有自己的图")

            nodur = items_without_duration(key)
            # 【留给「每日对齐」那一行用】那一行印的是上次跑完时记下的数字，
            # 印之前得先跟现在的实际情况对一下 —— 不然会出现「条目时长 ✔ 都有」
            # 和「还有 8 个没时长」同屏打架，用户没法判断该信哪个。
            live_nodur = len(nodur)
            if nodur:
                # 必须把片名列出来。只报个数字的话，用户看到"某个媒体库没有进度条
                # 记忆"会以为是那个库的设置没生效 —— 而实际上门槛早就调好了，
                # 缺的只是【某一部片子】的时长。一个是库的问题，一个是条目的问题，
                # 排查方向完全相反，光给数字分不出来。
                names = "、".join(n for _u, _i, n in nodur[:3])
                if len(nodur) > 3:
                    names += f" 等 {len(nodur)} 个"
                # 【必须说"还要多久"】只报个数字的话，人没法判断"它到底在不在补"——
                # 而它确实在补，只是每小时一批。不说清楚，看到两千多个只会以为没在跑。
                _per = min(max(HEAL_LIMIT, len(nodur) // 8), HEAL_LIMIT_MAX)
                _hrs = max(1, -(-len(nodur) // _per))    # 向上取整
                _hc("条目时长", "bad",
                    f"{names}  {YELLOW}没有时长，不会有进度条记忆{RST}")
                print(f"    {' ':<20}{DIM}后台每小时补一批（这次一批 {_per} 个），"
                      f"照这个速度还要约 {_hrs} 小时补完{RST}")
                todo.append((f"{len(nodur)} 个条目没探到时长，"
                             f"它们看到一半退出会被当成看完、下次从头开始",
                             f"【已经在自动补了】每小时一批、一批 {_per} 个，"
                             f"约 {_hrs} 小时补完，不用管它。想快一点就点"
                             "「5 生成媒体库」，它会另外在后台再补一批（最多 200 个）。"
                             "补一个要从网盘拉一段文件头，所以有意压着速度 —— "
                             "不限量实测一天能打掉 80 GB 流量"))
            elif slibs:
                _hc("条目时长", "ok", "都有")

            # strm 数和 Emby 条目数对不上，是"加了片子却不出来"的头号原因，
            # 而且 Emby 那边一声不吭。体检必须替它把这句话说出来。
            miss = strm_not_in_emby(d, key)
            if miss:
                _hc("Emby 收录", "bad",
                    f"{len(miss)} 个 strm 没被收进媒体库  "
                    f"{YELLOW}{os.path.basename(miss[0])}{RST}"
                    + (f" 等" if len(miss) > 1 else ""))
                todo.append((f"{len(miss)} 个 strm 生成了但 Emby 不认，"
                             f"表现是「网盘里加了片子，Emby 里不出来」",
                             "同一个文件夹里放多部片子时 Emby 只认其中一部；"
                             "在网盘里给每部片子单独建一个文件夹，再点「5 生成媒体库」"))
            elif slibs and n:
                _hc("Emby 收录", "ok", f"{n} 个 strm 都收进去了")
        except Exception as e:
            _hc("Emby 媒体库", "warn", _short_err(e))

    # MetaTube 是按番号刮成人片的。它出现在动画库/电影库的刮削器名单里，几乎肯定
    # 是装插件时被 Emby 默认加进去的，而不是用户的本意 —— 后果是那些库里冒出
    # JAV 封面。这种事必须主动报，用户不会想到去每个库翻刮削器名单。
    # 只陈述当前在哪些库生效，不判断对错 —— 开几个库是用户自己的事，体检的职责
    # 是让他看得见。真出问题（动画库冒 JAV 封面）时，这一行就是他要的那条线索
    if metatube_on(d) and key:
        mt_on = [n for n, _i, on, _o in metatube_libraries(key) if on]
        _hc("MetaTube 范围", "ok",
            "、".join(mt_on) if mt_on else f"{DIM}所有媒体库都没启用{RST}")

    # 【只报不改】看起来是剧集、却待在电影类型的库里。后果很具体：Emby 会把
    # 每一集当成一部独立电影，季集结构、剧集海报、"看到第几集"全都没有；
    # 而且同一个文件夹里名字相近的几集还容易被并成一部片的多个"版本"。
    # 不自动改是因为库的内容类型是【库级】设置，脚本没法按文件夹区分 ——
    # 要"自动"就得替用户新建或改媒体库，太越界。
    if key:
        _eps = episode_like_dirs(d)
        if _eps:
            _libs = emby_libs(key)
            _wrong = []
            for _p, _stem, _n in _eps:
                for _nm, _ps, _ct in _libs:
                    if any(_under(_p, _L) for _L in _ps):
                        if _ct == "movies":
                            _wrong.append((_stem, _n, _nm))
                        break
            if _wrong:
                _names = "、".join(f"{a}({b}集)" for a, b, _c in _wrong[:3])
                _hc("剧集布局", "warn",
                    f"{len(_wrong)} 组剧集在【电影】类型的库里"
                    f"\n{' ' * 27}{DIM}{_names}"
                    f"{'…' if len(_wrong) > 3 else ''}{RST}")
                todo.append((f"「{_wrong[0][0]}」看着是剧集（{_wrong[0][1]} 集），"
                             f"却在电影类型的库「{_wrong[0][2]}」里 —— "
                             f"每一集会变成一部独立电影，没有季集结构",
                             f"给它单独建一个【电视剧】类型的库指向那个文件夹。"
                             f"另外文件名要 Emby 解析得出集数才行，"
                             f"「剧名 - S01E12.mp4」这种最稳，中文「第12集」它常认不出"))
            else:
                _hc("剧集布局", "ok", f"{len(_eps)} 组剧集，都在电视剧库里")

    _hc_group("后台在跑", "这些是定时任务，红了不影响当下播放")

    # ---- 任务有没有叠罗汉 ----
    # 【这一项优先于下面所有】：它红了的时候，其它几项全是绿的 —— 保活"5 分钟前
    # 成功"、预热"已装"、对齐"跑过"，看上去无懈可击，而机器已经被自己的定时任务
    # 吃穿了内存。实测现场：十个进程 1.35 G、swap 满、视频放不了，体检从头绿到尾。
    # 分组上它属于"后台在跑"，但影响是实打实的播放中断，所以 todo 里按高优先给。
    tasks = running_tasks()
    if not tasks:
        _hc("任务并发", "ok", f"没有堆积{DIM}　同类任务同时只跑一个{RST}")
    else:
        by_sub = {}
        for _p, sub, age in tasks:
            by_sub.setdefault(sub, []).append(age)
        # 【正常和异常要用不同的写法】叠起来的时候「×N」是重点，没叠的时候它
        # 恒等于 ×1，写出来只是噪音 —— 而这一行挤，实测过长到把下一项顶到同一行。
        def _one(s, a):
            mins = a[0] // 60
            when = f"{mins} 分钟" if mins else f"{a[0]} 秒"
            return (f"{s}×{len(a)}（最久 {when}）" if len(a) > 1
                    else f"{s} 在跑 {when}")
        desc = "、".join(_one(s, a) for s, a in sorted(by_sub.items()))
        piled = [s for s, a in by_sub.items() if len(a) > 1]
        # 超时是按"下一次触发之前"设的，所以跑过头 = timeout 没生效（缺 coreutils，
        # 或者这进程是装 flock 之前起来的）
        overdue = [s for s, a in by_sub.items() if a[0] > CRON_TIMEOUT[s]]
        if piled or overdue:
            _hc("任务并发", "bad",
                f"{len(tasks)} 个后台任务同时在跑：{desc}"
                f"{DIM}（每个要占一百多兆内存）{RST}")
            todo.insert(0, (
                f"后台定时任务叠了 {len(tasks)} 个 —— 每个进程要占一百多兆内存，"
                f"堆多了会把内存和 swap 吃穿，表现就是「视频放不了」",
                "跑一次「7 更新」：会先杀掉卡住的，再给三条 cron 装上互斥锁和超时。"
                "急的话先手动清：pkill -f 'media-stack.py (keepalive|warm|sync)'"))
        else:
            _hc("任务并发", "ok", f"{desc}{DIM}　没有堆积{RST}")

    # ---- 保活 ----
    ka = keepalive_state(d)
    if not os.path.exists(KEEPALIVE_CRON):
        _hc("链路保活", "warn", "没装 —— 冷启动第一次播放会转圈几十秒")
        todo.append(("保活定时任务没装",
                     "跑一次「7 更新」会自动补上"))
    elif not ka:
        _hc("链路保活", "skip", f"已装，还没跑过（每 {KEEPALIVE_MIN} 分钟一次）")
    else:
        mins = int((time.time() - ka.get("ts", 0)) / 60)
        st, note = _stale_note(mins, KEEPALIVE_MIN, f"{KEEPALIVE_MIN} 分钟")
        if ka.get("ok"):
            _hc("链路保活", st,
                f"{mins} 分钟前成功，耗时 {ka.get('elapsed', 0)} 秒{note}")
            if st == "bad":
                todo.append((
                    f"链路保活该每 {KEEPALIVE_MIN} 分钟跑一次，实际已经 "
                    f"{mins // 60} 小时没跑了 —— 定时任务没在工作",
                    "先看 cron 装没装：cat /etc/cron.d/media-stack-keepalive；"
                    "手动跑一次看报什么错："
                    f"python3 {os.path.realpath(__file__)} keepalive；"
                    "跑一次「7 更新」会按当前版本重装这三条 cron"))
        else:
            _hc("链路保活", "warn",
                f"{mins} 分钟前失败：{ka.get('error', '')[:40]}")

    if os.path.exists(WARM_CRON):
        wm = warm_state(d)
        if not wm:
            # 【装了但从没跑成】和"刚装上还没到点"长得一样，只能说"还没跑过"，
            # 但至少不能再打绿勾 —— 那次三条任务全被锁死时，就是这一行一直绿着
            _hc("直链预热", "skip",
                f"已装，还没跑过（每 {WARM_EVERY_H} 小时一次）")
        else:
            wmin = int((time.time() - wm.get("ts", 0)) / 60)
            st3, note3 = _stale_note(wmin, WARM_EVERY_H * 60,
                                     f"{WARM_EVERY_H} 小时")
            when3 = f"{wmin // 60} 小时前" if wmin >= 60 else f"{wmin} 分钟前"
            _hc("直链预热", st3,
                f"{when3}热过「继续观看」和新加的片子"
                f"{DIM}（省掉点播放时的换直链等待）{RST}{note3}")
            if st3 == "bad":
                todo.append((
                    f"直链预热该每 {WARM_EVERY_H} 小时跑一次，实际已经 "
                    f"{wmin // 60} 小时没跑了 —— 新加的片子第一次点开要等换直链，"
                    f"新建的库也不会自动补时长和续播门槛",
                    "和「链路保活」多半是同一个原因（cron 没在工作）。"
                    "跑一次「7 更新」会重装这三条 cron"))
    else:
        _hc("直链预热", "warn", "没装 —— 隔一阵没看，第一次点播放要等换直链")
        todo.append(("直链预热没装，冷启动时第一次播放要等几秒到几十秒",
                     "跑一次「7 更新」会自动补上"))

    if os.path.exists(SYNC_CRON):
        # 光说"装了、排在几点"不够。用户第二天发现问题还在时，要能当场分辨
        # 是【没跑】还是【跑了但没修好】—— 这两种情况下一步做的事完全不同
        sy = sync_state(d)
        if not sy:
            _hc("每日对齐", "skip",
                f"已装，排在北京时间 {SYNC_HOUR_CST}，还没到点跑过")
        else:
            hrs = (time.time() - sy.get("ts", 0)) / 3600
            when = f"{hrs:.0f} 小时前" if hrs >= 1 else f"{hrs * 60:.0f} 分钟前"
            if not sy.get("ok"):
                _hc("每日对齐", "warn", f"{when}跑过但没跑完："
                                        f"{sy.get('error', '')[:40]}")
            else:
                did = []
                if sy.get("pruned"):
                    did.append(f"清了 {sy['pruned']} 个失效")
                fixed = sy.get("nodur_before", 0) - sy.get("nodur_after", 0)
                if fixed > 0:
                    did.append(f"补了 {fixed} 个时长")
                if sy.get("nodur_after"):
                    # live_nodur 是这次体检【当场数】出来的。上次跑完还差几个，
                    # 不代表现在还差 —— 中间每小时的对齐任务一直在补。
                    if live_nodur == 0:
                        did.append(f"{DIM}（那次跑完还差 {sy['nodur_after']} 个"
                                   f"时长，现在都齐了）{RST}")
                    else:
                        did.append(f"{YELLOW}还有 {live_nodur} 个没时长{RST}")
                if sy.get("missing"):
                    did.append(f"{YELLOW}{sy['missing']} 个没被 Emby 收录{RST}")
                st2, note2 = _stale_note(int(hrs * 60), 24 * 60, "天", late=1.5)
                _hc("每日对齐", st2,
                    f"{when}跑过  {'、'.join(did) if did else '没有需要处理的'}{note2}")
                if st2 == "bad":
                    todo.append((
                        f"每日对齐该一天跑一次，实际已经 {hrs:.0f} 小时没跑了 —— "
                        f"新建的媒体库、新加的片子不会自动补时长和续播门槛",
                        "和上面「链路保活」多半是同一个原因（cron 没在工作）。"
                        "跑一次「7 更新」会重装这三条 cron"))
    else:
        _hc("每日对齐", "warn", "没装 —— 新加的媒体库要手动点「5 生成媒体库」")
        todo.append(("每日自动对齐没装，新建媒体库的续播门槛不会自动跟上",
                     "跑一次「7 更新」会自动补上"))

    # ---- 网盘扫描（AutoFilm 自己的定时任务）----
    # 【这一行以前没有，而它才是"新片子会不会自己进来"的答案】上面那些定时任务都是
    # 脚本自己的 cron，唯独"去网盘里找新文件、生成 strm"这件事是 AutoFilm 按【它自己
    # 配置里的 cron】跑的，脚本这边一行都没报过。于是"我记得设过凌晨自动扫描，还在不在"
    # 这种问题，体检答不上来 —— 而这恰恰是最常被问到的一条。
    _af = os.path.join(d, "autofilm", "config", "config.yaml")
    _afc = ""
    try:
        _m = re.search(r"^\s*cron:\s*[\"']?([^\"'#\n]+)", open(_af, encoding="utf-8").read(),
                       re.M)
        _afc = (_m.group(1).strip() if _m else "")
    except OSError:
        pass
    if not _afc:
        _hc("网盘扫描", "warn", "读不到 AutoFilm 的定时配置 —— 新片子可能不会自己进来")
        todo.append(("AutoFilm 的定时扫描读不到，新加的片子不会自动进 Emby",
                     "跑一次「7 更新」会按当前版本重新生成它的配置"))
    else:
        _hc("网盘扫描", "ok" if "autofilm" in running else "bad",
            f"AutoFilm {cron_human(_afc)}"
            + ("" if "autofilm" in running else f"　{RED}容器没在跑{RST}")
            + f"　{DIM}新片子靠它变成 strm；等不及就点「5 生成媒体库」{RST}")

    # 【自动更新只管脚本】这一行要说清楚这个边界，否则用户看到"自动更新 ✔"
    # 会以为镜像也在自动跟，然后一年不点「7 更新」
    if os.path.exists(SELFUP_CRON):
        try:
            with open(os.path.join(d, "selfupdate.json")) as f:
                su = json.load(f)
        except Exception:
            su = {}
        if not su:
            _hc("脚本自动更新", "skip",
                f"已装，排在北京时间 {SELFUP_HOUR_CST}，还没到点跑过"
                f"{DIM}（只换脚本，镜像仍归「7 更新」）{RST}")
        else:
            _h = (time.time() - su.get("ts", 0)) / 3600
            _when = f"{_h:.0f} 小时前" if _h >= 1 else f"{_h * 60:.0f} 分钟前"
            _st, _note = _stale_note(int(_h * 60), 24 * 60, "天", late=1.5)
            if not su.get("ok"):
                _hc("脚本自动更新", "warn",
                    f"{_when}检查失败：{su.get('error', '')[:40]}")
                todo.append(("脚本自动更新拉不到最新版，仓库里修好的东西到不了这台机器",
                             "多半是网络问题，能等就等下一轮；急的话手动跑「7 更新」"))
            elif su.get("changed"):
                _hc("脚本自动更新", _st,
                    f"{_when}升到 v{su.get('to', '?')}"
                    f"{DIM}（只换脚本，镜像仍归「7 更新」）{RST}{_note}")
            else:
                _hc("脚本自动更新", _st,
                    f"{_when}检查过，已是最新"
                    f"{DIM}（只换脚本，镜像仍归「7 更新」）{RST}{_note}")
    else:
        _hc("脚本自动更新", "warn", "没装 —— 仓库里修好的东西要手动点「7 更新」才到")
        todo.append(("脚本自动更新没装，修复不会自己到这台机器上",
                     "跑一次「7 更新」会自动补上"))

    _hc_group("其它", "背景信息和到期提醒")

    # 版本必须看得见。全用 :latest 标签，「7 更新」每次都会拉最新的 —— 但用户
    # 无从知道自己手上是哪一版，也就没法判断某个毛病是不是升级带来的、或者
    # 已经被上游修掉了。能问出版本号的就报版本号，问不出的报镜像构建日期
    vers = stack_versions(read_emby_api_key(d) or "")
    if vers:
        _hc("版本", "ok", "  ".join(f"{k} {v}" for k, v in vers.items()))
        print(f"     {DIM}镜像都是 :latest，「7 更新」会拉最新版{RST}")

    # ---- 网盘授权令牌 ----
    # 看的是【长期凭据 refresh_token】，不是请求 URL 里那个几天就换一次的
    # access_token（那个驱动自己会续，见 storage_token_days 的注释）。
    # 实测这个长期凭据的有效期是一年量级，所以 14 天的提前量足够 —— 重新扫码
    # 需要人拿着手机操作，不能等到当天才说。
    for mp, days in storage_token_days(d):
        if days <= 0:
            _hc(f"授权 {mp}", "bad", f"{RED}已过期 {-days:.0f} 天{RST}")
            todo.append((f"{mp} 的网盘授权已过期 —— 目录还列得出来（读的是缓存），"
                         f"但点开任何文件都会转圈",
                         "OpenList → 存储 → 编辑该存储 → 重新扫码授权"))
        elif days < 14:
            _hc(f"授权 {mp}", "warn", f"{YELLOW}还剩 {days:.0f} 天{RST}")
            todo.append((f"{mp} 的网盘授权 {days:.0f} 天后到期",
                         "到期当天会突然打不开任何文件，且现象和线路故障一模一样。"
                         "趁早：OpenList → 存储 → 编辑该存储 → 重新扫码授权"))
        else:
            _hc(f"授权 {mp}", "ok", f"还剩 {days:.0f} 天")

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
            _hc("公网访问", "skip", f"还没有日志（下次「7 更新」刷新 nginx 配置后就有）")

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
        # 挂载路径单开一屏（原来埋在「后补参数」里，是个要手打整串路径的输入框）：
        # 加一个网盘是最常做的事之一，而且每个盘一个开关才是它的真实形态。
        if installed:
            _c0 = rebuild_cfg_from_disk(ms_install_dir())
            _n = len(_c0["scan_paths"])
            _sp = (f"{CYAN}{_n} 个目录{RST}" if _n else f"{YELLOW}一个都没选{RST}")
        else:
            _sp = f"{DIM}未安装{RST}"
        print(f"  4. 挂载路径（哪些网盘进 Emby·每个盘一个开关）  当前：{_sp}")
        print("  5. 生成媒体库（网盘挂好、或在网盘里整理过片子之后点这个）")
        print("  6. 链路体检（卡住 / 不出片子时先跑这个）")
        print("  7. 更新（拉最新镜像 + 按新版本刷新配置）")
        print("  8. 卸载")
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
            mount_paths_menu()
        elif c == "5":
            do_strm()
        elif c == "6":
            do_healthcheck()
        elif c == "7":
            do_update(from_menu=True)
        elif c == "8":
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
        # 三条 cron 子命令都先抢锁。cron.d 里已经用 flock -n 拦了一层，这里是
        # 第二层：flock 属于 util-linux，正常系统都有，但少了它就没人拦 ——
        # 而没人拦的后果实测过，是十个进程叠在一起把内存吃穿。锁在自己手里更稳。
        elif arg == "keepalive":          # cron 调的，安静跑，结果写 json
            if take_task_lock("keepalive"):
                do_keepalive()
        elif arg == "precache":           # cron 调的：开扫前刷一次目录缓存
            require_root()
            if take_task_lock("precache"):
                do_precache()
        elif arg == "sync":               # cron 调的每日对齐，同样不交互
            require_root()
            if take_task_lock("sync"):
                do_sync()
        elif arg == "warm":               # cron 调的直链预热
            require_root()
            if take_task_lock("warm"):
                do_warm()
        elif arg == "heal":               # 「4」扔后台的补时长，不交互
            require_root()
            if take_task_lock("heal"):
                do_heal()
        elif arg == "selfupdate":         # cron 调的自动更新，只换脚本
            require_root()
            if take_task_lock("selfupdate"):
                do_selfupdate()
        elif arg == "update":
            do_update()
        elif arg == "update-menu":        # 菜单里点的更新，且中途自我更新过；跑完回菜单
            do_update(from_menu=True)
            ask("\n按回车返回菜单...")
            main_menu()
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
