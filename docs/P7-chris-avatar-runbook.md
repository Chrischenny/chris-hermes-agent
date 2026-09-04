# P7 `chris-avatar` rollout and rollback

## Deployment record — 2026-09-04

- User-approved Policy: `gpt-5.6-sol` ratio Handoff `0.70`, Emergency enabled at
  `0.85`.
- Runtime Context Limit: 272,000; resolved thresholds: 190,400 and 231,200 Token.
- Current installed immutable Commit:
  `27835c11c5d4847482fc6eb71336009488f43610` (plugin `0.7.7`). This supersedes
  0.7.6 with fail-closed Session/Task ownership, bounded pre-Handoff and corrupt-state
  selection, persisted Segment message anchors, and archive format v2 conversation-prefix
  validation. Legacy archives are diagnostic evidence only and are never restored.
- Protected pre-rollout backup:
  `/home/chen/hermes-rollout-backups/chris-avatar-0.7.7-20260904T041528Z`. In addition
  to the normal Profile snapshot, this directory contains an online backup of the plugin
  database and a permission-preserving copy of all Emergency archives made before schema
  migration.
- `context.engine=context-handoff`; SOUL migration and installed-path Doctor passed.
- `hermes-gateway-chris-avatar.service` and the Desktop `hermes-dashboard.service`
  restarted at 2026-09-04 12:17 CST and remained active/running with `NRestarts=0`; the serve process listened
  on port 9119 and `/api/health` returned HTTP 200.
- Current Hermes Python links SQLite 3.50.4, so the plugin intentionally selected
  `journal_mode=DELETE`; plugin data directory/database permissions are `0700/0600`.
- The live database migrated from schema v1 to v2, added
  `context_segments.start_message_checksum`, and passed `PRAGMA integrity_check` and
  `foreign_key_check`. An installed-path read-only replay of Session
  `20260902_202600_d62df5` reduced 1,373 canonical messages to 18 selected messages
  (roughly 19,322 Token), retained its Checkpoint Bootstrap, excluded the unrelated active
  Task, and did not restore a legacy archive. Retain the backup, plugin database, archives,
  timestamps, and Session ID if an issue appears. The deployment account still cannot read
  the user journal, so service-log inspection is not claimed as completed.

This runbook is the live boundary for P7. The isolated test suite may run at any time, but
do not execute the backup, install, Profile mutation, Gateway restart, or rollback commands
against `chris-avatar` until the user has confirmed both policy values and explicitly
authorized the rollout.

## Required confirmations

Record all four values in the rollout evidence before changing the Profile:

- immutable 40-character plugin commit SHA;
- `gpt-5.6-sol` Handoff threshold type and value;
- whether Emergency Fallback is enabled and, if enabled, its type and threshold;
- explicit authorization to mutate `chris-avatar` and restart its Gateway.

The observed model Context Limit is a preflight fact, not a plugin default. Re-check it at
rollout time. The configured Emergency threshold must be above the Handoff sweet-zone start
and both must be below the current Context Limit.

## Preflight

Run these read-only checks and retain their output in the rollout evidence:

```bash
git -C "$HOME/.hermes/hermes-agent" rev-parse HEAD
hermes --version
systemctl --user show hermes-gateway-chris-avatar.service \
  --property=ActiveState --property=MainPID --property=ExecMainStartTimestamp
hermes -p chris-avatar config path
hermes -p chris-avatar config check
hermes -p chris-avatar sessions stats
```

In the plugin checkout, verify the selected release candidate and rerun the isolated gate:

```bash
git status --short
git rev-parse HEAD
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy chris_hermes_agent
uv run pytest --cov=chris_hermes_agent --cov-report=term-missing
uv build
```

## Backup

The helper uses SQLite's online backup API, records the Gateway/Hermes identity and current
session-file index, writes checksums, and restricts the backup to owner access. The backup
contains sensitive Profile material and must not be committed or shared.

```bash
P7_BACKUP_DIR="$HOME/hermes-rollout-backups/chris-avatar-$(date -u +%Y%m%dT%H%M%SZ)"
./scripts/chris-avatar-rollout.sh backup "$P7_BACKUP_DIR"
```

Do not proceed unless `SHA256SUMS`, `config.yaml`, `SOUL.md`, `state.db`,
`session-index.txt`, and `metadata.txt` exist in that directory.

## Install and configure

Use the final release-candidate SHA, not a branch or tag:

```bash
PLUGIN_COMMIT='<confirmed-40-character-commit-sha>'
hermes -p chris-avatar plugins install Chrischenny/chris-hermes-agent \
  --ref "$PLUGIN_COMMIT" --no-enable
hermes -p chris-avatar plugins doctor chris-hermes-agent --ci
```

