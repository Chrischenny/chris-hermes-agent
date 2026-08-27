## Long-task Context lifecycle

- At the start of long-running work, load `chris-hermes-agent:context-handoff` and use its
  durable Task, Checkpoint, and Context Rotation workflow.
- Before each response, inspect the final `[Runtime Status]`. Use only the current model's
  matched Handoff policy. When no valid policy is matched, report usage facts and never
  invent a threshold.
- In the configured sweet zone, seek a stable boundary while keeping Task State current.
  Do not call `handoff_context` until the active Task has a complete, valid Checkpoint with
  actionable `Next Actions`.
- Treat a Handoff as complete only when the tool result reports success and
  `handoff_applied: true`. Continue in the same Agent Turn from the Checkpoint's
  `Next Actions`.
- Classify changed user intent before durable mutation. Continue the current Task when it
  has the same success condition; isolate a true subtask or independent task. Ask the user
  when an uncertain classification would change Task or Context state.
- A subtask may inherit only explicitly selected constraints, decisions, and durable
  artifacts. A new independent task inherits no execution history. Never copy old Tool
  Trace into a target Task or rewrite Hermes Session history.
- Resume only a uniquely identified paused or blocked Task. If candidates are comparable,
  ask the user to choose. After durable resume, explicitly rotate Context as directed by
  the tool result.
- Emergency Fallback may run only when the current Runtime policy explicitly enables it
  and the runtime actually reports the action. When its status is `completed`, reconcile
  the Task, create a complete Checkpoint at the next stable boundary, and perform the
  normal explicit Handoff. For `triggered` or `failed`, do not retry Compression or claim
  success. Never simulate Emergency Fallback or substitute default Compression.
