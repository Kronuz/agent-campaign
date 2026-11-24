# file-holds: journal

State: waiting on jobs 261124-120834-4b8107 261124-120836-b93073 261124-120839-2c9188 261124-120841-1374fe 261124-120842-12ee30 261124-120844-3c7ebc
Quest: .campaign/campaign-v3/quests/file-holds.md (the lead's; never edit it)

Keep this current and commit it with the work, so a fresh contributor can continue. `campaign
status` reads State and the first step under Next. State is one of: working, waiting on jobs <ids>,
ready for review, fixing <findings>, blocked on <who>: <what>.

## Doing

Round 2 at b1820e2 (A1 and A4, msg-93): A1 and A4 with their fails-without, two shards, CI.

## Next

1. Record the round's jobs below; hand back.

## Blocked on

Nothing. (Earlier: `set` lacks --force and cli.py is outside the set. Lead's ruling: no flag is
added; `set` refuses outright. The quest file says so in Contracts and A1.)

## Evidence

Local (quiet light): `tests/run.py test_file_holds` OK.

| Criterion | Evidence |
|---|---|
| A1 | 261124-120834-4b8107 (test at b1820e2); 261124-120836-b93073 (fails-without: quests.py from 4bbdcae). Round 1 at c972859: 261124-113524-83863e done, 261124-113527-3ddb5d failed as it should |
| A4 | 261124-120839-2c9188 (test at b1820e2); 261124-120841-1374fe (fails-without: state.py from 4bbdcae) |
| A2 | 261124-120842-12ee30 (shard 1 of 2), 261124-120844-3c7ebc (shard 2 of 2): 36 modules, alternating. Stale round 1 at c972859: 261124-113534-9b0609 done; 261124-113530-bb27d2 failed in test_job_reuse.Identity.test_a_job_runs_with_its_campaigns_env_and_its_own_env_wins (the queued job ran before `config env=` under load: a timing race in job env, not file holds) |
| A3 | CI on the pushed branch at the head |

## Result

`set ID files=` on a quest that holds files (`writes`) refuses a set whose `clashes` gains a
quest it did not have, naming each with the globs they share ("split (src/auth/**)"), and
changes nothing; the refusal says to narrow files or wait for the holder with needs=. New
cross-campaign overlaps warn, as `start` does. No flag or record format changes. Code:
scripts/campaign_tool/quests.py cmd_set; test: tests/test_file_holds.py.

msg-93: `clashes` (state.py) drops a holder that waits for the quest (`waits_for`): the quest is
in the holder's needs or until, or in the needs of a quest those name, transitively; an
own-campaign NAME: prefix reads as the bare id, a foreign one never matches. start, status and
set files= read `clashes`, so they agree. Test:
FileHolds.test_a_holder_that_waits_for_a_quest_never_blocks_its_start.

## Rulings I made

- Narrowing is "gains no clash": only quests that clash after the set and did not before
  refuse. A quest started over X with `start --force` keeps X while it narrows or keeps its
  files.
- waits_for walks needs from the holder's needs and until together, so a holder parked until Y,
  where Y needs the quest, is exempt too: it cannot resume before the quest lands. Drops are not
  followed (needs and until read as stored, per the lead). `after` is not an exemption: an
  after-ordered quest may write concurrently.
- A planned quest's set never refuses: it holds nothing; `start` checks it.
- The fails-without runs the same test with quests.py from the base, agent-campaign:4bbdcae.
