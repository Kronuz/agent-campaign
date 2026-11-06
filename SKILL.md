---
name: campaign
description: Run a large engineering project with agents as a campaign. The game master (the lead session) splits the patron's (the owner's) goals into quests, each a coherent, independently verifiable outcome; adventurer agents complete them on their own branches and worktrees; a fresh paladin (an independent reviewer) examines each quest as its risk requires; and the `campaign` tool keeps the state, runs builds, tests and benchmarks as jobs on exact commits under `quiet`, and lands only what was tested. Use when starting or resuming a multi-part project (an engine, a library family, a migration, a rewrite), when work should run in parallel worktrees, when a quest needs review or is ready to land, when a scouting party should hunt for bugs or wins, or when the owner says "start the campaign", "resume the campaign", "status", "review it" or "land it".
---

# Campaign

The **game master** (the main session) turns the **patron**'s goals (the owner's) into **quests**.
An **adventurer** agent completes each quest on its own branch; machine work goes to a **job
queue** that runs it at exact commits while no agent waits. A **paladin** agent, the independent
reviewer, examines a quest from a fresh context when its risk calls for it. **`campaign land`**
puts a quest on `main` only when its checks passed at that exact code and its required reviews
were accepted for that exact content. Everything that matters is in the repository, so any agent,
the game master included, can be replaced.

```
goals ─► PLAN ─► quests ─► campaign start ─► adventurer ─► jobs (quiet) ─► evidence
                                                 │                             │
                                                 └─► paladin (by risk) ─► accept ─► campaign land
```

## Invariants

1. **Evidence, not narrative.** A check counts only when a job ran it on the code being landed;
   a report, a journal line or a paladin's say-so is never a green test.
2. **One writer per file set.** `campaign start` refuses overlapping file sets; `land` refuses
   changes outside the quest's set and edits to the game master's files.
3. **Criteria before code.** The game master writes each quest's acceptance criteria; adventurers
   never edit them, and a changed quest file voids earlier acceptances.
4. **Review follows risk, not ritual.** One fresh paladin per risk that needs one; it spends on
   what the evidence does not already establish.
5. **State lives in files.** `.campaign/<campaign>/` and git hold how the campaign runs; what the
   product is (designs, research, results, how-it-works notes) goes to `docs/` of the repository it
   describes (`templates/PLAN.md`, Documents).
6. **Agents never wait on machines, and measurements never run without isolation.**
7. **The patron decides goals, scope, and any removal or narrowing of user-visible behavior.**
   The game master decides the rest and records it in `PLAN.md`. **Campaign never rents
   machines** or creates any other billable resource.

## The party

| Role | Does | Writes |
|---|---|---|
| Patron (the owner) | Goals, non-goals, reserved decisions | `PLAN.md` goals |
| Game master (the main session) | Plans quests, assigns, accepts reviews, lands, talks to the patron | `state.json` (only through `campaign`), quest files, `PLAN.md`, `main` |
| Adventurer (`adventurer` agent) | One quest or task, with the method of its class | its branch and journal |
| Paladin (`paladin` agent) | Reviews one quest through one lens, from a fresh context | one review file |

An adventurer's **class** is the method it brings (`references/classes.md`); the game master names
the class and the model at every launch, choosing by the work, never by habit:

| Class | For | Model, effort |
|---|---|---|
| (none) | ordinary implementation | strongest (Opus class), medium to high |
| Sage | architecture, quest lists for consequential plans, designs of formats, protocols, durability, concurrency | strongest, high; max for a design about to freeze |
| Wizard | rarely: the mechanism itself is the limit, or a long-lived bet | strongest, max |
| Artificer | performance work | strongest, high |
| Scout | scouting parties and quick questions | Sonnet class, medium (patron, 2026-11-05) |
| Bard | documentation in the patron's voice | strongest, high |
| Squire | one mechanical change across repositories | Sonnet class, medium |

