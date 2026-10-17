---
name: vps-diag
description: 仓库主人报真机问题（播不了、卡、掉轨道、截不到封面、Emby 里多了 / 少了条目、定时任务没跑）时，给他一组只读诊断命令、读回结果、定位原因。我连不上他的 VPS，只能让他跑命令贴回来。
---

# 真机诊断（nodekit / media-stack）

我碰不到仓库主人的 VPS。流程：给他**只读**命令 → 他贴回输出 / 截图 → 我判断 → 再改代码。

## 规矩

- 只给只读命令。要改他机器上的东西，走脚本菜单或者改代码，不让他手敲修改命令。
- 每条命令说清「看什么、结果怎么判断」，按结果分几种情况写好下一步。
- 提醒他贴之前把 token、cookie、域名打码（`docker logs` 用 `grep -v -i -e token -e cookie` 先滤掉）。
- 命令里不要出现他的域名 / IP；需要的话让他自己替换。
- 先拿原文（报错原句、日志行），别凭猜改代码。

## 现成的工具（`claude/tools/`，都只读）

```
curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/trk.py | python3 - [片名]
curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/orphan.py | python3 -
curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/churn.py | python3 - 片名1 片名2
curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/coverdiag.py | python3 - 片名
```

- 掉轨道 / 有没有扫整个库 → `trk.py` + `cat /var/log/media-stack/scan.log`
- 盘和 strm、Emby 对不上（撤掉的盘还有条目、新盘没进库）→ `orphan.py`
- 某几部片每晚删了又建、重复条目 → `churn.py`
- 补时长 / 截封面结果 → `media-stack heal-log 40`（截封面一轮跑完才写日志）
- 某一部截封面不成 → `coverdiag.py 片名`（每条路、每种身份各截一次，只截不存）
- 开播慢 → `tail -30 /var/log/media-stack/gate.log`
- 连不连得上某个接口：本机 `curl -so /dev/null -m 15 -w "%{time_appconnect}\n" https://<主机>/`，
  容器里用 `docker run --rm --network mediastack alpine wget ...` 对照。

一次性的诊断脚本写好、用过两次以上的，收进 `claude/tools/`（只读、开头写用法），并更新那里的 README。
