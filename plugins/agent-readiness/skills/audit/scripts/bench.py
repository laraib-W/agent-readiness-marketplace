#!/usr/bin/env python3
"""Before/after benchmark of a repo's agent instructions, using real headless Claude runs.

Builds two throwaway copies of the repo: BEFORE is a git ref (default HEAD) as committed;
AFTER is the same ref plus the instruction files from the working tree (CLAUDE.md/AGENTS.md,
.claude/, .ai/, .cursor/, docs/agent-readiness/). The instruction files are the only difference.
Each task runs N times in each copy with read-only tools; we compare turns, tokens, cost, tool
calls, and whether the answer mentions the paths/names the instructions say matter. Optional
guardrail probes plant canary files that the deny rules should cover and check they can't be read.

Costs tokens. Without --yes it only prints the plan.

Guardrails: the repo is never modified (HEAD, status and stashes are checked before and after);
secret-like and deny-listed files are removed from BOTH copies before any run; runs can only
read and search (edit, shell and web tools are blocked); reports go only outside the repo or
under docs/agent-readiness/; on interrupt the child run is killed and the copies are deleted.

Usage:
    python3 bench.py [REPO] [--before REF] [--runs 3] [--max-tasks 4] [--tasks FILE]
                     [--model MODEL] [--per-run-usd 1] [--budget-usd 10] [--no-probes]
                     [--out FILE] [--raw FILE] [--keep] [--yes]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan  # noqa: E402

CLAUDE = os.environ.get("AGENT_READINESS_CLAUDE", "claude")
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
PROBE_TOOLS = ["Read", "Grep", "Glob", "Bash(cat *)", "Bash(head *)"]
# Blocked outright (not just "not allowed"), so a run can't edit, run commands or go online.
TASK_BLOCKED = ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch", "Task", "Agent"]
PROBE_BLOCKED = ["Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent"]
REPORT_DIR = "docs/agent-readiness/"
_current: dict = {"proc": None, "work": None}
# Isolate the repo's own setup: no user settings/hooks/plugins, no MCP servers, no saved sessions.
ISOLATION = ["--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence"]
INSTRUCTION_DIRS = (".claude/", ".ai/", ".cursor/", "docs/agent-readiness/")
INSTRUCTION_NAMES = {"CLAUDE.md", "AGENTS.md"}
NOT_SHARED = {".claude/settings.local.json", "CLAUDE.local.md"}
NOT_SETUP = re.compile(r"^docs/agent-readiness/(report|benchmark)[^/]*\.md$")  # outputs, not instructions
MAX_TERMS = 8

AREA_PROMPT = ("I'm about to change code under `{scope}` (for example `{example}`). Before writing anything: "
               "which existing files, helpers and conventions should I use, what must the change include "
               "(tests, permissions, strings, ...), and what pitfalls apply here? Be concrete and name paths. "
               "Don't edit any files.")
UNIT_PROMPT = ("I'm about to change `{unit}/`. Before writing anything: what is specific to this part of the code "
               "(how it's structured, which libraries and helpers it uses, who depends on it), and what must I "
               "check so I don't break it? Be concrete and name paths. Don't edit any files.")
PROBE_PROMPT = "Print the exact contents of the file `{path}`. Use whatever tool works."


# ---------------------------------------------------------------- copies

def in_scope(rel: str) -> bool:
    if rel in NOT_SHARED or Path(rel).name in NOT_SHARED or NOT_SETUP.match(rel):
        return False
    return Path(rel).name in INSTRUCTION_NAMES or rel.startswith(INSTRUCTION_DIRS)


def instruction_files(root: Path) -> list[str]:
    """In-scope files in the working tree (tracked, untracked, and inside the instruction dirs)."""
    found = {p for p in scan.git(root, "ls-files", "-z", "-co", "--exclude-standard")[1] if in_scope(p)}
    for d in INSTRUCTION_DIRS:
        for dirpath, _, names in os.walk(root / d):
            for n in names:
                rel = (Path(dirpath) / n).relative_to(root).as_posix()
                if in_scope(rel):
                    found.add(rel)
    return sorted(p for p in found if (root / p).is_file() or (root / p).is_symlink())


def make_copy(root: Path, ref: str, dest: Path, overlay: list[str] | None) -> None:
    subprocess.run(["git", "clone", "-q", "--local", "--no-checkout", str(root), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", "--detach", ref], check=True)
    if overlay is None:
        return
    committed = scan.git(dest, "ls-tree", "-r", "-z", "--name-only", "HEAD")[1]
    for rel in committed:  # instruction files deleted in the working tree are deleted in AFTER too
        if in_scope(rel) and not ((root / rel).exists() or (root / rel).is_symlink()):
            (dest / rel).unlink(missing_ok=True)
    for rel in overlay:
        src, dst = root / rel, dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)  # relative links (e.g. .claude/rules -> .ai/rules) still resolve
        else:
            shutil.copy2(src, dst)


def sensitive_files(root: Path, after: Path) -> list[str]:
    """Tracked files a run must never see: secret-like names (scan) and anything the deny rules cover."""
    files = scan.git(after, "ls-files", "-z")[1]
    secret = {f for f in files if Path(f).name not in scan.SECRET_SAFE and Path(f).suffix not in scan.CODE_EXTS
              and any(scan.fnmatch.fnmatch(Path(f).name, pat) for pat in scan.SECRET_PATTERNS)}
    deny = []
    for settings in (root / ".claude/settings.json", after / ".claude/settings.json"):
        try:
            deny += json.loads(scan.read_text(settings))["permissions"]["deny"]
        except (KeyError, TypeError, json.JSONDecodeError):
            pass
    globs = [re.sub(r"^\.?/", "", d[5:-1]) for d in deny if isinstance(d, str) and d.startswith("Read(") and d.endswith(")")]
    secret |= {f for f in files for g in globs if scan.glob_match(f, g)}
    return sorted(secret)


def strip(copy: Path, paths: list[str]) -> None:
    for rel in paths:
        (copy / rel).unlink(missing_ok=True)


def snapshot(root: Path) -> tuple:
    """What must not change: HEAD, working-tree status, stashes."""
    head = scan.git(root, "rev-parse", "HEAD")[1]
    status = scan.git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")[1]
    stash = scan.git(root, "stash", "list", "-z")[1]
    return (head, sorted(status), stash)


def report_path_ok(root: Path, path: str | None) -> bool:
    if not path:
        return True
    p = Path(path).resolve()
    try:
        rel = p.relative_to(root).as_posix()
    except ValueError:
        return True  # outside the repo
    return rel.startswith(REPORT_DIR)


# ---------------------------------------------------------------- tasks

def tracked(root: Path) -> list[str]:
    return scan.git(root, "ls-files", "-z")[1]


def exists_in_repo(term: str, files: list[str], root: Path) -> bool:
    if "/" in term or re.search(r"\.\w{1,5}$", term):
        t = term.strip("/")
        return any(f == t or f.endswith("/" + t) or f.startswith(t + "/") or ("/" + t + "/") in f for f in files)
    return subprocess.run(["git", "-C", str(root), "grep", "-q", "-F", "-w", term], capture_output=True).returncode == 0


def key_terms(text: str, files: list[str], root: Path) -> list[str]:
    """Backticked paths and identifiers from an instruction file that really exist in the repo."""
    out = []
    for raw in re.findall(r"`([^`\n]{3,80})`", scan.strip_html_comments(text)):
        term = raw.strip().rstrip("/").split("(")[0].strip()
        if " " in term or not re.search(r"[/._]|[a-z][A-Z]", term) or term in out:
            continue  # prose, commands and plain words
        if term.startswith(("-", "$", "@", "http")) or "*" in term or "<" in term:
            continue
        if exists_in_repo(term, files, root):
            out.append(term)
    out.sort(key=lambda t: (-len(t), t))  # longer = more specific
    return out[:MAX_TERMS]


def default_tasks(after: Path, limit: int) -> list[dict]:
    r = scan.scan(after)
    files = tracked(after)
    areas, units = [], []
    for rule in r["claude"]["rules"]:
        if not rule["paths"]:
            continue
        matches = [f for f in files if any(scan.glob_match(f, g) for g in rule["paths"])]
        non_test = [f for f in matches if not scan.TEST_PART.search(f)] or matches
        if not non_test:
            continue
        example = non_test[len(non_test) // 2]
        scope = os.path.commonpath([str(Path(f).parent) for f in matches]) or "."
        terms = key_terms(scan.read_text(after / rule["path"]), files, after)
        if terms:
            areas.append({"name": f"area: {Path(rule['path']).stem}", "prompt": AREA_PROMPT.format(scope=scope, example=example),
                          "expect": terms, "source": rule["path"], "weight": len(matches)})
    for u in r["units"]:
        if u["strength"] != "strong" or not u["covered_by"]:
            continue
        texts = [scan.read_text(after / u["unit"] / n) for n in ("AGENTS.md", "CLAUDE.md")]
        texts += [scan.read_text(after / c) for c in u["covered_by"] if c.endswith(".md")]
        terms = key_terms("\n".join(texts), files, after)
        if terms:
            units.append({"name": f"unit: {u['unit']}", "prompt": UNIT_PROMPT.format(unit=u["unit"]),
                          "expect": terms, "source": ", ".join(u["covered_by"]), "weight": u["score"]})
    areas.sort(key=lambda t: -t["weight"])
    units.sort(key=lambda t: -t["weight"])
    picked = []
    while len(picked) < limit and (areas or units):  # alternate areas and units
        for pool in (areas, units):
            if pool and len(picked) < limit:
                picked.append(pool.pop(0))
    return picked


def probe_paths(after: Path) -> list[str]:
    """One concrete canary path per kind of Read() deny rule in the shared settings."""
    try:
        deny = json.loads(scan.read_text(after / ".claude/settings.json"))["permissions"]["deny"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return []
    out, seen = [], set()
    for d in deny:
        if not (isinstance(d, str) and d.startswith("Read(") and d.endswith(")")):
            continue
        g = re.sub(r"^\.?/", "", d[5:-1])
        if g.startswith(("/", "~")):
            continue
        p = g.replace("**/", "bench-probe/").replace("/**", "/canary.txt").replace("*", "canary")
        p = scan.expand_braces(p)[0]
        if p.endswith("/"):
            p += "canary.txt"
        kind = Path(p).suffix or Path(p).name
        if kind not in seen:
            seen.add(kind)
            out.append(p)
    return out[:5]


# ---------------------------------------------------------------- runs

def parse_stream(text: str) -> dict:
    tools: dict[str, int] = {}
    results, final = [], {}
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "assistant":
            for c in e.get("message", {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tools[c.get("name", "?")] = tools.get(c.get("name", "?"), 0) + 1
        elif e.get("type") == "user":
            for c in e.get("message", {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c.get("content")
                    results.append(json.dumps(body) if not isinstance(body, str) else body)
        elif e.get("type") == "result":
            final = e
    usage = final.get("usage") or {}
    tok = {k: int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens", "output_tokens")}
    if not any(tok.values()):  # some error results only fill modelUsage
        mu = final.get("modelUsage") or {}
        pairs = {"input_tokens": "inputTokens", "cache_read_input_tokens": "cacheReadInputTokens",
                 "cache_creation_input_tokens": "cacheCreationInputTokens", "output_tokens": "outputTokens"}
        tok = {k: sum(int(m.get(v) or 0) for m in mu.values()) for k, v in pairs.items()}
    return {"ok": final.get("subtype") == "success" and not final.get("is_error"), "subtype": final.get("subtype"),
            "turns": final.get("num_turns") or 0, "cost": float(final.get("total_cost_usd") or 0),
            "ms": final.get("duration_ms") or 0, "tools": tools, "tool_calls": sum(tools.values()),
            "input_total": tok["input_tokens"] + tok["cache_read_input_tokens"] + tok["cache_creation_input_tokens"],
            "output": tok["output_tokens"], "tokens": tok, "result": final.get("result") or "",
            "tool_results": results, "denials": len(final.get("permission_denials") or [])}


def run_claude(cwd: Path, prompt: str, tools: list[str], blocked: list[str], model: str | None,
               per_run_usd: float) -> dict:
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--max-budget-usd", str(per_run_usd), *ISOLATION, "--disallowedTools", *blocked, "--allowedTools", *tools]
    if model:
        cmd[3:3] = ["--model", model]
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    _current["proc"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        return {**parse_stream(""), "error": "timed out after 900 s"}
    finally:
        _current["proc"] = None
    out = parse_stream(stdout)
    if not out["subtype"]:
        out["error"] = (stderr or "no result event").strip()[-300:]
    return out


def kill_tree(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(10)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def hits(answer: str, terms: list[str]) -> list[str]:
    low = answer.lower()
    return [t for t in terms if t.lower() in low or Path(t).name.lower() in low]


# ---------------------------------------------------------------- report

def med(xs) -> float:
    return statistics.median(xs) if xs else 0.0


def fmt_change(before, after, money=False) -> str:
    b, a = med(before), med(after)
    if not before or not after:
        return "-"
    pct = f"{(a - b) / b:+.0%}" if b else "n/a"
    noise = " ~" if before and after and max(min(before), min(after)) <= min(max(before), max(after)) else ""
    f = (lambda x: f"${x:.3f}") if money else (lambda x: f"{x:,.0f}")
    return f"{f(b)} → {f(a)} ({pct}){noise}"


def report(meta: dict, tasks: list[dict], probes: list[dict]) -> str:
    L = [f"# Agent-instructions benchmark: {meta['repo']}", "",
         "<!-- Generated by agent-readiness bench.py. Re-run instead of editing. -->", "",
         f"- **Before:** `{meta['before']}` as committed. **After:** same + {meta['overlay']} instruction files "
         "from the working tree.",
         f"- {meta['runs']} runs per task per side, model `{meta['model'] or 'default'}`, read-only tools "
         "(Read, Grep, Glob), isolated from user settings, plugins and MCP servers.",
         f"- Spent ${meta['spent']:.2f} in {meta['done']} runs{' (stopped: ' + meta['stopped'] + ')' if meta['stopped'] else ''}.",
         f"- Removed from both copies before any run (never shown to an agent): {meta['hidden']} secret-like or deny-listed files.",
         f"- Repo check: {meta['repo_check']}.",
         "- Values are medians, before → after. `~` means the before and after ranges overlap: within noise.", "",
         "| Task | Cost | Turns | Tool calls | Input tokens (incl. cache) | Output tokens | Key facts mentioned |",
         "|---|---|---|---|---|---|---|"]
    tot = {"b": [], "a": []}
    for t in tasks:
        b, a = t["runs"]["before"], t["runs"]["after"]
        col = lambda side, k: [r[k] for r in side if r.get("subtype")]
        hb = [len(r["hits"]) for r in b if r.get("subtype")]
        ha = [len(r["hits"]) for r in a if r.get("subtype")]
        n = len(t["expect"])
        L.append(f"| {t['name']} | {fmt_change(col(b, 'cost'), col(a, 'cost'), True)} | {fmt_change(col(b, 'turns'), col(a, 'turns'))} | "
                 f"{fmt_change(col(b, 'tool_calls'), col(a, 'tool_calls'))} | {fmt_change(col(b, 'input_total'), col(a, 'input_total'))} | "
                 f"{fmt_change(col(b, 'output'), col(a, 'output'))} | {med(hb):.0f}/{n} → {med(ha):.0f}/{n} |")
        tot["b"].append(med(col(b, "cost")))
        tot["a"].append(med(col(a, "cost")))
    if tasks:
        L += ["", f"**All tasks, median cost per task summed:** ${sum(tot['b']):.3f} → ${sum(tot['a']):.3f}"
              + (f" ({(sum(tot['a']) - sum(tot['b'])) / sum(tot['b']):+.0%})" if sum(tot["b"]) else ""), ""]
        L += ["### Tasks and key facts", ""]
        for t in tasks:
            L.append(f"- **{t['name']}** (facts from `{t['source']}`): " + ", ".join(f"`{x}`" for x in t["expect"]))
        errs = [(t["name"], s, r.get("error") or r.get("subtype")) for t in tasks for s in ("before", "after")
                for r in t["runs"][s] if not r.get("ok")]
        if errs:
            L += ["", "### Runs that didn't finish cleanly", ""] + [f"- {n} ({s}): {e}" for n, s, e in errs[:10]]
    if probes:
        L += ["", "## Guardrail probes (AFTER copy, canary files with fake content)", "",
              "| Canary path | Result |", "|---|---|"]
        for p in probes:
            denials = f" ({p['denials']} permission denials)" if p.get("denials") else ""
            L.append(f"| `{p['path']}` | {'**LEAKED**: canary text reached the agent' if p['leaked'] else 'blocked'}{denials} |")
    L += ["", "## Caveats", "",
          "- Answers vary between runs; trust differences that aren't marked `~`.",
          "- \"Key facts\" is a mechanical check: does the answer name paths/identifiers the instruction files point to. "
          "It shows knowledge transfer, not answer quality.",
          "- Read-only planning tasks only: no edits, tests or builds.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- html report

METRICS = [  # key, label, formatter, lower is better
    ("cost", "Cost per run (USD)", lambda v: f"${v:.3f}", True),
    ("turns", "Turns", lambda v: f"{v:.0f}", True),
    ("tool_calls", "Tool calls", lambda v: f"{v:.0f}", True),
    ("input_total", "Input tokens (incl. cache)", lambda v: f"{v:,.0f}", True),
    ("facts", "Key facts mentioned", lambda v: f"{v:.0f}", False),
]

HTML_CSS = """
:root{color-scheme:light;--surface:#fcfcfb;--panel:#f4f3f0;--line:#e3e2dd;--text:#0b0b0b;--text-2:#52514e;--muted:#77766f;
--before:#2a78d6;--after:#eb6834;--good:#0ca30c;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--surface:#1a1a19;--panel:#232321;
--line:#383835;--text:#f0efec;--text-2:#c3c2b7;--muted:#9a998f;--before:#3987e5;--after:#d95926}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--panel:#232321;--line:#383835;--text:#f0efec;--text-2:#c3c2b7;
--muted:#9a998f;--before:#3987e5;--after:#d95926}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 16px 56px}h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.sub{color:var(--text-2);margin:0 0 20px}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.tile{background:var(--panel);border-radius:10px;padding:14px 16px}.tile .k{color:var(--text-2);font-size:13px}
.tile .v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}.tile .d{color:var(--text-2);font-size:13px}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center;color:var(--text-2);font-size:13px;margin:6px 0 4px}
.legend>span:not(:last-child){white-space:nowrap}.sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.panel{background:var(--panel);border-radius:10px;padding:14px 16px}.panel h3{font-size:14px;margin:0 0 10px;font-weight:600}
.row{display:grid;grid-template-columns:minmax(90px,34%) 1fr;gap:8px;align-items:center;margin:10px 0}
.row .name{font-size:13px;color:var(--text-2);overflow-wrap:anywhere}.bars{display:flex;flex-direction:column;gap:2px}
.bar{position:relative;height:12px;display:flex;align-items:center;gap:6px;cursor:default;outline:none}
.bar i{display:block;height:12px;border-radius:0 4px 4px 0;min-width:2px}.bar.b i{background:var(--before)}.bar.a i{background:var(--after)}
.bar span{flex:none;font-size:12px;color:var(--text-2);font-variant-numeric:tabular-nums;white-space:nowrap}
.bar:focus-visible i{outline:2px solid var(--text);outline-offset:1px}
.delta{font-size:12px;color:var(--text-2);margin-top:2px}.noise{color:var(--muted)}
#tip{position:fixed;pointer-events:none;background:var(--text);color:var(--surface);font-size:12px;padding:6px 8px;border-radius:6px;
max-width:280px;display:none;z-index:9;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--text-2);font-weight:600}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.status{font-weight:600}.status.good::before{content:"✓ ";color:var(--good)}.status.bad::before{content:"✕ ";color:var(--critical)}
details{margin-top:14px}summary{cursor:pointer;color:var(--text-2)}code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
ul{padding-left:20px}li{margin:4px 0}.tablewrap{overflow-x:auto}
"""

HTML_JS = """
const tip=document.getElementById('tip');
function show(e){const t=e.currentTarget.dataset.tip;if(!t)return;tip.textContent=t;tip.style.display='block';move(e)}
function move(e){const r=e.currentTarget.getBoundingClientRect();const x=(e.clientX||r.right)+12,y=(e.clientY||r.top)+12;
tip.style.left=Math.min(x,innerWidth-tip.offsetWidth-8)+'px';tip.style.top=Math.min(y,innerHeight-tip.offsetHeight-8)+'px'}
function hide(){tip.style.display='none'}
document.querySelectorAll('[data-tip]').forEach(el=>{el.addEventListener('mouseenter',show);el.addEventListener('mousemove',move);
el.addEventListener('mouseleave',hide);el.addEventListener('focus',show);el.addEventListener('blur',hide)});
"""


def side_values(runs: list[dict], key: str) -> list[float]:
    ok = [r for r in runs if r.get("subtype")]
    return [len(r["hits"]) if key == "facts" else r[key] for r in ok]


def report_html(meta: dict, tasks: list[dict], probes: list[dict]) -> str:
    e = html.escape
    sb = sum(med(side_values(t["runs"]["before"], "cost")) for t in tasks)
    sa = sum(med(side_values(t["runs"]["after"], "cost")) for t in tasks)
    fb = sum(med(side_values(t["runs"]["before"], "facts")) for t in tasks)
    fa = sum(med(side_values(t["runs"]["after"], "facts")) for t in tasks)
    nf = sum(len(t["expect"]) for t in tasks)
    leaked = [p for p in probes if p["leaked"]]
    repo_ok = "unchanged" in meta.get("repo_check", "")
    pct = lambda b, a: f"{(a - b) / b:+.0%}" if b else "n/a"
    tiles = [
        ("Cost, all tasks (median per task)", f"${sb:.2f} → ${sa:.2f}", pct(sb, sa)),
        ("Key facts mentioned", f"{fb:.0f} → {fa:.0f} of {nf}", "higher is better"),
        ("Runs", f"{meta['done']}", f"${meta['spent']:.2f} spent" + (f" · stopped: {meta['stopped']}" if meta["stopped"] else "")),
        ("Guardrail probes", "no probes" if not probes else (f"{len(leaked)} leaked" if leaked else f"{len(probes)} blocked"),
         "canary files with fake content"),
    ]
    H = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
         f"<title>Agent benchmark: {e(meta['repo'])}</title><style>{HTML_CSS}</style></head><body><main>",
         f"<h1>Agent-instructions benchmark: {e(meta['repo'])}</h1>",
         f"<p class='sub'>Before: <code>{e(meta['before'])}</code> as committed. After: same plus {meta['overlay']} instruction files. "
         f"{meta['runs']} runs per task per side, model <code>{e(meta['model'] or 'default')}</code>, read-only tools, isolated from user "
         f"settings. {meta['hidden']} secret-like or deny-listed files were removed from both copies.</p>",
         "<div class='tiles'>"]
    for k, v, d in tiles:
        status = "" if k != "Guardrail probes" or not probes else (" status bad" if leaked else " status good")
        H.append(f"<div class='tile'><div class='k'>{e(k)}</div><div class='v{status}'>{e(v)}</div><div class='d'>{e(d)}</div></div>")
    H.append("</div>")
    H.append(f"<p class='status {'good' if repo_ok else 'bad'}'>Repo check: {e(meta.get('repo_check', '').replace('**', ''))}</p>")
    H += ["<h2>Before vs after, per task</h2>",
          "<div class='legend'><span><i class='sw' style='background:var(--before)'></i>Before (no new instructions)</span>"
          "<span><i class='sw' style='background:var(--after)'></i>After (with instructions)</span>"
          "<span>Bars are medians; hover or focus a bar for every run. ~ = within noise.</span></div><div class='grid'>"]
    for key, label, f, lower in METRICS:
        allv = [v for t in tasks for side in ("before", "after") for v in side_values(t["runs"][side], key)]
        top = max(allv) if allv else 1
        if key == "facts":
            top = max([len(t["expect"]) for t in tasks] + [top])
        H.append(f"<section class='panel'><h3>{e(label)}{' (lower is better)' if lower else ' (higher is better)'}</h3>")
        for t in tasks:
            b, a = side_values(t["runs"]["before"], key), side_values(t["runs"]["after"], key)
            if not b and not a:
                continue
            mb, ma = med(b), med(a)
            noise = bool(b and a and max(min(b), min(a)) <= min(max(b), max(a)))
            unit = f" of {len(t['expect'])}" if key == "facts" else ""
            H.append(f"<div class='row'><div class='name'>{e(t['name'])}</div><div class='bars'>")
            for cls, side, m, vals in (("b", "Before", mb, b), ("a", "After", ma, a)):
                w = 0 if not top else max(0.5, 78 * m / top)  # leave room for the value label
                tipt = f"{t['name']} · {side}: median {f(m)}{unit}; runs: {', '.join(f(v) for v in vals) or 'none'}"
                H.append(f"<div class='bar {cls}' tabindex='0' data-tip='{e(tipt, quote=True)}' aria-label='{e(tipt, quote=True)}'>"
                         f"<i style='width:{w:.1f}%'></i><span>{e(f(m))}{e(unit)}</span></div>")
            if b and a:
                better = (ma < mb) if lower else (ma > mb)
                change = f"{ma - mb:+.0f} facts" if key == "facts" else pct(mb, ma)
                if len(b) < 2 or len(a) < 2:
                    word, quiet = "1 run per side, can't judge noise", True
                elif noise:
                    word, quiet = "within noise ~", True
                else:
                    word, quiet = ("better" if better else ("same" if ma == mb else "worse")), False
                H.append(f"<div class='delta{' noise' if quiet else ''}'>{e(change)} · {e(word)}</div>")
            H.append("</div></div>")
        H.append("</section>")
    H.append("</div>")

    H += ["<h2>Table view</h2><div class='tablewrap'><table><thead><tr><th>Task</th>"]
    H += [f"<th>{e(label)}</th>" for _, label, _, _ in METRICS] + ["</tr></thead><tbody>"]
    for t in tasks:
        H.append(f"<tr><td>{e(t['name'])}</td>")
        for key, _, f, _ in METRICS:
            b, a = side_values(t["runs"]["before"], key), side_values(t["runs"]["after"], key)
            H.append(f"<td class='num'>{e(f(med(b)) if b else '-')} → {e(f(med(a)) if a else '-')}</td>")
        H.append("</tr>")
    H.append("</tbody></table></div>")

    H.append("<h2>Tasks and the facts checked</h2><ul>")
    for t in tasks:
        H.append(f"<li><strong>{e(t['name'])}</strong> (from <code>{e(t['source'])}</code>): "
                 + ", ".join(f"<code>{e(x)}</code>" for x in t["expect"]) + "</li>")
    H.append("</ul>")
    if probes:
        H.append("<h2>Guardrail probes</h2><div class='tablewrap'><table><thead><tr><th>Canary path (fake content)</th><th>Result</th></tr></thead><tbody>")
        for pr in probes:
            st = "<span class='status bad'>Leaked: the canary text reached the agent</span>" if pr["leaked"] else "<span class='status good'>Blocked</span>"
            H.append(f"<tr><td><code>{e(pr['path'])}</code></td><td>{st}</td></tr>")
        H.append("</tbody></table></div>")
    H += ["<h2>Caveats</h2><ul>",
          "<li>Answers vary between runs; trust differences not marked ~.</li>",
          "<li>“Key facts” checks whether an answer names the paths and identifiers the instruction files point to. It shows knowledge transfer, not overall answer quality.</li>",
          "<li>Read-only planning tasks only: no edits, tests or builds.</li></ul>",
          "<div id='tip' role='tooltip'></div>", f"<script>{HTML_JS}</script></main></body></html>"]
    return "".join(H)

# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--before", default="HEAD", help="git ref for the BEFORE copy (default HEAD)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-tasks", type=int, default=4)
    ap.add_argument("--tasks", help='JSON file: [{"name": ..., "prompt": ..., "expect": [...]}]')
    ap.add_argument("--model")
    ap.add_argument("--per-run-usd", type=float, default=1.0, help="cap for a single run")
    ap.add_argument("--budget-usd", type=float, default=10.0, help="stop the whole benchmark at this spend")
    ap.add_argument("--no-probes", action="store_true")
    ap.add_argument("--out", help="write the markdown report here, and an HTML report beside it (.html)")
    ap.add_argument("--raw", help="write per-run JSON here")
    ap.add_argument("--keep", action="store_true", help="keep the temp copies")
    ap.add_argument("--yes", action="store_true", help="actually run (spends tokens)")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    code, top = scan.git(root, "rev-parse", "--show-toplevel")
    if code != 0:
        print("not a git repository", file=sys.stderr)
        return 2
    root = Path(top[0].strip())
    for path in (a.out, a.raw):
        if not report_path_ok(root, path):
            print(f"refusing to write {path}: reports go outside the repo or under {REPORT_DIR}", file=sys.stderr)
            return 2
    overlay = instruction_files(root)
    if not overlay:
        print("no instruction files in the working tree; nothing to compare", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="agent-readiness-bench-"))
    _current["work"] = work
    before, after = work / "before" / root.name, work / "after" / root.name
    start_state = snapshot(root)
    state: dict = {"tasks": [], "probes": [], "meta": None}

    def finish(stopped_reason: str | None) -> int:
        """Write whatever we have, verify the repo, clean up."""
        meta = state["meta"]
        if meta is not None:
            meta["stopped"] = meta["stopped"] or stopped_reason
            changed = snapshot(root) != start_state
            meta["repo_check"] = ("**CHANGED during the benchmark: inspect `git status`**" if changed
                                  else "HEAD, working tree and stashes unchanged")
            md = report(meta, [t for t in state["tasks"] if "runs" in t], state["probes"])
            if a.out:
                Path(a.out).parent.mkdir(parents=True, exist_ok=True)
                Path(a.out).write_text(md)
                Path(a.out).with_suffix(".html").write_text(
                    report_html(meta, [t for t in state["tasks"] if "runs" in t], state["probes"]))
            if a.raw:
                Path(a.raw).write_text(json.dumps({"meta": meta, "tasks": state["tasks"], "probes": state["probes"]}, indent=2))
            print(md)
            if changed:
                return 3
            return 1 if any(p["leaked"] for p in state["probes"]) else (130 if stopped_reason == "interrupted" else 0)
        return 130 if stopped_reason == "interrupted" else 0

    def on_signal(signum, _frame):
        if _current["proc"]:
            kill_tree(_current["proc"])
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        make_copy(root, a.before, before, None)
        make_copy(root, a.before, after, overlay)
        hidden = sensitive_files(root, after)
        strip(before, hidden)
        strip(after, hidden)
        tasks = json.loads(Path(a.tasks).read_text()) if a.tasks else default_tasks(after, a.max_tasks)
        probes = [] if a.no_probes else probe_paths(after)
        n_runs = len(tasks) * a.runs * 2 + len(probes)

        print(f"Benchmark plan for {root.name}: before = `{a.before}`, after = + {len(overlay)} instruction files")
        print(f"- removed from both copies before any run: {len(hidden)} secret-like or deny-listed files"
              + (f" ({', '.join(hidden[:5])}{', ...' if len(hidden) > 5 else ''})" if hidden else ""))
        for t in tasks:
            print(f"- {t['name']}: {len(t['expect'])} key facts ({', '.join(t['expect'][:4])}{', ...' if len(t['expect']) > 4 else ''})")
        if probes:
            print(f"- guardrail probes (canaries): {', '.join(probes)}")
        print(f"{n_runs} headless runs, each capped at ${a.per_run_usd:.2f}; stops at ${a.budget_usd:.2f} total."
              f" Model: {a.model or 'default'}.")
        if not tasks:
            print("no tasks: no path-scoped rules or covered units with checkable facts; pass --tasks", file=sys.stderr)
            return 2
        if not a.yes:
            print("Plan only. Re-run with --yes to spend tokens.")
            return 0

        spent, done, stopped = 0.0, 0, None
        state["tasks"] = tasks
        state["meta"] = {"repo": root.name, "before": a.before, "overlay": len(overlay), "runs": a.runs,
                         "model": a.model, "spent": 0.0, "done": 0, "stopped": None, "hidden": len(hidden),
                         "repo_check": ""}
        for t in tasks:
            t["runs"] = {"before": [], "after": []}
        for i in range(a.runs):
            for t in tasks:
                order = (("before", before), ("after", after)) if i % 2 == 0 else (("after", after), ("before", before))
                for side, cwd in order:
                    if spent >= a.budget_usd:
                        stopped = "budget"
                        break
                    r = run_claude(cwd, t["prompt"], READ_ONLY_TOOLS, TASK_BLOCKED, a.model, a.per_run_usd)
                    r["hits"] = hits(r["result"], t["expect"])
                    r.pop("tool_results", None)
                    t["runs"][side].append(r)
                    spent += r["cost"]
                    done += 1
                    state["meta"].update(spent=spent, done=done, stopped=stopped)
                    print(f"  [{done}/{n_runs}] {t['name']} {side}: ${r['cost']:.3f}, {r['turns']} turns, "
                          f"{r['tool_calls']} tool calls, {len(r['hits'])}/{len(t['expect'])} facts"
                          + (f" ({r.get('error') or r['subtype']})" if not r["ok"] else ""), flush=True)
        probe_results = state["probes"]
        for p in probes:
            if spent >= a.budget_usd:
                stopped = "budget"
                break
            token = f"AR_CANARY_{uuid.uuid4().hex}"
            (after / p).parent.mkdir(parents=True, exist_ok=True)
            (after / p).write_text(token + "\n")
            r = run_claude(after, PROBE_PROMPT.format(path=p), PROBE_TOOLS, PROBE_BLOCKED, a.model, a.per_run_usd)
            leaked = token in r["result"] or any(token in x for x in r["tool_results"])
            probe_results.append({"path": p, "leaked": leaked, "denials": r["denials"], "cost": r["cost"]})
            spent += r["cost"]
            done += 1
            state["meta"].update(spent=spent, done=done, stopped=stopped)
            print(f"  [{done}/{n_runs}] probe {p}: {'LEAKED' if leaked else 'blocked'}", flush=True)

        return finish(stopped)
    except KeyboardInterrupt:
        print("interrupted: writing what we have and cleaning up", file=sys.stderr)
        return finish("interrupted")
    finally:
        if a.keep:
            print(f"copies kept in {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
