# Measurement

Read before planning, running or judging a benchmark. A number names the commits, host and
toolchain it measured, and expires with them.

## Short runs

A strict or exclusive job, and a fuzz or soak, lasts at most 10 minutes; `--long REASON` lifts the
cap and defers the job (priority -1), so it proves no criterion before landing. Longer fuzzing,
soaks and recovery tails are milestone checks.

| Stage | Length | Level | Purpose |
|---|---|---|---|
| Smoke | ≤ 3 min | `strict` | Every arm runs end to end with its effective configuration printed and asserted (the arm's settings applied, the server under test the one named, counters non-zero); a failed assertion aborts. |
| Row | ≤ 10 min | `strict`, or `exclusive` for a number that decides or gets published | One job per row, holding its interleaved A/B pairs (`--ref base=<commit>`; `$JOB_REF_BASE` is the baseline checkout). It decides by the rule (metric, arms, noise floor, threshold) the quest file fixed first; a rule changed after data is a Decisions row, every row rerun under it. |

Run a row a second time only when its interval straddles the target. Chain the stages with
`--after`, so a row runs only when its smoke passed.

## A/B rules

- **Run-level intervals.** Interleave whole runs and take the interval over per-run medians, never
  over individual queries. Count a difference only when it clears the A/A band.
- **Check what you time:** `file` on every benchmark binary, one toolchain per architecture (an
  Intel `cmake` on an arm64 Mac once built x86-64 binaries that ran under Rosetta).
- **Every run times itself:** start, warm-up, timed part and teardown, saved with the results.
- **Background work is measured under load:** merges and compaction compete for cores and I/O; a
  row measures whether they keep up and what they cost foreground latency.
- **State every ratio with its direction** ("3.4× faster", "9% smaller") and the honest magnitude,
  losses included; tie wins to CPU time, memory, dollars or power when the numbers allow.

## Who measures what

- A hot-path change runs one smoke A/B job against its parent before landing.
- An optimization has a budget of about five experiments; stop at the target or the third loser in
  a row, and record losers with their numbers.
- Published numbers come from `main` or the exact landed commit, never a branch. A passed
  measurement whose inputs are unchanged is reused, never rerun.
- Other platforms are measured once per milestone, as milestone checks (`--later`).

## Results

A results note (`docs/perf/<topic>.md`, or the journal for small ones) names the job ids, commits,
host and verdict. A result later shown invalid is labelled where it lives, with the reason, and
every claim citing it is corrected in the same change.
