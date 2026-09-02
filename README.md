# chris-hermes-agent

Hermes Native Plugin for agent-managed Task State, Checkpoints, and Context
Rotation during long-running work.

P7 plugin `0.7.5` is deployed to `chris-avatar`; installed-path Doctor, service
restart, health, and database integrity checks passed. Isolated integration, real
Hermes Host/install, restart, 10-rotation, and rollback tests are also complete. The
first Handoff/Emergency under a real developer
workload remains under normal operational observation. The plugin persists
profile-scoped Task State, exposes an atomic `handoff_context`
tool, and ships the Agent workflow that classifies task boundaries, creates
quality-checked Checkpoints, and selects an isolated Context without ending the
current Agent Turn. A model policy may now explicitly enable a last-resort,
request-only Emergency Compression path without rewriting canonical Session
history.

## Agent workflow

The bundled `chris-hermes-agent:context-handoff` Skill now defines the complete
operating sequence for current-task continuation, subtasks, independent new
tasks, paused-task resume, ordinary Handoff decisions, and failure recovery.
Low-confidence classification must be confirmed before a state-changing tool
call. Read-only Task inspection and search remain available while waiting for
that choice.

Checkpoint guidance includes all runtime-required fields, a semantic quality
self-check, Checksum/result validation, and the exact Handoff preflight.
`rejected_alternatives` is limited to viable approaches actually evaluated and
declined with a rationale; constraints, accepted decisions, unresolved issues,
and generic prohibitions remain in their own fields. The
companion [`SOUL-snippet.md`](./soul/SOUL-snippet.md) references only the
current Runtime Policy and contains no fixed model, Token, or ratio threshold.
It is also the source of the rules now migrated into `chris-avatar`.

## Context rotation

`handoff_context` validates the Checkpoint owner and checksum together with the
caller's expected active Task/Segment. One SQLite transaction closes the old
Segment, creates the new Segment, advances the Session pointer, and appends a
`HANDOFF_COMPLETED` event. Concurrent and repeated calls fail closed instead of
creating duplicate Segments or events.

The following Provider Request keeps Hermes' assembled stable head, inserts a
durable Task/Checkpoint Bootstrap, and retains only messages from the handoff
assistant Tool Call onward. This preserves the triggering Tool Call/Result pair
and subsequent Tool Loop while excluding the old Segment's bulk Tool Trace.
Hermes Session history is not modified or deleted. A corrupt Checkpoint or
invalid persisted cursor produces an isolated diagnostic Bootstrap rather than
silently restoring archived trace.

When a Task becomes paused, blocked, completed, or cancelled, its active Task
and Segment pointers are still cleared. The next request in that same Session
uses its newest Segment as a read-only recovery cursor when that Segment is
checkpoint-backed, instead of falling back to the complete canonical history. It
never skips a newer uncheckpointed Task to recover an older Task's cursor. A newly
activated Task always takes precedence over this inactive recovery selection.

## Runtime observation

`ContextHandoffEngine.select_context()` preserves the assembled request prefix
and appends one ephemeral `user` message at the tail. It uses Hermes'
`estimate_messages_tokens_rough()` for the current message estimate and never
writes Runtime Status into conversation history, the Session database, or the
plugin Event Log. Repeated selection replaces a prior generated tail status so
a request contains at most one current status.

`update_from_response()` normalizes both Hermes legacy and canonical Usage
fields. Missing Usage is shown as `unavailable`; it is not represented as a
real zero measurement. Model switches immediately re-resolve and display the
new policy and Context Limit. Unmatched or invalid policies remain
observation-only and never gain an inferred threshold.

## Policy configuration

Settings live under `plugins.entries.chris-hermes-agent.settings.handoff`.
Resolution order is exact model name, longest matching model-name substring,
provider, then `default_policy`. Thresholds support `ratio` and
`absolute_tokens`; enabled thresholds must be below the active model's context
limit, and Emergency must be above the Handoff sweet-zone start.

The safe initial configuration is observation-only:

```yaml
plugins:
  entries:
    chris-hermes-agent:
      settings:
        handoff:
          default_policy:
            handoff_enabled: false
            emergency_enabled: false
```

