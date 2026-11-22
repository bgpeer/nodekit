#!/usr/bin/env python3
"""Homepage 导航页宿主机端口 3000 → 3001 → 2999（仓库主人 11/19：3000 跟 AdGuard Home 安装向导抢，
  向导绑不上、服务一起一停；11/20 再改成 2999）。只改导航页关联的端口：宿主机侧走 HOMEPAGE_PORT = 2999，
  容器里监听的还是 3000；Emby / OpenList / AdGuard 的端口一个都不动。
  11/22 真机：已装 3001 的机器上 nginx 一重生成就写 2999，容器还跑在 3001，首页 502 ——
  方向是已装机器跟脚本走：重生成时把容器迁到 2999（migrate_homepage_port），三处一致。
"""
import importlib.util, os, re, sys, tempfile, io, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
check("HOMEPAGE_PORT = 2999", m.HOMEPAGE_PORT == 2999)
home = [t for t in m.SUBDOMAINS if t[0] == "home"]
check("SUBDOMAINS 里 home 那行走 HOMEPAGE_PORT（显示的地址跟这张表走；nginx 反代直接读常量）",
      len(home) == 1 and home[0][1] == 2999 and home[0][2] == "homepage")
cfg = {"has_domain": True, "install_dir": "/tmp/ms-test", "homepage": True,
       "metatube": False, "emby": True, "openlist": True}
compose = m.gen_compose(cfg)
check("新装的 compose：宿主机 2999 → 容器 3000", '"127.0.0.1:2999:3000"' in compose)
check("新装的 compose：宿主机上不再有 3000:3000", '"127.0.0.1:3000:3000"' not in compose)
src = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
HP = '"homepage": f"http' + '://home' + 'page:{HOMEPAGE_CONTAINER_PORT}"'   # 拼出来，claude/ 里不留网址字面量
check("容器内健康检查走 HOMEPAGE_CONTAINER_PORT（容器里监听的还是 3000，别动）",
      HP in src)
# 3000 这个数字在 media-stack.py 里只许出现在：容器端口常量那一行、
# \u3000 全角空格的正则、EP_TRIED_MAX 这个重试次数——别的 3000 都是误改
others = [ln for ln in src.splitlines() if "3000" in ln
          and not ln.lstrip().startswith("#")          # 注释里提一句旧端口不算改
          and "HOMEPAGE_CONTAINER_PORT = 3000" not in ln
          and "\\u3000" not in ln and "EP_TRIED_MAX" not in ln]
check("没有别的 3000 被顺手改了", not others)
# 端口总控制：两个端口值各只有一处定义；旧端口 3001 在代码里一个字都不剩
check("HOMEPAGE_PORT 只定义一次",
      len(re.findall(r"(?m)^HOMEPAGE_PORT\s*=", src)) == 1)
check("HOMEPAGE_CONTAINER_PORT 只定义一次",
      len(re.findall(r"(?m)^HOMEPAGE_CONTAINER_PORT\s*=", src)) == 1)
_l2999 = [ln for ln in src.splitlines() if "2999" in ln]
check("2999 只出现在 HOMEPAGE_PORT 定义那一行",
      len(_l2999) == 1 and "HOMEPAGE_PORT" in _l2999[0])
check("3001 在 media-stack.py 里一个字都不剩", "3001" not in src)
agh = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("AdGuard 的 WEB_PORT 还是 3000（向导端口，跟导航页没关系，不动）",
      "WEB_PORT = 3000" in agh)

print("已装 3001 迁到 2999（11/22 真机首页 502 的修法）")
class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc; self.stdout = out; self.stderr = err
_real_sh, _real_sub, _real_rebuild = m.sh, m.subprocess, m.rebuild_cfg_from_disk
def _fake_sh(port_out, rc=0):
    def f(cmd, **k):
        if "docker port homepage" in cmd:
            return _R(rc, port_out)
        if cmd.startswith("nginx -t"):
            return _R(0, "", "")
        return _R(0, "", "")
    return f
