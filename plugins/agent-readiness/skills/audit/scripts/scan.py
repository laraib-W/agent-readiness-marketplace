#!/usr/bin/env python3
"""Deterministic agent-readiness scan.

Collects facts about a repository's AI-agent setup without using an LLM, so the
agent spends tokens on judgment, not on counting. Standard library only.

Never reads secret files: env/key/credential files are reported by NAME only.
To count where Claude sessions start, it lists folder names (not contents) under
~/.claude/projects on this machine.

Usage:
    python3 scan.py [REPO_ROOT] [--format json|md] [--fail-on high|medium|low]
"""
from __future__ import annotations

import argparse
import builtins
import fnmatch
import keyword
import json
import os
import re
import subprocess
import sys
import time
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
BUDGET_UNIT = 15  # an app's own CLAUDE.md/AGENTS.md: only what differs from the area rule

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
    "Gemfile": "ruby", "composer.json": "php", "pubspec.yaml": "flutter", "Package.swift": "swift",
    "mix.exs": "elixir", "deno.json": "deno",
    "Dockerfile": "docker", "docker-compose.yml": "docker", "docker-compose.yaml": "docker",
    "compose.yml": "docker", "compose.yaml": "docker", "Makefile": "make",
    "tsconfig.json": "typescript", "angular.json": "angular", "vite.config.ts": "vite",
    "vite.config.js": "vite", "next.config.js": "nextjs", "next.config.mjs": "nextjs",
    "next.config.ts": "nextjs",
}
CI_GLOBS = [".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml",
            "bitbucket-pipelines.yml", ".circleci/config.yml", "Jenkinsfile", "azure-pipelines.yml"]
SECRET_PATTERNS = [".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
                   "credentials*.json", "service-account*.json", "secrets.*", "*.sqlite3", "*.dump"]
SECRET_SAFE = {".env.example", ".env.sample", ".env.template", ".env.dist"}
# Source files named like secrets (e.g. Django's secrets.py) are code, not secrets.
CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rb", ".rs", ".java",
             ".kt", ".php", ".cs", ".swift", ".scala", ".ex", ".exs"}
# Files that answer scorecard criteria H4-H6 and part of E1 (existence only).
HYGIENE = {
    "pre_commit": [".pre-commit-config.yaml", ".husky", "lefthook.yml", "lefthook.yaml", ".lefthook.yml"],
    "codeowners": ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"],
    "pr_template": [".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md",
                    ".github/PULL_REQUEST_TEMPLATE", "pull_request_template.md"],
    "dependency_updates": [".github/dependabot.yml", ".github/dependabot.yaml", "renovate.json",
                           "renovate.json5", ".github/renovate.json", ".renovaterc", ".renovaterc.json"],
    "secret_scanning": [".gitleaks.toml", ".secrets.baseline", ".trufflehog.yml", ".github/secret_scanning.yml"],
    "dev_environment": [".devcontainer", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"],
    "license": ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"],
    "contributing": ["CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"],
    "security_policy": ["SECURITY.md", ".github/SECURITY.md"],
    "editorconfig": [".editorconfig"],
    "architecture_docs": ["ARCHITECTURE.md", "docs/architecture.md", "docs/architecture", "docs/adr", "docs/adrs", "adr"],
}
SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# Unit detection: a folder whose children share a shape (e.g. every Django app has models.py,
# every feature has components/) is a collection; each child is a unit that may need its own rule.
CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".go", ".rb", ".java", ".kt", ".php", ".rs",
                 ".vue", ".svelte", ".cs", ".swift", ".scala", ".ex", ".exs"}
TOOLING = {"package.json", "pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "go.mod", "Cargo.toml",
           "tsconfig.json", ".eslintrc", ".eslintrc.js", ".eslintrc.json", "jest.config.js", "Makefile",
           "Dockerfile", "requirements.txt", "Gemfile", "composer.json", "pom.xml", "build.gradle"}
TEST_PART = re.compile(r"(^|/)(tests?|__tests__|spec|e2e|fixtures?)(/|$)|(^|/)(test_[^/]*|[^/]*_test\.\w+|[^/]*\.(test|spec)\.\w+|conftest\.py|tests?\.py|factor(y|ies)\.py)$")
NOT_PACKAGES = set(keyword.kwlist) | set(dir(builtins))  # "import from ..." in a docstring isn't an import
# Whole-line import syntax only, so prose in a docstring ("import there is...") doesn't count.
PY_IMPORT = re.compile(r"^(?:from\s+([A-Za-z_]\w*)[\w.]*\s+import\s+[(*\w]"
                       r"|import\s+([A-Za-z_]\w*)[\w.]*(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*\s*(?:#.*)?$)", re.M)
PY_LOCAL = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+[(*\w]|import\s+([\w.]+))", re.M)
JS_IMPORT_ANY = re.compile(r"""(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*)['"]([^'"]+)['"]""")
JS_IMPORT = re.compile(r"""(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*)['"]((?:@[\w.-]+/)?[\w.-]+)""")
UNIT_WINDOW_DAYS = 180
UNIT_MIN_CODE = 5        # ignore collections of tiny units (icon folders, chart variants)
UNIT_FIX_RATIO = 0.35    # share of recent commits whose subject says "fix"...
UNIT_FIX_LIFT = 1.5      # ...and this many times the collection's own fix rate (teams word commits differently)
UNIT_SIZE_RATIO = 3      # x the median sibling size
UNIT_SESSIONS = 3        # sessions started inside the unit -> nested CLAUDE.md is worth it
UNIT_HUB_SHARE = 0.5     # imported by this share of siblings -> a hub; changes ripple
UNIT_TOP = 10
# Repo-level structure: when a generated map, or a code-graph tool, pays for itself.
STRUCTURE_MAP_UNITS = 15
STRUCTURE_MAP_EDGES = 30
GRAPH_CODE_FILES = 5000
GRAPH_HUBS = 3
GRAPH_EDGES = 150
MAP_MARKER = "Generated by agent-readiness scan.py --map"
GRAPH_TOOL_DIRS = [".code-review-graph", "graphify-out", ".serena", ".sourcegraph", ".codegraph"]

FENCE_RE = re.compile(r"^\s*(```|~~~)")
IMPORT_RE = re.compile(r"(?<![`\w])@([~./\w-][\w./~-]*)")
BACKTICK_PATH_RE = re.compile(r"`([A-Za-z0-9_.~-][\w./-]*/[\w./-]*|[\w-]+\.(?:md|py|ts|tsx|js|json|toml|yml|yaml|cfg|ini|txt))`")
NPM_RE = re.compile(r"\b(?:npm run|yarn(?: run)?|pnpm(?: run)?|bun run)\s+([\w:.-]+)")
NPM_BUILTINS = {"install", "add", "remove", "test", "start", "build", "dev", "i", "ci", "exec", "dlx", "x",
                "workspace", "workspaces", "why", "upgrade", "up", "update", "init", "create", "info",
                "link", "global", "config", "cache", "audit", "outdated", "list", "ls"}
