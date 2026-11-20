# What campaign v2 recorded: the replay's inventory

`tools/replay/` loads every v2 record on this machine into one event timeline, so v3 can be
designed from what happened. This page lists each source, the time it spans, how much it holds, and
what it cannot tell. `recording-gaps.md` turns those last columns into what v3 must record.

All figures come from one pass, `quiet light v2-replay-inventory 30 "v2-replay inventory" --
python3 tools/replay/replay.py --refresh inventory`, run 2026-11-19 19:28 PST. It writes
`inventory.json` and `timeline.jsonl` under `~/.scratch/xapiand/v2-replay/`, and
`replay.py report inventory` prints the tables below. The analysis window is the 14 days that
ended when this quest started: 2026-11-06T02:39Z to 2026-11-20T02:39Z (`sources.SINCE`, `UNTIL`).

The timeline holds 38,345 events: 30,226 from jobs, 2,184 from state history, 2,619 from journal
commits, 2,578 from the leads' `campaign` calls and 738 from agent runs. Each event is a JSON line
with `t`, `campaign`, `quest`, `source` and `kind`.

## Sources

| Source | Where | Span | Counts | What it cannot tell |
|---|---|---|---|---|
| Job store | `~/.scratch/campaign/jobs/<id>/spec.json`, `state.json`, `for.json` | 2026-11-05T23:05Z to 2026-11-20T03:12Z; older jobs are pruned at 14 days | 7,737 jobs: 5,405 done, 1,046 failed, 57 timeout, 34 interrupted, 859 cancelled, 249 skipped, 77 queued, 10 running; by level 6,377 light, 448 strict, 779 exclusive, 133 none. 6,832 carry a read key (`rkey`), 4,782 are tagged for a criterion, 1,825 have a staged build | Why each job was submitted, and by whom. What it read: `reads.json` exists for 0 jobs and `inputs` is set on 0. How `claimed..began` splits into source preparation, build, admission and quiet's wait (only staged builds are timed). CPU used, as opposed to wall time. The light-slot charge the worker gave it |
| Campaign state | `.campaign/<name>/state.json` in each home | current value only | xapiand: 341 quests, 154 messages; detent: 229 quests, 128 messages | Anything past: dependencies (`after`, `needs`) and file sets are current values |
| State history | `git log -- .campaign/<name>/state.json` in each home, one commit per change | xapiand from 2026-11-08T23:46Z (1,201 commits); detent from 2026-11-08T20:29Z (982 commits) | xapiand: 156 new, 161 start, 447 set, 142 accept, 128 land, 140 close; detent: 162 new, 127 start, 308 set, 119 accept, 109 land, 116 close | Land refusals: a refused land writes nothing. The replay parses commit subjects, not the 1.6 MB snapshots, so a status change shows up only when its subject names it. Who ran the command |
| Journals | `git log --all -- .campaign/<name>/journal/` | same as the history | xapiand: 303 journals, 1,417 commits; detent: 180 journals, 1,202 commits | Agent turns: a journal commit stands in for a turn, and is not one |
| Agent usage | `campaign usage --json` in each home (Claude Code and Codex session logs) | xapiand runs 2026-11-09T05:22Z to 2026-11-18T19:20Z; detent 2026-10-02 to 2026-11-20 | xapiand: 144 runs (Claude); detent: 225 runs (173 Claude, 52 Codex) | What each turn achieved; cost (`cost_available` is false on every run). Xapiand runs after 2026-11-18T19:20Z are not attributed, though the lead launched agents on 2026-11-19 (efficiency.md, 17h): those turns are missing |
| Lead transcripts | `~/.claude/projects/*/<lead session>.jsonl`, main thread only, plus Claude Code's background task files (`/private/tmp/claude-501/<project>/<session>/tasks/*.output`) | both from 2026-11-09T05:19Z, when both leads first ran the tool; task files only from 2026-11-15 | xapiand: 1,613 `campaign` calls kept (281 land, 206 accept, 99 verify, 133 job submit, 129 job rerun), 92 land refusals, 2,528M tokens in the window, 2,969 prompts and 2,063 harness notices; detent: 965 calls, 35 refusals, 1,600M tokens | Calls made by the owner or in other sessions. Why the lead acted when it did. The result of a background call before 2026-11-15 |
| Agent transcripts | `<lead session>/subagents/agent-*.jsonl` | as the lead's | 3.7 GB (`replay.py calls`; see `tool-cost.md`) | Codex sessions are not scanned |
| efficiency.md | `.campaign/xapiand/efficiency.md` (detent's has 20 table rows) | 2026-11-08 to 2026-11-19 | 256 table rows; 12 hourly notes, from 2026-11-17 23:00 | Free text. Queue lengths are given only in the two 2026-11-19 notes |
| quiet | `~/.scratch/quiet/` | now | 6 live holds, 14 files | No history at all: every hold wait is inferred from job timestamps |
| Git | first-parent `main` or `master` of each repository under `~/code/Kronuz` | the window | 95 repositories with commits, 4,306 commits: Xapiand 1,919, Detent 1,275, agent-campaign 196, strata 112, consensus 111, logger 77, journal 75 | CI runs, their durations and results (GitHub only) |

A campaign named `campaign-v3` was registered after the window closed. It has no records and is
left out.

## How the sources join

A job names its campaign (the path of its `.campaign/<name>`), its quest, its repository and
commit. Of the 7,737 jobs, 197 name no quest. A quest's status over time comes from its history
subjects (`new`, `start`, `set ... status=S`, `land`), so the first three days of the window (before
2026-11-08) carry jobs but no quest status. An agent run names its quest through `campaign usage`'s
attribution. A lead call names a quest when its command line does. Repositories join on paths:
a job whose worktree is gone (`Xapiand-wt/xapiand/wide-search-loop`, for one) is read through its
base repository, whose objects it shared.

## Reproducing it

```sh
cd <agent-campaign checkout>
quiet light v2-replay-inventory 30 "v2-replay inventory" -- python3 tools/replay/replay.py --refresh inventory
python3 tools/replay/replay.py report inventory
```

`--refresh` reruns `campaign usage --json` and reparses the transcripts (about 0.9 GB). Without it
the replay reuses the caches in `~/.scratch/xapiand/v2-replay/`, so a rerun reads the same input.
The job store keeps changing; to reproduce these exact counts, keep `inventory.json` and the caches.
