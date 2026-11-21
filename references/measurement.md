# Measurement

Read before planning or judging a benchmark. A number names the commits, host and
toolchain it measured, and expires with them.

## Short runs

A strict or exclusive job lasts at most 10 minutes, a light fuzz or soak 30 with its build; `--long
REASON` lifts the cap and defers the job, so it proves no criterion before landing. Longer fuzzing,
soaks and recovery tails are milestone checks.

| Stage | Length | Level | Purpose |
|---|---|---|---|
| Smoke | seconds | `light` | The row's own command at its smallest size, at the commit the row will measure: every arm runs, its effective configuration is printed and asserted, and the output holds every field the decision reads; a failed assertion aborts. |
| Row | ≤ 10 min | `strict`; `exclusive` only for a published number or when the strict interval straddles the threshold, one job, never trials, A/As or platform repeats; `light` for memory, disk, bytes written and counts | One job per row, holding its interleaved A/B pairs (`--ref base=<commit>`; `$JOB_REF_BASE` is the baseline checkout). It decides by the rule (metric, arms, noise floor, threshold) the quest file fixed first; a rule changed after data is a Decisions row, every row rerun under it. |

A row measures the quest branch with `main` merged in first, so landing never re-measures it. Run a
row a second time only when its interval straddles the target. Chain the stages with `--after`, so
a row runs only when its smoke passed; `--build` builds in the row's checkout. On
macOS strict shares the machine with light work (`beside`).

## A/B rules

- **Run-level intervals.** Interleave whole runs and take the interval over per-run medians, never
  over individual queries. Count a difference only when it clears the A/A band.
- **Check what you time:** `file` on every benchmark binary, one toolchain per architecture.
- **Background work is measured under load:** merges and compaction compete for cores and I/O; a
  row measures whether they keep up and what they cost foreground latency.
- **State every ratio with its direction** ("3.4× faster", "9% smaller") and the honest magnitude,
  losses included; tie wins to CPU time, memory, dollars or power when the numbers allow.

## Who measures what

- An optimization has a budget of about five experiments; stop at the target or the third loser in
  a row, and record losers with their numbers.
- Published numbers come from `main` or the exact landed commit, never a branch.
- Other platforms are measured once per milestone (`--later`).

## Results

A results note (`docs/perf/<topic>.md`, or the journal for small ones) names the job ids, commits,
host and verdict. A result later shown invalid is labelled where it lives, with the reason, and
every claim citing it is corrected in the same change.