# Only `make` that starts a command, so prose in a fence ("make a copy") is ignored.
MAKE_RE = re.compile(r"(?:^|&&|\|\||[;|$])\s*make\s+([\w.-]+)")


# ---- Environment facts per part (scorecard E): can an agent set up, run and check its work? ----
ENV_FILES = {
    "lockfile": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb", "poetry.lock",
                 "Pipfile.lock", "uv.lock", "pdm.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock",
                 "pubspec.lock", "mix.lock", "packages.lock.json"],
    "runtime_pinned": [".nvmrc", ".node-version", ".python-version", ".tool-versions", "runtime.txt", ".ruby-version",
                       "rust-toolchain", "rust-toolchain.toml", ".java-version", "global.json", ".sdkmanrc", ".mise.toml"],
    "setup": [".devcontainer", "bin/setup", "script/setup", "script/bootstrap", "scripts/setup.sh", "scripts/bootstrap.sh",
              "setup.sh", "bootstrap.sh", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"],
    "linter": [".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml", "eslint.config.js",
               "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts", "biome.json", "biome.jsonc", "pylintrc",
               ".pylintrc", ".flake8", "ruff.toml", ".ruff.toml", ".golangci.yml", ".golangci.yaml", ".rubocop.yml",
               "phpcs.xml", ".swiftlint.yml", "clippy.toml", "detekt.yml", ".oxlintrc.json"],
    "formatter": [".prettierrc", ".prettierrc.js", ".prettierrc.json", ".prettierrc.yml", ".prettierrc.yaml",
                  "prettier.config.js", "prettier.config.mjs", ".isort.cfg", ".style.yapf", "rustfmt.toml",
                  ".rustfmt.toml", ".swiftformat", ".clang-format", "biome.json", "biome.jsonc", ".php-cs-fixer.php"],
    "type_checker": ["tsconfig.json", "mypy.ini", ".mypy.ini", "pyrightconfig.json", ".flowconfig"],
    "coverage": [".coveragerc", ".nycrc", ".nycrc.json", "codecov.yml", ".codecov.yml", "jest.config.js",
                 "vitest.config.ts", "vitest.config.js"],
}
# Config keys inside shared files that count too (pyproject sections, package.json fields, setup.cfg sections).
ENV_KEYS = {
    "pyproject.toml": {"linter": r"^\[tool\.(ruff|pylint|flake8)", "formatter": r"^\[tool\.(black|isort|ruff\.format)",
                       "type_checker": r"^\[tool\.(mypy|pyright)", "coverage": r"^\[tool\.coverage",
                       "runtime_pinned": r"^requires-python\s*="},
    "setup.cfg": {"linter": r"^\[(flake8|pylint)", "type_checker": r"^\[mypy", "coverage": r"^\[coverage:"},
    "tox.ini": {"linter": r"^\[flake8", "coverage": r"--cov"},
    "package.json": {"linter": r'"eslintConfig"\s*:', "formatter": r'"prettier"\s*:', "coverage": r'"(collectCoverage|coverageThreshold)"',
                     "runtime_pinned": r'"engines"\s*:'},
    "go.mod": {"runtime_pinned": r"^go\s+\d"},
}
SETUP_TARGET = re.compile(r"^(setup|bootstrap|install|init|requirements|deps)\s*:", re.M)
# What CI steps do. Covers runners (tox, nox, make, manage.py) that tool-name lists miss.
CI_STEPS = {
    "tests": r"\b(pytest|tox|nox|manage\.py\s+test|python -m (pytest|unittest)|jest|vitest|mocha|karma|go\s+test|cargo\s+test|"
             r"(npm|yarn|pnpm|bun)\s+(run\s+)?test|make\s+test|rspec|rake\s+test|phpunit|mvnw?\s+(test|verify)|"
             r"gradlew?\s+(test|check)|dotnet\s+test|swift\s+test|mix\s+test|cypress\s+run|playwright\s+test)\b",
    "lint": r"\b(eslint|biome|oxlint|pylint|flake8|ruff|black\s+--check|isort\s+--check|prettier\s+--check|mypy|pyright|"
            r"tsc\s+--noEmit|golangci-lint|rubocop|phpcs|swiftlint|(npm|yarn|pnpm)\s+(run\s+)?(lint|typecheck))\b",
    "build": r"\b((npm|yarn|pnpm|bun)\s+(run\s+)?build|docker\s+(build|buildx)|docker/build-push-action|make\s+build|"
             r"go\s+build|cargo\s+build|mvnw?\s+package|gradlew?\s+build|dotnet\s+build)\b",
    "deploy": r"\b(deploy|codedeploy|kubectl\s+apply|helm\s+(upgrade|install)|terraform\s+apply|serverless\s+deploy|"
              r"fly\s+deploy|vercel|netlify|gcloud\s+run|aws\s+ecs)\b",
    "security": r"\b(codeql|snyk|trivy|semgrep|gitleaks|trufflehog|bandit|npm\s+audit|pip-audit|safety\s+check|osv-scanner|"
                r"dependency-review)\b",
}


def ci_steps(root: Path, ci_files: list[str], parts: list[str]) -> list[dict]:
    """Per CI file: which kinds of steps it runs, and which app parts they run in."""
    out = []
    named = [p for p in parts if p != "."]
    for f in ci_files:
        text = read_text(root / f)
        kinds = sorted(k for k, rx in CI_STEPS.items() if re.search(rx, text, re.I | re.M))
        # A part is targeted if the workflow runs in it (working-directory, cd, --prefix, -C) or only on its paths.
        hit = [p for p in named if re.search(rf"(working-directory:\s*['\"]?\.?/?{re.escape(p)}\b|\bcd\s+\.?/?{re.escape(p)}\b|"
                                             rf"--prefix[= ]\.?/?{re.escape(p)}\b|-C\s+\.?/?{re.escape(p)}\b)", text)]
        ignored = [p for p in named if re.search(rf"paths-ignore:[^\n]*\n(\s+-[^\n]*\n)*?\s+-\s*['\"]?{re.escape(p)}/", text)]
        targets = hit or (["."] if ignored or not named else ["."])
        out.append({"file": f, "steps": kinds, "parts": targets})
    return out


