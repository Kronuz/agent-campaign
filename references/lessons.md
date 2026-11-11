# Lessons

The incidents behind campaign's rules, kept so a rule can be changed knowingly, each with what
prevents a recurrence; prefer a mechanism to a sentence.

## Incidents

| What happened | Invariant | Prevention |
|---|---|---|
| Idle bare-metal cloud machines ran for hours after agents forgot them; hundreds of dollars | No unattended billable resource | Campaign has no provisioning path. Any future cloud use needs the owner's authorization and provider-side expiry, budget alarms and cleanup that work without an agent. |
| An orphaned load generator held a workstation at load 87 for hours and killed every agent on it | A helper dies with its caller | Jobs kill their whole session when the command ends; the reaper kills a dead runner's leftovers |
| Builds outran 20-minute holds and kept running unheld; agents asked for 4-hour holds | A measurement runs only inside its isolation | `quiet` stops strict and exclusive commands at the deadline and refuses holds over 30 minutes |
| 30-40 timing holds queued at once; builds waited 1-7 hours; tiny probes waited 2.5 hours behind exclusive holds | Agents do not wait on machines; builds get turns | Jobs; starving light requests go before new strict holds |
| A gate passed on document counts while the values were wrong | Tests check values | Proof standards |
| A branch missing a landed sibling used an API the sibling removed and broke `main` after a clean merge | `main` holds tested code | `land` requires the branch to contain `main`'s code and evidence at that exact commit |
| A 20% write regression surfaced only at a phase gate and cost a bisect across 19 commits | Measure close to the change | Hot-path changes run a smoke A/B job against their parent before landing |
| Agents narrowed 19 user-visible behaviors alone, one to close a security finding | The owner decides user-visible behavior | Reviewers offer options and never make a narrowing blocking; the lead asks the owner |
| A critical-path step sat unlaunched for a week while its question was already answered; leads ran one quest at a time while ~25 were ready | Ready work is visible; idle capacity is filled | `campaign status` lists launchable quests, critical path first; the lead fills idle capacity at each wake from the hunt list |
| One message resumed a finished 556k-token agent; a repin cost 74k fresh against ~500k resumed | Resume only small contexts | Resume by message under ~150k tokens; otherwise a fresh agent from the journal |
| Code rechecks cost ~134k each and confirmed closures every time; a CHANGES review could be accepted with any passing job, even one from another repository running `true` | A fix is confirmed cheaply, by evidence of that fix | `accept --confirm FINDING=JOB`: the suite job passing the fix's test at the fixed code in the quest's repository |
| Explorers on fresh targets: 20 of 33 runs landed at a median of 93k tokens; a day of hunting found three bugs from 2019. Explorers timing under light holds pushed the load to 16; a scout rediscovered a fix already on `main` | Spend on cheap, targeted finding; timing is isolated | Exploration batches, timed only through strict and exclusive jobs, each target first confirmed on `main` |
| Design re-reviews ran 6-7 rounds on prose; a third fix round was text again | Settle designs with evidence | One design review with one text fix round, then the reviewer's probes as jobs |
| A design-text fix voided 14 observability jobs (one a 13-minute exclusive run) and merges of `main` reran one quest's evidence three times (47, 54, ~55 jobs); every rerun gave the same result | Expensive runs go once | Evidence survives merges from `main`, which CI tests; only a changed criterion voids acceptances |
| A measurement's decision rule changed three times after its data came in | Nothing weakens silently | The rule is in the quest file before the first run; a change after data is a recorded weakening |
| With `tested_docs` set, every docs-area landing voided the other areas' E2E evidence, and pages moved between area quests mid-flight | One writer per file set | Docs areas are separate quests from the start, chained with `--after`; docs of an in-flight format change stay in its quest |
| A system review read a stale `origin` | Reviews name exact commits | Prompts name the commit; tools read local refs |
| A lead appended its own disposition to a reviewer's file; an APPROVE was accepted at a head with commits the reviewer never saw | An acceptance covers what was reviewed | The acceptance records the review's content and its `Commit:`; changing either voids it |
| A failing check could be replaced by `true`, or a high-risk quest set to low, with no trace | Nothing weakens silently | `set --weaken REASON`, kept in the quest's history; acceptances voided |
| A narrative journal row ("tested it, all green") satisfied a criterion | Evidence, not narrative | Criteria name their evidence kind; an untagged one needs a job tagged for it |
| Local macOS TSan passed a race that Linux CI TSan caught; `std::jthread` broke libc++ builds | Evidence names its platform | Sanitizers and platforms run in CI, which landing requires |
| A duplicate thread pool beside the shared one; a recommended limit below the reference systems' | One mechanism; limits are load-bearing and at least the references' | The correctness lens asks for both |

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
