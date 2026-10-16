#!/usr/bin/env python3
"""Eval harness: can an agent redo real merged changes, with and without the repo's instructions?

  build  Mine merged PRs that change code and tests, then validate each one for free: the PR's
         tests must FAIL on the commit before the PR and PASS on the merge. Writes tasks.json.
  run    For each validated task, in a throwaway copy at the commit before the PR, an agent gets
         the PR's description and implements it (BEFORE: no new instructions; AFTER: the working
         tree's instruction files added). Then the PR's own tests, hidden until now, grade it.
         Costs tokens; without --yes it only prints the plan.

Guardrails (shared with bench.py): the repo is never modified (checked before/after); work
happens in temp clones; secret-like/deny-listed files are removed, except files the tests need,
which stay behind a harness-level Read deny for both sides; the agent may only read, edit inside
the copy and run the test command; reports go outside the repo or under docs/agent-readiness/.
Grading runs the agent's code: it runs in the temp copy with a minimal environment.

Usage:
    python3 evals.py build [REPO] --test-cmd 'TEST_RUN=true {python} -m pytest {tests} -q'
                     [--branch BRANCH] [--scan 200] [--max-tasks 6] [--out tasks.json]
    python3 evals.py run [REPO] [--tasks tasks.json] [--runs 1] [--model M]
                     [--per-run-usd 3] [--budget-usd 25] [--out report.md] [--yes]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench  # noqa: E402
import scan  # noqa: E402

DEFAULT_TASKS = "docs/agent-readiness/evals/tasks.json"
MAX_CODE_FILES = 5
MAX_LINES = 400
TEST_TIMEOUT = 900
AGENT_BLOCKED = ["WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"]
MISSING_FILE = re.compile(r"No such file or directory: '([^']+)'")
PROMPT = """Implement this change in the repository (it comes from a real pull request):

Title: {title}

{details}

Write the code. You can run tests with:
    {test_cmd}
Don't commit, and don't ask questions: make reasonable choices."""


# ---------------------------------------------------------------- shared helpers

def sh(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout


def default_python(root: Path) -> str:
    for cand in (".venv/bin/python", "venv/bin/python"):
        if (root / cand).exists():
            return str(root / cand)
    return sys.executable


def render_cmd(template: str, python: str, tests: list[str]) -> str:
    return template.replace("{python}", shlex.quote(python)).replace("{tests}", " ".join(shlex.quote(t) for t in tests))


def run_tests(cwd: Path, cmd: str) -> dict:
    """Run the test command with a minimal environment (it may execute agent-written code)."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    t = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT)
        out, code = (p.stdout + p.stderr), p.returncode
    except subprocess.TimeoutExpired:
        out, code = "timed out", 124
    return {"passed": code == 0, "code": code, "seconds": round(time.time() - t, 1), "tail": out[-1500:]}


def checkout(copy: Path, ref: str) -> None:
    subprocess.run(["git", "-C", str(copy), "checkout", "-q", "--force", "--detach", ref], check=True)
    subprocess.run(["git", "-C", str(copy), "clean", "-qfdx"], check=True)


# ---------------------------------------------------------------- build

def mine(root: Path, branch: str, n: int) -> list[dict]:
    out = []
    log = sh(root, "log", "--merges", "--first-parent", f"-n{n}", "--format=%H%x09%ad%x09%s", "--date=short", branch)
    for line in log.splitlines():
        sha, date, subject = line.split("\t", 2)
        rows = [r.split("\t") for r in sh(root, "diff", "--numstat", f"{sha}^1", sha).splitlines() if r.count("\t") == 2]
        paths = [r[2] for r in rows]
        lines = sum(int(a) + int(b) for a, b, _ in rows if a.isdigit() and b.isdigit())
        tests = [p for p in paths if scan.TEST_PART.search(p) and Path(p).suffix in scan.CODE_SUFFIXES]
        code = [p for p in paths if Path(p).suffix in scan.CODE_SUFFIXES and p not in tests]
        if (not tests or not code or len(code) > MAX_CODE_FILES or lines > MAX_LINES
                or any("/migrations/" in p for p in paths) or len(tests) + len(code) != len(paths)):
            continue
        body = sh(root, "log", "-1", "--format=%b", sha).strip()
        commits = [s for s in sh(root, "log", "--format=%s", f"{sha}^1..{sha}^2").splitlines()
                   if s and not s.lower().startswith("merge ")]
        pr = re.search(r"#(\d+)", subject)
        out.append({"id": f"pr-{pr.group(1)}" if pr else sha[:10], "date": date, "merge": sha, "base": f"{sha}^1",
                    "base_sha": sh(root, "rev-parse", f"{sha}^1").strip(), "title": body.splitlines()[0] if body else subject,
                    "details": "\n".join(f"- {c}" for c in commits[:15]) or subject,
                    "code_files": code, "test_files": tests, "lines": lines})
    return out


