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
- `AGENTS.md` exists and is accurate → keep it as the shared source; root `CLAUDE.md` imports it with `@AGENTS.md` and adds only Claude-specific content.
- Cursor/Copilot/Windsurf rules exist → don't delete. Note overlaps and propose moving shared content to `AGENTS.md` if the team uses several tools; ask the user.
- Only Claude is used → plain `CLAUDE.md`.

## 7. Code graphs / indexing / extra MCP servers
Default **no**. Claude Code finds code by searching on demand, which keeps context small. An indexing or graph MCP server adds tool definitions to every session. Propose one only with evidence (very large repo where discovery was slow in step 2), and tell the user to compare `/context` before and after. A Mermaid diagram in `docs/architecture.md` (referenced by path) is a cheap alternative for humans.

## 8. Guardrails
Always propose `.claude/settings.json` per `guardrails.md`, tailored to the secret files, migration tools and deploy scripts actually found.

## 9. Hooks
Propose a post-edit format/lint hook only if the formatter/linter exists and runs on a single file quickly. Scope it to the part of the repo it applies to.
