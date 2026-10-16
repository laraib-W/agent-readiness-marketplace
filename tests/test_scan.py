"""Regression check for scan.py. Run: python3 tests/test_scan.py (or pytest)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-readiness/skills/audit/scripts"))
import scan  # noqa: E402

FILES = {
    "packages/a/src/components/x/y/z/Deep.tsx": "",
    "src/a.tsx": "",
    "app/utils/secrets.py": "",
    ".storybook/main.js": "",
    "docs/arch.md": "# Arch\n",
    "Makefile": "test:\n\techo ok\n",
    "package.json": '{"scripts": {"build": "x"}}',
    ".env": "SECRET=1\n",
    "local.pem": "x\n",
    ".gitignore": ".env\nlocal.pem\n",
    "data.dump": "",  # not ignored, not tracked
    "e2e/cy.env": "K=v\n",
    "web/package.json": "{}",
    "web/src/a.js": "",
    "old/package.json": "{}",
    "README.md": "```bash\nmake quality\nyou have to make a copy\n```\n",
    "AGENTS.md": "# Shared\nRead `apps/users/api.py` and `api.py`. There is no `services/` package.\n",
    "apps/users/api.py": "",
    ".claude/skills/pointer/SKILL.md": "---\nname: pointer\ndescription: x\n---\nRead and follow `.ai/rules/x.md`.\n",
    ".claude/rules/csv.md": '---\npaths: "src/**/*.ts, docs/*.md"\n---\nx\n',
    ".cursor/rules/x.mdc": "Read and follow @.ai/rules/gone.md\n",
    ".claude/settings.json": '{"permissions": {"deny": ["Read(./.env)"]}}',
    ".claude/rules/braces.md": '---\npaths: ["src/**/*.{ts,tsx}"]\n---\nx\n',
    ".claude/rules/deep.md": '---\npaths: ["packages/**/Deep.tsx"]\n---\nx\n',
    ".claude/rules/dotdir.md": '---\npaths: [".storybook/**"]\n---\nx\n',
    ".claude/rules/dead.md": '---\npaths: ["nope/**"]\n---\nx\n',
    "CLAUDE.md": "See @docs/arch.md. Install @types/node. Ping @someone. Also @missing.md.\n"
                 "```bash\nmake -j4 test\nmake deploy\nyarn workspace web build\nnpm run lint\n```\n",
}


def run_scan() -> dict:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for p, body in FILES.items():
            (root / p).parent.mkdir(parents=True, exist_ok=True)
            (root / p).write_text(body)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-f", ".env", "CLAUDE.md", "old"], cwd=root, check=True)  # .env committed despite .gitignore
        old = {"GIT_AUTHOR_DATE": "2019-01-01T00:00:00", "GIT_COMMITTER_DATE": "2019-01-01T00:00:00"}
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "old", "--", "old"],
                       cwd=root, check=True, env={**os.environ, **old})
        return scan.scan(root)


def test_scan():
    r = run_scan()
    text = "\n".join(f["finding"] + " " + f.get("where", "") for f in r["findings"])

    never_loads = {f["where"] for f in r["findings"] if "never loads" in f["finding"]}
    assert never_loads == {".claude/rules/dead.md"}, never_loads

    missing = {i["import"] for i in r["claude"]["imports"] if not i["exists"]}
    assert missing == {"missing.md"}, missing

    stale = {s["command"] for s in r["checks"]["stale_commands"] if s["file"] == "CLAUDE.md"}
    assert stale == {"make deploy", "npm run lint"}, stale

    assert "app/utils/secrets.py" not in r["secrets"]["secret_like_files"]
    assert r["secrets"]["tracked"] == [".env"]
    assert "committed to git" in text
    # .env is denied, local.pem isn't
    undenied = next(f["finding"] for f in r["findings"] if "not denied by Read()" in f["finding"])
    assert set(undenied.split(": ", 1)[1].split(", ")) == {"data.dump", "e2e/cy.env", "local.pem"}, undenied
    assert r["hygiene"]["codeowners"] == []

    # check-ignore must actually run (it needs --stdin with -z)
    assert "secret-like files not gitignored: data.dump" in text
    # README commands are checked; prose in a fence isn't a command
    assert {s["command"] for s in r["checks"]["stale_commands"] if s["file"] == "README.md"} == {"make quality"}
    # AGENTS.md is ignored by Claude when a root CLAUDE.md doesn't import it
    assert any(f["where"] == "AGENTS.md" and "ignores this AGENTS.md" in f["finding"] for f in r["findings"])
    # suffix-matched and negated refs aren't stale; dangling @ref in another tool's file is
    stale_refs = {s["ref"] for s in r["checks"]["stale_paths"]}
    assert "@.ai/rules/gone.md" in stale_refs and not stale_refs & {"apps/users/api.py", "api.py", "services"}, stale_refs
    assert any("only points to another file" in f["finding"] for f in r["findings"])
    assert next(x for x in r["claude"]["rules"] if x["path"].endswith("csv.md"))["paths"] == ["src/**/*.ts", "docs/*.md"]
    assert "e2e/cy.env" in r["secrets"]["secret_like_files"]
    parts = {p["root"]: p for p in r["stack"]["parts"]}
    assert parts["old"]["dormant"] and not parts["web"]["dormant"]
    assert any(f["finding"].startswith("`web/`") and "no area instructions" in f["finding"] for f in r["findings"])
    assert not any(f["finding"].startswith("`old/`") and "no area instructions" in f["finding"] for f in r["findings"])
    untracked = next(f["finding"] for f in r["findings"] if "not committed" in f["finding"])
    assert ".claude/rules/braces.md" in untracked and "CLAUDE.md," not in untracked


def test_units():
    """A collection of similar apps; one has its own stack, one its own tooling, one is quiet."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for app in ["alpha", "beta", "gamma", "delta", "eps"]:
            for f in ["models.py", "urls.py", "views.py", "admin.py", "serializers.py"]:
                (root / "apps" / app).mkdir(parents=True, exist_ok=True)
                (root / "apps" / app / f).write_text("import django\n")
        (root / "apps/alpha/views.py").write_text('import langgraph\nimport pydantic\nfrom openai import OpenAI\n"""\nimport there is prose\n"""\n')
        (root / "apps/alpha/tests.py").write_text("import freezegun\n")  # test-only: not a signal
        (root / "apps/beta/package.json").write_text("{}")
        run = lambda *a, **kw: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                              cwd=root, check=True, capture_output=True, **kw)
        run("init", "-q")
        run("add", ".")
        run("commit", "-qm", "init")
        for i in range(10):  # recent activity in alpha and beta only
            for app in ["alpha", "beta"]:
                (root / "apps" / app / "models.py").write_text(f"# {i}\n")
            run("commit", "-qam", f"change {i}")
        units = {u["unit"]: u for u in scan.scan(root)["units"]}

    alpha, beta, gamma = units["apps/alpha"], units["apps/beta"], units["apps/gamma"]
    assert alpha["own_deps"] == ["langgraph", "openai", "pydantic"], alpha["own_deps"]
    assert alpha["strength"] == "strong" and alpha["suggested_home"] == "path-scoped rule"
    assert beta["strength"] == "strong" and beta["suggested_home"] == "nested CLAUDE.md/AGENTS.md"
    assert gamma["strength"] is None


if __name__ == "__main__":
    test_scan()
    test_units()
    print("ok")
