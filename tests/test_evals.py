"""Regression check for evals.py with a fake `claude` (no tokens). Run: python3 tests/test_evals.py"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

EVALS = Path(__file__).resolve().parents[1] / "plugins/agent-readiness/skills/audit/scripts/evals.py"

# Fake agent: writes mul() correctly only when the repo's instructions are present, and records
# whether the harness denied the config file the tests need.
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv
settings = json.loads(args[args.index("--settings") + 1]) if "--settings" in args else {}
cwd = Path(os.getcwd())
knows = (cwd / "AGENTS.md").exists()
src = (cwd / "app/calc.py").read_text()
body = "    return a * b\n" if knows else "    return a + b\n"
(cwd / "app/calc.py").write_text(src + "\n\ndef mul(a, b):\n" + body)
guarded = "Read(./settings/docker_config.json)" in settings.get("permissions", {}).get("deny", [])
print(json.dumps({"type": "result", "subtype": "success" if guarded else "error_unguarded", "is_error": not guarded, "num_turns": 3 if knows else 6,
                  "total_cost_usd": 0.1, "duration_ms": 10, "result": "done",
                  "usage": {"input_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0, "output_tokens": 5}}))
'''


def git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)


def test_evals():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        repo = d / "repo"
        for f, body in {
            "app/__init__.py": "import json\nCONFIG = json.load(open('settings/docker_config.json'))\n",
            "app/calc.py": "def add(a, b):\n    return a + b\n",
            "settings/docker_config.json": '{"debug": true}',
            "tests/__init__.py": "",
            "tests/test_add.py": "import unittest\nfrom app.calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        }.items():
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            (repo / f).write_text(body)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        # PR 7: adds mul() with a test (valid task)
        git(repo, "checkout", "-qb", "feat-mul")
        (repo / "app/calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        (repo / "tests/test_mul.py").write_text("import unittest\nfrom app.calc import mul\n\nclass T(unittest.TestCase):\n    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "Add mul()")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "feat-mul", "-m", "Merge pull request #7 from org/feat-mul", "-m", "Add multiplication")
        # PR 8: its test already passes before the change (not discriminating)
        git(repo, "checkout", "-qb", "chore")
        (repo / "app/calc.py").write_text((repo / "app/calc.py").read_text() + "\n# tidy\n")
        (repo / "tests/test_add2.py").write_text("import unittest\nfrom app.calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 1), 2)\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "More add tests")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "chore", "-m", "Merge pull request #8 from org/chore", "-m", "Tidy")
        # PR 9: a flaky test (alternates pass/fail through a file the test itself toggles)
        git(repo, "checkout", "-qb", "flaky")
        (repo / "app/calc.py").write_text((repo / "app/calc.py").read_text() + "\n\ndef sub(a, b):\n    return a - b\n")
        (repo / "tests/test_sub.py").write_text(
            "import os, unittest\nfrom app import calc\n\nclass T(unittest.TestCase):\n    def test_sub(self):\n"
            "        flip = os.path.exists('.flip')\n        open('.flip', 'w').close() if not flip else os.remove('.flip')\n"
            "        self.assertTrue(hasattr(calc, 'sub') and not flip)\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "Add sub()")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "flaky", "-m", "Merge pull request #9 from org/flaky", "-m", "Subtraction")
                # uncommitted instruction files + a deny rule covering the config the tests need
        (repo / "AGENTS.md").write_text("# calc\n- `mul` multiplies.\n")
        (repo / ".claude").mkdir()
        (repo / ".claude/settings.json").write_text('{"permissions": {"deny": ["Read(./settings/*_config.json)"]}}')
        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout

        fake = d / "claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        env = {**os.environ, "AGENT_READINESS_CLAUDE": str(fake)}
        cmd = "{python} -m unittest {tests}"

        b = subprocess.run([sys.executable, "-I", str(EVALS), "build", str(repo), "--test-cmd", cmd, "--branch", "main"],
                           env=env, capture_output=True, text=True)
        assert b.returncode == 0, b.stdout + b.stderr
        doc = json.loads((repo / "docs/agent-readiness/evals/tasks.json").read_text())
        by_id = {t["id"]: t for t in doc["tasks"]}
        assert by_id["pr-7"]["validation"]["valid"], by_id["pr-7"]["validation"]
        assert by_id["pr-7"]["validation"]["needs_files"] == ["settings/docker_config.json"]
        assert not by_id["pr-8"]["validation"]["valid"] and "already pass" in by_id["pr-8"]["validation"]["reason"]
        assert not by_id["pr-9"]["validation"]["valid"] and by_id["pr-9"]["validation"]["reason"].startswith("flaky"), by_id["pr-9"]["validation"]
        assert len(by_id["pr-7"]["validation"]["after_runs"]) == 3 and len(by_id["pr-7"]["validation"]["before_runs"]) == 3
        # early stops: a non-discriminating PR runs its before side once; a flaky one stops at the first flip
        assert len(by_id["pr-8"]["validation"]["before_runs"]) == 1, by_id["pr-8"]["validation"]
        assert len(by_id["pr-9"]["validation"]["after_runs"]) == 2 and not by_id["pr-9"]["validation"]["before_runs"]
        assert by_id["pr-7"]["validation"]["flakiness_checked"]

        # reuse: a second build validates nothing again
        again = subprocess.run([sys.executable, "-I", str(EVALS), "build", str(repo), "--test-cmd", cmd, "--branch", "main"],
                               env=env, capture_output=True, text=True)
        assert again.returncode == 0 and again.stdout.count("(reused)") == 3, again.stdout

        # slow tests: one run over the limit means no repeats; kept, but marked unchecked
        slow_out = d / "slow.json"
        slow = subprocess.run([sys.executable, "-I", str(EVALS), "build", str(repo), "--test-cmd", cmd, "--branch", "main",
                               "--max-test-seconds", "0", "--out", str(slow_out)], env=env, capture_output=True, text=True)
        assert slow.returncode == 0, slow.stdout + slow.stderr
        v7 = {t["id"]: t for t in json.loads(slow_out.read_text())["tasks"]}["pr-7"]["validation"]
        assert v7["valid"] and not v7["flakiness_checked"] and len(v7["after_runs"]) == 1 and "unchecked" in v7["reason"], v7
        assert by_id["pr-7"]["title"] == "Add multiplication" and "- Add mul()" in by_id["pr-7"]["details"]

        plan = subprocess.run([sys.executable, "-I", str(EVALS), "run", str(repo)], env=env, capture_output=True, text=True)
        assert plan.returncode == 0 and "Plan only" in plan.stdout and "pr-7" in plan.stdout, plan.stdout + plan.stderr

        out = repo / "docs/agent-readiness/evals/report.md"
        r = subprocess.run([sys.executable, "-I", str(EVALS), "run", str(repo), "--yes", "--out", str(out)],
                           env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        res = json.loads(out.with_suffix(".json").read_text())["results"]
        side = {x["side"]: x for x in res}
        assert not side["before"]["passed"] and side["after"]["passed"], res
        assert side["before"]["ok"] and side["after"]["ok"], res  # harness denied the config on both sides
        assert side["after"]["same_files_as_pr"] == 1.0 and "app/calc.py" in side["after"]["files_touched"]
        assert "Pass rate:** before 0/1, after 1/1" in out.read_text()
        # the user's repo: only the new docs/agent-readiness files, nothing else
        now = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
        assert set(now.splitlines()) - set(status.splitlines()) <= {"?? docs/"}, now


if __name__ == "__main__":
    test_evals()
    print("ok")
