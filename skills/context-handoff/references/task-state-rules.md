# Task state, inheritance, and isolation rules

## Result discipline

Every tool returns a JSON envelope. Advance the workflow only on `ok: true`. Preserve the
latest Task `version` and use it as `expected_version`; after `concurrent_update` or
`active_pointer_changed`, fetch current state and reconsider the operation.

The active Task and Segment must agree between durable state, the latest Runtime Status, and
the arguments to `handoff_context`. Never guess identifiers from older messages.

## Lifecycle ordering

### continuation

Update the existing Task and append selective events. Do not create a Task, change
`parent_task_id`, or isolate Context solely because the user sent a follow-up.

### subtask

1. Make the parent Task state current and create a parent Checkpoint.
2. Create the child with `parent_task_id` and only the selected inheritance fields below.
   Task creation validates the parent Checkpoint, pauses the parent, closes its Segment, and
   activates the child atomically.
3. Create a child-owned Checkpoint describing the child's initial recoverable state.
4. Call `handoff_context` using the child Task and Segment returned by creation.
5. Require `handoff_applied: true`, then continue from the child's `Next Actions`.

### new_task

Use the same ordering as `subtask`, but omit `parent_task_id` and inherited parent state. The
new Task receives its own goal, Checkpoint, Segment, Decisions, and Artifacts. The old Task's
Tool Trace and conversation history are not copied into the selected Context.

### resume

Checkpoint a different unfinished active Task first. Call `task_state_manage` with
`action: resume` only after one target is selected. A successful response intentionally has
`context_rotation_applied: false`, `context_rotation_required: true`, and
`next_required_action: call_handoff_context`. Use its returned Task, Segment, and Checkpoint
for the explicit rotation, and require `handoff_applied: true` before continuing.

## Subtask inheritance whitelist

Inheritance is opt-in and item-by-item. The only parent state that may be copied is:

- `constraints`: requirements that still apply to the child;
- `decisions`: binding choices the child must honor, including useful rationale;
- `artifacts`: durable inputs the child directly needs.

Set `parent_task_id` for lineage, but give the child its own title, goal, phase, current
state, Next Actions, and Checkpoint. Do not inherit the parent's completed work, in-progress
work, known issues, search aliases, tags, status, resume counters, events, Session IDs,
Context Segments, Checkpoint ID, conversation, or Tool Trace. An Artifact is a durable
reference, not permission to copy the output that produced it.

## Events

Append events for decisions and revocations, newly discovered constraints, completed phases,
material file changes, meaningful test failures/passes, and explicit blocked/completed/
cancelled transitions. Task lifecycle and Checkpoint/Handoff events already emitted by the
runtime must not be duplicated manually.

Do not append routine reads, transient Runtime Status, full diffs, shell output, or repeated
progress narration.

## Failure boundaries

- A missing, incomplete, mismatched, or corrupt Checkpoint stops pause/resume/Handoff.
- A failed target Task creation leaves the prior Task active because the transaction rolls
  back.
- A successful Task pointer update is not proof of Context Rotation.
- A failed Handoff does not authorize deleting history, invoking default Compression, or
  continuing as though isolation succeeded.
- When bootstrap diagnostics report missing or corrupt recovery state, stop and repair the
  durable state without reintroducing archived trace.