No model threshold is supplied by the plugin. Invalid or unmatched policies
produce diagnostic state and keep Handoff and Emergency behavior disabled.

The manifest intentionally remains at version 1 for compatibility with the
Hermes v0.20.5 installer. The plugin retains additive `api_version` and
`config_schema` fields, and P7 exercises the standard installer plus installed-path
runtime discovery in a temporary Profile.

## Emergency fallback

The host compression threshold is sourced only from the resolved, explicitly
enabled Emergency policy. At that boundary the plugin archives the complete
active Provider Request, records a Triggered event, and delegates summarization
to Hermes' `ContextCompressor`. It independently re-estimates the returned
request and accepts it only when it is below the configured Emergency threshold.

The compressed selection is request-local. `compress()` returns the original
canonical conversation unchanged, while the next `select_context()` serves the
verified compressed request plus messages added after the archive anchor.
Completed state is restored from the archive after a process restart. Delegate,
archive, corrupt-state, no-progress, and still-over-threshold failures are
fail-closed and surfaced with safe codes in Runtime Status.

Archives use random names under the Profile plugin data `archives/` directory,
with directory/file permissions `0700`/`0600`, a content checksum, and
Triggered/Completed/Failed events that contain metadata only. Active Context or
provider error text is never copied into the Event Log.

## Task lifecycle

`task_state_manage` supports `create`, `get`, `update`, `pause`, `search`,
`list`, `resume`, `block`, `complete`, and `cancel`. Starting a new task in a
busy Session requires a valid Checkpoint and pauses the unfinished task by
default. Task search covers state, selected decision events, Checkpoints,
artifacts, aliases, tags, and Next Actions without indexing bulk Tool Trace.

Its nested `state` Schema explicitly exposes the Task fields, including
`in_progress`. `checkpoint_create.checkpoint` exposes a separate closed Schema with
Checkpoint-only `current_state` and `rejected_alternatives`; unknown fields fail before
any durable write. This distinction is repeated in the bundled Skill so models do not copy
a Checkpoint payload into Task State.

Runtime data is created lazily under the active Hermes Profile at:

```text
<HERMES_HOME>/plugin-data/chris-hermes-agent/data.db
<HERMES_HOME>/plugin-data/chris-hermes-agent/archives/<random>.json
```

The data directory/database are restricted to `0700/0600`, and unsafe symlink,
non-regular-file, or multi-hardlink targets are rejected. SQLite uses WAL on fixed
versions and DELETE journal on versions affected by the WAL-reset corruption bug;
foreign keys, a busy timeout, versioned migrations, and optimistic locks protect Task
and Session state. Resume updates durable state and
returns `context_rotation_applied: false`, `context_rotation_required: true`,
and `next_required_action: call_handoff_context`; the explicit Handoff call
then performs the request-level Context switch.

Task creation provides the P5 isolation boundary. An unfinished active Task
must first have a valid Checkpoint; creation then pauses it and activates the
target Task atomically. Subtasks record `parent_task_id` and inherit only
explicitly selected constraints, decisions, and durable artifact references.
Independent tasks inherit none of that state. In both cases the Agent creates a
target-owned Checkpoint and explicitly rotates to the returned Task/Segment, so
old Tool Trace is excluded from the next request.

## Development

The contract suite runs against a Hermes Agent checkout:

```bash
uv sync --extra dev
HERMES_AGENT_ROOT="$HOME/.hermes/hermes-agent" \
  uv run pytest tests/contract
```

Run the complete verification suite:

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy chris_hermes_agent
uv run pytest --cov=chris_hermes_agent --cov-report=term-missing
uv build
hermes_temp_dir=$(mktemp -d)
HERMES_HOME="$hermes_temp_dir" hermes plugins doctor . --ci
```

See [the development plan](./Hermes%20长任务%20Context%20Handoff%20开发计划.md)
and [the original requirements](./Hermes%20长任务%20Context%20Handoff%20方案.md).
The guarded live procedure and one-command rollback are in the
[`chris-avatar` P7 runbook](./docs/P7-chris-avatar-runbook.md).
