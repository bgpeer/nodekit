# 工作笔记（Claude 每次开工先读）

记的是代码里不好一眼看出来、真机上踩出来的结论。规矩以仓库根目录的 `CLAUDE.md` 为准，这里不重复。
**不写任何安装人信息**：没有域名、IP、商家、机房位置、看了什么片。

## 怎么干活

- 改完：`claude/tests/` 写测试 → `bash claude/run_tests.sh` → 提 PR、squash 合并 → 工作分支重置到最新 main。
  流程细节见技能 `.claude/skills/ship`。
- 真机问题：先让仓库主人跑只读诊断（`claude/tools/`，见技能 `.claude/skills/vps-diag`），拿到原文再改，别凭猜。
- 回复仓库主人用中文、直白，先说结论；他看的是手机终端截图。
- 合并前可以叫审查智能体 `rule-checker` 对一遍 `CLAUDE.md`（泄漏、删功能、屏上文字、误伤播放）。

## media-stack.py 地图（只记关键的）

| 做什么 | 函数 |
|---|---|
| 「4 生成媒体库」 | `do_strm` → 收尾 `strm_finish`（清失效、压原盘、挪布局、报 Emby、建库、后台补时长 / 截封面） |
| 凌晨对齐（cron 04:50） | `do_sync` → `emby_sync_changes(covers=True)` → `align_library` |
| 每小时对齐（白天看片时一直在跑） | `align_library` 里的 `scan_if_grown` → `emby_sync_changes`（不传 covers） |
| 只报变动给 Emby | `emby_notify_changes`（≤300 条，每小时那条也用它，**别改**）；大批量用 `emby_notify_bulk` |
| Emby 里的空壳 | `emby_ghost_items` / `prune_emby_ghosts`（每小时，有两成刹车）/ `clear_retired_ghosts`（撤掉的盘，人点的） |
| 开播门 | `do_heal_gate`（15 秒硬上限）；改它要单独上、等仓库主人实测退出后进度刷新 |
| 「8 更新」 | `update_menu`：1 立即 → `do_update`；2 每月自动开关 → cron 每天 02:30 `do_imgupdate`（3 号起、本月没做成才做，`do_update(auto=True)`） |
| 截封面 | `fill_covers`；按钮 → 后台 `do_covers_mount`；生成后 / 凌晨新片 → `queue_new_covers` → `do_covers_new` |
| 盘那一屏 | `_drive_menu`，公共项 `_drive_std_items` / `_drive_tail_items` |
| 小雅 | 1.5.385 整栏删了（仓库主人：片不多、找不到想看的）；要挂走「4 WebDAV」。卸载里只留带走旧版装的那份 |
| 测试里加载脚本 | `importlib` 按路径加载 `media-stack.py`，再把要隔离的函数换成假的（看 `claude/tests` 里现成的写法） |

## 凌晨定时任务总表（北京时间，10/25 核对过四个脚本 + vps-net）

加新的定时任务先对这张表；带【队】的进 `bgpeer-jobq`，一个跑完再跑下一个，重启后接着跑。

