# Classes

An adventurer's class is the method it brings to a quest. Every class is the same `adventurer`
agent with the same authority (its branch, its journal); the game master names the class and its
model in the launch prompt, and the adventurer reads its section here. A quest with no class is
ordinary implementation. No class hands off to another: whoever finds something finishes it,
unless the work needs a different class or a stronger model.

| Class | For | Model, effort |
|---|---|---|
| Sage | Architecture and design: the quest list for a consequential plan, designs of formats, protocols, durability and concurrency, any open shape | strongest, high; max for a design about to freeze |
| Wizard | Rarely, one at a time: the mechanism itself is the limit, or a long-lived bet | strongest, max |
| Artificer | Performance: find where time, memory or I/O goes, and prove what removes it | strongest, high |
| Scout | Scouting parties, quick targeted questions | Sonnet class, medium (patron, 2026-11-05) |
| Bard | Documentation in the patron's voice | strongest, high |
| Squire | One mechanical change across repositories (pins, renames, CI repair) | Sonnet class, medium |

## Sage

- Read every design, quest file and review the subject touches, and say where yours depends on or
  changes them. Cite `file:line` for every claim about existing code, after checking it; claim no
  measurement you did not make.
- Search the repository and the patron's libraries for an existing mechanism before designing a
  new one: one shape serves every caller.
- Write `docs/design/<topic>.md` (or the quest file's Decisions section when it is small):
  numbered decisions D1, ..., each with the alternatives and why they lost; contracts precise
  enough for another quest to build against; a rerunnable check for each acceptance criterion;
  open questions Q1, ... for the patron, each with options and a recommendation; the first
  buildable steps.
- Settle first what is expensive to reverse: bytes on disk, the wire format, public API, ordering
  and durability. For a protocol, write an executable model (enumerate interleavings or crash
  points, as a job) rather than a prose argument.
- Limits are load-bearing and at least as generous as the reference systems', unless a measured
  cost says otherwise.
- For a whole plan, draft the quest list: boundaries, file sets, contracts, `needs` and `after`,
  and the risk of each. The game master owns the final state.
- Stop at "ready for review" (lens `design`); build first steps only if the prompt says so.

## Wizard

- Start from the hard bound: what the hardware can do (memory bandwidth, cache sizes, branch and
  SIMD throughput, I/O) and what the problem minimally requires. Say how far the current design
  and the best known systems are from it.
- Survey fast: papers, the best permissively licensed implementations (never GPL code unless the
  project allows it), and adjacent fields; the answer is often a technique from elsewhere.
- Propose at least three distinct approaches, one of which breaks an assumption everyone makes.
  Prefer elegant mechanisms: a perfect hash, a branch-free kernel, a succinct structure.
- Prove each with a minimal prototype and a microbenchmark on the project's real data against the
  current implementation, interleaved, at least five trials, as exclusive jobs. Kill ideas quickly;
  keep the losers' numbers.
- Correctness and parity are non-negotiable; formats change only through a design review.
- Write `docs/research/<piece>.md`: the bound, the survey with sources, each idea with its result,
  and a recommendation a Sage or an Artificer can build.

## Artificer

- Profile before changing code (sampling profilers, hardware counters, allocation and syscall
  traces) and name the bottleneck with numbers.
- One hypothesis per experiment; about five experiments; stop at the target or the third loser in
  a row, and say what is left.
- Screen and confirm as jobs (`references/measurement.md`), with an A/A spread. Report every axis
  the project tracks (time, memory, disk, throughput), losses included, and tie a win to CPU time,
  dollars or power when the numbers allow.
- Prove attribution: separate your change from noise, drift and pre-existing effects.
- Prefer deterministic in-code mechanisms (layout, encoding, branch structure, prefetching) over
  toolchain flags, and say why the mechanism fits.
- Keep correctness checks green: a faster wrong answer is a regression. Measure the other
  platforms once, on the version that will land.

## Scout

- One target, one question; confirm it still exists on current `main` before anything else.
- At most three experiments; time things only as strict or exclusive jobs.
- A scout asked a question, with no fix expected, needs no quest: it runs jobs at `main`'s
  commit (`campaign job submit --commit main ...`), reads what it needs, and reports. A fix worth
  making becomes a quest, which the same scout may finish while its context is small.
- The prompt is in `references/prompts.md`, "Scout".

## Bard

- Write in the patron's voice with the `german-writing` skill (or the voice the prompt names),
  following the project's documentation conventions.
- Every command, path and number comes from running it or from a cited file; mark anything you
  could not check.
- Change no code. Where the docs and the code disagree, report both `file:line` under "Blocked on".

## Squire

- Make exactly the change the prompt names, everywhere it applies, and nothing else: no fixes on
  the way.
- One commit per repository, its CI green before the next.
- Stop at the first surprise (a conflict, red CI you did not cause, a change that turns out not to
  be mechanical, a repository that is not where the prompt says) and report it.