def _mkd(port):
    d = tempfile.mkdtemp(prefix="ms-hp-")
    open(os.path.join(d, "docker-compose.yml"), "w").write(
        "services:\n  homepage:\n    ports:\n"
        f'      - "127.0.0.1:{port}:3000"\n')
    return d
def _stub_rebuild(tmp):
    m.rebuild_cfg_from_disk = lambda d: {"has_domain": True, "install_dir": tmp,
                                         "homepage": True, "metatube": False}
_calls = []
def _stub_sub(rc=0):
    _calls.clear()
    m.subprocess = type("S", (), {"run": lambda s, cmd, **k: (_calls.append(
        cmd if isinstance(cmd, str) else " ".join(cmd)), _R(rc, "", ""))[1]})()

# 1. 读容器发布端口：docker port 问出来的就是准的
m.sh = _fake_sh("127.0.0.1:3001\n")
check("容器跑在 3001：读出来是 3001", m.homepage_published_port() == 3001)
m.sh = _fake_sh("0.0.0.0:2999\n")
check("容器跑在 2999：读出来是 2999", m.homepage_published_port() == 2999)
m.sh = _fake_sh("")
check("容器不在：读不到是 None", m.homepage_published_port() is None)
def _boom(cmd, **k):
    raise RuntimeError("no docker")
m.sh = _boom
check("docker 问不到：不抛错，是 None", m.homepage_published_port() is None)
# 2. 读 compose 文件里的映射
d1 = _mkd(3001)
check("compose 写着 3001：读出来是 3001", m._compose_homepage_port(d1) == 3001)
d2 = _mkd(2999)
check("compose 写着 2999：读出来是 2999", m._compose_homepage_port(d2) == 2999)
check("没有 compose 文件：读不到是 None",
      m._compose_homepage_port(tempfile.mkdtemp(prefix="ms-hp-none-")) is None)
# 3. 迁：容器 3001 → compose 改成 2999 + 只重建 homepage
tmp = _mkd(3001); _stub_rebuild(tmp); m.sh = _fake_sh("127.0.0.1:3001\n"); _stub_sub()
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok_mig = m.migrate_homepage_port(tmp)
comp = open(os.path.join(tmp, "docker-compose.yml")).read()
check("迁：返回 True", ok_mig is True)
check("迁：compose 映射改成 2999", '"127.0.0.1:2999:3000"' in comp
      and '"127.0.0.1:3001:3000"' not in comp)
check("迁：只重建 homepage 这一个容器",
      len([c for c in _calls if "docker compose" in c]) == 1
      and any("up -d homepage" in c for c in _calls))
# 4. 本来就是 2999：什么都不动
tmp = _mkd(2999); _stub_rebuild(tmp); m.sh = _fake_sh("127.0.0.1:2999\n"); _stub_sub()
before = open(os.path.join(tmp, "docker-compose.yml")).read()
check("本来就是 2999：返回 True、不重建容器",
      m.migrate_homepage_port(tmp) is True and not _calls)
check("本来就是 2999：compose 一个字不动",
      open(os.path.join(tmp, "docker-compose.yml")).read() == before)
# 5. 容器已经是 2999、只有 compose 文件还写着 3001：只改文件，不重建
tmp = _mkd(3001); _stub_rebuild(tmp); m.sh = _fake_sh("127.0.0.1:2999\n"); _stub_sub()
check("只剩 compose 是旧的：返回 True、文件改成 2999、不重建容器",
      m.migrate_homepage_port(tmp) is True
      and not [c for c in _calls if "up -d" in c]
      and '"127.0.0.1:2999:3000"' in open(os.path.join(tmp, "docker-compose.yml")).read())
# 6. 容器不在：不做事、不报错
tmp = _mkd(3001); _stub_rebuild(tmp); m.sh = _fake_sh(""); _stub_sub()
check("容器不在：返回 True、compose 不动、不重建",
      m.migrate_homepage_port(tmp) is True and not _calls
      and '"127.0.0.1:3001:3000"' in open(os.path.join(tmp, "docker-compose.yml")).read())
