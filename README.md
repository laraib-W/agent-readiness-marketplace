# agent-readiness

A Claude Code plugin that makes any repository agent-ready, whether it has no AI files yet or already has `CLAUDE.md`, rules, `AGENTS.md` or Cursor/Copilot rules that need optimizing.

Its output is specific to each repo: every proposed instruction must cite evidence from that repo, and it optimizes for **relevant context only**. It measures how many instruction lines load in every session and pushes area-specific rules into files that load only when needed.

## What's inside

| Component | Type | What it does |
|---|---|---|
| `/agent-readiness:audit` | Skill (user-triggered) | Full flow: scan → discover → audit existing files → score → structure decision → report → apply approved changes → verify |
| `/agent-readiness:learn` | Skill (user-triggered) | Turns a correction into the narrowest rule (hook, path-scoped rule, nested or root `CLAUDE.md`) and prunes stale rules |
| `repo-architect` | Subagent (Read/Glob/Grep only) | Maps the app: stack, modules, data model, API, auth, frontend, commands, risk zones |
| `instruction-auditor` | Subagent (read-only by instruction; Bash for `git log`) | Checks existing instruction files against the code: stale, wrong, duplicated, misplaced |
| `scan.py` | Script (stdlib Python) | Deterministic facts: instruction files and when they load, always-loaded size, broken imports/globs, stale commands/paths, duplicates, secret-file protection, and which subfolders (apps, features, packages) stand out from their siblings enough to need their own rule or `CLAUDE.md` |

It complements the built-in `/doctor prompt-audit`, which reviews instruction wording. This plugin adds the deterministic scan, scoring, file layout and guardrails.

Both skills are user-triggered (`disable-model-invocation: true`), so they add **no per-turn context** when you're not using them.

## Install

**Try it locally:**
```bash
claude --plugin-dir ./plugins/agent-readiness
```

**For your team:**
```bash
claude plugin marketplace add laraib-W/agent-readiness-marketplace
claude plugin install agent-readiness@agent-readiness-tools
```

## Use

In the repo you want to improve:

```
/agent-readiness:audit scan        # quick, deterministic scan only
/agent-readiness:audit report      # full analysis + change plan, changes nothing
/agent-readiness:audit             # full flow, applies only what you approve
/agent-readiness:audit backend/    # focus on one part of a monorepo
/agent-readiness:learn "Claude put business logic in the view again"
```

The scan also runs on its own, e.g. in CI:
```bash
python3 plugins/agent-readiness/skills/audit/scripts/scan.py /path/to/repo --format json
```

Add `--fail-on high` (or `medium`/`low`) to exit 1 when findings at that severity exist.

Scanner regression check: `python3 tests/test_scan.py`.

Outputs go to `docs/agent-readiness/` in the target repo (`report.md`, `lessons.md`).

## Safety

- Discovery and audit are read-only; every change is shown as a diff and needs approval.
- It only edits agent-instruction files (`CLAUDE.md`, `AGENTS.md`, `.claude/`, other tools' rule files) and `docs/agent-readiness/`. Code, build, CI, README and `.gitignore` fixes are recommendations in the report. It never commits or pushes.
- Never reads `.env*` (except examples), keys, credentials or data dumps; the scan reports them by name only.
- No network calls; nothing leaves your machine except what Claude Code itself sends to the model.
- To see where you start sessions, the scan lists folder names (not contents) under `~/.claude/projects`.

## Scoring

Three sub-scores, each criterion 0/1/2, reported separately rather than added up:

- **A. Agent context** (6 criteria): instructions, relevant context only, accuracy, secrets out of reach, approval for dangerous actions, feedback after edits. This is what the plugin builds.
- **E. Agent environment** (8): setup, run, tests, risk-zone tests, lint, type checking, CI, local data. Can the agent check its own work? The team fixes these.
- **H. Repo hygiene** (6): secrets in git, docs, conventions, pre-commit, review routing, security automation. Good practice, not agent-specific.

A 5-level maturity model (Functional → Documented → Standardized → Optimized → Autonomous), adapted from Factory's Agent Readiness Model, gates on context and environment only. Every report includes a roadmap to the next two levels, split into plugin and team fixes. See `skills/audit/references/scorecard.md`.
