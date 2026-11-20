# What v2's commands cost, and what v3's hot path needs

The owner's target is that every command answers in milliseconds. v2's most frequent read
commands take one to eight seconds, and are called about 2,300 times a day. Almost none of that
time is useful work. It goes to reading 7,700 job directories, forking git about 140 times, and
asking GitHub, every call. An indexed store answers the same question in 1.6 ms in process and
20 ms from a compiled CLI. This page gives the measurements behind those claims.

## Sources

1. **Calls per day and production latency.** `quiet light v2-replay-calls 30 "v2-replay calls" --
   python3 tools/replay/replay.py --refresh calls` (2026-11-19 19:33 PST) scans the leads' and
   agents' Claude Code transcripts (`tools/replay/calls.py`). It finds 47,567 `campaign` and `quiet`
   calls, 32,738 of them in the window, and times each foreground call from its tool_use line to
   its tool_result line. Codex sessions are not scanned. "Timed" counts the calls that ran alone in
   a foreground command, so the latency is the command's own.
2. **Controlled timing and profiles.** `quiet strict v2-replay-toolcost 30 "v2-replay toolcost" --
   python3 tools/replay/replay.py toolcost` (2026-11-20T03:40Z, load 21/15/16 on 16 CPUs;
   `tools/replay/toolcost.py`). Each read-only command ran three times under `/usr/bin/time -l` in
   the xapiand home, and the median is reported. CAMPAIGN_JOBS pointed at a copy of the store's
   records (7,747 job directories): `status` reaps, and the store must not change. The real
   `state.json` was left untouched; the pass checks it. `status` and `brief` also ran under
   cProfile. The commands that change state (`land`, `accept`, `verify`, `job submit`, `set`) were
   not run; their latency comes from the transcripts only.

## Per command

