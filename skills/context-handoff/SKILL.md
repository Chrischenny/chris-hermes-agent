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

The plugin is in P0 contract-scaffolding mode. Its tools are visible so their
names and schemas remain stable, but Task State persistence and Context
Rotation are deliberately disabled.

Until the runtime reports that those phases are available:

- do not claim that a checkpoint was persisted;
- do not claim that a context handoff occurred;
- do not work around the disabled tools by deleting session history;
- continue using the active Hermes context normally.

The complete Task State, Checkpoint, new-task detection, and Handoff workflow
will be enabled by later implementation phases.
