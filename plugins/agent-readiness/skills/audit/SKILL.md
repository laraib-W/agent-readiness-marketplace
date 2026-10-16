---
name: audit
description: Make any repository agent-ready for Claude Code. Detects whether AI instruction files already exist, scores readiness, and proposes repo-specific, token-efficient CLAUDE.md, rules, skills, agents and guardrails; applies only what the user approves.
argument-hint: "[scan | report | apply] [optional sub-path] [layout=auto|nested|rules]"
disable-model-invocation: true
allowed-tools: Bash(python3 -I ${CLAUDE_SKILL_DIR}/scripts/scan.py *)
---

# Agent readiness audit

Arguments: `$ARGUMENTS`

- `scan`: run step 1 only and show the result.
- `report`: steps 1–6, write the report, change nothing else.
- `apply` or no argument: full flow, steps 1–8.
- A path (e.g. `backend/`): limit discovery and proposals to that part of the repo.
- `layout=nested` or `layout=rules`: where per-unit instructions go (default `auto`, decided by the scan). Pass it to the scan as `--layout`.

## Non-negotiables

1. **Repo-specific or nothing.** Every proposed line must be grounded in this repo, with a file path as evidence. If a sentence could be pasted unchanged into another repo's `CLAUDE.md`, delete it.
2. **Context is a budget.** Read `${CLAUDE_SKILL_DIR}/references/context-principles.md` before proposing any file. Prefer the narrowest home: path-scoped rule > nested `CLAUDE.md` > root `CLAUDE.md`. Removing or scoping an existing line is as valuable as adding one.
3. **No secrets, no real data.** Never open `.env*` (except examples), keys, credentials, dumps, or fixtures with personal data. Never connect to non-local databases or services.
4. **Nothing changes without approval.** Steps 1–6 change nothing except writing the report. Show diffs for existing files before editing them.
5. **Don't guess commands.** Only commands found in scripts/Makefile/CI/README, or verified by running a safe read-only command, may appear in instruction files. Otherwise mark them `UNVERIFIED`. A README is a source only where the scan didn't flag it. Run the single-test and lint commands once on one small file before writing them down. If they fail because of the local environment (e.g. a missing package), record the fix rather than dropping the command.
6. **Stay in scope; never commit.** The plugin creates or edits only agent-instruction files: `CLAUDE.md`/`AGENTS.md` at any level, `.claude/` (rules, settings, skills, agents), other tools' rule files (`.cursor/rules/`, a shared rules folder such as `.ai/`), and `docs/agent-readiness/`. Everything else (source code, Makefiles, CI, README, `.gitignore`, dependency and config files, repo settings) is a **recommendation** in the report, even when the fix is one line. It never runs `git commit` or `git push` in the target repo; committing is the user's call.

## Flow

### 1. Scan (deterministic, cheap)
Run:
```
python3 -I ${CLAUDE_SKILL_DIR}/scripts/scan.py . --format md
```
Keep the output; it gives the repo **state** (`greenfield`, `other-tools-only`, `existing`), the stack, every instruction file with when it loads, the always-loaded size, and mechanical findings (stale commands/paths, duplicates, unscoped rules, missing guardrails, committed secrets), and a `Hygiene` section that answers scorecard S8–S11. Don't re-derive what the scan already measured.

If the argument is `scan`, show it and stop.

### 2. Discover the app
Delegate to the **repo-architect** subagent. Give it the scan output (including the strong rows of the Units table) and the focus path. If good architecture docs already exist (README, `docs/`), tell it to verify and fill gaps rather than re-document everything. It returns a compact architecture summary with evidence.

### 3. Audit existing instructions (skip if state is `greenfield`)
Delegate to the **instruction-auditor** subagent with the scan output, the architecture summary, and the path `${CLAUDE_SKILL_DIR}/references/audit-existing.md` for its classification rules. It checks every existing instruction file (Claude and other tools) against the code: stale, wrong, generic, duplicated, conflicting, or misplaced content. Read `${CLAUDE_SKILL_DIR}/references/audit-existing.md` to interpret its result. If the user also runs the built-in `/doctor prompt-audit`, it covers wording quality; don't repeat its findings.

### 4. Score
Read `${CLAUDE_SKILL_DIR}/references/scorecard.md`. Score every criterion 0/1/2 using scan + discovery + audit evidence, compute the maturity level, and list the top 5 fixes by impact ÷ effort.

### 5. Decide the structure
Read `${CLAUDE_SKILL_DIR}/references/structure-decision.md` and answer each decision for this repo with evidence. The shape must follow the repo: a single small app may need only a short root `CLAUDE.md` and a `settings.json`; a monorepo may need per-part rules, and strong units inside a part may need their own (§1b). Don't add skills, agents or commands without a concrete, repo-specific reason.

### 6. Write the report
Write `docs/agent-readiness/report.md` (create the folder) containing:
1. Summary: state, maturity level, score, always-loaded size now → after the proposal
2. Architecture summary (from step 2, trimmed)
3. Scorecard table
4. Findings on existing files (from step 3)
5. Structure decision
6. **Roadmap**: next levels' failing gates and fixes, per `scorecard.md` (agent-doable vs team), with projected score and level
7. **Change plan**, a table with: action (create / edit / move / scope / delete) | file | why (evidence) | always-loaded lines delta
8. Questions for the team / `UNVERIFIED` items

Then show the user a short summary: level, score, the roadmap (what unlocks the next level), the change-plan table, and the context delta. Offer to apply the agent-doable roadmap fixes as change-plan rows. If the argument is `report`, stop here.

### 7. Apply approved changes
Ask which rows of the change plan to apply; only in-scope rows (non-negotiable 6) can be applied. For each approved row:
- **create**: write the file. Use `${CLAUDE_SKILL_DIR}/references/guardrails.md` for `.claude/settings.json`. For an approved structure map, run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/scan.py . --map` and save its output to `docs/agent-readiness/structure.md`.
- **edit / scope / move / delete**: show the diff first, wait for OK, then apply. Moving content means removing it from the old location in the same change, so nothing is duplicated.
- Never overwrite user-written content silently. Keep their wording where it is still correct.

### 8. Verify
Re-run the scan. Report before → after for: findings by severity, always-loaded lines and tokens, score. If the scan says instruction files aren't committed, tell the user to review and commit them (don't do it for them); until then nobody else benefits. Tell the user how to confirm in a live session (`/context`, and touching a file in each scoped area to see the `Loaded` line). Suggest `/agent-readiness:learn` for the ongoing feedback loop, and a scheduled re-audit: `scan.py --fail-on high` in CI, or a scheduled Claude Code routine (`/schedule`) running `/agent-readiness:audit report`.
