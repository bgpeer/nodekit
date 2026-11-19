#!/usr/bin/env python3
"""「8 更新」的三处回滚（1.5.431）：
  一、重启前再查一次有没有人在播：拉镜像要十几分钟，开头查的那次早过期了；
     期间有人开播了，自动更新这一轮不做，手动的问一声（默认不重启）。
  二、pull 失败就停住：不重渲染配置、不重启，配置的 bug 修复等下次拉得动时再来。
  三、prune 前留一份旧镜像（ms-backup 标签）；新镜像起不来指回旧镜像再起一次；
     重渲染前把旧配置备份到安装目录下的 .update-backup，重写中途出事放回去。"""
import importlib.util, os, sys, io, re, stat, tempfile, contextlib, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
src = open(SRC, encoding="utf-8").read()
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
os.environ["MS_SELF_UPDATED"] = "1"      # 跳过自我更新那一步（不联网）

T = tempfile.mkdtemp()
D = T + "/ms"
for sub in ("mediawarp/config", "autofilm/config"):
    os.makedirs(f"{D}/{sub}")
open(f"{D}/docker-compose.yml", "w").write("services:\n  emby:\n    image: emby/embyserver:latest\n")
open(f"{D}/.env", "w").write("PUID=0\n")
MW = f"{D}/mediawarp/config/config.yaml"
AF = f"{D}/autofilm/config/config.yaml"
CFG = {"install_dir": D, "domain": "", "has_domain": False, "homepage": False,
       "basic_auth": False, "emby_api_key": "", "crt": "", "key": ""}
RUN, SH = [], []
PULL, UP_FAIL, PLAY_SEQ, YN = [True], [False], [], []


class R:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def fake_run(cmd, **k):
    s = cmd if isinstance(cmd, str) else " ".join(cmd)
    RUN.append(s)
    if "docker compose" in s and "up -d" in s and "--remove-orphans" not in s:
        return R(1 if UP_FAIL[0] else 0)
    return R(0)


INSPECT = []
def fake_sh(cmd, check=False, timeout=None):
    SH.append(cmd)
    if "image inspect" in cmd:
        return R(0, (INSPECT.pop(0) if INSPECT else "") + "\n")
    return R(0)


m.ms_install_dir = lambda: D
m.is_installed = lambda d=None: True
m.read_emby_api_key = lambda d=None: "k"
m.stack_versions = lambda key="": {}
m._started_at = lambda name: (None, "t0")
m._versions_settle = lambda d, tries=8, gap=5: {}
m._print_version_diff = lambda b, a: None
m.pull_images = lambda compose, env_file: PULL[0]
m.someone_playing = lambda k, cache_s=0: PLAY_SEQ.pop(0) if PLAY_SEQ else False
def _ask(q, default=True):
    YN.append((q, default))
    return False
m.ask_yn = _ask
m.subprocess.run = fake_run
m.sh = fake_sh
m.rebuild_cfg_from_disk = lambda d: dict(CFG)
m.harden_secret_files = lambda d: 0
m.gen_mediawarp_conf = lambda cfg: "new-mw\n"
m.gen_autofilm_conf = lambda cfg: "new-af\n"
m.ensure_openlist_site_url = lambda d, cfg=None, quiet=False: False
m.ol_route_check = lambda d: None
m.metatube_on = lambda d: False
m.ol_init_status = lambda: True
m.install_cli = lambda d, quiet=True: None
m.reap_stale_tasks = lambda: 0
m.fetch_lib_rules = lambda d: False
m.dir_cache_auto_apply = lambda d: None
m.sync_hls_service = lambda d: None
m.sync_alitv_service = lambda d: None
m.sync_net_mtu = lambda d: False
m.install_keepalive = lambda d: None
m.install_route_cron = lambda: None
m.install_sync_cron = lambda d: None
m.install_warm_cron = lambda d: None
m.install_heal_cron = lambda d: None
m.sync_heal_daemon = lambda: None
m.sync_heal_gate = lambda **k: None
m.gate_fresh_restart = lambda k: None
m.traffic_install_cron = lambda: None
m.install_selfupdate_cron = lambda d: None
m.install_imgup_cron = lambda d: None
m.autofilm_schedule_fix = lambda d, fresh=False: None
m.normalize_strm_files = lambda d: 0
m.purge_node_rule = lambda d: None
m.read_yaml_scalar = lambda p, k: ""


