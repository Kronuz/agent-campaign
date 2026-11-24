# Quests with nothing moving them, before and after lead-driver

lead-driver makes `campaign wait` return whenever the lead's to-do list has work, so a quest should
no longer sit with nothing moving it while its lead waits. This page measures that quantity in
xapiand and detent for the 3 days before lead-driver's install and from that install until
todo-mail's, the next change that moves the same quantity.

## The short answer

The after window holds a 20.56 h account-wide outage. From 2026-11-22 00:26:35 -0800 until the
reset at 21:00, every lead and agent call was refused with "You've hit your weekly limit", so
nothing could move any quest for most of A+1. Counted as is, the after window looks flat: 231.2
quest-hours per 24 h with nothing moving them (active and review, both campaigns), against 234.7
before (-1.5%). With the outage set apart, the after window is 113.4 per 24 h (-51.7%), and the
share of in-flight time with nothing moving it falls from 32.9% to 19.8%. The other two
attributions of agent time agree, at -54.2% and -51.5%.

That is a large drop, but it does not show that lead-driver halved idle time. Only 33.5 h of
comparable after time remain: 3.1 h before the outage and 30.4 h after it. All 10 of the after
window's other installs fall in that time, among them lead-claim, which delivers every message
to the lead. The data supports this much: in the 33.5 h when the account could work, quests sat
with nothing moving them at about half the before rate, and lead-driver is one of 11 changes
installed by then.

## Windows

Each install time is the dotfiles commit that bumped the campaign skill:
1. Before: 2026-11-18 21:22:54 to 2026-11-21 21:22:54 -0800, the 3 days before lead-driver's
   install (dotfiles 08a762a, agent-campaign:f958fe7).
2. After: 2026-11-21 21:22:54 to 2026-11-24 03:24:03 -0800, 2.25 days, up to todo-mail's install
   (dotfiles aff9241, agent-campaign:709523e). todo-mail mails the lead its to-do and escalates
   undone items, so later days would measure both changes at once.

Days are 24 h from lead-driver's install: B-3 to B-1 before it, A+1 and A+2 after it, and A+3,
the last 6.02 h. No day straddles the install. Rates per 24 h compare the unequal windows.

The script finds the outage itself; its log lists it under `usage-limit outages`. It runs from
2026-11-22T08:26:35Z to 2026-11-23T05:00:00Z, 20.56 h, all inside A+1. Four further refusals of
a minute or less, on 2026-11-19 at 14:03 -0800 and 2026-11-22 at 00:26 -0800, are set apart too.
Without the outage, the after window is 1.394 days (33.5 h). The before window loses about a
minute.

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
   start: 1,104 for xapiand and 859 for detent.
2. The job store, `~/.scratch/campaign/jobs`. A task's job carries its quest's id and counts for
   its quest.
3. Agent runs from the Claude Code transcripts under `~/.claude/projects`. The leads' sessions are
   `-Users-kronuz-code-Kronuz-Xapiand/b5982d78` and `-Users-kronuz-code-Detent/da895235`, and their
   agents are in each session's `subagents/`.

It follows the replay's labels, with three input corrections that each close a gap the replay
named, and one addition:
1. Status and `after` are read from `state.json`'s content at each commit, not parsed from commit
   subjects. `after` is the value in force at the time, not today's. A dependency counts as landed
   when its quest is landed or dropped (`Campaign.met`). A cross-campaign entry is read from that
   campaign's state at the time; none was unresolved.
2. `campaign usage` attributes only 152 of the 681 agent runs that touch the windows. Its parser
   needs the wording of `references/prompts.md`, and most launches were worded freely. The headline
   attribution ("spans") also places the other 529 by their launch prompt: the prompt must name the
   campaign as a word, and the quest is the first known id after the word "quest", else the
   earliest known id. The match agrees with `campaign usage` on all 152 of its runs. 52 agent
   transcripts in the windows name no known quest of either campaign and count for none.