def validate(root: Path, task: dict, copy: Path, template: str, python: str, hidden: list[str]) -> dict:
    """Free: tests must fail before the PR and pass after it. Learns which hidden files tests need."""
    needs: list[str] = []
    cmd = render_cmd(template, python, task["test_files"])
    after = {"passed": False, "tail": ""}
    for _ in range(3):  # restore up to a few files the tests can't run without
        checkout(copy, task["merge"])
        bench.strip(copy, [h for h in hidden if h not in needs])
        after = run_tests(copy, cmd)
        rels = {os.path.relpath(os.path.realpath(m), os.path.realpath(copy)) if os.path.isabs(m) else m
                for m in MISSING_FILE.findall(after["tail"])}
        missing = sorted(r for r in rels if r in hidden and r not in needs)
        if after["passed"] or not missing:
            break
        needs += missing
    checkout(copy, task["base_sha"])
    bench.strip(copy, [h for h in hidden if h not in needs])
    for t in task["test_files"]:  # bring in the PR's tests, nothing else
        blob = subprocess.run(["git", "-C", str(root), "show", f"{task['merge']}:{t}"], capture_output=True)
        (copy / t).parent.mkdir(parents=True, exist_ok=True)
        (copy / t).write_bytes(blob.stdout)
    before = run_tests(copy, cmd)
    ok = after["passed"] and not before["passed"]
    reason = ("ok" if ok else "tests fail on the merge commit too (environment or flaky)" if not after["passed"]
              else "tests already pass before the change (not discriminating)")
    return {"valid": ok, "reason": reason, "needs_files": needs, "pass_after": after, "fail_before": before}


def repo_root(path: str) -> Path:
    return Path(sh(Path(path).resolve(), "rev-parse", "--show-toplevel").strip() or path).resolve()


def under(root: Path, path: str | None) -> str | None:
    """Relative paths are relative to the repo root, wherever the script is run from."""
    return None if path is None else str(path if os.path.isabs(path) else root / path)


def cmd_build(a) -> int:
    root = repo_root(a.root)
    a.out = under(root, a.out)
    if not bench.report_path_ok(root, a.out):
        print(f"refusing to write {a.out}: outside the repo or under {bench.REPORT_DIR} only", file=sys.stderr)
        return 2
    python = a.python or default_python(root)
    cands = mine(root, a.branch, a.scan)
    print(f"{len(cands)} candidate PRs in the last {a.scan} merges of {a.branch}")
    start = bench.snapshot(root)
    work = Path(tempfile.mkdtemp(prefix="agent-readiness-evals-"))
    tasks = []
    try:
        copy = work / root.name
        bench.make_copy(root, "HEAD", copy, None)
        hidden = bench.sensitive_files(root, copy)
        for c in cands[: a.max_tasks * 3]:
            if len([t for t in tasks if t["validation"]["valid"]]) >= a.max_tasks:
                break
            v = validate(root, c, copy, a.test_cmd, python, hidden)
            c["validation"] = v
            tasks.append(c)
            print(f"- {c['id']} ({c['date']}, {len(c['code_files'])} code + {len(c['test_files'])} test files): "
                  f"{'VALID' if v['valid'] else 'skip'}: {v['reason']}" + (f"; tests need {', '.join(v['needs_files'])}" if v["needs_files"] else ""))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if bench.snapshot(root) != start:
        print("**repo changed during build: inspect git status**", file=sys.stderr)
        return 3
    doc = {"repo": root.name, "branch": a.branch, "test_cmd": a.test_cmd, "python": python,
           "built": time.strftime("%Y-%m-%d"), "tasks": tasks}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(doc, indent=2))
    n = len([t for t in tasks if t["validation"]["valid"]])
    print(f"{n} valid tasks written to {a.out}")
    return 0 if n else 1