Paladins run on the strongest model at high effort (max for critical quests). For critical quests
and the plan challenge, send the paladin from a different model family when the harness offers
one: reviewers of the same family share the adventurers' blind spots.

The game master does small edits itself (a five-line fix, a review commit, a trivial conflict): a
launch costs more. Harness specifics (launching, background runs, resuming, waiting):
`references/harness.md`.

## Start a campaign

1. In the home repository's main checkout: `campaign init --name NAME [--ci github]`. The patron
   writes Goals, Non-goals, Constraints and Reserved decisions in `.campaign/<campaign>/PLAN.md`.
2. Split the goals into quests: `campaign new ID --title T --risk R --files GLOBS
   [--needs IDS] [--after IDS] [--provides C] [--consumes C] [--milestone M]
   --check NAME[:LEVEL[:MIN]]=CMD`. Fill in each quest file (`.campaign/<campaign>/quests/ID.md`):
   outcome, criteria A1... each naming its evidence, contracts, decisions, what may land early.
3. When the architecture is consequential, one paladin (lens `design`) challenges the plan and the
   quest list before anything is built. This is the one review worth waiting for.

## Quests

- **Size:** the smallest change that delivers a complete, meaningful behavior a paladin can hold
  in mind, usually a few hundred to ~1,500 lines. Split bigger ones; fold a change not worth its
  own landing into the quest it serves.
- **`needs`** must land before a quest starts; **`after`** must land before it lands. A quest may
  start against an `after` dependency only because that dependency's quest file fixes the contract.
- **Contracts** (`--provides`, `--consumes`) name APIs, formats and protocols, so `campaign check`
  sees two quests touching one.
- **Tasks:** parallel writers inside one quest (`campaign start ID --task T --files GLOBS`),
  merged into the quest branch by the game master. The quest is still reviewed once.
- **Increments:** `campaign land ID --upto COMMIT --reason R` lands a prefix that is safe alone
  (behavior-preserving refactors, tests, unused internal APIs, code behind a disabled switch, docs).
  Never early: bytes on disk or the wire, public API, user-visible behavior, or the part the
  review exists for.
- **Cross-repository work** is several quests (`--repo PATH`) ordered by `after`, with an
  integration job that pins every repository's commit (`--ref NAME=PATH@REF`). Landing is ordered,
  never atomic: each intermediate state must work.
- **Scope changes:** `campaign set ID ...`; superseded work is `status=dropped reason=...`.

## Direct path first

Only what decides the next step blocks it. A quest's **gate checks** (`--check`: the build, the
tests, and the evidence its risk requires) block its landing. Its **deferred checks** (`--later`:
other platforms, long soaks, full matrices, extended fuzzing, repeated confirm runs) are queued at
low priority on the landed code, never block anyone, and must be green before the milestone
closes. A red one is an error in `campaign check`: fix forward or revert, then
`campaign verify ID --deferred` reruns them on `main` and clears it. The evidence a high or
critical risk needs (TSan for concurrency, fuzzing for a decoder, the crash model for durability)
is always a gate; only its extended runs may wait. Code specific to a platform gates on that
platform. Slow CI workflows can be made non-gating the same way
(`campaign config ci_later=NAME,...`).

## Run a quest

1. `campaign start ID` creates `quest/ID` in its own worktree with a journal. Launch an adventurer
   with the "Adventurer" prompt (`references/prompts.md`); run quests in parallel only when they
   are ready, disjoint and not waiting on the same machine.