3. A run's agent time is its transcript's first to last line, as in the replay. The "turns"
   variant counts only the gaps of at most 5 minutes between its lines, so time an agent sat idle
   inside one transcript is not counted as moving the quest.
4. The addition: usage-limit outages are found in the transcripts and set apart. An outage starts
   at a refused call (`"apiError":"usage_limit_reached"` in a lead's or an agent's transcript). It
   ends at the reset that refusal names, or at the next real response in any of those
   transcripts, whichever comes first. Every rate is printed with and without that time.

## Result

Active and review together, per 24 h, under the headline attribution. In the "w/o outage" rows,
the outage's hours are left out of both the hours counted and the window's length:

| Campaign | Window | Nothing moving /24 h | In flight /24 h | Share | Whole days, min to max | Counting `after` waits as nothing /24 h |
|---|---|---|---|---|---|---|
| detent | before | 69.1 | 239.7 | 28.8% | 34.1 to 110.6 | 100.2 |
| detent | after | 77.6 | 270.7 | 28.7% | 48.7 to 121.6 | 150.2 |
| detent | after w/o outage | 42.7 | 245.3 | 17.4% | 48.7 (A+2) | 130.4 |
| xapiand | before | 165.6 | 473.7 | 35.0% | 147.2 to 200.4 | 330.3 |
| xapiand | after | 153.6 | 404.1 | 38.0% | 87.2 to 258.1 | 306.9 |
| xapiand | after w/o outage | 70.7 | 326.9 | 21.6% | 87.2 (A+2) | 192.1 |
| both | before | 234.7 | 713.4 | 32.9% | 209.8 to 259.7 | 430.5 |
| both | after | 231.2 | 674.7 | 34.3% | 135.9 to 379.7 | 457.1 |
| both | after w/o outage | 113.4 | 572.2 | 19.8% | 135.9 (A+2) | 322.5 |

Without its brief refusals, the before window rounds to the same 234.7 for both campaigns. Inside
the outage alone, both campaigns had 423.0 quest-hours per 24 h with nothing moving them, 50.3% of
their in-flight time.

By day: quest-hours with nothing moving them, and in flight. The A+1 column leaves out the
outage, which has a column of its own:

| Campaign | Hours | B-3 | B-2 | B-1 | A+1 (3.4 h) | Outage (20.56 h) | A+2 | A+3 (6.02 h) | Before /24 h | After /24 h | After w/o outage /24 h |
|---|---|---|---|---|---|---|---|---|---|---|---|
| detent | nothing moving (active) | 45.4 | 106.7 | 24.5 | 2.1 | 56.6 | 43.2 | 2.9 | 58.9 | 46.6 | 34.6 |
| detent | nothing moving (review) | 17.2 | 3.9 | 9.6 | 4.3 | 58.6 | 5.5 | 1.5 | 10.2 | 31.0 | 8.1 |
| detent | nothing moving (blocked) | 119.9 | 120.0 | 142.1 | 24.1 | 143.9 | 168.0 | 42.1 | 127.4 | 168.0 | 168.0 |
| detent | in flight (active) | 149.3 | 246.4 | 192.5 | 28.8 | 185.0 | 221.5 | 33.8 | 196.1 | 208.5 | 203.8 |
| detent | in flight (review) | 42.0 | 10.1 | 78.8 | 13.6 | 82.2 | 36.5 | 7.7 | 43.6 | 62.2 | 41.4 |
| detent | in flight (blocked) | 119.9 | 120.0 | 142.1 | 28.2 | 164.5 | 225.2 | 66.2 | 127.4 | 215.0 | 229.2 |
| xapiand | nothing moving (active) | 99.1 | 69.7 | 147.2 | 7.3 | 210.2 | 85.6 | 0.1 | 105.4 | 134.7 | 66.7 |
| xapiand | nothing moving (review) | 47.9 | 79.3 | 53.2 | 3.6 | 37.0 | 1.7 | 0.2 | 60.2 | 18.9 | 4.0 |
| xapiand | nothing moving (blocked) | 119.9 | 156.3 | 208.0 | 34.5 | 205.6 | 240.0 | 60.4 | 161.4 | 240.1 | 240.2 |
| xapiand | in flight (active) | 355.4 | 246.1 | 497.0 | 76.4 | 412.4 | 297.3 | 23.6 | 366.3 | 359.7 | 285.0 |
| xapiand | in flight (review) | 99.8 | 120.3 | 102.0 | 12.8 | 41.3 | 37.0 | 8.7 | 107.4 | 44.4 | 42.0 |
| xapiand | in flight (blocked) | 167.9 | 215.7 | 256.0 | 42.2 | 246.7 | 288.0 | 74.6 | 213.2 | 289.4 | 290.3 |

