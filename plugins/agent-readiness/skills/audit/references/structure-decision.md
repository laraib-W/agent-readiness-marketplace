# Structure decision

Answer each question for **this** repo, with evidence. The output is a short "Structure decision" section in the report and drives the change plan.

## 1. Repo shape → instruction layout

| Shape (from scan `app_roots`, `is_monorepo`) | Layout |
|---|---|
| Single app, one toolchain | Root `CLAUDE.md` + a few path-scoped rules for risk areas |
| Monorepo, parts share conventions | Small root `CLAUDE.md` + path-scoped rules per part (`.claude/rules/backend/*.md` with `paths: ["backend/**"]`) |
| Monorepo, parts differ a lot or people often open sessions inside a part | Small root `CLAUDE.md` + `CLAUDE.md` in each part |
| Very large monorepo with team ownership | Per-part `CLAUDE.md` + path-scoped rules inside each part; root stays a map |

Pick one pattern per folder. Never both a nested `CLAUDE.md` and path-scoped rules saying the same thing.

Every active part in the scan's `parts` list needs area instructions (a scoped rule or nested file) once it has conventions worth stating; a map line in the root file is not enough. Skip parts the scan marks dormant unless the user says they're still used.

## 1b. Units inside a part (apps, features, packages)
The scan's **Units** table lists subfolders that stand out from their siblings (own libraries, own tooling, sessions started inside, much larger, or fixed much more often). For each **strong** candidate:
1. Have discovery sample 2–3 of its files and one sibling's. Write down only what differs: the distinct libraries and how they're used (client setup, where to mock them), its own folders, its own commands, its own data or failure modes. If nothing differs beyond size, write nothing and say so in the report.
2. Home, as the scan suggests:
   - **path-scoped rule** → `.claude/rules/<part>/<unit>.md` with `paths: ["<unit>/**"]`. Mirror the collection in the rules folder (`.claude/rules/backend/jobs.md`), so one app's rules sit together. 3–10 lines; don't repeat the collection-wide rule.
   - **nested `CLAUDE.md`/`AGENTS.md`** (own tooling, or people start sessions there) → a file inside the unit with its commands and layout. Use `AGENTS.md` if the team uses other tools too.
   - If the team keeps shared rules elsewhere (e.g. `.ai/rules/`), put the file there and symlink it, as in §6.
3. **Weak** candidates (one signal): no file. List them in the report as hotspots for `/agent-readiness:learn`.
4. Cap a first pass at about five unit files; more come from the feedback loop.

## 2. What goes in the root `CLAUDE.md`
Only: one-paragraph summary, repo map, verified core commands, rules that apply everywhere, risk zones as one-liners pointing to their rule/agent, and pointers (backticks, not `@`) to deeper docs. Target ≤ 80 lines.

## 3. Which rule files
One per topic that has evidence: a risk zone, a recurring mistake, or a convention seen in ≥ 2 places that a linter doesn't enforce. For each: filename, `paths:` globs (check they match real files), 3–10 rules.

## 4. Skills, commands, agents
Propose one only if you can name the concrete repo workflow it serves and the files it uses.
- Multi-step procedure seen in the repo (e.g. adding an endpoint touches url, view, serializer, test, frontend client) → skill.
- Manual, person-triggered workflow → skill with `disable-model-invocation: true`.
- Risk area that needs a second look (migrations, permissions, personal data, API contract between parts) → read-only reviewer agent.
Default for a first pass: zero to two of these. More can come from the feedback loop.

## 5. Where `.claude/` lives
Repo-root `.claude/` for project config. Generic, cross-repo helpers belong in a shared plugin, not copied into each repo.

## 6. Other AI tools
- `AGENTS.md` exists and is accurate → keep it as the shared source. Claude reads it natively when there's no root `CLAUDE.md`. **Creating a `CLAUDE.md` hides `AGENTS.md` from Claude** unless `CLAUDE.md` imports it, so any new `CLAUDE.md` starts with `@AGENTS.md` and adds only Claude-specific content.
- The team already shares area rules in one place (an `.ai/` folder, Cursor rules pointing to files) → don't copy them. Add `paths:` frontmatter to the shared file and symlink it into `.claude/rules/`, so there is still one copy.
- Skills whose body only says "read and follow X" are rules in disguise: they load only if Claude decides to invoke them and cost description context every turn. Replace them with path-scoped rules.
- Cursor/Copilot/Windsurf rules exist → don't delete. Note overlaps and propose moving shared content to `AGENTS.md` if the team uses several tools; ask the user.
- Only Claude is used → plain `CLAUDE.md`.

## 7. Code graphs / indexing / extra MCP servers
Default **no**. Claude Code finds code by searching on demand, which keeps context small. An indexing or graph MCP server adds tool definitions to every session. Propose one only with evidence (very large repo where discovery was slow in step 2), and tell the user to compare `/context` before and after. A Mermaid diagram in `docs/architecture.md` (referenced by path) is a cheap alternative for humans.

## 8. Guardrails
Always propose `.claude/settings.json` per `guardrails.md`, tailored to the secret files, migration tools and deploy scripts actually found.

## 9. Hooks
Propose a post-edit format/lint hook only if the formatter/linter exists and runs on a single file quickly. Scope it to the part of the repo it applies to.
