---
# Is the analyst's plan grounded in the code it read?
name: plan-names-real-files
stations: [plan]
labels:
  grounded: 1
  partly_grounded: 0.5
  ungrounded: 0
pass_score: 1
---
The analyst reads the repository and writes a plan (its final message holds
it as JSON: approach, plan steps, files_of_interest). Judge whether the files,
modules and functions the plan names are ones the transcript shows the agent
actually found -- in a Read, Glob or Grep result, or a git log/diff output.

- `grounded`: every file or symbol the plan relies on appears in a tool result
  earlier in the transcript.
- `partly_grounded`: most do, but one or two named paths never appear in any
  tool result (a guessed filename, a module that may not exist).
- `ungrounded`: the plan's key files never appear in any tool result, or the
  agent made few or no tool calls and planned from the ticket text alone.

A plan whose verdict is needs_input (it asks questions instead) is judged on
whether its questions cite what it found; if it found nothing, `ungrounded`.
