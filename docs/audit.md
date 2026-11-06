# Campaign 1.x audit

> Status: CURRENT (2026-11-05). Audit of `home/.agents/skills/campaign` at `dotfiles:ccde58b`,
> its 14 agent types, its scripts, its git history (68 commits since 2026-10-17), and the
> records of four projects run with it (strata, Detent, Xapiand, cpp-libraries, read at their
> `main` on 2026-11-05). Numbers come from those records unless marked as estimates.

## What was audited

| Part | Size |
|---|---|
| `SKILL.md` | 555 lines, 6,888 words |
| `references/` (11 files plus an IAM policy) | 1,290 lines of markdown, 12,071 words |
| `README.md` | 240 lines |
| Agent types (`home/.claude/agents/`, 14 files; Copilot copies generated) | 544 lines, 6,183 words |
| Scripts: `quiet.sh` 450, `lease.sh` 641, `pace.sh` 238, `blockers.sh` 136, `report.sh` 65, `lease-grant.sh` 53, `sync-agents.sh` 49 | 1,632 lines |
| Operator surface | 15 roles, 6 host tools, ~20 document kinds, ~10 finding-ID schemes |

## What campaign does well

These are proven by the records and survive into 2.0.

1. **State in the repository.** Ledgers, designs and reviews in git let a fresh session resume.
   Detent's salvage of 13 archived heads (47 commits) found nothing lost
   (`Detent/docs/status/salvage-2026-11-03.md`).
2. **Fresh-context reviewers that build and probe.** Reviews of new concurrency and durability
   code found material defects nearly every time: a use-after-free and lost wake-ups (Xapiand
   pool), a nested-wait deadlock (WorkPool), rollback truncating records live readers hold (write
   buffer item 4), a rejected transaction moving the read position (shared reader views), and a
   durability protocol REJECT (commit barriers). These are the reviews worth paying for.
3. **Criteria agreed before building, never edited by the builder.** Matches Anthropic's published
   harness work and stops quiet weakening of tests.
4. **Proof standards.** Values not counts, a revert check per fix, a dying mutant, sanitizers in
   separate builds, oracles and property tests, the concurrency list. Several escapes in the
   calibration logs are exactly the cases these rules name.
5. **Measurement discipline.** Smoke, screen, confirm; run-level A/B intervals after an A/A;
   checking the binary's architecture; claims tied to a commit.
6. **`quiet`.** A real, small mechanism: atomic `mkdir` lock, dead holds reaped by pid and
   deadline, core partitioning on Linux and FreeBSD, honest exclusive on macOS, FIFO strict
   requests, disk and load gates. It works for every agent on the machine, campaign or not.
7. **Owner authority over user-visible behavior.** Born of a real audit that found 19
   user-facing changes agents had decided alone.
8. **`lease`.** Well engineered in the end (four independent stops, proven on a t4g.nano), but
   it arrived after money was lost and still depends on agents calling it correctly. 2.0 removes
   it from campaign (see "Classification").

## Where it spends without return

Measured on strata's exploration log (`strata/docs/perf/exploration-log.md`, 2026-10-20 to
11-04, 133 runs with token counts, ~30.6M tokens including cached context):

| Role | Runs | Tokens | Median |
|---|---|---|---|
| developer | 49 | 12.2M | 240k |
| code review | 16 | 3.3M | 210k |
| explorer | 27 | 2.9M | 93k |
| performance engineer | 8 | 2.4M | 297k |
| architect review | 7 | 2.1M | 236k (max 552k) |
| recheck | 9 | 1.1M | 134k |
| designer | 5 | 0.8M | 170k |
| rows mixing roles | 5 | 4.6M | 783k |

1. **Rechecks confirm, they rarely find.** Every dedicated code recheck in the log reported the
   named findings closed ("all six CLOSED", "SR1-SR4, CR2, CR3 closed", "L1, L2, C1-C4 CLOSED"):
   ~1.1M tokens to confirm what rerunning the reviewer's probes would have shown.