def reset():
    import shutil
    shutil.rmtree(f"{D}/.update-backup", ignore_errors=True)
    open(MW, "w").write("old-mw\n")
    open(AF, "w").write("old-af\n")
    RUN.clear(); SH.clear()
    PULL[0] = True; UP_FAIL[0] = False
    PLAY_SEQ[:] = [False, False]
    YN.clear(); INSPECT[:] = ["sha256:old", "sha256:new"]


def run(auto=False):
    b = io.StringIO()
    with contextlib.redirect_stdout(b), contextlib.redirect_stderr(b):
        r = m.do_update(auto=auto)
    return r, plain(b.getvalue())


def upped():
    return any("docker compose" in s and "up -d" in s and "--remove-orphans" not in s for s in RUN)


def restarted(svc):
    return any(s == f"docker restart {svc}" for s in RUN)


print("一、重启前再查一次（拉镜像期间有人开始播）")
reset(); PLAY_SEQ[:] = [False, True]
r, out = run()
check("手动：有人开播了 → 不重启、不换镜像，返回 False", r is False and not upped()
      and "docker image prune -f" not in SH and open(MW).read() == "old-mw\n" and open(AF).read() == "old-af\n")
check("手动：问一声、默认不重启", YN == [("还是现在就重启换上新镜像？", False)] and "拉镜像期间有人开始看片" in out)
reset(); PLAY_SEQ[:] = [False, True]
r, out = run(auto=True)
check("自动：这一轮不做（None），不问", r is None and not YN and not upped())

print("二、pull 失败就停住")
reset(); PULL[0] = False
r, out = run()
check("返回 False，不 up、不重启、不 prune", r is False and not upped() and not RUN
      and "docker image prune -f" not in SH)
check("配置一个字没动，也没留备份目录", open(MW).read() == "old-mw\n" and open(AF).read() == "old-af\n"
      and not os.path.exists(f"{D}/.update-backup") and "配置没动" in out)

print("三、prune 前留一份旧镜像")
reset()
r, out = run()
check("换上了（True），旧镜像先打 ms-backup 标签、再 prune", r is True
      and "docker tag sha256:old emby/embyserver:ms-backup" in SH
      and SH.index("docker tag sha256:old emby/embyserver:ms-backup") < SH.index("docker image prune -f"))
check("没变的镜像不用留（只打一次标签）", SH.count("docker tag sha256:old emby/embyserver:ms-backup") == 1)
check("配置换成新的、容器重启了", open(MW).read() == "new-mw\n" and open(AF).read() == "new-af\n"
      and restarted("mediawarp") and restarted("autofilm"))

print("四、新镜像起不来：指回旧镜像再起一次")
reset(); UP_FAIL[0] = True
r, out = run()
check("返回 False，旧镜像指回原名、再 up 一次", r is False
      and "docker tag sha256:old emby/embyserver:latest" in SH
      and any("--remove-orphans" in s for s in RUN))
check("没 prune、配置没动、没留备份目录", "docker image prune -f" not in SH
      and open(MW).read() == "old-mw\n" and open(AF).read() == "old-af\n"
      and not os.path.exists(f"{D}/.update-backup") and "已指回旧镜像" in out)

print("五、重渲染前留备份，中途出事放回去")
reset()
r, out = run()
bdir = f"{D}/.update-backup"
check("备份里是旧配置、目录 0700", r is True and open(f"{bdir}/mediawarp/config/config.yaml").read() == "old-mw\n"
      and open(f"{bdir}/autofilm/config/config.yaml").read() == "old-af\n"
      and stat.S_IMODE(os.stat(bdir).st_mode) == 0o700)
reset()
REAL_WA = m.write_atomic
def boom_wa(path, text, mode=0o644):
    if path == AF:
        raise OSError("disk full")
    return REAL_WA(path, text, mode)
m.write_atomic = boom_wa
r, out = run()
m.write_atomic = REAL_WA
check("写 autofilm 配置时磁盘满：返回 False，两份配置都放回旧的", r is False
      and open(MW).read() == "old-mw\n" and open(AF).read() == "old-af\n" and "已放回更新前的配置" in out)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