| Command | Calls in window | Per day | Lead / agents | Production median s (p90) | Measured wall s | user s | sys s | Instructions | Involuntary switches |
|---|---|---|---|---|---|---|---|---|---|
| `campaign status` | 1,514 | 108 | 759 / 755 | 8.54 (21.2) | 5.00 | 8.08 | 6.57 | 9.9 G | 73,419 |
| `campaign brief` (all quests) | 1,985 | 142 | 588 / 1,397 | 4.65 (10.2) | 4.12 | 7.19 | 5.51 | 7.5 G | 82,399 |
| `campaign brief QUEST` | (in the above) | | | | 1.68 | 1.56 | 0.74 | 3.3 G | 5,518 |
| `campaign check` | 311 | 22 | 129 / 182 | 17.0 (120) | 2.11 | 2.28 | 1.41 | 6.3 G | 7,696 |
| `campaign job list` | 3,627 | 259 | 976 / 2,651 | 2.88 (7.7) | 1.20 | 0.87 | 0.31 | 3.1 G | 946 |
| `campaign wait` (finished job) | 3,064 | 219 | 954 / 2,110 | blocks by design | 1.04 | 0.76 | 0.24 | 2.7 G | 633 |
| `campaign get` | 964 | 69 | 282 / 682 | 1.34 (2.8) | 0.27 | 0.19 | 0.07 | 0.65 G | 189 |
| `campaign job show` | 3,410 | 244 | 401 / 3,009 | 1.35 (3.7) | 0.20 | 0.15 | 0.04 | 0.52 G | 67 |
| `quiet check` | 726 | 52 | 29 / 697 | 0.83 (2.0) | 0.32 | 0.16 | 0.12 | 0.52 G | 396 |
| `campaign job submit` | 5,404 | 386 | 216 / 5,188 | 3.78 (12.6) | not run (writes) | | | | |
| `campaign verify` | 1,449 | 104 | 168 / 1,281 | 2.29 (5.4) | not run (writes) | | | | |
| `campaign land` | 536 | 38 | 508 / 28 | 10.7 (44.8) | not run (writes) | | | | |
| `campaign accept` | 434 | 31 | 372 / 62 | 3.67 (9.1) | not run (writes) | | | | |
| `campaign set` | 760 | 54 | 742 / 18 | 6.58 (13.5) | not run (writes) | | | | |
| `campaign job log` | 1,880 | 134 | 118 / 1,762 | 1.25 (2.5) | | | | | |
| `quiet list` | 1,363 | 97 | 298 / 1,065 | 1.29 (60.9) | | | | | |
| `python3 -c pass` (floor) | | | | | 0.06 | 0.03 | 0.01 | 0.15 G | 85 |
| `python3 -c "import campaign_tool"` (v2's floor) | | | | | 0.22 | 0.15 | 0.05 | 0.48 G | 487 |

Leaving out the calls that block on purpose (`campaign wait`, `quiet light`, `quiet strict`), the
11,682 timed calls spent 25.6 hours waiting on the tool. Scaled to all 27,593 such calls, that is
about 60 agent and lead hours in 14 days, or 4.3 hours a day. Every one of those hours is an agent
turn kept open, and its context reread, while the tool works.

The production medians are higher than the controlled ones, for two reasons. In production the
real store's directories also hold logs, build outputs and `src/` checkouts, while the copy holds
only the records. And production calls ran on a host at load 15 to 50. The lead's own measurement
of `status` was 11.4 s wall, with 10.5 s user and 9.8 s sys.

## Where `status` spends its time

The cProfile of `status` takes 9.07 s of in-process time. Its user plus sys time exceeds its wall
time because it fans work out to a thread pool and to child processes. Here is what it does on each
call:

| Primitive | Calls | Seconds (own) |
|---|---|---|
| child processes (`subprocess.Popen`) | 279 (143 `fork_exec`) | 1.10 (fork/exec 1.12) |
| `stat` | 12,551 | 0.21 (plus 15,317 `DirEntry.stat`) |
| `open` | 1,953 | 0.19 |
| `json.loads` | 1,424 | 1.85 cumulative |
| `select.poll` and `read` on child pipes | 1,106 / 563 | 0.54 / 0.16 |

And here is where it spends cumulative time: `quest_view` (341 calls, 7.39 s), `landing_gaps`
(4.18 s), `criteria_evidence` (3.91 s), `job_at` (831 calls, 3.74 s), `tagged_row` (3.54 s),
`job_with_key` (1,814 calls, 2.26 s), `read_json` (777 calls, 1.71 s), `gh_ask` (1.49 s: GitHub's
API, inside a read command), `key_links` (1.45 s: a `readlink` per key link) and `clashes`
(1.09 s). `brief` has the same shape: `job_at` 5.45 s, `landing_gaps` 4.74 s, `list_keys` 1.81 s,
`reap` 1.31 s and `gh_ask` 2.05 s.

The cause is structural, and it is not Python's speed. Each call rebuilds the whole campaign's
derived state from scratch: every quest's view, every criterion's evidence from every job record,
the git trees to compare, CI from GitHub. It then throws all of it away. The 341 quest views are
computed even for landed quests.

## The hot path, prototyped

The hot path is the question `status`, `brief` and the driver ask most: the in-flight quests and
their jobs by state. `toolcost.py` loads the same 7,738 job records into two stores. One is a SQLite
table with an index on (campaign, quest, state), 4.6 MB. The other is an append-only JSON-lines log,
0.9 MB. It then asks the question four ways, each in a fresh process, five runs, median:

| Way | Wall ms | user ms | sys ms |
|---|---|---|---|
| Python, scanning every job directory (v2's `all_jobs` read) | 4,180 | 1,400 | 1,170 |
| Python, reading the whole append-only log | 120 | 90 | 20 |
| Python, opening SQLite and running the indexed query | 90 | 60 | 20 |
| `sqlite3` CLI (compiled), same query | 20 | <10 | <10 |
| The same query in an already-running process | 1.6 | | |

`/usr/bin/time` resolves 10 ms, so the CLI's user and sys times read as zero.

## What this settles for v3

1. **Storage decides most of it.** An index turns a 4.2 s directory scan into a 1.6 ms query, a
   factor of about 2,600. An append-only log read whole is 35 times faster than the scan, but its
   cost still grows with history. Read the log once into an index, and keep that index incremental.
2. **Language sets the floor for a fresh process.** Python's start is 60 ms, and importing v2's
   9,000-line module is another 160 ms. A compiled CLI with SQLite answers in 20 ms. So
   "milliseconds" per call means one of two shapes. Either a long-lived driver process answers over
   a socket, where the query is the 1.6 ms, or a compiled CLI reads an index, which costs about
   20 ms. A fresh Python process cannot get under about 90 ms, even with a perfect store.
3. **Derived state belongs in the store, updated by events.** Quest views, evidence and CI state
   change when a job ends, a commit lands or CI reports, about 4,300 commits and 7,700 jobs in 14
   days. Recomputing them on every read, at 2,300 reads a day, does the same work 100 to 1,000
   times over.
4. **No network or git in a read path.** `gh_ask` costs 1.5 to 2 s per `status` call, and the 140
   git forks cost 1.1 s. v3 records CI and refs as events when they change, and reads only its
   store.

What this does not settle: the cost of the write commands (`land` at a median 10.7 s, `job submit`
at 3.8 s, `set` at 6.6 s) was not profiled, because they cannot run on the real state without
changing it. A copy of a home with its repositories would allow that.
