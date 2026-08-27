---
name: context-handoff
description: Manage durable task checkpoints and rotate active Context for long-running Hermes work without ending the current Agent Turn.
version: 0.1.0
metadata:
  hermes:
    tags: [context, handoff, long-running-tasks]
---

# Context Handoff

## Current implementation status

The plugin runtime has completed P4: Task State and Checkpoint persistence plus
atomic Context Rotation are available. The full Agent-facing workflow for task
classification, Checkpoint quality, and automatic Handoff decisions is still
scheduled for P5.

Until that workflow is added:

- do not infer task/subtask boundaries or Handoff timing from this stub;
- do not call `handoff_context` without a persisted valid Checkpoint and the
  exact active Task/Segment returned by the runtime;
- do not claim Rotation succeeded unless `handoff_applied` is `true`;
- never delete or rewrite Hermes Session history.

P5 will replace this status stub with the complete Task State, Checkpoint,
new-task detection, and Handoff operating workflow.
