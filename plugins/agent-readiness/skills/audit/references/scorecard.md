# Readiness scorecard

Score each criterion **0 (missing) / 1 (partial) / 2 (good)** with evidence (file path or scan finding) and a one-line fix. Mark a criterion **N/A** only when it truly doesn't apply (e.g. no frontend → no frontend type-check) and exclude it from the total.

## Core criteria (weight 2)

| # | Criterion | 2 = good |
|---|---|---|
| C1 | Agent instructions | Root `CLAUDE.md` (or `AGENTS.md` + import) exists, accurate, within budget |
| C2 | Context efficiency | Always-loaded ≤ ~150 lines; area-specific rules path-scoped; no duplicates; no large imports; every active part and strong unit has scoped instructions or a recorded reason not to |
| C3 | Instruction accuracy | No stale commands/paths; nothing contradicts the code (scan + auditor) |
| C4 | Setup | Documented, copy-pasteable; `.env.example` lists every required variable |
| C5 | Run | Each app part starts with one documented command |
| C6 | Tests | Run locally without external services; single test runnable by path. **Run them** (a sample per part): a suite that mostly fails on a clean checkout scores 0 for that part |
| C7 | Secrets safety | Secret files gitignored; `settings.json` denies reading them |
| C8 | Dangerous actions | Migrations, deploys, data deletion, destructive git require approval |
| C9 | Fast feedback | Lint/format (and type-check where used) runnable per file/part; hook runs them after edits |

## Supporting criteria (weight 1)

| # | Criterion | 2 = good |
|---|---|---|
| S1 | Lint / format configured | Config files present and used in CI |
| S2 | Type checking | Configured where the language supports it |
| S3 | CI | Runs lint + tests for **every** part; same commands work locally. A part whose tests CI never runs scores at most 1 |
| S4 | Local data | Seed data / factories; no production data needed |
| S5 | Risk-zone tests | Auth, permissions, core flows have tests |
| S6 | Docs | README and architecture docs match the code |
| S7 | Conventions | Consistent enough to state as rules |
| S8 | Pre-commit hooks | Husky / Lefthook / pre-commit configured |
| S9 | Repo hygiene | PR template, CODEOWNERS |
| S10 | Dev environment | Devcontainer or Docker Compose for local setup |
| S11 | Security automation | Dependency updates and secret scanning enabled |

S8–S11 start from the scan's `Hygiene` section. GitHub secret scanning is a repo setting, not a file: if no file shows it, mark that part `UNVERIFIED` rather than 0.

**Total** = Σ(core × 2) + Σ(supporting × 1), shown as `x / max` and a percentage.

## Maturity level

Adapted from Factory's Agent Readiness Model. A level is reached only when all of its gates pass (score 2 unless noted).

| Level | Name | Gates |
|---|---|---|
| 0 | Below level 1 | Any level-1 gate fails. Report it as "below level 1", naming the failing gate |
| 1 | Functional | C5, C6 ≥ 1, S1 ≥ 1 |
| 2 | Documented | Level 1 + C1, C4, S6 ≥ 1 |
| 3 | Standardized | Level 2 + C3, C7, C8, S3 |
| 4 | Optimized | Level 3 + C2, C9, S5 ≥ 1 |
| 5 | Autonomous | Level 4 + an active feedback loop (`docs/agent-readiness/lessons.md` updated in the last 30 days) and a scheduled re-audit (`scan.py --fail-on high` in CI, or a scheduled routine running the audit) |

## Roadmap to the next levels

Levels are gates, so a decent score can still sit at a low level. Always add a roadmap: for the current level + 1 and + 2, list each failing gate with its current score, the concrete fix (file, command or setting), effort (S/M/L), and who can do it. *Plugin* fixes are the ones inside its scope (instruction files, `.claude/settings.json`, `docs/agent-readiness/`); the audit can apply them. Everything else is *team*: code, build files, CI, README, `.gitignore`, credentials, owners, repo settings. Give the team a concrete recommendation (the exact target, step or line), but never apply it. Then add the remaining non-gate points by gain ÷ effort. Show the projected score and level after the plugin fixes, after the team's quick wins, and after everything. If plugin fixes alone can't lift the level, say so plainly.

## Top fixes

Rank failing criteria by impact ÷ effort. Weight safety (C7, C8) and accuracy (C3) above everything else: a wrong or unsafe setup is worse than a missing one.
