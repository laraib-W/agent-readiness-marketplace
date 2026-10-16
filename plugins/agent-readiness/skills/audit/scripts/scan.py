#!/usr/bin/env python3
"""Deterministic agent-readiness scan.

Collects facts about a repository's AI-agent setup without using an LLM, so the
agent spends tokens on judgment, not on counting. Standard library only.

Never reads secret files: env/key/credential files are reported by NAME only.

Usage:
    python3 scan.py [REPO_ROOT] [--format json|md] [--fail-on high|medium|low]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build",
    ".next", ".nuxt", "coverage", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", "target", "vendor", ".idea", ".vscode", "staticfiles", ".turbo",
    ".gradle", "Pods", ".terraform",
}
MAX_DEPTH = 6

# Budgets (targets from the context principles; tune per team).
BUDGET_ROOT_CLAUDE = 80
BUDGET_FILE_WARN = 200
BUDGET_RULE = 40
BUDGET_ALWAYS_LOADED = 150
BUDGET_DESCRIPTION_CHARS = 300

# Instruction files used by other AI tools (candidates to merge/reuse).
OTHER_TOOL_FILES = [
    "AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules", ".clinerules",
    ".github/copilot-instructions.md",
]
OTHER_TOOL_DIRS = [".cursor/rules", ".windsurf/rules", ".github/instructions", ".clinerules"]

MANIFESTS = {
    "package.json": "node", "pnpm-workspace.yaml": "node-monorepo", "turbo.json": "node-monorepo",
    "nx.json": "node-monorepo", "lerna.json": "node-monorepo",
    "requirements.txt": "python", "pyproject.toml": "python", "Pipfile": "python",
    "setup.py": "python", "manage.py": "django", "go.mod": "go", "Cargo.toml": "rust",
    "pom.xml": "java", "build.gradle": "java", "build.gradle.kts": "kotlin",
    "Gemfile": "ruby", "composer.json": "php", "pubspec.yaml": "flutter",
    "Dockerfile": "docker", "docker-compose.yml": "docker", "docker-compose.yaml": "docker",
    "compose.yml": "docker", "compose.yaml": "docker", "Makefile": "make",
    "tsconfig.json": "typescript", "angular.json": "angular", "vite.config.ts": "vite",
    "vite.config.js": "vite", "next.config.js": "nextjs", "next.config.mjs": "nextjs",
    "next.config.ts": "nextjs",
}
CI_GLOBS = [".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml",
            "bitbucket-pipelines.yml", ".circleci/config.yml", "Jenkinsfile", "azure-pipelines.yml"]
SECRET_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
                   "credentials*.json", "service-account*.json", "secrets.*", "*.sqlite3", "*.dump"]
SECRET_SAFE = {".env.example", ".env.sample", ".env.template", ".env.dist"}
# Source files named like secrets (e.g. Django's secrets.py) are code, not secrets.
CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rb", ".rs", ".java",
             ".kt", ".php", ".cs", ".swift", ".scala", ".ex", ".exs"}
# Files that answer scorecard criteria S8-S11 (existence only).
HYGIENE = {
    "pre_commit": [".pre-commit-config.yaml", ".husky", "lefthook.yml", "lefthook.yaml", ".lefthook.yml"],
    "codeowners": ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"],
    "pr_template": [".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md",
                    ".github/PULL_REQUEST_TEMPLATE", "pull_request_template.md"],
    "dependency_updates": [".github/dependabot.yml", ".github/dependabot.yaml", "renovate.json",
                           "renovate.json5", ".github/renovate.json", ".renovaterc", ".renovaterc.json"],
    "secret_scanning": [".gitleaks.toml", ".secrets.baseline", ".trufflehog.yml", ".github/secret_scanning.yml"],
    "dev_environment": [".devcontainer", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"],
}
SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

FENCE_RE = re.compile(r"^\s*(```|~~~)")
IMPORT_RE = re.compile(r"(?<![`\w])@([~./\w-][\w./~-]*)")
BACKTICK_PATH_RE = re.compile(r"`([A-Za-z0-9_.~-][\w./-]*/[\w./-]*|[\w-]+\.(?:md|py|ts|tsx|js|json|toml|yml|yaml|cfg|ini|txt))`")
NPM_RE = re.compile(r"\b(?:npm run|yarn(?: run)?|pnpm(?: run)?|bun run)\s+([\w:.-]+)")
NPM_BUILTINS = {"install", "add", "remove", "test", "start", "build", "dev", "i", "ci", "exec", "dlx", "x",
                "workspace", "workspaces", "why", "upgrade", "up", "update", "init", "create", "info",
                "link", "global", "config", "cache", "audit", "outdated", "list", "ls"}
MAKE_RE = re.compile(r"\bmake\s+([\w.-]+)")


def rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def walk(root: Path):
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        depth = len(d.parts) - root_depth
        dirnames[:] = [x for x in dirnames if x not in IGNORE_DIRS and depth < MAX_DEPTH
                       and not (x.startswith(".") and x not in {".claude", ".github", ".cursor", ".windsurf", ".clinerules", ".circleci"})]
        for f in filenames:
            yield d / f


def git(root: Path, *args: str) -> tuple[int, list[str]]:
    """Run git in root; NUL-separated output. (128, []) if not a repo or git is missing."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
        return r.returncode, [x for x in r.stdout.split("\0") if x]
    except (OSError, subprocess.SubprocessError):
        return 128, []


