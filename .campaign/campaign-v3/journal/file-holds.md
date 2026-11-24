# file-holds: journal

State: ready for review
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
| A1 | <jobs below> |
| A2 | <jobs below> |
| A3 | <CI> |

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
