# chris-hermes-agent

Hermes Native Plugin for agent-managed Task State, Checkpoints, and Context
Rotation during long-running work.

The repository has completed **P1: configuration and Policy Resolver**. The
plugin now reads profile-scoped handoff settings, validates model policies, and
re-resolves them whenever Hermes changes models. Task persistence, Context
Rotation, and Emergency Compression execution remain disabled until their
implementation phases are complete.

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
