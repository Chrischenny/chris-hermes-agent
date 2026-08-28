---
name: context-handoff
description: Manage durable Hermes tasks and active Context Rotation.
version: 0.4.2
author: Chrischenny
metadata:
  hermes:
    tags: [context, handoff, long-running-tasks]
---

# Context Handoff

Keep long-running work recoverable without ending the current Agent Turn or copying old
execution noise into a new Context. Task meaning belongs here and in the Task tools;
`ContextHandoffEngine` only observes the active request and applies an explicit rotation.

## When to Use

Use this workflow when work may span Context segments or Sessions, when the user changes or
resumes a Task, or when Runtime Status reports that a configured Handoff sweet zone has been
reached. Ordinary short work that has no durable Task does not need this workflow.

## Invoke deferred Task tools

Hermes may expose this plugin's Task tools through the generic deferred `tool_call` tool.
When it does, the outer wrapper contains exactly `name` and `arguments`; put every
plugin-specific field inside `arguments`. For example, an update is wrapped as:

```json
{
  "name": "task_state_manage",
  "arguments": {
    "action": "update",
    "task_id": "task-example",
    "expected_version": 6,
    "state": {
      "current_phase": "verification"
    }
  }
}
```

Apply the same boundary to `task_event_append` and `checkpoint_create`. Never omit the
outer `name`. Never place `task_id` outside `arguments` or split tool-specific fields
between the two levels. If Hermes returns `invalid_argument`, rebuild the wrapper from the
deferred tool schema and fresh durable identifiers. Never infer a missing Task ID,
Checkpoint ID, Segment ID, or expected version; keep the mutation fail-closed.

## Start or re-enter work

On a long task, a changed user goal, or a resume request:

1. Read the final `[Runtime Status]` message. Treat its Task, Segment, model policy,
   estimated usage, and last actual usage as runtime facts for this request only.
2. Call `task_state_manage` with `action: get` to reconcile the durable active Task.
3. Classify the request before changing durable state. Read
   [new-task detection](references/new-task-detection.md) whenever the request may be a
   continuation, subtask, independent task, or resume.
4. If the classification is `ambiguous` and choosing would change Task or Context state,
   ask the user before any state-changing tool call.

Do not create a second Task for ordinary continuation. Do not rotate merely because a new
user message arrived.

## Task State fields versus Checkpoint fields

These payloads deliberately use different schemas. Never copy the complete Checkpoint
object into `task_state_manage.state`.

Task State fields are exactly:

```text
title, goal, constraints, current_phase, completed, in_progress,
known_issues, next_actions, decisions, artifacts, search_aliases, tags
```

For `action: create`, put the non-empty `title` and `goal` inside `state`; Task status is
set by the lifecycle action and is not a `state` field. Use `in_progress` for current Task
work. For `action: update`, send only changed Task State fields plus `task_id` and the last
returned `expected_version`.

Checkpoint fields are exactly:

```text
goal, constraints, current_phase, completed, current_state, decisions,
rejected_alternatives, known_issues, artifacts, next_actions
```

All Checkpoint fields are required by `checkpoint_create.checkpoint`, and `next_actions`
must be non-empty. `current_state` and `rejected_alternatives` are Checkpoint-only; they
must never be passed to `task_state_manage.state`.

## Maintain the current Task

- Use `task_state_manage` with `action: update` to keep phase, completed work, current
  work (`in_progress`), issues, decisions, artifacts, and Next Actions current. Use the
  returned version for the next optimistic update.
- Use `task_event_append` for durable decisions, revoked decisions, constraints, phase
  boundaries, material file changes, and meaningful test outcomes. Do not log Runtime
  Status or bulk shell/file/tool output.
- Follow [task state rules](references/task-state-rules.md) for lifecycle transitions,
  inheritance, resume, and tool-result checks.

## Decide when to rotate

Use current estimated prompt tokens for the immediate decision and last actual prompt
tokens only as lagging calibration.

- If Runtime Status has no matched valid Handoff policy, remain observation-only. Never
  invent a token or ratio threshold.
- Once the configured sweet-zone start is reached, prefer the next stable boundary: a
  testable change is complete, a decision is settled, a failure is understood, or the
  next action can be stated precisely.
- Delay only while the current atomic operation would become ambiguous if interrupted.
  Update durable state while approaching the boundary.
- Never invoke default Compression as a normal Handoff substitute.

Emergency Fallback is a last resort and only exists when Runtime Status shows a matched,
explicitly configured Emergency threshold. Never trigger it as an ordinary Handoff
substitute or invent a threshold. When Runtime Status reports `completed`, treat the
request as a temporary recovery Context: reconcile the active Task, persist a complete
Checkpoint as soon as state is stable, and perform the normal explicit Handoff. When it
reports `triggered` or `failed (...)`, do not retry compression, delete history, or claim
success; preserve the durable state and resolve the reported failure safely.

## Create a Checkpoint and hand off

Read [the checkpoint template](references/checkpoint-template.md), then:

1. Reconcile the active Task and the exact active Segment from the latest Runtime Status.
2. Update Task State and append any material events that are not yet durable.
3. Run the template quality self-check. Call `checkpoint_create` for the active Task.
4. Continue only when the result has `ok: true`, a Checkpoint ID, and a checksum. Keep the
   returned Checkpoint ID; never guess or reuse an unrelated reference.
5. Call `handoff_context` with that Checkpoint, a concrete reason, and the exact active
   Task and Segment IDs observed immediately before the call.
6. Treat rotation as successful only when the result has `ok: true` and
   `handoff_applied: true`. On a stale pointer or concurrent update, inspect fresh state
   and restart the preflight; do not retry old expected IDs.
7. In the next Provider Request, verify that the bootstrap names the expected Task,
   Checkpoint, and Segment. Continue directly from Checkpoint `Next Actions` in the same
   Agent Turn.

Never delete or rewrite Hermes Session history. Rotation changes only the active Provider
Request selection.

## Change or resume Tasks

For a `subtask` or `new_task`, first checkpoint the unfinished active Task. Then create the
target Task; creation atomically pauses the old Task after validating its Checkpoint. A
subtask receives `parent_task_id` and only explicitly selected inherited state. Create a
separate Checkpoint owned by the newly active target Task, then call `handoff_context` with
the target Task and Segment returned by creation. The target Checkpoint is required because
a parent Checkpoint cannot bootstrap another Task.

For resume, search paused and blocked Tasks. Resume a single clear candidate; ask the user
to choose among comparable candidates. Before resume, checkpoint any different unfinished
active Task. A successful `task_state_manage` resume updates the durable pointer but reports
`context_rotation_required: true`; call `handoff_context` explicitly with its returned Task,
Segment, and Checkpoint. Resume is incomplete until `handoff_applied: true`.

Detailed ordering, inheritance rules, and failure behavior are in
[task state rules](references/task-state-rules.md).