The same quantity under each attribution of agent time, both campaigns, active and review:

| Agent time | Before /24 h | After /24 h | After w/o outage /24 h | Change w/o outage | Share before | Share after w/o outage |
|---|---|---|---|---|---|---|
| spans: every attributed run, first to last line (headline) | 234.7 | 231.2 | 113.4 | -51.7% | 32.9% | 19.8% |
| usage: `campaign usage`'s runs only (the replay's literal input) | 273.8 | 292.2 | 125.4 | -54.2% | 38.4% | 21.9% |
| turns: every attributed run, line gaps of at most 5 min | 259.3 | 292.5 | 125.8 | -51.5% | 36.3% | 22.0% |

Each quest's contiguous nothing-moving time (active or review), clipped to the window, under the
headline attribution. Without the outage, a stretch that crosses it is cut in two, so the
long-stretch figures for that row are a floor:

| Campaign | Window | Stretches | Hours /24 h | In stretches of 1 h or more /24 h | In stretches of 4 h or more /24 h | Longest (h) | Median (min) |
|---|---|---|---|---|---|---|---|
| detent | before | 165 | 69.1 | 64.2 | 48.0 | 29.1 | 1.4 |
| detent | after | 80 | 77.6 | 74.7 | 63.2 | 24.4 | 1.6 |
| detent | after w/o outage | 86 | 42.7 | 37.0 | 15.7 | 6.2 | 1.5 |
| xapiand | before | 362 | 165.6 | 151.9 | 109.5 | 22.8 | 6.0 |
| xapiand | after | 112 | 153.6 | 150.6 | 134.2 | 21.3 | 4.3 |
| xapiand | after w/o outage | 118 | 70.7 | 64.4 | 36.7 | 9.2 | 3.6 |

Each lead's active hours, by the replay's measure (gaps between its transcript's main-thread
lines, each capped at 5 minutes):

| Day | B-3 | B-2 | B-1 | A+1 (3.4 h) | Outage (20.56 h) | A+2 | A+3 (6.02 h) |
|---|---|---|---|---|---|---|---|
| detent lead active (h) | 12.8 | 19.9 | 15.1 | 2.6 | 0.8 | 16.3 | 3.9 |
| xapiand lead active (h) | 19.7 | 20.7 | 19.5 | 3.2 | 1.9 | 22.1 | 5.0 |

What the numbers say:
1. The flat headline averages two different periods. During the outage, the campaigns had 423.0
   quest-hours per 24 h with nothing moving them. In the rest of the after window they had 113.4.
2. Outside the outage, the drop shows under every attribution (-51.5% to -54.2%) and in both
   campaigns (detent -38.2%, xapiand -57.3%). The long stretches shrink too: hours in stretches of
   4 h or more fall from 157.5 to 52.4 per 24 h, though cutting stretches at the outage lowers
   the second figure.
3. The leads were not absent on A+1; their calls were refused. The review's probes (jobs
   261124-051751-2546d8 and 261124-051529-5741b5) found 24 refusals in the xapiand lead's
   transcript between 00:26 and 20:18 -0800, which refused every hourly check it received. They
   found 8 in the detent lead's, from 00:26 to 03:16. The leads' few active hours inside the outage
   (0.8 and 1.9) are those refused attempts.
4. A+2, the one whole day outside the outage, had 135.9, below every before day (209.8 to 259.7).
   It is one day, and 6 of the after window's 10 other installs fell inside it. It agrees with the
   drop without separating lead-driver from those installs.
