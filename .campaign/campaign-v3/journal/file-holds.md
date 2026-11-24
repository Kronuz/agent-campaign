# file-holds: journal

State: waiting on jobs 261124-113524-83863e 261124-113527-3ddb5d 261124-113530-bb27d2 261124-113534-9b0609
Quest: .campaign/campaign-v3/quests/file-holds.md (the lead's; never edit it)

Keep this current and commit it with the work, so a fresh contributor can continue. `campaign
status` reads State and the first step under Next. State is one of: working, waiting on jobs <ids>,
ready for review, fixing <findings>, blocked on <who>: <what>.

## Doing

Evidence round at the head: A1 and its fails-without, two suite shards, CI.

## Next

1. Record the round's jobs below; hand back.

## Blocked on

Nothing. (Earlier: `set` lacks --force and cli.py is outside the set. Lead's ruling: no flag is
added; `set` refuses outright. The quest file says so in Contracts and A1.)

## Evidence

Local (quiet light): `tests/run.py test_file_holds` OK.

| Criterion | Evidence |
|---|---|
| A1 | 261124-113524-83863e (test at c972859); 261124-113527-3ddb5d (fails-without: quests.py from 4bbdcae) |
| A2 | 261124-113530-bb27d2 (shard 1 of 2), 261124-113534-9b0609 (shard 2 of 2): 36 modules, alternating |
| A3 | CI on the pushed branch at c972859 |

## Result

`set ID files=` on a quest that holds files (`writes`) refuses a set whose `clashes` gains a
quest it did not have, naming each with the globs they share ("split (src/auth/**)"), and
changes nothing; the refusal says to narrow files or wait for the holder with needs=. New
cross-campaign overlaps warn, as `start` does. No flag or record format changes. Code:
scripts/campaign_tool/quests.py cmd_set; test: tests/test_file_holds.py.

## Rulings I made

- Narrowing is "gains no clash": only quests that clash after the set and did not before
  refuse. A quest started over X with `start --force` keeps X while it narrows or keeps its
  files.
- A planned quest's set never refuses: it holds nothing; `start` checks it.
- The fails-without runs the same test with quests.py from the base, agent-campaign:4bbdcae.
