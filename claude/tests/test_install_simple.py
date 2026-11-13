#!/usr/bin/env python3
"""第一次安装只问域名和三组账号密码（仓库主人 11/12 重装时：「这个路径扫描不要出现在刚开始安装里面……这个路径谁知道是什么
  路径啊刚开始都懵逼了，刚开始安装就应该是设置网页入口帐号密码和emby帐号密码挂载帐号密码，设置好了直接就安装成功了，
  然后提示叫人家去挂路径」）。

  原来第一次装要答十几问：安装目录、媒体目录、时区、PUID、PGID、Homepage、扫描路径、cron、要不要自动配 nginx……
  1.5.415：第一次装这些全用默认值不问；已经装着再点「1 安装」照旧整套都问（这些项没删）。
钉住：
  一、第一次装（simple）不问的那几项走默认值；扫描路径先空着（装完在「3 挂载路径」里加）
  二、重跑时这些项照旧都问（功能没少）
  三、三组账号：网页入口、Emby、OpenList（用户名密码都能定；格式不对重问）
  四、OpenList 用户名：容器起来、密码设好之后再改；改不成说清去哪改
  五、装完只说接下来做什么：挂载路径 → 生成媒体库 → 播放器登录"""
import importlib.util, os, sys, re
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
main = SRC[SRC.index("\ndef main():"):SRC.index("\ndef mediawarp_conf_path")]

print("一、第一次装不问的项")
check("第一次装 = 这台机器上还没装着", "simple = not is_installed(ms_install_dir())" in main)
check("q / q_yn：第一次装直接给默认值，重跑才问", "return default if simple else ask(prompt, default)" in main
      and "return default if simple else ask_yn(prompt, default)" in main)
for what, pat in (("安装目录", 'q("安装目录（存放配置）", DEFAULT_DIR)'), ("媒体目录", 'q("媒体目录（存放 strm 和字幕）"'), ("时区", 'q("时区", "Asia/Shanghai")'),
                  ("PUID", 'q("PUID", "0")'), ("PGID", 'q("PGID", "0")'), ("Homepage", 'q_yn("装 Homepage 导航面板（推荐）？", True)'),
                  ("cron", 'q("strm 生成 cron（6 位：秒 分 时 日 月 周）", DEFAULT_STRM_CRON)'),
                  ("自动配 nginx", 'q_yn("要自动配好 nginx 反代和证书吗（只新增 conf.d 文件）？", True)'),
                  ("加弹框密码", 'q_yn("给它加一道密码？（Emby / OpenList 有自己的账号，不套）", True)')):
    check(f"{what}：第一次装不问、用默认值", pat in main)
check("扫描路径：第一次装不问、先空着", 'cfg["scan_spec"] = [] if simple else parse_scan_spec(ask("扫描路径", "/quark"))' in main)
cfg = m.rebuild_cfg_from_disk("/nonexistent-ms"); cfg.update(scan_paths=[], scan_spec=[], strm_cron=m.DEFAULT_STRM_CRON)
try:
    conf = m.gen_autofilm_conf(cfg)
except Exception as e:
    conf = f"EXC {e}"
check("扫描路径空着也生成得出 AutoFilm 配置（没有任务、不报错）", conf.startswith("#") and "alist2strm_tasks" in conf)

print("二、重跑照旧都问")
check("重跑时扫描路径那段说明还在、还是循环到填对为止", "if not simple:\n        print(f\"  {DIM}扫描路径：可以填一条" in main and 'warn("填一条路径、多条逗号隔开、或者 y（自动）。")' in main)
check("原来的每一问都还在代码里（没删，只是第一次装不问）", all(t in main for t in ("安装目录（存放配置）", "媒体目录（存放 strm 和字幕）", "时区", "PUID", "PGID",
      "装 Homepage 导航面板", "扫描路径", "strm 生成 cron", "要自动配好 nginx 反代和证书吗", "给它加一道密码")))

