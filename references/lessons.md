# Lessons

The incidents behind campaign's rules, kept so a rule can be changed knowingly. Each names what
prevents a recurrence now; prefer a mechanism to a sentence. Sources: the strata, Detent, Xapiand
and cpp-libraries records (October-November 2026).

## Incidents

| What happened | Invariant | Prevention |
|---|---|---|
| Idle bare-metal cloud machines ran for hours after agents forgot them; hundreds of dollars | No unattended billable resource | Campaign has no provisioning path. Any future cloud use needs the owner's authorization and provider-side expiry, budget alarms and cleanup that work without an agent. |
| An orphaned load generator held a workstation at load 87 for hours and killed every agent on it | A helper dies with its caller | Jobs kill their whole session when the command ends; the reaper kills a dead runner's leftovers |
| Builds outran 20-minute holds and kept running unheld; agents asked for 4-hour holds | A measurement runs only inside its isolation | `quiet` stops strict and exclusive commands at the deadline and refuses holds over 30 minutes |
| 30-40 timing holds queued at once; builds waited 1-7 hours; tiny probes waited 2.5 hours behind exclusive holds | Agents do not wait on machines; builds get turns | Jobs; starving light requests go before new strict holds |
| A gate passed on document counts while the values were wrong | Tests check values | Proof standards |
| A branch missing a landed sibling used an API the sibling removed and broke `main` after a clean merge | `main` holds tested code | `land` requires the branch to contain `main`'s code and evidence at that exact commit |
| A 20% write regression surfaced only at a phase gate and cost a bisect across 19 commits | Measure close to the change | Hot-path changes run a smoke A/B job against their parent before review |
| Agents narrowed 19 user-visible behaviors alone, one to close a security finding | The owner decides user-visible behavior | Reviewers offer options and never make a narrowing blocking; the lead asks the owner |
| A critical-path step sat unlaunched for a week while its question was already answered | Ready work is visible | `campaign status` lists ready quests |
| One message resumed a finished 556k-token agent; a repin cost 74k fresh against ~500k resumed | Resume only small contexts | Resume by message under ~150k tokens; otherwise a fresh agent from the journal |
| Code rechecks cost ~134k each and confirmed closures every time | Confirm fixes cheaply | The lead reruns the reviewer's probes as jobs, named per finding (`accept --confirm`) |
| Swarms of Sonnet explorers on fresh targets: 20 of 33 runs landed at a median of 93k tokens; a day of hunting found three bugs from 2019 | Spend on cheap, targeted finding | Exploration batches |
| Explorers measuring under light holds at once pushed the load to 16; a scout spent its run rediscovering a fix already on `main` | Timing is isolated; targets are fresh | An exploration batch times only through strict and exclusive jobs; each contributor first confirms its target on `main` |
| Design re-reviews ran 6-7 rounds on prose (findings to DM-R49) | Settle designs with evidence | One design review; disputes go to an executable model |
| A system review read a stale `origin` | Reviews name exact commits | Prompts name the commit; tools read local refs |
| A CHANGES review could be accepted with any passing job, even one from another repository running `true` (2026-11-08) | A fix is confirmed by evidence of that fix | `accept --confirm FINDING=JOB`: per blocking finding, a job that passed at the fixed code in the quest's repository |
| A lead appended its own disposition to a reviewer's file; an APPROVE was accepted at a head with commits the reviewer never saw (2026-11-05, 2026-11-08) | An acceptance covers what was reviewed | The acceptance records the review's content and its `Commit:`; changing either voids it |
| A failing check could be replaced by `true`, or a high-risk quest set to low, with no trace (2026-11-08) | Nothing weakens silently | `set --weaken REASON`, kept in the quest's history; acceptances voided |
| A narrative journal row ("tested it, all green") satisfied a criterion (2026-11-08) | Evidence, not narrative | Criteria name their evidence kind; an untagged one needs a job |
| Local macOS TSan passed a race that Linux CI TSan caught; `std::jthread` broke libc++ builds | Evidence names its platform | CI on every platform before landing |
| A duplicate thread pool beside the shared one | One mechanism, one implementation | The correctness lens asks for it |
| A recommended limit was below the reference systems' | Limits are load-bearing and at least the references' | The correctness lens asks for it |

## Git and shell traps

- `git push ... | tail` in an `&&` chain hides a failed push; one deleted a remote branch. Check
  each push's own exit status (`land --push` does).
- Stage explicit paths, never `git commit -a`, where another agent may be writing.
- Under `pipefail`, `cmd | grep -q` fails spuriously when the reader exits early; capture first.
- The agent shell may be zsh: an unquoted `$var` does not split; use arrays or `bash -c`.
- Edit YAML by whole lines and verify it by parsing it.
- A stale remote-tracking ref looks like a force-push: `git fetch` before raising an alarm.
- Keep build directories outside synced trees: `rsync --delete` once wiped them.
- Backgrounded commands never read a stdin that stays open (an `eza` alias for `ls` hung on an
  agent shell's socket): use `</dev/null`.
- A new CI job runs on a branch first; turned on straight on `main`, it brings its failures in on
  every landing. Never dispatch a release workflow from a test branch.

## Other teams' code

A patch to upstream code is generated from the upstream commit and checked by a drift test, never
edited by hand. A bug found in another team's code gets its own minimal change with a failing test,
landed first. Every change to another team's code is listed where its owner can review it.