def expand_braces(glob: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", glob)
    if not m:
        return [glob]
    return [x for alt in m.group(1).split(",")
            for x in expand_braces(glob[:m.start()] + alt + glob[m.end():])]


def glob_match(path: str, glob: str) -> bool:
    """Approximates Claude Code globs: `**/` may match zero dirs, `{a,b}` alternatives."""
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.replace("**/", ""))
               for g in expand_braces(glob))


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def strip_code(text: str) -> list[str]:
    """Lines outside fenced code blocks (imports in fences aren't parsed)."""
    out, in_fence = [], False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return out


def code_segments(text: str) -> list[str]:
    """Fenced block lines and inline `code` spans: where real commands live."""
    out, in_fence = [], False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(line)
        else:
            out.extend(re.findall(r"`([^`]+)`", line))
    return out


def strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter parser: scalars and simple lists."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data, key = {}, None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([\w-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val == "":
                data[key] = []
            elif val.startswith("[") and val.endswith("]"):
                data[key] = [v.strip().strip("'\"") for v in re.split(r",(?![^{]*\})", val[1:-1]) if v.strip()]
            else:
                data[key] = val.strip("'\"")
        elif key and line.strip().startswith("- "):
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(line.strip()[2:].strip().strip("'\""))
        elif key and isinstance(data.get(key), str):
            data[key] += " " + line.strip()
    return data


def body_lines(text: str) -> int:
    t = strip_html_comments(text)
    if t.startswith("---"):
        end = t.find("\n---", 3)
        if end != -1:
            t = t[end + 4:]
    return len([l for l in t.splitlines() if l.strip()])


