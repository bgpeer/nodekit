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
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import time

SCRIPT_VERSION = "1.0.0"

# ---- 节点（xy-installer.py）的落盘状态，只读 ----
BGP_DIR    = "/etc/bgpeer"
STATE_FILE = BGP_DIR + "/state.json"        # 含 domain / sni_split 等
MS_STATE   = BGP_DIR + "/media-stack.json"  # 本脚本自己的：记住装在哪个目录
ACME_CRT   = "/etc/ssl/sb/acme.crt"         # 节点的 acme 证书（判断有没有真证书）

# ---- 本脚本自己的东西 ----
DEFAULT_DIR   = "/opt/media-stack"
NGX_SITE      = "/etc/nginx/conf.d/media-stack.conf"
HTPASSWD_FILE = "/etc/nginx/.media-stack.htpasswd"
CLI_PATH      = "/usr/local/bin/media-stack"
CLI_ALIAS     = "/usr/local/bin/emby"
SNI_HTTPS_PORT_FALLBACK = 8443              # 和 xy-installer.py 的常量一致

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


def save_ms_state(install_dir):
    """记住装在哪，「使用信息 / 更新 / 卸载」就不用每次问一遍安装目录。"""
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        with open(MS_STATE, "w") as f:
            json.dump({"install_dir": install_dir}, f, ensure_ascii=False, indent=2)
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

    parts.append("networks:\n  mediastack:\n    name: mediastack\n")
    return "\n".join(parts)


def gen_autofilm_conf(cfg):
    """AutoFilm：定时遍历 OpenList，把网盘里的视频写成 .strm 文本文件。

    mode 必须是 AlistURL 而不是 RawURL：RawURL 会把网盘的临时直链写死进 strm，
    而夸克这类直链只有几小时有效期，过期后整个媒体库集体播放失败。
    AlistURL 存的是 OpenList 的永久路径，播放时才由 MediaWarp 实时换取新鲜直链。
    """
    return f"""# 由 media-stack.py 自动生成。改完执行 docker restart autofilm 生效。
alist:
  - id: openlist
    base_url: http://openlist:5244
    public_url:
    username: {cfg['ol_user']}
    password: {cfg['ol_pass']}
    otp_code:
    token:
    wait_time: 0.2          # 每次请求间隔，夸克风控较严，别调成 0

alist2strm_tasks:
  - id: 网盘
    cron: "{cfg['strm_cron']}"
    alist: openlist
    source_dir: {cfg['cloud_mount']}
    target_dir: {STRM_PATH}
    mode: AlistURL          # 见上方注释，网盘场景不要改成 RawURL
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
      enabled: true         # 网盘删了文件，本地 strm 跟着删
      ignore:
      smart_protection:
        enabled: true       # 防止网盘抽风返回空目录时把整个媒体库误删
        threshold: 100
        grace_scans: 3
"""


def gen_mediawarp_conf(cfg):
    """MediaWarp：反代在 Emby 前面，拦截播放请求并 302 到网盘直链。"""
    return f"""# 由 media-stack.py 自动生成。改完执行 docker restart mediawarp 生效。
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
  http_strm_ttl: 1m
  alist_api_ttl: 10m
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

http_strm:
  enable: false
  proxy: false
  final_url: true
  compatibility_mode: false
  prefix_list: []

alist_strm:
  enable: true
  proxy: true
  # raw_url: false 表示 302 到 OpenList，再由 OpenList 跳到网盘（两跳），
  # 兼容性最好。想少一跳可改 true 让 MediaWarp 直接拿网盘直链，
  # 但网盘对请求头较敏感，改之前先确认能正常播放。
  raw_url: false
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
        icon = {"emby": "emby.png", "openlist": "alist.png"}.get(container, "")
        services.append(f"""    - {label}:
        icon: {icon}
        href: {url_for(sub, port)}
        description: {'影视播放' if container == 'emby' else '网盘挂载'}
        siteMonitor: {monitor.get(container, f'http://{container}:{port}')}""")
    widgets = """- resources:
    cpu: true
    memory: true
    disk: /
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
  update)  dc pull && dc up -d && echo "已更新到最新镜像" ;;
  strm)
    docker restart autofilm >/dev/null 2>&1 || { echo "autofilm 没在运行"; exit 1; }
    echo "已触发 strm 生成，Ctrl-C 可退出日志跟踪:"; docker logs -f --tail 50 autofilm ;;
  302)
    echo "跟踪 MediaWarp 日志。现在去播放一集，看到 302 就说明直链生效:"
    docker logs -f --tail 20 mediawarp ;;
  *) echo "未知命令:$1。看 media-stack help"; exit 1 ;;