def env_facts(root: Path, relpaths: dict, parts: list[str], ci: list[dict], make_targets: set) -> list[dict]:
    out = []
    for part in parts or ["."]:
        base = root if part == "." else root / part
        facts = {}
        for kind, names in ENV_FILES.items():
            facts[kind] = [n for n in names if (base / n).exists()]
        for fname, keys in ENV_KEYS.items():
            text = read_text(base / fname)
            for kind, rx in keys.items():
                if text and re.search(rx, text, re.M) and fname not in facts[kind]:
                    facts[kind].append(f"{fname} ({kind.replace('_', ' ')})")
        # Pinned requirements count as a lockfile for pip (e.g. pip-compile output).
        reqs = [f for f in ["requirements.txt"] + sorted(str(p.relative_to(base)) for p in base.glob("requirements/*.txt"))
                if (base / f).is_file()]
        if not facts["lockfile"] and any(re.search(r"^[\w.-]+==", read_text(base / f), re.M) for f in reqs):
            facts["lockfile"].append(next(f for f in reqs if re.search(r"^[\w.-]+==", read_text(base / f), re.M)) + " (pinned)")
        if part == "." and SETUP_TARGET.search("\n".join(f"{t}:" for t in make_targets)):
            facts["setup"].append("Makefile target")
        mine = [c for c in ci if part in c["parts"]]
        facts["ci_tests"] = [c["file"] for c in mine if "tests" in c["steps"]]
        facts["ci_lint"] = [c["file"] for c in mine if "lint" in c["steps"]]
        has_tests = any(scan_part(rp, part) and TEST_PART.search(rp) for rp in relpaths)
        out.append({"part": part, "has_tests": has_tests, **facts})
    return out


def scan_part(rp: str, part: str) -> bool:
    return part == "." or rp.startswith(part + "/")


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


def git(root: Path, *args: str, stdin: str | None = None) -> tuple[int, list[str]]:
    """Run git in root; NUL-separated output. (128, []) if not a repo or git is missing."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True, text=True, timeout=30)
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
    """Resolve @imports recursively (max four hops), as Claude Code does."""
    if depth > 3:
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
    code, out = git(root, "check-ignore", "--no-index", "--stdin", "-z", stdin="\0".join(paths) + "\0")
    if code > 1:
        return {}
    return {p: (p in set(out)) for p in paths}


def session_counts(root: Path) -> dict[str, int]:
    """Claude Code sessions started in root or below, keyed by relative folder ("." = root)."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
    enc = lambda p: re.sub(r"[^A-Za-z0-9]", "-", str(p))
    prefix = enc(root)
    out: dict[str, int] = {}
    try:
        dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith(prefix)]
    except OSError:
        return out
    for d in dirs:
        rest = d.name[len(prefix):]
        if rest and not rest.startswith("-"):
            continue
        n = sum(1 for _ in d.glob("*.jsonl"))
        # Encoding is lossy ("/" and "-" both become "-"), so match against real folders.
        out[rest.lstrip("-") or "."] = n
    return out


def match_session_dir(encoded: str, folder: str) -> bool:
    return encoded == re.sub(r"[^A-Za-z0-9]", "-", folder)


