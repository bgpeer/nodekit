# claude/tools —— 真机诊断（全部只读）

在 VPS 上一行跑，不用先下载。都只读，不改机器上任何东西；脚本位置默认 `/etc/bgpeer/media-stack.py`，
装在别处就加 `MS_SCRIPT=/路径/media-stack.py`。

| 工具 | 看什么 | 用法 |
|---|---|---|
| `trk.py` | 整个库几个条目有音视频轨、Emby 计划任务最近什么时候跑完（有没有扫整个库） | `curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/trk.py \| python3 - [片名]` |
| `orphan.py` | OpenList 挂着哪些盘、扫描路径、本地每个盘几个 strm、Emby 收了几个、撤掉的盘有没有留下空条目 | `curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/orphan.py \| python3 -` |
| `churn.py` | 某几部片本地 strm 和 Emby 条目对不对得上（每晚删了又建、重复条目） | `curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/churn.py \| python3 - 片名1 片名2` |

日志类的直接用现成的：`media-stack heal-log 40`（补时长 / 截封面）、`cat /var/log/media-stack/scan.log`（报给 Emby 的增删、
有没有扫整个库）、`tail -30 /var/log/media-stack/gate.log`（开播门每次开播用了多久）。