esac
'''


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

    print(f"\n  {BOLD}▸ 容器状态{RST}")
    r = sh(f"docker compose -f {os.path.join(d, 'docker-compose.yml')} "
           f"--env-file {env_file} ps", timeout=60)
    out = (r.stdout or "").strip()
    print("\n".join("      " + ln for ln in out.splitlines()) if out
          else f"      {YELLOW}容器没在跑，用「3 更新」或 media-stack start 拉起来。{RST}")

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
        "host_ip":  "",     # 只有无域名模式才用得上，下面按需取
    }
    cfg["has_domain"] = bool(cfg["domain"]) and have("nginx")
    cfg["basic_auth"] = bool(cfg["ba_pass"]) and os.path.exists(HTPASSWD_FILE)
    cfg["ngx_port"] = detect_nginx_https_port()
    cfg["http2_directive"] = nginx_supports_http2_directive()
    cfg["crt"] = f"/etc/nginx/certs/{cfg['domain']}.crt" if cfg["domain"] else ""
    cfg["key"] = f"/etc/nginx/certs/{cfg['domain']}.key" if cfg["domain"] else ""
    if not cfg["has_domain"]:
        cfg["host_ip"] = public_ip()
    return cfg


def do_update():
    """更新：拉最新镜像 + 按当前脚本重新生成配置。用户数据和密码都不动。"""
    d = ms_install_dir()
    if not is_installed(d):
        warn(f"还没安装（{d} 下没有 docker-compose.yml）。先选 1 安装。")
        return
    compose = os.path.join(d, "docker-compose.yml")
    env_file = os.path.join(d, ".env")

    info("拉取最新镜像...")
    r = subprocess.run(f"docker compose -f {compose} --env-file {env_file} pull",
                       shell=True, timeout=1800)
    if r.returncode != 0:
        err("拉取镜像失败，看上面的报错。")
        return
    info("用新镜像重启容器...")
    r = subprocess.run(f"docker compose -f {compose} --env-file {env_file} up -d",
                       shell=True, timeout=900)
    if r.returncode != 0:
        err("重启失败，看上面的报错。")
        return
    ok("镜像已更新")
    sh("docker image prune -f", timeout=300)

    # 再把脚本生成的那几份配置按当前版本重刷一遍。
    # 只重刷「纯脚本产物」：nginx 站点、Homepage 导航。
    # mediawarp / autofilm 的配置里有 API Key、挂载路径、cron 这些用户填的东西，
    # 也可能被手改过，这里一律不碰 —— 更新不该悄悄覆盖用户的输入。
    cfg = rebuild_cfg_from_disk(d)
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

    print()
    ok("更新完成：镜像、nginx 站点、导航面板都已是当前版本")
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
    for p in (HTPASSWD_FILE, CLI_PATH, CLI_ALIAS, MS_STATE):
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
    cfg["cloud_mount"] = ask("网盘在 OpenList 里的挂载路径", "/quark")
    cfg["strm_cron"] = ask("strm 生成 cron（6 位：秒 分 时 日 月 周）", "0 0 5 * * *")
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
    save_ms_state(cfg["install_dir"])   # 记住装在哪，菜单里的 2/3/4 就不用再问

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
    print(f"  1) OpenList → 存储 → 添加 → 驱动选 {BOLD}QuarkTV{RST}（不是 Quark！）")
    print(f"     挂载路径填 {BOLD}{cfg['cloud_mount']}{RST} → 保存 → 用网盘手机 App 扫码")
    print(f"     → 扫完把该存储{BOLD}先禁用再启用{RST}，token 才会生效")
    print("  2) 打开 Emby 完成首次安装向导 → 设置 → 高级 → API 密钥 → 新建并复制")
    print("  3) 重跑本脚本，在「Emby API Key」那步粘贴进去")
    print("  4) media-stack strm  触发一次 strm 生成")
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
        print("  0. 返回")
        print("-" * 60)
        c = ask("请选择").strip()
        if c in ("0", ""):
            return
        if c == "1":
            set_emby_api_key()
        elif c == "2":
            set_web_credentials()
        else:
            print("无效选择。")
            continue
        ask("\n按回车返回...")


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
        print("  4. 更新（拉最新镜像 + 按新版本刷新配置）")
        print("  5. 卸载")
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
            do_update()
        elif c == "5":
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