2. **Design text spirals.** Design re-checks keep finding new blocking items (18 of 27 re-check
   sections that list them): `durability-modes-design.md` ran 7 re-checks (3,554 lines, findings
   to DM-R49), `format-13-design.md` 6 (2,817 lines). The cold-tier design spent ~1.27M tokens
   before a line of code. Prose about a mechanism can be re-reviewed forever; an executable model
   settles it (Detent's namespace-delivery model found 14 violations in 283 crash cases).
3. **One step, many agents.** Write-buffer step 1 items 2-3 took developer 333k + verification
   103k + design revisions 358k + architect re-check 234k + code review 285k + fixes 189k +
   recheck 165k ≈ 1.67M. WorkPool took nine agents and 1.83M. The defects found justified some
   of it; the handoffs and re-reads did not.
4. **Explorer to developer.** The developer "reruns its evidence first" by rule, so every
   explorer win is reproduced by a second context before it is finished.
5. **Reviews outweigh the work.** strata's `docs/` holds 77,646 lines; 36,149 (47%) are review
   files, more than its designs (18,640). Plain approval is rare (2 of ~92 verdicts); 64 were
   "APPROVE WITH CONDITIONS", so nearly every review buys a condition round.
6. **Agents wait on the machine.** On the cpp-libraries gate, 30-40 timing holds queued at once,
   strict holds waited up to 2.7 h and builds 1-7 h (`references/cloud-workers.md`). A Detent
   re-check's tiny Python probes waited ~2.5 h behind other agents' exclusive holds
   (`Detent/docs/reviews/namespace-blocks-preliminary-2026-11-04.md:102`). An agent that waits
   either holds a turn open or polls (`sleep 300; tail LOG` in Copilot), and each poll rereads
   its whole context.
7. **Quota pacing manufactures work.** `pace advise` targets 95% of each 5-hour window and 97% of
   the week, floors the agent count at 2, and SKILL.md says "Never idle: while quota remains, keep
   at least two agents on meaningful work". Utilization is the objective; outcomes are not.
8. **The lead does clerical work.** It maintains the status index, an exploration-log row per run,
   a document index, status lines, the book outline, a critical-path pill, two-hourly reports,
   and a quartermaster agent for chores. Every one of these is derivable or scriptable.
9. **Documentation by rule.** A how-it-works note per mechanism, a status line on every document,
   a daily document audit by a fresh agent, a gate report per phase, a calibration log every
   reviewer reads. cpp-libraries' `LESSONS.md` reached 3,585 lines.

## Conflicting instructions

Each is resolved by a change of mechanism in 2.0, not by another exception.

| # | Conflict | Resolution in 2.0 |
|---|---|---|
| C1 | "Gates decide what lands, never what starts" against "no build ahead on work a pending review will change" | Two dependency kinds: `needs` (must land before starting) and `after` (must land before landing). Starting early is allowed only against a stated contract. |
| C2 | Reviews are risk-based, yet a sensitive row's review "always runs", a proof auditor runs before every review, and system reviews run at every gate | One risk rule picks the lenses; system review fires on detected events (shared contract, file overlap), not on a calendar |
| C3 | "Never throttle how many agents run" against "bound fan-out by the host" and "launch only what can run now" | Agents follow ready packages; machines follow the job queue. They are throttled separately. |
| C4 | "Never idle: keep at least two agents" against "never launch work to have something to report" | No minimum. Pacing advice is deleted. |
| C5 | Quota should be spent efficiently, yet pace targets 95-97% use | Quota is reported, never targeted |
| C6 | A hold is "at most 30 minutes", yet at its deadline "the command runs on unheld" | `quiet` ends a strict or exclusive command at its deadline (exit 124) and refuses longer holds; the job is recorded as timed out |
| C7 | The rechecker's agent file says "use it for every re-check of code fixes"; SKILL.md says only for a new mechanism | No rechecker. A fix is confirmed by rerunning the reviewer's probes as jobs; a new mechanism gets a scoped review |
| C8 | Hand off at ~250k (SKILL.md) against "past ~300k, finish up to ~450k" (Contributor prompt) | One lifecycle rule in one place; prompts state no thresholds |
| C9 | README's role table (sonnet everywhere) against SKILL.md (opus for judgment, owner ruling 2026-11-04) | One model table, in SKILL.md only |
| C10 | Explorer: "launch many" against "timing explorers one at a time" | No explorer type; measurements are jobs, serialized by the queue |
| C11 | "Rent before queuing for hours" against the money lost to forgotten machines | Campaign provisions nothing |
| C12 | Two-hourly reports "ask running agents for status" against "never message an agent past 150k" | Status is derived from git and the job queue by `campaign status`; no agent is woken to report |
| C13 | "Do the small things yourself" against "keep the lead's context small" and "hand chores to the quartermaster" | Chores are commands (`campaign land`, `clean`, `wait`); the lead runs them in one line |
| C14 | Reviewers "verify by building and running" against "don't rebuild the matrix", with no way to reuse the builder's results | Jobs are keyed by commit, command and host; a reviewer's identical job returns the builder's result. Reviewers spend on probes. |
| C15 | Design reviews "only at key points" against design re-checks after every revision (7 rounds observed) | A design is reviewed once; a revision is re-reviewed only when it adds a mechanism. Disputed behavior is settled by an executable model or test, not another text round. |
| C16 | "No hold for correctness runs" against the global rule "run builds and tests under `quiet light`", which queues tiny probes behind exclusive holds | A job may run at level `none` (no hold) for trivial work; nobody waits in any case |

## Classification

Preserve: fundamental and useful. Simplify: worth it, too complicated. Consolidate: duplicated.
Automate: a tool should enforce it. Optional: some projects. Remove: cost exceeds benefit.

### Roles

| Mechanism | Class | Why, and what removal costs |
|---|---|---|
| Lead as single writer of `main` and state | Preserve, Automate | Parallel writers make conflicting implicit decisions (Cognition; principle 1). State changes go through `campaign`, which refuses to write outside the home repository's main checkout. |
| Explorer, developer, validator, performance engineer, researcher, designer, maintenance engineer, tech writer | Consolidate into **builder** | They differ in assignment, not authority, independence or tools. The assignment and model are chosen per launch. The explorer's budget rule moves into the measurement reference. |
| Code reviewer, security reviewer, architect | Consolidate into **reviewer** with a lens | Same authority (one review file, no code), same independence. The lens (correctness, security, durability, design, system) carries the different instructions. |
| Rechecker | Remove | ~1.1M tokens confirmed closures. Rerunning the reviewer's kept probes as jobs is deterministic. A fix that adds a mechanism gets a scoped review instead. |
| Proof auditor | Remove | An agent deciding whether to launch an agent. Job records show which checks ran at which commit; the reviewer judges sufficiency. |
| Quartermaster | Automate | Its chores (merge after green, CI watch, index rows, cleanup) are `campaign land`, `campaign wait`, `campaign status`, `campaign clean`. |
| Area leads past 6-8 workstreams | Optional | One sentence |

### Workflow and review

| Mechanism | Class | Why |
|---|---|---|
| Stage-1 review of the plan | Preserve | The one review to wait for, when the architecture is consequential |
| Done criteria set by the lead, never edited by builders | Preserve, Automate | `campaign land` refuses a branch that edits lead-owned state |
| Preliminary design review at key points | Simplify | Only for decisions expensive to reverse; once; settled by models and tests |
| Review tiers (floor table, adjust table, defaults, five rules) | Simplify | One four-row risk rule with lenses |
| Per-branch code review | Consolidate | One review per package (the coherent outcome), not per branch or step |
| Phase gates, gate reports, gate reviews | Simplify | A milestone is a package whose acceptance runs the full validation as jobs on `main` |
| System review at each gate and whenever 3+ designs are in flight | Simplify | Event-triggered: `campaign check` reports overlapping files and shared contracts |
| Proof standards and the concurrency list | Preserve | Proportional to risk; not ritual for mechanical edits |
| Calibration log read by every reviewer | Simplify | A short lessons file, pruned |
| Review yield; drop a review type with no yield in ~5 runs | Preserve, Simplify | Verdicts and blocking-finding counts are read from review files by `campaign status --reviews` |
| Explore-first workflow | Simplify | Kept as **sweeps**: a swarm of cheap explorers on a target list, each target its own package, finishing its own fix; no mandatory explorer → developer handoff |
| Hunts behind the landings | Preserve (as sweeps) | The best yield in the records: 33 explorer runs (29 on Sonnet at medium), median 93k tokens, 20 landed or merged; one day of hunting found three bugs from 2019. The schedule obligation goes; the mechanism stays. |
| How-it-works note per mechanism, book outline | Optional | A criterion the lead adds for substantial mechanisms only |
| Status lines on every document, `INDEX.md`, daily document audit | Remove | State carries status; designs keep a status line; the audit agent goes |
| Per-workstream ledger plus status index plus exploration log | Consolidate, Automate | `state.json` (lead, through the tool), package brief, work file (builder, on its branch), git history; the index is derived |
| Two-hourly report with a critical-path pill | Simplify | Event reports; any periodic report is generated from `campaign status` |
| Finding IDs, owner-facing naming | Simplify | Keep "say it in words to the owner"; IDs only inside review files |
| "Move fast" mode | Simplify | One line: the lightest review the rule allows, never below the floor |

### Cost controls

| Mechanism | Class | Why |
|---|---|---|
| Resume while the context is small (~150k), else start fresh from the files | Preserve | Measured: a repin cost 74k fresh against ~500k resumed; one message resumed a 556k agent |
| Scope a launch to one landable step, hand off at ~250k | Simplify | One lifecycle rule; packages give the scope |
| Short prompts and reports | Preserve | |
| Experiment budget (~5, stop at the third loser) | Preserve | In the measurement reference |
| Opus for judgment, Sonnet for mechanical (owner, 2026-11-04) | Preserve | A model table by assignment |
| `pace advise`, floor of 2, 95/97% targets, "never idle" | Remove | Manufactures work |
| `pace` window reporting | Simplify | Passive report only |

### Host and machines

| Mechanism | Class | Why |
|---|---|---|
| `quiet` light, strict, exclusive | Preserve | Works on every platform it claims |
| Hold of at most 30 minutes; command continues unheld at the deadline | Automate | `quiet` kills a strict or exclusive command at its deadline and refuses longer requests |
| Agents waiting on holds, builds and CI; polling | Automate | Job queue with immutable sources, a self-exiting worker, and one `campaign wait` for the lead |
| Bound every helper (process groups, traps) | Automate | Jobs run in their own process group and are killed with it |
| `blockers --watch` | Optional | Useful across several hosts; not part of the core |
| Split macOS hosts by role; VMs; remote hosts | Optional | Host reference |
| Shared ccache | Preserve | |
| `lease`, `lease-grant`, cloud-workers, IAM policy, "rent before queuing" | Remove | Natural-language safety already failed with money. Deferred capability with external controls. |

### Integration and authority

| Mechanism | Class | Why |
|---|---|---|
| Worktree and branch per writer | Preserve, Automate | `campaign start` |
| CI triggers for every agent prefix | Simplify | One prefix, `pkg/` |
| Green from a fresh environment, every CI platform | Preserve | Two calibration escapes were platform-only |
| Rebase onto `main`; build after merging several | Automate | `campaign land` fast-forwards only a rebased head that carries the evidence |
| `explore/` branches land notes, not code | Remove | No explore branches |
| Worktree cleanup with safety checks | Automate | `campaign clean` |
| Git and shell traps | Simplify | Lessons archive; the tool avoids most |
| Owner rules on narrowing user-visible behavior | Preserve | |
| Never launder a permission | Preserve | |
| Decision requests in plain words with a recommendation | Preserve | |
| Remove a harness part at each gate (principle 7) | Simplify | Prune by measured yield |
| One shape everywhere (owner's Zen) | Preserve | A reviewer check |
| Peer sessions | Simplify | Library owner edits; shared tooling has one writer |
| Other teams' code (generated patches, register) | Optional | Lessons archive |
| Copilot agent files generated from Claude's | Preserve | Two files instead of fourteen, plus Codex |

## Representative workflows

Agent counts follow the 1.x rules; token figures are medians from the strata log. Totals are
**estimates** built from those medians, not measurements of single runs.

| Workflow | 1.x agents and reviews | Est. 1.x tokens | 2.0 path | Est. 2.0 tokens |
|---|---|---|---|---|
| 1. Small non-sensitive fix found by measurement | explorer (93k) → developer reruns evidence and finishes (~115k) → lead skim; 2 contexts, 0 reviews | ~210k | one builder investigates and fixes; checks as jobs; lead reads the diff | ~120k |
| 2. Medium feature, 3 steps, shared API | per step: developer (240k), proof auditor (~60k), code review (210k), fix round (~110k); 12 agents, 3 reviews | ~1.86M | one package; builders per task (3 × 240k), one integrated review (~300k), one fix round (110k) | ~1.13M |
| 3. Performance optimization | lead benchmark → explorer (93k, strict holds, waits) → developer reruns and finishes (240k) → perf engineer if stuck (297k) → code review (210k) → lead gate measurement; agents wait hours on holds | ~550-850k | builder profiles, submits screen and confirm jobs, ends its turn while queued, resumes on results; review if risk is normal or higher | ~350-560k |
| 4. Concurrency or durability change | designer (170k) → architect (236k, up to 552k) → revisions and re-checks (2-7 rounds, 100-234k each) → developer (300k) → code or architect review (285k) → fixes (190k) → recheck (134k) | ~1.7-2.4M (observed 1.67M and 1.83M) | design note with an executable model → one design review → builder → one durability review → fixes confirmed by rerun jobs | ~1.2-1.3M |
| 5. Persistent format or protocol | as 4, plus a decoder-safety review (114-162k) and a freeze-readiness review (552k); format 13 took 6 re-check rounds | ~2-3M | design review once; security lens at landing on fuzz evidence; one critical review at the freeze | ~1.4-1.8M |
| 6. Multi-package architectural change | per-design architect reviews, a system review at each gate and whenever 3+ designs are live (~250k each), gate reports and gate reviews | +0.5-1M per phase | contracts declared per package; `campaign check` reports conflicts; a system review only on an event; milestone validation as jobs | +0.25M per real conflict |

The repeated reading in 1.x comes from three places: each handoff (explorer → validator →
developer → auditor → reviewer → rechecker) rebuilds the same context; each review round rereads
the design and earlier reviews; and the lead rereads ledgers, indexes and reports on every wake.
Every workflow above keeps at least the reviews that found material defects in the records: the
first review of new concurrency, durability, format and security code.

## Incidents worth keeping

The compact archive is `home/.agents/skills/campaign/references/lessons.md`. Prevention moves
from prose to mechanism wherever a mechanism exists.

| Failure | Cause | Invariant | Prevention in 2.0 |
|---|---|---|---|
| Idle bare-metal leases ran for hours, hundreds of dollars | Termination depended on an agent remembering | No unattended billable resource | Campaign provisions nothing; cloud is a deferred capability with external expiry |
| Orphaned load generator held a workstation at load 87, killed every agent | Helper outlived its caller | Every process a job starts dies with it | Jobs run in their own process group with a hard timeout; the reaper kills orphans |
| Builds outran 20-minute holds; agents asked for 4-hour holds | The deadline did not stop the command | A measurement runs only inside its isolation | `quiet` kills strict and exclusive commands at the deadline and caps their length |
| Measurement under a compile; Rosetta binaries timed as arm64 | No isolation, unchecked binary | Authoritative numbers need isolation and identity | Exclusive jobs on immutable commits; the measurement reference keeps the `file` check |
| A critical-path step sat unlaunched a week | Nobody saw it was ready | Ready work is visible | `campaign status` lists ready packages |
| Agents narrowed 19 user-visible behaviors alone | Reviewers ruled on behavior | The owner rules on user-visible behavior | Kept as a rule; the review template says "offer options" |
| A gate passed on counts while values were wrong | Count-only tests | Tests check values | Proof standards in the review reference |
| A branch missing a landed sibling broke `main` after a clean merge | Merge without build | `main` is built at the landed commit | `land` only fast-forwards a rebased head that carries the evidence |
| A system review read a stale `origin` | Reviewer used remote refs | Reviews name exact commits | The review prompt names the commit; status reads local refs |
| A message resumed a 556k agent | Resume regardless of size | Resume only small contexts | Lifecycle rule |
| A failed push hidden in a pipe deleted a remote branch | Exit status lost | Check each push's status | `land` pushes only when told and checks the exit status |
| Platform-only failures (libc++ `jthread`, Linux-only TSan race) | Local evidence stood for all platforms | Evidence names its platform | Job records carry the host; CI on every platform before landing |
