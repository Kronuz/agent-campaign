# What v3 must record so v4 can repeat this analysis

This replay of campaign v2 (`tools/replay/`, see `replay-inventory.md`) had to infer most of what it
reports: why a job ran, what it read, why it waited, what an agent turn achieved, and why a quest
did not land. Each gap below names the field that would have answered it, what the replay did
instead, and how much of the accounting rests on that inference. v3 records each as an append-only
event with a timestamp, the actor (lead, agent id and turn, worker, owner) and the quest.

The counts come from `python3 tools/replay/replay.py inventory` (`inventory.json`) and
`replay.py account` (`account.json`), over 2026-11-06T02:39Z to 2026-11-20T02:39Z.

## Jobs

| Gap | Field v3 records | What the replay did instead | What rests on it |
|---|---|---|---|
| Why the job was submitted | `cause`: criterion or check, `retry_of`, `rerun_of`, and the event that made it necessary (a commit, a main merge, a script edit, a flake) | grouped jobs by shape (repository, command, directory, level, build, environment) and called a later one a rerun | the whole rerun split in `where-time-went.md` |
| Who submitted it | `by`: agent id and turn, or the lead, or the tool (`verify`, `rerun`) | nothing: 197 of 7,704 jobs name no quest; none names an agent | agent cost per job |
| What it read | `reads`: every file and blob it opened, and every outside file | `reads.json` exists for 0 of 7,704 jobs (no strace on macOS); used `git diff` against the command's named paths, the build files and the quest's file set | the necessary/avoidable split; its avoidable share is an upper bound |
| Its declared inputs | `inputs` on every check | 0 jobs carry `inputs` | same |
| Each phase | `queued`, `claimed`, `source_ready`, `build_started/ended`, `hold_requested`, `hold_granted`, `began`, `ended` | `claimed..began` lumps preparation, build, admission and quiet's wait together | hold waits (`waits` query) |
| Why it waited, at each moment | a wait record per change of cause: `slot busy`, `light cap`, `drain`, `strict beside`, `load gate`, `fair share`, `order`, `dependency`, `owner hold` | sampled the machine every minute and blamed what held it; 288 slot-hours of idle light capacity with light work ready have no recorded reason | the light-capacity split |
| Its weight | the light slots it was charged, over time, and its CPU time (rusage) | none: the worker's measured-use charges are not stored | the same 288 slot-hours |
| Host load | load average and free memory each minute | none (quiet's load gate refuses holds above 2x CPUs, unrecorded) | the same |
| Results beyond 14 days | a compact record per job, kept forever (the logs may go) | jobs before 2026-11-05 15:05 are pruned (`prune.log` names them only) | any window longer than 14 days |

## The machine (quiet and the worker)

| Gap | Field v3 records | Instead |
|---|---|---|
| Hold history | each hold's request, grant, release, level, owner and cores | quiet keeps live holds only (8 files at the time of the inventory) |
| Worker decisions | each tick's starts and skips with their reason, and each drain turn's open and close | reconstructed from job timestamps |
| The scheduler's version | the tool commit each decision ran under | `campaign_tool.py` changed 20 times in the window (`git log --since=2026-11-05 -- scripts/campaign_tool.py`); the simulator models one version |

## Quests and landing

| Gap | Field v3 records | Instead |
|---|---|---|
| Land attempts | every `land` with its result, each refusal's reason codes, the quest head and main's head | parsed "cannot land yet" from the lead's transcripts and from Claude Code's background task files, which are kept only from 2026-11-15 |
| CI rounds | each CI run per commit: workflow, start, end, result | none locally; CI rounds are counted as distinct refused commits |
| Status over time | each status change as an event with the fields that changed | parsed state.json commit subjects (`new`, `start`, `set status=`, `land`); dependencies (`after`) are current values only |
| Review rounds | each review launch, verdict, the commit reviewed, and why a re-accept was needed | `accept` subjects; re-accepts counted inside refusal episodes |
| Why a quest was idle | a "waiting on" field the driver keeps current: jobs, agent, review, CI, owner, nothing | in-flight hours with no job, no agent run and every `after` landed are called "nothing moving it" |

## Agents and the lead

| Gap | Field v3 records | Instead |
|---|---|---|
| Each agent turn | start, end, tokens by kind, cost, the quest, the prompt kind (launch, resume, fix), why it ended, and its outcome (commits, jobs submitted, state changes) | `campaign usage --json` totals per run; no outcome |
| Lead cost per quest | tokens and time per lead action, attributed to the quest it acted on | the lead is one session: its tokens are summed per hour from its transcript, never per quest |
| Each tool call | command, caller, latency, exit, result size | scanned transcripts for `campaign` and `quiet` calls (`replay.py calls`); Codex sessions are not scanned |
| Owner prompts and decisions | each owner instruction and each decision asked of the owner, with when it was answered | user lines in the lead's transcript |
| Cost | the harness's cost per run | `cost_available` is false for every run |

## Disk

| Gap | Field v3 records | Instead |
|---|---|---|
| Owner of each directory | for every build slot, worktree and scratch task: owner quest or job, created, last used, expiry | `du` plus matching slot names to the jobs that named them (`replay.py leftovers`) |