# ---------------------------------------------------------------- run

def agent_tools(test_cmd_prefix: str) -> list[str]:
    return ["Read", "Grep", "Glob", "Edit(./**)", "Write(./**)", "MultiEdit(./**)", f"Bash({test_cmd_prefix} *)"]


def harness_settings(needs: list[str]) -> str:
    """Harness-level guard, identical for BEFORE and AFTER: deny reading files the tests need."""
    return json.dumps({"permissions": {"deny": [f"Read(./{p})" for p in needs]}})


def changed_files(copy: Path) -> list[str]:
    out = sh(copy, "status", "--porcelain", "-uall")
    return sorted({l[3:].split(" -> ")[-1] for l in out.splitlines() if l.strip()})


def grade(root: Path, copy: Path, task: dict, cmd: str, overlay: list[str]) -> dict:
    touched = [f for f in changed_files(copy) if f not in overlay and not bench.in_scope(f)]
    for t in task["test_files"]:  # reveal the PR's own tests (overwrite any agent version)
        blob = subprocess.run(["git", "-C", str(root), "show", f"{task['merge']}:{t}"], capture_output=True)
        (copy / t).parent.mkdir(parents=True, exist_ok=True)
        (copy / t).write_bytes(blob.stdout)
    result = run_tests(copy, cmd)
    code = set(task["code_files"])
    agent_code = {f for f in touched if not scan.TEST_PART.search(f)}
    overlap = len(code & agent_code) / len(code | agent_code) if (code | agent_code) else 0.0
    return {"passed": result["passed"], "test_seconds": result["seconds"], "test_tail": result["tail"][-600:],
            "files_touched": touched, "same_files_as_pr": round(overlap, 2)}


def cmd_run(a) -> int:
    root = repo_root(a.root)
    a.out, a.tasks = under(root, a.out), under(root, a.tasks)
    for path in (a.out,):
        if not bench.report_path_ok(root, path):
            print(f"refusing to write {path}: outside the repo or under {bench.REPORT_DIR} only", file=sys.stderr)
            return 2
    doc = json.loads(Path(a.tasks).read_text())
    tasks = [t for t in doc["tasks"] if t["validation"]["valid"]][: a.max_tasks]
    if not tasks:
        print("no valid tasks; run `evals.py build` first", file=sys.stderr)
        return 2
    overlay = bench.instruction_files(root)
    template, python = doc["test_cmd"], doc.get("python") or default_python(root)
    prefix = render_cmd(template, python, ["__T__"]).split("__T__")[0].strip()
    n = len(tasks) * a.runs * 2
    print(f"Eval plan for {root.name}: {len(tasks)} tasks x {a.runs} runs x 2 sides = {n} agent runs, "
          f"each capped at ${a.per_run_usd:.2f}; stops at ${a.budget_usd:.2f}. Model: {a.model or 'default'}.")
    for t in tasks:
        print(f"- {t['id']}: {t['title'][:70]} ({len(t['code_files'])} code files; graded by {len(t['test_files'])} test files)")
    if not a.yes:
        print("Plan only. Re-run with --yes to spend tokens.")
        return 0

    start = bench.snapshot(root)
    work = Path(tempfile.mkdtemp(prefix="agent-readiness-evals-"))
    results, spent, stopped = [], 0.0, None
    try:
        for i in range(a.runs):
            for t in tasks:
                for side in (("before", "after") if i % 2 == 0 else ("after", "before")):
                    if spent >= a.budget_usd:
                        stopped = "budget"
                        break
                    copy = work / f"{t['id']}-{side}-{i}" / root.name
                    bench.make_copy(root, t["base_sha"], copy, overlay if side == "after" else None)
                    hidden = bench.sensitive_files(root, copy)
                    bench.strip(copy, [h for h in hidden if h not in t["validation"]["needs_files"]])
                    cmd = render_cmd(template, python, t["test_files"])
                    prompt = PROMPT.format(title=t["title"], details=t["details"], test_cmd=render_cmd(template, python, ["<test path>"]))
                    r = bench.run_claude(copy, prompt, agent_tools(prefix), AGENT_BLOCKED, a.model, a.per_run_usd,
                                         extra=["--settings", harness_settings(t["validation"]["needs_files"])])
                    g = grade(root, copy, t, cmd, overlay if side == "after" else [])
                    r.pop("tool_results", None)
                    spent += r["cost"]
                    results.append({"task": t["id"], "side": side, "run": i, **{k: r[k] for k in ("cost", "turns", "tool_calls", "input_total", "output", "ok", "subtype")}, **g})
                    print(f"  [{len(results)}/{n}] {t['id']} {side}: {'PASS' if g['passed'] else 'fail'}, ${r['cost']:.2f}, "
                          f"{r['turns']} turns, files match {g['same_files_as_pr']:.0%}", flush=True)
                    shutil.rmtree(copy.parent, ignore_errors=True)
    except KeyboardInterrupt:
        stopped = "interrupted"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    changed = bench.snapshot(root) != start
    md = run_report(root.name, doc, tasks, results, spent, stopped, changed, a)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md)
        Path(a.out).with_suffix(".json").write_text(json.dumps({"tasks": tasks, "results": results}, indent=2))
    print(md)
    return 3 if changed else 0


