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

Add repo-specific paths: data dumps, `*.sqlite3`, fixture folders with real personal data, cloud credential folders, folders the code writes exports or uploads to, and **config files the settings loader reads credentials from** (e.g. `settings/*_config.json` picked by an env var). Find those by reading the loader, not the files. `.env.example` stays readable; if a deny pattern would catch it, use more specific patterns.

`Read()` deny rules govern Claude's file tools, not shell commands: `cat .env` through Bash isn't blocked by them. For real protection also enable sandboxing (`"sandbox": {"enabled": true}`) or add a `PreToolUse` hook on `Bash` that rejects commands touching the denied paths. Verify the current behaviour at https://code.claude.com/docs/en/permissions and say which option you chose in the report.

Hook shape (the hook gets the tool call as JSON on stdin; exit 2 blocks it and shows stderr to Claude). Put this repo's denied patterns in the regex:

```json
"PreToolUse": [
  { "matcher": "Bash",
    "hooks": [ { "type": "command",
      "command": "grep -qE '<pattern>|<pattern>' && { echo 'Blocked: touches a secret or data path' >&2; exit 2; } || exit 0" } ] }
]
```

Before writing it, pipe a few sample payloads (`{"tool_name":"Bash","tool_input":{"command":"..."}}`) through the command: denied paths must exit 2, and the repo's normal test/lint commands must exit 0.

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

`.gitignore` is outside the plugin's scope. If secret or data files aren't ignored, deny reading them here and **recommend** the `.gitignore` lines in the report.