| 时刻 | 任务 | 脚本 | 会重启什么 |
|---|---|---|---|
| 每月 3 日起 02:30 | 镜像自动更新（本月没成就第二天再试，只在 02:00–03:00 起头）【队】 | media-stack | Emby 那几个容器 |
| 每天 02:50 | 节点脚本自更新【队】 | xy-installer | 无 |
| 每天 03:00 | 规则刷新【队】 | cn-block | sing-box |
| 每天 03:00 | whitelist-inject（mack-a 共存用，没装就没有；不进队） | vps-net | 规则变了才重启 mack-a 的 xray |
| 每月 1 日 03:05 | 网络优化脚本自更新【队】（排在 nginx 升级前；新调优参数要手动重跑优化） | net-optimize | 自适应 QoS（出口闲着时） |
| 每月 1 日 03:10 | nginx 升级【队】 | net-optimize | nginx |
| 每天 03:20 | 三大配置重生成【队】 | xy-installer | 无 |
| 每天 03:30 | 刷新元数据（按盘，默认关）【队】 | media-stack | 无（Emby 后台忙） |
| 每月 2 日 04:00 | 内核更新【队】 | xy-installer | sing-box / xray |
| 每天 04:10 | Emby 脚本自更新【队】 | media-stack | 开播门（没人播时） |
| 每天 04:15 | 刷目录缓存（不进队，要赶 04:20） | media-stack | 无 |
| 每天 04:20 | AutoFilm 扫库（容器自己的定时） | — | 无 |
| 每天 04:50 | 每日对齐【队】 | media-stack | 无 |
| 每小时 :00 / :30、每分钟 | 预热 / 保活 / heal-tick（轻，不进队，有人播就让路） | media-stack | 无 |

两条不进队的（04:15、04:20）是唯一会被挤到的：排在前面的活不许拖过 04:15。

## 真机结论（踩过的坑）

- **Emby 扫整个库会清掉 strm 条目的音视频轨**（strm 里是网盘路径，重读读不出）。能只报变动就只报变动；
  没人在终端前一律不扫整个库（`emby_scan_wait` 里拦）。
- **Emby 收到「已删除」通知不一定认**：整个主目录都没了时找不到上级去刷，等多久都不删。要删就按条目 id 删
  （`DELETE /Items/{id}`），而且**所在文件夹必须存在**，不然回 404「Could not find a part of the path」——
  临时按 Emby 的 PUID/PGID 建回空文件夹再删。
- **机器重启后 MediaWarp 可能没登录上 OpenList**（容器一起起来，它登录时 OpenList 还没好）：之后每次播放都回 Emby 的
  404「Could not find a part of the path」、播放器闪退，挂载里却能播。`docker restart mediawarp` 就好；每小时那轮由
  `mediawarp_cold_start_broken` 认出来自动重启（1.5.386）；「8 更新」换了 OpenList 容器当场等它起好再重启 MediaWarp（1.5.387）。nginx 每月自动升级只重载 nginx，不会触发。
- **晚高峰夸克接口慢 → Emby 里一直转圈**（10/25）：MediaWarp 问 OpenList 要直链超时（它自己 30 秒放弃回 404），
  跟没登录上（秒回 404、闪退）是两回事；开播门先替它取好地址后就能播。看 `docker logs mediawarp` 里
  「context deadline exceeded」/「failed get link」区分。
- **Emby 收到通知要先攒 60–90 秒才动**，前一两分钟「0」是正常的。
- **115 的直链按来访 UA 签发**：谁要的地址只认谁来下。播放、截封面都要用 `pan115_url(播放器的脸)`。
- **夸克 / 阿里的服务器在阿里云**：仓库主人那台 VPS 晚高峰连它们的接口常超时（本机也超时，不是 Docker 的事），
  晚上阿里系的接口会卡、夸克截封面会断；白天正常。
- **OpenList 的 /d/ 每请求一次就去网盘换一次直链**：ffmpeg 读一部片要请求好几次（开头、文件尾的 moov、三分之一处），
  网盘一抖其中某次就是半截（partial file / Invalid data）。要连读几次的，先 `resolve_once` 换成直链再读。
- **阿里没会员原画限速约 0.7 Mbps**，超清片必卡；转码流不限速。
- **小雅（已删，留着备查）**：开放 token 要走小雅自己那家（auth.xiaoya.pro）拿；它的 /d/ 不带签名一律给提示图；
  播放前要把片转存进自己阿里盘，空间不够就「获取新file id失败」。
- **网页播放器放不了很多 mkv**（HEVC / DTS），测能不能播要用 Hills / Emby 客户端。
- 会话容器会被回收，scratchpad 会清空：测试、笔记一律放仓库 `claude/`。