def find_units(root: Path, files: list[str], rules: list[dict], nested_dirs: set[str],
               sessions: dict[str, int], owned: set[str], layout: str = "auto",
               shared_tools: bool = False) -> tuple[list[dict], dict[tuple[str, str], int]]:
    """Group units into collections, map who imports whom, and measure the signals that say a
    unit needs its own instructions. Returns (units, edges) where edges[(a, b)] counts a's files importing b."""
    files = [f for f in files if not IGNORE_DIRS.intersection(Path(f).parts)]
    kids: dict[str, dict[str, set]] = {}
    for f in files:
        parts = Path(f).parts
        for i in range(1, min(len(parts) - 1, MAX_DEPTH)):
            if parts[i].startswith((".", "__")):
                break
            kids.setdefault("/".join(parts[:i]), {}).setdefault(parts[i], set()).add(parts[i + 1])
    code = {}
    for f in files:
        if Path(f).suffix in CODE_SUFFIXES:
            code.setdefault(str(Path(f).parent), []).append(f)

    def code_in(u: str) -> list[str]:
        return [f for d, fs in code.items() if d == u or d.startswith(u + "/") for f in fs]

    found = {}
    for parent, ch in kids.items():
        if len(ch) < 3:
            continue
        freq: dict[str, int] = {}
        for names in ch.values():
            for n in names:
                freq[n] = freq.get(n, 0) + 1
        template = {n for n, c in freq.items() if c >= max(3, len(ch) / 2)}
        if len(template) < 2:
            continue
        sizes = sorted(len(code_in(f"{parent}/{k}")) for k in ch)
        if sizes[len(sizes) // 2] < UNIT_MIN_CODE:
            continue
        found[parent] = (ch, template, sizes[len(sizes) // 2])
    # Keep the outermost collections (a feature's components/ folder isn't another collection).
    found = {p: v for p, v in found.items() if not any(p != q and p.startswith(q + "/") for q in found)}
    if not found:
        return [], {}

    commits: dict[str, int] = {}
    fixes: dict[str, int] = {}
    log = "\0".join(git(root, "log", f"--since={UNIT_WINDOW_DAYS}.days", "-z", "--name-only", "--format=%x01%s")[1])
    for rec in log.split("\x01")[1:]:
        subject, _, rest = rec.partition("\n")
        touched = set()
        for n in (x.strip("\n") for x in rest.split("\0")):
            for parent in found:
                if n.startswith(parent + "/"):
                    touched.add(parent + "/" + n[len(parent) + 1:].split("/")[0])
        for u in touched:
            commits[u] = commits.get(u, 0) + 1
            if re.search(r"\bfix", subject, re.I):
                fixes[u] = fixes.get(u, 0) + 1

    unit_set = {f"{p}/{k}" for p, (ch, _, _) in found.items() for k in ch}
    # Absolute imports may be written from any source root (`hirestream.apps.jobs`, `apps.jobs`).
    bases = {""} | {Path(p).parents[i].as_posix() for p in found for i in range(len(Path(p).parents))} | set(found)
    bases.discard(".")

    def unit_of(target: str) -> str | None:
        for base in bases:
            parts = Path(f"{base}/{target}" if base else target).parts
            for i in range(len(parts), 0, -1):
                if "/".join(parts[:i]) in unit_set:
                    return "/".join(parts[:i])
        return None

    # ---- Pass 1: libraries and imports of other units, per unit (test files excluded) ----
    local = {Path(f).parts[0] for f in files} | {Path(f).stem for f in files if f.endswith(".py")}
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    deps: dict[str, set] = {}
    edges: dict[tuple[str, str], int] = {}
    for u in unit_set:
        d = set()
        for f in code_in(u):
            if TEST_PART.search(f):
                continue  # test-only libraries and fixtures don't make a unit different
            text = read_text(root / f)
            targets = []
            if f.endswith(".py"):
                d |= {a or b for a, b in PY_IMPORT.findall(text)}
                for a, b in PY_LOCAL.findall(text):
                    dotted = a or b
                    if dotted.startswith("."):
                        dots = len(dotted) - len(dotted.lstrip("."))
                        base = Path(f).parent
                        for _ in range(dots - 1):
                            base = base.parent
                        targets.append(f"{base.as_posix()}/{dotted.lstrip('.').replace('.', '/')}".rstrip("/"))
                    else:
                        targets.append(dotted.replace(".", "/"))
            else:
                for spec in JS_IMPORT_ANY.findall(text):
                    if spec.startswith("."):
                        targets.append(os.path.normpath(os.path.join(str(Path(f).parent), spec)))
                    else:
                        d.add("/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0])
                        targets.append(spec)  # may be a src-root alias like 'apps/jobs'
            seen_here = set()
            for t in targets:
                dst = unit_of(t)
                if dst and dst != u and dst not in seen_here:
                    seen_here.add(dst)
                    edges[(u, dst)] = edges.get((u, dst), 0) + 1
        deps[u] = {n for n in d if n and not n.startswith(".") and n not in stdlib and n not in local
                   and n not in NOT_PACKAGES}
    fan_in: dict[str, set] = {}
    for (a, b) in edges:
        fan_in.setdefault(b, set()).add(a)

    # ---- Pass 2: signals and the suggested home ----
    nested_kind = "nested AGENTS.md + CLAUDE.md importing it" if shared_tools else "nested CLAUDE.md"
    units = []
    for parent, (ch, template, median) in found.items():
        usage: dict[str, int] = {}
        for k in ch:
            for x in deps[f"{parent}/{k}"]:
                usage[x] = usage.get(x, 0) + 1
        cap = max(1, len(ch) // 10)
        all_c = sum(commits.get(f"{parent}/{k}", 0) for k in ch)
        base_fix = sum(fixes.get(f"{parent}/{k}", 0) for k in ch) / all_c if all_c else 0
        fix_bar = max(UNIT_FIX_RATIO, UNIT_FIX_LIFT * base_fix)
        hub_bar = max(3, round(UNIT_HUB_SHARE * (len(ch) - 1)))
        # Follow the repo's existing convention: if sibling units already carry their own file, nest.
        convention = any(str(Path(d).parent) == parent for d in nested_dirs)
        for k, names in ch.items():
            u = f"{parent}/{k}"
            cfiles = code_in(u)
            own_deps = sorted(x for x in deps[u] if usage[x] <= cap)
            own_dirs = sorted(n for n in names - template
                              if (root / u / n).is_dir() and not TEST_PART.search(n) and not n.startswith((".", "__")))
            tooling = sorted(n for n in names if n in TOOLING)
            c, fx = commits.get(u, 0), fixes.get(u, 0)
            sess = sum(n for enc, n in sessions.items() if enc != "." and
                       (match_session_dir(enc, u) or enc.startswith(re.sub(r"[^A-Za-z0-9]", "-", u) + "-")))
            users = sorted(x for x in fan_in.get(u, set()) if x.startswith(parent + "/"))
            uses = sorted({b for (a, b) in edges if a == u})
            specific = [r["path"] for r in rules if r["paths"] and any(u in g for g in r["paths"])]
            nested = any(d == u or d.startswith(u + "/") for d in nested_dirs)
            reasons = []
            if tooling:
                reasons.append(f"own tooling ({', '.join(tooling)})")
            if sess >= UNIT_SESSIONS:
                reasons.append(f"{sess} sessions started inside it")
            if u in owned:
                reasons.append("its own CODEOWNERS entry")
            if len(own_deps) >= 2:
                reasons.append(f"dependencies no sibling uses ({', '.join(own_deps[:5])})")
            # Several libraries nobody else uses, in code that's being worked on, is a different stack.
            distinct_stack = len(own_deps) >= 3 and c >= 10
            if len(users) >= hub_bar:
                reasons.append(f"imported by {len(users)} of {len(ch) - 1} siblings (changes ripple)")
            if c >= 10 and fx / c >= fix_bar:
                reasons.append(f"{fx} of {c} commits in {UNIT_WINDOW_DAYS} days are fixes "
                               f"(siblings: {base_fix:.0%})")
            if median and len(cfiles) >= UNIT_SIZE_RATIO * median and len(cfiles) >= 30:
                reasons.append(f"{len(cfiles)} code files, {len(cfiles) // median}x the median sibling")
            if c == 0:
                reasons = []  # untouched for the whole window: not worth instructions now
            home_signal = bool(tooling or sess >= UNIT_SESSIONS or u in owned or convention)
            # With other AI tools in the repo, a nested AGENTS.md (+ a CLAUDE.md importing it) is the one
            # file every tool reads; a path-scoped rule would need a shared copy, a link and a pointer.
            if layout == "nested" or (layout == "auto" and (home_signal or shared_tools)):
                home = nested_kind
            else:
                home = "path-scoped rule"
            # One signal is a hotspot to watch (/learn); two independent ones, or a reason to open
            # sessions there, justify instructions of its own.
            strong = home_signal and (tooling or sess >= UNIT_SESSIONS or u in owned)
            strength = ("strong" if reasons and (strong or distinct_stack or len(reasons) >= 2)
                        else "weak" if reasons else None)
            units.append({"collection": parent, "unit": u, "code_files": len(cfiles), "commits": c, "fix_commits": fx,
                          "own_deps": own_deps, "own_dirs": own_dirs, "tooling": tooling, "sessions": sess,
                          "used_by": users, "uses": uses,
                          "covered_by": specific + ([u + "/ (nested file)"] if nested else []),
                          "reasons": reasons, "strength": strength, "suggested_home": home if reasons else None,
                          # rank: more independent signals first, then activity
                          "score": len(reasons) * 1000 + c})
    units.sort(key=lambda x: -x["score"])
    return units, edges


def structure_summary(root: Path, files: list[str], units: list[dict], edges: dict, mcp_names: list[str],
                      all_paths: list[str]) -> dict:
    """Repo-level shape, and whether a generated map or a code-graph tool would pay for itself."""
    code_files = sum(1 for f in files if Path(f).suffix in CODE_SUFFIXES and not IGNORE_DIRS.intersection(Path(f).parts))
    pairs = {(a, b) for (a, b) in edges}
    cycles = sorted({tuple(sorted((a, b))) for (a, b) in pairs if (b, a) in pairs})
    hubs = sorted((u for u in units if any(r.startswith("imported by") for r in u["reasons"])),
                  key=lambda u: -len(u["used_by"]))
    tools = [t for t in GRAPH_TOOL_DIRS if (root / t).exists()]
    wired = [m for m in mcp_names if any(k in m.lower() for k in ("graph", "index", "serena", "sourcegraph", "code"))]
    maps = [p for p in all_paths if p.endswith(".md") and MAP_MARKER in read_text(root / p)]
    recs = []
    if not maps and (len(units) >= STRUCTURE_MAP_UNITS or len(pairs) >= STRUCTURE_MAP_EDGES):
        recs.append({"kind": "a structure map",
                     "why": f"{len(units)} units and {len(pairs)} cross-unit import links; a generated map "
                            "(`scan.py --map`) referenced by path lets an agent see who depends on whom without searching",
                     "cost": "none until read (referenced by path, not @-imported)"})
    if code_files >= GRAPH_CODE_FILES or (len(hubs) >= GRAPH_HUBS and len(pairs) >= GRAPH_EDGES):
        recs.append({"kind": "a code graph",
                     "why": f"{code_files} code files, {len(hubs)} hub units, {len(pairs)} cross-unit links: impact "
                            "questions (\"what breaks if I change X\") are slow to answer by grep",
                     "cost": "tool definitions in every session; measure with /context before and after"})
    if tools and not wired:
        recs.append({"kind": "wiring the existing code graph to Claude",
                     "why": f"{', '.join(tools)} exists but no server in .mcp.json uses it (other editors may)",
                     "cost": "add it to .mcp.json only if the code-graph recommendation applies"})
    return {"code_files": code_files, "units": len(units), "cross_unit_links": len(pairs),
            "hubs": [{"unit": u["unit"], "used_by": len(u["used_by"])} for u in hubs],
            "cycles": [list(c) for c in cycles], "graph_tools": tools, "graph_mcp": wired, "maps": maps,
            "recommendations": recs}


def structure_map(r: dict) -> str:
    """Deterministic repo map for docs/: regenerate it, don't hand-edit it."""
    L = [f"# Structure map: {r['repo']}", "",
         "<!-- Generated by agent-readiness scan.py --map. Regenerate instead of editing. -->", ""]
    by_coll: dict[str, list] = {}
    for u in r["units"]:
        by_coll.setdefault(u["collection"], []).append(u)
    short = lambda p: p.rsplit("/", 1)[-1]
    for coll, us in sorted(by_coll.items()):
        L += [f"## `{coll}/` ({len(us)} units)", "", "| Unit | Code files | Uses | Used by | Own libraries |", "|---|---|---|---|---|"]
        for u in sorted(us, key=lambda x: -len(x["used_by"])):
            L.append(f"| `{short(u['unit'])}` | {u['code_files']} | {', '.join(short(x) for x in u['uses']) or '-'} | "
                     f"{len(u['used_by'])} | {', '.join(u['own_deps'][:6]) or '-'} |")
        L.append("")
    s = r["structure"]
    if s["cycles"]:
        L += ["## Import cycles between units", ""] + [f"- `{a}` ↔ `{b}`" for a, b in s["cycles"]] + [""]
    return "\n".join(L)


def norm_line(l: str) -> str:
    return re.sub(r"\s+", " ", l.strip().lstrip("-*0123456789. ").lower())


def codeowned_dirs(root: Path) -> set[str]:
    """Folders with their own CODEOWNERS line (a team boundary)."""
    out = set()
    for name in HYGIENE["codeowners"]:
        for line in read_text(root / name).splitlines():
            pat = line.split("#", 1)[0].split()
            if len(pat) >= 2:
                out.add(pat[0].strip("/").removesuffix("/**").removesuffix("/*").strip("/"))
    return out


def mcp_server_names(root: Path) -> list[str]:
    """MCP servers configured for Claude (.mcp.json) — not other editors' configs."""
    try:
        return sorted((json.loads(read_text(root / ".mcp.json")).get("mcpServers") or {}).keys())
    except (json.JSONDecodeError, AttributeError):
        return []


def scan(root: Path, layout: str = "auto") -> dict:
    root = root.resolve()
    relpaths = {rel(f, root): f for f in walk(root)}
    git_code, tracked = git(root, "ls-files", "-z")
    tracked = set(tracked)
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
    workspaces = '"workspaces"' in read_text(root / "package.json")
    is_monorepo = len([d for d in app_roots if d != "."]) >= 2 or workspaces or any(
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
            if isinstance(paths, str):  # Claude Code also accepts a comma-separated string
                paths = [g.strip() for g in re.split(r",(?![^{]*\})", paths) if g.strip()]
            in_nested = not rp.startswith(".claude/")
            matched = None
            if paths:
                matched = any(glob_match(p2, g) for g in paths for p2 in relpaths)
            rules.append({"path": rp, "paths": paths,
                          "external_symlink": f.is_symlink() and not str(f.resolve()).startswith(str(root) + os.sep),
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
                            "lines": body_lines(t),
                            # A body that only says "read X.md" is a rule in disguise.
                            "pointer_only": kind == "skill" and body_lines(t) <= 3
                            and bool(re.search(r"[`@][\w./-]+\.md", t.split("---", 2)[-1]))})
        return out

    skills = component("skill", ".claude/skills/*/SKILL.md")
    agents = component("agent", ".claude/agents/*.md")
    commands = component("command", ".claude/commands/*.md")

    # ---- AGENTS.md: read natively only when no CLAUDE.md exists at the root ----
    has_root_claude = any(c["loads"] == "always" for c in claude_md)
    agents_md = []
    for rp, f in relpaths.items():
        if f.name == "AGENTS.md":
            t = read_text(f)
            at_root = str(Path(rp).parent) in {".", ".claude"}
            agents_md.append({"path": rp, "at_root": at_root, "lines": body_lines(t), "approx_tokens": approx_tokens(t),
                              "loads": ("always (read natively, no CLAUDE.md)" if not has_root_claude else "only if imported")
                              if at_root else "on-demand (folders without their own CLAUDE.md)"})

    # ---- Imports from always-loaded files ----
    imports, seen = [], set()
    launch_files = [c["path"] for c in claude_md if c["loads"] == "always"] + \
                   [r["path"] for r in rules if r["loads"] == "always"] + \
                   [a["path"] for a in agents_md if a["loads"].startswith("always")]
    for lp in launch_files:
        imports.extend(find_imports(relpaths[lp], root, seen))
    imported = {Path(i["resolved"]).as_posix() for i in imports}
    # Nested AGENTS.md: Claude only reads it if no CLAUDE.md exists above (default setting), or if the
    # CLAUDE.md beside it imports it.
    for a in agents_md:
        if a["at_root"]:
            continue
        beside = Path(a["path"]).parent / "CLAUDE.md"
        if beside.as_posix() in relpaths:
            a["loads"] = ("on-demand (imported by the CLAUDE.md beside it)" if re.search(r"(?<![`\w])@AGENTS\.md\b", read_text(root / beside))
                          else "never (the CLAUDE.md beside it doesn't import it)")
        elif has_root_claude:
            a["loads"] = "never (a CLAUDE.md exists above; add one beside it containing @AGENTS.md)"
    for a in agents_md:
        if a["at_root"] and has_root_claude:
            a["loads"] = "always (imported by CLAUDE.md)" if a["path"] in imported else "never (CLAUDE.md exists and doesn't import it)"

    sessions = session_counts(root)
    nested_dirs = {str(Path(x["path"]).parent) for x in claude_md + agents_md if str(Path(x["path"]).parent) not in {".", ".claude"}}
    unit_files = sorted(tracked) if git_code == 0 else sorted(relpaths)
    shared_tools = any((root / p).exists() for p in OTHER_TOOL_FILES + OTHER_TOOL_DIRS)
    units, edges = find_units(root, unit_files, rules, nested_dirs, sessions, codeowned_dirs(root),
                              layout, shared_tools)
    structure = structure_summary(root, unit_files, units, edges, mcp_server_names(root), sorted(relpaths))

    # ---- Monorepo parts: is each one covered by area instructions? ----
    parts = []
    for ar in app_roots:
        if ar == ".":
            continue
        inside = [p for p in relpaths if p.startswith(ar + "/")]
        nested = any(str(Path(x["path"]).parent).startswith(ar) for x in claude_md + agents_md)
        scoped = [r["path"] for r in rules if r["paths"] and any(glob_match(p, g) for g in r["paths"] for p in inside)]
        last = git(root, "log", "-1", "--format=%ct", "--", ar)[1]
        last_ts = int(last[0].strip()) if last and last[0].strip().isdigit() else None
        parts.append({"root": ar, "files": len(inside), "nested_instructions": nested, "rules": scoped,
                      "sessions": sum(n for enc, n in sessions.items() if match_session_dir(enc, ar)),
                      "last_change": time.strftime("%Y-%m-%d", time.localtime(last_ts)) if last_ts else None,
                      "dormant": bool(last_ts and time.time() - last_ts > 365 * 86400)})

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
    nested_agents_md = [a["path"] for a in agents_md if not a["at_root"]]

    # ---- Secrets (names only, never read) ----
    secret_files = sorted({rp for rp, f in relpaths.items()
                           if f.name not in SECRET_SAFE and f.suffix not in CODE_EXTS
                           and any(fnmatch.fnmatch(f.name, p) for p in SECRET_PATTERNS)})
    ignored = git_ignored(root, secret_files)
    tracked_secrets = sorted(set(git(root, "ls-files", "-z", "--", *secret_files)[1])) if secret_files else []
    env_examples = sorted(rp for rp, f in relpaths.items() if f.name in SECRET_SAFE)

    # ---- Quality checks across instruction files ----
    other_dir_files = sorted(rp for rp in relpaths if any(rp.startswith(d + "/") for d in OTHER_TOOL_DIRS))
    instr_paths = list(dict.fromkeys(
        [c["path"] for c in claude_md] + [r["path"] for r in rules] + [a["path"] for a in agents_md] +
        [p["path"] for p in other_detail if "lines" in p] + other_dir_files +
        [x["path"] for x in skills + agents + commands]))
    # READMEs are checked for stale commands only: the audit trusts README commands as a source.
    readmes = [rp for rp in relpaths if Path(rp).name.lower() == "readme.md"
               and str(Path(rp).parent) in set(app_roots) | {"."}]
    # Instructions often name files relative to some folder (`users/api/v1/views.py`) or bare
    # (`utils.py`): a ref counts as present if any repo path ends with it.
    known = set(relpaths) | {a.as_posix() for p in relpaths for a in Path(p).parents if a.as_posix() != "."}

    def ref_exists(ref: str, ip: str) -> bool:
        return (root / ref).exists() or ((root / ip).parent / ref).exists() or \
            any(k == ref or k.endswith("/" + ref) for k in known)

    negation = re.compile(r"\b(no|not|never|don't|doesn't|without|instead of)\b", re.I)
    line_index: dict[str, list[str]] = {}
    stale_paths, stale_cmds = [], []
    for ip in instr_paths:
        t = read_text(root / ip)
        for l in strip_code(strip_html_comments(t)):
            n = norm_line(l)
            if len(n) >= 25 and not n.startswith("#"):
                line_index.setdefault(n, []).append(ip)
            if ip in launch_files:
                continue  # their @imports are resolved (and reported) above
            for m in IMPORT_RE.finditer(re.sub(r"`[^`]*`", "", l)):
                ref = m.group(1).rstrip(".,;:)")
                if Path(ref).suffix and not ref_exists(ref, ip):
                    stale_paths.append({"file": ip, "ref": "@" + ref})
            if negation.search(l):
                continue  # "there is no `services/` package" names a path on purpose
            for m in BACKTICK_PATH_RE.finditer(l):
                ref = m.group(1).rstrip("/.")
                if any(ch in ref for ch in "*{}<>$") or ref.startswith(("http", "~")):
                    continue
                if not ref_exists(ref, ip):
                    stale_paths.append({"file": ip, "ref": ref})
    all_scripts = set().union(*npm_scripts.values()) if npm_scripts else set()
    for ip in instr_paths + readmes:
        t = read_text(root / ip)
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
    native_agents = [a for a in agents_md if a["loads"].startswith("always (read")]
    always_lines = sum(a["lines"] for a in native_agents) + \
                   sum(c["lines"] for c in claude_md if c["loads"] == "always") + \
                   sum(r["lines"] for r in rules if r["loads"] == "always") + \
                   sum(i["lines"] for i in imports if i["exists"])
    always_tokens = sum(a["approx_tokens"] for a in native_agents) + \
                    sum(c["approx_tokens"] for c in claude_md if c["loads"] == "always") + \
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
    active_parts = [p for p in app_roots if not any(x["root"] == p and x["dormant"] for x in parts)]
    ci_detail = ci_steps(root, ci, active_parts)
    environment = env_facts(root, relpaths, active_parts, ci_detail, make_targets)
    for e in environment:
        label = "repo root" if e["part"] == "." else f"`{e['part']}/`"
        if e["has_tests"] and ci and not e["ci_tests"]:
            add("medium", f"{label} has tests but no CI step runs them (agents' changes there aren't checked by CI)")
        if not e["runtime_pinned"] and e["part"] != ".":
            add("low", f"{label}: no runtime version pinned (.nvmrc, .python-version, engines, ...): agents and CI may use different versions")

    for d in duplicates[:20]:
        add("medium", f"same instruction appears in {len(d['files'])} files: \"{d['line'][:80]}\"", ", ".join(d["files"]))
    for s in stale_paths[:30]:
        add("medium" if "/" in s["ref"] else "low", f"references `{s['ref']}`, which no repo path matches", s["file"])
    for s in stale_cmds[:30]:
        add("medium" if s["file"] in readmes else "high", f"`{s['command']}`: {s['issue']}", s["file"])
    for label, items, cap in (("duplicate", duplicates, 20), ("stale path", stale_paths, 30),
                              ("stale command", stale_cmds, 30)):
        if len(items) > cap:
            add("info", f"...and {len(items) - cap} more {label} findings (full list under `checks` in --format json)")
    for pt in parts:
        if pt["dormant"]:
            add("info", f"`{pt['root']}/` hasn't changed since {pt['last_change']}; low priority for instructions")
        elif not pt["nested_instructions"] and not pt["rules"]:
            add("medium", f"`{pt['root']}/` ({pt['files']} files) has no area instructions: no path-scoped rule "
                          "or nested CLAUDE.md/AGENTS.md matches its files")
    for rec in structure["recommendations"]:
        add("medium" if rec["kind"] == "a structure map" else "low", f"consider {rec['kind']}: {rec['why']} (cost: {rec['cost']})")
    if structure["cycles"]:
        cyc = structure["cycles"]
        add("low", f"{len(cyc)} import cycles between units (e.g. {', '.join(f'`{a}` ↔ `{b}`' for a, b in cyc[:3])}); "
                   "hub units' instructions should say what they own and what dependents may rely on")
    candidates = [u for u in units if u["strength"] == "strong" and not u["covered_by"]]
    for u in candidates[:UNIT_TOP]:
        add("medium", f"`{u['unit']}/` may need its own {u['suggested_home']}: {'; '.join(u['reasons'])}")
    if len(candidates) > UNIT_TOP:
        add("info", f"...and {len(candidates) - UNIT_TOP} more unit candidates (see `units` in --format json)")
    for r in rules:
        if r["external_symlink"] and r["paths"]:
            add("high", "symlink points outside the repo; path-scoped external rules never load", r["path"])
    for x in skills:
        if x["pointer_only"]:
            add("medium", "skill only points to another file; if it's constraints for certain files, a path-scoped "
                          "`.claude/rules/` file (a symlink works) loads them automatically with no per-turn cost", x["path"])
    claude_present = bool(claude_md or rules or skills or agents or commands or
                          any(a["loads"].startswith("always (read") for a in agents_md))
    if not claude_present and not other:
        add("info", "no AI instruction files found (greenfield)")
    if other and not claude_present:
        add("medium", f"only other tools' instruction files exist ({', '.join(other)}); Claude reads none of them. "
                      "Share them via AGENTS.md (Claude reads it natively) or a CLAUDE.md that imports it")
    for a in agents_md:
        if a["loads"].startswith("never"):
            add("high", f"Claude ignores this AGENTS.md: {a['loads'][7:-1]}" if not a["at_root"] else
                "Claude ignores this AGENTS.md because a root CLAUDE.md exists; add `@AGENTS.md` to CLAUDE.md", a["path"])
    unlinked = [p for p in other if p != "AGENTS.md" and p.rstrip("/") not in imported]
    if unlinked and claude_present:
        add("low", f"both Claude and other-tool instruction files exist ({', '.join(unlinked)}); check for drift/duplication")
    local_only = {"CLAUDE.local.md", "settings.local.json"}
    link_targets = [rel(relpaths[r["path"]].resolve(), root) for r in rules
                    if relpaths[r["path"]].is_symlink() and not r["external_symlink"]]
    shared = list(dict.fromkeys(instr_paths + link_targets +
                                ([".claude/settings.json"] if ".claude/settings.json" in settings else [])))
    untracked = [p for p in shared if git_code == 0 and p not in tracked and Path(p).name not in local_only]
    if untracked:
        add("medium", f"instruction files not committed, so teammates don't get them: {', '.join(untracked[:10])}"
                      + (f" (+{len(untracked) - 10} more)" if len(untracked) > 10 else ""))
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
    if not env_examples and any(Path(p).name.startswith(".env") for p in secret_files):
        add("medium", "env files exist but no .env.example documents the variable names")
    if not ci:
        add("low", "no CI config found")

    findings.sort(key=lambda f: SEV_ORDER[f["severity"]])

    state = "existing" if claude_present else "other-tools-only" if other else "greenfield"

    return {
        "repo": root.name,
        "state": state,
        "stack": {"manifests": sorted(manifests, key=lambda m: m["path"]), "app_roots": app_roots,
                  "is_monorepo": is_monorepo, "ci": ci, "parts": parts,
                  "npm_scripts": {k: sorted(v) for k, v in npm_scripts.items()},
                  "make_targets": sorted(make_targets)},
        "claude": {"claude_md": claude_md, "rules": rules, "imports": imports, "skills": skills,
                   "agents": agents, "commands": commands, "settings": settings, "mcp_json": mcp_json},
        "agents_md": agents_md,
        "units": units,
        "structure": structure,
        "sessions": sessions,
        "other_tools": {"files": other_detail, "nested_agents_md": nested_agents_md},
        "context_budget": {"always_loaded_lines": always_lines, "always_loaded_approx_tokens": always_tokens,
                           "target_lines": BUDGET_ALWAYS_LOADED,
                           "model_invocable_description_chars": desc_chars},
        "secrets": {"secret_like_files": secret_files, "gitignored": ignored, "tracked": tracked_secrets,
                    "env_examples": env_examples},
        "hygiene": hygiene,
        "environment": environment,
        "ci_steps": ci_detail,
        "checks": {"duplicates": duplicates, "stale_paths": stale_paths, "stale_commands": stale_cmds},
        "findings": findings,
    }


def covering(r: dict, root: Path, targets: list[str]) -> list[dict]:
    """For each file, the instruction files Claude loads when working on it, and where to edit them."""
    out = []
    for t in targets:
        t = Path(t).resolve().relative_to(root).as_posix() if os.path.isabs(t) else t.strip("./") or t
        hits = []

        def add(path, loads, lines, budget, kind):
            f = root / path
            edit_at = rel(f.resolve(), root) if f.is_symlink() else path
            hits.append({"file": path, "edit_at": edit_at, "loads": loads, "lines": lines, "budget": budget,
                         "room": budget - lines, "kind": kind})

        for c in r["claude"]["claude_md"]:
            d = str(Path(c["path"]).parent)
            body = [l for l in strip_html_comments(read_text(root / c["path"])).splitlines() if l.strip()]
            if body and all(re.fullmatch(r"\s*@\S+\s*", l) for l in body):
                continue  # a shim that only imports another file: edit the imported file instead
            if c["loads"] == "always":
                add(c["path"], "every session", c["lines"], BUDGET_ROOT_CLAUDE, "root")
            elif t.startswith(d + "/"):
                add(c["path"], f"when working in `{d}/`", c["lines"], BUDGET_UNIT, "nested")
        for a in r["agents_md"]:
            d = str(Path(a["path"]).parent)
            if a["loads"].startswith("never"):
                continue
            if a["at_root"]:
                add(a["path"], "every session", a["lines"], BUDGET_ROOT_CLAUDE, "root")
            elif t.startswith(d + "/"):
                add(a["path"], f"when working in `{d}/`", a["lines"], BUDGET_UNIT, "nested")
        for i in r["claude"]["imports"]:
            if i["exists"] and not any(h["file"] == i["resolved"] for h in hits):
                add(i["resolved"], f"every session (imported by `{i['from']}`)", i["lines"], BUDGET_ROOT_CLAUDE, "root")
        for x in r["claude"]["rules"]:
            if x["loads"] == "always":
                add(x["path"], "every session (no `paths:`)", x["lines"], BUDGET_RULE, "rule")
            elif x["paths"] and any(glob_match(t, g) for g in x["paths"]):
                add(x["path"], "when matching files are touched: " + ", ".join(x["paths"]), x["lines"], BUDGET_RULE, "rule")
        # Edit the most specific file first: nested, then scoped rule, then root.
        order = {"nested": 0, "rule": 1, "root": 2}
        hits.sort(key=lambda h: (order[h["kind"]], -len(h["file"])))
        out.append({"target": t, "exists": (root / t).exists(), "covered_by": hits,
                    "scoped": any(h["kind"] != "root" for h in hits)})
    return out


def covering_md(cov: list[dict]) -> str:
    L = []
    for c in cov:
        L += [f"## `{c['target']}`" + ("" if c["exists"] else " (no such file)"), ""]
        if not c["scoped"]:
            L += ["No scoped rule or app file covers this path: a new path-scoped rule is the right home "
                  "(or the root file, if it truly applies everywhere).", ""]
        if c["covered_by"]:
            L += ["| Instruction file | Edit at | Loads | Lines / budget |", "|---|---|---|---|"]
            for h in c["covered_by"]:
                over = " **full: consolidate before adding**" if h["room"] <= 0 else ""
                L.append(f"| `{h['file']}` | `{h['edit_at']}` | {h['loads']} | {h['lines']} / {h['budget']}{over} |")
            L.append("")
    return "\n".join(L)


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
    for a in r["agents_md"]:
        L.append(f"| `{a['path']}` | {a['loads']} | {a['lines']} |")
    for i in r["claude"]["imports"]:
        L.append(f"| `@{i['import']}` (from `{i['from']}`) | always (import) | {i['lines']} |")
    for o in [o for o in r["other_tools"]["files"] if o["path"] != "AGENTS.md"]:
        L.append(f"| `{o['path']}` | other tool | {o.get('lines', str(o.get('files')) + ' files')} |")
    L += ["", f"Skills: {len(r['claude']['skills'])} · Agents: {len(r['claude']['agents'])} · "
          f"Commands: {len(r['claude']['commands'])} · Settings: {', '.join(r['claude']['settings']) or 'none'}"]
    L += ["", "## Stack", ""] + [f"- `{m['path']}` ({m['kind']})" for m in r["stack"]["manifests"]]
    L += [f"- CI: {', '.join(r['stack']['ci']) or 'none'}"]
    for pt in r["stack"]["parts"]:
        cov = ", ".join(pt["rules"]) or ("nested CLAUDE.md/AGENTS.md" if pt["nested_instructions"] else "none")
        L.append(f"- Part `{pt['root']}/`: {pt['files']} files, last change {pt['last_change'] or '?'}"
                 f"{' (dormant)' if pt['dormant'] else ''}, instructions: {cov}")
    cands = [u for u in r["units"] if u["strength"] == "strong"]
    weak = [u for u in r["units"] if u["strength"] == "weak"]
    if r["units"]:
        counts = {}
        for u in r["units"]:
            counts[u["collection"]] = counts.get(u["collection"], 0) + 1
        colls = ", ".join(f"`{c}/` ({n} units)" for c, n in sorted(counts.items()))
        L += ["", "## Units (subfolders that may need their own instructions)", "",
              f"Collections: {colls}. Window: last {UNIT_WINDOW_DAYS} days.", ""]
        if cands:
            L += ["| Unit | Code files | Commits (fixes) | Signals | Suggested home | Covered by |", "|---|---|---|---|---|---|"]
            for u in cands[:UNIT_TOP * 2]:
                L.append(f"| `{u['unit']}/` | {u['code_files']} | {u['commits']} ({u['fix_commits']}) | "
                         f"{'; '.join(u['reasons'])} | {u['suggested_home']} | {', '.join(u['covered_by']) or '-'} |")
        else:
            L.append("No unit stands out from its siblings; collection-wide rules are enough.")
        st = r["structure"]
        L += ["", f"**Structure:** {st['code_files']} code files, {st['units']} units, {st['cross_unit_links']} "
                  f"cross-unit import links, {len(st['cycles'])} cycles. Hubs: "
                  + (", ".join(f"`{h['unit']}/` (used by {h['used_by']})" for h in st["hubs"][:6]) or "none")
                  + (f". Graph tools present: {', '.join(st['graph_tools'])}" if st["graph_tools"] else "")
                  + (f". Structure map: {', '.join(f'`{m}`' for m in st['maps'])} (regenerate with `--map` after big moves)"
                     if st["maps"] else "")]
        for rec in st["recommendations"]:
            L.append(f"- Consider **{rec['kind']}**: {rec['why']}. Cost: {rec['cost']}.")
        if weak:
            L += ["", "One signal only (watch with `/agent-readiness:learn`, no file yet): " +
                  ", ".join(f"`{u['unit']}/` ({u['reasons'][0]})" for u in weak[:UNIT_TOP])]
    if r["sessions"]:
        L += ["", "Sessions started here: " + ", ".join(f"`{k}` {v}" for k, v in sorted(r["sessions"].items()))]
    if r.get("environment"):
        yes = lambda v: "✓" if v else "-"
        L += ["", "## Environment per part (scorecard E)", "",
              "| Part | Lockfile | Runtime pinned | Setup | Linter | Formatter | Type checker | Coverage | CI tests | CI lint |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for e in r["environment"]:
            L.append(f"| `{e['part']}` | " + " | ".join(yes(e[k]) for k in ("lockfile", "runtime_pinned", "setup", "linter",
                     "formatter", "type_checker", "coverage", "ci_tests", "ci_lint")) + " |")
        L += [""] + [f"- `{c['file']}`: {', '.join(c['steps']) or 'no recognised steps'} (in {', '.join(f'`{p}`' for p in c['parts'])})"
                     for c in r.get("ci_steps", [])]
    L += ["", "## Repo files (scorecard H4-H6, E1)", ""] + \
         [f"- {k}: {', '.join(v) or 'none found'}" for k, v in r["hygiene"].items()]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--format", choices=["json", "md"], default="md")
    ap.add_argument("--layout", choices=["auto", "nested", "rules"], default="auto",
                    help="where units' own instructions go: auto (by signals), nested CLAUDE.md/AGENTS.md, or path-scoped rules")
    ap.add_argument("--map", action="store_true", help="print the generated structure map (markdown) instead of the report")
    ap.add_argument("--covering", nargs="+", metavar="PATH",
                    help="list the instruction files that load for these files (for /agent-readiness:learn)")
    ap.add_argument("--fail-on", choices=["high", "medium", "low"],
                    help="exit 1 if any finding is at or above this severity (for CI)")
    a = ap.parse_args()
    root = Path(a.root)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    result = scan(root, a.layout)
    if a.covering:
        cov = covering(result, root.resolve(), a.covering)
        print(json.dumps(cov, indent=2) if a.format == "json" else covering_md(cov))
        return 0
    if a.map:
        print(structure_map(result))
        return 0
    print(json.dumps(result, indent=2) if a.format == "json" else to_md(result))
    if a.fail_on and any(SEV_ORDER[f["severity"]] <= SEV_ORDER[a.fail_on] for f in result["findings"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
