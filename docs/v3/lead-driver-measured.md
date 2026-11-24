# Quests with nothing moving them, before and after lead-driver

lead-driver makes `campaign wait` return whenever the lead's to-do list has work, so a quest should
no longer sit with nothing moving it while its lead waits. This page measures that quantity in
xapiand and detent for the 3 days before lead-driver's install and from that install until
todo-mail's, the next change that moves the same quantity.

## The short answer

lead-driver did not measurably reduce it. Active and review quests in both campaigns had
234.7 quest-hours per 24 h with nothing moving them before the install and 231.2 after (-1.5%).
Single days before ranged from 210 to 260 and the two whole days after were 380 and 136, so the
difference is far inside the noise. Under the two other attributions of agent time the after
window is worse, +7% and +13%, and the hours inside idle stretches of 4 h or more rose 25%
(157.5 to 197.4 per 24 h). The largest confounder is the leads themselves: on the first day
after the install both were nearly absent (3.3 and 5.2 active hours, against 13 to 21 before),
and that day was the worst of both windows.

## Windows

Each install time is the dotfiles commit that bumped the campaign skill:
1. Before: 2026-11-18 21:22:54 to 2026-11-21 21:22:54 -0800, the 3 days before lead-driver's
   install (dotfiles 08a762a, agent-campaign:f958fe7).
2. After: 2026-11-21 21:22:54 to 2026-11-24 03:24:03 -0800, 2.25 days, up to todo-mail's install
   (dotfiles aff9241, agent-campaign:709523e). todo-mail mails the lead its to-do and escalates
   undone items, so later days would measure both changes at once.

Days are 24 h from lead-driver's install: B-3 to B-1 before it, A+1 and A+2 after it, and A+3,
the last 6.02 h. No day straddles the install. Rates per 24 h compare the unequal windows.

## Method

This is the replay's `quest_hours` query (agent-campaign branch `quest/xapiand/v2-replay`,
`tools/replay/account.py`, reported in `where-time-went.md`). Each quest-hour of an in-flight quest
(status active, review or blocked) takes the first label that holds:
1. An agent run of the quest.
2. A job of the quest running (began to ended).
3. A job of the quest queued or in its hold (submitted to began).
4. An `after` dependency not landed.
5. Otherwise, **nothing moving it**, kept apart by status.

"Nothing moving it" is therefore exactly the replay's definition: no agent run, no job queued or
running, and every `after` landed. The headline sums the active and review statuses, which the
lead moves; blocked quests wait on the owner and are shown apart.

