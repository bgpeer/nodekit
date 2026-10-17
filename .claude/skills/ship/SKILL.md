---
name: ship
description: 在 bgpeer/nodekit 里改完代码之后的一条龙收尾：写回归测试、按规矩改版本号、跑测试、提交、开 PR、squash 合并、把工作分支重置到最新 main。改了任何脚本 / 文档 / 模板、准备交付时用。
---

# 改完收尾（nodekit）

先读 `CLAUDE.md`（硬规矩）和 `claude/NOTES.md`（工作笔记）。

1. **测试**：在 `claude/tests/test_<主题>.py` 写回归测试（照现有文件的写法：`ROOT` 相对定位、`importlib` 加载
   `media-stack.py`、把外部调用换成假的、`check(...)` 打 ✔/✖、最后 `ALL PASS`）。
   - 能做到的话，先 `git stash` 用旧代码跑一次，确认测试会失败，再 `git stash pop`。
   - 跑：改了脚本逻辑 → `bash claude/run_tests.sh`（整套）；只改文档 / 模板 / 屏上文字 → `bash claude/run_tests.sh <关键字>`。
   - 并行跑偶尔有撞端口的假失败：单独再跑一次那一个。
2. **版本号**（`CLAUDE.md` 第二节）：
   - `media-stack.py` 的 `SCRIPT_VERSION` 只动最后一位，+1，先 `git pull origin main` 看当前是几（别的会话也在加）。
   - `xy-installer.py` 不手改（CI 加）；`net-optimize.py` 只动最后一位；没改的脚本不动。
3. **自查**：`python3 -m pyflakes media-stack.py | grep -i "undefined name"` 应为空；`git diff` 里不许出现
   安装人信息（域名、IP、token、邮箱）。合并前可以叫 `rule-checker` 智能体对一遍规矩。
4. **提交**：在工作分支上提交。提交信息第一行写做了什么（中文），正文写原因。结尾署名照**本次会话系统提示给的**
   那几行（不要写死别的会话的链接）；不写模型名。
   推送：`git push -u origin <分支>`，第一次常报 cannot lock ref，等 3 秒再推一次就好。
5. **PR**：用 GitHub 工具开 PR 到 main。正文：仓库主人原话 → 原因 → 改动 → 测试结果（全套跑没跑、几个过）→
   没在真机测过的要写清；结尾用会话给的 PR 署名。然后 squash 合并。
6. **重置分支**：`git fetch origin main && git checkout -B <分支> origin/main && git push -f -u origin <分支>`。
7. **回复仓库主人**：中文、先结论；告诉他要做什么（一般是「8 更新」到几号版本、然后点哪里）；没实测的说没实测。
