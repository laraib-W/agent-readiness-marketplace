"""Regression check for scan.py. Run: python3 tests/test_scan.py (or pytest)."""
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
        subprocess.run(["git", "add", "-f", ".env"], cwd=root, check=True)  # committed despite .gitignore
        return scan.scan(root)


def test_scan():
    r = run_scan()
    text = "\n".join(f["finding"] + " " + f.get("where", "") for f in r["findings"])

    never_loads = {f["where"] for f in r["findings"] if "never loads" in f["finding"]}
    assert never_loads == {".claude/rules/dead.md"}, never_loads

    missing = {i["import"] for i in r["claude"]["imports"] if not i["exists"]}
    assert missing == {"missing.md"}, missing

    stale = {s["command"] for s in r["checks"]["stale_commands"]}
    assert stale == {"make deploy", "npm run lint"}, stale

    assert "app/utils/secrets.py" not in r["secrets"]["secret_like_files"]
    assert r["secrets"]["tracked"] == [".env"]
    assert "committed to git" in text
    # .env is denied, local.pem isn't
    assert "not denied by Read() rules in .claude/settings.json: local.pem" in text
    assert r["hygiene"]["codeowners"] == []


if __name__ == "__main__":
    test_scan()
    print("ok")