def approx_tokens(text: str) -> int:
    return max(1, len(strip_html_comments(text)) // 4) if text else 0


def find_imports(path: Path, root: Path, seen: set, depth: int = 0) -> list[dict]:
    """Resolve @imports recursively (max depth 5), as Claude Code does."""
    if depth > 5:
        return []
    out = []
    text = read_text(path)
    for line in strip_code(strip_html_comments(text)):
        line_no_spans = re.sub(r"`[^`]*`", "", line)
        for m in IMPORT_RE.finditer(line_no_spans):
            target = m.group(1).rstrip(".,;:)")
            if target.startswith("http"):
                continue
            tp = Path(os.path.expanduser(target)) if target.startswith("~") else (path.parent / target)
            tp = tp.resolve()
            key = str(tp)
            exists = tp.is_file()
            if key in seen or (not exists and not tp.suffix):
                continue  # already seen, or an @mention / @scope/package rather than a file
            seen.add(key)
            t = read_text(tp) if exists else ""
            out.append({
                "from": rel(path, root), "import": target, "resolved": rel(tp, root),
                "exists": exists, "lines": body_lines(t), "approx_tokens": approx_tokens(t),
                "external": not str(tp).startswith(str(root.resolve())),
            })
            if exists and tp.suffix == ".md":
                out.extend(find_imports(tp, root, seen, depth + 1))
    return out


def git_ignored(root: Path, paths: list[str]) -> dict:
    if not paths:
        return {}
    code, out = git(root, "check-ignore", "--no-index", "-z", *paths)
    if code > 1:
        return {}
    return {p: (p in set(out)) for p in paths}


def norm_line(l: str) -> str:
    return re.sub(r"\s+", " ", l.strip().lstrip("-*0123456789. ").lower())


def scan(root: Path) -> dict:
    root = root.resolve()
    relpaths = {rel(f, root): f for f in walk(root)}
    # The walk is depth-limited and skips most dot-dirs; git's list isn't. Keep the walk too:
    # it finds gitignored secret files that git won't list.
    for rp in git(root, "ls-files", "-z", "-co", "--exclude-standard")[1]:
        if rp not in relpaths and not IGNORE_DIRS.intersection(Path(rp).parts) and (root / rp).is_file():
            relpaths[rp] = root / rp

    # ---- Stack ----
    manifests, dirs_with_manifests = [], set()
    for rp, f in relpaths.items():
        if f.name in MANIFESTS:
            manifests.append({"path": rp, "kind": MANIFESTS[f.name]})
            if f.name in {"package.json", "pyproject.toml", "requirements.txt", "manage.py", "go.mod",
                          "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "composer.json", "pubspec.yaml"}:
                dirs_with_manifests.add(str(Path(rp).parent))
    ci = sorted({rp for rp in relpaths for g in CI_GLOBS if fnmatch.fnmatch(rp, g)})
    app_roots = sorted(dirs_with_manifests)
    is_monorepo = len([d for d in app_roots if d != "."]) >= 2 or any(
        m["kind"] == "node-monorepo" for m in manifests)

    npm_scripts: dict[str, set] = {}
    for rp, f in relpaths.items():
        if f.name == "package.json":
            try:
                npm_scripts[str(Path(rp).parent)] = set((json.loads(read_text(f)).get("scripts") or {}).keys())
            except (json.JSONDecodeError, AttributeError):
                pass
    make_targets: set = set()
    for rp, f in relpaths.items():
        if f.name == "Makefile":
            make_targets |= set(re.findall(r"^([\w.-]+):(?!=)", read_text(f), flags=re.M))

    # ---- Claude instruction files ----
    claude_md = []
    for rp, f in relpaths.items():
        if f.name in {"CLAUDE.md", "CLAUDE.local.md"}:
            parent = str(Path(rp).parent)
            at_root = parent in {".", ".claude"}
            t = read_text(f)
            claude_md.append({"path": rp, "loads": "always" if at_root else "on-demand (when working in that folder)",
                              "lines": body_lines(t), "approx_tokens": approx_tokens(t)})

    rules = []
    for rp, f in relpaths.items():
        if "/.claude/rules/" in "/" + rp and f.suffix == ".md":
            t = read_text(f)
            fm = frontmatter(t)
            paths = fm.get("paths") or []
            if isinstance(paths, str):
                paths = [paths]
            in_nested = not rp.startswith(".claude/")
            matched = None
            if paths:
                matched = any(glob_match(p2, g) for g in paths for p2 in relpaths)
            rules.append({"path": rp, "paths": paths,
                          "loads": "on-demand (matching files)" if paths or in_nested else "always",
                          "paths_match_any_file": matched,
                          "lines": body_lines(t), "approx_tokens": approx_tokens(t)})

    def component(kind: str, pattern: str) -> list[dict]:
        out = []
        for rp, f in relpaths.items():
            if fnmatch.fnmatch(rp, pattern) or fnmatch.fnmatch(rp, "*/" + pattern):
                t = read_text(f)
                fm = frontmatter(t)
                desc = fm.get("description", "")
                if isinstance(desc, list):
                    desc = " ".join(desc)
                out.append({"path": rp, "name": fm.get("name", Path(rp).stem if kind != "skill" else Path(rp).parent.name),
                            "description_chars": len(desc),
                            "model_invocable": str(fm.get("disable-model-invocation", "false")).lower() != "true",
                            "tools": fm.get("tools") or fm.get("allowed-tools"),
                            "lines": body_lines(t)})
        return out

    skills = component("skill", ".claude/skills/*/SKILL.md")
    agents = component("agent", ".claude/agents/*.md")
    commands = component("command", ".claude/commands/*.md")

    # ---- Imports from always-loaded files ----
    imports, seen = [], set()
    for c in claude_md:
        if c["loads"] == "always":
            imports.extend(find_imports(relpaths[c["path"]], root, seen))
    for r in rules:
        if r["loads"] == "always":
            imports.extend(find_imports(relpaths[r["path"]], root, seen))

    # ---- Settings ----
    settings = {}
    for name in [".claude/settings.json", ".claude/settings.local.json"]:
        f = root / name
        if f.is_file():
            try:
                data = json.loads(read_text(f))
            except json.JSONDecodeError:
                settings[name] = {"error": "invalid JSON"}
                continue
            perms = data.get("permissions", {}) or {}
            deny = perms.get("deny", []) or []
            settings[name] = {
                "allow": len(perms.get("allow", []) or []), "ask": len(perms.get("ask", []) or []),
                "deny": len(deny),
                "deny_read": [re.sub(r"^\.?/", "", d[5:-1]) for d in deny
                              if isinstance(d, str) and d.startswith("Read(") and d.endswith(")")],
                "hooks": sorted((data.get("hooks") or {}).keys()),
                "enabled_plugins": sorted((data.get("enabledPlugins") or {}).keys()),
            }
    mcp_json = (root / ".mcp.json").is_file()

    # ---- Other AI tools ----
    other = [p for p in OTHER_TOOL_FILES if (root / p).is_file()]
    for d in OTHER_TOOL_DIRS:
        if (root / d).is_dir():
            other.append(d + "/")
    other_detail = []
    for p in other:
        fp = root / p
        if fp.is_file():
            t = read_text(fp)
            other_detail.append({"path": p, "lines": body_lines(t)})
        else:
            other_detail.append({"path": p, "files": sum(1 for x in fp.rglob("*") if x.is_file())})
    nested_agents_md = [rp for rp in relpaths if rp.endswith("/AGENTS.md")]

    # ---- Secrets (names only, never read) ----
    secret_files = sorted({rp for rp, f in relpaths.items()
                           if f.name not in SECRET_SAFE and f.suffix not in CODE_EXTS
                           and any(fnmatch.fnmatch(f.name, p) for p in SECRET_PATTERNS)})
    ignored = git_ignored(root, secret_files)
    tracked_secrets = sorted(set(git(root, "ls-files", "-z", "--", *secret_files)[1])) if secret_files else []
    env_examples = sorted(rp for rp, f in relpaths.items() if f.name in SECRET_SAFE)

    # ---- Quality checks across instruction files ----
    instr_paths = [c["path"] for c in claude_md] + [r["path"] for r in rules] + \
                  [p["path"] for p in other_detail if "lines" in p]
    line_index: dict[str, list[str]] = {}
    stale_paths, stale_cmds = [], []
    for ip in instr_paths:
        t = read_text(root / ip)
        content_lines = strip_html_comments(t).splitlines()
        for l in strip_code(strip_html_comments(t)):
            n = norm_line(l)
            if len(n) >= 25 and not n.startswith("#"):
                line_index.setdefault(n, []).append(ip)
        for m in BACKTICK_PATH_RE.finditer(t):
            ref = m.group(1).rstrip("/.")
            if any(ch in ref for ch in "*{}<>$") or ref.startswith(("http", "~")):
                continue
            base = (root / ip).parent
            if not ((root / ref).exists() or (base / ref).exists()):
                stale_paths.append({"file": ip, "ref": ref})
        all_scripts = set().union(*npm_scripts.values()) if npm_scripts else set()
        for l in code_segments(strip_html_comments(t)):
            for m in NPM_RE.finditer(l):
                s = m.group(1)
                if s.startswith("-"):
                    continue
                if not npm_scripts:
                    stale_cmds.append({"file": ip, "command": m.group(0), "issue": "no package.json in repo"})
                elif s not in all_scripts and s not in NPM_BUILTINS:
                    stale_cmds.append({"file": ip, "command": m.group(0), "issue": "npm script not found in any package.json"})
            for m in MAKE_RE.finditer(l):
                s = m.group(1)
                if s.startswith("-"):
                    continue
                if not make_targets:
                    stale_cmds.append({"file": ip, "command": m.group(0), "issue": "no Makefile targets in repo"})
                elif s not in make_targets:
                    stale_cmds.append({"file": ip, "command": m.group(0), "issue": "make target not found"})
    duplicates = [{"line": k[:120], "files": sorted(set(v))} for k, v in line_index.items() if len(set(v)) > 1]

    # ---- Always-loaded budget ----
    always_lines = sum(c["lines"] for c in claude_md if c["loads"] == "always") + \
                   sum(r["lines"] for r in rules if r["loads"] == "always") + \
                   sum(i["lines"] for i in imports if i["exists"])
    always_tokens = sum(c["approx_tokens"] for c in claude_md if c["loads"] == "always") + \
                    sum(r["approx_tokens"] for r in rules if r["loads"] == "always") + \
                    sum(i["approx_tokens"] for i in imports if i["exists"])
    desc_chars = sum(x["description_chars"] for x in skills + agents + commands if x["model_invocable"])

    # ---- Findings ----
    findings = []

    def add(sev, msg, where=None):
        findings.append({"severity": sev, "finding": msg, **({"where": where} if where else {})})

    for c in claude_md:
        limit = BUDGET_ROOT_CLAUDE if c["loads"] == "always" else BUDGET_FILE_WARN
        if c["lines"] > BUDGET_FILE_WARN:
            add("high", f"{c['lines']} lines; over Anthropic's 200-line guidance, adherence drops", c["path"])
        elif c["lines"] > limit:
            add("medium", f"{c['lines']} lines; over the {limit}-line target", c["path"])
    for r in rules:
        if r["loads"] == "always":
            add("medium", "rule has no `paths:` so it loads in every session; scope it if it only applies to part of the code", r["path"])
        if r["lines"] > BUDGET_RULE:
            add("low", f"{r['lines']} lines; split by topic (target {BUDGET_RULE})", r["path"])
        if r["paths"] and r["paths_match_any_file"] is False:
            add("high", "`paths:` globs match no files in the repo; this rule never loads", r["path"])
    for i in imports:
        if not i["exists"]:
            add("high", f"@import target missing: {i['import']}", i["from"])
        elif i["lines"] > 30:
            add("medium", f"@{i['import']} imports {i['lines']} lines at launch; reference it by path instead unless every session needs it", i["from"])
    if always_lines > BUDGET_ALWAYS_LOADED:
        add("high", f"always-loaded instructions total ~{always_lines} lines (~{always_tokens} tokens); target ≤{BUDGET_ALWAYS_LOADED}")
    for x in skills + agents + commands:
        if x["model_invocable"] and x["description_chars"] > BUDGET_DESCRIPTION_CHARS:
            add("low", f"description is {x['description_chars']} chars and is loaded every turn; tighten it", x["path"])
        if x["model_invocable"] and x["description_chars"] == 0:
            add("medium", "no description; Claude can't tell when to use it", x["path"])
    hygiene = {k: [p for p in v if (root / p).exists()] for k, v in HYGIENE.items()}

    for d in duplicates[:20]:
        add("medium", f"same instruction appears in {len(d['files'])} files: \"{d['line'][:80]}\"", ", ".join(d["files"]))
    for s in stale_paths[:30]:
        add("medium", f"references `{s['ref']}` which doesn't exist", s["file"])
    for s in stale_cmds[:30]:
        add("high", f"`{s['command']}`: {s['issue']}", s["file"])
    for label, items, cap in (("duplicate", duplicates, 20), ("stale path", stale_paths, 30),
                              ("stale command", stale_cmds, 30)):
        if len(items) > cap:
            add("info", f"...and {len(items) - cap} more {label} findings (full list under `checks` in --format json)")
    if not claude_md and not other:
        add("info", "no AI instruction files found (greenfield)")
    if other and not claude_md:
        add("medium", f"other tools' instruction files exist ({', '.join(other)}); reuse them (e.g. CLAUDE.md importing AGENTS.md) instead of duplicating")
    imported = {Path(i["resolved"]).as_posix() for i in imports}
    unlinked = [p for p in other if p.rstrip("/") not in imported]
    if unlinked and claude_md:
        add("low", f"both Claude and other-tool instruction files exist ({', '.join(unlinked)}); check for drift/duplication")
    if tracked_secrets:
        add("high", f"secret-like files are committed to git (gitignore doesn't untrack them; "
                    f"`git rm --cached` and rotate): {', '.join(tracked_secrets[:10])}")
    unignored = [p for p, ok in ignored.items() if not ok and p not in tracked_secrets]
    if unignored:
        add("high", f"secret-like files not gitignored: {', '.join(unignored[:10])}")
    # Only the shared settings.json counts: settings.local.json is personal and gitignored.
    deny_read = (settings.get(".claude/settings.json") or {}).get("deny_read", [])
    undenied = [p for p in secret_files if not any(glob_match(p, g) for g in deny_read)]
    if undenied:
        add("high", f"secret-like files not denied by Read() rules in .claude/settings.json: {', '.join(undenied[:10])}")
    if ".claude/settings.json" not in settings:
        add("medium", "no .claude/settings.json (no shared permission guardrails or hooks)")
    if not env_examples and secret_files:
        add("medium", "env files exist but no .env.example documents the variable names")
    if not ci:
        add("low", "no CI config found")

    findings.sort(key=lambda f: SEV_ORDER[f["severity"]])

    state = "greenfield" if not claude_md and not other and not rules else \
            "other-tools-only" if not claude_md and not rules else "existing"

    return {
        "repo": root.name,
        "state": state,
        "stack": {"manifests": sorted(manifests, key=lambda m: m["path"]), "app_roots": app_roots,
                  "is_monorepo": is_monorepo, "ci": ci,
                  "npm_scripts": {k: sorted(v) for k, v in npm_scripts.items()},
                  "make_targets": sorted(make_targets)},
        "claude": {"claude_md": claude_md, "rules": rules, "imports": imports, "skills": skills,
                   "agents": agents, "commands": commands, "settings": settings, "mcp_json": mcp_json},
        "other_tools": {"files": other_detail, "nested_agents_md": nested_agents_md},
        "context_budget": {"always_loaded_lines": always_lines, "always_loaded_approx_tokens": always_tokens,
                           "target_lines": BUDGET_ALWAYS_LOADED,
                           "model_invocable_description_chars": desc_chars},
        "secrets": {"secret_like_files": secret_files, "gitignored": ignored, "tracked": tracked_secrets,
                    "env_examples": env_examples},
        "hygiene": hygiene,
        "checks": {"duplicates": duplicates, "stale_paths": stale_paths, "stale_commands": stale_cmds},
        "findings": findings,
    }


def to_md(r: dict) -> str:
    L = [f"# Agent readiness scan: {r['repo']}", "", f"**State:** {r['state']}  ",
         f"**Monorepo:** {r['stack']['is_monorepo']}  ",
         f"**App roots:** {', '.join(r['stack']['app_roots']) or '-'}  ",
         f"**Always-loaded instructions:** ~{r['context_budget']['always_loaded_lines']} lines "
         f"(~{r['context_budget']['always_loaded_approx_tokens']} tokens; target ≤{r['context_budget']['target_lines']} lines)", ""]
    L += ["## Findings", ""]
    if not r["findings"]:
        L.append("None.")
    for f in r["findings"]:
        L.append(f"- **{f['severity']}**: {f['finding']}" + (f" — `{f['where']}`" if f.get("where") else ""))
    L += ["", "## Instruction files", "", "| File | Loads | Lines |", "|---|---|---|"]
    for c in r["claude"]["claude_md"]:
        L.append(f"| `{c['path']}` | {c['loads']} | {c['lines']} |")
    for x in r["claude"]["rules"]:
        L.append(f"| `{x['path']}` | {x['loads']} | {x['lines']} |")
    for i in r["claude"]["imports"]:
        L.append(f"| `@{i['import']}` (from `{i['from']}`) | always (import) | {i['lines']} |")
    for o in r["other_tools"]["files"]:
        L.append(f"| `{o['path']}` | other tool | {o.get('lines', str(o.get('files')) + ' files')} |")
    L += ["", f"Skills: {len(r['claude']['skills'])} · Agents: {len(r['claude']['agents'])} · "
          f"Commands: {len(r['claude']['commands'])} · Settings: {', '.join(r['claude']['settings']) or 'none'}"]
    L += ["", "## Stack", ""] + [f"- `{m['path']}` ({m['kind']})" for m in r["stack"]["manifests"]]
    L += [f"- CI: {', '.join(r['stack']['ci']) or 'none'}"]
    L += ["", "## Hygiene (scorecard S8-S11)", ""] + \
         [f"- {k}: {', '.join(v) or 'none found'}" for k, v in r["hygiene"].items()]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--format", choices=["json", "md"], default="md")
    ap.add_argument("--fail-on", choices=["high", "medium", "low"],
                    help="exit 1 if any finding is at or above this severity (for CI)")
    a = ap.parse_args()
    root = Path(a.root)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    result = scan(root)
    print(json.dumps(result, indent=2) if a.format == "json" else to_md(result))
    if a.fail_on and any(SEV_ORDER[f["severity"]] <= SEV_ORDER[a.fail_on] for f in result["findings"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
