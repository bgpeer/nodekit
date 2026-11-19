#!/usr/bin/env python3
"""CI 测试门 .github/run-tests-gate.sh：只在「未知失败」时红灯。
  方案（gate 脚本头注释里写了理由）：确定性已知失败放行、并行偶发失败串行重跑最多三次、
  其余失败红灯。桩一个假的 runner（照 run_tests.sh 的格式印 "✖ test_xxx.py"）和
  三个假测试：一直失败的已知项、第一次失败第二次过的偶发项、一直失败的未知项。
"""
import os, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GATE = os.path.join(ROOT, ".github", "run-tests-gate.sh")
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def fixture(names):
    """一座假测试目录 + 一个假 runner：跑 FAKE_RUN 里列的测试，失败的印 ✖ 行，任一失败就退 1。"""
    d = tempfile.mkdtemp()
    for n in names:
        if n == "test_flaky":
            open(os.path.join(d, n + ".py"), "w").write(
                "import os, sys\n"
                "s = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.flaky-ran')\n"
                "if os.path.exists(s): sys.exit(0)\n"
                "open(s, 'w').write('x'); sys.exit(1)\n")
        elif n == "test_flaky2":
            # 前两次失败、第三次过：门要给够重跑次数才绿。
            open(os.path.join(d, n + ".py"), "w").write(
                "import os, sys\n"
                "s = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.flaky2-n')\n"
                "n = int(open(s).read()) if os.path.exists(s) else 0\n"
                "open(s, 'w').write(str(n + 1))\n"
                "sys.exit(0 if n >= 2 else 1)\n")
        else:
            open(os.path.join(d, n + ".py"), "w").write("import sys; sys.exit(1)\n")
    run = os.path.join(d, "run.sh")
    open(run, "w").write(
        "#!/usr/bin/env bash\n"
        "d=$(dirname \"$0\"); fail=0\n"
        "for f in $FAKE_RUN; do\n"
        "  python3 \"$d/$f.py\" </dev/null >/dev/null 2>&1 || { echo \"✖ $f.py\"; fail=1; }\n"
        "done\n"
        "exit $fail\n")
    os.chmod(run, 0o755)
    return d, run

def gate(d, run, known, run_names):
    env = dict(os.environ, GATE_RUNNER=run, GATE_TESTS_DIR=d,
               GATE_KNOWN=known, FAKE_RUN=" ".join(run_names))
    r = subprocess.run(["bash", GATE], env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr

d, run = fixture(["test_known_fail", "test_flaky", "test_unknown_fail"])
rc, out = gate(d, run, "test_known_fail", ["test_known_fail", "test_flaky"])
check("已知失败 + 偶发失败（重跑过了）→ 门绿", rc == 0 and "门绿" in out)
d15, run15 = fixture(["test_flaky2"])
rc, out = gate(d15, run15, "test_known_fail", ["test_flaky2"])
check("两次失败、第三次过 → 门绿（重跑要给够次数）", rc == 0 and "门绿" in out)
d2, run2 = fixture(["test_known_fail", "test_flaky", "test_unknown_fail"])
rc, out = gate(d2, run2, "test_known_fail", ["test_known_fail", "test_unknown_fail"])
check("未知失败（重跑也不过）→ 门红，指名道姓",
      rc == 1 and "未知失败" in out and "test_unknown_fail.py" in out)

# runner 自己崩了（退非零、一个 ✖ 行都没有）→ 不能当成通过
d3 = tempfile.mkdtemp()
crash = os.path.join(d3, "run.sh")
open(crash, "w").write("#!/usr/bin/env bash\necho 'boom'; exit 2\n")
os.chmod(crash, 0o755)
rc, out = gate(d3, crash, "test_known_fail", [])
check("runner 崩了、没有 ✖ 行 → 门红", rc == 1 and "未知失败" in out)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
