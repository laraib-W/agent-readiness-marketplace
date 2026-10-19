---
name: learn
description: Turn a correction or a lessons log into the narrowest possible instruction (path-scoped rule, nested CLAUDE.md, root CLAUDE.md, hook or permission), and prune rules that no longer matter.
argument-hint: "[what went wrong, or empty to process docs/agent-readiness/lessons.md]"
disable-model-invocation: true
allowed-tools: Bash(python3 -I ${CLAUDE_SKILL_DIR}/../audit/scripts/scan.py *)
---

# Learn from a mistake

Input: `$ARGUMENTS`

Open `docs/agent-readiness/lessons.md` (create it with columns `date | task | what went wrong | action taken` if missing). If the input is given, append it as a new row dated today. Then process every row with an empty "action taken".

The goal is a better instruction set, not a longer one. Each lesson should usually **change** a line that already exists; a new line or file is the exception.

For each lesson:

1. **Confirm it's real and recurring.** Find the evidence in the code: the files where the mistake happened or would happen. A one-off mistake from an unclear request doesn't need a rule.
2. **Find what already covers those files.** Run:
   ```
   python3 -I ${CLAUDE_SKILL_DIR}/../audit/scripts/scan.py . --covering <evidence files>
   ```
   It lists, most specific first, every instruction file Claude loads for those files, where to edit it (the shared source when a rule is a symlink) and its room under the line budget. Read each listed file before deciding.
3. **Pick the home** (stop at the first that fits):
   1. A tool can enforce it → hook or permission in `.claude/settings.json`. If only a lint rule can enforce it, recommend the rule to the user (lint config is outside this plugin's scope) and keep a one-line instruction until it lands.
   2. A listed file already has a line on the same subject (same layer, path, helper or identifier) → **rewrite that line**: sharpen it, add the exception, or point to the example file. Don't add a second line next to it.
   3. The most specific listed file has room → add one line there. An app's own file comes before a scoped rule, a scoped rule before the root file.
   4. The scan says nothing scoped covers the path → a new `.claude/rules/<topic>.md` with `paths:` (check the globs match real files with `--covering`). If the team keeps shared rules elsewhere (e.g. `.ai/rules/` linked into `.claude/rules/`), create it there and link it.
   5. It's a multi-step procedure → a skill (new or existing).
   6. It truly applies everywhere → the root file.

   **Never append to a file that's full** (room ≤ 0). Consolidate it in the same change so it ends within budget: merge overlapping lines, drop lines a tool already enforces or that point to code that's gone, or split it by topic into scoped files.
4. **Write one specific line**, pointing to an example file rather than pasting code.
5. **Check for duplicates and conflicts** against every file the scan listed, not only the one you're editing. A line elsewhere about the same path or identifier means merge or replace, never repeat.
6. **Show the diff and wait for approval**, then apply (instruction files and `.claude/settings.json` only; never commit) and fill in "action taken" (or `no rule: <reason>` when step 1 rules it out, so the row isn't reprocessed).

Then prune: list rules that duplicate a tool, reference missing files, or that the user says haven't mattered recently, and propose deleting them.

End with, for each file you changed, its lines before → after, and the always-loaded instruction size before → after (lines in root `CLAUDE.md` + unscoped rules + imports).
