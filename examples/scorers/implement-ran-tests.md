---
# Did the implementer check its own work before handing it to review?
name: implement-ran-tests
stations: [implement, fix]
labels:
  ran_and_passed: 1
  ran_and_failed: 0
  did_not_run: 0
sample_rate: 100
---
Judge whether the agent ran the repository's tests (the "Test command" above,
or a narrower subset of it that covers the files it changed) *after its last
code edit*, and what the final run showed.

- `ran_and_passed`: a test run appears after the last Edit/Write, and its
  result shows the tests passing. A narrower run (one file, one package) counts
  if it covers what was changed.
- `ran_and_failed`: the last test run after the last edit shows failures, and
  the agent finished anyway -- including when its final message says the
  failures are "unrelated" without showing they also fail on the base branch.
- `did_not_run`: no test command appears after the last edit. Running tests
  early and then editing again does not count. A lint or type-check alone is
  not a test run.

If there is no test command configured and the transcript shows no attempt to
find one, that is `did_not_run`. If the log was truncated or the run timed out
before the end, use `insufficient_evidence`.
