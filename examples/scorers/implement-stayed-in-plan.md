---
# Did the change stay inside the approved plan?
name: implement-stayed-in-plan
stations: [implement]
labels:
  in_plan: 1
  minor_drift: 0.5
  out_of_plan: 0
pass_score: 1
sample_rate: 50
---
Compare the files the agent edited (Edit and Write tool calls) and what its
final message says it did with the approach, plan and acceptance criteria
above.

- `in_plan`: every edited file is plausibly needed by a plan step or an
  acceptance criterion. Tests for the changed code, and a lockfile changed by an
  install the plan implies, are in plan.
- `minor_drift`: one or two unplanned edits that are small and clearly in
  service of the plan (a renamed helper, a fixed typo in a touched file), and
  the final message mentions them.
- `out_of_plan`: unrelated refactors, reformatting files the plan does not
  touch, new features or dependencies nobody asked for, or unplanned edits the
  final message does not mention.

If no plan or approach was recorded, use `insufficient_evidence`: there is
nothing to have stayed inside.