The numbers come from `docs/v3/lead-driver-measured.py`, next to this page, run as a campaign
job (`python3 docs/v3/lead-driver-measured.py`; the quest's journal names the job). It prints
every table here and writes `measure.json` under `--out`. It reads, read-only:
1. Each home's `state.json` at each of its commits in the windows, plus the one in force at the
   start: 1,105 for xapiand, 860 for detent.
2. The job store, `~/.scratch/campaign/jobs`. A task's job carries its quest's id and counts for
   its quest.
3. Agent runs from the Claude Code transcripts under `~/.claude/projects`. The leads' sessions are
   `-Users-kronuz-code-Kronuz-Xapiand/b5982d78` and `-Users-kronuz-code-Detent/da895235`, and their
   agents are in each session's `subagents/`.

It follows the replay's labels with three input corrections, each one closing a gap the replay
named:
1. Status and `after` are read from `state.json`'s content at each commit, not parsed from commit
   subjects. `after` is the value in force at the time, not today's. A dependency counts as landed
   when its quest is landed or dropped (`Campaign.met`). A cross-campaign entry is read from that
   campaign's state at the time; none was unresolved.
2. `campaign usage` attributes only 152 of the 681 agent runs that touch the windows; its parser
   needs the wording of `references/prompts.md`, and most launches were worded freely. The headline
   ("spans") also attributes the other 529 by their launch prompt: the prompt must name the
   campaign as a word, and the quest is the first known id after the word "quest", else the
   earliest known id. 52 agent transcripts in the windows name no known quest of either campaign
   and count for none.
3. A run's agent time is its transcript's first to last line, as in the replay. The "turns"
   variant counts only the gaps of at most 5 minutes between its lines, so time an agent sat idle
   inside one transcript is not counted as moving the quest.

## Result

Active and review together, per 24 h, under the headline attribution:

| Campaign | Window | Nothing moving /24 h | In flight /24 h | Share | Whole days, min to max | Counting `after` waits as nothing /24 h |
|---|---|---|---|---|---|---|
| detent | before | 69.1 | 239.7 | 28.8% | 34.1 to 110.6 | 100.2 |
| detent | after | 77.6 | 270.7 | 28.7% | 48.7 to 121.6 | 150.2 |
| xapiand | before | 165.6 | 473.7 | 35.0% | 147.2 to 200.4 | 330.3 |
| xapiand | after | 153.6 | 404.1 | 38.0% | 87.2 to 258.1 | 306.9 |
| both | before | 234.7 | 713.4 | 32.9% | 209.8 to 259.7 | 430.5 |
| both | after | 231.2 | 674.7 | 34.3% | 135.9 to 379.7 | 457.1 |

By day, quest-hours with nothing moving them, and in flight:

| Campaign | Hours | B-3 | B-2 | B-1 | A+1 | A+2 | A+3 (6 h) | Before /24 h | After /24 h |
|---|---|---|---|---|---|---|---|---|---|
| detent | nothing moving (active) | 45.4 | 106.7 | 24.5 | 58.7 | 43.2 | 2.9 | 58.9 | 46.6 |
| detent | nothing moving (review) | 17.2 | 3.9 | 9.6 | 62.9 | 5.5 | 1.5 | 10.2 | 31.0 |
| detent | nothing moving (blocked) | 120.0 | 120.0 | 142.1 | 168.0 | 168.0 | 42.1 | 127.4 | 168.0 |
| detent | in flight (active) | 149.3 | 246.4 | 192.5 | 213.8 | 221.5 | 33.8 | 196.1 | 208.5 |
| detent | in flight (review) | 42.0 | 10.1 | 78.8 | 95.8 | 36.5 | 7.7 | 43.6 | 62.2 |
| detent | in flight (blocked) | 120.0 | 120.0 | 142.1 | 192.6 | 225.2 | 66.2 | 127.4 | 215.0 |
| xapiand | nothing moving (active) | 99.2 | 69.7 | 147.2 | 217.5 | 85.6 | 0.1 | 105.4 | 134.7 |
| xapiand | nothing moving (review) | 48.0 | 79.3 | 53.2 | 40.6 | 1.7 | 0.2 | 60.2 | 18.9 |
| xapiand | nothing moving (blocked) | 120.0 | 156.3 | 208.0 | 240.1 | 240.0 | 60.4 | 161.4 | 240.1 |
| xapiand | in flight (active) | 355.7 | 246.1 | 497.0 | 488.8 | 297.3 | 23.6 | 366.3 | 359.7 |
| xapiand | in flight (review) | 99.9 | 120.3 | 102.0 | 54.1 | 37.0 | 8.7 | 107.4 | 44.4 |
| xapiand | in flight (blocked) | 168.0 | 215.7 | 256.0 | 288.8 | 288.0 | 74.6 | 213.2 | 289.4 |

The same quantity under each attribution of agent time, both campaigns, active and review:

| Agent time | Before /24 h | After /24 h | Change | Share before | Share after |
|---|---|---|---|---|---|
| spans: every attributed run, first to last line (headline) | 234.7 | 231.2 | -1.5% | 32.9% | 34.3% |
| usage: `campaign usage`'s runs only (the replay's literal input) | 273.8 | 292.2 | +6.7% | 38.4% | 43.3% |
| turns: every attributed run, line gaps of at most 5 min | 259.3 | 292.5 | +12.8% | 36.3% | 43.3% |

Each quest's contiguous nothing-moving time (active or review), clipped to the window, headline
attribution:

| Campaign | Window | Stretches | Per 24 h | Hours /24 h | In stretches of 1 h or more | In stretches of 4 h or more | Longest (h) | Median (min) |
|---|---|---|---|---|---|---|---|---|
| detent | before | 165 | 55.0 | 69.1 | 64.2 | 48.0 | 29.1 | 1.4 |
| detent | after | 80 | 35.5 | 77.6 | 74.7 | 63.2 | 24.4 | 1.6 |
| xapiand | before | 362 | 120.7 | 165.6 | 151.9 | 109.5 | 22.8 | 6.0 |
| xapiand | after | 112 | 49.8 | 153.6 | 150.6 | 134.2 | 21.3 | 4.3 |

Each lead's active hours, the replay's measure (gaps between its transcript's main-thread lines,
each capped at 5 minutes), against both campaigns' active and review hours with nothing moving:

| Day | B-3 | B-2 | B-1 | A+1 | A+2 | A+3 (6 h) |
|---|---|---|---|---|---|---|
| detent lead active (h) | 12.8 | 19.9 | 15.1 | 3.3 | 16.3 | 3.9 |
| xapiand lead active (h) | 19.7 | 20.7 | 19.5 | 5.2 | 22.1 | 5.0 |
| Both campaigns, nothing moving (h) | 209.8 | 259.6 | 234.5 | 379.7 | 136.0 | 4.7 |