# 7. up 失败：返回 False、明说没迁成
tmp = _mkd(3001); _stub_rebuild(tmp); m.sh = _fake_sh("127.0.0.1:3001\n"); _stub_sub(rc=1)
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok_mig = m.migrate_homepage_port(tmp)
check("up 失败：返回 False、屏上说清没迁成",
      ok_mig is False and "没迁成" in b.getvalue())
# 8. 端到端：apply_nginx_site 先迁容器、再写 nginx，三处一致
tmp = _mkd(3001); _stub_rebuild(tmp); m.sh = _fake_sh("127.0.0.1:3001\n"); _stub_sub()
m.nginx_reload = lambda: None
m.ua_spoof_mounts = lambda: []
m.hls_wanted = lambda d: False; m.hls_port = lambda: 0; m.hls_direct_on = lambda: False
m.heal_gate_ready = lambda: False
m.ali_tc_mounts = lambda: {}; m.pan115_mounts = lambda d: []
_real_makedirs = os.makedirs
os.makedirs = lambda *a, **k: None
m.NGX_SITE = os.path.join(tmp, "media-stack.conf")
_cfg = {"domain": "ys.example", "has_domain": True, "install_dir": tmp,
        "data_root": tmp + "/media", "homepage": True, "metatube": False,
        "emby": True, "openlist": True, "ol_user": "admin", "ol_pass": "p",
        "ba_user": "media", "ba_pass": "pw", "basic_auth": False,
        "host_ip": "1.2.3.4", "ngx_port": 443, "http2_directive": False,
        "crt": tmp + "/c.pem", "key": tmp + "/k.pem"}
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok_site = m.apply_nginx_site(_cfg)
os.makedirs = _real_makedirs
site = open(m.NGX_SITE).read()
comp = open(os.path.join(tmp, "docker-compose.yml")).read()
check("端到端：apply_nginx_site 返回 True", ok_site is True)
check("端到端：容器迁过去了（只重建 homepage）",
      len([c for c in _calls if "docker compose" in c]) == 1
      and any("up -d homepage" in c for c in _calls))
check("端到端：compose 是 2999", '"127.0.0.1:2999:3000"' in comp)
check("端到端：nginx 首页指 2999、不再指 3001",
      "proxy_pass http://127.0.0.1:2999;" in site
      and "127.0.0.1:3001" not in site)
# 9. 新装：gen_nginx_site 本来就是 2999，不用迁
m.sh, m.subprocess, m.rebuild_cfg_from_disk = _real_sh, _real_sub, _real_rebuild
m.ua_spoof_mounts = lambda: []
m.hls_wanted = lambda d: False; m.hls_port = lambda: 0; m.hls_direct_on = lambda: False
m.heal_gate_ready = lambda: False
m.ali_tc_mounts = lambda: {}; m.pan115_mounts = lambda d: []
site = m.gen_nginx_site(_cfg)
check("新装的 nginx 站点：首页指 2999",
      "proxy_pass http://127.0.0.1:2999;" in site
      and "127.0.0.1:3001" not in site)
# 10. 改常量就改全套：只换 HOMEPAGE_PORT 的值，compose 和 nginx 一起变
print("改常量就改全套（端口总控制）")
_old_hp = m.HOMEPAGE_PORT
m.HOMEPAGE_PORT = 2998
try:
    comp2 = m.gen_compose({"has_domain": True, "install_dir": "/tmp/ms-test",
                           "homepage": True, "metatube": False,
                           "emby": True, "openlist": True})
    site2 = m.gen_nginx_site(_cfg)
    check("改 HOMEPAGE_PORT：compose 跟着变",
          '"127.0.0.1:2998:3000"' in comp2 and '"127.0.0.1:2999:3000"' not in comp2)
    check("改 HOMEPAGE_PORT：nginx 跟着变",
          "proxy_pass http://127.0.0.1:2998;" in site2
          and "127.0.0.1:2999" not in site2)
finally:
    m.HOMEPAGE_PORT = _old_hp
check("改完恢复：常量还是 2999", m.HOMEPAGE_PORT == 2999)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
