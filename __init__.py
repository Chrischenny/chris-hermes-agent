"""Native Hermes plugin entrypoint."""

if __package__:
    from .chris_hermes_agent.plugin import register
else:  # pragma: no cover - direct local import used by generic Python tooling
    from chris_hermes_agent.plugin import register

__all__ = ["register"]