## 小火箭配置行（10/20 苹果实测，结论已写进 shadowrocket_line）

- 路径 / Host / grpc 服务名：`obfs-uri=` / `obfs-host=`，**不是**分享链接里的 `path=` / `obfsParam=`（那样小火箭读不到，
  请求打到伪装站首页，nginx 日志 200 不是 101）。ws：`obfs=websocket`；httpupgrade：`obfs=httpupgrade`；
  grpc：`obfs=grpc,obfs-uri=服务名`（不带 /）；xhttp：`obfs=xhttp,obfs-uri,obfs-host`。`alpn=http/1.1` 不是解药，别加回去。
- 小火箭握手带 X25519MLKEM768（指纹 Safari26_3）：xray 26.9.8+ 的 reality（²）小火箭能连，不再跳过。
- 还没测出来：VLESS Encryption（量子加密）在配置行里的参数名，目前跳过；单条链接导入实测能连。
- 测写法用 `claude/tools/srtest.py`：同一节点放几种写法生成测试订阅，手机 C 测试一轮就定，别一次猜一种。
- 订阅 .conf 加到小火箭首页刷新报「不能获取订阅节点」：小火箭没连接时直连订阅端口，可能被干扰；先连节点再刷新。

## nginx 443 后面挂了谁（10/22 起）

- 挂在 nginx 后面 = 入站监听 127.0.0.1。sing-box：ws² 家族、sni-split 下的 reality-vision；xray：vless-ws、vmess-ws、
  xhttp-tls（nginx 用 grpc_pass 进 h2c）、sni-split 下的 reality-vision（要跟 sing-box 用不同伪装域名才进分流）。
- 两个核心各一个伪装域名：state 里 `sni` / `sni_xr`，`_xr_sni()` 取 xray 的；nginx stream 只能按 SNI 分，重复了分不开。
- xray 的 reality-grpc² / xhttp² 挂在 vision² 那个 reality 入口后面（sni-split 下）：入口解开 reality 后默认回落到
  nginx 本地 h2c `REALITY_FB_PORT`，按服务名 / 路径分。回落不能按 alpn 配（REALITY 解开后 realAlpn 为空）；
  回落口用 TCP 不用 unix socket（nginx 异常退出留下 .sock 会再也起不来）。删 vision² 入口改名 `reality-entry` 留下，
  零用户照样回落；见 `_xr_entry_fixup`。sing-box 的 reality-grpc¹ 没有回落，还在自己的端口。
- 测抗量子签名要用长证书链的目标站，短链 xray 不签，错的 pqv 也能连；本机测代理记得 `env -u no_proxy`（127.0.0.1 在里面）。
- sing-box 的 grpc-tls（10/23 新增）：自己域名证书的 grpc，nginx 前置时本地明文、nginx 按 /服务名/ grpc_pass；reality-grpc¹ 照旧在自己的端口。
- tuic 有域名真证书时上 UDP 443（nginx / reality 都只占 TCP 443）；`_udp443_ok` 建 IPv6 socket 要包 try（没开 IPv6 的机器报 97）。
- 增删协议、换伪装域名后一律 `_nginx_sync_from_cfg()` 按两个核心【当前配置】重建反代表 + 分流表，nginx -t 不过放回原文件。
- pq（pqv）按 UUID 对链接，不按端口（挂 nginx 后链接端口都是 443）。

## 网络优化 ↔ 节点（节点画像，10/25 起）

- xy-installer 的 `netprofile_sync` 写 `/etc/bgpeer/netprofile.json`（只有端口：两核心入站含 127.0.0.1 的、CDN、订阅、
  nginx 本地 8443-8445、hy2 跳跃段、本机别的服务在听的 TCP），签名对不上 net-optimize config 里的 `PROFILE_SIG`
  才叫 `--apply-profile`（只改 `ip_local_reserved_ports` + sysctl 文件那一行，不重启服务）。调用点：装 / 加 / 删协议、
  CDN 增删、换订阅端口、进面板。
