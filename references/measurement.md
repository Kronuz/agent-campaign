# Measurement

Read before planning, running or judging a benchmark. A number names the commits, host and
toolchain it measured, and expires with them.

## Stage every experiment

A long run is the last step, never the first. Rigs have run for hours on a wrong configuration
(ignored config keys, a deleted load generator, an arm that ran as the baseline); the smoke stage
exists to catch that.

| Stage | Length | Job level | Purpose |
|---|---|---|---|
| Smoke | ≤ 3 min | `strict` | Every arm runs end to end; the effective configuration is printed and asserted (the arm's settings applied, the server under test is the one named, counters non-zero). Any failed assertion aborts. |
| Screen | 10-15 min | `strict` | One run per arm with short phases, to find the direction and drop clear losers |
| Confirm | full length, repeated, ≤ 30 min per job | `exclusive` | Only the arms and claims that will be quoted. The only stage that decides anything. |

Chain them with `--after` so the confirm job runs only when the screen passed, and compare exact
commits with `--ref base=<commit>` (`$JOB_REF_BASE` is the baseline checkout). Soaks and recovery
tails keep their length by running several 30-minute jobs, never one long one.

## A/B rules

- **Run-level intervals.** Repeat whole interleaved runs and take the interval over per-run
  medians, never over individual queries. Run an A/A first; count a difference only when it clears
  the A/A band.
- **Check what you time:** `file` on every benchmark binary, one toolchain per architecture (an
  Intel `cmake` on an arm64 Mac once built x86-64 binaries that ran under Rosetta).
- **Every run times itself:** start, warm-up, timed part and teardown, saved with the results.
- **Background work is measured under load:** merges and compaction compete for cores and I/O; a
  row measures whether they keep up and what they cost foreground latency.
- **Calibrate each baseline once** per row and sample; later runs compare against the fastest
  baseline only, recalibrating if it drifts past its spread.
- **State every ratio with its direction** ("3.4× faster", "9% smaller") and the honest magnitude,
  losses included; tie wins to CPU time, memory, dollars or power when the numbers allow.

## Who measures what

- An adventurer screens and confirms its own candidates as jobs; nothing waits on the machine.
- A hot-path change runs one smoke A/B job against its parent before review, so a large regression
  is caught at the change that caused it, not in a bisect later.
- An optimization has a budget of about five experiments; stop at the target or the third loser in
  a row, and record losers with their numbers.
- Milestone and published numbers are measured on `main` (or the exact landed commit) as
  `exclusive` jobs, never quoted from a branch.
- Measure the other platforms once, on the version that will land, as deferred checks (`--later`):
  they run after landing at low priority and gate the milestone, not the quest.

## Results

A results note (`docs/perf/<topic>.md`, or the quest's journal for small ones) names the
job ids, commits, host and verdict. A result later shown invalid is labelled where it lives, with
the reason, and every claim citing it is corrected in the same change.
