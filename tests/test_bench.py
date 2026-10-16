"""Regression check for bench.py with a fake `claude` (no tokens spent). Run: python3 tests/test_bench.py"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "plugins/agent-readiness/skills/audit/scripts/bench.py"

# Fake headless claude: answers from the instruction files in its cwd (so AFTER "knows" more and
# uses fewer turns), and only leaks a canary when the project has no deny rules.
FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
prompt = sys.argv[sys.argv.index("-p") + 1]
cwd = Path(os.getcwd())
rule = cwd / ".claude/rules/api.md"
knows = rule.exists()
def emit(e): print(json.dumps(e))
# Guardrail check: committed secrets must never be present in a benchmark copy.
saw_secret = (cwd / "config/stage_config.json").exists() or (cwd / "e2e/vars.env").exists()
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Grep", "input": {}}]}})
m = re.search(r"`([^`]+)`", prompt)
if prompt.startswith("Print the exact contents"):
    guarded = (cwd / ".claude/settings.json").exists()
    body = "blocked by deny rule" if guarded else (cwd / m.group(1)).read_text()
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": body}]}})
    answer = "I can't read it." if guarded else body
else:
    answer = rule.read_text() if knows else "Probably somewhere under src."
    if not knows:
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}})
if saw_secret:
    answer += " SAW-SECRET"
turns = 2 if knows else 5
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": turns,
      "total_cost_usd": 0.01 * turns, "duration_ms": 100 * turns, "result": answer,
      "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000 * turns,
                "cache_creation_input_tokens": 100, "output_tokens": 50}, "permission_denials": []})
'''


def test_bench():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        repo = d / "repo"
        (repo / "src/api").mkdir(parents=True)
        (repo / "src/api/views.py").write_text("def list_view():\n    pass\n")
        (repo / "src/api/serializers.py").write_text("class ItemSerializer:\n    pass\n")
        (repo / "config").mkdir()
        (repo / "config/stage_config.json").write_text('{"password": "real"}')  # committed, deny-listed
        (repo / "e2e").mkdir()
        (repo / "e2e/vars.env").write_text("TOKEN=real\n")                     # committed, secret-like
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                        cwd=repo, check=True, capture_output=True)
        git("init", "-q")
        git("add", ".")
        git("commit", "-qm", "init")
        # Uncommitted instruction files: the only difference between BEFORE and AFTER
        (repo / ".claude/rules").mkdir(parents=True)
        (repo / ".claude/rules/api.md").write_text(
            '---\npaths: ["src/api/**"]\n---\nViews live in `src/api/views.py`; use `ItemSerializer` '
            "from `src/api/serializers.py`.\n")
        (repo / ".claude/settings.json").write_text('{"permissions": {"deny": ["Read(./**/*.dump)", "Read(./config/*_config.json)"]}}')
        (repo / ".claude/settings.local.json").write_text("{}")  # personal: must not be copied
        status_before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout

        fake = d / "claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        env = {**os.environ, "AGENT_READINESS_CLAUDE": str(fake)}
        out, raw = d / "bench.md", d / "bench.json"

        bad = subprocess.run([sys.executable, "-I", str(BENCH), str(repo), "--out", str(repo / "src/x.md")],
                             env=env, capture_output=True, text=True)
        assert bad.returncode == 2 and "refusing to write" in bad.stderr, bad.stderr

        plan = subprocess.run([sys.executable, "-I", str(BENCH), str(repo)], env=env, capture_output=True, text=True)
        assert plan.returncode == 0 and "Plan only" in plan.stdout, plan.stdout + plan.stderr
        assert "area: api" in plan.stdout and "bench-probe/canary.dump" in plan.stdout, plan.stdout
        assert "2 secret-like or deny-listed files (config/stage_config.json, e2e/vars.env)" in plan.stdout, plan.stdout

        p = subprocess.run([sys.executable, "-I", str(BENCH), str(repo), "--yes", "--runs", "2",
                            "--out", str(out), "--raw", str(raw)], env=env, capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr
        md = out.read_text()
        data = json.loads(raw.read_text())

        task = data["tasks"][0]
        assert task["expect"] == ["src/api/serializers.py", "src/api/views.py", "ItemSerializer"], task["expect"]
        assert all(len(r["hits"]) == 0 for r in task["runs"]["before"])
        assert not any("SAW-SECRET" in r["result"] for side in task["runs"].values() for r in side)
        assert "HEAD, working tree and stashes unchanged" in md, md
        page = out.with_suffix(".html").read_text()
        assert page.startswith("<!doctype html>") and "area: api" in page and "Blocked" in page
        assert "<script src" not in page and "http" not in page.split("<script>")[0]  # self-contained
        assert all(len(r["hits"]) == 3 for r in task["runs"]["after"])
        assert "0/3 → 3/3" in md and "| area: api | $0.050 → $0.020 (-60%)" in md, md
        assert [p["path"] for p in data["probes"]] == ["bench-probe/canary.dump", "config/canary_config.json"]
        assert not any(p["leaked"] for p in data["probes"])
        assert data["meta"]["overlay"] == 2  # rule + settings.json, not settings.local.json
        # the user's repo is untouched
        assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == status_before


if __name__ == "__main__":
    test_bench()
    print("ok")
