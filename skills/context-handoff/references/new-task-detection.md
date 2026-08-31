# New-task detection

Classify user intent before a mutation that would create, pause, resume, complete, or rotate
a Task. Read the active Task goal, phase, Next Actions, and the latest user request; do not
infer the relationship from topic words alone.

## Outcomes

| Outcome | Evidence | Durable action |
|---|---|---|
| `continuation` | The request advances, corrects, verifies, or narrows the active Task's success condition. | Update the same Task. Keep its current Segment unless an ordinary Handoff is independently due. |
| `subtask` | The request has its own deliverable but directly serves the active Task and should be traceable beneath it. | Checkpoint the parent, create a Task with `parent_task_id`, inherit only selected state, checkpoint the child, then rotate. |
| `new_task` | The request has an independent success condition and can be completed without advancing the active Task. | Checkpoint the active Task, create an unrelated Task, checkpoint the target, then rotate. |
| `ambiguous` | More than one outcome remains plausible and the choices would produce meaningfully different durable state or Context. | Ask the user before any state-changing tool call. |

A request for a small prerequisite is not automatically a subtask: keep it as
`continuation` when separate lifecycle, search, pause, or resume would add no value. A topic
change is not automatically independent when it is required by the current success condition.

## Confidence rule

Use `continuation`, `subtask`, or `new_task` without interruption only when the relationship
is clear from the active Task and the user's language. When uncertain, state the two concrete
interpretations and ask one concise question. Do not create a provisional Task, pause the
active Task, append a classification Decision, or call `handoff_context` before the answer.

Read-only reconciliation is allowed while waiting: `task_state_manage` actions `get`, `list`,
and `search` do not commit to a classification.

## Continuation and resume discovery

A request to return to previous work is a resume lookup, not a `new_task`:

1. When history supplies an exact Task ID, call `task_state_manage` with `action: get` and
   that ID. Never replace this authoritative lookup with search, and never infer that the
   Task is absent because search returned no candidates.
2. Without an exact ID, search or list all unfinished statuses: `active`, `paused`, and
   `blocked`. Expand the user's wording with known aliases and inspect candidate state and
   the latest Checkpoint; title-string equality is not required.
3. If the clearly matching Task is `active` in another Session, you must not call `create`.
   Report the existing Task and keep state unchanged because this version does not attach
   an active Task to a new Session.
4. Resume a unique, clearly matching `paused` or `blocked` candidate.
5. If multiple candidates are comparable, show concise titles/goals and ask the user to
   choose; similarity order alone is not authorization to mutate state.
6. Only after exact lookup and all-unfinished discovery find no matching Task may you report
   that no continuation candidate exists and classify a newly stated goal normally.