def run_report(repo, doc, tasks, results, spent, stopped, changed, a) -> str:
    def side(task, s):
        return [r for r in results if r["task"] == task and r["side"] == s]
    L = [f"# Eval: can an agent redo merged changes in {repo}?", "",
         "<!-- Generated by agent-readiness evals.py. Re-run instead of editing. -->", "",
         f"- Tasks: {len(tasks)} merged PRs that change code and tests, validated (their tests fail before the PR and pass after).",
         f"- Each task: the agent gets the PR's title and commit list on the commit before the PR, implements it, "
         f"then the PR's own tests (hidden from the agent) grade it. {a.runs} run(s) per side.",
         "- BEFORE: no new instructions. AFTER: the working tree's instruction files added.",
         f"- Spent ${spent:.2f}{' (stopped: ' + stopped + ')' if stopped else ''}. Repo check: "
         + ("**CHANGED: inspect git status**" if changed else "HEAD, working tree and stashes unchanged") + ".", "",
         "| Task | Before: pass | After: pass | Cost before → after | Turns before → after | Same files as PR before → after |",
         "|---|---|---|---|---|---|"]
    tb = ta = 0
    for t in tasks:
        b, af = side(t["id"], "before"), side(t["id"], "after")
        pb, pa = sum(r["passed"] for r in b), sum(r["passed"] for r in af)
        tb, ta = tb + pb, ta + pa
        m = lambda rs, k: bench.med([r[k] for r in rs])
        L.append(f"| {t['id']}: {t['title'][:50]} | {pb}/{len(b)} | {pa}/{len(af)} | ${m(b, 'cost'):.2f} → ${m(af, 'cost'):.2f} | "
                 f"{m(b, 'turns'):.0f} → {m(af, 'turns'):.0f} | {m(b, 'same_files_as_pr'):.0%} → {m(af, 'same_files_as_pr'):.0%} |")
    nb = len([r for r in results if r["side"] == "before"])
    na = len([r for r in results if r["side"] == "after"])
    L += ["", f"**Pass rate:** before {tb}/{nb}, after {ta}/{na}.", "",
          "## Caveats", "",
          "- Few tasks and runs: one task flipping changes the rate a lot. Treat it as a smoke test, not a score.",
          "- The PR's commit messages are the spec; real tickets may say more or less.",
          "- Grading runs the agent's code in the temp copy with a minimal environment; network is not blocked.", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("root", nargs="?", default=".")
    b.add_argument("--test-cmd", required=True, help="template with {python} and {tests}")
    b.add_argument("--python")
    b.add_argument("--branch", default="HEAD")
    b.add_argument("--scan", type=int, default=200, help="how many recent merges to look at")
    b.add_argument("--max-tasks", type=int, default=6)
    b.add_argument("--out", default=DEFAULT_TASKS)
    r = sub.add_parser("run")
    r.add_argument("root", nargs="?", default=".")
    r.add_argument("--tasks", default=DEFAULT_TASKS)
    r.add_argument("--max-tasks", type=int, default=6)
    r.add_argument("--runs", type=int, default=1)
    r.add_argument("--model")
    r.add_argument("--per-run-usd", type=float, default=3.0)
    r.add_argument("--budget-usd", type=float, default=25.0)
    r.add_argument("--out")
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    return cmd_build(a) if a.cmd == "build" else cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())