print("三、三组账号")
check("Emby：一行标题说清是干什么的，问用户名（回车 = 不建）、密码（留空随机）", "▸ Emby 账号" in main
      and 'ask("Emby 用户名（回车＝不建，装完自己去网页设）", "")' in main and 'ask("Emby 密码（留空则随机生成）", "")' in main)
check("OpenList：第一次装问用户名（默认 admin）、密码（留空随机）", "▸ OpenList 账号" in main and 'ask("OpenList 用户名", cfg["ol_user"])' in main
      and 'ask("OpenList 密码（留空则随机生成）", "")' in main)
check("OpenList 用户名 / 密码格式不对：说清规则、重问（跟「7 设置 → 13」同一套规则）", "OL_USER_RE.fullmatch(_u)" in main and "OL_PASS_RE.fullmatch(_p)" in main
      and main.count("while True:") >= 3)
check("网页入口：一行标题，问用户名、密码（留空随机）", "▸ 网页入口账号" in main and '"网页入口用户名" if simple' in main and '"网页入口密码（留空则随机生成）" if simple' in main)
check("每组账号的标题都说了它是登录哪里用的", all(t in main for t in ("播放器登录用", "挂网盘的后台 list.域名 登录用", "打开首页 home.域名 时弹的那道密码")))

print("四、OpenList 用户名装的时候定")
i_init, i_ren, i_emby = main.index('ol_init_setup(cfg["ol_user_want"], cfg["ol_pass"])'), main.index('ol_rename_admin(_d, cfg["ol_user_want"])'), main.index("emby_first_setup(")
check("顺序：先把 OpenList 管理员设好，再去建 Emby 账号", i_init < i_ren < i_emby)
check("新版 OpenList（库里没有管理员）：脚本调初始化接口建，用户名一步到位", "if _st is False:\n        _why = ol_init_setup(" in main)
check("老版本（它自己建了 admin）：照旧 admin set，再改名", '"admin", "set", cfg["ol_pass"]' in main and i_ren > 0)
check("admin set 的退出码不算数（没管理员时它也回 0）：真登得上才算设好", 'capture_output=True).returncode == 0 and _ol_login(cfg["ol_user"], cfg["ol_pass"]):' in main)
check("配置先照 admin 写，用户名定了再重写两份配置", 'cfg["ol_user_want"] = cfg["ol_user"]' in main and "_ol_account_apply(_d)" in main)
check("没设好：屏上说原因 + 指路「7 设置 → 13」，不再假装 ✔", 'warn(f"OpenList 管理员账号没设好：{_why}")' in main and "7 设置 → 13 OpenList 账号" in main
      and 'ok("OpenList 管理员密码已设置")' not in main)

print("五、装完说接下来做什么")
tail = main[main.index("▸ 接下来"):]
check("三步：挂载路径扫码 → 生成媒体库 → 播放器登录", "「3 挂载路径」里选网盘、扫码登录" in tail and "「4 生成媒体库」" in tail and "播放器登录上面的 Emby 地址" in tail)
check("Emby 没自动建成的：多一步指路「7 设置 → 1 → 1 自动生成」", 'if not cfg["emby_api_key"]:' in tail and "7 设置 → 1 → 1 自动生成" in tail)
check("不再叫人去 OpenList 网页手动挂盘、去 Emby 网页建库关选项（脚本都代劳了）", all(t not in main for t in ("OpenList → 存储 → 添加，挂载路径填", "Emby 添加媒体库，路径指向", "章节图像提取")))

print("五之二、菜单上不再写「（重跑可改配置）」（1.5.428，仓库主人：会让人误会）")
SRC2 = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
check("「1. 安装」后面不再挂那句", '"  1. 安装" + ("（重跑可改配置）"' not in SRC2 and 'print("  1. 安装")' in SRC2)
check("屏上任何地方都不再出现这句", not [l for l in SRC2.splitlines() if "重跑可改配置" in l and not l.strip().startswith("#")])

print("六、版本号")
check("media-stack 1.5.415 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 416))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
