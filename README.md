# chris-hermes-agent

Hermes Native Plugin for agent-managed Task State, Checkpoints, and Context
Rotation during long-running work.

The repository is currently at **P0: plugin scaffolding and host-contract
tests**. The plugin surface is intentionally fail-closed: tools are registered
for contract stability, while persistence and Context Rotation remain disabled
until their implementation phases are complete.

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
hermes plugins doctor . --ci
```

See [the development plan](./Hermes%20长任务%20Context%20Handoff%20开发计划.md)
and [the original requirements](./Hermes%20长任务%20Context%20Handoff%20方案.md).
