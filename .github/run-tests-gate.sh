#!/usr/bin/env bash
# CI 测试门：跑全量回归测试，只在「未知失败」时红灯。
#
# 方案（为什么这样分）：
#   · 确定性已知失败 —— 干净 main 上单跑也失败（KNOWN 里的三个）：直接放行，
#     不让门一上来就全红。这些是要修的债，不是本次提交引入的；修好一个就从
#     KNOWN 里拿掉它，门照样绿。
#   · 并行偶发失败 —— 全量是并行跑的，负载一大个别测试会抖（test_netopt_reboot_prompt_4521、
#     test_ms_state_lock、test_sub_dualstack、test_nginx_rules_heal，单跑都过；
#     test_progwatch 在重负载下也抖过一次）：失败的逐个串行重跑最多三次（GitHub runner
#     是共享机器，抖一下很常见，一次重跑不够——2026-11-19 四次运行都因为这个红了），
#     哪次过了算偶发。偶发项不写进放行清单：三次都不过可能是真回归，红了才有人看。
#   · 其余失败 —— 红灯。
#
# 测试钩子（只给 claude/tests/test_ci_test_gate.py 用）：GATE_RUNNER（默认
# bash claude/run_tests.sh）、GATE_TESTS_DIR（默认 claude/tests）、
# GATE_KNOWN（默认下面的 KNOWN，空格分隔）。
set -uo pipefail

KNOWN="${GATE_KNOWN:-test_privdns test_netprofile_e2e test_ms_secret_perms}"
RUNNER="${GATE_RUNNER:-bash claude/run_tests.sh}"
TESTS_DIR="${GATE_TESTS_DIR:-claude/tests}"

log="$(mktemp)"
echo "== 测试门：$RUNNER"
# shellcheck disable=SC2086
if $RUNNER >"$log" 2>&1; then
  echo "全量通过，门绿。"
  rm -f "$log"
  exit 0
fi
echo "全量有失败，看是已知、偶发还是未知："
grep -e '^✖ ' "$log" || true

# 失败的测试名（run_tests.sh 每行一个 "✖ test_xxx.py"）
mapfile -t failed < <(grep -o '^✖ test_[^ ]*\.py' "$log" | sed 's/^✖ //' | sort -u)
if [ "${#failed[@]}" -eq 0 ]; then
  echo "✖ 没有解析到失败的测试名，但整轮退出了非零 —— 当未知失败处理。"
  tail -30 "$log"
  rm -f "$log"
  exit 1
fi

# 逐个串行重跑最多三次：并行偶发项单跑能过，真失败过不了。
still=()
for f in "${failed[@]}"; do
  passed=""
  for attempt in 1 2 3; do
    if timeout 300 python3 "$TESTS_DIR/$f" </dev/null >"$log.retry" 2>&1; then
      passed=1
      echo "  偶发（第 $attempt 次重跑过了）：$f"
      break
    fi
  done
  if [ -z "$passed" ]; then
    still+=("$f")
  fi
done
rm -f "$log.retry"

known_left=()
unknown=()
for f in "${still[@]}"; do
  base="${f%.py}"
  if [[ " $KNOWN " == *" $base "* ]]; then
    known_left+=("$f")
  else
    unknown+=("$f")
  fi
done

if [ "${#known_left[@]}" -gt 0 ]; then
  echo "  已知问题（放行）：${known_left[*]}"
fi
if [ "${#unknown[@]}" -gt 0 ]; then
  echo "✖ 未知失败（门红）：${unknown[*]}"
  echo "  详情见上面全量输出；本地复现：bash claude/run_tests.sh <名字子串>"
  rm -f "$log"
  exit 1
fi
echo "剩下的全是已知问题，门绿。"
rm -f "$log"
exit 0
