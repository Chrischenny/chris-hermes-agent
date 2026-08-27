# chris-hermes-agent

Hermes Native Plugin for agent-managed Task State, Checkpoints, and Context
Rotation during long-running work.

The repository has completed **P4: Context Rotation**. The plugin persists
profile-scoped task state, exposes an atomic `handoff_context` tool, and selects
a checkpoint-based Context on the next Provider Request without ending the
current Agent Turn. Emergency Compression remains disabled until its later
phase.

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

## Task lifecycle

`task_state_manage` supports `create`, `get`, `update`, `pause`, `search`,
`list`, `resume`, `block`, `complete`, and `cancel`. Starting a new task in a
busy Session requires a valid Checkpoint and pauses the unfinished task by
default. Task search covers state, selected decision events, Checkpoints,
artifacts, aliases, tags, and Next Actions without indexing bulk Tool Trace.

Runtime data is created lazily through Hermes `plugin_db()` at:

```text
<HERMES_HOME>/plugin-data/chris-hermes-agent/data.db
```

SQLite runs with WAL, foreign keys, a busy timeout, versioned migrations, and
optimistic locks on Task and Session state. Resume updates durable state and
returns `context_rotation_applied: false`, `context_rotation_required: true`,
and `next_required_action: call_handoff_context`; the explicit Handoff call
then performs the request-level Context switch.

## Development

The contract suite runs against a Hermes Agent checkout:

```bash
uv sync --extra dev
HERMES_AGENT_ROOT="$HOME/.hermes/hermes-agent" \
  uv run pytest tests/contract
```

Run the complete verification suite:

```bash
uv run ruff check .
uv run mypy chris_hermes_agent
uv run pytest --cov=chris_hermes_agent --cov-report=term-missing
uv build
hermes plugins doctor . --ci
```

See [the development plan](./Hermes%20长任务%20Context%20Handoff%20开发计划.md)
and [the original requirements](./Hermes%20长任务%20Context%20Handoff%20方案.md).
