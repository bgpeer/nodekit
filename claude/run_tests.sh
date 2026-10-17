#!/usr/bin/env bash
# 跑 claude/tests 下的全部回归测试（并行），列出没过的；全过退出码 0。
# 用法：bash claude/run_tests.sh            全部
#       bash claude/run_tests.sh xiaoya     只跑文件名里带 xiaoya 的
cd "$(dirname "$0")/tests" || exit 1
pat="${1:-}"
fail=0
for f in test_*${pat}*.py; do
  [ -e "$f" ] || continue
  ( out=$(timeout 300 python3 "$f" </dev/null 2>&1); rc=$?
    if [ $rc -ne 0 ]; then echo "✖ $f"; echo "$out" | grep -e ✖ -e Error | head -5 | sed 's/^/    /'; exit 1; fi ) &
done
for j in $(jobs -p); do wait "$j" || fail=1; done
[ $fail -eq 0 ] && echo "ALL PASS（$(ls test_*${pat}*.py | wc -l) 个）"
exit $fail
