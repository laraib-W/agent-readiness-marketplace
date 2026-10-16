# Auditing existing instruction files

When AI files already exist, the job is mostly **optimization**: keep what's right, fix what's wrong, move what's misplaced, remove what's noise. Never rewrite from scratch when targeted edits will do.

## Classify every line or block

| Class | Test | Action |
|---|---|---|
| Keep | Correct, specific, needed every session | Keep in place |
| Scope | Correct but only relevant to part of the code | Move to a path-scoped rule or nested `CLAUDE.md` |
| Extract | A multi-step procedure | Move to a skill |
| Externalize | Long explanation/architecture | Move to `docs/`, leave a one-line pointer |
| Fix | Wrong: stale command, missing path, contradicts code | Correct with evidence |
| Dedupe | Same rule in several files | Keep one copy in the narrowest correct home |
| Enforce | "Always/never" rule a tool can enforce | Convert to hook/permission/lint rule, then delete text |
| Delete | Generic ("write clean code"), linter-enforced, obsolete | Remove |
| Ask | Can't tell if still true | List under questions for the team |

## Common problems to look for

- Large `@imports` of README/architecture docs loaded every session.
- Unscoped `.claude/rules/` files that only concern one area.
- `paths:` globs that match nothing (rule never loads).
- Commands that don't exist (`npm run x` with no such script, removed make targets).
- References to moved/deleted files.
- Conflicts between `CLAUDE.md`, nested files, rules and `AGENTS.md`.
- Skill/agent descriptions that are long, vague, or overlap so Claude picks the wrong one.
- Agents with more tools than they need (reviewers should be read-only).
- Settings that allow broad Bash (`Bash(*)`) or don't deny secret files.
- A root `CLAUDE.md` next to an `AGENTS.md` it doesn't import: Claude never sees `AGENTS.md`.
- Skills used as rules (body is just a pointer to a rules file).
- Instruction files that aren't committed: they only help the person who wrote them.
- README commands that no longer exist (e.g. a documented `make` target with no recipe). Agents copy them. Keep them out of instruction files, and recommend the README/Makefile fix in the report (outside the plugin's scope).

## Report format

For each existing instruction file: path, loads (always / on demand), lines, and a table of findings: class | excerpt (≤ 1 line) | evidence | proposed action. End with the always-loaded line count now and after the proposed actions.

## Respect ownership

These files were written by people on the team. Preserve their wording where it's still correct, show diffs, and explain each removal in one line.
