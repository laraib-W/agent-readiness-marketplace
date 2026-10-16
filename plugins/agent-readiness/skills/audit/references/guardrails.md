# Guardrails (`.claude/settings.json`)

Tailor to what the scan and discovery actually found. If the file exists, merge; never drop the team's existing entries.

## Deny (never allowed)

Reading secrets and local data, for every pattern that applies to this repo:

```json
"deny": [
  "Read(./.env)",
  "Read(./.env.*)",
  "Read(./**/.env)",
  "Read(./**/.env.*)",
  "Read(./**/*.pem)",
  "Read(./**/*.key)",
  "Read(./**/credentials*.json)",
  "Read(./**/secrets.*)"
]
```

Add repo-specific paths: data dumps, `*.sqlite3`, fixture folders with real personal data, cloud credential folders. `.env.example` stays readable; if a deny pattern would catch it, use more specific patterns.

`Read()` deny rules govern Claude's file tools, not shell commands: `cat .env` through Bash isn't blocked by them. For real protection also enable sandboxing (`"sandbox": {"enabled": true}`) or add a `PreToolUse` hook on `Bash` that rejects commands touching the denied paths. Verify the current behaviour at https://code.claude.com/docs/en/permissions and say which option you chose in the report.

## Ask (approval each time)

Only commands that exist in this repo, for example:
- Migrations: `Bash(python manage.py migrate*)`, `Bash(alembic upgrade*)`, `Bash(npx prisma migrate*)`
- Deploys and infra: deploy scripts, `Bash(terraform apply*)`, `Bash(kubectl *)`
- Destructive: `Bash(git push --force*)`, `Bash(git reset --hard*)`, `Bash(docker compose down -v*)`, `Bash(rm -rf *)`

## Allow (no prompt)

Only verified, safe, frequent commands: the test, lint, format and type-check commands from the report. Use the narrowest pattern (e.g. `Bash(pytest *)`, `Bash(npm run lint*)`), never `Bash(*)`.

## Hooks (optional)

A `PostToolUse` hook on `Edit|Write` that formats the edited file, only when the formatter runs on a single file in about a second. Example shape:

```json
"hooks": {
  "PostToolUse": [
    { "matcher": "Edit|Write",
      "hooks": [ { "type": "command", "command": "<repo's format-one-file command>" } ] }
  ]
}
```

Verify the exact hook input format and command against https://code.claude.com/docs/en/hooks before writing it.

Explain each rule in one line in the report so the team can review it.
