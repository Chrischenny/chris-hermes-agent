# chris-hermes-agent

Hermes Native Plugin for agent-managed Task State, Checkpoints, and Context
Rotation during long-running work.

The repository has completed **P2: SQLite and Task State**. The plugin now
persists profile-scoped Tasks, Events, Checkpoints, Context Segments, and
Session Active Pointers. It supports pausing unfinished work, natural-language
search, and cross-session resume. Provider Context Rotation and Emergency
Compression execution remain disabled until their later implementation phases.

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
returns `context_rotation_applied: false` until P4 connects the Provider
Context switch.

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
