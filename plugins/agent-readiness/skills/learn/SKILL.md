---
name: learn
description: Turn a correction or a lessons log into the narrowest possible instruction (path-scoped rule, nested CLAUDE.md, root CLAUDE.md, hook or permission), and prune rules that no longer matter.
argument-hint: "[what went wrong, or empty to process docs/agent-readiness/lessons.md]"
disable-model-invocation: true
---

# Learn from a mistake

Input: `$ARGUMENTS`

Open `docs/agent-readiness/lessons.md` (create it with columns `date | task | what went wrong | action taken` if missing). If the input is given, append it as a new row dated today. Then process every row with an empty "action taken".

For each lesson:

1. **Confirm it's real and recurring.** Find the evidence in the code. A one-off mistake from an unclear request doesn't need a rule.
2. **Pick the narrowest home** (stop at the first that fits):
   1. A tool can enforce it → hook, permission in `.claude/settings.json`, or a lint rule. No instruction text.
   2. It only concerns certain files → `.claude/rules/<topic>.md` with `paths:` (add to an existing topic file if one fits; check the globs match real files).
   3. It concerns one sub-app that has its own `CLAUDE.md` → that file.
   4. It's a multi-step procedure → a skill (new or existing).
   5. It truly applies everywhere → root `CLAUDE.md`.
3. **Write one specific line**, pointing to an example file rather than pasting code.
4. **Check for duplicates and conflicts** across `CLAUDE.md` files, rules and `AGENTS.md`. Merge or replace instead of adding.
5. **Show the diff and wait for approval**, then apply and fill in "action taken" (or `no rule: <reason>` when step 1 rules it out, so the row isn't reprocessed).

Then prune: list rules that duplicate a tool, reference missing files, or that the user says haven't mattered recently, and propose deleting them.

End with the always-loaded instruction size before → after (lines in root `CLAUDE.md` + unscoped rules + imports).
