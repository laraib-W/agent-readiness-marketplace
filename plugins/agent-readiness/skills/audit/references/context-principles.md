# Context principles

Goal: the agent sees **only what's relevant to the task in front of it**. Every always-loaded line is paid on every request and dilutes the instructions that matter.

## When Claude Code loads each file

| File | Loads | Cost |
|---|---|---|
| Root `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` | Every session | Always |
| `@path` imports in those files | At launch, with the importing file (max four hops deep) | Always; imports organize, they don't save tokens |
| Root `AGENTS.md` | Every session **only when no root `CLAUDE.md`/`CLAUDE.local.md` exists**; otherwise only if `CLAUDE.md` imports it | Always (or never) |
| `.claude/rules/*.md` without `paths:` | Every session | Always |
| `.claude/rules/*.md` with `paths:` | When Claude reads/edits a matching file, or `cat`/`head`s one through Bash | Only when relevant |
| Symlink in `.claude/rules/` | Like the file it points to; a target outside the repo needs approval, and path-scoped external rules never load | Same as target |
| `CLAUDE.md` in a subdirectory | When Claude works in that subdirectory | Only when relevant |
| Skill name + description | Every turn, unless `disable-model-invocation: true` | Small but constant |
| Skill body and its reference files | When the skill runs / when it reads them | On demand |
| Subagent | Own context; only its summary returns | Keeps main context clean |
| Files referenced by path in backticks (no `@`) | Only if Claude chooses to read them | On demand |
| `<!-- HTML comments -->` in `CLAUDE.md` | Stripped before loading | Free; use for maintainer notes |
| MCP servers in `.mcp.json` | Tool definitions every session (subject to tool search) | Constant; add only with proven need |

Rule frontmatter: `paths` is the only field read. It takes a YAML list or a comma-separated string, and globs support `**` and `{a,b}`. If the YAML doesn't parse, the rule loads in **every** session.

Source: https://code.claude.com/docs/en/memory, https://code.claude.com/docs/en/skills, https://code.claude.com/docs/en/plugins

## Where content goes

| Content | Home |
|---|---|
| Facts every session needs: what the app is, repo map, core commands, the few rules that apply everywhere | Root `CLAUDE.md` |
| Rules for one area (models, API views, components, migrations, tests, infra) | `.claude/rules/<topic>.md` with `paths:` |
| Commands/conventions of one sub-app in a monorepo | That sub-app's `CLAUDE.md` **or** path-scoped rules; one pattern per folder |
| Multi-step procedures | `.claude/skills/<name>/SKILL.md`, long references in files beside it |
| Workflows a person triggers by name | Skill with `disable-model-invocation: true` |
| Focused reviews in a risk area | `.claude/agents/<name>.md`, read-only tools |
| Architecture, deep explanations, diagrams | `docs/`, referenced by path, not `@`-imported |
| Must always / never happen | Hooks and `permissions` in `.claude/settings.json` |
| Anything a linter/formatter/type-checker enforces | Nowhere |
| Shared with other AI tools (Cursor, Codex, Copilot) | `AGENTS.md`, with `CLAUDE.md` importing it (one source of truth) |
| Area rules shared with other tools | One shared file (e.g. `.ai/rules/x.md`) with `paths:` frontmatter, symlinked into `.claude/rules/` |

## Budgets (targets)

- Root `CLAUDE.md` ≤ 80 lines. Any instruction file ≤ 200 (Anthropic's guidance).
- One topic per rule file, ≤ 40 lines.
- Always-loaded total (root files + unscoped rules + imports) ≤ ~150 lines.
- Skill/agent descriptions: 1–2 sentences.
- A rule lives in exactly one file.

## Writing rules that work

- Specific and checkable: "Business logic goes in `apps/*/services.py`, not in views" beats "keep views thin".
- State the exception when there is one.
- Point to an example file instead of pasting code.
- Prefer one line per rule; group under short headings.
- Delete rules that stopped mattering.