What the numbers say:
1. The headline total is flat, and the daily spread is far larger than the change. The after
   window's two whole days are 380 and 136 quest-hours, against a before range of 210 to 260.
2. The idle time is concentrated in long stretches, before and after. More than 90% of it lies in
   stretches of an hour or more, and after the install there are fewer stretches, each longer.
   lead-driver lists a quest with nothing moving it at once and again after 30 minutes undone, so
   it should have shortened these. It did not: the lead was told, and the quest still sat.
3. A+1, the day after the install, is the worst day of both windows (379.7), and on it both
   leads were nearly absent: 3.3 and 5.2 active hours, against 13 to 21 on every before day.
   lead-driver wakes a lead that is waiting; it cannot act for a lead that is not running. Only
   one other install fell in A+1 (ci-reuse-disjoint-merge, 2026-11-22 00:26). Over the after window, xapiand's
   most idle active quests were ninja-only, split-datetime, turn-log-parity, split-xapiand-tests
   and schema-split, 25 to 29 h each (`per_quest` in `measure.json`).
4. A+2 is the one after day with both leads present as before (16.3 and 22.1 h), and it is the
   lowest whole day of both windows: 136.0, against 210 to 260 before (42% under the before
   mean). That is one day, and 6 of the after window's 9 other installs fell inside it, so it is
   a lead, not a measurement.
5. Blocked quests with nothing moving them rose from 288.8 to 408.1 quest-hours per 24 h. lead-driver
   adds a "park" item that marks a quest waiting on the owner as blocked, so some idle time moved
   out of active into blocked. That shift would lower the active figure without moving any quest,
   and the active figure did not fall either.

## Limits

1. The after window is 2.25 days, 2 of them whole. The before window's three days span 50
   quest-hours (21% of its mean) and the after window's two whole days span 244, so a change of
   the size measured, 3.5 quest-hours per 24 h, is far below what these windows resolve.
2. Other changes were installed inside both windows (dotfiles log, `campaign: install` and
   `Bump the campaign skill` commits):
   - Before: 23 skill installs and auto mode letting contributors push (2026-11-20 13:14).
     Among them: drains taking turns with light work (2026-11-20 01:24), v3's quests as features
     built by tasks (04:56, 05:55), evidence by inputs (11:38), reliable delivery of messages to
     the lead (16:33, 18:03), light work held behind a queued drain (23:47), split-big-files
     (2026-11-21 14:11), lead-broadcasts (15:03), generator-clash and gate-check-jobs (20:06).
   - After: 10 installs and a regeneration of the Codex and Copilot agents (2026-11-23 08:48):
     ci-reuse-disjoint-merge (2026-11-22 00:26); broadcast-every-lead, session-title,
     unversioned-refs, check-set-by-name, check-spec-one and evidence-hints (2026-11-23 05:58);
     task-probes and landing-priority (07:30); lead-driver-flake and task-jobs-order (11:10);
     lead-claim, which keeps a live lead's record and delivers every message to it (16:13);
     docs-script-code (19:21); evidence-holes (20:14); ci-owns-sanitizers (21:45); slow-once
     (23:57); sidebar-lead-only (2026-11-24 01:30).
3. Lead presence is not controlled, and it is the largest confounder here: the leads' active
   hours per 24 h fell from 15.9 to 10.4 (detent) and from 20.0 to 14.4 (xapiand), almost all of
   it on A+1. The transcripts record when a lead ran, not why it stopped (the owner's sessions,
   sleep, a stopped or compacting session).
4. CI is not a mover in this method. lead-driver's own `activity()` counts CI queued or running
   at a quest's head as a live waiter; here such a quest counts as nothing moving it. CI runs
   are not recorded locally, so this cannot be corrected from the records.
5. In v3, `after` orders landings and does not stop work. A quest whose only obstacle is an
   unlanded `after` is labelled "waiting on a dependency", not idle, as in the replay. The last
   column of the first table counts those hours as idle too; they rise more after the install
   (430.5 to 457.1).
6. Agent attribution rests on the launch prompt for 529 of 681 runs. A prompt that names another
   quest first can misattribute its run. The usage-only row bounds this: with no prompt matching
   at all, the after window is 6.7% worse.
7. An agent run that ends its turn to wait on jobs is still labelled by those jobs, but one that
   sits idle inside a transcript counts as moving its quest under the headline attribution. The
   "turns" row removes that time, and it makes the after window 12.8% worse.
8. Status changes are dated by the commit that wrote `state.json`, so they are exact to the
   tool's own record but not to when a lead decided.