Construct the `handoff` JSON only from the user-confirmed values. This is the shape for
absolute-token policies; use the documented `ratio` shape instead if the user chose ratios.
Replace every symbolic placeholder before producing live configuration.

```jsonc
{
  "model_policies": {
    "gpt-5.6-sol": {
      "handoff_enabled": true,
      "sweet_zone": {
        "type": "absolute_tokens",
        "start": CONFIRMED_HANDOFF_START
      },
      "emergency": {
        "enabled": CONFIRMED_EMERGENCY_ENABLED,
        "type": "absolute_tokens",
        "threshold": CONFIRMED_EMERGENCY_THRESHOLD
      }
    }
  }
}
```

Apply the confirmed single-line JSON and enable without built-in tool overrides:

```bash
HANDOFF_POLICY_JSON='<confirmed-single-line-json>'
hermes -p chris-avatar config set --force \
  plugins.entries.chris-hermes-agent.settings.handoff "$HANDOFF_POLICY_JSON"
hermes -p chris-avatar plugins enable chris-hermes-agent --no-allow-tool-override
hermes -p chris-avatar config set --force context.engine context-handoff
hermes -p chris-avatar config check
hermes -p chris-avatar plugins doctor chris-hermes-agent --ci
```

Replace the existing fixed-model/fixed-token long-task rule in `SOUL.md` with
[`soul/SOUL-snippet.md`](../soul/SOUL-snippet.md). Keep unrelated user content unchanged.
Review the diff and confirm that no model name, fixed threshold, or forced Session restart
remains in the migrated long-task section.

## Restart and observation

Restart only after configuration, installed-plugin Doctor, and SOUL review pass:

```bash
systemctl --user restart hermes-gateway-chris-avatar.service
systemctl --user is-active hermes-gateway-chris-avatar.service
journalctl --user -u hermes-gateway-chris-avatar.service --since '-5 minutes' \
  --no-pager
```

Gateway and `hermes serve` are separate host processes. If Desktop or WebUI reaches this
Profile through a long-running serve process, restart that service too so live Agent
instances rebuild their System Prompt, tools, Skill registry, and ContextEngine snapshot:

```bash
systemctl --user restart hermes-dashboard.service
systemctl --user is-active hermes-dashboard.service
```

Use a new, isolated Session for the live test. Verify a long Tool Loop, a stable-boundary
Checkpoint/Handoff, continued work in the same Turn, old Tool Trace exclusion, and a second
Gateway restart. Do not deliberately force Emergency by fabricating usage. If real request
pressure reaches it, verify the runtime-reported state and then complete a normal
Checkpoint/Handoff at the next stable boundary.

Cross-check the plugin database without printing Checkpoint or Event payloads:

```bash
PLUGIN_DB="$HOME/.hermes/profiles/chris-avatar/plugin-data/chris-hermes-agent/data.db"
sqlite3 "$PLUGIN_DB" 'PRAGMA integrity_check;'
sqlite3 "$PLUGIN_DB" \
  'SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status;'
sqlite3 "$PLUGIN_DB" \
  'SELECT event_type, COUNT(*) FROM task_events GROUP BY event_type ORDER BY event_type;'
sqlite3 "$PLUGIN_DB" \
  'SELECT COUNT(*) AS segment_count FROM context_segments;'
```

Acceptance requires clean Gateway logs, an intact Hermes Session, current Runtime Status,
traceable Task/Checkpoint/Segment/Event identities, and successful continuation after
restart. Keep the backup through the observation window.

## One-command rollback

From the same verified checkout, the following command validates the backup, stops the
Gateway, disables the plugin, restores original config/SOUL/Session state, integrity-checks
the restored database, and starts the Gateway:

```bash
./scripts/chris-avatar-rollout.sh rollback "$P7_BACKUP_DIR"
```

Then verify:

```bash
systemctl --user is-active hermes-gateway-chris-avatar.service
hermes -p chris-avatar config check
hermes -p chris-avatar sessions stats
```

If a Desktop/WebUI serve process was restarted for rollout, restart it after rollback as
well; otherwise it may retain the rolled-back plugin and prompt snapshot in memory.

Rollback intentionally preserves
`plugin-data/chris-hermes-agent/data.db` and Emergency archives as diagnostic evidence. It
does not uninstall the plugin or delete any archive.

## Archive retention

- During rollout and observation, retain the Profile backup, plugin SQLite database, and
  every Emergency archive without modification.
- After acceptance, keep the pre-rollout backup and Emergency evidence for at least 30 days.
- Cleanup is manual and requires a separate user decision. Before removal, verify archive
  checksums and their Segment/Event references, make a second protected backup, and operate
  only on explicitly selected closed Segments.
- Never automate cleanup as part of install, restart, acceptance, or rollback.
