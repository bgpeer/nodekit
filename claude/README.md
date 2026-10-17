# claude/ —— Claude 的专属文件夹

仓库主人 10/17 同意：「如果我的仓库需要放对你有利的东西我允许你这么做，但是你要新建一个文件夹……
那个文件夹就是你的专属文件夹可以放你的任何东西，不要给我的代码搞混了」。

- 这里放的是 Claude 改脚本时自查用的东西（回归测试等），**不是**脚本的一部分：
  安装 / 更新都不会下载它，CI（版本号、SHA256SUMS）也不看它。
- 起因：测试原来放在云端会话的临时目录里，会话容器一回收就全没了（10/17 丢过一整套）。
- 不放任何安装人信息：测试里只有假数据，没有 IP、域名、token、cookie。

## 里面有什么

- `NOTES.md`：工作笔记 —— 脚本地图、真机踩过的坑、仓库主人的偏好。每次开工先读。
- `tests/` + `run_tests.sh`：回归测试。
- `tools/`：真机诊断小工具（全部只读），用法见 `tools/README.md`。
- 仓库根目录的 `.claude/`（仓库主人 10/17 同意）：Claude Code 自动认的技能和智能体 ——
  技能 `ship`（改完收尾一条龙）、`vps-diag`（真机诊断）；智能体 `rule-checker`（合并前对照 CLAUDE.md 审一遍）。

## 回归测试

```
bash claude/run_tests.sh           # 全部
bash claude/run_tests.sh xiaoya    # 只跑文件名带 xiaoya 的
```

每次改 `media-stack.py` 等脚本的逻辑，新写的测试也放进 `claude/tests/`，跟改动一起提交。