5. Blocked quests with nothing moving them rose from 288.8 to 408.1 quest-hours per 24 h, but the
   two figures do not count the same quests. lead-driver's install runs `migrate_parks`
   (agent-campaign:f958fe7, `scripts/campaign_tool/campaign.py`). It makes a stored `blocked` quest
   that waits only on other quests `planned`, which takes it out of the in-flight set, and parks
   every other one until the owner. lead-driver's "park" item then marks more of the quests that
   wait on the owner as blocked.

## Limits

1. The comparable after time is 33.5 h: 3.1 h before the outage and 30.4 h after it. That is
   less than a day and a half, against 3 days before. The before window's own days span 50
   quest-hours (21% of its mean). The drop measured here is larger than that spread. A smaller
   effect would not show at this length, and one day cannot separate lead-driver from the changes
   below.
2. Other changes were installed inside both windows (dotfiles log, `campaign: install` and
   `Bump the campaign skill` commits):
   - Before: 23 skill installs, and auto mode letting contributors push (2026-11-20 13:14).
     Among them: drains taking turns with light work (2026-11-20 01:24), v3's quests as features
     built by tasks (04:56, 05:55), evidence by inputs (11:38), reliable delivery of messages to
     the lead (16:33, 18:03), light work held behind a queued drain (23:47), split-big-files
     (2026-11-21 14:11), lead-broadcasts (15:03), and generator-clash and gate-check-jobs (20:06).
   - After: 10 installs, plus a regeneration of the Codex and Copilot agents (2026-11-23 08:48).
     The installs: ci-reuse-disjoint-merge (2026-11-22 00:26); broadcast-every-lead,
     session-title, unversioned-refs, check-set-by-name, check-spec-one and evidence-hints
     (2026-11-23 05:58); task-probes and landing-priority (07:30); lead-driver-flake and
     task-jobs-order (11:10); lead-claim, which keeps a live lead's record and delivers every
     message to it (16:13); docs-script-code (19:21); evidence-holes (20:14); ci-owns-sanitizers
     (21:45); slow-once (23:57); and sidebar-lead-only (2026-11-24 01:30). All 10 fall in the
     comparable after time.
3. Lead presence is recorded, but not controlled for. When a lead stopped because its calls were
   refused, the transcript records why: the usage-limit refusals are lines in it, and they are
   what sets the outage apart. A lead stopped by the owner, by sleep or by a stopped session
   leaves only silence, which is not separated here.
4. CI is not a mover in this method. lead-driver's own `activity()` counts CI queued or running at
   a quest's head as a live waiter; here, such a quest counts as nothing moving it. CI runs are not
   recorded locally, so the records cannot correct this.
5. In v3, `after` orders landings and does not stop work. As in the replay, a quest whose only
   obstacle is an unlanded `after` is labelled "waiting on a dependency", not idle. The last column
   of the first table counts those hours as idle too. Without the outage, that figure still falls,
   from 430.5 to 322.5 (-25%).
6. Agent attribution rests on the launch prompt for 529 of the 681 runs. The match agrees with
   `campaign usage` on all 152 runs that both name. 8 of the 529 (17.5 h of span) were launched
   from another campaign's lead session, and 4 of those are campaign-v3 contributors counted for a
   detent or xapiand quest. The usage-only row bounds this error: with no prompt matching at all,
   the drop is 54.2%.
7. Under the headline attribution, an agent run counts as moving its quest from its first line to
   its last, idle time included. Agents launched just before the outage ran through it to after
   the reset (restart-capacity 20.9 h, group-catalog 20.8 h, split-check-members 21.5 h,
   authority-pages-design 22.9 h), so the outage's own row understates its idle time. The rates
   without the outage leave that time out. The "turns" row, which counts only an agent's active
   lines, gives the same drop (-51.5%).
8. Status changes are dated by the commit that wrote `state.json`. They are exact to the tool's
   own record, but not to when a lead decided.
