---
# Are the reviewer's blocking findings something an implementer can act on?
name: review-findings-specific
stations: [review]
labels:
  specific: 1
  vague: 0
sample_rate: 100
---
The reviewer's final message is a JSON verdict. Judge its `blocking_findings`
and `criteria_results`.

- `specific`: each blocking finding names a file (and ideally a function or
  line) and says what is wrong and why, and the transcript shows the reviewer
  looked at that code (a Read or git diff of it). A verdict of `approve` with no
  blocking findings is `specific` when its criteria_results cite evidence from
  the diff or a test run, rather than restating the criteria.
- `vague`: a finding a person could not act on without redoing the review
  ("improve error handling", "add more tests"), a finding about code the
  transcript never shows the reviewer opening, or an approval that cites no
  evidence at all.
