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
| 截封面 | `fill_covers`；按钮 → 后台 `do_covers_mount`；生成后 / 凌晨新片 → `queue_new_covers` → `do_covers_new` |
| 盘那一屏 | `_drive_menu`，公共项 `_drive_std_items` / `_drive_tail_items` |
| 小雅 | 1.5.385 整栏删了（仓库主人：片不多、找不到想看的）；要挂走「4 WebDAV」。卸载里只留带走旧版装的那份 |
| 测试里加载脚本 | `importlib` 按路径加载 `media-stack.py`，再把要隔离的函数换成假的（看 `claude/tests` 里现成的写法） |

## 真机结论（踩过的坑）

- **Emby 扫整个库会清掉 strm 条目的音视频轨**（strm 里是网盘路径，重读读不出）。能只报变动就只报变动；
  没人在终端前一律不扫整个库（`emby_scan_wait` 里拦）。
- **Emby 收到「已删除」通知不一定认**：整个主目录都没了时找不到上级去刷，等多久都不删。要删就按条目 id 删
  （`DELETE /Items/{id}`），而且**所在文件夹必须存在**，不然回 404「Could not find a part of the path」——
  临时按 Emby 的 PUID/PGID 建回空文件夹再删。
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
- reality-grpc / reality-xhttp 留在自己的端口：跟 vision 同一个 SNI、ALPN 也一样，nginx 认不出该给谁。
- 增删协议、换伪装域名后一律 `_nginx_sync_from_cfg()` 按两个核心【当前配置】重建反代表 + 分流表，nginx -t 不过放回原文件。
- pq（pqv）按 UUID 对链接，不按端口（挂 nginx 后链接端口都是 443）。

## 仓库主人的偏好

- 屏上精简、整齐；危险操作醒目一行 + 默认否；功能不许悄悄删。
- 长时间跑的东西必须有进度（「卡在这里好长时间了，也没显示是不是在运行」）。
- 不要扫整个库；凌晨 / 每小时的活不许误伤播放。
- 说话要实在：没在真机上测过的就说没测过。