- **老版 net-optimize 绝不能喂新参数**：它用 `parse_known_args`，不认的参数直接忽略、转头跑整套优化。新加子命令一律
  先查本地那份认不认（`"--apply-profile" in 文件`），菜单里的 `--adaptive`、`--xanmod` 同理。
- 挑端口的整段 15000-45000 故意照留：只留在用的端口的话，新协议从挑端口到绑上（下内核、签证书）有几十秒空档，
  出站连接可能正好占走。UDP 监听口不扫：sing-box 替用户发 QUIC / DNS 的客户端套接字在 /proc/net/udp 里跟服务端口一样。
- 节点不开 TFO（内核 `tcp_fastopen=3` 只是允许，套接字不开就不生效）：reality 冒充别人家网站，别人不回 TFO、我们回了
  就露馅；国内运营商还常丢带数据的 SYN。UDP 大缓冲、网卡 UDP GRO 本来就对所有机器开着，不用按画像开关。
- **fq / BBR 模块在哪**（10/25 从 Ubuntu 仓库拆包查的）：24.04 的通用 / 谷歌 / AWS / Azure / 甲骨文内核、22.04 的通用 / 谷歌 / AWS
  内核，`sch_fq`、`tcp_bbr`、`sch_cake` 都在基础 `linux-modules` 包里；24.04 的 `linux-image-kvm` 只是指向通用内核的壳；
  22.04 的 kvm 内核配置是 `# CONFIG_NET_SCH_FQ is not set`、`# CONFIG_TCP_CONG_ADVANCED is not set`（没 fq、没 BBR、没 cake），
  装什么模块都没用。Debian 的仓库这个容器连不上，没拆包核对过。判断走 `kernel_has()` 读 `/boot/config-<内核>`。
- 端到端测试 `test_netprofile_e2e.py` 在 `unshare --mount --net` 里跑真脚本、真内核参数（/etc 那几个目录盖 tmpfs），
  不碰本机；改保留端口相关的逻辑先跑它。

## 客户端的坑（真机踩过）

- **Bettbox 推荐换成 ClashMi**（仓库主人 10/24）：Bettbox 用自己一套 DNS / 规则 / TUN 复写盖掉订阅里的原生配置
  （我们配的「自己域名走国内 DoH 直连、嗅探 853」这些全被覆盖），结果同一台机、同一批节点在电信下 ClashMi 全绿、
  Bettbox 全红。关键是**它在软件层关掉复写也不回退到订阅原生配置**（要么用它的、要么空的），没救。这是客户端的设计
  缺陷，服务器 / 订阅端改不了，也不该为迁就它改订阅（会搞坏别的正常客户端）。ClashMi 能真正关掉软件复写 / TUN 复写、
  走原生配置，全协议实测通。之前查 Bettbox 私人 DNS 连不上、给它提 PR（见任务 #26/#27），是同一类毛病。
- **封 IP 看客户端里节点通不通，别信 nmap 裸扫**：nmap `-Pn -p 443` 回 `filtered` 不代表节点不通——GFW / 运营商会丢掉
  裸 SYN 扫描，而 reality / 真实代理流量照过（伪装起作用）。同一台日本机，nmap 443 filtered、ClashMi 里全节点却 110~170ms 全通。
  判「是不是封 IP」以客户端实连为准；真要用 nmap，先把手机代理彻底关掉（开着的话 TUN 本地答 SYN，延迟 0.4ms 是假 open）。

## 仓库主人的偏好

- 屏上精简、整齐；危险操作醒目一行 + 默认否；功能不许悄悄删。
- 长时间跑的东西必须有进度（「卡在这里好长时间了，也没显示是不是在运行」）。
- 不要扫整个库；凌晨 / 每小时的活不许误伤播放。
- 说话要实在：没在真机上测过的就说没测过。
