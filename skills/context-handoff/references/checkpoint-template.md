# Checkpoint template and quality gate

Use this reference before `checkpoint_create` or any operation that pauses, resumes, or
rotates a Task. The payload has exactly these fields:

```json
{
  "goal": "The current Task outcome, specific enough to detect completion",
  "constraints": ["Still-applicable requirements and authorization boundaries"],
  "current_phase": "The smallest useful phase label",
  "completed": ["Verified outcomes, not activity logs"],
  "current_state": ["Facts needed to continue from the present repository/runtime state"],
  "decisions": ["Decision — rationale — affected scope"],
  "rejected_alternatives": ["Alternative — why it was rejected"],
  "known_issues": ["Unresolved issue — evidence or impact"],
  "artifacts": ["Exact file path, commit, test, record, or other durable identifier"],
  "next_actions": ["Concrete first action", "Ordered follow-up action"]
}
```

All fields are required. Array fields may be empty only when there is genuinely nothing to
record. `next_actions` must contain at least one non-empty action.

## Quality self-check

Before persistence, verify that:

- `goal` matches the owning Task's current goal and does not silently broaden it;
- `constraints` includes live safety, scope, compatibility, and user-choice boundaries;
- `completed` contains only outcomes supported by current files, state, or test evidence;
- `current_state` tells a fresh Agent what is true now without requiring old Tool Trace;
- `decisions` preserves rationale and scope for choices that affect later work;
- `rejected_alternatives` prevents already-settled paths from being reconsidered blindly;
- `known_issues` distinguishes an observed problem from speculation;
- `artifacts` uses durable, precise references and contains no secrets or raw command dumps;
- `next_actions` starts with an executable action and preserves dependency order;
- no field copies large shell output, file bodies, conversation transcript, or Tool Trace.

After `checkpoint_create`, require `ok: true`, retain the returned `checkpoint_id`, and
confirm that `content_checksum` is present. The runtime computes the checksum and validates
it again before pause, resume, and Handoff. Never manufacture or edit a checksum.

For a new Task or subtask, create a new Checkpoint owned by that target Task after it becomes
active. A parent's Checkpoint may inform explicitly selected state, but it is never the
target Task's recovery Checkpoint.
