---
name: audit
description: Make any repository agent-ready for Claude Code. Detects whether AI instruction files already exist, scores readiness, and proposes repo-specific, token-efficient CLAUDE.md, rules, skills, agents and guardrails; applies only what the user approves.
argument-hint: "[scan | report | apply | bench | eval] [optional sub-path] [layout=auto|nested|rules]"
disable-model-invocation: true
allowed-tools: Bash(python3 -I ${CLAUDE_SKILL_DIR}/scripts/scan.py *), Bash(python3 -I ${CLAUDE_SKILL_DIR}/scripts/bench.py *), Bash(python3 -I ${CLAUDE_SKILL_DIR}/scripts/evals.py *)
---

# Agent readiness audit

Arguments: `$ARGUMENTS`

- `scan`: run step 1 only and show the result.
- `report`: steps 1–6, write the report, change nothing else.
- `apply` or no argument: full flow, steps 1–8.
- A path (e.g. `backend/`): limit discovery and proposals to that part of the repo.
- `bench`: measure the instructions with real headless Claude runs, before vs after (see **Benchmark** below). Costs tokens.
- `eval`: can an agent redo real merged changes, graded by their own tests, with and without the instructions (see **Eval harness** below). `build` is free; `run` costs tokens.
- `layout=nested` or `layout=rules`: where per-unit instructions go (default `auto`, decided by the scan). Pass it to the scan as `--layout`.

## Non-negotiables

1. **Repo-specific or nothing.** Every proposed line must be grounded in this repo, with a file path as evidence. If a sentence could be pasted unchanged into another repo's `CLAUDE.md`, delete it.
2. **Context is a budget.** Read `${CLAUDE_SKILL_DIR}/references/context-principles.md` before proposing any file. Prefer the narrowest home: path-scoped rule > nested `CLAUDE.md` > root `CLAUDE.md`. Removing or scoping an existing line is as valuable as adding one.
3. **No secrets, no real data.** Never open `.env*` (except examples), keys, credentials, dumps, or fixtures with personal data. Never connect to non-local databases or services.
4. **Nothing changes without approval.** Steps 1–6 change nothing except writing the report. Show diffs for existing files before editing them.
5. **Don't guess commands.** Only commands found in scripts/Makefile/CI/README, or verified by running a safe read-only command, may appear in instruction files. Otherwise mark them `UNVERIFIED`. A README is a source only where the scan didn't flag it. Run the single-test and lint commands once on one small file before writing them down, and read the result, not just the exit code: if every file fails with the same error (a parse or config error), the command is broken. Record the working form and recommend fixing the original. If they fail because of the local environment (e.g. a missing package), record the fix rather than dropping the command.
6. **Stay in scope; never commit.** The plugin creates or edits only agent-instruction files: `CLAUDE.md`/`AGENTS.md` at any level, `.claude/` (rules, settings, skills, agents), other tools' rule files (`.cursor/rules/`, a shared rules folder such as `.ai/`), and `docs/agent-readiness/`. Everything else (source code, Makefiles, CI, README, `.gitignore`, dependency and config files, repo settings) is a **recommendation** in the report, even when the fix is one line. It never runs `git commit` or `git push` in the target repo; committing is the user's call.

## Flow

### 1. Scan (deterministic, cheap)
Run:
```
python3 -I ${CLAUDE_SKILL_DIR}/scripts/scan.py . --format md
```
Keep the output; it gives the repo **state** (`greenfield`, `other-tools-only`, `existing`), the stack, every instruction file with when it loads, the always-loaded size, and mechanical findings (stale commands/paths, duplicates, unscoped rules, missing guardrails, committed secrets), and a `Repo files` section that answers scorecard H4–H6. Don't re-derive what the scan already measured.

If the argument is `scan`, show it and stop.

### 2. Discover the app
Delegate to the **repo-architect** subagent. Give it the scan output (including the strong rows of the Units table) and the focus path. If good architecture docs already exist (README, `docs/`), tell it to verify and fill gaps rather than re-document everything. It returns a compact architecture summary with evidence.

### 3. Audit existing instructions (skip if state is `greenfield`)
Delegate to the **instruction-auditor** subagent with the scan output, the architecture summary, and the path `${CLAUDE_SKILL_DIR}/references/audit-existing.md` for its classification rules. It checks every existing instruction file (Claude and other tools) against the code: stale, wrong, generic, duplicated, conflicting, or misplaced content. Read `${CLAUDE_SKILL_DIR}/references/audit-existing.md` to interpret its result. If the user also runs the built-in `/doctor prompt-audit`, it covers wording quality; don't repeat its findings.

### 4. Score
Read `${CLAUDE_SKILL_DIR}/references/scorecard.md`. Score every criterion 0/1/2 using scan + discovery + audit evidence, report the three sub-scores (A context, E environment, H hygiene) separately, compute the maturity level from A and E, and list the top 5 fixes by impact ÷ effort.

### 5. Decide the structure
Read `${CLAUDE_SKILL_DIR}/references/structure-decision.md` and answer each decision for this repo with evidence. The shape must follow the repo: a single small app may need only a short root `CLAUDE.md` and a `settings.json`; a monorepo may need per-part rules, and strong units inside a part may need their own (§1b). Don't add skills, agents or commands without a concrete, repo-specific reason.

