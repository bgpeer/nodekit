#!/usr/bin/env python3
"""第一次安装：`docker compose up -d` 失败就停住（1.5.431）。
钉住：
  一、容器没起来：不建账号（OpenList / Emby 那几步都不走）、不写 CREDENTIALS.txt，
     最后不报「部署完成」，只说失败原因和重跑一次。
  二、对照：起来了就照旧走完，凭据写好、「部署完成」。"""
import importlib.util, os, sys, io, re, tempfile, contextlib, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
src = open(SRC, encoding="utf-8").read()
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)

T = tempfile.mkdtemp()
D = T + "/ms"
UP_FAIL = [True]
CALLS = []


class R:
    def __init__(self, rc=0):
        self.returncode, self.stdout, self.stderr = rc, "", ""


def fake_run(cmd, **k):
    s = cmd if isinstance(cmd, str) else " ".join(cmd)
    CALLS.append(s)
    if "docker compose" in s and "up -d" in s:
        return R(1 if UP_FAIL[0] else 0)
    return R(0)


m.DEFAULT_DIR = D
m.ms_install_dir = lambda: D
m.is_installed = lambda d=None: False
m.require_root = lambda: None
m.ensure_docker = lambda: None
m.node_state = lambda: {}
m.ask_domain = lambda prompt, default: ""
m.ask = lambda prompt, default="": default     # 回车 = 用默认值，跟真 ask 空输入的行为一样
m.public_ip = lambda: "1.2.3.4"
m.have = lambda b: False
m.read_emby_api_key = lambda d=None: ""
m.keep_or_new = lambda *a, **k: "OlPass123"
m.resolve_scan_paths = lambda d, spec: []
m.save_emby_api_key = lambda d, k: None
m.metatube_preinstall = lambda cfg: cfg.update(metatube=True, metatube_files=[], metatube_ver="")
m.gen_compose = lambda cfg: "services: {}\n"
m.gen_autofilm_conf = lambda cfg: "af\n"
m.gen_mediawarp_conf = lambda cfg: "mw\n"
m.gen_homepage_conf = lambda cfg: ("s\n", "sv\n", "w\n")
m.sh = lambda cmd, **k: R(0)
m.subprocess.run = fake_run
m.ol_init_status = lambda: True
m._ol_login = lambda u, p: True
m.ol_init_setup = lambda *a, **k: CALLS.append("ol_init_setup") or ""
m.emby_first_setup = lambda *a, **k: CALLS.append("emby_first_setup") or ("", "no")
m._ol_account_apply = lambda d: CALLS.append("ol_account_apply")
m.install_cli = lambda d, quiet=False: None
m.mark_drive_defaults_empty = lambda d: None
m.install_keepalive = lambda d: None
m.install_route_cron = lambda: None
m.install_sync_cron = lambda d: None
m.install_warm_cron = lambda d: None
m.install_heal_cron = lambda d: None
m.sync_heal_daemon = lambda: None
m.sync_heal_gate = lambda **k: None
m.traffic_install_cron = lambda: None
m.save_ms_state = lambda *a, **k: None
m.build_summary = lambda cfg, colored=True: "summary"


def run():
    CALLS.clear()
    b = io.StringIO()
    with contextlib.redirect_stdout(b), contextlib.redirect_stderr(b):
        m.main()
    return plain(b.getvalue())


print("一、容器起不来就停住")
UP_FAIL[0] = True
out = run()
check("报容器启动失败，不报「部署完成」", "容器启动失败" in out and "部署完成" not in out)
check("不写 CREDENTIALS.txt", not os.path.exists(f"{D}/CREDENTIALS.txt"))
check("不建账号：OpenList / Emby 那几步都没走", "ol_init_setup" not in CALLS and "emby_first_setup" not in CALLS)
check("配置文件已经写好了（修好重跑就行）", os.path.exists(f"{D}/docker-compose.yml") and os.path.exists(f"{D}/.env"))
check("源码里：失败分支 return 在建账号之前", src.index('tip("容器没起来，这次安装没完成') < src.index('设置 OpenList 管理员账号'))

print("二、对照：起来了就照旧走完")
UP_FAIL[0] = False
out = run()
check("报「部署完成」、凭据写好", "部署完成" in out and os.path.exists(f"{D}/CREDENTIALS.txt")
      and open(f"{D}/CREDENTIALS.txt").read().startswith("summary"))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
