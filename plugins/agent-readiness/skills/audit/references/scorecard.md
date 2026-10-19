# Readiness scorecard

Three sub-scores, because they answer different questions and have different owners:

| Sub-score | Question | Who can fix it |
|---|---|---|
| **A. Agent context** | Does the agent know the repo, see only what's relevant, and stay inside guardrails? | This plugin (instruction files, `.claude/settings.json`) |
| **E. Agent environment** | Can the agent set up, run and check its own work? | The team (code, build, CI) |
| **H. Repo hygiene** | General good practice. It helps humans review agent work but isn't agent-specific | The team |

Score each criterion **0 (missing) / 1 (partial) / 2 (good)** with evidence (file path or scan finding) and a one-line fix. Mark a criterion **N/A** only when it truly doesn't apply (e.g. no frontend → no frontend type-check) and exclude it from that sub-score. Report each sub-score as `x / max` and a percentage. Don't add them into one total: a single number hides that context can be complete while the environment is broken.

## A. Agent context

| # | Criterion | 2 = good |
|---|---|---|
| A1 | Instructions | Root `CLAUDE.md` (or `AGENTS.md` + import) exists, is accurate and within budget, and is **committed**. Uncommitted files score at most 1: only one machine benefits |
| A2 | Relevant context only | Always-loaded ≤ ~150 lines; area rules path-scoped; no duplicates or large imports; every active part and strong unit has scoped instructions or a recorded reason not to |
| A3 | Accuracy | No stale commands or paths; every command in instructions was run and its output checked; nothing contradicts the code |
| A4 | Secrets out of reach | Every secret, credential-config and personal-data path found is denied to Claude: `Read()` deny rules **and** a Bash hook or sandbox (deny rules don't cover `cat`) |
| A5 | Dangerous actions | Migrations, deploys, data deletion and destructive git need approval (`ask`) |
| A6 | Feedback after edits | A hook formats/lints the edited file, using commands verified to work |

## E. Agent environment

| # | Criterion | 2 = good |
|---|---|---|
| E1 | Setup | A fresh checkout gets to a working dev setup by copy-paste or one command (script, devcontainer or Compose); config templates list every variable; no hand edits to code |
| E2 | Run | Each part starts with one documented command |
| E3 | Tests | Run locally without external services; one test runnable by path; **run a sample per part**: a suite that mostly fails on a clean checkout scores 0 for that part |
| E4 | Risk-zone tests | Auth, permissions, tenancy and core flows have tests |
| E5 | Lint / format | Runs per file and **works** (output checked: one error repeated on every file means it's broken) |
| E6 | Type checking | Configured where the language supports it |
| E7 | CI | Runs lint and tests for **every** part with the same commands as local. A part whose tests CI never runs scores at most 1 |
| E8 | Local data | Seed data or factories; no production data or dumps needed |

## H. Repo hygiene

| # | Criterion | 2 = good |
|---|---|---|
| H1 | Secrets in git | Secret and data files gitignored; none committed |
| H2 | Docs | README and architecture docs match the code |
| H3 | Conventions | Consistent enough to state as rules |
| H4 | Pre-commit | Husky / Lefthook / pre-commit configured |
| H5 | Review routing | PR template, CODEOWNERS |
| H6 | Security automation | Dependency updates and secret scanning enabled |

E1 and E5–E7 start from the scan's **Environment per part** table: lockfile, pinned runtime and setup command (E1), linter and formatter (E5), type checker (E6), and which CI workflow runs tests and lint for each part (E7). The table shows what's configured; still run the commands before scoring 2. H2–H6 start from the **Repo files** section (license, contributing guide, security policy, editorconfig, architecture docs, pre-commit, CODEOWNERS, PR template, dependency updates, secret scanning). GitHub secret scanning is a repo setting, not a file: if no file shows it, mark that part `UNVERIFIED` rather than 0.

## Maturity level

Adapted from Factory's Agent Readiness Model. Gates use **context and environment only**: hygiene is advice and never holds the level down. A level is reached only when all of its gates pass (score 2 unless noted).

| Level | Name | Gates |
|---|---|---|
| 0 | Below level 1 | Any level-1 gate fails. Report it as "below level 1", naming the failing gate |
| 1 | Functional | E2, E3 ≥ 1, E5 ≥ 1 |
| 2 | Documented | Level 1 + A1, A3 ≥ 1, E1 |
| 3 | Standardized | Level 2 + A3, A4, A5, E7 |
| 4 | Optimized | Level 3 + A2, A6, E3, E4 ≥ 1 |
| 5 | Autonomous | Level 4 + an active feedback loop (`docs/agent-readiness/lessons.md` updated in the last 30 days) and a scheduled re-audit (`scan.py --fail-on high` in CI, or a scheduled routine running the audit) |

## Roadmap to the next levels

Levels are gates, so good sub-scores can still sit at a low level. Always add a roadmap: for the current level + 1 and + 2, list each failing gate with its current score, the concrete fix (file, command or setting), effort (S/M/L), and who can do it. *Plugin* fixes are the ones inside its scope (instruction files, `.claude/settings.json`, `docs/agent-readiness/`); the audit can apply them. Everything else is *team*: code, build files, CI, README, `.gitignore`, credentials, owners, repo settings. Give the team a concrete recommendation (the exact target, step or line), but never apply it. Then add the remaining points per sub-score by gain ÷ effort. Show the projected sub-scores and level after the plugin fixes, after the team's quick wins, and after everything. If plugin fixes alone can't lift the level, say so plainly.

## Top fixes

Rank failing criteria by impact ÷ effort. Weight safety (A4, A5, H1) and accuracy (A3) above everything else: a wrong or unsafe setup is worse than a missing one. Within the rest, environment (E) comes before hygiene (H), because it decides whether the agent can check its own work.