### 6. Write the report
Write `docs/agent-readiness/report.md` (create the folder) containing:
1. Summary: state, maturity level, the three sub-scores, always-loaded size now → after the proposal
2. Architecture summary (from step 2, trimmed)
3. Scorecard table
4. Findings on existing files (from step 3)
5. Structure decision
6. **Roadmap**: next levels' failing gates and fixes, per `scorecard.md` (agent-doable vs team), with projected score and level
7. **Change plan**, a table with: action (create / edit / move / scope / delete) | file | why (evidence) | always-loaded lines delta
8. Questions for the team / `UNVERIFIED` items

Then show the user a short summary: level, the three sub-scores, the roadmap (what unlocks the next level), the change-plan table, and the context delta. Offer to apply the plugin's roadmap fixes as change-plan rows. If the argument is `report`, stop here.

### 7. Apply approved changes
Ask which rows of the change plan to apply; only in-scope rows (non-negotiable 6) can be applied. For each approved row:
- **create**: write the file. Use `${CLAUDE_SKILL_DIR}/references/guardrails.md` for `.claude/settings.json`. For an approved structure map, run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/scan.py . --map` and save its output to `docs/agent-readiness/structure.md`.
- **edit / scope / move / delete**: show the diff first, wait for OK, then apply. Moving content means removing it from the old location in the same change, so nothing is duplicated.
- Never overwrite user-written content silently. Keep their wording where it is still correct.

### 8. Verify
Re-run the scan. Report before → after for: findings by severity, always-loaded lines and tokens, the three sub-scores and the level. If the scan says instruction files aren't committed, tell the user to review and commit them (don't do it for them); until then nobody else benefits. Tell the user how to confirm in a live session (`/context`, and touching a file in each scoped area to see the `Loaded` line). Suggest `/agent-readiness:learn` for the ongoing feedback loop, and a scheduled re-audit: `scan.py --fail-on high` in CI, or a scheduled Claude Code routine (`/schedule`) running `/agent-readiness:audit report`.

## Benchmark (`bench`)

Measures whether the instruction files actually help, using real headless Claude runs. It never modifies the repo: it builds two throwaway copies, BEFORE (a git ref, default `HEAD`) and AFTER (the same ref plus the working tree's instruction files), so the instruction files are the only difference.

1. Run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/bench.py .` with no `--yes`. It only prints the plan: tasks picked from the scan (path-scoped areas and covered strong units, each with the paths and names its instructions point to), guardrail canary probes, the number of runs, and the cost caps.
2. Show the plan to the user and **ask before spending**. They can change `--runs`, `--max-tasks`, `--model`, `--per-run-usd`, `--budget-usd`, pass their own tasks with `--tasks file.json`, or skip probes with `--no-probes`.
3. On approval, run it with `--yes --out docs/agent-readiness/benchmark.md --raw docs/agent-readiness/benchmark.json`.
4. Summarize: median cost, turns, tool calls and tokens per task (before → after), the key-fact hit rate, and any canary marked **LEAKED**. A leak means a deny rule or hook doesn't work in a live session: fix the guardrail before anything else. Differences marked `~` are within noise.

Runs are isolated from the user's own settings, plugins and MCP servers (`--setting-sources project --strict-mcp-config`), and aren't saved as sessions. Canary files hold fake text, never real secrets.

## Eval harness (`eval`)

Measures whether agents can make real changes here, not just answer questions. Tasks are merged PRs that change code and tests; the PR's tests, hidden from the agent, grade its work.

1. **Test command.** Take the verified single-test command from the instructions or discovery and turn it into a template with `{python}` and `{tests}` (e.g. `TEST_RUN=true {python} -m pytest {tests} -q`). Add the repo's fast options when it has them: keeping the test database between runs (pytest-django's `--reuse-db`, Rails' `--keep-db`-style flags), skipping coverage, stopping at the first failure (`-x`). Only the PR's own test files run, never the whole suite. If tests can't run locally (missing packages, broken baseline), stop and say what to fix: the harness can't grade without them.
2. **Build (free):** `python3 -I ${CLAUDE_SKILL_DIR}/scripts/evals.py build . --test-cmd '<template>' [--branch <main branch>]`. It mines recent merged PRs (code + tests, no migrations, small), and keeps only those whose tests fail on the commit before the PR and pass on the merge, the same way in each of 3 runs (`--repeat`): a PR with mixed results is dropped as flaky, since it would reward or penalize the agent at random. It stops at the first decisive result, so bad PRs cost one or two runs. If a single run takes longer than `--max-test-seconds` (default 120), the PR isn't repeated: it's kept, marked "flakiness unchecked", and flagged † in the report. `--max-minutes` caps the whole build, it prints an estimate after the first PR, and PRs already validated at the same merge commit are reused (`--revalidate` to redo them). For a very slow suite, start with `--repeat 1`. Files the tests need that are secret-like (e.g. a config JSON) stay in the copy but are denied to the agent. It writes `docs/agent-readiness/evals/tasks.json`. Show the valid tasks and the skip reasons.
3. **Plan, then ask.** `python3 -I ${CLAUDE_SKILL_DIR}/scripts/evals.py run .` prints the plan and caps. Edit tasks cost more than `bench` runs; ask before spending.
4. **Run:** add `--yes --out docs/agent-readiness/evals/report.md`. Report the pass rate before vs after, per task cost and turns, and how often the agent touched the same files as the PR. Treat a handful of tasks as a smoke test, not a score.

Agents may only read, edit inside their temp copy, and run the test command. Grading executes the agent's code in the temp copy with a minimal environment; network isn't blocked, so only run it on code you'd let an agent run anyway.

