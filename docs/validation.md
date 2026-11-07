# Campaign 2.0 validation

> Status: CURRENT (2026-11-06). What was run, what it showed, and what was not run. Hosts: Apple
> M4 Pro (14 cores, macOS 26.6), git 2.50.1, Python 3.9.6 and 3.15, shared with other agents; and
> a Linux development machine (x86_64, 4 cores with SMT, kernel 5.15, git 2.40.4, Python 3.9.19
> and 3.15). Commit hashes are `dotfiles:` unless named.

## Static checks

| Check | Result |
|---|---|
| `ruff check` and `ruff format` (skill's `ruff.toml`) on every tool in `scripts/` and the tests | clean |
| `pyrefly check` (strict preset, Python 3.9 target) | 0 errors |
| `shellcheck -S warning` on `install.sh`, the only shell script | clean |
| `sync-agents.py --check` (Copilot and Codex files match the sources) | clean; the Codex TOML parses with `tomllib`; output byte-identical to the shell version's but for its mark |

## Executable tests

`home/.agents/skills/campaign/tests`, run with `python3 run.py` from that directory (or
`python3 -m unittest`, one test at a time). Each test builds throwaway repositories with their own
job queue and `quiet` directory.

| Suite | Covers |
|---|---|
| `test_campaign` | quest creation and starting, progress from criteria and evidence, dependency kinds and cycles, the single-writer guard, status transitions, ownership at start and at landing, edits to the game master's files, file overlap reports, evidence at the exact code (and kept across journal-only commits), failing checks, landed code equal to tested code, rebases after `main` moves, review acceptance (approve, changes with confirming jobs, reject, waivers, drift, changed quest file, upstream changes for high risk), review counts, increments, the GitHub CI gate (stub `gh`), `--push` to a bare remote, the cmux sidebar (the real `report`, stub `cmux`), interrupted landings (completed and cleared), deleted and unrecorded branches, contracts with two providers, cleanup |
| `test_jobs` | identity (the submitted commit while the branch moves on, reuse, toolchain fingerprint, refs into other repositories, `--sync`), scheduling (`--after`, skips, priority, the worker's lifetime), limits (no-hold timeouts, measurement deadlines, the 30-minute cap, helpers killed with their job, cancel), crashes (lost runner reaped with its orphans and retried, interrupted without retries, a result recorded before the runner died, a stale runner fenced off), contention (Scenario G) |
| `test_quiet` | deadline stop and exit code 124, waiting requests in `quiet list`, exit codes passing through, the 30-minute cap, the hold outliving a command that ignores TERM, a starving build served before a queued measurement, bounded commands under a CPU burner per core, a TERM to `quiet` stopping its command's group and freeing the hold, `QUIET_PAUSE` stopping light work and resuming it; on Linux, strict and light holds on disjoint cores and qemu vCPU threads moved and restored (fake VMs with `CPU n/KVM` threads) |
| `test_scenarios` | Scenarios A-F, H, I from the brief and a scouting party, end to end |
| `test_campaign.Campaigns` | several campaigns on one machine: unique names, re-registration on the next write, overlap and contract warnings both ways, `status --all`, a `NAME:ID` dependency enforced at landing, and a landing into a shared repository waiting for its lock (5 of 5 mutants failed) |
| `test_campaign.Usage`, `test_budget` | `campaign usage` on Copilot's event shapes: runs attributed by the launch prompt (adventurer, resume, paladin), other campaigns and unrelated agents left out, logs older than the campaign skipped, game master sessions counted only when they ran its agents, jobs' run and wait time; the instructions' word budget |
| `test_campaign.SharedHome`, `test_campaign.Migration`, `test_agents` | two campaigns in one home: the main checkout asks which, a quest's worktree knows, the same quest ID gets `quest/demo/fix` and `quest/perf/fix` (fails with branches named `quest/ID`), each game master's wait sees only its own campaign's commits (fails with one wait record per home); a campaign in the older `docs/campaign/` layout running in place, `campaign migrate` refused while a quest is in flight and then moving it in one commit, evidence from before the move reused after it (fails with `code_tree` excluding only `.campaign`); generated agent files keep the effort, files from the shell `sync-agents` are replaced, hand-written ones kept (fails without the old marks) |
| `test_campaign.DirectPath`, `test_jobs.Scheduling` | deferred checks: landing waits only for gates, `land` queues the rest on the landed code at priority -1, a red one is an error until `verify --deferred` reruns it green, the 10-minute cap on deferred measurements, non-gating CI workflows, deferred work never taking every light slot |

Result, both suites at once on the shared host (the condition that exposed two bugs below):
see "Final run".

### Scenarios

| Scenario | Test | What it shows |
|---|---|---|
| A small bug fix | `test_a_small_bug_fix_needs_one_adventurer_and_no_paladin` | low risk lands on green checks with no lens and no review file |
| B medium feature | `test_b_a_medium_feature_gets_one_integrated_review` | two parallel tasks with disjoint files, an overlapping third refused, one review for the quest |
| C performance | `test_c_performance_work_measures_exact_commits_without_a_waiting_agent` | screen (strict) then confirm (exclusive) chained with `--after`; both commits recorded; the branch moves on while queued; the game master's one blocking wait |
| D security | `test_d_a_security_surface_needs_its_own_lens_and_no_waiver` | the security lens is required and cannot be waived; CHANGES needs a confirming job |
| E durability | `test_e_durability_gets_an_early_design_review_and_one_code_review` | design accepted before the code exists and kept after implementation; two reviews in total, no recheck |
| F interrupted campaign | `test_f_an_interrupted_campaign_is_reconstructed_from_files` and the live recovery test below | journal state, dirty worktree and job state reconstructed; resubmission reuses the pending job; the job's lost runner is reaped and retried; uncommitted work kept |
| G shared host | `test_jobs.Contention`, `test_quiet` | two builds and an exclusive benchmark never overlap; nobody polls; starving builds get a turn |
| H interacting quests | `test_h_interacting_quests_are_ordered_and_integrated` | contract change reported; an integration job pins both repositories' commits; `after` orders the landings |
| I cloud attempt | `test_i_campaign_has_no_way_to_rent_machines` | no provisioning command in the skill or tool; `lease` gone from `links.tsv` and from the tool's help |
| Scouting party | `test_a_scouting_party_hunts_disjoint_areas_in_parallel` | overlapping hunt areas refused; two strict measurements serialized; one hunt lands, one is dropped "nothing found" |

### Mutation check

Each key mechanism was broken once in a copy of the skill (`~/.scratch/campaign2/mutants.py`),
and the named test was run against it. **14 of 14 mutants failed their test** on the final code:
the sweep after a job, attempt fencing, the worker staying while jobs run, ownership at start,
scope at landing, drift voiding acceptance, upstream changes voiding high-risk acceptance, a
failed check blocking landing, landing-intent recovery, the reaper killing orphans, evidence keyed
by code, `quiet` stopping measurements, the hold's grace, and starvation fairness. Two mutants
first survived, each exposing a weak test (and in one case a real defect, below); both tests were
fixed.

The direct-path mechanisms got their own mutants (`mutants_later.py`): **8 of 8 failed their
test**: landing waiting on deferred checks, `land` not queuing them, `check` hiding a red one,
`verify` running everything, the rerun not recorded, no cap on deferred measurements, `ci_later`
ignored, and deferred work taking every light slot. `campaign usage` got six: **5 of 6 failed
their test**; the survivor guarded a case no prompt produces, and the guard was removed.

## Defects found by validation

| Defect | Found by | Fix | Evidence of the fix |
|---|---|---|---|
| `quiet`'s timer subshell, TERMed right after forking, could still create the expiry marker, so starved commands were reported past their deadline (13 of 16 trivial jobs under CPU load) | running both suites in parallel; reproduced with a CPU-burner script | SIGKILL the timer; decide expiry by the clock | 16 of 16 done under the same load; `test_bounded_commands_finish_normally_under_cpu_load` fails on the old `quiet` (4 of 8 killed) and passes now |
| The runner wrote no heartbeat while sweeping leftovers or retrying a locked `git worktree add`, so under load the reaper killed a live runner | the parallel run | heartbeat touched in both | the parallel runs since |
| Committing only the journal moved the head and voided identical evidence | the fresh-agent recovery test | jobs keyed by the commit's tree without `docs/campaign/` | `test_campaign_metadata_commits_keep_the_evidence` |
| `code_tree` asked git for `X^{tree}^{commit}` on repositories without `docs/` | the 3.15 run | `rev-parse X^{tree}` directly | `test_refs_check_out_other_commits_and_repositories` |
| An early `design` acceptance would be voided by the implementation that follows it | writing Scenario E | design acceptance pinned to the quest file only | Scenario E |
| The orphan test passed without the reaper | the mutation check | the first attempt hangs | the mutant now fails |
| A starving build wrote its marker only after winning `quiet`'s directory lock, which a fast-polling measurement held most of the time, so fairness depended on timing (the real code passed 1 of 3) | the mutation check, after a mutant of one half of the rule survived | the marker is written before taking the lock | 5 of 5 passes; the one-sided mutant fails 3 of 3 |
| A job submitted `--after` one job reused an identical job queued `--after` another, inheriting its order and its skip when that one failed | the test suites run in parallel under other sessions' load (a slow worker leaves the first job queued) | a job that runs after others is found by its key and the jobs it follows | `test_a_pending_job_is_reused_only_when_it_runs_after_the_same_jobs`, which fails on the old lookup |
| bash printed "Killed: 9" when the timer was stopped, into every bounded command's stderr | a manual trace | the timer is disowned | a strict run prints nothing extra |
| On Linux a process group whose leader is an unreaped zombie stays signalable, so after every timeout or cancel the runner's `kill_group` waited its full 20 s without a heartbeat and the reaper took the live runner for dead (`interrupted`); macOS answers `EPERM` for such a group, so it never showed there | the first Linux run (2 of 91 failing) | the leader is reaped while polling, and the heartbeat touched | `test_a_no_hold_job_is_stopped_at_its_timeout` and `test_cancel_stops_queued_and_running_jobs` fail on Linux without it and pass with it |
| The no-hold timeout test backdated `began` as soon as the file existed, but the shell creates it before `date` writes it, and that write reset the mtime | the Linux runs (2 of 6) | the test waits for the content | 10 of 10 |

## Live agent tests

Two fresh agents, launched with no context but a prompt, in GitHub Copilot CLI with its default
model, on throwaway repositories. The first two ran before the theme rename (with the words
package, builder, reviewer and work file); the third ran the themed skill with classes.

### Recovery (brief §11.3)

Fixture: quest `ttl` (normal risk) with two commits on its branch, an uncommitted half-written
test, a job whose runner was SIGKILLed mid-run (`--retries 1`), and an open patron decision (Q1)
the quest file says not to make. The agent got only the Resume prompt and the skill's path.

| Check | Result |
|---|---|
| Reconstructed goal, criteria, file set, branch, base, uncommitted work, jobs, open decision | yes, in **8 tool calls** before its first change; 179 s in total |
| Kept the uncommitted work | finished the draft test rather than discarding it |
| Duplicated machine work | none: the killed job was requeued once by the reaper and not resubmitted |
| Decided Q1 | no: recorded under "Blocked on", with the observation that `ttl=0` currently expires only by accident |
| Edited the quest file or state | no (file hash unchanged; `campaign check` clean) |
| Evidence | the unit check at the head, two mutants killed (as jobs), an attribution job showing the check command was broken on `main` |

It also found a bug in the fixture (the check command could never pass) and handled it inside
its file set, recording the ruling. Its five findings about the skill are folded in (see the
defects table and `dotfiles:a2026dd`).

### First-time game master

Fixture: a repository with three goals (fix whitespace word counting, fix a README typo, add
`top_words`). The agent read only `SKILL.md` (and the references it pointed to) and ran the
campaign.

| Check | Result |
|---|---|
| Completed | three quests landed (low, normal, low), a milestone check on `main`, `campaign check` clean; 694 s |
| Risk calls | typo low (no review), feature normal (one correctness review), a test follow-up low |
| Subagents | one reviewer (Opus, high); no unnecessary agents |
| Patron questions | none needed; it decided two `top_words` semantics itself and recorded them as decisions |
| Friction | six items: a mid-run tool change (mine), journal commits forcing reruns (fixed), a reused job hiding its label, `wait` not reporting finished jobs, invariant 7's wording, three unclear rules; all fixed |

### Themed game master

Fixture: a `dedupe` library with a quadratic `unique()` and a README to correct. The agent read
only `SKILL.md`, ran the campaign and was asked whether the vocabulary blurred any instruction.

| Check | Result |
|---|---|
| Completed | two quests landed (`unique-fast` normal as an Artificer, `readme-guarantees` low as a Bard, written by the game master itself), a milestone validation on `main`, `campaign check` clean; ~50 min, on a host at load 81 of 14 CPUs |
| Evidence | exclusive confirm job on `main`: 49.34 s to 5.72 ms at 200k items, A/A control within 0.1%; one correctness paladin: APPROVE, 0 blocking, 5,029 cases compared with the old code, 9 mutants |
| Vocabulary | role names clear; two collisions: "class" (adventurer class and the review table's finding class) and "risk" (quest risk and a finding class). Fixed: the review column is now Kind (defect, gap, nit) |
| Usage (`campaign usage`, read later from the session's logs) | 4 agent runs: the Artificer 2.0M tokens (cost 360), the paladin 2.1M (330), two fresh resumes 1.9M (420, more than the build, because `write_agent` could not resume it); 24 jobs ran 20 min and waited 26 min at load 81 |
| Friction | `write_agent` refused a non-background agent, so resumption was a fresh adventurer (harness.md says so now); `quiet list` hid waiting requests (now listed); `status` counted rejected candidates and probes meant to fail on a landed quest (closed quests now show only queued and running jobs) |

## Comparison with 1.x

A live A/B of the same tasks under both versions was not run: it would cost several million tokens
per scenario to get a sample too small to generalize. What can be stated:

| Measure | 1.x | 2.0 | Source |
|---|---|---|---|
| Instruction text agents read (skill, references, agent types) | 25,142 words | 10,284 words (59% less) | `wc -w` on both branches |
| Agent types | 14 | 2 | files |
| Roles | 15 | 4 (patron, game master, adventurer, paladin) | SKILL.md |
| Code shipped | 1,632 lines of scripts | 3,076 lines of scripts (the tool replaces procedure; `lease` removed) | `wc -l` |
| Tests | none | 75 unit and scenario tests | tests/ |
| Recovery from a lost session | "costs a resume prompt" (claimed) | 8 orientation calls, nothing lost or duplicated (measured once) | live test |
| Per-workflow tokens | see `audit.md`, "Representative workflows" | 30-45% fewer on medium and high-risk workflows | **estimates** from strata medians, not measurements |
| Rechecks per review | 1.x rule for new mechanisms; 9 runs, ~1.1M tokens in two weeks | none (deterministic reruns) | strata log; SKILL.md |
| Agents waiting on holds | hours (2.5 h and up to 7 h observed) | none by construction; jobs queue, agents end their turn | records; tests |

## Final run

On the shared host, each under `quiet light` (so at the lowest CPU priority). The last code
commit is the load test's cleanup; the record commits after it change only this directory.

| Interpreter | Tests | Result | Wall time |
|---|---|---|---|
| Python 3.9.6 | 75 | OK | 364 s |
| Python 3.15 | 75 | OK | 455 s for 68 (`test_campaign`, `test_jobs`, `test_scenarios`, `test_agents`, `test_budget`), 88 s for `test_quiet`'s 7 |

The run before it, with both interpreters at once while other sessions held the load at 44 to 90
on 14 CPUs, failed two timing tests on 3.9 (a job ordered `--after` another was skipped, and
the CPU-load test timed out) and the CPU-load test on 3.15. At the lowest priority, the runners'
heartbeats starved past the tests' 10-second stale threshold (production uses 60 s). Both
tests passed 3 of 3 times at a load of 90 when run without `nice`. The CPU-load test also left
its `quiet` requests running when it timed out, which held the suite's output pipe open after it
finished; it now kills them.

One line of stderr in the 3.9 run, `child setpgid (N to N): Operation not permitted`, is bash's
job control in `quiet`'s `bounded()`: it sets the group of a child that has already exited. The
child sets its own group, so nothing is affected.

## Linux run and the Python `quiet`

`quiet` moved from bash to Python (`scripts/quiet.py`) with the same commands, exit codes, hold
files and environment, and the suite then ran on Linux for the first time. What changed: signals
set a flag that the waits check, so none lands mid-fork; a TERM or HUP to `quiet` now reaches an
unbounded light command, which bash left running; the lock is a `flock` on `.flock`, which the
kernel frees when a holder dies (bash's `.lock` directory is not shared, so the two versions do not
exclude each other while a machine switches); cores are pinned with the affinity calls instead
of `taskset`; the pause watchdog has its own session, so a job runner's sweep can't stop it before
it resumes the paused jobs; and a one-CPU host admits light work (bash's default load cap was 0).
The pause test also passes against the bash `quiet`.

| Host | Interpreter | Tests | Result | Wall time |
|---|---|---|---|---|
| macOS | Python 3.9.6 | 95 | OK, 2 skipped (Linux only) | 356 s |
| macOS | Python 3.15 | 95 | OK, 2 skipped (Linux only) | 545 s |
| Linux | Python 3.9.19 | 95 | OK | 229 s |
| Linux | Python 3.15 | 95 | OK | 215 s |

## One shape for the tools

Every tool is Python with an `argparse` interface: `campaign`, `quiet`, `pace`, `report` and
`sync-agents` (`install.sh` stays shell, as the bootstrap). Help is colored on Python 3.14 and
later, output is colored and marked with emoji on a terminal (`scripts/style.py`; `NO_COLOR` turns
both off, `FORCE_COLOR` on) and plain words in pipes, which agents read, each command has `--help`,
and usage errors exit 2. `campaign` and `quiet` import `report` instead of starting it, so a
`quiet` run costs no extra process outside cmux. `sync-agents.py` writes the same bytes as the
shell version for the four agent types plus edge cases (quotes, `%`, `$HOME`, a second `---`, no
final newline, an unmapped model, a model override), apart from its mark. A campaign keeps its
files in `.campaign/<name>/` at the root of its home repository, so one repository can hold
several; branches are `quest/<campaign>/<id>` and worktrees `<repo>-wt/<campaign>/<id>`; jobs, wait
records and sidebar pills are keyed by campaign. Older campaigns keep `docs/campaign/` until
`campaign migrate`; their jobs keep the home as their tag, so jobs submitted before the upgrade
still wake their game master.

## Parallel tests and quest progress

`tests/run.py` runs each test in its own process, four at once (the most a shared machine allows a
test suite), then the tests marked `alone` one at a time: the CPU-load and contention tests, which
time the machine, and the qemu pinning test, because a strict hold moves every qemu process of the
user, including another test's fake VMs. Every other test isolates its repositories, queue and
`quiet` directory, so the order does not matter. Most of the remaining time is waiting, not work:
the crash tests wait out a 10-second stale threshold and Scenario F about 18 s.

| Host | Interpreter | Tests | One at a time | Four at once |
|---|---|---|---|---|
| macOS | Python 3.9.6 | 102 | 356 s (95 tests) | 169 s |
| macOS | Python 3.15 | 102 | 545 s (95 tests) | 179 s |
| Linux | Python 3.9.19 | 102 | 229 s (95 tests) | 92 s |
| Linux | Python 3.15 | 102 | 215 s (95 tests) | 88 s |

`campaign status` shows each quest's progress: the acceptance criteria in its quest file that its
journals' Evidence tables (the quest's and its tasks') name with evidence, the journal's next step,
and the campaign's share (landed quests whole, plus criteria with evidence). Bars use
`CAMPAIGN_BAR`'s two characters (default `█░`, which every terminal font has; `#-` when the output
cannot encode them), and color only on a terminal without `NO_COLOR`. The jobs line counts the jobs
at the quest's code that passed and failed, and lists the failures no journal names by id yet
(failing is part of iterating; an unread failure is what needs a look). A row whose evidence is a
`<placeholder>` or reads pending, queued, running, failed or missing does not count; the quest line
names the criteria met and those no journal lists yet (the quest template's `<criterion>`
placeholder is not one), and `campaign status ID` lists every criterion as met, pending or with no
row, with the rows behind it. The tests fail against the previous `campaign.py`, with task journals
ignored, with pending rows counted, without the list, with journals' job ids ignored, and with
`CAMPAIGN_BAR` ignored.

Status took 9.2 s on the Codex machine's dense-pages quest (96 finished jobs). A copy of its shape
here (95 jobs, 131 commits) took 13.0 s and started 325 git processes, 294 of them `ls-tree` and
`mktree` computing each job's code tree to see whether it ran at the quest's code. `code_tree` now
reads raw trees through one `git cat-file --batch` and hashes the filtered entries itself, which
gives the id `git mktree` writes: 1.6 s and 32 processes. A test compares it with `mktree` on
every layout (`.campaign`, `docs/campaign`, `docs` holding only the campaign, `docs` as a file), so
job keys from before the change still match, and another fails if status starts a git process per
job (37 against 29 for four jobs before the change). 104 tests pass on macOS with Python 3.9 (167
s) and 3.15 (176 s); the Linux run of this change waits for the VPN.

## Criteria, tasks and cited jobs gate landing

`campaign land` checked dependencies, the rebase, the file set, gate checks at the exact code,
reviews for the current content and CI, but not the quest's own criteria or tasks: a quest could
land with none of its criteria shown, and a task's unmerged commits would silently stay behind. A
final landing now also needs:

- every task merged into the target, or dropped with a reason (`campaign set ID --task T
  status=dropped reason=R`; status shows it as dropped and stops counting it);
- every criterion in the quest file with evidence in an Evidence table of the journals in the
  commit being landed (the template's `<criterion>` placeholder is not one);
- every job such a row names passed at that code: a failed, missing or unversioned job does not
  count, and one that ran older code counts once the same job passes at this code. `campaign job
  rerun ID...` resubmits it at the quest's head, and the gap names the ids to rerun.

`campaign job submit` warns when a command names a path in another job's directory or in a checkout
of its repository, which the job's key does not cover (the dense-pages reader smoke read another
job's build and a file in the home checkout). The game master changes scope only through the quest
file, which voids accepted reviews and adds the new criteria to the gate, and takes an ask that
contradicts a measurement, a ruling or a review finding back to the patron with that evidence first.

The tests fail with each gate removed, with merged tasks counted as unmerged, with cited jobs
unchecked, and with reruns not counted. The test harness clears the template's criteria after
`campaign new`, as a game master filling in the quest file would; the criteria tests keep them.

## Agents' status, notes and other homes

`campaign status` grew into a view for people (bars, criteria, color). Agents now run `campaign
brief`: one line per thing to act on (landing gaps, open notes, failed jobs no journal names,
pending jobs), or the same as JSON with `--json`, and in a quest's worktree only that quest and its
file set. Both read each
repository's branch heads once and remember git answers keyed by commit ids, which never change.
On a fixture shaped like dense-pages (15 criteria, 5 tasks, 132 commits, 98 jobs, journals citing
jobs) on this Mac, under load:

| Command | Before | Now | git processes |
|---|---|---|---|
| `campaign status` | 4.0 s | 2.05 s | 90, now 40 |
| `campaign brief` | | 1.24 s | 20 |

An agent outside a quest passes what it found with `campaign note NAME:ID "text" --from WHO`, from
anywhere on the machine: the note waits in the home's `.git` (any agent can leave one without being
the campaign's one writer), wakes a waiting game master, shows in status and brief, and blocks the
landing until a journal of the quest names its id or the game master closes it with a reason; at
landing it moves into the quest's state. A tool run outside the home names it with `-C HOME` (as
git does), `--campaign NAME` (looked up in the machine's registry) or `$CAMPAIGN_HOME`; a write
from a quest's worktree still refuses. The tests fail without the notes gate, without the
worktree's scope, without `-C`, without the registry lookup, and with explicit homes refused.

## One name, one hold; quiet inside a hold

Two `quiet` bugs, both reproduced before the fix. A second request under a held name took the hold
at once (the level counts skipped holds of the requester's own name), so two strict measurements
ran together and shared one sidebar pill, the waiting one's "queued" over the running one's. And a
`quiet` call inside a hold, such as a benchmark wrapped in `quiet strict` inside a strict campaign
job, waited behind its own parent until the parent's deadline stopped both, its pill reading
"queued" the whole time.

A name held or requested by another live process is now refused at once (checked under the lock,
together with recording a strict request). `quiet` gives its command `QUIET_HELD`; a nested request
at that level or lower runs at once under the hold, and a stronger one is refused with the reason.
A waiting request's pill says what it waits for: `queued (behind 2 light holds): ...`, `queued
(load 9 settling): ...`. Each test fails with its fix reverted.

## Not run

- Claude Code and Codex were not exercised live; Codex is not installed here. Agent files for
  both are generated; the Codex format is from vendor docs.
- FreeBSD: `quiet`'s `cpuset` path.
- Real GitHub CI (the gate ran against a stub `gh`).
- Large repositories: job checkouts and `git mktree` on trees of hundreds of thousands of files.
- A multi-day campaign with many quests; the live tests are small.
- `pace` with a Claude Code snapshot (none on this host); the Copilot path ran.