2. The adventurer works in its worktree, keeps its journal current, and produces evidence as jobs:
   `campaign job submit --quest ID --level L --minutes M -- CMD` and `campaign verify ID` (the
   quest's checks at its head). When only jobs remain, it records their ids and ends its turn.
3. The game master waits with one background `campaign wait --commits` (no tokens until a job
   ends or an adventurer commits), then resumes the adventurer by message while its context is
   small (about 150k tokens), or starts a fresh one with the "Resume" prompt.
   If a finishing agent does not wake it, it keeps its turn, blocking on the harness's agent
   wait while agents are out (`references/harness.md`).
4. When the journal says "ready for review", the game master sets `status=review`, launches the
   paladins the risk requires, commits each review file to `.campaign/<campaign>/reviews/ID-LENS.md`, and
   relays blocking findings to the adventurer.
5. `campaign accept ID LENS [--jobs J]` once the review approves, or after the game master confirmed
   each blocking fix by rerunning its probe as a job. Then `campaign land ID` and `campaign clean ID`.

## Scouting parties

When there is a list worth hunting, send a party of cheap scouts at once, one target each: the
losing rows of a fresh benchmark, a profile's hotspots, recently landed or never-checked code where
the proof is thinnest (count-only tests, untested error paths, boundaries), or a backlog of small
bugs. In 1.x this found most wins and old bugs: 33 explorer runs at a median of 93k tokens, 20 of
them landed.

1. Targets come from a fresh measurement or probe on current `main`, never from an old document.
2. One quest per target, areas disjoint (`campaign new hunt-<area> --risk low|normal --files
   <area>`), launched together with the "Scout" prompt. A scout that only answers a question
   needs no quest: it runs its jobs at `main`'s commit and reports.
3. Each scout confirms the problem on `main`, then fixes it with evidence or reports nothing,
   within about three experiments. Timing goes through jobs, so the party never fights over the
   machine.
4. A verified fix continues as its quest, finished by the same scout while its context is small
   and reviewed by risk; a cause without a fix goes to a stronger adventurer with the notes; an
   empty hunt is dropped with reason "nothing found". Two empty hunts in an area send the next
   party elsewhere.
5. Size the party by its targets and by what the game master can review and land, never by idle
   quota.

## Risk and review

| Risk (first match wins) | Before building | Before final landing |
|---|---|---|
| **critical**: irreversible (freezing a format or protocol for release, migrating users' data, removing user-visible behavior) | patron approval; a `design` review | the high-risk review, plus acceptance evidence the paladin ran itself |
| **high**: persistent formats, wire protocols, durability, concurrency, security surfaces, shared public APIs | a `design` review when a decision is expensive to reverse | one review by the lens of the dominant risk (`durability`, `security` or `correctness`) |
| **normal**: ordinary features and fixes | — | one `correctness` review, or `--waive REASON` for a small change with strong evidence |
| **low**: docs, tests, tooling, mechanical edits, behavior-preserving refactors | — | checks green; the game master reads the diff |

- **One paladin by default,** carrying every risk question the quest raises (the prompt names
  three to five). A second only for a named question that needs separate expertise or
  independence (a decoder inside a durability change), with the reason recorded.
- **Fixes are confirmed by reruns, not re-reviews.** A fix that adds a mechanism or changes an
  assumption gets a review scoped to the fix.
- **Two fix rounds, then stop.** A third means the quest or design is wrong: re-plan or ask the
  patron. Non-blocking findings become a follow-up quest (usually low risk) or are dropped with a
  reason in the review file; never another review round.
- **A design is reviewed once.** Settle disputed behavior with an executable model or test, not
  another text round. Decide before building only what is expensive to reverse; verify during
  building what a test can show; leave to the quest review what needs the whole change; leave to
  milestone validation what needs several quests.
- **System review on events, not on a calendar:** when `campaign check` reports two quests
  changing one file or one contract, ask one paladin (lens `system`) that specific question.
- **Yield:** `campaign status --reviews` counts verdicts and blocking defects. An optional review
  (normal risk) that finds nothing material over about five quests of a kind is dropped for that
  kind; high and critical reviews are never dropped by yield.

## Evidence

Proof is proportional to the claim (`references/review.md`): tests check values, not counts; a
fix has a test that fails without it; a claim has a mutant a test kills; sanitizers in separate
builds; an oracle or property test where one exists; the concurrency list for concurrency claims.
A mechanical edit needs its checks and CI. Performance claims follow `references/measurement.md`:
before and after at named commits, `exclusive` for numbers that decide anything, honest magnitude.

## Machine work

| Level | For | Isolation (`quiet`) |
|---|---|---|
| `none` | tiny single-threaded probes | no hold |
| `light` | builds, test suites, fuzzing | a few at once, nice 19 |
| `strict` | smoke timings, behavioral runs | own cores on Linux; alone on macOS |
| `exclusive` | numbers that decide or get published, cache-sensitive A/Bs | the machine to itself |

Jobs run at the commit they were submitted for, in their own checkout, so the adventurer keeps
editing; a commit that touches only `.campaign/` (a journal) keeps its evidence. A strict or
exclusive job lasts at most 30 minutes and is stopped at its deadline (the result is then
invalid); split longer work. Every process a job starts dies with it. A worker starts by itself
and exits when the queue is empty. Without a worker, `campaign job submit --sync` runs the job in
the foreground and records the same result. Mechanics: `references/host.md`.

## Integration

`campaign land` checks, in order: dependencies landed; the target is on the branch and `main`
changed nothing but `.campaign/` since it branched (otherwise rebase, `campaign verify`,
retry); no edits to the game master's files or outside the file set; every check passed on the
target's code; every required lens accepted for the current content (and, for high risk, no
upstream code change since); CI green when configured. It fast-forwards `main`, or merges when only
campaign metadata differs, verifies the landed code equals the tested code, records the landing,
and pushes only with `--push`. Each milestone in `PLAN.md` runs its full validation as a job on
`main` (an identical earlier result counts; `--force` reruns a check that depends on more than the
code); a red result is fixed or reverted before the next landing.

Other campaigns on the machine share the job queue, `quiet` and the quota. `campaign check` reports
their quests that overlap yours in a shared repository or change a contract you consume; order them
with `--after NAME:ID`, which landing enforces, and tell their game master (`campaign status --all`).

## Recovery

Every session, game master or adventurer, starts with `campaign status` (the quest log) and
`campaign check`, then reads the journal and `git log`. Observable state wins: git, worktrees and
job records over any document. A dirty worktree is kept, never discarded. An interrupted landing
is completed or cleared by the next state-writing command; a job whose runner died is requeued (if
it allows retries) or marked interrupted; a merged but unrecorded branch is recorded with
`campaign land ID --record COMMIT`. Never invent a missing decision: write it under "Blocked on"
and ask.

## Cost

- No minimum number of agents and no utilization target. `pace` reports quota: check it before
  launching agents in parallel, which multiplies the burn. `campaign usage` shows what each quest,
  role and class cost. Idle capacity is not waste.
- Prompts point at files and commits; final reports stay under ~250 words, the detail in the
  journal or review file.
- Report to the patron on events (a landing, a blocker, a decision); any periodic report is built
  from `campaign status`, never by waking agents. Inside cmux the tool and `quiet` keep the sidebar
  current by themselves (`references/host.md`); the game master adds `report notify` only for a
  decision the patron must make.

## The patron's authority

The patron decides goals and non-goals, significant scope changes, removing or narrowing
user-visible behavior (even to close a security finding), reserved public APIs and formats,
commitments to other teams, new external services or repositories, and anything that spends
money. A decision request gives the decision, the facts, the options with tradeoffs, a
recommendation, and what deferring costs, in plain words. An action the permission system denied
an agent stays undone until the patron authorizes it.

## References

| File | Read it |
|---|---|
| `references/prompts.md` | before launching an adventurer, scout or paladin |
| `references/classes.md` | the method of the class an adventurer is sent as |
| `references/review.md` | before writing tests for a claim, reviewing, or judging proof |
| `references/measurement.md` | before planning or judging any benchmark |
| `references/host.md` | for jobs, `quiet`, build slots, remote hosts and failure behavior |
| `references/harness.md` | for Claude Code, Copilot CLI and Codex specifics |
| `references/lore.md` | before changing a rule: the incidents behind them |
| `references/migration.md` | when resuming a project started under campaign 1.x |
